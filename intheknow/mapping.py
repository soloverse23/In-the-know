"""Column mapping against the business's own (dynamic) entities.

A file's columns are mapped to *targets* of one entity:
    "date", "amount"                    scalar fields
    "name.first", "name.last"           parts of a person-name field (composed into one name)
    "customer.name", "customer.phone"   attributes of a referenced record (resolved, created if new)
    "extra" / "ignore"                  keep in _extra JSON / drop
Order of trust: remembered layout -> synonym match (exact, then token overlap) -> the
assistant (map_columns). Values are checked for plausibility before a mapping is accepted.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from . import schema
from .normalize import header_key, infer_date_order, parse_date, parse_money, parse_month, phone_key


@dataclass
class Mapping:
    entity: str
    columns: dict[str, Optional[str]]            # header -> target | "extra" | None (ignore)
    origin: str = "synonyms"                     # memory | synonyms | assistant | records
    unmatched: list[str] = field(default_factory=list)
    date_order: Optional[str] = None

    def header_for(self, target: str) -> Optional[str]:
        return next((h for h, t in self.columns.items() if t == target), None)


def layout_signature(headers: list[str]) -> str:
    keys = sorted(header_key(h) for h in headers if header_key(h))
    return hashlib.sha256("|".join(keys).encode()).hexdigest()[:16]


ROLE_WORDS = {   # generic words for a link column, whatever the trade calls it
    "offering": ["offering", "offer", "service", "services", "product", "products", "item", "article", "treatment", "menu item",
                 "serviciu", "produs", "articol", "prestation", "produit", "soin", "acte", "leistung", "produkt", "artikel", "behandlung"],
    "party": ["customer", "client", "guest", "member", "patient", "buyer", "clienta", "pacient", "kunde", "kundin", "gast"],
    "staff": ["staff", "employee", "seller", "cashier", "stylist", "barista", "angajat", "vanzator", "employe", "vendeur", "mitarbeiter", "verkaufer"],
}


def targets(spec: dict, conn: Optional[sqlite3.Connection] = None) -> dict[str, list[str]]:
    """target -> folded synonym keys for one entity."""
    out: dict[str, list[str]] = {}

    def keys(words):
        return [k for k in (header_key(w) for w in words) if k]

    for f in spec["fields"]:
        labels = [x for v in f.get("label", {}).values() for x in (v if isinstance(v, list) else [v])]
        if f["type"] == "ref":
            subs = dict(f.get("sub") or {"name": []})
            subs.setdefault("name", [])
            for sub, syn in subs.items():
                base = list(syn)
                if sub == "name":
                    base += [f["name"], f.get("ref", "")] + labels + ROLE_WORDS.get(f.get("role_attr"), []) + f.get("synonyms", [])
                    if conn is not None and f.get("ref"):
                        ref = schema.entity(conn, f["ref"])
                        if ref:
                            base += [x for v in ref.get("label", {}).values() for x in (v if isinstance(v, list) else [v])]
                out[f"{f['name']}.{sub}"] = keys(base)
        else:
            out[f["name"]] = keys(f.get("synonyms", []) + [f["name"].replace("_", " ")] + labels)
            for part, syn in (f.get("parts") or {}).items():
                out[f"{f['name']}.{part}"] = keys(syn)
    return out


def _score(h: str, syn: str) -> int:
    if h == syn:
        return 1000 + len(syn)
    ht, st = set(h.split()), set(syn.split())
    if st and st <= ht and len(syn) >= 3:
        return 100 + len(syn)            # synonym words all appear in the header
    if ht and ht <= st and len(h) >= 4:
        return 50 + len(h)               # header is a shorter form of a synonym
    return 0


def match_entity(spec: dict, headers: list[str], conn: Optional[sqlite3.Connection] = None) -> dict[str, Optional[str]]:
    tg = targets(spec, conn)
    candidates = []
    for h in headers:
        hk = header_key(h)
        if not hk:
            continue
        for t, syns in tg.items():
            best = max((_score(hk, s) for s in syns), default=0)
            if best:
                candidates.append((best, h, t))
    result: dict[str, Optional[str]] = {h: None for h in headers}
    used_t: set[str] = set()
    for score, h, t in sorted(candidates, key=lambda x: -x[0]):
        if result[h] is None and t not in used_t:
            result[h] = t
            used_t.add(t)
    return result


def _split_names(spec: dict, cols: dict[str, Optional[str]], records: list[dict]) -> dict[str, Optional[str]]:
    """'Nom' + 'Prénom': when a first-name column exists, a 'name' column of single words is the family name."""
    for target_first in [t for t in cols.values() if t and t.endswith(".first")]:
        base = target_first[: -len(".first")]
        if f"{base}.last" in cols.values():
            continue
        name_target = base if base in cols.values() else (f"{base}.name" if f"{base}.name" in cols.values() else None)
        if not name_target:
            continue
        h = next(h for h, t in cols.items() if t == name_target)
        vals = [str(r.get(h)).strip() for r in records[:80] if r.get(h) not in (None, "")]
        if vals and sum(" " not in v for v in vals) >= 0.7 * len(vals):
            cols[h] = f"{base}.last"
    return cols


def required_covered(spec: dict, mapped: set[str]) -> bool:
    for f in spec["fields"]:
        attr = f.get("role_attr")
        if not attr or not schema.ROLES[spec["role"]].get(attr, ("", False))[1]:
            continue
        if f["name"] in mapped:
            continue
        if f["type"] == "text" and {f"{f['name']}.first", f"{f['name']}.last"} <= mapped:
            continue
        return False
    if spec["role"] == "custom":
        return bool(mapped)
    return True


def _plausible(spec: dict, cols: dict[str, Optional[str]], records: list[dict]) -> bool:
    sample = records[:60]
    if not sample:
        return True
    for h, t in cols.items():
        if not t or "." in t:
            continue
        f = schema.field(spec, t)
        if not f or not (f.get("role_attr") and schema.ROLES[spec["role"]].get(f["role_attr"], ("", False))[1]):
            continue
        vals = [r.get(h) for r in sample if r.get(h) not in (None, "")]
        if not vals:
            return False
        if f["type"] == "money":
            ok = sum(parse_money(v) is not None for v in vals)
        elif f["type"] == "date":
            order = infer_date_order(vals)
            ok = sum(parse_date(v, order) is not None for v in vals)
        elif f["type"] == "month":
            ok = sum(parse_month(v) is not None for v in vals)
        else:
            ok = sum(bool(re.search(r"[^\W\d_]", str(v))) for v in vals)
        if ok < 0.6 * len(vals):
            return False
    return True


def detect(conn: sqlite3.Connection, headers: list[str], records: list[dict]) -> Optional[Mapping]:
    row = conn.execute("SELECT entity, mapping, date_order FROM mapping_memory WHERE signature=?",
                       (layout_signature(headers),)).fetchone()
    if row and schema.entity(conn, row["entity"]):
        cols = json.loads(row["mapping"])
        return Mapping(row["entity"], cols, "memory", [h for h, t in cols.items() if t is None], row["date_order"])
    best, best_score = None, -1
    for name, spec in schema.entities(conn).items():
        cols = _split_names(spec, match_entity(spec, headers, conn), records)
        mapped = {t for t in cols.values() if t}
        if not required_covered(spec, mapped) or not _plausible(spec, cols, records):
            continue
        # Prefer the entity that explains most columns; transactions win ties (they carry the most meaning).
        score = len(mapped) * 10 + (1 if spec["role"] == "transaction" else 0)
        if score > best_score:
            best_score, best = score, Mapping(name, cols, "synonyms", [h for h, t in cols.items() if t is None])
    return best


def remember(conn: sqlite3.Connection, headers: list[str], m: Mapping) -> None:
    conn.execute("INSERT OR REPLACE INTO mapping_memory(signature, entity, mapping, date_order) VALUES (?,?,?,?)",
                 (layout_signature(headers), m.entity, json.dumps(m.columns, ensure_ascii=False), m.date_order))


def infer_type(values: list) -> str:
    vals = [v for v in values if v not in (None, "")][:80]
    if not vals:
        return "text"
    n = len(vals)
    s = [str(v) for v in vals]
    if sum(bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", x.strip(), re.I)) for x in s) >= 0.8 * n:
        return "email"
    if sum(bool(re.fullmatch(r"\+?[\d\s\-().]{8,}", x.strip())) and phone_key(x) is not None for x in s) >= 0.8 * n \
            and all(len(re.sub(r"\D", "", x)) >= 9 for x in s):
        return "phone"
    order = infer_date_order(vals)
    if order and sum(parse_date(v, order) is not None for v in vals) >= 0.8 * n:
        return "date"
    if sum(bool(re.search(r"(€|lei|ron|eur|\$|£|chf)", x, re.I)) for x in s) >= 0.5 * n and \
            sum(parse_money(v) is not None for v in vals) >= 0.8 * n:
        return "money"
    if sum(isinstance(v, (int, float)) or bool(re.fullmatch(r"-?\d+([.,]\d+)?", x.strip())) for v, x in zip(vals, s)) >= 0.9 * n:
        return "number"
    return "text"


def field_name_for(spec: dict, header: str) -> str:
    base = header_key(header).replace(" ", "_")[:36] or "column"
    if not base[0].isalpha():
        base = "f_" + base
    name, i = base, 2
    while schema.field(spec, name) or name in schema.RESERVED:
        name, i = f"{base}_{i}", i + 1
    return name
