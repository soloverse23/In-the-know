"""Facts brought in from other connectors (Calendar, Gmail, Square...): never stored twice, changes replace, preview first."""
import pytest

from conftest import DEMO, SETUP, TODAY
from intheknow.context import ContextEngine
from intheknow.store import MemoryStore


@pytest.fixture(scope="module")
def salon_bytes() -> bytes:
    e = ContextEngine(MemoryStore(), TODAY)
    e.call_view("setup_business", SETUP["salon_aurora"])
    e.call_view("add_data", {"paths": [str(DEMO / "salon_aurora")]})
    return e.conn.serialize()


@pytest.fixture()
def e(salon_bytes):
    return ContextEngine(MemoryStore(salon_bytes), TODAY)


def cap(e, items, **kw):
    _, env = e.call_view("capture", {"items": items, **kw})
    return env["blocks"][0]["items"]


def synced_tx(e):
    return [dict(r) for r in e.conn.execute("SELECT * FROM v_transactions WHERE source LIKE 'sync:%' ORDER BY id")]


def visit(eid, customer="Mihaela Stoica", offering="Tuns", amount="120", date="2026-10-28", status=None, staff="Bianca"):
    it = {"type": "visit", "quote": f"Calendar: {offering} {customer}", "customer": customer, "offering": offering, "amount": amount,
          "date": date, "staff": staff, "source": "calendar", "external_id": eid}
    if status:
        it["status"] = status
    return it


def meeting(eid, date="2026-11-05", time="10:00", text="Supplier meeting: Loreal"):
    return {"type": "task", "quote": text, "text": text, "date": date, "time": time, "source": "google_calendar", "external_id": eid}


def test_calendar_synced_twice_is_stored_once(e):
    items = [visit("ev1"), visit("ev2", customer="Simona Matei", offering="Vopsit", amount="250"), meeting("ev3")]
    first = cap(e, items)
    assert [i["status"] for i in first] == ["saved", "saved", "saved"]
    assert all(any(c.get("icon") == "sync" for c in i["chips"]) for i in first)          # card shows where it came from
    again = cap(e, items)
    assert [i["status"] for i in again] == ["known", "known", "known"]
    assert len(synced_tx(e)) == 2
    assert e.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_moved_and_cancelled_events_replace_the_old_record(e):
    cap(e, [visit("ev1"), meeting("ev3")])
    moved = cap(e, [meeting("ev3", date="2026-11-07", time="15:30")])[0]
    assert moved["status"] == "saved" and moved["note"]["t"] == "cap.sync_updated"
    [task] = [dict(r) for r in e.conn.execute("SELECT due, due_time FROM tasks")]
    assert task == {"due": "2026-11-07", "due_time": "15:30"}
    cap(e, [visit("ev1", status="cancelled", amount="0")])
    [tx] = synced_tx(e)                                                                    # replaced, not added
    assert tx["amount"] == 0


def test_change_that_cannot_be_saved_keeps_the_old_record(e):
    cap(e, [visit("ev1")])
    res = cap(e, [visit("ev1", offering="Something we never sold")])[0]
    assert res["status"] == "clarify"
    [tx] = synced_tx(e)
    assert tx["offering"] == "Tuns"                                                        # nothing half-replaced


def test_one_email_several_facts(e):
    mail = [{"type": "task", "quote": "I'll pay the rest (200 lei) on Friday", "text": "Raluca pays the rest", "customer": "Raluca Dumitru",
             "amount": "200", "date": "2026-11-07", "source": "gmail", "external_id": "msg-77"},
            {"type": "fact", "quote": "I'm allergic to ammonia", "customer": "Raluca Dumitru", "fact_kind": "allergy",
             "text": "allergic to ammonia", "source": "gmail", "external_id": "msg-77"}]
    assert [i["status"] for i in cap(e, mail)] == ["saved", "saved"]
    assert [i["status"] for i in cap(e, mail)] == ["known", "known"]


def test_square_payments_imported_twice(e):
    rows = [{"date": "2026-10-30", "customer": "Cristina Marin", "service": "Coafat", "amount": "90", "external_id": "pay_1"},
            {"date": "2026-10-30", "customer": "Simona Matei", "service": "Manichiură", "amount": "70", "external_id": "pay_2"}]
    tx = next(n for n, s in __import__("intheknow.schema", fromlist=["x"]).entities(e.conn).items() if s["role"] == "transaction")
    _, env = e.call_view("import_records", {"entity": tx, "rows": rows, "source": "square"})
    assert env["blocks"][-1]["items"][0]["loaded"] == 2
    _, env = e.call_view("import_records", {"entity": tx, "rows": rows, "source": "square"})
    item = env["blocks"][-1]["items"][0]
    assert item["loaded"] == 0 and item["duplicate"] == 2
    assert len([t for t in synced_tx(e) if t["source"] == "sync:square"]) == 2


def test_preview_saves_nothing(e):
    store = e.store
    cap(e, [visit("ev9")], dry_run=True)                                                   # first use of capture is logged once
    n = store.n
    prev = cap(e, [visit("ev9"), meeting("ev10")], dry_run=True)
    assert [i["status"] for i in prev] == ["ready", "ready"] and not any("undo" in i for i in prev)
    assert synced_tx(e) == [] and store.n == n                                              # not even uploaded
    assert [i["status"] for i in cap(e, [visit("ev9"), meeting("ev10")])] == ["saved", "saved"]


def test_context_tells_when_each_app_was_synced(e):
    e.call_view("get_context", {})
    cap(e, [visit("ev1"), {**meeting("ev3"), "source": "outlook"}])
    ctx = e.call("get_context", {})
    assert "Last synced from other apps: calendar 2026-11-01, outlook 2026-11-01" in ctx


def test_synced_record_can_be_undone_and_owner_words_unchanged(e):
    [res] = cap(e, [visit("ev1")])
    e.call_view("undo_change", {"change_id": res["undo"]})
    assert synced_tx(e) == []
    [own] = cap(e, [{"type": "visit", "quote": "Ana had a cut", "customer": "Mihaela Stoica", "offering": "Tuns", "amount": "120"}])
    assert own["status"] == "saved" and not any(c.get("icon") == "sync" for c in own["chips"])
