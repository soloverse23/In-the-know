"""Cleaning the memory from chat: change tables, browse rows, and update / delete / merge / dedupe records.

Every change is snapshotted (table `trash`) and listed in schema_changes, so one Undo
restores it exactly. Bulk or destructive edits return a preview first and only apply with
confirm=true (after the owner agreed).
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Optional

from . import schema
from .normalize import fold, name_key, norm_text
from .views import N, T, block, col, view

PREVIEW_LIMIT = 20          # updates touching more rows than this need confirm=true
MAX_ROWS = 20000


class EditOps:
    """Mixin for ContextEngine (needs self.conn, self.ingestor, self.lang)."""

    # ----------------------------------------------------------------- helpers
    def _spec(self, entity: str) -> dict:
        spec = schema.entity(self.conn, entity or "")
        if spec is None:
            raise ValueError(f"unknown table {entity!r}; tables: {list(schema.entities(self.conn))}")
        return spec

    def _field(self, spec: dict, name: str) -> dict:
        f = schema.field(spec, name)
        if f is None:
            raise ValueError(f"{spec['name']} has no field {name!r}; fields: {[x['name'] for x in spec['fields']]}")
        return f

    def _ref_name_col(self, ref_entity: str) -> Optional[str]:
        rs = schema.entity(self.conn, ref_entity)
        if not rs:
            return None
        k = schema.role_field(rs, "name") or self.ingestor._custom_key(rs)
        return schema.column(k) if k else None

    def _where(self, spec: dict, where: Optional[dict]) -> tuple[str, list]:
        """Safe WHERE from a small filter language (never raw SQL)."""
        where = where or {}
        clauses, params = [], []
        allowed = {"ids", "file_id", "source", "equals", "contains", "empty", "date_from", "date_to", "min", "max", "search", "fact"}
        unknown = set(where) - allowed
        if unknown:
            raise ValueError(f"unknown filter keys {sorted(unknown)}; use {sorted(allowed)}")
        if where.get("ids"):
            ids = [int(i) for i in where["ids"]]
            clauses.append(f"id IN ({','.join('?' * len(ids))})")
            params += ids
        if where.get("file_id") is not None:
            clauses.append("_file_id = ?")
            params.append(int(where["file_id"]))
        if where.get("source"):
            src = where["source"]
            clauses.append("_source LIKE ?" if src == "file" else "_source = ?")
            params.append("file:%" if src == "file" else src)
        for name, value in (where.get("equals") or {}).items():
            f = self._field(spec, name)
            c = schema.column(f)
            if value is None:
                clauses.append(f"{c} IS NULL")
            elif f["type"] == "ref":
                nc = self._ref_name_col(f["ref"])
                clauses.append(f"{c} IN (SELECT id FROM {schema.table(f['ref'])} WHERE lower({nc}) = lower(?))")
                params.append(str(value))
            else:
                v = self.ingestor.convert(f, value) if f["type"] not in ("text", "enum") else norm_text(value)
                clauses.append(f"{c} = ?" if f["type"] != "text" else f"lower({c}) = lower(?)")
                params.append(v)
        for name, value in (where.get("contains") or {}).items():
            f = self._field(spec, name)
            if f["type"] == "ref":
                nc = self._ref_name_col(f["ref"])
                clauses.append(f"{schema.column(f)} IN (SELECT id FROM {schema.table(f['ref'])} WHERE lower({nc}) LIKE lower(?))")
            else:
                clauses.append(f"lower(CAST({schema.column(f)} AS TEXT)) LIKE lower(?)")
            params.append(f"%{value}%")
        for name in where.get("empty") or []:
            c = schema.column(self._field(spec, name))
            clauses.append(f"({c} IS NULL OR {c} = '')")
        for key, op in (("min", ">="), ("max", "<=")):
            for name, value in (where.get(key) or {}).items():
                f = self._field(spec, name)
                clauses.append(f"{schema.column(f)} {op} ?")
                params.append(self.ingestor.convert(f, value) if f["type"] in ("money", "number", "date", "time", "month") else value)
        if where.get("search"):                       # free text across every text field and linked names
            ors = []
            for f in spec["fields"]:
                if f["type"] in ("text", "enum", "phone", "email"):
                    ors.append(f"lower(CAST({schema.column(f)} AS TEXT)) LIKE lower(?)")
                    params.append(f"%{where['search']}%")
                elif f["type"] == "ref" and (nc := self._ref_name_col(f["ref"])):
                    ors.append(f"{schema.column(f)} IN (SELECT id FROM {schema.table(f['ref'])} WHERE lower({nc}) LIKE lower(?))")
                    params.append(f"%{where['search']}%")
            clauses.append("(" + (" OR ".join(ors) or "0") + ")")
        if where.get("fact"):                         # people with a matching current fact: {kind?, key?, text?, polarity?}
            if spec["role"] != "party":
                raise ValueError("the fact filter works on the people table")
            from .people import fact_where
            c, pp = fact_where(self.conn, spec["name"], where["fact"], self.today)
            clauses.append(c)
            params += pp
        df = schema.role_field(spec, "date")
        if where.get("date_from") and df:
            clauses.append(f"{schema.column(df)} >= ?")
            params.append(where["date_from"])
        if where.get("date_to") and df:
            clauses.append(f"{schema.column(df)} <= ?")
            params.append(where["date_to"])
        return (" AND ".join(clauses) or "1"), params

    def _pick_fields(self, spec: dict, fields: Optional[list[str]]) -> list[dict]:
        return [self._field(spec, n) for n in fields] if fields else spec["fields"][:9]

    def _order(self, spec: dict, sort: Optional[str]) -> str:
        if not sort:
            return "id"
        desc = sort.startswith("-")
        return f"{schema.column(self._field(spec, sort.lstrip('-')))} {'DESC' if desc else 'ASC'} NULLS LAST, id"

    def _display_rows(self, spec: dict, rows: list[dict], limit: int = 30,
                      fields: Optional[list[dict]] = None) -> tuple[list[dict], list[dict]]:
        shown = fields or spec["fields"][:9]
        cols = [{"k": "id", "raw": "id", "fmt": "number"}]
        for f in shown:
            fmt = {"money": "money", "date": "date", "number": "number"}.get(f["type"], "text")
            lab = f.get("label", {}).get(self.lang)
            cols.append({"k": f["name"], "raw": (lab[0] if isinstance(lab, list) else lab) or f["name"].replace("_", " "), "fmt": fmt})
        names: dict[str, dict] = {}
        out = []
        for r in rows[:limit]:
            d = {"id": r["id"]}
            for f in shown:
                v = r.get(schema.column(f))
                if f["type"] == "ref" and v is not None:
                    cache = names.setdefault(f["ref"], {})
                    if v not in cache:
                        nc = self._ref_name_col(f["ref"])
                        hit = self.conn.execute(f"SELECT {nc} AS n FROM {schema.table(f['ref'])} WHERE id=?", (v,)).fetchone() if nc else None
                        cache[v] = hit["n"] if hit else f"#{v}"
                    v = cache[v]
                d[f["name"]] = v
            out.append(d)
        return cols, out

    # ----------------------------------------------------------------- browse
    def find_records(self, entity: str, where: Optional[dict] = None, fields: Optional[list[str]] = None,
                     sort: Optional[str] = None, limit: int = 10, count_by: Optional[str] = None) -> str:
        """Quiet, server-side answer to a question about any table: only matching rows, only the asked fields."""
        spec = self._spec(entity)
        w, p = self._where(spec, where)
        tbl = schema.table(entity)
        total = self.conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {w}", p).fetchone()[0]
        if count_by:
            f = self._field(spec, count_by)
            c = schema.column(f)
            if f["type"] == "ref" and (nc := self._ref_name_col(f["ref"])):
                sql = (f"SELECT k, COUNT(*) AS n FROM (SELECT (SELECT {nc} FROM {schema.table(f['ref'])} WHERE id = {tbl}.{c}) AS k "
                       f"FROM {tbl} WHERE {w}) GROUP BY k ORDER BY n DESC LIMIT 30")
            else:
                sql = f"SELECT {c} AS k, COUNT(*) AS n FROM {tbl} WHERE {w} GROUP BY k ORDER BY n DESC LIMIT 30"
            groups = self.conn.execute(sql, p).fetchall()
            return f"{total} matching {entity} rows, by {count_by}: " + ", ".join(f"{g['k'] if g['k'] is not None else '(empty)'}: {g['n']}" for g in groups)
        limit = max(1, min(limit, 50))
        rows = [dict(r) for r in self.conn.execute(f"SELECT * FROM {tbl} WHERE {w} ORDER BY {self._order(spec, sort)} LIMIT ?", [*p, limit])]
        if not rows:
            return f"No {entity} rows match. Fields: " + ", ".join(f["name"] for f in spec["fields"])
        picked = self._pick_fields(spec, fields)
        _, disp = self._display_rows(spec, rows, limit, picked)
        money = {f["name"] for f in picked if f["type"] == "money"}

        def cell(k, v):
            if v is None or v == "":
                return "-"
            if k in money and isinstance(v, (int, float)):
                return self.money(v)
            s = str(v).replace("\n", " ").replace("|", "/")
            return s if len(s) <= 140 else s[:137] + "..."
        keys = ["id"] + [f["name"] for f in picked]
        out = [f"{total} matching {entity} rows" + (f" (showing {len(disp)}; narrow with where/sort or use count_by)" if total > len(disp) else "") + ":",
               "| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
        out += ["| " + " | ".join(cell(k, d.get(k)) for k in keys) + " |" for d in disp]
        out.append("Answer in your own words. To show these to the owner as a card: browse_records with where={ids:[...]} and the same fields.")
        return "\n".join(out)

    def browse_records(self, entity: str, where: Optional[dict] = None, limit: int = 30,
                       fields: Optional[list[str]] = None, sort: Optional[str] = None) -> dict:
        spec = self._spec(entity)
        w, p = self._where(spec, where)
        tbl = schema.table(entity)
        total = self.conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {w}", p).fetchone()[0]
        rows = [dict(r) for r in self.conn.execute(f"SELECT * FROM {tbl} WHERE {w} ORDER BY {self._order(spec, sort)} LIMIT ?",
                                                    [*p, max(1, min(limit, 200))])]
        cols, disp = self._display_rows(spec, rows, limit, self._pick_fields(spec, fields))
        return view("browse_records", {"raw": schema.label(spec, self.lang, True)},
                    [block("kv", items=[{"t": "kv.rows_found", "v": total, "fmt": "number"}]),
                     block("table", columns=cols, rows=disp, more=max(0, total - len(disp)))],
                    subtitle=T("sub.browse", n=N(total)),
                    hints=["Fields: " + ", ".join(f"{f['name']}:{f['type']}" + (f"->{f['ref']}" if f["type"] == "ref" else "") for f in spec["fields"]),
                           "Clean with edit_records (update / delete / merge / dedupe) and change_schema (rename_field / remove_field / change_field / remove_entity)."])

    # ----------------------------------------------------------- schema edits
    def _schema_change(self, action: str, entity: Optional[str], field: Any, new_name: Optional[str], label: Optional[dict],
                       new_type: Optional[str], ref: Optional[str], confirm: bool) -> tuple[int, str]:
        fname = field.get("name") if isinstance(field, dict) else field
        if action == "remove_field":
            spec = self._spec(entity)
            f = self._field(spec, fname)
            n = self.conn.execute(f"SELECT COUNT(*) FROM {schema.table(entity)} WHERE {schema.column(f)} IS NOT NULL").fetchone()[0]
            if n and not confirm:
                raise PreviewNeeded(T("edit.preview_remove_field", field=fname, n=N(n)))
            return schema.remove_field(self.conn, entity, fname), fname
        if action == "rename_field":
            self._field(self._spec(entity), fname)
            return schema.rename_field(self.conn, entity, fname, new_name, label), new_name or fname
        if action == "relabel_entity":
            self._spec(entity)
            if not label:
                raise ValueError("relabel_entity needs label {en,fr,ro,de: [singular, plural]}")
            return schema.relabel_entity(self.conn, entity, label), entity
        if action == "remove_entity":
            spec = self._spec(entity)
            n = self.conn.execute(f"SELECT COUNT(*) FROM {schema.table(entity)}").fetchone()[0]
            if n and not confirm:
                raise PreviewNeeded(T("edit.preview_remove_entity", entity=schema.label(spec, self.lang, True), n=N(n)))
            return schema.remove_entity(self.conn, entity), entity
        if action == "change_field":
            return self._change_field(entity, fname, new_type, ref, confirm), fname
        raise ValueError("unknown action")

    def _change_field(self, entity: str, fname: str, new_type: Optional[str], ref: Optional[str], confirm: bool) -> int:
        spec = self._spec(entity)
        old = self._field(spec, fname)
        if new_type not in schema.FIELD_TYPES:
            raise ValueError(f"new_type must be one of {sorted(schema.FIELD_TYPES)}")
        new = {k: v for k, v in old.items() if k not in ("type", "ref", "role_attr", "values", "value_synonyms", "sub")}
        new["type"] = new_type
        if new_type == "ref":
            target = self._spec(ref)
            new["ref"] = ref
            role_map = {"offering": "offering", "party": "party", "staff": "staff"}
            attr = role_map.get(target["role"])
            if spec["role"] == "transaction" and attr and not schema.role_field(spec, attr):
                new["role_attr"] = attr
        elif old.get("role_attr") and schema.ROLES[spec["role"]].get(old["role_attr"], ("",))[0] == new_type:
            new["role_attr"] = old["role_attr"]
        schema.validate_field(new, set(schema.entities(self.conn)), spec["role"])
        rows = self.conn.execute(f"SELECT id, {schema.column(old)} AS v FROM {schema.table(entity)} WHERE {schema.column(old)} IS NOT NULL").fetchall()
        if rows and not confirm:
            raise PreviewNeeded(T("edit.preview_change_field", field=fname, n=N(len(rows)), type=new_type))
        values, failed = {}, 0
        target = schema.entity(self.conn, ref) if new_type == "ref" else None
        for r in rows:
            if new_type == "ref":
                v = self.ingestor.resolve_ref(target, {"name": r["v"]}, None, from_transaction=False) if str(r["v"]).strip() else None
            else:
                v = self.ingestor.convert(new, r["v"])
            if v is None:
                failed += 1
            values[r["id"]] = v
        change = schema.replace_field(self.conn, entity, old, new, values, "assistant")
        if failed:
            self.conn.execute("UPDATE schema_changes SET payload=json_set(payload, '$.failed', ?) WHERE id=?", (failed, change))
        return change

    # ------------------------------------------------------------ record edits
    def edit_records(self, entity: str, action: str, where: Optional[dict] = None, set: Optional[dict] = None,
                     keep: Optional[str] = None, duplicate: Optional[str] = None, confirm: bool = False,
                     person: Optional[str] = None, include_sensitive: bool = True) -> dict:
        import builtins
        updates = set                       # the tool argument is called `set`; keep the builtin usable below
        self._require_setup()
        spec = self._spec(entity)
        tbl = schema.table(entity)
        if action in ("forget_person", "export_person"):
            if spec["role"] != "party":
                raise ValueError(f"{action} works on the people table ({schema.primary(self.conn, 'party')['name']})")
            who = person or keep
            if not who:
                raise ValueError(f"{action} needs `person` (name, phone or id)")
            return self.forget_person(who, confirm) if action == "forget_person" else self.export_person(who, include_sensitive)
        if action not in ("update", "delete", "merge", "dedupe"):
            raise ValueError("action: update | delete | merge | dedupe | forget_person | export_person")
        if action in ("update", "delete") and not where:
            raise ValueError("update/delete need a `where` filter (ids, file_id, source, equals, contains, empty, date_from, date_to)")
        snap: dict[str, list[dict]] = defaultdict(list)
        seen: "builtins.set[tuple[str, int]]" = builtins.set()
        # aliases before the change: a merge moves them, and undo must give the duplicate its spellings/phones back
        aliases = [dict(r) for r in self.conn.execute("SELECT * FROM aliases WHERE entity=?", (entity,))] if action in ("merge", "dedupe") else []

        def take(t: str, rid: int) -> None:
            if (t, rid) not in seen:
                seen.add((t, rid))
                r = self.conn.execute(f"SELECT * FROM {t} WHERE id=?", (rid,)).fetchone()
                if r:
                    snap[t].append(dict(r))

        if action in ("update", "delete"):
            w, p = self._where(spec, where)
            rows = [dict(r) for r in self.conn.execute(f"SELECT * FROM {tbl} WHERE {w} LIMIT {MAX_ROWS}", p)]
            if not rows:
                return view("edit_records", T("view.edit"), [block("text", **T("edit.nothing"))])
            if action == "delete":
                refs = self._references_to(spec["name"], [r["id"] for r in rows])
                if refs:
                    raise ValueError(f"these rows are used by {refs}; merge them into another record or delete those first")
            if (action == "delete" or len(rows) > PREVIEW_LIMIT) and not confirm:
                return self._preview(spec, rows, action, updates)
            maxids = self._max_ids()
            for r in rows:
                take(tbl, r["id"])
            if action == "delete":
                self.conn.execute(f"DELETE FROM {tbl} WHERE id IN ({','.join('?' * len(rows))})", [r["id"] for r in rows])
            else:
                self._apply_update(spec, rows, updates or {})
            n = len(rows)
            inserted = self._new_ids(maxids)
        elif action == "merge":
            a, b = self._find_record(spec, keep), self._find_record(spec, duplicate)
            if a == b:
                raise ValueError("keep and duplicate are the same record")
            if not confirm:
                rows = [dict(self.conn.execute(f"SELECT * FROM {tbl} WHERE id=?", (i,)).fetchone()) for i in (a, b)]
                return self._preview(spec, rows, "merge", None)
            self._merge(spec, a, b, take)
            n, inserted = 2, {}
        else:   # dedupe
            groups = self._duplicate_groups(spec)
            if not groups:
                return view("edit_records", T("view.edit"), [block("text", **T("edit.no_duplicates"))])
            if not confirm:
                rows = [dict(self.conn.execute(f"SELECT * FROM {tbl} WHERE id=?", (i,)).fetchone()) for g in groups for i in g]
                return self._preview(spec, rows, "dedupe", None, groups=len(groups))
            for g in groups:
                for dup in g[1:]:
                    self._merge(spec, g[0], dup, take)
            n, inserted = sum(len(g) - 1 for g in groups), {}
        change = self.conn.execute("INSERT INTO schema_changes(op, payload, status, origin) VALUES ('edit_records', ?, 'applied', 'assistant')",
                                   (json.dumps({"entity": entity, "action": action, "rows": n}),)).lastrowid
        schema.snapshot(self.conn, change, dict(snap), inserted=inserted, aliases=aliases)
        self.conn.commit()
        return view("edit_records", T("view.edit"),
                    [block("feed", items=[{"kind": "record", "status": "saved", **T(f"edit.done_{action}", n=N(n), entity=schema.label(spec, self.lang, True)),
                                           "undo": str(change)}])],
                    hints=[f"Applied as change #{change}; undo_change('{change}') reverts it exactly."])

    def _preview(self, spec: dict, rows: list[dict], action: str, set_: Optional[dict], groups: Optional[int] = None) -> dict:
        cols, disp = self._display_rows(spec, rows, 15)
        msg = T(f"edit.preview_{action}", n=N(len(rows)), groups=N(groups or 0), entity=schema.label(spec, self.lang, True))
        return view("edit_records", T("view.edit_preview"),
                    [block("alerts", items=[{**msg, "level": "warn"}]), block("table", columns=cols, rows=disp, more=max(0, len(rows) - 15))],
                    hints=["NOTHING CHANGED YET. Show the owner what will change and ask; if they agree, call edit_records again with the "
                           "same arguments plus confirm=true." + (f" Changes: {json.dumps(set_, ensure_ascii=False)}" if set_ else "")])

    def _apply_update(self, spec: dict, rows: list[dict], set_: dict) -> None:
        if not set_:
            raise ValueError("update needs `set` {field: value} or {field: {from_field: other_field}}")
        tbl = schema.table(spec["name"])
        for name, value in set_.items():
            f = self._field(spec, name)
            c = schema.column(f)
            for r in rows:
                raw = value
                if isinstance(value, dict) and "from_field" in value:
                    src = self._field(spec, value["from_field"])
                    raw = r.get(schema.column(src))
                    if src["type"] == "ref" and raw is not None:
                        nc = self._ref_name_col(src["ref"])
                        raw = self.conn.execute(f"SELECT {nc} FROM {schema.table(src['ref'])} WHERE id=?", (raw,)).fetchone()[0]
                if raw is None or raw == "":
                    v = None
                elif f["type"] == "ref":
                    v = self.ingestor.resolve_ref(schema.entity(self.conn, f["ref"]), {"name": raw}, None, from_transaction=False)
                else:
                    v = self.ingestor.convert(f, raw)
                    if v is None:
                        raise ValueError(f"cannot read {raw!r} as {f['type']} for {name}")
                self.conn.execute(f"UPDATE {tbl} SET {c}=? WHERE id=?", (v, r["id"]))

    def _references_to(self, entity: str, ids: list[int]) -> dict[str, int]:
        out = {}
        for ent, s in schema.entities(self.conn).items():
            for f in s["fields"]:
                if f["type"] == "ref" and f.get("ref") == entity:
                    n = self.conn.execute(f"SELECT COUNT(*) FROM {schema.table(ent)} WHERE {schema.column(f)} IN ({','.join('?' * len(ids))})", ids).fetchone()[0]
                    if n:
                        out[ent] = out.get(ent, 0) + n
        return out

    def _find_record(self, spec: dict, q: Optional[str]) -> int:
        if q is None:
            raise ValueError("merge needs `keep` and `duplicate` (record id or name)")
        if str(q).isdigit():
            r = self.conn.execute(f"SELECT id FROM {schema.table(spec['name'])} WHERE id=?", (int(q),)).fetchone()
            if r:
                return r["id"]
        if spec["role"] == "party":
            hit = self.find_party(str(q))
            if isinstance(hit, int):
                return hit
            if isinstance(hit, list):
                raise ValueError(f"{q!r} matches several: {', '.join(f'#{x['id']} {x['name']}' for x in hit)}; use the id")
        key = schema.role_field(spec, "name") or self.ingestor._custom_key(spec)
        if key:
            hits = [r["id"] for r in self.conn.execute(f"SELECT id, {schema.column(key)} AS k FROM {schema.table(spec['name'])}")
                    if r["k"] and name_key(r["k"]) == name_key(str(q))]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise ValueError(f"{q!r} matches ids {hits}; use the id")
        raise ValueError(f"no {spec['name']} record {q!r}")

    def _merge(self, spec: dict, keep: int, dup: int, take) -> None:
        tbl = schema.table(spec["name"])
        take(tbl, keep)
        take(tbl, dup)
        for ent, s in schema.entities(self.conn).items():
            for f in s["fields"]:
                if f["type"] == "ref" and f.get("ref") == spec["name"]:
                    t = schema.table(ent)
                    for r in self.conn.execute(f"SELECT id FROM {t} WHERE {schema.column(f)}=?", (dup,)).fetchall():
                        take(t, r["id"])
                    self.conn.execute(f"UPDATE {t} SET {schema.column(f)}=? WHERE {schema.column(f)}=?", (keep, dup))
        a = dict(self.conn.execute(f"SELECT * FROM {tbl} WHERE id=?", (keep,)).fetchone())
        b = dict(self.conn.execute(f"SELECT * FROM {tbl} WHERE id=?", (dup,)).fetchone())
        upd = {}
        for f in spec["fields"]:
            c = schema.column(f)
            if a.get(c) in (None, "") and b.get(c) not in (None, ""):
                upd[c] = b[c]
            elif f.get("role_attr") == "notes" and a.get(c) and b.get(c) and fold(b[c]) not in fold(a[c]):
                upd[c] = f"{a[c]}\n{b[c]}"
        if upd:
            self.conn.execute(f"UPDATE {tbl} SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", [*upd.values(), keep])
        self.conn.execute("UPDATE OR IGNORE aliases SET record_id=? WHERE entity=? AND record_id=?", (keep, spec["name"], dup))
        self.conn.execute("DELETE FROM aliases WHERE entity=? AND record_id=?", (spec["name"], dup))
        self.conn.execute(f"DELETE FROM {tbl} WHERE id=?", (dup,))
        if spec["role"] == "party":
            for r in self.conn.execute("SELECT id FROM contact_log WHERE party_id=?", (dup,)).fetchall():
                take("contact_log", r["id"])
            self.conn.execute("UPDATE contact_log SET party_id=? WHERE party_id=?", (keep, dup))
            self._move_person_links(spec["name"], keep, dup, take)

    def _duplicate_groups(self, spec: dict) -> list[list[int]]:
        tbl = schema.table(spec["name"])
        key = schema.role_field(spec, "name") or self.ingestor._custom_key(spec)
        groups: dict[str, list[int]] = defaultdict(list)
        if key and spec["role"] not in ("transaction", "cost"):
            for r in self.conn.execute(f"SELECT id, {schema.column(key)} AS k FROM {tbl} ORDER BY id"):
                if r["k"]:
                    groups[name_key(r["k"])].append(r["id"])
        else:
            cols = [schema.column(f) for f in spec["fields"]]
            for r in self.conn.execute(f"SELECT id, {', '.join(cols)} FROM {tbl} ORDER BY id"):
                groups[json.dumps([r[c] for c in cols], default=str)].append(r["id"])
        return [g for g in groups.values() if len(g) > 1]

    def _max_ids(self) -> dict[str, int]:
        return {schema.table(n): self.conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {schema.table(n)}").fetchone()[0] for n in schema.entities(self.conn)}

    def _new_ids(self, before: dict[str, int]) -> dict[str, list[int]]:
        out = {}
        for t, m in before.items():
            ids = [r[0] for r in self.conn.execute(f"SELECT id FROM {t} WHERE id > ?", (m,))]
            if ids:
                out[t] = ids
        return out


class PreviewNeeded(Exception):
    def __init__(self, msg: dict):
        super().__init__(msg.get("t"))
        self.msg = msg
