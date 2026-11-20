"""The whole product through the MCP server: onboarding from empty, chat capture, dynamic schema, design, imports."""
import asyncio
import json
import re
import zipfile

import pytest

from intheknow import i18n
from intheknow.context import ContextEngine
from intheknow.mcp_server import build_server
from conftest import DEMO, SETUP, TODAY, loaded


def call(server, name, args=None):
    r = asyncio.run(server.call_tool(name, args or {}))
    return r.content[0].text, r.structured_content, bool(r.is_error)


@pytest.fixture
def salon(tmp_path):
    e = loaded(tmp_path, "salon_aurora")
    return e, build_server(e)


def test_server_surface(tmp_path):
    s = build_server(ContextEngine(tmp_path / "x.db", TODAY))
    tools = {t.name: t for t in asyncio.run(s.list_tools())}
    assert len(tools) == 31
    for name in ("get_started", "capture", "add_data", "get_business_brief", "update_design", "change_schema", "import_records"):
        assert tools[name].meta["ui"]["resourceUri"] == "ui://intheknow/view.html"
    assert tools["get_business_brief"].annotations.read_only_hint and not tools["capture"].annotations.read_only_hint
    assert tools["undo_change"].annotations.destructive_hint
    assert {p.name for p in asyncio.run(s.list_prompts())} == {"start", "onboarding", "weekly_review", "win_back", "import_history", "design_dashboard", "monthly_report", "sync_from"}
    assert {str(r.uri) for r in asyncio.run(s.list_resources())} >= {"ui://intheknow/view.html", "intheknow://brief", "intheknow://this-week"}
    html = list(asyncio.run(s.read_resource("ui://intheknow/view.html")))[0].content
    assert '"ro"' in html and "Încasări" in html                      # all four dictionaries are embedded


def test_onboarding_from_empty_memory(tmp_path):
    e = ContextEngine(tmp_path / "new.db", TODAY)
    s = build_server(e)
    text, view, err = call(s, "get_business_brief")                # empty -> onboarding, never an error
    assert view["view"] == "get_started" and "[>] Tell me about your business" in text
    text, err_view, err = call(s, "capture", {"items": [{"type": "fact", "quote": "x", "text": "y"}]})
    assert err and "not set up" in text
    text, view, _ = call(s, "setup_business", SETUP["salon_aurora"])
    assert view["lang"] == "ro" and "Ești asistentul afacerii Salon Aurora" in text   # Project instructions in Romanian
    call(s, "get_started", {"step_done": "instructions"})
    call(s, "add_data", {"paths": [str(DEMO / "salon_aurora")]})
    text, view, _ = call(s, "get_started")
    assert "[x] Bring in your data" in text and "[>] Check what I understood" in text
    call(s, "get_started", {"step_done": "review"})
    call(s, "capture", {"items": [{"type": "goal", "quote": "q", "text": "50.000 lei pe lună"}]})
    call(s, "review_period", {"start": "2026-10-01", "end": "2026-10-31"})
    call(s, "update_design", {"patch": {"brand": {"primary": "#123456"}}})
    text, view, _ = call(s, "get_started")
    assert all(i["state"] == "done" for i in next(b for b in view["blocks"] if b["type"] == "checklist")["items"])


def test_capture_is_precise(salon):
    e, s = salon
    text, view, _ = call(s, "capture", {"items": [
        {"type": "visit", "quote": "q", "customer": "Ioana Popescu", "offering": "hair color", "amount": "350 lei", "staff": "Bianca"},
        {"type": "visit", "quote": "q", "customer": "Maria", "offering": "tuns", "amount": "120"},          # ambiguous first name
        {"type": "visit", "quote": "q", "customer": "Popescu", "offering": "tuns", "amount": "120"},        # partial -> ask, never guess
        {"type": "visit", "quote": "q", "customer": "Elena Nouă", "offering": "lift gene", "amount": "200"},  # unknown offering
        {"type": "service", "quote": "q", "offering": "Lift gene", "price": "200 lei", "unit_cost": "40"},
        {"type": "customer_note", "quote": "q", "customer": "ioana popescu", "text": "doar sâmbăta"},
    ]})
    items = next(b for b in view["blocks"] if b["type"] == "feed")["items"]
    st = [i["status"] for i in items]
    # the new service is saved first in the batch, so the visit with it goes through; ambiguous/partial names are asked
    assert st.count("saved") == 4 and st.count("clarify") == 2
    assert "Several customers match “Maria”" in text and "Several customers match “Popescu”" in text
    _, view, _ = call(s, "capture", {"items": [{"type": "visit", "quote": "q", "customer": "Elena Nouă", "offering": "lift gene", "amount": "200"}]})
    assert next(b for b in view["blocks"] if b["type"] == "feed")["items"][0]["status"] == "known"
    # same visit again -> known, not double counted
    text, view, _ = call(s, "capture", {"items": [{"type": "visit", "quote": "q", "customer": "Ioana Popescu", "offering": "vopsit", "amount": "350", "staff": "Bianca"}]})
    assert "ALREADY KNOWN" in text
    # undo from the card
    rec = next(i["undo"] for i in items if i["status"] == "saved" and i["kind"] == "transaction")
    before = e.conn.execute("SELECT COUNT(*) FROM v_transactions").fetchone()[0]
    call(s, "undo_change", {"change_id": rec})
    assert e.conn.execute("SELECT COUNT(*) FROM v_transactions").fetchone()[0] == before - 1
    _, view, _ = call(s, "get_person_brief", {"name_or_phone": "POPESCU IOANA"})
    ent = next(b for b in view["blocks"] if b["type"] == "entity")
    assert "doar sâmbăta" in ent["notes"]


def test_custom_trade_with_dynamic_entity(tmp_path):
    e = ContextEngine(tmp_path / "groomer.db", TODAY)
    s = build_server(e)
    call(s, "setup_business", {"name": "Pfotenglück", "vertical": "generic", "locale": "de", "currency": "EUR", "country": "DE"})
    text, view, err = call(s, "change_schema", {"action": "create_entity", "spec": {
        "name": "pet", "role": "custom", "label": {"en": ["Pet", "Pets"], "de": ["Tier", "Tiere"]},
        "fields": [{"name": "name", "type": "text"}, {"name": "species", "type": "enum", "values": ["dog", "cat"]},
                   {"name": "owner", "type": "ref", "ref": "customer"}, {"name": "breed", "type": "text"}]}})
    assert not err
    call(s, "capture", {"items": [
        {"type": "customer_info", "quote": "q", "customer": "Anna Schmidt", "phone": "0151 2345678"},
        {"type": "offering", "quote": "q", "offering": "Waschen & Schneiden", "price": "55 €", "unit_cost": "6"},
        {"type": "record", "quote": "q", "entity": "pet", "fields": {"name": "Bello", "species": "dog", "owner": "Anna Schmidt", "breed": "Pudel"}},
        {"type": "sale", "quote": "q", "customer": "Anna Schmidt", "offering": "waschen & schneiden", "amount": "55"}]})
    _, view, _ = call(s, "get_person_brief", {"name_or_phone": "Anna Schmidt"})
    pets = next(b for b in view["blocks"] if b["type"] == "table")
    assert pets["rows"][0]["name"] == "Bello" and pets["title"]["raw"] == "Tiere"
    text, view, err = call(s, "change_schema", {"action": "create_entity", "spec": {"name": "clients2", "role": "party", "fields": [{"name": "name", "type": "text", "role_attr": "name"}]}})
    assert err and "already has a party" in text                        # governance: no second customer table


def test_design_from_chat(salon):
    e, s = salon
    text, view, err = call(s, "update_design", {"patch": {"brand": {"primary": "#6B8E23", "font": "Fraunces"},
                                                          "brief": {"sections": ["kpis", "trend"], "kpis": ["revenue", "net_profit"]},
                                                          "views": {"team": {"title": "Echipa", "blocks": [{"type": "chart", "by": "staff"},
                                                                                                             {"type": "table", "source": "staff"}]}}}})
    assert not err
    _, view, _ = call(s, "get_business_brief")
    assert view["brand"]["primary"] == "#6B8E23" and [b["type"] for b in view["blocks"]] == ["kpis", "chart"]
    _, view, _ = call(s, "show_view", {"name": "team"})
    assert [b["type"] for b in view["blocks"]] == ["chart", "table"]
    for bad in ({"brand": {"primary": "red"}}, {"brand": {"font": "Comic Sans"}}, {"views": {"x": {"blocks": [{"type": "html", "html": "<script>"}]}}},
                {"brief": {"sections": ["kpis", "evil"]}}):
        assert call(s, "update_design", {"patch": bad})[2]
    call(s, "undo_change", {"change_id": "design"})
    _, view, _ = call(s, "get_business_brief")
    assert view["brand"]["primary"] != "#6B8E23"


def test_metrics_and_sql_are_read_only(salon):
    e, s = salon
    text, view, err = call(s, "define_metric", {"name": "keratin_share", "label": {"en": "Keratin share", "ro": "Pondere keratină"}, "format": "percent",
        "sql": "SELECT 100.0 * SUM(CASE WHEN offering='Keratină' THEN amount END) / SUM(amount) FROM v_transactions WHERE counted=1 AND date BETWEEN :start AND :end"})
    assert not err
    call(s, "update_design", {"patch": {"brief": {"kpis": ["revenue", "keratin_share"]}}})
    _, view, _ = call(s, "get_business_brief")
    assert view["blocks"][0]["items"][1]["label_text"]["ro"] == "Pondere keratină"
    assert call(s, "define_metric", {"name": "bad", "label": {"en": "x"}, "sql": "DELETE FROM e_visit"})[2]
    assert "Error" in call(s, "query_data", {"sql": "UPDATE e_visit SET amount=0"})[0]
    assert "Error" in call(s, "query_data", {"sql": "SELECT 1; DROP TABLE e_visit"})[0]
    assert "| n |" in call(s, "query_data", {"sql": "SELECT COUNT(*) AS n FROM v_transactions"})[0]


def test_photo_rows_unknown_file_and_mapping_memory(salon):
    e, s = salon
    text, view, _ = call(s, "import_records", {"entity": "visit", "source": "photo:agenda.jpg", "rows": [
        {"date": "2026-10-30", "customer": "Ioana Popescu", "service": "Vopsit", "staff": "Bianca", "amount": "350"},
        {"date": "2026-10-30", "customer": "Ana Nouă", "service": "Tuns", "amount": "120 lei"}]})
    assert "2 new" in text
    csv = "Ziua;Cine;Ce;Cât\n30.10.2026;Ioana Popescu;Pensat;40\n"
    text, view, _ = call(s, "add_data", {"filename": "caiet2.csv", "content": csv})
    f = next(b for b in view["blocks"] if b["type"] == "files")["items"][0]
    assert f["entity"] == "unknown"
    text, view, err = call(s, "map_columns", {"file_id": f["file_id"], "entity": "visit",
                                              "columns": {"Ziua": "date", "Cine": "customer.name", "Ce": "service.name", "Cât": "amount"}})
    assert not err and "1 new" in text
    text, view, _ = call(s, "add_data", {"filename": "caiet3.csv", "content": "Ziua;Cine;Ce;Cât\n31.10.2026;Ioana Popescu;Tuns;120\n"})
    assert "→ visit: 1 new" in text                                      # same layout recognized from memory


def test_old_chats_import(salon, tmp_path):
    e, s = salon
    claude = [{"name": "Salon", "created_at": "2026-09-01T10:00:00Z", "chat_messages": [
        {"sender": "human", "text": "Azi a venit Ioana Popescu la vopsit, 350 lei", "created_at": "2026-09-01T10:00:00Z"},
        {"sender": "assistant", "text": "Notat", "created_at": "2026-09-01T10:00:05Z"},
        {"sender": "human", "text": "mulțumesc!", "created_at": "2026-09-01T10:01:00Z"}]}]
    p = tmp_path / "export.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("conversations.json", json.dumps(claude))
    text, view, _ = call(s, "import_chat_history", {"path": str(p)})
    assert "Ioana Popescu la vopsit" in text and "mulțumesc" not in text
    gpt = [{"title": "Shop", "create_time": 1780000000, "mapping": {"a": {"message": {"author": {"role": "user"}, "content": {"parts": ["Chiria a crescut la 7.000 lei"]}, "create_time": 1780000000}}}}]
    (tmp_path / "conversations.json").write_text(json.dumps(gpt))
    text, view, _ = call(s, "import_chat_history", {"path": str(tmp_path / "conversations.json")})
    assert "Chiria" in text
    text, _, _ = call(s, "import_chat_history")
    assert "All relevant old messages have been processed" in text


def test_every_view_is_language_neutral(salon):
    e, s = salon
    for name, args in (("get_business_brief", {}), ("get_action_list", {}), ("get_data_status", {}), ("find_customers", {"segment": "top_spenders"}),
                       ("prepare_decision_context", {"topic": "reducere keratină", "offering": "Keratină"})):
        text, view, err = call(s, name, args)
        assert not err and view["lang"] == "ro"
        assert not re.search(r"[؀-ۿ]", text + json.dumps(view, ensure_ascii=False))   # no Persian anywhere


def test_dictionaries_complete():
    en = set(i18n.table("en"))
    for lg in ("fr", "ro", "de"):
        assert set(i18n.table(lg)) == en, lg
    used = set()
    import pathlib
    for f in pathlib.Path("intheknow").glob("*.py"):
        used |= set(re.findall(r'T\("([a-z_]+\.[a-z0-9_.]+)"', f.read_text()))
    assert used <= en, used - en
