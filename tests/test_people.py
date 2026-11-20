"""People layer (P1): facts, contradictions, relations, sensitivity, consent, forget/export, through the MCP server."""
import asyncio
import datetime as dt
import json

import pytest

from intheknow.context import ContextEngine
from intheknow.mcp_server import build_server
from conftest import TODAY, loaded


def call(server, name, args=None):
    r = asyncio.run(server.call_tool(name, args or {}))
    return r.content[0].text, r.structured_content, bool(r.is_error)


def feed(view):
    return next(b for b in view["blocks"] if b["type"] == "feed")["items"]


def blk(view, kind):
    return next((b for b in view["blocks"] if b["type"] == kind), None)


@pytest.fixture
def salon(tmp_path):
    e = loaded(tmp_path, "salon_aurora")
    return e, build_server(e)


def fact(customer, kind, text, **kw):
    return {"type": "fact", "quote": kw.pop("quote", text), "customer": customer, "fact_kind": kind, "text": text, **kw}


def test_person_facts_are_saved_with_source_and_answer_what_they_dislike(salon):
    e, s = salon
    text, view, err = call(s, "capture", {"items": [
        fact("Ioana Popescu", "dislike", "waiting more than 10 minutes", quote="Ioana hates waiting, more than 10 minutes and she leaves"),
        fact("Ioana Popescu", "preference", "Saturday mornings", key="slot"),
        fact("Ioana Popescu", "dealbreaker", "ammonia dyes"),          # synonym of must_not
        {"type": "fact", "quote": "We open at 9", "text": "We open at 9 on Saturdays"},   # no person -> business knowledge
    ]})
    items = feed(view)
    assert not err and [i["status"] for i in items] == ["saved"] * 4
    assert items[0]["undo"].startswith("fact:") and items[3]["undo"].startswith("kn:")
    assert e.conn.execute("SELECT COUNT(*) FROM knowledge WHERE text LIKE 'We open at 9%'").fetchone()[0] == 1
    # the same thing again is known, not stored twice
    _, view, _ = call(s, "capture", {"items": [fact("ioana popescu", "dislike", "Waiting more than 10 minutes")]})
    assert feed(view)[0]["status"] == "known"
    assert e.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 3
    # "what does Ioana dislike?" -> answered with source: date, channel and her exact words
    text, _, _ = call(s, "search_knowledge", {"query": "Ioana dislike"})
    assert "waiting more than 10 minutes" in text and "2026-11-01" in text and "hates waiting" in text
    text, view, _ = call(s, "get_person_brief", {"name_or_phone": "Popescu Ioana"})
    f = blk(view, "facts")
    assert [i["polarity"] for i in f["items"]][:2] == ["must_not", "dislikes"]           # deal-breakers first
    assert "NEVER Deal-breaker: ammonia dyes" in text and "− Dislike: waiting more than 10 minutes" in text


def test_unknown_kind_is_asked_not_guessed(salon):
    _, s = salon
    text, view, _ = call(s, "capture", {"items": [fact("Ioana Popescu", "zodiac sign", "Leo")]})
    assert feed(view)[0]["status"] == "clarify" and "not a known kind of fact" in text
    text, view, err = call(s, "change_schema", {"action": "add_fact_kind", "spec": {"name": "zodiac", "label": {"en": "Zodiac sign", "ro": "Zodie"},
                                                                                  "synonyms": ["zodiac sign", "zodie"]}})
    assert not err
    change = feed(view)[0]["undo"]
    _, view, _ = call(s, "capture", {"items": [fact("Ioana Popescu", "zodie", "Leo")]})
    assert feed(view)[0]["status"] == "saved"
    text, _, err = call(s, "undo_change", {"change_id": change})
    assert err and "used by 1" in text                            # a kind in use is not dropped silently
    call(s, "undo_change", {"change_id": feed(view)[0]["undo"]})
    assert not call(s, "undo_change", {"change_id": change})[2]


def test_contradiction_raises_a_question_and_keeps_history(salon):
    e, s = salon
    call(s, "capture", {"items": [fact("Ioana Popescu", "constraint", "max 300 lei per visit", key="budget", date="2026-03-04")]})
    text, view, _ = call(s, "capture", {"items": [fact("Ioana Popescu", "constraint", "max 400 lei per visit", key="Budget")]})
    item = feed(view)[0]
    assert item["status"] == "clarify" and item["question"]
    assert "it was “max 300 lei per visit”, now “max 400 lei per visit”" in text and "NOT saved yet" in text
    assert e.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1           # nothing overwritten
    # asking again does not create a second question
    _, view2, _ = call(s, "capture", {"items": [fact("Ioana Popescu", "constraint", "max 400 lei per visit", key="budget")]})
    assert feed(view2)[0]["question"] == item["question"]
    call(s, "resolve_question", {"resolution": "confirm", "question_id": item["question"]})
    text, view, _ = call(s, "get_person_brief", {"name_or_phone": "Ioana Popescu"})
    f = blk(view, "facts")["items"][0]
    assert f["value"] == "max 400 lei per visit" and f["previous"]["value"] == "max 300 lei per visit"
    assert "(was “max 300 lei per visit” on 2026-03-04)" in text
    rows = e.conn.execute("SELECT value, superseded_by FROM facts ORDER BY id").fetchall()
    assert rows[0]["superseded_by"] == f["id"] and rows[1]["superseded_by"] is None
    # undoing the new fact makes the old one current again
    call(s, "undo_change", {"change_id": f"fact:{f['id']}"})
    _, view, _ = call(s, "get_person_brief", {"name_or_phone": "Ioana Popescu"})
    assert blk(view, "facts")["items"][0]["value"] == "max 300 lei per visit"
    # a dismissed change keeps the old value
    _, view, _ = call(s, "capture", {"items": [fact("Ioana Popescu", "constraint", "no limit", key="budget")]})
    call(s, "resolve_question", {"resolution": "dismiss", "question_id": feed(view)[0]["question"]})
    assert [r["value"] for r in e.conn.execute("SELECT value FROM facts WHERE superseded_by IS NULL")] == ["max 300 lei per visit"]


def test_opposite_polarity_is_a_change(salon):
    _, s = salon
    call(s, "capture", {"items": [fact("Ioana Popescu", "preference", "dark coffee", polarity="likes")]})
    _, view, _ = call(s, "capture", {"items": [fact("Ioana Popescu", "preference", "dark coffee", polarity="dislikes")]})
    assert feed(view)[0]["status"] == "clarify"


def test_sensitive_facts_are_masked_everywhere(salon):
    e, s = salon
    _, view, _ = call(s, "capture", {"items": [fact("Ioana Popescu", "allergy", "nuts (carries an EpiPen)"),
                                               fact("Ioana Popescu", "health", "pregnant", sensitivity="normal"),   # never lowered
                                               fact("Ioana Popescu", "preference", "tea, no sugar")]})
    assert [i["status"] for i in feed(view)] == ["saved"] * 3
    assert e.conn.execute("SELECT COUNT(*) FROM facts WHERE sensitivity='special'").fetchone()[0] == 2
    text, view, _ = call(s, "get_person_brief", {"name_or_phone": "Ioana Popescu"})
    f = blk(view, "facts")
    assert "EpiPen" not in text + json.dumps(view) and "pregnant" not in text + json.dumps(view)
    assert {m["kind"] for m in f["masked"]} == {"allergy", "health"} and "sensitive fact(s) hidden" in text
    text, view, _ = call(s, "get_person_brief", {"name_or_phone": "Ioana Popescu", "show_sensitive": True})
    assert "EpiPen" in text and not blk(view, "facts")["masked"]
    # query_data: the raw table reads as NULL, the view masks special values, normal values stay readable
    assert "EpiPen" not in call(s, "query_data", {"sql": "SELECT value, quote FROM facts"})[0]
    out = call(s, "query_data", {"sql": "SELECT kind, value FROM v_facts ORDER BY id"})[0]
    assert "[sensitive]" in out and "tea, no sugar" in out and "EpiPen" not in out
    assert "EpiPen" not in call(s, "search_knowledge", {"query": "nuts allergy"})[0]
    # and they are not listed in the quiet context either
    assert "EpiPen" not in call(s, "get_context")[0]


def test_sensitive_entity_field_is_masked_in_sql(salon):
    e, s = salon
    call(s, "change_schema", {"action": "add_field", "entity": "customer", "field": {"name": "id_number", "type": "text", "sensitive": True}})
    call(s, "edit_records", {"entity": "customer", "action": "update", "where": {"search": "Ioana Popescu"}, "set": {"id_number": "2850101123456"}})
    assert e.conn.execute("SELECT COUNT(*) FROM e_customer WHERE id_number IS NOT NULL").fetchone()[0] == 1
    assert "2850101123456" not in call(s, "query_data", {"sql": "SELECT name, id_number FROM e_customer WHERE id_number IS NOT NULL OR 1"})[0]


def test_consent_task_when_the_pack_requires_it(salon):
    e, s = salon
    e.conn.execute("UPDATE settings SET value='1' WHERE key='consent_required'")
    text, view, _ = call(s, "capture", {"items": [fact("Ioana Popescu", "medication", "blood thinners")]})
    assert feed(view)[0]["status"] == "saved" and "consent" in text                       # stored, not refused
    assert e.conn.execute("SELECT COUNT(*) FROM tasks WHERE text LIKE 'Ask Ioana Popescu for consent%'").fetchone()[0] == 1
    call(s, "capture", {"items": [fact("Ioana Popescu", "consent", "agreed to keep health notes", quote="da, puteți nota")]})
    _, view, _ = call(s, "capture", {"items": [fact("Ioana Popescu", "allergy", "latex")]})
    assert feed(view)[0].get("note") is None


def test_relations_both_directions_and_people_around(salon):
    e, s = salon
    text, view, _ = call(s, "capture", {"items": [
        {"type": "relation", "quote": "q", "customer": "Reza Ahmadi", "relation": "husband", "other": "Ioana Popescu"},
        {"type": "relation", "quote": "q", "customer": "Ioana Popescu", "relation": "daughter", "other": "Elena Popescu"},
        {"type": "relation", "quote": "q", "customer": "Ioana Popescu", "relation": "recommended by", "other": "Maria Ionescu"},
        fact("Reza Ahmadi", "dislike", "loud music in the salon"),
    ]})
    assert [i["status"] for i in feed(view)] == ["saved"] * 4
    # 'daughter' names the inverse: Elena is Ioana's parent
    r = e.conn.execute("SELECT a_id, b_id FROM relations WHERE type='parent_of'").fetchone()
    assert e._party_name(r["a_id"]) == "Elena Popescu" and e._party_name(r["b_id"]) == "Ioana Popescu"
    # symmetric relation said the other way round is already known
    _, view, _ = call(s, "capture", {"items": [{"type": "relation", "quote": "q", "customer": "Ioana Popescu", "relation": "wife", "other": "Reza Ahmadi"}]})
    assert feed(view)[0]["status"] == "known"
    text, view, _ = call(s, "get_person_brief", {"name_or_phone": "Ioana Popescu"})
    rel = {i["name"]: i for i in blk(view, "relations")["items"]}
    assert set(rel) == {"Reza Ahmadi", "Elena Popescu", "Maria Ionescu"}
    assert "Maria Ionescu: referred Ioana Popescu" in text
    assert "Reza Ahmadi: spouse of Ioana Popescu" in text and "loud music" in text
    assert "Elena Popescu: parent of Ioana Popescu" in text
    _, view, _ = call(s, "get_person_brief", {"name_or_phone": "Elena Popescu"})
    assert blk(view, "relations")["items"][0]["label"]["en"] == "child of"
    assert "| 3 |" in call(s, "query_data", {"sql": "SELECT COUNT(*) AS n FROM v_relations WHERE 'Ioana Popescu' IN (a_name, b_name)"})[0]
    rid = e.conn.execute("SELECT id FROM relations WHERE type='spouse_of'").fetchone()[0]
    call(s, "undo_change", {"change_id": f"rel:{rid}"})
    assert e.conn.execute("SELECT COUNT(*) FROM relations WHERE type='spouse_of'").fetchone()[0] == 0


def test_find_people_by_fact(salon):
    _, s = salon
    call(s, "capture", {"items": [fact("Ioana Popescu", "must", "parking nearby", key="parking"),
                                  fact("Maria Ionescu", "must", "parking", key="parking"),
                                  fact("Elena Nouă", "preference", "parking would be nice")]})
    text, _, _ = call(s, "find_records", {"entity": "customer", "where": {"fact": {"kind": "must", "key": "parking"}}, "fields": ["name"]})
    assert text.startswith("2 matching") and "Ioana Popescu" in text and "Elena" not in text
    text, _, _ = call(s, "find_records", {"entity": "customer", "where": {"fact": {"text": "parking"}}, "count_by": "name"})
    assert text.startswith("3 matching")
    assert call(s, "find_records", {"entity": "visit", "where": {"fact": {"kind": "must"}}})[2]


def test_merge_moves_facts_and_relations(salon):
    e, s = salon
    call(s, "capture", {"items": [{"type": "customer_info", "quote": "q", "customer": "Ioana P. Dup", "phone": "0700 000 111"},
                                  fact("Ioana P. Dup", "preference", "window seat"),
                                  {"type": "relation", "quote": "q", "customer": "Ioana P. Dup", "relation": "friend", "other": "Ana Prietena"}]})
    _, view, _ = call(s, "edit_records", {"entity": "customer", "action": "merge", "keep": "Ioana Popescu", "duplicate": "Ioana P. Dup", "confirm": True})
    change = feed(view)[0]["undo"]
    keep = e.find_party("Ioana Popescu")
    assert e.conn.execute("SELECT record_id FROM facts").fetchone()[0] == keep
    assert e.conn.execute("SELECT a_id FROM relations").fetchone()[0] == keep
    call(s, "undo_change", {"change_id": change})
    dup = e.find_party("0700 000 111")
    assert dup != keep and e.conn.execute("SELECT record_id FROM facts").fetchone()[0] == dup


def test_forget_person_preview_erase_undo_and_purge(salon):
    e, s = salon
    rev = lambda: e.conn.execute("SELECT SUM(amount) FROM v_transactions WHERE counted=1").fetchone()[0]   # noqa: E731
    before_rev, before_tx = rev(), e.conn.execute("SELECT COUNT(*) FROM v_transactions").fetchone()[0]
    call(s, "capture", {"items": [fact("Ioana Popescu", "allergy", "ammonia"),
                                  {"type": "relation", "quote": "q", "customer": "Reza Ahmadi", "relation": "husband", "other": "Ioana Popescu"},
                                  {"type": "task", "quote": "q", "text": "Call Ioana about her colour", "customer": "Ioana Popescu", "date": "2026-11-05"},
                                  {"type": "fact", "quote": "q", "text": "Ioana Popescu brought 3 friends in October"}]})
    pid = e.find_party("Ioana Popescu")
    text, view, _ = call(s, "edit_records", {"entity": "customer", "action": "forget_person", "person": "Ioana Popescu"})
    assert "NOTHING CHANGED YET" in text and e.find_party("Ioana Popescu") == pid
    text, view, _ = call(s, "edit_records", {"entity": "customer", "action": "forget_person", "person": "Ioana Popescu", "confirm": True})
    change = feed(view)[0]["undo"]
    c = e.conn
    assert e.find_party("Ioana Popescu") is None
    assert c.execute("SELECT COUNT(*) FROM facts WHERE record_id=?", (pid,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM aliases WHERE record_id=? AND entity='customer'", (pid,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM tasks WHERE text LIKE '%Ioana%'").fetchone()[0] == 0
    assert c.execute("SELECT text FROM knowledge WHERE text LIKE '%brought 3 friends%'").fetchone()[0] == "[forgotten] brought 3 friends in October"
    assert not c.execute("SELECT 1 FROM raw_files WHERE content_text LIKE '%Ioana Popescu%' OR content_text LIKE '%POPESCU Ioana%'").fetchone()
    assert "Ioana" not in json.dumps([dict(r) for r in c.execute("SELECT payload FROM schema_changes")])
    assert (rev(), c.execute("SELECT COUNT(*) FROM v_transactions").fetchone()[0]) == (before_rev, before_tx)   # accounts intact
    # undo restores everything exactly
    call(s, "undo_change", {"change_id": change})
    assert e.find_party("Ioana Popescu") == pid
    assert c.execute("SELECT COUNT(*) FROM facts WHERE record_id=?", (pid,)).fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM v_transactions WHERE party_id=?", (pid,)).fetchone()[0] > 0
    assert c.execute("SELECT 1 FROM raw_files WHERE content_text LIKE '%Ioana Popescu%'").fetchone()
    # after the undo window the snapshot is purged and the erasure is final
    _, view, _ = call(s, "edit_records", {"entity": "customer", "action": "forget_person", "person": "Ioana Popescu", "confirm": True})
    change = feed(view)[0]["undo"]
    e._today = TODAY + dt.timedelta(days=8)
    call(s, "get_context")
    assert c.execute("SELECT COUNT(*) FROM trash WHERE change_id=?", (int(change),)).fetchone()[0] == 0
    assert call(s, "undo_change", {"change_id": change})[2]


def test_export_person_has_everything(salon):
    _, s = salon
    call(s, "capture", {"items": [fact("Ioana Popescu", "allergy", "ammonia"), fact("Ioana Popescu", "preference", "Saturday mornings"),
                                  {"type": "relation", "quote": "q", "customer": "Reza Ahmadi", "relation": "husband", "other": "Ioana Popescu"}]})
    text, view, err = call(s, "edit_records", {"entity": "customer", "action": "export_person", "person": "Ioana Popescu"})
    data = json.loads(blk(view, "copy")["text"])
    assert not err and data["person"]["name"] == "Ioana Popescu" and len(data["facts"]) == 2 and data["relations"][0]["with"] == "Reza Ahmadi"
    assert len(data["history"]) > 5 and "ammonia" in blk(view, "copy")["text"]
    _, view, _ = call(s, "edit_records", {"entity": "customer", "action": "export_person", "person": "Ioana Popescu", "include_sensitive": False})
    assert "ammonia" not in blk(view, "copy")["text"]
    assert call(s, "edit_records", {"entity": "visit", "action": "forget_person", "person": "Ioana Popescu"})[2]


def test_context_and_old_name(salon):
    e, s = salon
    call(s, "capture", {"items": [fact("Ioana Popescu", "goal", "grow her hair long for the wedding")]})
    _, view, _ = call(s, "capture", {"items": [fact("Ioana Popescu", "goal", "short bob", key="hair")]})
    call(s, "capture", {"items": [fact("Ioana Popescu", "goal", "long hair", key="hair")]})
    ctx = call(s, "get_context")[0]
    assert "People knowledge: 2 facts about 1 people" in ctx and "allergy*" in ctx and "spouse_of" in ctx
    assert "1 fact(s) changed" in ctx
    assert e.call("get_customer_context", {"name_or_phone": "Ioana Popescu"}).startswith("# Ioana Popescu")   # old name still works


def test_fresh_setup_seeds_vocabulary_and_key_facts(tmp_path):
    e = ContextEngine(tmp_path / "x.db", TODAY)
    s = build_server(e)
    call(s, "setup_business", {"name": "Test", "vertical": "generic", "locale": "en", "currency": "EUR", "country": "RO"})
    assert e.conn.execute("SELECT COUNT(*) FROM vocab WHERE type='fact_kind'").fetchone()[0] >= 15
    e.conn.execute("INSERT INTO settings(key, value) VALUES ('key_facts', '[\"goal\", \"constraint\"]')")
    call(s, "capture", {"items": [fact("Ana Pop", "goal", "run a 10k")]})
    text, view, _ = call(s, "get_person_brief", {"name_or_phone": "Ana Pop"})
    assert [m["kind"] for m in blk(view, "facts")["missing"]] == ["constraint"] and "Not known yet: Constraint" in text
