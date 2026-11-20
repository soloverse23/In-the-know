"""People layer: what we know about each person, who they are linked to, and how carefully to treat it.

- facts: typed, sourced, dated knowledge about a record (usually a person). Facts are never
  overwritten: a different value for the same (person, kind, key) raises a question, and the
  confirmed one supersedes the old one, which is kept as history.
- relations: typed links between two records (spouse_of, parent_of, referred_by, ...).
- vocabulary: fact kinds and relation types come from the pack and can be extended from chat
  (change_schema add_fact_kind / add_relation_type), with labels in en/fr/ro/de.
- sensitivity: 'special' facts (health, documents) are masked in briefs, query_data and exports
  unless asked for; packs can require recorded consent before they are stored.
- forget_person / export_person: erasure (with preview, one undo window, then purge) and
  subject-access export.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections import defaultdict
from typing import Any, Optional

from . import schema
from .normalize import fold, name_key, norm_text, phone_key
from .views import D, N, T, block, view

L = lambda en, fr, ro, de: {"en": en, "fr": fr, "ro": ro, "de": de}  # noqa: E731

POLARITIES = ("likes", "dislikes", "must", "must_not", "neutral")
POLARITY_ALIASES = {"like": "likes", "positive": "likes", "loves": "likes", "dislike": "dislikes", "negative": "dislikes",
                    "hates": "dislikes", "must_have": "must", "required": "must", "need": "must", "never": "must_not",
                    "must_not_have": "must_not", "no": "must_not", "none": "neutral"}
SENSITIVITY = ("normal", "private", "special")
CONFIDENCE = ("confirmed", "stated", "inferred")
FORGET_UNDO_DAYS = 7            # after this, the snapshot of a forgotten person is purged for good
MASK = "[sensitive]"


def _kind(label, polarity="neutral", sensitivity="normal", synonyms=()):
    return {"label": label, "polarity": polarity, "sensitivity": sensitivity, "synonyms": list(synonyms)}


BASE_FACT_KINDS: dict[str, dict] = {
    "note": _kind(L("Note", "Note", "Notă", "Notiz"), synonyms=["info", "detail", "detaliu", "remarque", "hinweis"]),
    "preference": _kind(L("Preference", "Préférence", "Preferință", "Vorliebe"), "likes",
                        synonyms=["likes", "like", "prefers", "wish", "wishes", "prefera", "preferinta", "dorinta", "aime", "prefere",
                                  "souhait", "mag", "wunsch", "bevorzugt"]),
    "dislike": _kind(L("Dislike", "N'aime pas", "Nu-i place", "Mag nicht"), "dislikes",
                     synonyms=["dislikes", "hates", "doesnt like", "nu ii place", "uraste", "n aime pas", "deteste", "mag nicht", "hasst"]),
    "must": _kind(L("Must have", "Indispensable", "Obligatoriu", "Muss"), "must",
                  synonyms=["must have", "requirement", "needs", "need", "obligatoriu", "neaparat", "indispensable", "obligatoire",
                            "exigence", "muss", "pflicht", "unbedingt"]),
    "must_not": _kind(L("Deal-breaker", "Rédhibitoire", "Exclus", "Ausschlusskriterium"), "must_not",
                      synonyms=["dealbreaker", "deal breaker", "never", "no go", "exclus", "niciodata", "in niciun caz", "jamais",
                                "redhibitoire", "niemals", "ausgeschlossen", "auf keinen fall"]),
    "goal": _kind(L("Goal", "Objectif", "Obiectiv", "Ziel"), synonyms=["objective", "target", "obiectiv", "objectif", "ziel"]),
    "constraint": _kind(L("Constraint", "Contrainte", "Constrângere", "Einschränkung"),
                        synonyms=["limit", "limitation", "schedule", "availability", "constrangere", "disponibilitate", "contrainte",
                                  "disponibilite", "einschrankung", "verfugbarkeit"]),
    "health": _kind(L("Health", "Santé", "Sănătate", "Gesundheit"), sensitivity="special",
                    synonyms=["medical", "injury", "condition", "pain", "sanatate", "accidentare", "durere", "sante", "blessure",
                              "douleur", "gesundheit", "verletzung", "schmerz"]),
    "allergy": _kind(L("Allergy", "Allergie", "Alergie", "Allergie"), "must_not", "special",
                     synonyms=["allergic", "intolerance", "alergic", "intoleranta", "allergique", "allergisch", "unvertraglichkeit"]),
    "medication": _kind(L("Medication", "Traitement", "Medicație", "Medikament"), sensitivity="special",
                        synonyms=["medicine", "pills", "medicament", "medicamente", "tratament", "medikamente"]),
    "motivation": _kind(L("Motivation", "Motivation", "Motivație", "Motivation"),
                        synonyms=["why", "reason", "motiv", "motivatie", "motif", "raison", "grund"]),
    "communication_style": _kind(L("Communication style", "Style de communication", "Stil de comunicare", "Kommunikationsstil"),
                                 synonyms=["style", "tone", "channel", "contact preference", "stil", "ton", "canal", "kanal"]),
    "family": _kind(L("Family", "Famille", "Familie", "Familie"), sensitivity="private",
                    synonyms=["children", "kids", "copii", "enfants", "kinder"]),
    "finances": _kind(L("Finances", "Finances", "Finanțe", "Finanzen"), sensitivity="private",
                      synonyms=["financing", "income", "money situation", "bani", "finantare", "argent", "financement", "geld",
                                "finanzierung"]),
    "life_event": _kind(L("Life event", "Événement de vie", "Eveniment de viață", "Lebensereignis"), sensitivity="private",
                        synonyms=["birthday", "exam", "exams", "new job", "wedding", "baby", "moving", "zi de nastere", "examen",
                                  "nunta", "anniversaire", "mariage", "demenagement", "geburtstag", "prufung", "hochzeit", "umzug"]),
    "document": _kind(L("Document", "Document", "Document", "Dokument"), sensitivity="special",
                      synonyms=["passport", "id card", "pasaport", "buletin", "passeport", "carte d identite", "reisepass", "ausweis"]),
    "consent": _kind(L("Consent", "Consentement", "Consimțământ", "Einwilligung"),
                     synonyms=["agreed", "gdpr", "rgpd", "dsgvo", "acord", "consimtamant", "accord", "einverstanden"]),
}


def _rel(fwd, inv, symmetric=False, synonyms=(), inverse_synonyms=()):
    return {"label": {lg: [fwd[lg], inv[lg]] for lg in fwd}, "symmetric": symmetric, "synonyms": list(synonyms),
            "inverse_synonyms": list(inverse_synonyms)}


BASE_RELATION_TYPES: dict[str, dict] = {
    "spouse_of": _rel(L("spouse of", "conjoint de", "soț/soție cu", "Ehepartner von"), L("spouse of", "conjoint de", "soț/soție cu", "Ehepartner von"),
                      True, ["spouse", "husband", "wife", "partner", "married to", "sot", "sotie", "partener", "mari", "femme", "epoux",
                             "epouse", "conjoint", "ehemann", "ehefrau", "partnerin", "ehepartner"]),
    "family_of": _rel(L("family of", "famille de", "rudă cu", "verwandt mit"), L("family of", "famille de", "rudă cu", "verwandt mit"),
                      True, ["family", "relative", "sibling", "brother", "sister", "ruda", "frate", "sora", "famille", "frere", "soeur",
                             "verwandt", "bruder", "schwester"]),
    "parent_of": _rel(L("parent of", "parent de", "părinte al lui", "Elternteil von"), L("child of", "enfant de", "copilul lui", "Kind von"),
                      False, ["parent", "mother", "father", "mom", "dad", "mama", "tata", "mere", "pere", "mutter", "vater"],
                      ["child", "son", "daughter", "kid", "copil", "fiu", "fiica", "enfant", "fils", "fille", "kind", "sohn", "tochter"]),
    "referred_by": _rel(L("referred by", "recommandé par", "recomandat de", "empfohlen von"), L("referred", "a recommandé", "a recomandat pe", "hat empfohlen"),
                        False, ["recommended by", "sent by", "recomandat de", "trimis de", "recommande par", "envoye par", "empfohlen von"],
                        ["recommended", "referred", "a recomandat", "a recommande", "hat empfohlen"]),
    "decides_for": _rel(L("decides for", "décide pour", "decide pentru", "entscheidet für"), L("decisions by", "décisions par", "decizia o ia", "Entscheidung durch"),
                        False, ["decides", "decision maker", "decide", "decideur", "entscheidet", "entscheider"]),
    "friend_of": _rel(L("friend of", "ami de", "prieten cu", "befreundet mit"), L("friend of", "ami de", "prieten cu", "befreundet mit"),
                      True, ["friend", "prieten", "prietena", "ami", "amie", "freund", "freundin"]),
    "works_with": _rel(L("works with", "travaille avec", "lucrează cu", "arbeitet mit"), L("works with", "travaille avec", "lucrează cu", "arbeitet mit"),
                       True, ["colleague", "coworker", "coleg", "colega", "collegue", "kollege", "kollegin"]),
}

VOCAB_TYPES = {"fact_kind": BASE_FACT_KINDS, "relation_type": BASE_RELATION_TYPES}


# --- Vocabulary -----------------------------------------------------------------------------

def ensure_vocab(conn: sqlite3.Connection, pack: Optional[dict] = None) -> None:
    """Seed the base vocabulary plus the pack's own kinds/types (never overwrites what exists)."""
    pack = pack or {}
    for vtype, base in VOCAB_TYPES.items():
        own = pack.get("fact_kinds" if vtype == "fact_kind" else "relation_types") or {}
        for name, spec in {**base, **own}.items():
            conn.execute("INSERT OR IGNORE INTO vocab(type, name, spec, origin) VALUES (?,?,?,?)",
                         (vtype, name, json.dumps(spec, ensure_ascii=False), "pack"))


def _vocab(conn: sqlite3.Connection, vtype: str) -> dict[str, dict]:
    rows = conn.execute("SELECT name, spec FROM vocab WHERE type=? ORDER BY rowid", (vtype,)).fetchall()
    if not rows:                                   # databases created before the people layer
        from .db import get_setting
        from .verticals import pack
        ensure_vocab(conn, pack(get_setting(conn, "vertical", "generic")))
        rows = conn.execute("SELECT name, spec FROM vocab WHERE type=? ORDER BY rowid", (vtype,)).fetchall()
    return {r["name"]: json.loads(r["spec"]) for r in rows}


def fact_kinds(conn: sqlite3.Connection) -> dict[str, dict]:
    return _vocab(conn, "fact_kind")


def relation_types(conn: sqlite3.Connection) -> dict[str, dict]:
    return _vocab(conn, "relation_type")


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", fold(s)).strip()


def match_kind(conn: sqlite3.Connection, raw: Optional[str]) -> Optional[str]:
    if not raw:
        return "note"
    kinds = fact_kinds(conn)
    k = _norm(raw)
    for name, spec in kinds.items():
        if k == _norm(name) or k in {_norm(s) for s in spec.get("synonyms", [])} or k in {_norm(v) for v in spec["label"].values()}:
            return name
    return None


def match_relation(conn: sqlite3.Connection, raw: Optional[str]) -> tuple[Optional[str], bool]:
    """(relation type, swapped): swapped=True when the words name the inverse ('son' for parent_of)."""
    k = _norm(raw)
    for name, spec in relation_types(conn).items():
        if k == _norm(name) or k in {_norm(s) for s in spec.get("synonyms", [])} or k in {_norm(v[0]) for v in spec["label"].values()}:
            return name, False
        if not spec.get("symmetric") and (k in {_norm(s) for s in spec.get("inverse_synonyms", [])}
                                          or k in {_norm(v[1]) for v in spec["label"].values()}):
            return name, True
    return None, False


def validate_vocab(vtype: str, name: str, spec: dict) -> dict:
    if not isinstance(name, str) or not schema._NAME.match(name):
        raise schema.SchemaError(f"invalid name {name!r}: lowercase letters, digits, underscore")
    spec = dict(spec or {})
    label = spec.get("label") or {}
    if vtype == "fact_kind":
        if spec.get("polarity", "neutral") not in POLARITIES:
            raise schema.SchemaError(f"polarity must be one of {POLARITIES}")
        if spec.get("sensitivity", "normal") not in SENSITIVITY:
            raise schema.SchemaError(f"sensitivity must be one of {SENSITIVITY}")
        out = _kind({lg: (label.get(lg) or label.get("en") or name.replace("_", " ").capitalize()) for lg in schema.LANGS},
                    spec.get("polarity", "neutral"), spec.get("sensitivity", "normal"), spec.get("synonyms") or [])
    else:
        def pair(v):
            return v if isinstance(v, list) and len(v) == 2 else [v, v]
        en = label.get("en") or name.replace("_", " ")
        out = {"label": {lg: pair(label.get(lg) or en) for lg in schema.LANGS}, "symmetric": bool(spec.get("symmetric")),
               "synonyms": spec.get("synonyms") or [], "inverse_synonyms": spec.get("inverse_synonyms") or []}
    if not all(isinstance(s, str) for s in out["synonyms"]):
        raise schema.SchemaError("synonyms must be strings")
    return out


def add_vocab(conn: sqlite3.Connection, vtype: str, name: str, spec: dict, origin: str = "assistant") -> int:
    _vocab(conn, vtype)
    if conn.execute("SELECT 1 FROM vocab WHERE type=? AND name=?", (vtype, name)).fetchone():
        raise schema.SchemaError(f"{vtype.replace('_', ' ')} {name!r} already exists")
    spec = validate_vocab(vtype, name, spec)
    conn.execute("INSERT INTO vocab(type, name, spec, origin) VALUES (?,?,?,?)", (vtype, name, json.dumps(spec, ensure_ascii=False), origin))
    return schema._log(conn, f"add_{vtype}", {"name": name}, origin)


def undo_vocab(conn: sqlite3.Connection, vtype: str, name: str) -> str:
    used = conn.execute("SELECT COUNT(*) FROM facts WHERE kind=?" if vtype == "fact_kind" else "SELECT COUNT(*) FROM relations WHERE type=?",
                        (name,)).fetchone()[0]
    if used:
        raise schema.SchemaError(f"{name} is used by {used} saved items; remove those first")
    conn.execute("DELETE FROM vocab WHERE type=? AND name=?", (vtype, name))
    return f"removed {vtype.replace('_', ' ')} {name}"


def labels(spec: dict, idx: Optional[int] = None) -> dict:
    return {lg: (v[idx] if idx is not None else v) for lg, v in spec["label"].items()}


# --- Fact queries ------------------------------------------------------------------------------

def active_clause(today: dt.date, alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    return f"{a}superseded_by IS NULL AND ({a}valid_until IS NULL OR {a}valid_until >= '{today.isoformat()}')"


def facts_for(conn: sqlite3.Connection, entity: str, rid: int, today: dt.date) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM facts WHERE entity=? AND record_id=? AND {active_clause(today)} "
                                          "ORDER BY observed_at DESC, id DESC", (entity, rid))]


def fact_where(conn: sqlite3.Connection, entity: str, flt: dict, today: dt.date) -> tuple[str, list]:
    """`id IN (...)` clause for find_records / browse_records: people with a matching current fact."""
    if not isinstance(flt, dict):
        raise ValueError("fact filter: {kind?, key?, text?, polarity?}")
    clauses, params = [f"entity = ?", active_clause(today)], [entity]
    if flt.get("kind"):
        k = match_kind(conn, flt["kind"])
        if k is None:
            raise ValueError(f"unknown fact kind {flt['kind']!r}; kinds: {', '.join(fact_kinds(conn))}")
        clauses.append("kind = ?")
        params.append(k)
    if flt.get("key"):
        clauses.append("key = ?")
        params.append(key_of(flt["key"]))
    if flt.get("polarity"):
        clauses.append("polarity = ?")
        params.append(POLARITY_ALIASES.get(flt["polarity"], flt["polarity"]))
    if flt.get("text"):
        clauses.append("(lower(value) LIKE lower(?) OR lower(COALESCE(key,'')) LIKE lower(?))")
        params += [f"%{flt['text']}%"] * 2
    return f"id IN (SELECT record_id FROM facts WHERE {' AND '.join(clauses)})", params


def key_of(raw) -> Optional[str]:
    k = re.sub(r"[^a-z0-9]+", "_", fold(raw)).strip("_")[:40]
    return k or None


# --- Redaction for forget_person ------------------------------------------------------------------

def _folded_index(text: str) -> tuple[str, list[int]]:
    """Folded text and, for every folded char, the index of the original char it came from."""
    out, idx = [], []
    for i, ch in enumerate(text):
        f = fold(ch) if not ch.isspace() else " "
        out.append(f)
        idx += [i] * len(f)
    return "".join(out), idx


def redact(text: Optional[str], names: list[str], phone: Optional[str], country: str, mark: str = "[forgotten]") -> tuple[Optional[str], int]:
    if not text:
        return text, 0
    hits = 0
    for needle in sorted({fold(n) for n in names if n and len(fold(n)) >= 3}, key=len, reverse=True):
        folded, idx = _folded_index(text)
        spans = []
        start = 0
        while (pos := folded.find(needle, start)) != -1:
            end = pos + len(needle)
            before = folded[pos - 1] if pos else " "
            after = folded[end] if end < len(folded) else " "
            if not before.isalnum() and not after.isalnum():
                spans.append((idx[pos], idx[end - 1] + 1))
            start = end
        for a, b in reversed(spans):
            text = text[:a] + mark + text[b:]
        hits += len(spans)
    if phone:
        def sub(m):
            nonlocal hits
            if phone_key(m.group(0), country) == phone:
                hits += 1
                return mark
            return m.group(0)
        text = re.sub(r"\+?\d[\d\s.\-/()]{5,}\d", sub, text)
    return text, hits


# --- Mixin ------------------------------------------------------------------------------------------

class PeopleOps:
    """Mixin for ContextEngine: person facts and relations (capture), briefs, forget/export."""

    # ------------------------------------------------------------------- capture
    def _person(self, it: dict, create: bool = True):
        return self._party(it, create=create)

    def _cap_person_fact(self, it: dict) -> dict:
        value = norm_text(it.get("text") or it.get("value"))
        if not value:
            return {"status": "clarify", **T("q.text_empty")}
        kind = match_kind(self.conn, it.get("fact_kind"))
        kinds = fact_kinds(self.conn)
        if kind is None:
            return {"status": "clarify", **T("q.unknown_fact_kind", kind=str(it.get("fact_kind")), list=", ".join(kinds))}
        pid, note = self._person(it, create=True)
        if pid is None:
            return {"status": "clarify", **note}
        spec = kinds[kind]
        pol = POLARITY_ALIASES.get(it.get("polarity"), it.get("polarity")) or spec.get("polarity", "neutral")
        if pol not in POLARITIES:
            return {"status": "clarify", **T("q.bad_value", field="polarity", list=", ".join(POLARITIES))}
        base = spec.get("sensitivity", "normal")
        sens = it.get("sensitivity") if it.get("sensitivity") in SENSITIVITY else base
        if SENSITIVITY.index(sens) < SENSITIVITY.index(base):        # never lower than the kind's default
            sens = base
        conf = it.get("confidence") if it.get("confidence") in CONFIDENCE else ("stated" if self._source(it) == "history" else "confirmed")
        party = schema.primary(self.conn, "party")["name"]
        fact = {"entity": party, "record_id": pid, "kind": kind, "key": key_of(it.get("key")) if it.get("key") else None,
                "value": value[:1000], "polarity": pol, "sensitivity": sens, "confidence": conf, "source": it.get("source_id") or self._source(it),
                "quote": self._quote(it), "observed_at": self._date(it.get("date")),
                "valid_until": self._date(it.get("valid_until"), default_today=False) if it.get("valid_until") else None}
        name = self._party_name(pid)
        kl = {"$e": kind, **labels(spec)}
        chips = [{"v": name, "icon": "person"}, {"v": kl, "icon": "tag"}, {"v": value, "icon": "note"}]
        same = self._same_fact(fact)
        if same is not None:
            if same["value"] == "__known__":
                return {"status": "known", **T("cap.person_fact", name=name, kind=kl), "chips": chips}
            qid = self._fact_conflict(fact, same, name, kl)
            return {"status": "clarify", **T("q.fact_conflict", name=name, kind=kl, old=same["value"], date=D(same["observed_at"]), new=value),
                    "question": qid}
        fid = self._insert_fact(fact)
        out = {"status": "saved", **T("cap.person_fact", name=name, kind=kl), "chips": chips, "undo": f"fact:{fid}"}
        if note == "new":
            out["note"] = T("cap.new_person")
        if sens == "special" and self._consent_missing(party, pid):
            text = f"Ask {name} for consent to keep {kl['en'].lower()} information"
            if not self.conn.execute("SELECT 1 FROM tasks WHERE status='open' AND text=?", (text,)).fetchone():
                self.conn.execute("INSERT INTO tasks(text, party_id, quote) VALUES (?,?,?)", (text, pid, "consent"))
            out["note"] = T("cap.consent_missing", name=name)
        return out

    def _same_fact(self, fact: dict) -> Optional[dict]:
        """An active fact that this one repeats ('__known__') or contradicts (returned), else None."""
        rows = self.conn.execute(f"SELECT * FROM facts WHERE entity=? AND record_id=? AND kind=? AND {active_clause(self.today)}",
                                 (fact["entity"], fact["record_id"], fact["kind"])).fetchall()
        for r in rows:
            same_val = fold(r["value"]) == fold(fact["value"])
            if fact["key"] and r["key"] == fact["key"]:
                if same_val and r["polarity"] == fact["polarity"]:
                    return {"value": "__known__"}
                return dict(r)
            if not fact["key"] and same_val:
                return {"value": "__known__"} if r["polarity"] == fact["polarity"] else dict(r)
        return None

    def _insert_fact(self, fact: dict) -> int:
        cols = [k for k in fact if k != "supersedes"]
        return self.conn.execute(f"INSERT INTO facts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                 [fact[k] for k in cols]).lastrowid

    def _fact_conflict(self, fact: dict, old: dict, name: str, kl: dict) -> int:
        payload = {"old_id": old["id"], "fact": fact, "name": name}
        for q in self.conn.execute("SELECT id, payload FROM questions WHERE kind='fact_conflict' AND status='open'"):
            p = json.loads(q["payload"])
            if p["old_id"] == old["id"] and fold(p["fact"]["value"]) == fold(fact["value"]):
                return q["id"]
        text = (f"{name} · {kl['en']}{' (' + fact['key'] + ')' if fact['key'] else ''}: on {old['observed_at']} it was “{old['value']}”, "
                f"now “{fact['value']}”. Replace the old one?")
        return self.conn.execute("INSERT INTO questions(kind, text, payload) VALUES ('fact_conflict', ?, ?)",
                                 (text, json.dumps(payload, ensure_ascii=False))).lastrowid

    def _resolve_fact_conflict(self, payload: dict, confirm: bool) -> None:
        if not confirm:
            return
        old = self.conn.execute("SELECT id, superseded_by FROM facts WHERE id=?", (payload["old_id"],)).fetchone()
        new_id = self._insert_fact(payload["fact"])
        if old and old["superseded_by"] is None:
            self.conn.execute("UPDATE facts SET superseded_by=? WHERE id=?", (new_id, old["id"]))

    def _consent_missing(self, entity: str, pid: int) -> bool:
        from .db import get_setting
        if get_setting(self.conn, "consent_required", "0") != "1":
            return False
        return self.conn.execute(f"SELECT 1 FROM facts WHERE entity=? AND record_id=? AND kind='consent' AND polarity!='must_not' AND "
                                 f"{active_clause(self.today)}", (entity, pid)).fetchone() is None

    def _cap_relation(self, it: dict) -> dict:
        if not it.get("other") or not (it.get("customer") or it.get("phone")):
            return {"status": "clarify", **T("q.missing", fields="customer, other, relation")}
        rtype, swapped = match_relation(self.conn, it.get("relation"))
        types = relation_types(self.conn)
        if rtype is None:
            return {"status": "clarify", **T("q.unknown_relation", rel=str(it.get("relation")), list=", ".join(types))}
        a, note = self._person(it, create=True)
        if a is None:
            return {"status": "clarify", **note}
        b, note_b = self._person({"customer": it["other"]}, create=True)
        if b is None:
            return {"status": "clarify", **note_b}
        if a == b:
            return {"status": "clarify", **T("q.same_person")}
        if swapped:
            a, b = b, a
        party = schema.primary(self.conn, "party")["name"]
        spec = types[rtype]
        lab = {"$e": rtype, **labels(spec, 0)}
        chips = [{"v": self._party_name(a), "icon": "person"}, {"v": lab, "icon": "tag"}, {"v": self._party_name(b), "icon": "person"}]
        pairs = [(a, b), (b, a)] if spec.get("symmetric") else [(a, b)]
        for x, y in pairs:
            if self.conn.execute("SELECT 1 FROM relations WHERE type=? AND a_entity=? AND a_id=? AND b_entity=? AND b_id=? AND until IS NULL",
                                 (rtype, party, x, party, y)).fetchone():
                return {"status": "known", **T("cap.relation"), "chips": chips}
        rid = self.conn.execute("INSERT INTO relations(a_entity, a_id, b_entity, b_id, type, since, note, source, quote) VALUES (?,?,?,?,?,?,?,?,?)",
                                (party, a, party, b, rtype, self._date(it.get("date"), default_today=False) if it.get("date") else None,
                                 norm_text(it.get("text")) or None, it.get("source_id") or self._source(it), self._quote(it))).lastrowid
        out = {"status": "saved", **T("cap.relation"), "chips": chips, "undo": f"rel:{rid}"}
        if "new" in (note, note_b):
            out["note"] = T("cap.new_person")
        return out

    # ---------------------------------------------------------------- undo
    def _undo_fact(self, fid: int) -> int:
        r = self.conn.execute("SELECT id FROM facts WHERE id=?", (fid,)).fetchone()
        if not r:
            return 0
        self.conn.execute("UPDATE facts SET superseded_by=NULL WHERE superseded_by=?", (fid,))   # the previous value is current again
        return self.conn.execute("DELETE FROM facts WHERE id=?", (fid,)).rowcount

    # ---------------------------------------------------------------- brief
    def _people_blocks(self, pid: int, show_sensitive: bool) -> list[dict]:
        party = schema.primary(self.conn, "party")["name"]
        kinds = fact_kinds(self.conn)
        rows = facts_for(self.conn, party, pid, self.today)
        items, hidden = [], defaultdict(int)
        for r in rows:
            spec = kinds.get(r["kind"], {"label": {lg: r["kind"] for lg in schema.LANGS}})
            if r["sensitivity"] == "special" and not show_sensitive:
                hidden[r["kind"]] += 1
                continue
            prev = self.conn.execute("SELECT value, observed_at FROM facts WHERE superseded_by=?", (r["id"],)).fetchone()
            items.append({"id": r["id"], "kind": r["kind"], "label": labels(spec), "key": r["key"], "value": r["value"],
                          "polarity": r["polarity"], "sensitivity": r["sensitivity"], "confidence": r["confidence"],
                          "date": r["observed_at"], "source": r["source"], "quote": r["quote"],
                          "previous": {"value": prev["value"], "date": prev["observed_at"]} if prev else None})
        order = {"must_not": 0, "must": 1, "dislikes": 2, "likes": 3, "neutral": 4}
        items.sort(key=lambda i: (order.get(i["polarity"], 5), i["kind"]))
        masked = [{"kind": k, "label": labels(kinds.get(k, {"label": {lg: k for lg in schema.LANGS}})), "n": n} for k, n in hidden.items()]
        present = {r["kind"] for r in rows}
        missing = [{"kind": k, "label": labels(kinds[k])} for k in self._key_facts() if k in kinds and k not in present]
        blocks = []
        if items or masked or missing:
            blocks.append(block("facts", title=T("sec.facts"), items=items, masked=masked, missing=missing))
        rel = self._relations_for(party, pid)
        if rel:
            blocks.append(block("relations", title=T("sec.people_around"), items=rel, person=self._party_name(pid)))
        return blocks

    def _key_facts(self) -> list[str]:
        from .verticals import GENERIC, PACKS
        own = json.loads(self.setting("key_facts", "null") or "null")
        return own if own is not None else PACKS.get(self.setting("vertical"), GENERIC).get("key_facts", [])

    def _relations_for(self, entity: str, pid: int) -> list[dict]:
        types = relation_types(self.conn)
        out = []
        q = ("SELECT * FROM relations WHERE until IS NULL AND ((a_entity=? AND a_id=?) OR (b_entity=? AND b_id=?)) ORDER BY id")
        for r in self.conn.execute(q, (entity, pid, entity, pid)):
            mine_a = r["a_entity"] == entity and r["a_id"] == pid
            other = r["b_id"] if mine_a else r["a_id"]
            spec = types.get(r["type"], {"label": {lg: [r["type"], r["type"]] for lg in schema.LANGS}})
            # Label from the OTHER person's side: "Reza — spouse of", "Ana — parent of" (Ana is this person's parent).
            label = labels(spec, 1 if mine_a else 0)
            ofacts = [f for f in facts_for(self.conn, entity, other, self.today) if f["sensitivity"] != "special"
                      and f["polarity"] in ("must", "must_not", "dislikes", "likes")][:3]
            out.append({"id": r["id"], "person_id": other, "name": self._party_name(other), "type": r["type"], "label": label,
                        "note": r["note"], "facts": [{"polarity": f["polarity"], "value": f["value"]} for f in ofacts]})
        return out

    # --------------------------------------------------------------- forget / export
    def _person_scope(self, pid: int) -> dict[str, Any]:
        """Everything held about one person: rows to delete, refs to clear, texts to redact."""
        c = self.conn
        spec = schema.primary(c, "party")
        ent, tbl = spec["name"], schema.table(spec["name"])
        rec = dict(c.execute(f"SELECT * FROM {tbl} WHERE id=?", (pid,)).fetchone())
        name = self._party_name(pid)
        pf = schema.role_field(spec, "phone")
        phone = rec.get(pf["name"]) if pf else None
        delete = defaultdict(list)
        clear = defaultdict(list)                      # table -> [(row, column)]
        delete[tbl].append(rec)
        delete["facts"] += [dict(r) for r in c.execute("SELECT * FROM facts WHERE entity=? AND record_id=?", (ent, pid))]
        delete["relations"] += [dict(r) for r in c.execute("SELECT * FROM relations WHERE (a_entity=? AND a_id=?) OR (b_entity=? AND b_id=?)",
                                                           (ent, pid, ent, pid))]
        delete["contact_log"] += [dict(r) for r in c.execute("SELECT * FROM contact_log WHERE party_id=?", (pid,))]
        delete["tasks"] += [dict(r) for r in c.execute("SELECT * FROM tasks WHERE party_id=?", (pid,))]
        for other, s in schema.entities(c).items():
            for f in s["fields"]:
                if f["type"] == "ref" and f.get("ref") == ent and other != ent:
                    rows = [dict(r) for r in c.execute(f"SELECT * FROM {schema.table(other)} WHERE {schema.column(f)}=?", (pid,))]
                    if s["role"] == "custom":           # their own things (e.g. their pet): removed with them
                        delete[schema.table(other)] += rows
                    else:                              # sales/visits stay for the accounts, without the person
                        clear[schema.table(other)] += [(r, schema.column(f)) for r in rows]
        names = [name] + [" ".join(reversed(name.split()))] if len(name.split()) > 1 else [name]
        country = self.setting("country", "RO")
        redact_rows = defaultdict(list)                # table -> [(row, column, new_text)]
        for t_, cols in (("knowledge", ("text", "quote")), ("history_queue", ("text",)), ("raw_files", ("content_text",)),
                         ("questions", ("text", "payload")), ("tasks", ("text", "quote")), ("decisions", ("decision", "reason", "quote"))):
            for r in c.execute(f"SELECT * FROM {t_}"):
                r = dict(r)
                if t_ == "tasks" and r["party_id"] == pid:
                    continue
                changed = {}
                for col_ in cols:
                    new, hits = redact(r[col_], names, phone, country)
                    if hits:
                        changed[col_] = new
                if changed:
                    redact_rows[t_].append((r, changed))
        aliases = [dict(r) for r in c.execute("SELECT * FROM aliases WHERE entity=? AND record_id=?", (ent, pid))]
        return {"entity": ent, "name": name, "delete": delete, "clear": clear, "redact": redact_rows, "aliases": aliases}

    def forget_person(self, person: str, confirm: bool = False) -> dict:
        spec = schema.primary(self.conn, "party")
        pid = self._find_record(spec, person)
        sc = self._person_scope(pid)
        counts = {"facts": len(sc["delete"]["facts"]), "relations": len(sc["delete"]["relations"]),
                  "records": sum(len(v) for k, v in sc["delete"].items() if k.startswith("e_")) - 1,
                  "unlinked": sum(len(v) for v in sc["clear"].values()), "texts": sum(len(v) for v in sc["redact"].values()),
                  "tasks": len(sc["delete"]["tasks"]) + len(sc["delete"]["contact_log"])}
        items = [{"t": f"kv.forget_{k}", "v": v, "fmt": "number"} for k, v in counts.items()]
        if not confirm:
            return view("edit_records", T("view.edit_preview"),
                        [block("alerts", items=[{**T("people.preview_forget", name=sc["name"], days=N(FORGET_UNDO_DAYS)), "level": "warn"}]),
                         block("kv", items=items)],
                        hints=["NOTHING CHANGED YET. Show the owner what will be erased; if they agree, call edit_records again with "
                               "action='forget_person', the same person and confirm=true."])
        snap: dict[str, list[dict]] = defaultdict(list)
        for t_, rows in sc["delete"].items():
            for r in rows:
                snap[t_].append(r)
                self.conn.execute(f"DELETE FROM {t_} WHERE id=?", (r["id"],))
        for t_, pairs in sc["clear"].items():
            for r, col_ in pairs:
                snap[t_].append(r)
                self.conn.execute(f"UPDATE {t_} SET {col_}=NULL WHERE id=?", (r["id"],))
        for t_, pairs in sc["redact"].items():
            for r, changed in pairs:
                snap[t_].append(r)
                self.conn.execute(f"UPDATE {t_} SET {', '.join(f'{k}=?' for k in changed)} WHERE id=?", [*changed.values(), r["id"]])
        self.conn.execute("DELETE FROM aliases WHERE entity=? AND record_id=?", (sc["entity"], pid))
        purge = (self.today + dt.timedelta(days=FORGET_UNDO_DAYS)).isoformat()
        change = schema._log(self.conn, "edit_records", {"entity": sc["entity"], "action": "forget_person", "rows": 1, "purge_after": purge}, "assistant")
        schema.snapshot(self.conn, change, dict(snap), inserted={}, aliases=sc["aliases"])
        self.conn.commit()
        return view("edit_records", T("view.edit"),
                    [block("feed", items=[{"kind": "record", "status": "saved", **T("people.forgotten", date=D(purge)), "undo": str(change)}]),
                     block("kv", items=items)],
                    hints=[f"Erased. undo_change('{change}') restores everything until {purge}; after that it is gone for good. "
                           "Do not repeat the person's name in your reply."])

    def purge_forgotten(self) -> int:
        """Delete snapshots of forgotten people whose undo window has passed."""
        n = 0
        for r in self.conn.execute("SELECT id FROM schema_changes WHERE op='edit_records' AND status='applied' AND "
                                   "json_extract(payload,'$.action')='forget_person' AND json_extract(payload,'$.purge_after') < ?",
                                   (self.today.isoformat(),)).fetchall():
            self.conn.execute("DELETE FROM trash WHERE change_id=?", (r["id"],))
            self.conn.execute("UPDATE schema_changes SET status='purged' WHERE id=?", (r["id"],))
            n += 1
        return n

    def export_person(self, person: str, include_sensitive: bool = True) -> dict:
        spec = schema.primary(self.conn, "party")
        pid = self._find_record(spec, person)
        sc = self._person_scope(pid)
        kinds = fact_kinds(self.conn)

        def clean(r: dict) -> dict:
            return {k: v for k, v in r.items() if v not in (None, "", "{}") and k not in ("_file_id", "_source_row")}
        rec = clean(sc["delete"][schema.table(spec["name"])][0])
        facts = []
        for f in sc["delete"]["facts"]:
            masked = f["sensitivity"] == "special" and not include_sensitive
            facts.append(clean({"kind": kinds.get(f["kind"], {"label": {"en": f["kind"]}})["label"]["en"], "key": f["key"],
                                "value": MASK if masked else f["value"], "polarity": f["polarity"], "sensitivity": f["sensitivity"],
                                "said_on": f["observed_at"], "valid_until": f["valid_until"], "current": f["superseded_by"] is None,
                                "source": f["source"], "words": None if masked else f["quote"]}))
        rels = [{"type": r["type"], "with": self._party_name(r["b_id"] if r["a_id"] == pid else r["a_id"]), "since": r["since"]}
                for r in sc["delete"]["relations"]]
        history = [{"date": r["date"], "what": r["offering"], "amount_minor": r["amount"], "status": r["status"]}
                   for r in self.conn.execute("SELECT date, offering, amount, status FROM v_transactions WHERE party_id=? ORDER BY date", (pid,))]
        related = {t_[2:]: [clean(r) for r in rows] for t_, rows in sc["delete"].items() if t_.startswith("e_") and t_ != schema.table(spec["name"])}
        data = {"exported_on": self.today.isoformat(), "business": self.setting("name"), "person": rec, "spellings": [a["key"][2:] for a in sc["aliases"]],
                "facts": facts, "relations": rels, "history": history, "contacts": [clean(r) for r in sc["delete"]["contact_log"]],
                "tasks": [clean(r) for r in sc["delete"]["tasks"]], "related_records": related,
                "mentions_in_notes": sum(len(v) for v in sc["redact"].values())}
        text = json.dumps(data, ensure_ascii=False, indent=1, default=str)
        return view("edit_records", T("view.export_person", name=sc["name"]),
                    [block("kv", items=[{"t": "kv.forget_facts", "v": len(facts), "fmt": "number"},
                                        {"t": "kv.forget_relations", "v": len(rels), "fmt": "number"},
                                        {"t": "kv.history", "v": len(history), "fmt": "number"}]),
                     block("copy", title=T("people.export_title"), text=text, steps=[])],
                    hints=["This is everything held about the person (subject access request). Offer to turn it into a clean PDF or "
                           "Artifact for them; sensitive facts are included unless include_sensitive=false."])

    # ------------------------------------------------------------------ context
    def _people_context(self) -> list[str]:
        c = self.conn
        lines = []
        n = c.execute(f"SELECT COUNT(*) n, COUNT(DISTINCT record_id) p FROM facts WHERE {active_clause(self.today)}").fetchone()
        if n["n"]:
            lines.append(f"People knowledge: {n['n']} facts about {n['p']} people; use get_person_brief before meeting or writing to someone.")
        nq = c.execute("SELECT COUNT(*) FROM questions WHERE kind='fact_conflict' AND status='open'").fetchone()[0]
        if nq:
            lines.append(f"{nq} fact(s) changed and wait for the owner's answer (resolve_question confirm = keep the new value).")
        lines.append("Fact kinds: " + ", ".join(f"{k}{'*' if s.get('sensitivity') == 'special' else ''}" for k, s in fact_kinds(c).items())
                     + " (* sensitive). Relation types: " + ", ".join(relation_types(c)) + ".")
        return lines
