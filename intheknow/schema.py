"""Dynamic schema engine.

Every business gets real tables shaped for its trade (visits and stylists for a salon,
appointments and practitioners for a clinic, sales and products for a café, pets for a
dog groomer...). Each table is bound to a fixed *role* so metrics and UI work for all:

    party        customers / patients / members         (required: name)
    staff        stylists / practitioners / baristas    (required: name)
    offering     services / treatments / products       (required: name)
    transaction  visits / appointments / sales          (required: date, amount)
    cost         expenses                                (required: month, amount)
    resource     rooms / chairs / machines              (required: name)
    custom       anything else (pets, memberships...)   (needs at least one field)

Specs are validated in code, applied in one transaction, versioned in schema_changes and
can be undone. Role views (v_parties, v_transactions, ...) are rebuilt after every change
and are the only contract the metrics, tools and UI depend on.
"""
from __future__ import annotations

import json
import re
import sqlite3
from copy import deepcopy
from typing import Any, Optional

LANGS = ("en", "fr", "ro", "de")
FIELD_TYPES = {"text", "number", "money", "date", "time", "month", "phone", "email", "enum", "bool", "ref"}
SQL_TYPE = {"text": "TEXT", "number": "REAL", "money": "INTEGER", "date": "TEXT", "time": "TEXT", "month": "TEXT",
            "phone": "TEXT", "email": "TEXT", "enum": "TEXT", "bool": "INTEGER"}
MAX_ENTITIES = 25
MAX_FIELDS = 40
SINGLETON_ROLES = {"party", "staff", "offering", "cost"}
MAX_TRANSACTION_ENTITIES = 3

# role -> attribute -> (type, required)
ROLES: dict[str, dict[str, tuple[str, bool]]] = {
    "party": {"name": ("text", True), "phone": ("phone", False), "email": ("email", False),
              "first_seen": ("date", False), "notes": ("text", False)},
    "staff": {"name": ("text", True)},
    "offering": {"name": ("text", True), "price": ("money", False), "unit_cost": ("money", False),
                 "category": ("text", False), "duration_min": ("number", False)},
    "transaction": {"date": ("date", True), "amount": ("money", True), "party": ("ref", False),
                    "offering": ("ref", False), "staff": ("ref", False), "quantity": ("number", False),
                    "status": ("enum", False), "time": ("time", False)},
    "cost": {"month": ("month", True), "amount": ("money", True), "category": ("text", False)},
    "resource": {"name": ("text", True)},
    "custom": {},
}
# Transactions with these statuses are not revenue (no-shows, cancellations).
NON_REVENUE_STATUS = ("no_show", "cancelled")
SYSTEM_COLUMNS = (
    "_source TEXT", "_file_id INTEGER", "_source_row INTEGER", "_quote TEXT",
    "_extra TEXT NOT NULL DEFAULT '{}'", "_provisional INTEGER NOT NULL DEFAULT 0",
    "_created TEXT NOT NULL DEFAULT (datetime('now'))",
)
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
RESERVED = {"id", "select", "from", "where", "table", "order", "group", "index", "values", "default"}


class SchemaError(ValueError):
    pass


# --- Registry access ---------------------------------------------------------

def entities(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["name"]: json.loads(r["spec"]) for r in conn.execute("SELECT name, spec FROM entity_types ORDER BY rowid")}


def entity(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    r = conn.execute("SELECT spec FROM entity_types WHERE name=?", (name,)).fetchone()
    return json.loads(r["spec"]) if r else None


def by_role(conn: sqlite3.Connection, role: str) -> list[dict]:
    return [s for s in entities(conn).values() if s["role"] == role]


def primary(conn: sqlite3.Connection, role: str) -> Optional[dict]:
    found = by_role(conn, role)
    return found[0] if found else None


def field(spec: dict, name: str) -> Optional[dict]:
    return next((f for f in spec["fields"] if f["name"] == name), None)


def role_field(spec: dict, attr: str) -> Optional[dict]:
    """The field of an entity that plays a role attribute (e.g. the 'amount' of a visit)."""
    return next((f for f in spec["fields"] if f.get("role_attr") == attr), None)


def table(name: str) -> str:
    return f"e_{name}"


def column(f: dict) -> str:
    return f"{f['name']}_id" if f["type"] == "ref" else f["name"]


def label(spec: dict, lang: str = "en", plural: bool = False) -> str:
    lab = spec.get("label", {}).get(lang) or spec.get("label", {}).get("en") or [spec["name"], spec["name"]]
    return lab[1 if plural else 0] if isinstance(lab, list) else lab


# --- Validation ----------------------------------------------------------------

def _check_name(kind: str, name: str) -> None:
    if not isinstance(name, str) or not _NAME.match(name) or name in RESERVED or name.startswith("_"):
        raise SchemaError(f"invalid {kind} name {name!r}: use lowercase letters, digits, underscore (2-40 chars)")


def validate_field(f: dict, known_entities: set[str], role: str) -> dict:
    f = deepcopy(f)
    _check_name("field", f.get("name"))
    if f.get("type") not in FIELD_TYPES:
        raise SchemaError(f"field {f['name']}: type must be one of {sorted(FIELD_TYPES)}")
    if f["type"] == "ref":
        if f.get("ref") not in known_entities:
            raise SchemaError(f"field {f['name']}: ref must name an existing entity ({sorted(known_entities)})")
    if f["type"] == "enum":
        vals = f.get("values") or []
        if not vals or not all(isinstance(v, str) and _NAME.match(v) for v in vals):
            raise SchemaError(f"field {f['name']}: enum needs 'values' as lowercase identifiers")
    attr = f.get("role_attr")
    if attr is not None:
        if attr not in ROLES[role]:
            raise SchemaError(f"field {f['name']}: role_attr {attr!r} not valid for role {role} ({sorted(ROLES[role])})")
        expected = ROLES[role][attr][0]
        if expected != f["type"] and not (expected == "text" and f["type"] in ("text", "enum")):
            raise SchemaError(f"field {f['name']}: role_attr {attr} needs type {expected}")
    for key in ("synonyms",):
        if key in f and not all(isinstance(s, str) for s in f[key]):
            raise SchemaError(f"field {f['name']}: synonyms must be strings")
    if "sensitive" in f and not isinstance(f["sensitive"], bool):
        raise SchemaError(f"field {f['name']}: sensitive must be true or false")
    f.setdefault("synonyms", [])
    f.setdefault("label", {})
    return f


def validate_spec(conn: sqlite3.Connection, spec: dict, new: bool = True) -> dict:
    spec = deepcopy(spec)
    _check_name("entity", spec.get("name"))
    role = spec.get("role")
    if role not in ROLES:
        raise SchemaError(f"role must be one of {sorted(ROLES)}")
    existing = entities(conn)
    if new:
        if spec["name"] in existing:
            raise SchemaError(f"entity {spec['name']} already exists; add fields with add_field")
        if len(existing) >= MAX_ENTITIES:
            raise SchemaError(f"limit of {MAX_ENTITIES} entities reached")
        if role in SINGLETON_ROLES and any(s["role"] == role for s in existing.values()):
            raise SchemaError(f"this business already has a {role} entity; add fields to it instead")
        if role == "transaction" and sum(s["role"] == "transaction" for s in existing.values()) >= MAX_TRANSACTION_ENTITIES:
            raise SchemaError("too many transaction entities")
    fields = spec.get("fields") or []
    if not fields:
        raise SchemaError("an entity needs at least one field")
    if len(fields) > MAX_FIELDS:
        raise SchemaError(f"max {MAX_FIELDS} fields")
    known = set(existing) | {spec["name"]}
    spec["fields"] = [validate_field(f, known, role) for f in fields]
    names = [f["name"] for f in spec["fields"]]
    if len(names) != len(set(names)):
        raise SchemaError("duplicate field names")
    attrs = [f["role_attr"] for f in spec["fields"] if f.get("role_attr")]
    if len(attrs) != len(set(attrs)):
        raise SchemaError("a role attribute is assigned to two fields")
    missing = {a for a, (_, req) in ROLES[role].items() if req} - set(attrs)
    if missing:
        raise SchemaError(f"role {role} requires fields with role_attr {sorted(missing)}")
    labels = spec.setdefault("label", {})
    for lang in LANGS:
        labels.setdefault(lang, labels.get("en") or [spec["name"].replace("_", " ").title()] * 2)
    spec.setdefault("synonyms", [])
    return spec


# --- Apply / undo ----------------------------------------------------------------

def _log(conn, op: str, payload: dict, origin: str, status: str = "applied") -> int:
    return conn.execute(
        "INSERT INTO schema_changes(op, payload, status, origin) VALUES (?,?,?,?)",
        (op, json.dumps(payload, ensure_ascii=False), status, origin),
    ).lastrowid


def create_entity(conn: sqlite3.Connection, spec: dict, origin: str = "system") -> int:
    spec = validate_spec(conn, spec, new=True)
    cols = ["id INTEGER PRIMARY KEY"] + [f"{column(f)} {SQL_TYPE.get(f['type'], 'INTEGER')}" for f in spec["fields"]] + list(SYSTEM_COLUMNS)
    conn.execute(f"CREATE TABLE {table(spec['name'])} ({', '.join(cols)})")
    for f in spec["fields"]:
        if f["type"] == "ref" or f.get("role_attr") in ("date", "month"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{spec['name']}_{column(f)} ON {table(spec['name'])}({column(f)})")
    conn.execute("INSERT INTO entity_types(name, role, spec) VALUES (?,?,?)", (spec["name"], spec["role"], json.dumps(spec, ensure_ascii=False)))
    change = _log(conn, "create_entity", {"entity": spec["name"], "role": spec["role"]}, origin)
    rebuild_views(conn)
    return change


def add_field(conn: sqlite3.Connection, entity_name: str, f: dict, origin: str = "system") -> int:
    spec = entity(conn, entity_name)
    if spec is None:
        raise SchemaError(f"unknown entity {entity_name!r}")
    if len(spec["fields"]) >= MAX_FIELDS:
        raise SchemaError(f"{entity_name} already has {MAX_FIELDS} fields")
    f = validate_field(f, set(entities(conn)), spec["role"])
    if field(spec, f["name"]):
        raise SchemaError(f"{entity_name} already has a field {f['name']}")
    if f.get("role_attr") and role_field(spec, f["role_attr"]):
        raise SchemaError(f"{entity_name} already has a field for {f['role_attr']}")
    f.setdefault("label", {})
    conn.execute(f"ALTER TABLE {table(entity_name)} ADD COLUMN {column(f)} {SQL_TYPE.get(f['type'], 'INTEGER')}")
    spec["fields"].append(f)
    conn.execute("UPDATE entity_types SET spec=? WHERE name=?", (json.dumps(spec, ensure_ascii=False), entity_name))
    change = _log(conn, "add_field", {"entity": entity_name, "field": f["name"], "type": f["type"]}, origin)
    rebuild_views(conn)
    return change


def undo(conn: sqlite3.Connection, change_id: int) -> str:
    row = conn.execute("SELECT * FROM schema_changes WHERE id=?", (change_id,)).fetchone()
    if not row or row["status"] != "applied":
        raise SchemaError(f"change {change_id} cannot be undone")
    p = json.loads(row["payload"])
    if row["op"] == "add_field":
        spec = entity(conn, p["entity"])
        f = field(spec, p["field"])
        conn.execute(f"ALTER TABLE {table(p['entity'])} DROP COLUMN {column(f)}")
        spec["fields"] = [x for x in spec["fields"] if x["name"] != p["field"]]
        conn.execute("UPDATE entity_types SET spec=? WHERE name=?", (json.dumps(spec, ensure_ascii=False), p["entity"]))
        msg = f"removed field {p['field']} from {p['entity']}"
    elif row["op"] == "create_entity" and not conn.execute("SELECT 1 FROM trash WHERE change_id=?", (change_id,)).fetchone():
        refs = [n for n, s in entities(conn).items() if n != p["entity"] and any(f.get("ref") == p["entity"] for f in s["fields"])]
        if refs:
            raise SchemaError(f"{p['entity']} is referenced by {refs}; undo those first")
        conn.execute(f"DROP TABLE {table(p['entity'])}")
        conn.execute("DELETE FROM entity_types WHERE name=?", (p["entity"],))
        conn.execute("DELETE FROM aliases WHERE entity=?", (p["entity"],))
        msg = f"removed entity {p['entity']}"
    elif row["op"] in ("add_fact_kind", "add_relation_type"):
        from .people import undo_vocab
        msg = undo_vocab(conn, row["op"][4:], p["name"])
    else:
        return undo_structural(conn, row)
    conn.execute("UPDATE schema_changes SET status='undone' WHERE id=?", (change_id,))
    rebuild_views(conn)
    return msg


def apply_pack(conn: sqlite3.Connection, pack: dict) -> list[int]:
    """Create the entities of a vertical pack (in dependency order)."""
    changes = []
    for spec in pack["entities"]:
        if entity(conn, spec["name"]) is None:
            changes.append(create_entity(conn, spec, origin="pack"))
    return changes


# --- Role views ----------------------------------------------------------------

def _sel(spec: Optional[dict], attr: str, alias: str = "t", default: str = "NULL") -> str:
    if spec is None:
        return default
    f = role_field(spec, attr)
    return f"{alias}.{column(f)}" if f else default


def rebuild_views(conn: sqlite3.Connection) -> None:
    for v in ("v_facts", "v_relations", "v_parties", "v_staff", "v_offerings", "v_transactions", "v_costs", "v_customers", "v_visits", "v_expenses"):
        conn.execute(f"DROP VIEW IF EXISTS {v}")
    party, staff, offering, cost = (primary(conn, r) for r in ("party", "staff", "offering", "cost"))

    if party:
        conn.execute(f"""CREATE VIEW v_parties AS SELECT t.id, {_sel(party,'name')} AS name, {_sel(party,'phone')} AS phone,
            {_sel(party,'email')} AS email, {_sel(party,'first_seen')} AS first_seen, {_sel(party,'notes')} AS notes,
            t._provisional AS provisional, t._extra AS extra FROM {table(party['name'])} t""")
    else:
        conn.execute("CREATE VIEW v_parties AS SELECT NULL id, NULL name, NULL phone, NULL email, NULL first_seen, NULL notes, 0 provisional, '{}' extra WHERE 0")
    if staff:
        conn.execute(f"CREATE VIEW v_staff AS SELECT t.id, {_sel(staff,'name')} AS name FROM {table(staff['name'])} t")
    else:
        conn.execute("CREATE VIEW v_staff AS SELECT NULL id, NULL name WHERE 0")
    if offering:
        conn.execute(f"""CREATE VIEW v_offerings AS SELECT t.id, {_sel(offering,'name')} AS name, {_sel(offering,'price')} AS price,
            {_sel(offering,'unit_cost')} AS unit_cost, {_sel(offering,'category')} AS category FROM {table(offering['name'])} t""")
    else:
        conn.execute("CREATE VIEW v_offerings AS SELECT NULL id, NULL name, NULL price, NULL unit_cost, NULL category WHERE 0")

    parts = []
    for tx in by_role(conn, "transaction"):
        pf, of, sf = role_field(tx, "party"), role_field(tx, "offering"), role_field(tx, "staff")
        qty = _sel(tx, "quantity", default="1")
        status = _sel(tx, "status")
        joins = []
        if pf:
            joins.append(f"LEFT JOIN v_parties p ON p.id = t.{column(pf)}")
        if of:
            joins.append(f"LEFT JOIN v_offerings o ON o.id = t.{column(of)}")
        if sf:
            joins.append(f"LEFT JOIN v_staff s ON s.id = t.{column(sf)}")
        parts.append(f"""SELECT '{tx['name']}' AS entity, t.id, {_sel(tx,'date')} AS date, {_sel(tx,'time')} AS time,
            {'p.id' if pf else 'NULL'} AS party_id, {'p.name' if pf else 'NULL'} AS party, {'p.phone' if pf else 'NULL'} AS phone,
            {'o.id' if of else 'NULL'} AS offering_id, {'o.name' if of else 'NULL'} AS offering, {'o.category' if of else 'NULL'} AS category,
            {'s.id' if sf else 'NULL'} AS staff_id, {'s.name' if sf else 'NULL'} AS staff,
            {_sel(tx,'amount')} AS amount, COALESCE({qty}, 1) AS quantity, {status} AS status,
            CASE WHEN {status} IN {NON_REVENUE_STATUS} THEN 0 ELSE 1 END AS counted,
            {'COALESCE(o.unit_cost, 0) * COALESCE(' + qty + ', 1)' if of else '0'} AS unit_cost,
            {'(o.unit_cost IS NULL)' if of else '1'} AS unit_cost_missing, t._source AS source
            FROM {table(tx['name'])} t {' '.join(joins)}""")
    if parts:
        conn.execute("CREATE VIEW v_transactions AS " + " UNION ALL ".join(parts))
    else:
        conn.execute("""CREATE VIEW v_transactions AS SELECT NULL entity, NULL id, NULL date, NULL time, NULL party_id, NULL party,
            NULL phone, NULL offering_id, NULL offering, NULL category, NULL staff_id, NULL staff, 0 amount, 1 quantity,
            NULL status, 1 counted, 0 unit_cost, 1 unit_cost_missing, NULL source WHERE 0""")
    if cost:
        conn.execute(f"""CREATE VIEW v_costs AS SELECT t.id, {_sel(cost,'month')} AS month, COALESCE({_sel(cost,'category')}, 'other') AS category,
            {_sel(cost,'amount')} AS amount FROM {table(cost['name'])} t""")
    else:
        conn.execute("CREATE VIEW v_costs AS SELECT NULL id, NULL month, NULL category, 0 amount WHERE 0")
    # People layer: special facts are masked; the raw table is not readable from query_data.
    conn.execute(f"""CREATE VIEW v_facts AS SELECT f.id, f.record_id AS person_id, p.name AS person, f.kind, f.key,
        CASE WHEN f.sensitivity = 'special' THEN '{'[sensitive]'}' ELSE f.value END AS value, f.polarity, f.sensitivity, f.confidence,
        f.observed_at, f.valid_until, (f.superseded_by IS NULL) AS current, f.source
        FROM facts f LEFT JOIN v_parties p ON p.id = f.record_id WHERE f.entity = {f"'{party['name']}'" if party else 'NULL'}""")
    conn.execute("""CREATE VIEW v_relations AS SELECT r.id, r.type, r.a_id, pa.name AS a_name, r.b_id, pb.name AS b_name, r.since, r.until
        FROM relations r LEFT JOIN v_parties pa ON pa.id = r.a_id LEFT JOIN v_parties pb ON pb.id = r.b_id""")
    # Customer roll-up used by tools and query_data.
    conn.execute("""CREATE VIEW v_customers AS
        SELECT p.id, p.name, p.phone, p.provisional,
               COUNT(x.id) AS visits, COALESCE(SUM(x.amount), 0) AS total_spend,
               CASE WHEN p.first_seen IS NULL THEN MIN(x.date)
                    WHEN MIN(x.date) IS NULL OR p.first_seen < MIN(x.date) THEN p.first_seen ELSE MIN(x.date) END AS first_seen,
               MIN(x.date) AS first_visit_in_data, MAX(x.date) AS last_visit
        FROM v_parties p LEFT JOIN v_transactions x ON x.party_id = p.id AND x.counted = 1
        GROUP BY p.id""")


def describe(conn: sqlite3.Connection, lang: str = "en") -> str:
    """Compact text description of the current schema (for the assistant)."""
    lines = []
    for name, spec in entities(conn).items():
        fields = ", ".join(
            f"{f['name']}:{f['type']}" + (f"->{f['ref']}" if f["type"] == "ref" else "") + (f" [{f['role_attr']}]" if f.get("role_attr") else "")
            for f in spec["fields"])
        lines.append(f"- {name} ({spec['role']}): {fields}")
    return "\n".join(lines) or "(no entities yet)"


def public_spec(spec: dict) -> dict[str, Any]:
    return {"name": spec["name"], "role": spec["role"], "label": spec.get("label", {}),
            "fields": [{k: f[k] for k in ("name", "type", "role_attr", "ref", "values", "label", "sensitive") if k in f} for f in spec["fields"]]}


# --- Structural edits (all undoable through a snapshot in `trash`) ---------------------------

def _rows(conn: sqlite3.Connection, tbl: str, where: str = "1", params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {tbl} WHERE {where}", params)]


def snapshot(conn: sqlite3.Connection, change_id: int, tables: dict[str, list[dict]], **extra) -> None:
    conn.execute("INSERT OR REPLACE INTO trash(change_id, data) VALUES (?,?)",
                 (change_id, json.dumps({"tables": tables, **extra}, ensure_ascii=False, default=str)))


def restore_rows(conn: sqlite3.Connection, tables: dict[str, list[dict]]) -> None:
    for tbl, rows in tables.items():
        for r in rows:
            conn.execute(f"DELETE FROM {tbl} WHERE id=?", (r["id"],))
            conn.execute(f"INSERT INTO {tbl} ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", list(r.values()))


def _save_spec(conn, spec: dict) -> None:
    conn.execute("UPDATE entity_types SET spec=? WHERE name=?", (json.dumps(spec, ensure_ascii=False), spec["name"]))


def _required(spec: dict, f: dict) -> bool:
    return bool(f.get("role_attr") and ROLES[spec["role"]].get(f["role_attr"], ("", False))[1])


def _referencing(conn, entity_name: str) -> list[tuple[str, dict]]:
    return [(n, f) for n, s in entities(conn).items() for f in s["fields"] if f["type"] == "ref" and f.get("ref") == entity_name]


def remove_field(conn: sqlite3.Connection, entity_name: str, field_name: str, origin: str = "assistant") -> int:
    spec = entity(conn, entity_name)
    if spec is None:
        raise SchemaError(f"unknown entity {entity_name!r}")
    f = field(spec, field_name)
    if f is None:
        raise SchemaError(f"{entity_name} has no field {field_name!r} ({[x['name'] for x in spec['fields']]})")
    if _required(spec, f):
        raise SchemaError(f"{field_name} is required for a {spec['role']} table and cannot be removed")
    col = column(f)
    values = [{"id": r["id"], col: r[col]} for r in conn.execute(f"SELECT id, {col} FROM {table(entity_name)} WHERE {col} IS NOT NULL")]
    conn.execute(f"DROP INDEX IF EXISTS ix_{entity_name}_{col}")
    conn.execute(f"ALTER TABLE {table(entity_name)} DROP COLUMN {col}")
    spec["fields"] = [x for x in spec["fields"] if x["name"] != field_name]
    _save_spec(conn, spec)
    change = _log(conn, "remove_field", {"entity": entity_name, "field": field_name, "rows": len(values)}, origin)
    snapshot(conn, change, {}, field=f, values=values, entity=entity_name)
    rebuild_views(conn)
    return change


def rename_field(conn: sqlite3.Connection, entity_name: str, field_name: str, new_name: Optional[str] = None,
                 label: Optional[dict] = None, origin: str = "assistant") -> int:
    spec = entity(conn, entity_name)
    if spec is None:
        raise SchemaError(f"unknown entity {entity_name!r}")
    f = field(spec, field_name)
    if f is None:
        raise SchemaError(f"{entity_name} has no field {field_name!r}")
    before = deepcopy(spec)
    if new_name and new_name != field_name:
        _check_name("field", new_name)
        if field(spec, new_name):
            raise SchemaError(f"{entity_name} already has a field {new_name!r}")
        old_col = column(f)
        f["name"] = new_name
        conn.execute(f"ALTER TABLE {table(entity_name)} RENAME COLUMN {old_col} TO {column(f)}")
        for r in conn.execute("SELECT signature, mapping FROM mapping_memory WHERE entity=?", (entity_name,)).fetchall():
            m = {h: (new_name + t[len(field_name):] if t and (t == field_name or t.startswith(field_name + ".")) else t)
                 for h, t in json.loads(r["mapping"]).items()}
            conn.execute("UPDATE mapping_memory SET mapping=? WHERE signature=?", (json.dumps(m, ensure_ascii=False), r["signature"]))
    if label:
        f["label"] = {lg: (v if isinstance(v, list) else [v, v]) for lg, v in label.items()}
    _save_spec(conn, spec)
    change = _log(conn, "rename_field", {"entity": entity_name, "field": field_name, "new_name": new_name or field_name}, origin)
    snapshot(conn, change, {}, spec_before=before)
    rebuild_views(conn)
    return change


def relabel_entity(conn: sqlite3.Connection, entity_name: str, label: dict, origin: str = "assistant") -> int:
    spec = entity(conn, entity_name)
    if spec is None:
        raise SchemaError(f"unknown entity {entity_name!r}")
    before = deepcopy(spec)
    spec["label"].update({lg: (v if isinstance(v, list) else [v, v]) for lg, v in label.items()})
    _save_spec(conn, spec)
    change = _log(conn, "relabel_entity", {"entity": entity_name}, origin)
    snapshot(conn, change, {}, spec_before=before)
    return change


def remove_entity(conn: sqlite3.Connection, entity_name: str, origin: str = "assistant") -> int:
    spec = entity(conn, entity_name)
    if spec is None:
        raise SchemaError(f"unknown entity {entity_name!r}")
    refs = [n for n, _ in _referencing(conn, entity_name) if n != entity_name]
    if refs:
        raise SchemaError(f"{entity_name} is linked from {refs}; remove those links first")
    rows = _rows(conn, table(entity_name))
    aliases = [dict(r) for r in conn.execute("SELECT * FROM aliases WHERE entity=?", (entity_name,))]
    conn.execute(f"DROP TABLE {table(entity_name)}")
    conn.execute("DELETE FROM entity_types WHERE name=?", (entity_name,))
    conn.execute("DELETE FROM aliases WHERE entity=?", (entity_name,))
    conn.execute("DELETE FROM mapping_memory WHERE entity=?", (entity_name,))
    change = _log(conn, "remove_entity", {"entity": entity_name, "rows": len(rows)}, origin)
    snapshot(conn, change, {}, spec=spec, rows=rows, aliases=aliases)
    rebuild_views(conn)
    return change


def replace_field(conn: sqlite3.Connection, entity_name: str, old: dict, new: dict, values: dict[int, object], origin: str) -> int:
    """Swap a field for a new definition (type change / text -> link) with already-converted values."""
    spec = entity(conn, entity_name)
    tbl = table(entity_name)
    old_col, new_col = column(old), column(new)
    backup = [{"id": r["id"], old_col: r[old_col]} for r in conn.execute(f"SELECT id, {old_col} FROM {tbl}")]
    tmp = f"__new_{new['name']}"
    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {tmp} {SQL_TYPE.get(new['type'], 'INTEGER')}")
    for rid, v in values.items():
        conn.execute(f"UPDATE {tbl} SET {tmp}=? WHERE id=?", (v, rid))
    conn.execute(f"DROP INDEX IF EXISTS ix_{entity_name}_{old_col}")
    conn.execute(f"ALTER TABLE {tbl} DROP COLUMN {old_col}")
    conn.execute(f"ALTER TABLE {tbl} RENAME COLUMN {tmp} TO {new_col}")
    if new["type"] == "ref":
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{entity_name}_{new_col} ON {tbl}({new_col})")
    before = deepcopy(spec)
    spec["fields"] = [new if x["name"] == old["name"] else x for x in spec["fields"]]
    _save_spec(conn, spec)
    change = _log(conn, "change_field", {"entity": entity_name, "field": old["name"], "from": old["type"], "to": new["type"]}, origin)
    snapshot(conn, change, {}, spec_before=before, old=old, new=new, values=backup)
    rebuild_views(conn)
    return change


def undo_structural(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Undo remove_field / rename_field / relabel_entity / remove_entity / change_field / edit_records."""
    t = conn.execute("SELECT data FROM trash WHERE change_id=?", (row["id"],)).fetchone()
    if t is None:
        raise SchemaError("no snapshot for this change")
    d = json.loads(t["data"])
    p = json.loads(row["payload"])
    op = row["op"]
    if op == "remove_field":
        f, ent = d["field"], d["entity"]
        spec = entity(conn, ent)
        conn.execute(f"ALTER TABLE {table(ent)} ADD COLUMN {column(f)} {SQL_TYPE.get(f['type'], 'INTEGER')}")
        for v in d["values"]:
            conn.execute(f"UPDATE {table(ent)} SET {column(f)}=? WHERE id=?", (v[column(f)], v["id"]))
        spec["fields"].append(f)
        _save_spec(conn, spec)
        msg = f"field {f['name']} restored in {ent}"
    elif op in ("rename_field", "relabel_entity"):
        before = d["spec_before"]
        cur = entity(conn, before["name"])
        if op == "rename_field" and p["new_name"] != p["field"]:
            f_now = field(cur, p["new_name"])
            f_old = field(before, p["field"])
            conn.execute(f"ALTER TABLE {table(before['name'])} RENAME COLUMN {column(f_now)} TO {column(f_old)}")
        _save_spec(conn, before)
        msg = f"{op.replace('_', ' ')} reverted"
    elif op == "remove_entity":
        spec = d["spec"]
        cols = ["id INTEGER PRIMARY KEY"] + [f"{column(f)} {SQL_TYPE.get(f['type'], 'INTEGER')}" for f in spec["fields"]] + list(SYSTEM_COLUMNS)
        conn.execute(f"CREATE TABLE {table(spec['name'])} ({', '.join(cols)})")
        conn.execute("INSERT INTO entity_types(name, role, spec) VALUES (?,?,?)", (spec["name"], spec["role"], json.dumps(spec, ensure_ascii=False)))
        restore_rows(conn, {table(spec["name"]): d["rows"]})
        for a in d["aliases"]:
            conn.execute("INSERT OR IGNORE INTO aliases(entity, key, record_id) VALUES (?,?,?)", (a["entity"], a["key"], a["record_id"]))
        msg = f"table {spec['name']} restored with {len(d['rows'])} rows"
    elif op == "change_field":
        ent, old, new = p["entity"], d["old"], d["new"]
        tbl = table(ent)
        conn.execute(f"DROP INDEX IF EXISTS ix_{ent}_{column(new)}")
        conn.execute(f"ALTER TABLE {tbl} DROP COLUMN {column(new)}")
        conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {column(old)} {SQL_TYPE.get(old['type'], 'INTEGER')}")
        for v in d["values"]:
            conn.execute(f"UPDATE {tbl} SET {column(old)}=? WHERE id=?", (v[column(old)], v["id"]))
        _save_spec(conn, d["spec_before"])
        msg = f"field {old['name']} restored as {old['type']}"
    elif op == "edit_records":
        for tbl, ids in (d.get("inserted") or {}).items():
            for i in ids:
                conn.execute(f"DELETE FROM {tbl} WHERE id=?", (i,))
        restore_rows(conn, d["tables"])
        for a in d.get("aliases") or []:
            conn.execute("INSERT OR REPLACE INTO aliases(entity, key, record_id) VALUES (?,?,?)", (a["entity"], a["key"], a["record_id"]))
        msg = f"{p.get('action', 'edit')} on {p.get('entity')} reverted ({p.get('rows', 0)} rows)"
    else:
        raise SchemaError(f"operation {op} is not undoable")
    conn.execute("UPDATE schema_changes SET status='undone' WHERE id=?", (row["id"],))
    rebuild_views(conn)
    return msg
