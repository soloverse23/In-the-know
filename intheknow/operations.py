"""Write side: everything that flows from the chat into the business memory.

Channels: files (add_data), photos and other assistant-extracted rows (import_records),
conversation facts (capture), schema evolution (change_schema), design (update_design),
custom metrics (define_metric) and housekeeping (merge, questions, contacts, undo).
Every write is validated in code, deduplicated, and reported as a view the UI can render.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Optional

from . import design, schema
from . import mapping as mp
from . import metrics as M
from .db import get_setting, set_setting
from .ingest import IngestReport, Ingestor
from .normalize import display_name, fold, infer_date_order, name_key, norm_text, parse_date, parse_money, parse_month, phone_key
from .views import schema_field_view, D, M as MV, N, T, block, view

CAPTURE_TYPES = ("transaction", "customer_note", "customer_info", "offering", "expense", "goal", "policy", "fact",
                 "decision", "staff", "record", "task", "relation")
LOCAL_SOURCES = {"chat", "history", "photo", "file", "list"}
SYNC_LABELS = {"calendar": "Calendar", "google_calendar": "Google Calendar", "outlook_calendar": "Outlook Calendar",
               "gmail": "Gmail", "outlook": "Outlook", "email": "Email", "square": "Square", "shopify": "Shopify",
               "stripe": "Stripe", "sumup": "SumUp", "paypal": "PayPal", "quickbooks": "QuickBooks", "whatsapp": "WhatsApp",
               "calendly": "Calendly", "fresha": "Fresha", "booksy": "Booksy", "doctolib": "Doctolib", "treatwell": "Treatwell"}


def sync_source(value) -> Optional[str]:
    """The connector an item came from ('square', 'gmail'...), or None for the owner's own words, photos, old chats."""
    v = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")[:30]
    v = v.removeprefix("sync_")
    return v if v and v not in LOCAL_SOURCES and not v.startswith(("photo", "list")) else None


def sync_label(src: str) -> str:
    return SYNC_LABELS.get(src, src.replace("_", " ").title())


def _fingerprint(it: dict) -> str:
    keep = {k: v for k, v in it.items() if k not in ("quote", "source", "external_id")}
    return hashlib.sha1(json.dumps(keep, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()[:16]


TYPE_ALIASES = {"visit": "transaction", "sale": "transaction", "appointment": "transaction", "order": "transaction",
                "service": "offering", "product": "offering", "price": "offering", "cost": "expense", "note": "customer_note",
                "customer": "customer_info", "rule": "policy", "team": "staff", "reminder": "task", "follow_up": "task",
                "meeting": "task", "promise": "task", "receivable": "task", "todo": "task", "relationship": "relation", "link": "relation",
                "person_fact": "fact"}
KNOWLEDGE_KINDS = {"goal": "Goal", "policy": "Rule", "fact": "Fact", "staff": "Team"}


def E(role_or_entity: str, en: str) -> dict:
    """Entity label param: rendered in the owner's language by the UI, English for the model."""
    return {"$e": role_or_entity, "en": en}


class WriteOps:
    """Mixin for ContextEngine."""

    @property
    def ingestor(self) -> Ingestor:
        if getattr(self, "_ingestor", None) is None:
            self._ingestor = Ingestor(self.conn)
        return self._ingestor

    # ---------------------------------------------------------------- setup
    def setup_business(self, name: str, vertical: str, locale: str = "en", currency: str = "EUR", country: str = "RO",
                       city: Optional[str] = None) -> dict:
        from .verticals import PACKS, pack
        if locale not in schema.LANGS:
            raise ValueError(f"locale must be one of {schema.LANGS}")
        vertical = vertical if vertical in PACKS else "generic"
        for k, v in (("name", norm_text(name)), ("vertical", vertical), ("locale", locale), ("currency", currency.upper()),
                     ("country", country.upper()), ("city", city or "")):
            set_setting(self.conn, k, v)
        changes = schema.apply_pack(self.conn, pack(vertical)) if not schema.entities(self.conn) else []
        from .people import ensure_vocab
        ensure_vocab(self.conn, pack(vertical))
        if get_setting(self.conn, "consent_required") is None:
            set_setting(self.conn, "consent_required", "1" if pack(vertical).get("consent_required") else "0")
        if not get_setting(self.conn, "design"):
            set_setting(self.conn, "design", json.dumps(design.default_spec(pack(vertical)), ensure_ascii=False))
        self.conn.commit()
        v = self.get_started()
        v["blocks"].insert(1, self._schema_block(T("setup.schema_title"), changes))
        return v

    def _schema_block(self, title, changes: Optional[list] = None) -> dict:
        ents = [{"name": n, "role": s["role"], "label": schema.label(s, self.lang, True),
                 "fields": [schema_field_view(f) for f in s["fields"]]}
                for n, s in schema.entities(self.conn).items()]
        return block("schema", title=title, entities=ents, changes=[{"id": c, "op": "pack", "status": "applied"} for c in (changes or [])][:0])

    # ---------------------------------------------------------------- files
    def add_data(self, paths: Optional[list[str]] = None, filename: Optional[str] = None, content: Optional[str] = None) -> dict:
        self._require_setup()
        reports: list[IngestReport] = []
        errors = []
        for p in paths or []:
            try:
                reports += self.ingestor.add_path(p)
            except FileNotFoundError:
                errors.append({**T("err.path_not_found", path=p), "level": "warn"})
            except Exception as exc:  # noqa: BLE001
                errors.append({**T("err.read_failed", path=p, msg=str(exc)), "level": "warn"})
        if content:
            reports.append(self.ingestor.add_content(filename or "chat_upload.csv", content))
        if not reports and not errors:
            raise ValueError("give `paths` (local files/folders) or `content` + `filename`")
        return self._files_view(reports, errors)

    def upload_card(self, url: str, minutes: int = 30) -> dict:
        """Card with the owner's upload page link (hosted connector)."""
        self._require_setup()
        return view("upload_files", T("view.upload"), [block("upload", url=url, minutes=minutes)],
                    subtitle=T("sub.upload", n=N(minutes)),
                    hints=[f"Upload page for the owner (works for {minutes} minutes, in the first browser that opens it): {url}",
                           "Put this link in your reply as a clickable link (the card also has a button). On that page they add CSV, "
                           "Excel or notes files, or a Claude/ChatGPT export .zip, from their phone, computer or Google Drive. "
                           "When they say they are done, call get_context to see what arrived, then go through open questions "
                           "(get_data_status) and, if old chats were added, import_chat_history()."])

    def import_uploads(self, files: list[tuple[str, bytes]]) -> dict:
        """Files the owner sent from the upload page or picked in their Drive: data files and old-chat exports."""
        self._require_setup()
        from .onboarding import is_chat_export
        data, chats, chat_files = [], 0, 0
        for name, raw in files:
            if is_chat_export(name, raw):
                chats += self.load_chat_export(name, raw)
                chat_files += 1
            else:
                data.append((name, raw))
        v = self._files_view(self.ingestor.add_files(data) if data else [], [])
        v["chat_messages"], v["chat_files"] = chats, chat_files
        if chat_files:
            self._mark_used("history")
            v["hints"].append(f"{chats} new messages from the owner's old chats are queued: call import_chat_history() "
                              "(no path) to work through them in batches.")
        return v

    def import_records(self, entity: str, rows: list[dict], source: str = "photo") -> dict:
        """Rows the assistant extracted itself (photo of a notebook/receipt/price list, a pasted list...)."""
        self._require_setup()
        if not rows:
            raise ValueError("rows is empty")
        sync = sync_source(source)
        if not sync:
            src = source if source.startswith(("photo", "chat", "history", "list")) else f"photo:{source}"
            return self._files_view([self.ingestor.import_records(entity, rows, src)], [])
        # rows from another connector: each may carry external_id; rows already synced are skipped
        kind = f"rows:{entity}"
        fresh, ids, known = [], [], 0
        for r in rows:
            r = dict(r)
            eid = str(r.pop("external_id", "") or "").strip()[:200]
            if eid and self.conn.execute("SELECT 1 FROM external_refs WHERE source=? AND external_id=? AND kind=?",
                                         (sync, eid, kind)).fetchone():
                known += 1
                continue
            fresh.append(r)
            if eid:
                ids.append(eid)
        if fresh:
            report = self.ingestor.import_records(entity, fresh, f"sync:{sync}")
        else:
            from .ingest import IngestReport
            report = IngestReport(f"sync:{sync}", entity)
        report.rows_duplicate += known
        report.rows_total += known
        if known:
            report.notes.append(f"{known} already synced from {sync_label(sync)}")
        self.conn.executemany("INSERT OR IGNORE INTO external_refs(source, external_id, kind) VALUES (?,?,?)",
                              [(sync, e, kind) for e in ids])
        set_setting(self.conn, f"sync_last:{sync}", f"{self.today.isoformat()}|{len(rows)}")
        self.conn.commit()
        return self._files_view([report], [])

    def _files_view(self, reports: list[IngestReport], errors: list[dict]) -> dict:
        items = []
        for r in reports:
            spec = schema.entity(self.conn, r.entity) if r.entity not in ("unknown", "skipped", "knowledge") else None
            items.append({
                "name": r.file, "file_id": r.file_id, "entity": r.entity,
                "entity_label": {lg: schema.label(spec, lg, True) for lg in schema.LANGS} if spec else None,
                "already": r.already, "total": r.rows_total, "loaded": r.rows_loaded, "updated": r.rows_updated,
                "duplicate": r.rows_duplicate, "flagged": r.rows_flagged, "date_range": r.date_range, "date_order": r.date_order,
                "questions": r.questions, "notes": r.notes[:5],
                "mapping": [self._target_label(spec, h, t) for h, t in r.mapping.items()] if spec else [],
                "new_fields": r.new_fields, "headers": r.headers if r.entity == "unknown" else None,
                "sample": r.sample if r.entity == "unknown" else None,
            })
        hints = ["Check each column mapping; fix wrong ones with map_columns(file_id, entity, columns)."]
        if any(i["entity"] == "unknown" for i in items):
            hints.append("Unrecognized table: decide what one row is. If it is a new kind of record, create it with change_schema, "
                         "then call map_columns. Entities:\n" + schema.describe(self.conn))
        if any(i["new_fields"] for i in items):
            hints.append("New fields were added automatically (undo_change with their id if not wanted).")
        n_q = self.conn.execute("SELECT COUNT(*) FROM questions WHERE status='open'").fetchone()[0]
        if n_q:
            hints.append(f"{n_q} open data questions: get_data_status, then ask the owner the important ones.")
        self._mark_used("import")
        blocks = ([block("alerts", items=errors)] if errors else []) + [block("files", items=items)]
        return view("add_data", T("view.import"), blocks, subtitle=T("sub.import", files=N(len(items)),
                                                                      rows=N(sum(i["loaded"] + i["updated"] for i in items))), hints=hints)

    def _target_label(self, spec: dict, header: str, target: Optional[str]) -> dict:
        """Describe a column mapping so the UI can show it in the owner's language."""
        out = {"h": header, "target": target or "ignore"}
        if not target or target in ("extra", "ignore"):
            return out
        base, _, sub = target.partition(".")
        f = schema.field(spec, base)
        if f is None:
            return out
        if f["type"] == "ref":
            ref = schema.entity(self.conn, f["ref"])
            out["label"] = {lg: schema.label(ref, lg) for lg in schema.LANGS} if ref else None
            if sub and sub != "name":
                out["sub"] = f"sub.{sub}"
        elif f.get("role_attr"):
            out["tk"] = f"attr.{f['role_attr']}"
            if sub:
                out["sub"] = f"sub.{sub}"
        else:
            lab = f.get("label", {})
            out["label"] = {lg: (lab.get(lg) or [header])[0] if isinstance(lab.get(lg), list) else (lab.get(lg) or header) for lg in schema.LANGS}
        return out

    def map_columns(self, file_id: int, entity: str, columns: dict[str, str]) -> dict:
        self._require_setup()
        row = self.conn.execute("SELECT name, kind, content_text FROM raw_files WHERE id=?", (file_id,)).fetchone()
        if not row or row["kind"] != "table":
            raise ValueError(f"no table file with file_id {file_id}; see get_data_status")
        spec = schema.entity(self.conn, entity)
        if spec is None:
            raise ValueError(f"unknown entity {entity}; existing: {list(schema.entities(self.conn))}")
        from .ingest import table_from_text
        headers, records = table_from_text(row["content_text"])
        norm = {norm_text(k): v for k, v in columns.items()}
        unknown = [k for k in norm if k not in headers]
        if unknown:
            raise ValueError(f"not columns of this file: {unknown}; file columns: {headers}")
        valid = set(mp.targets(spec, self.conn))
        cols: dict[str, Optional[str]] = {}
        for h in headers:
            tgt = (norm.get(h) or "extra").strip()
            if tgt == "ignore":
                cols[h] = None
            elif tgt in ("extra", "new") and schema.field(spec, mp.field_name_for({"fields": []}, h)) is not None:
                cols[h] = mp.field_name_for({"fields": []}, h)          # that field already exists: just use it
            elif tgt in ("extra", "new"):
                vals = [r.get(h) for r in records]
                fname = mp.field_name_for(spec, h)
                schema.add_field(self.conn, entity, {"name": fname, "type": mp.infer_type(vals), "synonyms": [h],
                                                     "label": {lg: [h, h] for lg in schema.LANGS}}, origin="assistant")
                spec = schema.entity(self.conn, entity)
                cols[h] = fname
            elif tgt in valid:
                cols[h] = tgt
            elif schema.field(spec, tgt) and schema.field(spec, tgt)["type"] == "ref":
                cols[h] = f"{tgt}.name"
            else:
                raise ValueError(f"{tgt!r} is not a target of {entity}. Valid: {sorted(valid)} or extra/new/ignore")
        mapped = {t for t in cols.values() if t}
        if not mp.required_covered(spec, mapped):
            raise ValueError("required fields not mapped: " + ", ".join(f["name"] for f in spec["fields"]
                                                                         if f.get("role_attr") and schema.ROLES[spec["role"]].get(f["role_attr"], ("", False))[1]))
        for h, t in cols.items():                      # learn: this header means this target from now on
            base = (t or "").split(".")[0]
            f = schema.field(spec, base) if t else None
            if f and f["type"] != "ref" and "." not in t and h not in f.get("synonyms", []):
                f.setdefault("synonyms", []).append(h)
        self.conn.execute("UPDATE entity_types SET spec=? WHERE name=?", (json.dumps(spec, ensure_ascii=False), entity))
        report = self.ingestor.apply_mapping(file_id, entity, cols)
        mp.remember(self.conn, headers, mp.Mapping(entity, cols, "assistant"))
        self.conn.commit()
        v = self._files_view([report], [])
        v["hints"].append("This column layout is remembered: next exports with the same columns import automatically.")
        return v

    # -------------------------------------------------------------- capture
    def capture(self, items: list[dict], dry_run: bool = False) -> dict:
        self._require_setup()
        if not items:
            raise ValueError("no items")
        order = {"offering": 0, "staff": 1, "customer_info": 2, "relation": 3}
        norm_items = []
        for it in items:
            it = {k: v for k, v in dict(it).items() if v not in (None, "", {})}
            it["type"] = TYPE_ALIASES.get(it.get("type"), it.get("type"))
            norm_items.append(it)
        norm_items.sort(key=lambda it: order.get(it.get("type"), 5))
        out = []
        synced: dict[str, int] = {}
        for it in norm_items:
            kind = it.get("type")
            src = sync_source(it.get("source"))
            try:
                if kind not in CAPTURE_TYPES:
                    res = {"status": "error", **T("err.capture_type", type=str(kind))}
                elif src and it.get("external_id"):
                    res = self._cap_synced(it, kind, src)
                else:
                    res = getattr(self, f"_cap_{kind}")(it)
            except Exception as exc:  # noqa: BLE001
                res = {"status": "error", **T("err.generic", msg=str(exc))}
            if src:
                res["chips"] = list(res.get("chips") or []) + [{"v": sync_label(src), "icon": "sync"}]
                if res["status"] in ("saved", "known"):
                    synced[src] = synced.get(src, 0) + 1
            out.append({"kind": kind, **res})
        if dry_run:                                    # preview: nothing is kept
            self.conn.rollback()
            self._mark_clean()
            for o in out:
                o.pop("undo", None)
                if o["status"] == "saved":
                    o["status"] = "ready"
            n_ready = sum(o["status"] == "ready" for o in out)
            return view("capture", T("view.capture_preview"), [block("feed", items=out)],
                        subtitle=T("sub.capture_preview", n=N(n_ready), total=N(len(out))),
                        hints=["PREVIEW ONLY: nothing was saved. Summarise it for the owner in one or two lines (counts per kind, "
                               "anything unusual) and ask. If they agree, call capture again with the same items and dry_run=false. "
                               "Items marked known are already in memory."])
        for src, n in synced.items():
            set_setting(self.conn, f"sync_last:{src}", f"{self.today.isoformat()}|{n}")
        self.conn.commit()
        if any(o["status"] == "saved" for o in out):
            self._mark_used("capture")
        hints = []
        if any(o["status"] == "clarify" for o in out):
            hints.append("Ask the owner the clarification items in ONE short question, then capture again with the missing details.")
        conflicts = [o["question"] for o in out if o.get("question")]
        if conflicts:
            hints.append(f"Changed facts were NOT saved yet (question ids {conflicts}): ask the owner, then resolve_question('confirm', "
                         f"question_ids={conflicts}) keeps the new value (the old one stays as history), 'dismiss' keeps the old one.")
        hints.append("Confirm briefly in one line; the card shows what was saved (each item has an Undo button).")
        n_saved = sum(o["status"] == "saved" for o in out)
        return view("capture", T("view.capture"), [block("feed", items=out)], subtitle=T("sub.capture", n=N(n_saved), total=N(len(out))), hints=hints)

    def _cap_synced(self, it: dict, kind: str, src: str) -> dict:
        """An item from another connector: skipped if already synced unchanged, replaced if it changed (e.g. a moved event)."""
        eid = str(it["external_id"]).strip()[:200]
        fp = _fingerprint(it)
        prev = self.conn.execute("SELECT fingerprint, ref FROM external_refs WHERE source=? AND external_id=? AND kind=?",
                                 (src, eid, kind)).fetchone()
        if prev is not None and prev["fingerprint"] == fp:
            return {"status": "known", **T("cap.already_synced", source=sync_label(src))}
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN")                  # a bare SAVEPOINT would start (and RELEASE commit) its own transaction
        self.conn.execute("SAVEPOINT sync_item")
        replaced = False
        if prev is not None and prev["ref"]:
            self.undo_change(prev["ref"])               # the app changed it: the old version goes, the new one is saved
            replaced = True
        res = getattr(self, f"_cap_{kind}")(it)
        if res["status"] not in ("saved", "known"):
            self.conn.execute("ROLLBACK TO sync_item")  # nothing half-replaced
            self.conn.execute("RELEASE sync_item")
            return res
        self.conn.execute("INSERT INTO external_refs(source, external_id, kind, fingerprint, ref) VALUES (?,?,?,?,?) "
                          "ON CONFLICT(source, external_id, kind) DO UPDATE SET fingerprint=excluded.fingerprint, "
                          "ref=excluded.ref, synced_at=datetime('now')",
                          (src, eid, kind, fp, res.get("undo") if res["status"] == "saved" else (prev["ref"] if prev else None)))
        self.conn.execute("RELEASE sync_item")
        if replaced and res["status"] == "saved":
            res["note"] = T("cap.sync_updated", source=sync_label(src))
        return res

    def _date(self, value, default_today: bool = True) -> Optional[str]:
        if not value:
            return self.today.isoformat() if default_today else None
        f = fold(value)
        if f in ("today", "azi", "astazi", "aujourd hui", "aujourd'hui", "heute"):
            return self.today.isoformat()
        if f in ("yesterday", "ieri", "hier", "gestern"):
            return (self.today - dt.timedelta(days=1)).isoformat()
        if f in ("tomorrow", "maine", "demain", "morgen"):
            return (self.today + dt.timedelta(days=1)).isoformat()
        if f in ("day after tomorrow", "poimaine", "apres demain", "ubermorgen"):
            return (self.today + dt.timedelta(days=2)).isoformat()
        d = parse_date(value, infer_date_order([value]))
        if d is None:
            raise ValueError(f"unreadable date {value!r}")
        return d.isoformat()

    def _party(self, it: dict, create: bool):
        """(record_id, note_key) or (None, clarification dict)."""
        q = it.get("customer") or it.get("phone")
        if not q:
            return None, T("q.missing", fields="customer")
        country = get_setting(self.conn, "country", "RO")
        phone = phone_key(it.get("phone"), country) if it.get("phone") else None
        found = self.find_party(phone) if phone else None
        if found is None and it.get("customer"):
            found = self.find_party(it["customer"])
            spec = schema.primary(self.conn, "party")
            exact = self.ingestor.alias(spec["name"], f"n:{name_key(it['customer'])}")
            if isinstance(found, int) and exact is None:
                return None, T("q.partial_customer", q=it["customer"], name=self._party_name(found))
        if isinstance(found, list):
            return None, T("q.ambiguous_customer", q=it.get("customer"), list=", ".join(f"{r['name']} ({r['phone'] or '—'})" for r in found[:6]))
        if found is not None:
            if phone:
                spec = schema.primary(self.conn, "party")
                owner = self.ingestor.alias(spec["name"], f"p:{phone}")
                if owner is not None and owner != found:
                    return None, T("q.phone_other", phone=phone)
                pf = schema.role_field(spec, "phone")
                self.ingestor.add_alias(spec["name"], f"p:{phone}", found)
                if pf:
                    self.conn.execute(f"UPDATE {schema.table(spec['name'])} SET {pf['name']}=COALESCE({pf['name']}, ?) WHERE id=?", (phone, found))
            return found, None
        if not create:
            return None, T("q.customer_not_found", q=q)
        spec = schema.primary(self.conn, "party")
        cols = {}
        pf = schema.role_field(spec, "phone")
        if phone and pf:
            cols[pf["name"]] = phone
        rid, _ = self.ingestor.resolve_party(display_name(it.get("customer")) or phone, cols, {}, provisional=False, source="chat")
        return rid, "new"

    def _party_name(self, rid: int) -> str:
        return self.conn.execute("SELECT name FROM v_parties WHERE id=?", (rid,)).fetchone()["name"]

    def _quote(self, it: dict) -> Optional[str]:
        return norm_text(it.get("quote"))[:500] or None

    def _source(self, it: dict) -> str:
        src = sync_source(it.get("source"))
        if src:
            return f"sync:{src}"
        return it.get("source") if it.get("source") in ("history", "photo") else "chat"

    def _cap_transaction(self, it: dict) -> dict:
        txs = schema.by_role(self.conn, "transaction")
        spec = next((t for t in txs if t["name"] == it.get("entity")), txs[0] if txs else None)
        if spec is None:
            return {"status": "error", **T("err.no_entity", role="transaction")}
        label = E("transaction", schema.label(spec, "en"))
        amount = parse_money(it.get("amount") if it.get("amount") is not None else it.get("price"))
        status = it.get("status")
        if amount is None and status in ("no_show", "cancelled"):
            amount = 0
        missing = [k for k, ok in (("amount", amount is not None),) if not ok]
        of = schema.role_field(spec, "offering")
        if of and not it.get("offering") and not it.get("service") and not it.get("product"):
            missing.append("offering")
        if missing:
            return {"status": "clarify", **T("q.missing", fields=", ".join(missing))}
        cols: dict[str, Any] = {}
        oid = None
        if of:
            raw = norm_text(it.get("offering") or it.get("service") or it.get("product"))
            oid = self.ingestor.match_offering(raw)
            if oid is None:
                names = [r["name"] for r in self.conn.execute("SELECT name FROM v_offerings ORDER BY name")]
                return {"status": "clarify", **T("q.unknown_offering", name=raw, list=", ".join(names[:20]))}
            cols[schema.column(of)] = oid
        pf_ = schema.role_field(spec, "party")
        note = None
        pid = None
        if pf_ and (it.get("customer") or it.get("phone")):
            pid, note = self._party(it, create=True)
            if pid is None:
                return {"status": "clarify", **note}
            cols[schema.column(pf_)] = pid
        sf = schema.role_field(spec, "staff")
        staff = display_name(it.get("staff"))
        if sf and staff:
            sid, _ = self.ingestor.upsert_named(schema.entity(self.conn, sf["ref"]), staff, {})
            cols[schema.column(sf)] = sid
        date = self._date(it.get("date"))
        cols[schema.role_field(spec, "date")["name"]] = date
        cols[schema.role_field(spec, "amount")["name"]] = amount
        for attr, key in (("time", "time"), ("quantity", "quantity"), ("status", "status")):
            f = schema.role_field(spec, attr)
            if f and it.get(key) is not None:
                cols[f["name"]] = self.ingestor.convert(f, it[key])
        for k, v in (it.get("fields") or {}).items():
            f = schema.field(spec, k)
            if f and f["type"] != "ref":
                cols[f["name"]] = self.ingestor.convert(f, v)
        if self.ingestor._transaction_exists(spec, cols, None):
            return {"status": "known", **T("cap.known", entity=label), "chips": self._tx_chips(pid, oid, amount, date, staff)}
        src = self._source(it)
        rid = self.conn.execute(
            f"INSERT INTO {schema.table(spec['name'])} ({', '.join(cols)}, _source, _quote) VALUES ({', '.join('?' * (len(cols) + 2))})",
            [*cols.values(), src, self._quote(it)]).lastrowid
        extra = None
        if pid:
            n = self.conn.execute("SELECT COUNT(*) FROM v_transactions WHERE party_id=? AND counted=1", (pid,)).fetchone()[0]
            extra = T("cap.new_customer") if note == "new" else T("cap.nth", n=N(n))
        return {"status": "saved", **T("cap.transaction", entity=label), "chips": self._tx_chips(pid, oid, amount, date, staff),
                "note": extra, "undo": f"rec:{spec['name']}:{rid}"}

    def _tx_chips(self, pid, oid, amount, date, staff) -> list:
        chips = []
        if pid:
            chips.append({"v": self._party_name(pid), "icon": "person"})
        if oid:
            chips.append({"v": self.conn.execute("SELECT name FROM v_offerings WHERE id=?", (oid,)).fetchone()["name"], "icon": "tag"})
        chips.append({"v": MV(amount), "icon": "money"})
        chips.append({"v": D(date), "icon": "calendar"})
        if staff:
            chips.append({"v": staff, "icon": "staff"})
        return chips

    def _cap_customer_note(self, it: dict) -> dict:
        text = norm_text(it.get("text"))
        if not text:
            return {"status": "clarify", **T("q.text_empty")}
        pid, q = self._party(it, create=False)
        if pid is None:
            return {"status": "clarify", **q}
        spec = schema.primary(self.conn, "party")
        nf = schema.role_field(spec, "notes")
        cur = self.conn.execute(f"SELECT {nf['name']} AS n FROM {schema.table(spec['name'])} WHERE id=?", (pid,)).fetchone()["n"] or ""
        name = self._party_name(pid)
        if fold(text) in fold(cur):
            return {"status": "known", **T("cap.customer_note", name=name), "chips": [{"v": text, "icon": "note"}]}
        self.conn.execute(f"UPDATE {schema.table(spec['name'])} SET {nf['name']}=? WHERE id=?", ((cur + "\n" if cur else "") + text, pid))
        return {"status": "saved", **T("cap.customer_note", name=name), "chips": [{"v": text, "icon": "note"}]}

    def _cap_customer_info(self, it: dict) -> dict:
        pid, note = self._party(it, create=True)
        if pid is None:
            return {"status": "clarify", **note}
        spec = schema.primary(self.conn, "party")
        for k, v in (it.get("fields") or {}).items():
            f = schema.field(spec, k)
            if f and f["type"] != "ref" and not f.get("role_attr") in ("name",):
                self.conn.execute(f"UPDATE {schema.table(spec['name'])} SET {f['name']}=? WHERE id=?", (self.ingestor.convert(f, v), pid))
        if it.get("text"):
            self._cap_customer_note({**it, "customer": self._party_name(pid), "phone": None})
        chips = [{"v": self._party_name(pid), "icon": "person"}]
        if it.get("phone"):
            chips.append({"v": phone_key(it["phone"], get_setting(self.conn, "country", "RO")) or it["phone"], "icon": "phone"})
        return {"status": "saved", **T("cap.customer_new" if note == "new" else "cap.customer_info", name=self._party_name(pid)), "chips": chips}

    def _cap_offering(self, it: dict) -> dict:
        spec = schema.primary(self.conn, "offering")
        name = norm_text(it.get("offering") or it.get("service") or it.get("product") or it.get("name"))
        if not name:
            return {"status": "clarify", **T("q.missing", fields="name")}
        cols = {}
        for attr, key in (("price", "price"), ("unit_cost", "unit_cost"), ("category", "category")):
            f = schema.role_field(spec, attr)
            val = it.get(key) if key != "price" else (it.get("price") or it.get("amount"))
            if f and val not in (None, ""):
                cols[f["name"]] = parse_money(val) if f["type"] == "money" else norm_text(val)
        oid = self.ingestor.match_offering(name)
        label = E("offering", schema.label(spec, "en"))
        if oid is None:
            if schema.role_field(spec, "price")["name"] not in cols:
                return {"status": "clarify", **T("q.offering_price", name=name)}
            oid, _ = self.ingestor.upsert_offering(name, cols)
            key = "cap.offering_new"
        else:
            if cols:
                self.conn.execute(f"UPDATE {schema.table(spec['name'])} SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", [*cols.values(), oid])
            key = "cap.offering_update"
        self.conn.execute("UPDATE questions SET status='resolved', resolution='set in chat' WHERE kind='unknown_offering' AND status='open' "
                          "AND json_extract(payload,'$.record_id')=?", (oid,))
        r = self.conn.execute("SELECT name, price, unit_cost FROM v_offerings WHERE id=?", (oid,)).fetchone()
        chips = [{"v": r["name"], "icon": "tag"}, {"v": MV(r["price"]), "icon": "money"}]
        if r["unit_cost"] is not None:
            chips.append({"v": MV(r["unit_cost"]), "icon": "cost"})
        return {"status": "saved", **T(key, entity=label), "chips": chips}

    def _cap_expense(self, it: dict) -> dict:
        spec = schema.primary(self.conn, "cost")
        amount = parse_money(it.get("amount"))
        if amount is None:
            return {"status": "clarify", **T("q.missing", fields="amount")}
        if it.get("recurring"):
            return self._recurring_cost(it, amount)
        month = parse_month(it["month"]) if it.get("month") else f"{self.today.year:04d}-{self.today.month:02d}"
        if not month:
            return {"status": "clarify", **T("q.date_unreadable", v=it.get("month"))}
        category = norm_text(it.get("category")) or "other"
        cols = {schema.role_field(spec, "month")["name"]: month, schema.role_field(spec, "amount")["name"]: amount}
        cf = schema.role_field(spec, "category")
        if cf:
            cols[cf["name"]] = category
        chips = [{"v": category, "icon": "tag"}, {"v": MV(amount), "icon": "money"}, {"v": {"$mo": month}, "icon": "calendar"}]
        if self.ingestor._cost_exists(spec, cols, -1):
            return {"status": "known", **T("cap.expense"), "chips": chips}
        rid = self.conn.execute(f"INSERT INTO {schema.table(spec['name'])} ({', '.join(cols)}, _source, _quote) VALUES ({', '.join('?' * (len(cols) + 2))})",
                                [*cols.values(), self._source(it), self._quote(it)]).lastrowid
        return {"status": "saved", **T("cap.expense"), "chips": chips, "undo": f"rec:{spec['name']}:{rid}"}

    def _recurring_cost(self, it: dict, amount: int) -> dict:
        """Rent, salaries, subscriptions: counted in every month from `month` on; a new amount replaces the old one."""
        category = norm_text(it.get("category")) or "other"
        start = parse_month(it["month"]) if it.get("month") else f"{self.today.year:04d}-{self.today.month:02d}"
        chips = [{"v": category, "icon": "tag"}, {"v": MV(amount), "icon": "money"}, {"v": {"$mo": start}, "icon": "calendar"}]
        cur = self.conn.execute("SELECT * FROM recurring_costs WHERE lower(category)=lower(?) AND end_month IS NULL", (category,)).fetchone()
        if cur and cur["amount"] == amount:
            return {"status": "known", **T("cap.recurring", ), "chips": chips}
        from .metrics import shift_month
        if cur:
            self.conn.execute("UPDATE recurring_costs SET end_month=? WHERE id=?", (shift_month(start, -1), cur["id"]))
        rid = self.conn.execute("INSERT INTO recurring_costs(category, amount, start_month, quote) VALUES (?,?,?,?)",
                                (category, amount, start, self._quote(it))).lastrowid
        return {"status": "saved", **T("cap.recurring"), "chips": chips,
                "note": T("cap.recurring_replaces", amount=MV(cur["amount"])) if cur else None, "undo": f"rc:{rid}"}

    def _cap_task(self, it: dict) -> dict:
        text = norm_text(it.get("text"))
        if not text:
            return {"status": "clarify", **T("q.text_empty")}
        due = self._date(it.get("date") or it.get("due"), default_today=False) if (it.get("date") or it.get("due")) else None
        pid = None
        if it.get("customer"):
            pid, _ = self._party(it, create=False)
            pid = pid if isinstance(pid, int) else None
        amount = parse_money(it.get("amount")) if it.get("amount") else None
        assignee = display_name(it.get("staff")) or None
        if self.conn.execute("SELECT 1 FROM tasks WHERE status='open' AND text=? AND due IS ?", (text, due)).fetchone():
            return {"status": "known", **T("cap.task"), "chips": [{"v": text, "icon": "note"}]}
        rid = self.conn.execute("INSERT INTO tasks(text, due, due_time, party_id, assignee, amount, quote) VALUES (?,?,?,?,?,?,?)",
                                (text, due, it.get("time"), pid, assignee, amount, self._quote(it))).lastrowid
        chips = [{"v": text, "icon": "note"}]
        if due:
            chips.append({"v": D(due), "icon": "calendar"})
        if it.get("time"):
            chips.append({"v": it["time"], "icon": "calendar"})
        if assignee:
            chips.append({"v": assignee, "icon": "staff"})
        if amount is not None:
            chips.append({"v": MV(amount), "icon": "money"})
        return {"status": "saved", **T("cap.task"), "chips": chips, "undo": f"task:{rid}"}

    def complete_task(self, task_id: int, undo: bool = False) -> str:
        n = self.conn.execute("UPDATE tasks SET status=?, done_at=? WHERE id=?",
                              ("open" if undo else "done", None if undo else self.today.isoformat(), task_id)).rowcount
        if not n:
            raise ValueError(f"task {task_id} not found")
        return f"Task {task_id} marked {'open' if undo else 'done'}."

    def _knowledge(self, kind: str, it: dict, text: Optional[str] = None) -> dict:
        text = norm_text(text or it.get("text"))
        if not text:
            return {"status": "clarify", **T("q.text_empty")}
        for r in self.conn.execute("SELECT text FROM knowledge"):
            if fold(text) == fold(r["text"]) or (len(text) > 25 and fold(text) in fold(r["text"])):
                return {"status": "known", **T(f"cap.{kind}"), "chips": [{"v": text, "icon": "note"}]}
        rid = self.conn.execute("INSERT INTO knowledge(section, text, origin, kind, quote) VALUES (?,?,?,?,?)",
                                (KNOWLEDGE_KINDS[kind], text, self._source(it), kind, self._quote(it))).lastrowid
        return {"status": "saved", **T(f"cap.{kind}"), "chips": [{"v": text, "icon": "note"}], "undo": f"kn:{rid}"}

    def _cap_goal(self, it):
        return self._knowledge("goal", it)

    def _cap_policy(self, it):
        return self._knowledge("policy", it)

    def _cap_fact(self, it):
        if it.get("customer") or it.get("phone"):           # about a person -> people layer
            return self._cap_person_fact(it)
        return self._knowledge("fact", it)

    def _cap_staff(self, it: dict) -> dict:
        name = display_name(it.get("staff"))
        if not name:
            return {"status": "clarify", **T("q.missing", fields="staff")}
        sspec = schema.primary(self.conn, "staff")
        if sspec:
            self.ingestor.upsert_named(sspec, name, {})
        text = norm_text(it.get("text")) or ""
        if fold(name) not in fold(text):
            text = f"{name}: {text}".strip(": ")
        if it.get("date"):
            text += f" ({self._date(it['date'])})"
        return self._knowledge("staff", it, text)

    def _cap_decision(self, it: dict) -> dict:
        text = norm_text(it.get("text"))
        if not text:
            return {"status": "clarify", **T("q.text_empty")}
        review = self._date(it.get("review_on"), default_today=False) or (self.today + dt.timedelta(days=30)).isoformat()
        chips = [{"v": text, "icon": "note"}, {"v": D(review), "icon": "calendar"}]
        if self.conn.execute("SELECT 1 FROM decisions WHERE decision=?", (text,)).fetchone():
            return {"status": "known", **T("cap.decision", date=D(review)), "chips": chips}
        rid = self.conn.execute("INSERT INTO decisions(decided_on, decision, reason, expected_outcome, review_on, quote) VALUES (?,?,?,?,?,?)",
                                (self._date(it.get("date")), text, it.get("reason"), it.get("expected_outcome"), review, self._quote(it))).lastrowid
        return {"status": "saved", **T("cap.decision", date=D(review)), "chips": chips, "undo": f"dec:{rid}"}

    def _cap_record(self, it: dict) -> dict:
        spec = schema.entity(self.conn, it.get("entity") or "")
        if spec is None:
            return {"status": "clarify", **T("q.unknown_entity", entity=str(it.get("entity")), list=", ".join(schema.entities(self.conn)))}
        values = {}
        for k, v in (it.get("fields") or {}).items():
            f = schema.field(spec, k)
            if f is None:
                return {"status": "clarify", **T("q.unknown_field", field=k, entity=spec["name"], list=", ".join(x["name"] for x in spec["fields"]))}
            values[f"{k}.name" if f["type"] == "ref" else k] = v
        status, info = self.ingestor.load(spec, values, {}, source=self._source(it), quote=self._quote(it))
        label = E(spec["name"], schema.label(spec, "en"))
        chips = [{"v": str(v)} for v in (it.get("fields") or {}).values()][:5]
        if status == "flagged":
            return {"status": "clarify", **T("err.generic", msg=str(info))}
        if status == "duplicate":
            return {"status": "known", **T("cap.record", entity=label), "chips": chips}
        rid = self.conn.execute(f"SELECT MAX(id) FROM {schema.table(spec['name'])}").fetchone()[0]
        return {"status": "saved", **T("cap.record", entity=label), "chips": chips,
                "undo": f"rec:{spec['name']}:{rid}" if spec["role"] in ("transaction", "cost", "custom") else None}

    # --------------------------------------------------------------- schema
    def change_schema(self, action: str, entity: Optional[str] = None, spec: Optional[dict] = None, field: Any = None,
                      new_name: Optional[str] = None, label: Optional[dict] = None, new_type: Optional[str] = None,
                      ref: Optional[str] = None, confirm: bool = False) -> dict:
        from .editing import PreviewNeeded
        self._require_setup()
        if action == "create_entity":
            if not spec:
                raise ValueError("create_entity needs `spec` {name, role, label:{en,fr,ro,de:[singular, plural]}, fields:[...]}")
            change = schema.create_entity(self.conn, spec, origin="assistant")
            target, fname = spec["name"], None
        elif action == "add_field":
            if not entity or not isinstance(field, dict):
                raise ValueError("add_field needs `entity` and `field` {name, type, label?, synonyms?}")
            change = schema.add_field(self.conn, entity, field, origin="assistant")
            target, fname = entity, field["name"]
        elif action in ("add_fact_kind", "add_relation_type"):
            from .people import add_vocab
            if not spec or not spec.get("name"):
                raise ValueError(f"{action} needs `spec` {{name, label:{{en,fr,ro,de}}, synonyms?, "
                                 + ("polarity?, sensitivity: normal|private|special}" if action == "add_fact_kind"
                                    else "symmetric?, label as [forward, inverse], inverse_synonyms?}"))
            change = add_vocab(self.conn, action[4:], spec["name"], spec)
            self.conn.commit()
            return view("change_schema", T("view.schema_change"),
                        [block("feed", items=[{"kind": "record", "status": "saved", **T(f"people.{action}", name=spec["name"]), "undo": str(change)}])],
                        hints=[f"Applied as change #{change}; undo_change('{change}') reverts it."])
        elif action in ("rename_field", "remove_field", "change_field", "relabel_entity", "remove_entity"):
            if not entity:
                raise ValueError(f"{action} needs `entity`")
            if action not in ("relabel_entity", "remove_entity") and not field:
                raise ValueError(f"{action} needs `field` (the field name)")
            try:
                change, fname = self._schema_change(action, entity, field, new_name, label, new_type, ref, confirm)
            except PreviewNeeded as p:
                return view("change_schema", T("view.edit_preview"), [block("alerts", items=[{**p.msg, "level": "warn"}])],
                            hints=["NOTHING CHANGED YET. Ask the owner; if they agree, call change_schema again with confirm=true."])
            target = entity
        else:
            raise ValueError("action: create_entity | add_field | rename_field | remove_field | change_field | relabel_entity | remove_entity "
                             "| add_fact_kind | add_relation_type")
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM schema_changes WHERE id=?", (change,)).fetchone()
        payload = json.loads(row["payload"])
        blocks = []
        s_ = schema.entity(self.conn, target)
        if s_:
            blocks.append(block("schema", title=T("view.schema_change"), entities=[{"name": s_["name"], "role": s_["role"], "label": schema.label(s_, self.lang, True),
                                                                                  "fields": [schema_field_view(f) for f in s_["fields"]]}],
                                changes=[{"id": change, "op": action, "entity": target, "field": fname, "status": "applied", "undoable": True}]))
        else:
            blocks.append(block("feed", items=[{"kind": "record", "status": "saved", **T("edit.entity_removed", entity=target), "undo": str(change)}]))
        if payload.get("failed"):
            blocks.append(block("alerts", items=[{**T("edit.convert_failed", n=N(payload["failed"])), "level": "warn"}]))
        return view("change_schema", T("view.schema_change"), blocks,
                    hints=[f"Applied as change #{change}; undo_change('{change}') reverts it exactly."])

    # ------------------------------------------------------------- design
    def update_design(self, patch: dict, note: Optional[str] = None) -> dict:
        self._require_setup()
        spec, changes = design.apply_patch(self.conn, patch, note)
        self._mark_used("design")
        blocks = [block("design", brand=spec["brand"], brief=spec["brief"], views=list(spec["views"]), changes=changes)]
        hints = ["Design saved (undo_change('design') reverts). Show the result: call get_business_brief or show_view(name)."]
        return view("update_design", T("view.design"), blocks, hints=hints)

    def define_metric(self, name: str, label: dict, sql: str, format: str = "number") -> dict:
        self._require_setup()
        if not schema._NAME.match(name):
            raise ValueError("metric name: lowercase letters, digits, underscore")
        if format not in ("number", "money", "percent"):
            raise ValueError("format: number | money | percent")
        sql = M.validate_metric_sql(sql)
        s, e = M.month_bounds(M.last_full_month(self.today))
        value = M.eval_metric(self.readonly(), sql, s, e)     # proves it runs, read-only
        self.conn.execute("INSERT OR REPLACE INTO metric_defs(name, label, sql, format) VALUES (?,?,?,?)",
                          (name, json.dumps(label, ensure_ascii=False), sql, format))
        self.conn.commit()
        return view("define_metric", T("view.metric"), [block("kv", items=[{"raw": label.get(self.lang) or label.get("en") or name, "v": value, "fmt": format}])],
                    hints=["Use it in update_design (brief.kpis or a kpis block)."])

    # --------------------------------------------------------- housekeeping
    def merge_customers(self, keep: str, duplicate: str) -> dict:
        a, b = self.find_party(keep), self.find_party(duplicate)
        for q, v in ((keep, a), (duplicate, b)):
            if v is None:
                raise ValueError(f"customer {q!r} not found")
            if isinstance(v, list):
                raise ValueError(f"{q!r} is ambiguous: {', '.join(x['name'] for x in v)}; use the phone or full name")
        if a == b:
            return view("merge_customers", T("view.merge"), [block("text", **T("merge.same"))])
        spec = schema.primary(self.conn, "party")
        tbl = schema.table(spec["name"])
        ka = dict(self.conn.execute(f"SELECT * FROM {tbl} WHERE id=?", (a,)).fetchone())
        kb = dict(self.conn.execute(f"SELECT * FROM {tbl} WHERE id=?", (b,)).fetchone())
        moved = 0
        for ent, s in schema.entities(self.conn).items():
            for f in s["fields"]:
                if f["type"] == "ref" and f.get("ref") == spec["name"]:
                    moved += self.conn.execute(f"UPDATE {schema.table(ent)} SET {schema.column(f)}=? WHERE {schema.column(f)}=?", (a, b)).rowcount
        self.conn.execute("UPDATE OR IGNORE aliases SET record_id=? WHERE entity=? AND record_id=?", (a, spec["name"], b))
        self.conn.execute("DELETE FROM aliases WHERE entity=? AND record_id=?", (spec["name"], b))
        upd = {}
        for f in spec["fields"]:
            n = f["name"]
            if ka.get(n) in (None, "") and kb.get(n) not in (None, ""):
                upd[n] = kb[n]
        nf = schema.role_field(spec, "notes")
        if nf and ka.get(nf["name"]) and kb.get(nf["name"]):
            upd[nf["name"]] = f"{ka[nf['name']]}\n{kb[nf['name']]}"
        ff = schema.role_field(spec, "first_seen")
        if ff and kb.get(ff["name"]) and (not ka.get(ff["name"]) or kb[ff["name"]] < ka[ff["name"]]):
            upd[ff["name"]] = kb[ff["name"]]
        upd["_extra"] = json.dumps({**json.loads(kb["_extra"] or "{}"), **json.loads(ka["_extra"] or "{}")}, ensure_ascii=False)
        upd["_provisional"] = 0
        self.conn.execute(f"UPDATE {tbl} SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", [*upd.values(), a])
        self.conn.execute(f"DELETE FROM {tbl} WHERE id=?", (b,))
        self.conn.execute("UPDATE contact_log SET party_id=? WHERE party_id=?", (a, b))
        self._move_person_links(spec["name"], a, b)
        self.conn.execute("UPDATE questions SET status='resolved', resolution=? WHERE status='open' AND json_extract(payload,'$.record_id')=?",
                          (f"merged into {a}", b))
        self.conn.commit()
        return view("merge_customers", T("view.merge"), [block("feed", items=[{"kind": "customer_info", "status": "saved",
                                                                              **T("merge.done", a=ka["name"], b=kb["name"], n=N(moved))}])])

    def resolve_question(self, resolution: str, question_id: Optional[int] = None, question_ids: Optional[list[int]] = None,
                         all_open: bool = False, note: Optional[str] = None) -> str:
        if resolution not in ("confirm", "dismiss"):
            raise ValueError("resolution: confirm | dismiss")
        ids = list(question_ids or []) + ([question_id] if question_id is not None else [])
        if all_open:
            ids = [r["id"] for r in self.conn.execute("SELECT id FROM questions WHERE status='open'")]
        if not ids:
            raise ValueError("give question_id, question_ids or all_open=true")
        spec = schema.primary(self.conn, "party")
        done = 0
        for qid in ids:
            q = self.conn.execute("SELECT * FROM questions WHERE id=? AND status='open'", (qid,)).fetchone()
            if not q:
                continue
            payload = json.loads(q["payload"])
            if q["kind"] == "fact_conflict":
                self._resolve_fact_conflict(payload, resolution == "confirm")
            if resolution == "confirm" and q["kind"] in ("new_party", "new_parties") and spec:
                rids = payload.get("record_ids") or ([payload["record_id"]] if payload.get("record_id") else [])
                for rid in rids:
                    self.conn.execute(f"UPDATE {schema.table(spec['name'])} SET _provisional=0 WHERE id=?", (rid,))
            self.conn.execute("UPDATE questions SET status=?, resolution=? WHERE id=?",
                              ("resolved" if resolution == "confirm" else "dismissed", note or resolution, qid))
            done += 1
        left = self.conn.execute("SELECT COUNT(*) FROM questions WHERE status='open'").fetchone()[0]
        return f"{done} question(s) {resolution}ed; {left} open."

    def mark_contacted(self, customer: str, channel: Optional[str] = None, note: Optional[str] = None) -> str:
        pid = self.find_party(customer)
        if not isinstance(pid, int):
            raise ValueError(f"customer {customer!r} not found or ambiguous")
        self.conn.execute("INSERT INTO contact_log(party_id, contacted_on, channel, note) VALUES (?,?,?,?)",
                          (pid, self.today.isoformat(), channel, note))
        return f"Contact with {self._party_name(pid)} logged for {self.today.isoformat()}."

    def undo_change(self, change_id: str) -> dict:
        cid = str(change_id)
        if cid == "design":
            spec = design.undo(self.conn)
            msg = T("undo.design" if spec else "undo.nothing")
        elif cid.startswith("rec:"):
            _, ent, rid = cid.split(":")
            spec = schema.entity(self.conn, ent)
            if spec is None:
                raise ValueError("unknown record")
            n = self.conn.execute(f"DELETE FROM {schema.table(ent)} WHERE id=? AND (_source IN ('chat','history','photo') OR _source LIKE 'photo%' OR _source LIKE 'list%' OR _source LIKE 'sync:%')",
                                  (int(rid),)).rowcount
            msg = T("undo.record" if n else "undo.nothing")
        elif cid.startswith("kn:"):
            n = self.conn.execute("DELETE FROM knowledge WHERE id=? AND origin!='file'", (int(cid[3:]),)).rowcount
            msg = T("undo.record" if n else "undo.nothing")
        elif cid.startswith("rc:"):
            r = self.conn.execute("SELECT * FROM recurring_costs WHERE id=?", (int(cid[3:]),)).fetchone()
            n = 0
            if r:
                from .metrics import shift_month
                self.conn.execute("UPDATE recurring_costs SET end_month=NULL WHERE lower(category)=lower(?) AND end_month=?",
                                  (r["category"], shift_month(r["start_month"], -1)))
                n = self.conn.execute("DELETE FROM recurring_costs WHERE id=?", (r["id"],)).rowcount
            msg = T("undo.record" if n else "undo.nothing")
        elif cid.startswith("task:"):
            n = self.conn.execute("DELETE FROM tasks WHERE id=?", (int(cid[5:]),)).rowcount
            msg = T("undo.record" if n else "undo.nothing")
        elif cid.startswith("fact:"):
            msg = T("undo.record" if self._undo_fact(int(cid[5:])) else "undo.nothing")
        elif cid.startswith("rel:"):
            n = self.conn.execute("DELETE FROM relations WHERE id=?", (int(cid[4:]),)).rowcount
            msg = T("undo.record" if n else "undo.nothing")
        elif cid.startswith("dec:"):
            n = self.conn.execute("DELETE FROM decisions WHERE id=?", (int(cid[4:]),)).rowcount
            msg = T("undo.record" if n else "undo.nothing")
        else:
            text = schema.undo(self.conn, int(cid))
            msg = T("undo.schema", what=text)
            self.conn.execute("DELETE FROM trash WHERE change_id=?", (int(cid),))
        from .i18n import t as tr
        return "Undone: " + tr(msg["t"], "en", msg.get("p"))

    def _move_person_links(self, entity: str, keep: int, dup: int, take=None) -> None:
        """Facts and relations of a duplicate person move to the record that is kept."""
        if take:
            for r in self.conn.execute("SELECT id FROM facts WHERE entity=? AND record_id=?", (entity, dup)).fetchall():
                take("facts", r["id"])
            for r in self.conn.execute("SELECT id FROM relations WHERE (a_entity=? AND a_id=?) OR (b_entity=? AND b_id=?)",
                                       (entity, dup, entity, dup)).fetchall():
                take("relations", r["id"])
        self.conn.execute("UPDATE facts SET record_id=? WHERE entity=? AND record_id=?", (keep, entity, dup))
        self.conn.execute("UPDATE relations SET a_id=? WHERE a_entity=? AND a_id=?", (keep, entity, dup))
        self.conn.execute("UPDATE relations SET b_id=? WHERE b_entity=? AND b_id=?", (keep, entity, dup))
        self.conn.execute("DELETE FROM relations WHERE a_entity=b_entity AND a_id=b_id")     # a link between the two duplicates

    def _require_setup(self) -> None:
        if not self.is_setup:
            raise ValueError("The business is not set up yet: call get_started, then setup_business(name, vertical, locale, currency, country).")
