"""Ingestion: raw file -> mapping -> normalization -> entity resolution -> dynamic tables.

Works on whatever entities this business has (from its vertical pack or created by the
assistant). Unknown columns become new fields automatically (an undoable schema change);
identical rows coming from overlapping exports or from chat are never counted twice.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from openpyxl import load_workbook

from . import mapping as mp
from . import schema
from .db import get_setting
from .normalize import (
    compact_key, display_name, fold, infer_date_order, name_key, norm_text, parse_date, parse_money,
    parse_month, parse_time, phone_key,
)
from .verticals import offering_groups

TABLE_EXT = {".csv", ".xlsx", ".xlsm", ".tsv"}
TEXT_EXT = {".md", ".txt"}
ROLE_ORDER = {"offering": 0, "staff": 1, "party": 2, "resource": 3, "custom": 4, "cost": 5, "transaction": 6}


@dataclass
class IngestReport:
    file: str
    entity: str
    file_id: Optional[int] = None
    mapping: dict = field(default_factory=dict)
    mapping_origin: str = ""
    rows_total: int = 0
    rows_loaded: int = 0
    rows_updated: int = 0
    rows_duplicate: int = 0
    rows_flagged: int = 0
    notes: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    sample: list[dict] = field(default_factory=list)
    date_order: Optional[str] = None
    date_range: list[str] = field(default_factory=list)
    questions: int = 0
    new_fields: list[dict] = field(default_factory=list)       # [{field, type, change_id}]
    already: bool = False


@dataclass
class Source:
    name: str
    kind: str                      # table | text
    text: str
    headers: list[str] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)


# --- Readers -------------------------------------------------------------------

def _sniff_delimiter(text: str) -> str:
    head = "\n".join(text.splitlines()[:5])
    counts = {d: head.count(d) for d in (",", ";", "\t")}
    return max(counts, key=counts.get)


def table_from_text(text: str) -> tuple[list[str], list[dict]]:
    text = text.lstrip("﻿")
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=_sniff_delimiter(text)) if any(c.strip() for c in r)]
    if not rows:
        return [], []
    headers = [norm_text(h) for h in rows[0]]
    return headers, [dict(zip(headers, r)) for r in rows[1:]]


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def read_table(path: Path) -> tuple[list[str], list[dict], str]:
    return read_table_bytes(path.name, path.read_bytes())


def read_table_bytes(name: str, raw: bytes) -> tuple[list[str], list[dict], str]:
    if Path(name).suffix.lower() in (".csv", ".tsv"):
        text = _decode(raw)
        h, r = table_from_text(text)
        return h, r, text
    ws = load_workbook(io.BytesIO(raw), data_only=True, read_only=True).active
    rows = [list(r) for r in ws.iter_rows(values_only=True) if any(c not in (None, "") for c in r)]
    if not rows:
        return [], [], ""
    buf = io.StringIO()
    csv.writer(buf).writerows([["" if c is None else (c.isoformat() if isinstance(c, (dt.date, dt.datetime)) else c) for c in r] for r in rows])
    headers = [norm_text(h) for h in rows[0]]
    return headers, [dict(zip(headers, r)) for r in rows[1:]], buf.getvalue()


def looks_like_table(text: str) -> bool:
    lines = [ln for ln in text.strip().splitlines()[:5] if ln.strip()]
    d = _sniff_delimiter(text)
    return len(lines) >= 2 and all(ln.count(d) >= 1 for ln in lines) and not lines[0].lstrip().startswith("#")


def split_sections(text: str) -> list[tuple[str, str]]:
    sections, title, buf = [], "General", []
    for line in text.splitlines():
        heading = re.match(r"^#{1,6}\s+(.*)", line)
        if heading:
            if "".join(buf).strip():
                sections.append((title, "\n".join(buf).strip()))
            title, buf = heading.group(1).strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections.append((title, "\n".join(buf).strip()))
    return sections


def source_from_path(path: Path) -> Optional[Source]:
    return source_from_bytes(path.name, path.read_bytes()) if path.suffix.lower() in TABLE_EXT | TEXT_EXT else None


def source_from_bytes(name: str, raw: bytes) -> Optional[Source]:
    """A file that arrived as bytes (upload page, Drive). None for unsupported types."""
    ext = Path(name).suffix.lower()
    if ext in TABLE_EXT:
        h, r, text = read_table_bytes(name, raw)
        return Source(name, "table", text, h, r)
    if ext in TEXT_EXT:
        return Source(name, "text", _decode(raw))
    return None


def source_from_content(filename: str, content: str) -> Source:
    content = content.lstrip("﻿")
    ext = Path(filename).suffix.lower()
    if ext in (".csv", ".tsv") or (ext not in TEXT_EXT and looks_like_table(content)):
        h, r = table_from_text(content)
        return Source(filename, "table", content, h, r)
    return Source(filename, "text", content)


# --- Ingestor --------------------------------------------------------------------

class Ingestor:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._pending_new: list[tuple[int, str]] = []

    @property
    def country(self) -> str:
        return get_setting(self.conn, "country", "RO")

    @property
    def vertical(self) -> str:
        return get_setting(self.conn, "vertical", "generic")

    # public ---------------------------------------------------------------
    def add_path(self, path: str | Path) -> list[IngestReport]:
        path = Path(path).expanduser()
        if not path.exists():
            raise FileNotFoundError(path)
        files = sorted(p for p in path.iterdir() if p.is_file() and not p.name.startswith(".")) if path.is_dir() else [path]
        sources, reports = [], []
        for f in files:
            src = source_from_path(f)
            if src:
                sources.append(src)
            else:
                reports.append(IngestReport(f.name, "skipped", notes=["unsupported type (CSV, XLSX, MD, TXT; send images in the chat)"]))
        return self.ingest_sources(sources) + reports

    def add_files(self, files: list[tuple[str, bytes]]) -> list[IngestReport]:
        """Files as (name, bytes): the upload page and Drive imports. Same matching as add_path."""
        sources, reports = [], []
        for name, raw in files:
            src = source_from_bytes(name, raw)
            if src:
                sources.append(src)
            else:
                reports.append(IngestReport(name, "skipped", notes=["unsupported type (CSV, XLSX, MD, TXT; send images in the chat)"]))
        return self.ingest_sources(sources) + reports

    def add_content(self, filename: str, content: str) -> IngestReport:
        return self.ingest_sources([source_from_content(filename, content)])[0]

    def ingest_sources(self, sources: list[Source]) -> list[IngestReport]:
        tables, texts = [], []
        for src in sources:
            if src.kind == "table":
                tables.append((src, mp.detect(self.conn, src.headers, src.records)))
            else:
                texts.append(src)
        role = lambda m: ROLE_ORDER.get(schema.entity(self.conn, m.entity)["role"], 9) if m else 99  # noqa: E731
        tables.sort(key=lambda t: role(t[1]))
        reports = [self._ingest_table(src, m) for src, m in tables]
        reports += [self._ingest_text(src) for src in texts]
        self.conn.commit()
        return reports

    def apply_mapping(self, file_id: int, entity: str, columns: dict[str, Optional[str]]) -> IngestReport:
        row = self.conn.execute("SELECT name, content_text FROM raw_files WHERE id=?", (file_id,)).fetchone()
        headers, records = table_from_text(row["content_text"])
        for tname, spec_ in schema.entities(self.conn).items():          # rows this file created are replaced, not duplicated
            if spec_["role"] in ("transaction", "cost", "custom", "resource"):
                self.conn.execute(f"DELETE FROM aliases WHERE entity=? AND record_id IN (SELECT id FROM {schema.table(tname)} WHERE _file_id=?)",
                                  (tname, file_id))
                self.conn.execute(f"DELETE FROM {schema.table(tname)} WHERE _file_id=?", (file_id,))
        m = mp.Mapping(entity, columns, "assistant", [h for h, t in columns.items() if t is None])
        report = self._ingest_table(Source(row["name"], "table", row["content_text"], headers, records), m, file_id=file_id)
        self.conn.execute("UPDATE questions SET status='resolved', resolution=? WHERE kind='unknown_file' AND status='open' "
                          "AND json_extract(payload,'$.file_id')=?", (f"mapped as {entity}", file_id))
        self.conn.commit()
        return report

    def import_records(self, entity: str, rows: list[dict], source: str) -> IngestReport:
        """Rows already extracted by the assistant (e.g. from a photo); keys are field names or targets."""
        spec = schema.entity(self.conn, entity)
        if spec is None:
            raise ValueError(f"unknown entity {entity!r}; existing: {list(schema.entities(self.conn))}")
        keys = sorted({k for r in rows for k in r})
        valid = set(mp.targets(spec, self.conn))
        cols = {}
        for k in keys:
            f = schema.field(spec, k)
            cols[k] = f"{k}.name" if f and f["type"] == "ref" else (k if k in valid else "extra")
        text = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
        src = Source(source, "records", text, keys, rows)
        m = mp.Mapping(entity, cols, "records")
        report = self._ingest_table(src, m, kind="records")
        self.conn.commit()
        return report

    # text ---------------------------------------------------------------------
    def _ingest_text(self, src: Source) -> IngestReport:
        file_id = self._store_raw(src.name, "text", src.text, "knowledge")
        report = IngestReport(src.name, "knowledge", file_id=file_id)
        if file_id is None:
            report.already = True
            return report
        for title, body in split_sections(src.text):
            self.conn.execute("INSERT INTO knowledge(source_file_id, section, text) VALUES (?,?,?)", (file_id, title, body))
            report.rows_loaded += 1
        self._save_report(report)
        return report

    def _store_raw(self, name: str, kind: str, text: str, entity: Optional[str]) -> Optional[int]:
        digest = hashlib.sha256(text.encode()).hexdigest()
        if self.conn.execute("SELECT 1 FROM raw_files WHERE sha256=?", (digest,)).fetchone():
            return None
        return self.conn.execute("INSERT INTO raw_files(name, kind, sha256, content_text, entity) VALUES (?,?,?,?,?)",
                                 (name, kind, digest, text, entity)).lastrowid

    def _save_report(self, r: IngestReport) -> None:
        if r.file_id:
            self.conn.execute("UPDATE raw_files SET entity=?, report=? WHERE id=?",
                              (None if r.entity == "unknown" else r.entity, json.dumps(asdict(r), ensure_ascii=False, default=str), r.file_id))

    # tables --------------------------------------------------------------------
    def _ingest_table(self, src: Source, m: Optional[mp.Mapping], file_id: Optional[int] = None, kind: str = "table") -> IngestReport:
        if file_id is None:
            file_id = self._store_raw(src.name, kind, src.text, m.entity if m else None)
            if file_id is None:
                return IngestReport(src.name, m.entity if m else "unknown", already=True)
        if m is None:
            r = IngestReport(src.name, "unknown", file_id=file_id, rows_total=len(src.records), headers=src.headers,
                             sample=[{k: str(v) for k, v in rec.items()} for rec in src.records[:3]])
            self.question("unknown_file", f"File '{src.name}' was not recognized. Columns: {', '.join(src.headers)}",
                          {"file_id": file_id, "file": src.name, "headers": src.headers})
            r.questions = 1
            self._save_report(r)
            return r

        spec = schema.entity(self.conn, m.entity)
        q_before = self.conn.execute("SELECT COALESCE(MAX(id),0) FROM questions").fetchone()[0]
        r = IngestReport(src.name, m.entity, file_id=file_id, mapping_origin=m.origin, rows_total=len(src.records), headers=src.headers)

        # Unmatched columns become new fields (undoable) when they carry data; empty ones are ignored.
        if m.origin in ("synonyms", "records"):
            for h in list(m.columns):
                if m.columns[h] not in (None, "extra"):
                    continue
                vals = [rec.get(h) for rec in src.records]
                if not any(v not in (None, "") for v in vals):
                    m.columns[h] = None
                    continue
                typ = mp.infer_type(vals)
                fname = mp.field_name_for(spec, h)
                try:
                    change = schema.add_field(self.conn, m.entity, {
                        "name": fname, "type": typ, "synonyms": [h],
                        "label": {lang: [h, h] for lang in schema.LANGS}}, origin="import")
                    spec = schema.entity(self.conn, m.entity)
                    m.columns[h] = fname
                    r.new_fields.append({"field": fname, "header": h, "type": typ, "change_id": change})
                except schema.SchemaError:
                    m.columns[h] = "extra"
        r.mapping = dict(m.columns)

        date_header = next((h for h, t in m.columns.items() if t and (schema.field(spec, t) or {}).get("type") == "date"), None)
        if m.date_order is None and date_header:
            m.date_order = infer_date_order(rec.get(date_header) for rec in src.records)
        r.date_order = m.date_order
        if kind == "table":
            mp.remember(self.conn, src.headers, m)

        dates = []
        for i, rec in enumerate(src.records, start=2):
            values = {}
            extra = {}
            for h, t in m.columns.items():
                v = rec.get(h)
                if v in (None, ""):
                    continue
                if t == "extra":
                    extra[h] = str(v)
                elif t:
                    values[t] = v
            status, info = self.load(spec, values, extra, file_id=file_id, row=i, order=m.date_order, source=src.name if src.name.startswith("sync:") else f"file:{file_id}")
            if status == "inserted":
                r.rows_loaded += 1
                if info:
                    dates.append(info)
            elif status == "updated":
                r.rows_updated += 1
            elif status == "duplicate":
                r.rows_duplicate += 1
            else:
                r.rows_flagged += 1
                if len(r.notes) < 8:
                    r.notes.append(f"row {i}: {info}")
        if dates:
            r.date_range = [min(dates), max(dates)]
        self._flush_new_parties(src.name)
        r.questions = self.conn.execute("SELECT COUNT(*) FROM questions WHERE id>?", (q_before,)).fetchone()[0]
        self._save_report(r)
        return r

    # value conversion ----------------------------------------------------------
    def convert(self, f: dict, v, order: Optional[str] = None):
        t = f["type"]
        if v in (None, ""):
            return None
        if t == "money":
            return parse_money(v)
        if t == "date":
            d = parse_date(v, order)
            return d.isoformat() if d else None
        if t == "time":
            return parse_time(v)
        if t == "month":
            return parse_month(v)
        if t == "number":
            if isinstance(v, (int, float)):
                return float(v)
            try:
                return float(str(v).replace(",", ".").strip())
            except ValueError:
                return None
        if t == "phone":
            return phone_key(v, self.country)
        if t == "email":
            s = norm_text(v).lower()
            return s if "@" in s else None
        if t == "bool":
            return 1 if fold(v) in ("1", "yes", "y", "true", "da", "oui", "ja", "x") else 0
        if t == "enum":
            key = fold(v).replace("_", " ").replace("-", " ")
            for value, syns in (f.get("value_synonyms") or {}).items():
                if key == value.replace("_", " ") or key in [fold(s) for s in syns]:
                    return value
            return key.replace(" ", "_") if key.replace(" ", "_") in (f.get("values") or []) else None
        return display_name(v) if f.get("role_attr") == "name" else norm_text(v)

    # loading one record ---------------------------------------------------------
    def load(self, spec: dict, values: dict[str, Any], extra: dict, *, file_id=None, row=None, order=None,
             source="chat", quote=None) -> tuple[str, Any]:
        role = spec["role"]
        cols: dict[str, Any] = {}
        refs: dict[str, dict] = {}
        for target, v in values.items():
            base, _, sub = target.partition(".")
            f = schema.field(spec, base)
            if not f:
                continue
            if f["type"] == "ref":
                refs.setdefault(base, {})[sub or "name"] = v
            elif sub in ("first", "last"):
                cols.setdefault(f"__{base}", {})[sub] = norm_text(v)
            else:
                val = self.convert(f, v, order)
                if val is None and f.get("role_attr") in ("date", "amount", "month") and v not in (None, ""):
                    return "flagged", f"unreadable {f['name']} {v!r}"
                cols[f["name"]] = val
        for key in [k for k in cols if k.startswith("__")]:
            parts = cols.pop(key)
            cols.setdefault(key[2:], display_name(" ".join(p for p in (parts.get("first"), parts.get("last")) if p)))
        for k, v in list(cols.items()):
            if v is None:
                del cols[k]

        missing = [f["name"] for f in spec["fields"] if f.get("role_attr") and schema.ROLES[role].get(f["role_attr"], ("", False))[1]
                   and f["name"] not in cols]
        if missing:
            return "flagged", f"missing {', '.join(missing)}"

        if role == "party":
            nf_ = schema.role_field(spec, "name")["name"]
            raw = values.get(nf_) or " ".join(str(values.get(f"{nf_}.{p}") or "") for p in ("first", "last")).strip()
            guess = isinstance(raw, str) and (raw.isupper() or raw.islower())
            rid, created = self.resolve_party(cols.get(nf_), cols, extra, source=source, name_guess=guess)
            return ("inserted" if created else "updated"), None
        if role == "offering":
            nf = schema.role_field(spec, "name")["name"]
            rid, created = self.upsert_offering(cols.pop(nf), cols)
            return ("inserted" if created else "updated"), None
        if role in ("staff", "resource"):
            nf = schema.role_field(spec, "name")["name"]
            rid, created = self.upsert_named(spec, cols[nf], cols)
            return ("inserted" if created else "updated"), None

        # transactions, costs and custom records reference others
        date_field = schema.role_field(spec, "date")
        when = cols.get(date_field["name"]) if date_field else None
        for base, attrs in refs.items():
            f = schema.field(spec, base)
            target = schema.entity(self.conn, f["ref"])
            rid = self.resolve_ref(target, attrs, when, from_transaction=role == "transaction")
            if rid is None:
                if f.get("role_attr") == "party":
                    continue                                   # anonymous sale / unknown card: keep the sale
                return "flagged", f"could not resolve {base} {attrs}"
            cols[schema.column(f)] = rid

        if role == "transaction" and self._transaction_exists(spec, cols, file_id):
            return "duplicate", None
        if role in ("custom", "resource"):
            hit = self._custom_match(spec, cols)
            if hit is not None:
                rid, same = hit
                if same:
                    return "duplicate", None
                upd = {k: v for k, v in cols.items() if v is not None}
                if extra:
                    upd["_extra"] = json.dumps(extra, ensure_ascii=False)
                self.conn.execute(f"UPDATE {schema.table(spec['name'])} SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", [*upd.values(), rid])
                return "updated", None
        if role == "cost" and self._cost_exists(spec, cols, file_id):
            return "duplicate", None
        sys_cols = {"_source": source, "_file_id": file_id, "_source_row": row, "_quote": quote,
                    "_extra": json.dumps(extra, ensure_ascii=False)}
        allcols = {**cols, **{k: v for k, v in sys_cols.items() if v is not None}}
        rid = self.conn.execute(f"INSERT INTO {schema.table(spec['name'])} ({', '.join(allcols)}) VALUES ({', '.join('?' * len(allcols))})",
                                list(allcols.values())).lastrowid
        key = self._custom_key(spec)
        if role in ("custom", "resource") and key and cols.get(schema.column(key)):
            self.add_alias(spec["name"], f"n:{name_key(cols[schema.column(key)])}", rid)
        return "inserted", when

    @staticmethod
    def _custom_key(spec: dict) -> Optional[dict]:
        """The identifying field of a custom table: role 'name', or a text field called name/title/<entity>."""
        f = schema.role_field(spec, "name")
        if f:
            return f
        for cand in ("name", "title", spec["name"], "nume", "nom", "bezeichnung"):
            g = schema.field(spec, cand)
            if g and g["type"] == "text":
                return g
        return None

    def _custom_match(self, spec: dict, cols: dict):
        """(id, identical?) of an existing row of a custom table: same key name, or exactly the same values."""
        tbl = schema.table(spec["name"])
        key = self._custom_key(spec)
        rid = None
        if key and cols.get(schema.column(key)):
            rid = self.alias(spec["name"], f"n:{name_key(cols[schema.column(key)])}")
            if rid is None:
                for r in self.conn.execute(f"SELECT id, {schema.column(key)} AS k FROM {tbl}"):
                    if r["k"] and name_key(r["k"]) == name_key(cols[schema.column(key)]):
                        rid = r["id"]
                        break
        if rid is None:
            data = {k: v for k, v in cols.items() if v is not None}
            if not data:
                return None
            r = self.conn.execute(f"SELECT id FROM {tbl} WHERE {' AND '.join(f'{k} IS ?' for k in data)}", list(data.values())).fetchone()
            return (r["id"], True) if r else None
        row = self.conn.execute(f"SELECT * FROM {tbl} WHERE id=?", (rid,)).fetchone()
        if row is None:                                   # stale pointer to a removed row
            self.conn.execute("DELETE FROM aliases WHERE entity=? AND record_id=?", (spec["name"], rid))
            return None
        cur = dict(row)
        same = all(cur.get(k) == v for k, v in cols.items() if v is not None)
        return rid, same

    def _transaction_exists(self, spec: dict, cols: dict, file_id) -> bool:
        """Same transaction already recorded from another source (overlapping exports, chat then file)."""
        checks = []
        for attr in ("date", "amount", "party", "offering"):
            f = schema.role_field(spec, attr)
            if f:
                c = schema.column(f)
                checks.append((c, cols.get(c)))
        rf = schema.field(spec, "receipt")
        if rf and cols.get("receipt"):
            checks.append(("receipt", cols["receipt"]))
        tf = schema.role_field(spec, "time")
        if tf and cols.get(tf["name"]):
            checks.append((tf["name"], cols[tf["name"]]))
        where = " AND ".join(f"{c} IS ?" for c, _ in checks)
        sql = f"SELECT 1 FROM {schema.table(spec['name'])} WHERE {where} AND (_file_id IS NULL OR _file_id IS NOT ?)"
        if file_id is None:
            sql = f"SELECT 1 FROM {schema.table(spec['name'])} WHERE {where}"
            return self.conn.execute(sql, [v for _, v in checks]).fetchone() is not None
        return self.conn.execute(sql, [v for _, v in checks] + [file_id]).fetchone() is not None

    def _cost_exists(self, spec: dict, cols: dict, file_id) -> bool:
        checks = [(schema.column(f), cols.get(schema.column(f))) for f in spec["fields"] if f.get("role_attr") in ("month", "amount", "category")]
        where = " AND ".join(f"{c} IS ?" for c, _ in checks)
        return self.conn.execute(f"SELECT 1 FROM {schema.table(spec['name'])} WHERE {where} AND (_file_id IS NULL OR _file_id IS NOT ?)",
                                 [v for _, v in checks] + [file_id]).fetchone() is not None

    # resolution ----------------------------------------------------------------
    def alias(self, entity: str, key: str) -> Optional[int]:
        r = self.conn.execute("SELECT record_id FROM aliases WHERE entity=? AND key=?", (entity, key)).fetchone()
        return r["record_id"] if r else None

    def add_alias(self, entity: str, key: str, rid: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO aliases(entity, key, record_id) VALUES (?,?,?)", (entity, key, rid))

    def resolve_ref(self, target: dict, attrs: dict, when: Optional[str], from_transaction: bool) -> Optional[int]:
        role = target["role"]
        name = attrs.get("name")
        if not name and (attrs.get("first") or attrs.get("last")):
            name = " ".join(x for x in (attrs.get("first"), attrs.get("last")) if x)
        if role == "party":
            phone = phone_key(attrs.get("phone"), self.country) if attrs.get("phone") else None
            for key, val in attrs.items():                     # other identifiers, e.g. a loyalty card number
                f = schema.field(target, key)
                if key not in ("name", "phone", "first", "last") and f and val not in (None, ""):
                    hit = self.conn.execute(f"SELECT id FROM {schema.table(target['name'])} WHERE {f['name']}=?",
                                            (norm_text(val),)).fetchone()
                    if hit:
                        return hit["id"]
            if not name and not phone:
                return None
            cols = {"__first_seen": when}
            pf = schema.role_field(target, "phone")
            if phone and pf:
                cols[pf["name"]] = phone
            rid, _ = self.resolve_party(display_name(name) if name else phone, cols, {}, provisional=from_transaction)
            return rid
        if not name:
            return None
        if role == "offering":
            return self.resolve_offering(norm_text(name))
        rid, _ = self.upsert_named(target, display_name(name), {})
        return rid

    def resolve_party(self, name: Optional[str], cols: dict, extra: dict, provisional: bool = False, source: str = "file",
                      name_guess: Optional[bool] = None):
        spec = schema.primary(self.conn, "party")
        ent, tbl = spec["name"], schema.table(spec["name"])
        nf = schema.role_field(spec, "name")["name"]
        pf = schema.role_field(spec, "phone")
        ff = schema.role_field(spec, "first_seen")
        notes_f = schema.role_field(spec, "notes")
        seen = cols.pop("__first_seen", None)
        phone = cols.get(pf["name"]) if pf else None
        nk = name_key(name) if name else ""
        rid = self.alias(ent, f"p:{phone}") if phone else None
        if rid is None and nk:
            cand = self.alias(ent, f"n:{nk}")
            if cand is not None:
                other = self.conn.execute(f"SELECT {pf['name']} AS ph FROM {tbl} WHERE id=?", (cand,)).fetchone()["ph"] if pf else None
                if not (phone and other and other != phone):           # same name + different phone = different person
                    rid = cand
        created = False
        if rid is None:
            data = {nf: display_name(name) if name else phone, **{k: v for k, v in cols.items() if k != nf}}
            if ff and seen and not data.get(ff["name"]):
                data[ff["name"]] = seen
            data["_provisional"] = 1 if provisional else 0
            if name_guess if name_guess is not None else (name and (str(name).isupper() or str(name).islower())):
                extra = {**extra, "__name_guess": True}            # casing/order guessed; a better spelling may replace it
            data["_extra"] = json.dumps(extra, ensure_ascii=False)
            rid = self.conn.execute(f"INSERT INTO {tbl} ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})", list(data.values())).lastrowid
            created = True
            if provisional and not phone:
                self._pending_new.append((rid, display_name(name)))
        else:
            cur = dict(self.conn.execute(f"SELECT * FROM {tbl} WHERE id=?", (rid,)).fetchone())
            upd = {}
            cur_extra = json.loads(cur["_extra"] or "{}")
            better = name_guess is False if name_guess is not None else (name and not (str(name).isupper() or str(name).islower()))
            if cur_extra.get("__name_guess") and name and better and name_key(name) == name_key(cur[nf]):
                upd[nf] = display_name(name)
                cur_extra.pop("__name_guess")
                upd["_extra"] = json.dumps(cur_extra, ensure_ascii=False)
            for k, v in cols.items():
                if k == nf or v in (None, ""):
                    continue
                if notes_f and k == notes_f["name"] and cur.get(k) and str(v) not in str(cur[k]):
                    upd[k] = f"{cur[k]}\n{v}"
                elif cur.get(k) in (None, ""):
                    upd[k] = v
            if ff and seen and (not cur.get(ff["name"]) or seen < cur[ff["name"]]):
                upd[ff["name"]] = seen
            if extra:
                upd["_extra"] = json.dumps({**cur_extra, **extra}, ensure_ascii=False)
            if not provisional and cur["_provisional"]:
                upd["_provisional"] = 0
            if upd:
                self.conn.execute(f"UPDATE {tbl} SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", [*upd.values(), rid])
        if phone:
            owner = self.alias(ent, f"p:{phone}")
            if owner is None:
                self.add_alias(ent, f"p:{phone}", rid)
        if nk:
            self.add_alias(ent, f"n:{nk}", rid)
        return rid, created

    def upsert_named(self, spec: dict, name: str, cols: dict):
        tbl, nf = schema.table(spec["name"]), schema.role_field(spec, "name")["name"]
        key = f"n:{name_key(name)}"
        rid = self.alias(spec["name"], key)
        if rid is None:
            data = {nf: name, **cols}
            rid = self.conn.execute(f"INSERT INTO {tbl} ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})", list(data.values())).lastrowid
            self.add_alias(spec["name"], key, rid)
            return rid, True
        if cols:
            self.conn.execute(f"UPDATE {tbl} SET {', '.join(f'{k}=COALESCE(?, {k})' for k in cols)} WHERE id=?", [*cols.values(), rid])
        return rid, False

    def match_offering(self, raw: str) -> Optional[int]:
        """Existing offering for a name variant, without creating anything."""
        spec = schema.primary(self.conn, "offering")
        if spec is None:
            return None
        ent, nf = spec["name"], schema.role_field(spec, "name")["name"]
        key = compact_key(raw)
        rid = self.alias(ent, f"a:{key}")
        if rid:
            return rid
        names = {r["id"]: r["n"] for r in self.conn.execute(f"SELECT id, {nf} AS n FROM {schema.table(ent)}")}
        hit = next((i for i, n in names.items() if compact_key(n) == key), None)        # same name, other spelling
        if hit is None:
            for group in offering_groups(self.vertical):                                   # known trade variants
                gk = {compact_key(g) for g in group}
                if key in gk:
                    found = [i for i, n in names.items() if compact_key(n) in gk]
                    if len(found) == 1:
                        hit = found[0]
                        break
        if hit is None and len(key) >= 4:
            found = [i for i, n in names.items() if key in compact_key(n) or compact_key(n) in key]
            # only a unique containment counts, and never when both are multi-word names that merely share a word
            if len(found) == 1 and not (len(fold(raw).split()) > 1 and len(fold(names[found[0]]).split()) > 1):
                hit = found[0]
        if hit is None:
            toks = set(fold(raw).split())
            found = [i for i, n in names.items() if toks and toks == set(fold(n).split())]
            hit = found[0] if len(found) == 1 else None
        if hit:
            self.add_alias(ent, f"a:{key}", hit)
        return hit

    def upsert_offering(self, name: str, cols: dict, exact: bool = True):
        """From a price list / product list: only an exact (spelling-insensitive) name match updates."""
        spec = schema.primary(self.conn, "offering")
        rid = self.alias(spec["name"], f"a:{compact_key(name)}") if exact else self.match_offering(name)
        if rid is None:
            data = {schema.role_field(spec, "name")["name"]: name, **cols}
            rid = self.conn.execute(f"INSERT INTO {schema.table(spec['name'])} ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                                    list(data.values())).lastrowid
            self.add_alias(spec["name"], f"a:{compact_key(name)}", rid)
            return rid, True
        if cols:
            self.conn.execute(f"UPDATE {schema.table(spec['name'])} SET {', '.join(f'{k}=COALESCE(?, {k})' for k in cols)} WHERE id=?",
                              [*cols.values(), rid])
        return rid, False

    def resolve_offering(self, raw: str) -> int:
        rid = self.match_offering(raw)
        if rid:
            return rid
        rid, _ = self.upsert_offering(display_name(raw) if raw.islower() or raw.isupper() else raw, {})
        self.question("unknown_offering", f"'{raw}' is not in the price list. What is its price and cost?",
                      {"record_id": rid, "name": raw})
        return rid

    def _flush_new_parties(self, file_name: str) -> None:
        """One grouped question per file, and only when a customer list exists to compare against."""
        pending, self._pending_new = self._pending_new, []
        if not pending:
            return
        party = schema.primary(self.conn, "party")
        has_list = self.conn.execute("SELECT 1 FROM raw_files WHERE entity=?", (party["name"],)).fetchone() is not None
        if not has_list:          # no customer list yet: people seen in sales simply are the customers
            for rid, _ in pending:
                self.conn.execute(f"UPDATE {schema.table(party['name'])} SET _provisional=0 WHERE id=?", (rid,))
            return
        names = [n for _, n in pending]
        self.question("new_parties", f"{len(names)} people in '{file_name}' are not in the customer list: {', '.join(names[:12])}"
                      + (" ..." if len(names) > 12 else "") + ". Are they new customers (confirm) or spelling variants of existing ones (merge)?",
                      {"entity": party["name"], "record_ids": [r for r, _ in pending], "names": names, "file": file_name, "count": len(names)})

    def question(self, kind: str, text: str, payload: dict) -> None:
        if self.conn.execute("SELECT 1 FROM questions WHERE kind=? AND status='open' AND payload=?",
                             (kind, json.dumps(payload, ensure_ascii=False))).fetchone():
            return
        self.conn.execute("INSERT INTO questions(kind, text, payload) VALUES (?,?,?)", (kind, text, json.dumps(payload, ensure_ascii=False)))
