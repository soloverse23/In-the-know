"""Smarter behaviour: quiet context, cards only on request, tasks, recurring costs, grouped questions."""
import asyncio

from intheknow import metrics as M
from conftest import DEMO, SETUP, TODAY, loaded
from intheknow.context import ContextEngine
from intheknow.mcp_server import build_server


def call(s, name, args=None):
    r = asyncio.run(s.call_tool(name, args or {}))
    return r.content[0].text, r.structured_content, bool(r.is_error)


def test_quiet_tools_draw_no_card(tmp_path):
    e = loaded(tmp_path, "salon_aurora")
    s = build_server(e)
    tools = {t.name: t for t in asyncio.run(s.list_tools())}
    for quiet in ("get_context", "get_metrics", "resolve_question", "mark_contacted", "complete_task", "undo_change", "query_data", "find_records"):
        assert not (tools[quiet].meta or {}).get("ui"), quiet
    for card in ("get_business_brief", "get_action_list", "capture", "review_period"):
        assert tools[card].meta["ui"]["resourceUri"]
    text, view, _ = call(s, "get_context")
    assert view is None and "Last full month 2026-10" in text and "Show a card only if" in text
    call(s, "capture", {"items": [{"type": "visit", "quote": "q", "customer": "Ioana Popescu", "offering": "vopsit", "amount": "350"}]})
    text, _, _ = call(s, "get_context")
    assert "Since the last check: 1 new visit" in text
    text, view, _ = call(s, "get_metrics", {"metrics": ["revenue", "net_profit"], "start": "2026-10-01", "end": "2026-10-31", "by": "staff"})
    assert view is None and "17,110 lei" in text and "previous 23,060 lei" in text and "Bianca" in text


def test_grouped_questions_and_batch_resolution(tmp_path):
    e = loaded(tmp_path, "salon_aurora")
    s = build_server(e)
    qs = e.conn.execute("SELECT kind, json_extract(payload,'$.count') FROM questions WHERE status='open'").fetchall()
    assert [tuple(q) for q in qs] == [("new_parties", 19)]                          # one grouped question, not 19
    text, view, err = call(s, "resolve_question", {"resolution": "confirm", "all_open": True})
    assert view is None and "1 question(s) confirmed" in text
    assert e.conn.execute("SELECT COUNT(*) FROM v_parties WHERE provisional=1").fetchone()[0] == 0


def test_no_questions_when_there_is_no_customer_list(tmp_path):
    e = ContextEngine(tmp_path / "x.db", TODAY)
    s = build_server(e)
    call(s, "setup_business", {"name": "Shop", "vertical": "cafe", "locale": "en", "currency": "USD", "country": "RO"})
    call(s, "add_data", {"filename": "sales.csv", "content": "date,customer,product,amount\n2026-10-20,Ana Pop,Item A,140\n2026-10-21,Dan Ionescu,Item B,250\n"})
    assert e.conn.execute("SELECT COUNT(*) FROM questions WHERE kind LIKE 'new_part%'").fetchone()[0] == 0


def test_tasks_and_recurring_costs_from_chat(tmp_path):
    e = loaded(tmp_path, "salon_aurora")
    s = build_server(e)
    lm = M.month_bounds("2026-10")
    before = M.period_summary(e.conn, *lm)["net_profit"]
    text, view, _ = call(s, "capture", {"items": [
        {"type": "expense", "quote": "chiria e de acum 7.000 lei pe lună", "category": "Chirie", "amount": "7.000 lei", "recurring": "monthly", "month": "2026-10"},
        {"type": "task", "quote": "Ana îmi dă 200 lei poimâine", "text": "Ana îmi plătește restul", "customer": "Ioana Popescu", "amount": "200", "date": "day after tomorrow"},
        {"type": "task", "quote": "mâine la 20:00 întâlnire cu furnizorul", "text": "Întâlnire cu furnizorul", "date": "tomorrow", "time": "20:00", "staff": "Bianca"}]})
    feed = next(b for b in view["blocks"] if b["type"] == "feed")["items"]
    assert [i["status"] for i in feed] == ["saved", "saved", "saved"]
    # the recorded October rent (6,500) wins over the recurring one for October; November onwards uses 7,000
    assert M.period_summary(e.conn, *lm)["net_profit"] == before
    nov = M.costs_for(e.conn, *M.month_bounds("2026-11"))
    assert nov["by_category"]["Chirie"] == 700000
    # rent changes again: the old amount stops, the new one starts
    call(s, "capture", {"items": [{"type": "expense", "quote": "q", "category": "Chirie", "amount": "7500", "recurring": "monthly", "month": "2027-01"}]})
    assert M.costs_for(e.conn, *M.month_bounds("2027-01"))["by_category"]["Chirie"] == 750000
    assert M.costs_for(e.conn, *M.month_bounds("2026-12"))["by_category"]["Chirie"] == 700000
    # tasks show up in this week's card with a Done action, and in the quiet context
    _, view, _ = call(s, "get_action_list")
    tasks = view["blocks"][0]
    assert tasks["actions"] == ["done"] and len(tasks["rows"]) == 2 and tasks["rows"][0]["time"] is None or tasks["rows"][0]["due"] == "2026-11-02"
    text, _, _ = call(s, "get_context")
    assert "Întâlnire cu furnizorul (due 2026-11-02 20:00)" in text and "Recurring monthly costs: Chirie" in text
    tid = tasks["rows"][0]["_id"]
    assert "done" in call(s, "complete_task", {"task_id": tid})[0]
    _, view, _ = call(s, "get_action_list")
    assert len(view["blocks"][0]["rows"]) == 1


def test_capture_card_is_compact_when_all_saved(tmp_path):
    e = loaded(tmp_path, "salon_aurora")
    s = build_server(e)
    _, view, _ = call(s, "capture", {"items": [{"type": "fact", "quote": "q", "text": "Salonul e închis lunea."}]})
    feed = view["blocks"][0]
    assert feed["type"] == "feed" and all(i["status"] == "saved" for i in feed["items"])
    html = list(asyncio.run(s.read_resource("ui://intheknow/view.html")))[0].content
    assert "compactItem" in html and 'class="compact"' in html


def test_questions_are_answered_on_the_server_not_by_reading_tables(tmp_path):
    e = loaded(tmp_path, "salon_aurora")
    s = build_server(e)
    # find_records: only matching rows, only the asked fields, links by name, no card
    text, view, err = call(s, "find_records", {"entity": "visit", "where": {"search": "Ioana"}, "fields": ["date", "customer", "amount"],
                                               "sort": "-date", "limit": 3})
    assert not err and view is None
    head = text.splitlines()[1]
    assert head == "| id | date | customer | amount |" and len([l for l in text.splitlines() if l.startswith("| ") and "---" not in l]) == 4
    assert "Ioana" in text and "lei" in text
    text, view, _ = call(s, "find_records", {"entity": "visit", "count_by": "staff"})
    assert view is None and "by staff:" in text and "Bianca" in text
    text, _, err = call(s, "find_records", {"entity": "visit", "where": {"min": {"amount": "300"}}, "fields": ["amount"], "sort": "amount", "limit": 1})
    assert not err and "300" in text
    # the card shows only the chosen rows and columns
    ids = [int(l.split("|")[1]) for l in call(s, "find_records", {"entity": "visit", "limit": 2})[0].splitlines()[3:5]]
    _, view, _ = call(s, "browse_records", {"entity": "visit", "where": {"ids": ids}, "fields": ["date", "customer"]})
    table = [b for b in view["blocks"] if b["type"] == "table"][0]
    assert [c["k"] for c in table["columns"]] == ["id", "date", "customer"] and len(table["rows"]) == 2
    # query_data never dumps a whole table into the conversation
    text, _, _ = call(s, "query_data", {"sql": "SELECT * FROM v_transactions"})
    assert len([l for l in text.splitlines() if l.startswith("| ")]) == 26 and "more rows not shown" in text
