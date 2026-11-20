"""The engine reproduces the ground truth of three messy businesses exactly."""
import pytest

from intheknow import metrics as M
from intheknow import schema
from intheknow.normalize import name_key
from conftest import TODAY, truth

NAMES = ["salon_aurora", "kine_saone", "kaffeehaus"]


@pytest.mark.parametrize("name", NAMES)
def test_core_numbers_match_ground_truth(engines, name):
    e, gt = engines[name], truth(name)
    cur = M.period_summary(e.conn, *M.month_bounds("2026-10"))
    prev = M.period_summary(e.conn, *M.month_bounds("2026-09"))
    assert cur["revenue"] == gt["revenue"]
    assert prev["revenue"] == gt["revenue_prev"]
    assert M.pct_change(cur["revenue"], prev["revenue"]) == gt["change_pct"]
    assert cur["by_offering"][0]["name"] == gt["top_offering"]
    assert cur["by_offering"][0]["revenue"] == gt["top_offering_revenue"]
    assert cur["by_staff"][0]["name"] == gt["top_staff"]
    assert cur["cost_of_goods"] == gt["cost_of_goods"]
    assert cur["expenses"] == gt["costs"]
    assert cur["net_profit"] == gt["net_profit"]
    assert M.offering_margins(e.conn)[0]["name"] == gt["best_margin_offering"]


@pytest.mark.parametrize("name", ["salon_aurora", "kine_saone"])
def test_people_resolved_across_files_and_spellings(engines, name):
    e, gt = engines[name], truth(name)
    assert e.conn.execute("SELECT COUNT(*) FROM v_parties").fetchone()[0] == gt["customers"]


def test_salon_lapsed_duplicate_and_campaign(engines):
    e, gt = engines["salon_aurora"], truth("salon_aurora")
    stats = M.customer_stats(e.conn, TODAY)
    lapsed = M.segment(stats, "lapsed_regulars")
    assert len(lapsed) == gt["lapsed_regulars"]
    assert [name_key(s.name) for s in lapsed[:3]] == [name_key(n) for n in gt["lapsed_top3"]]
    ioana = next(s for s in stats if name_key(s.name) == name_key("Ioana Popescu"))
    d = gt["duplicate_person"]
    assert (ioana.visits, ioana.total_spend, ioana.last_visit) == (d["visits"], d["spend"], d["last_visit"])
    assert ioana.name == "Ioana Popescu"                       # all-caps duplicate row did not win the spelling
    c = gt["campaign"]
    ids = [r["id"] for r in e.conn.execute("SELECT id FROM v_customers WHERE first_seen BETWEEN ? AND ? AND visits > 0", (c["start"], c["end"]))]
    returned = sum(e.conn.execute("SELECT COUNT(DISTINCT date) FROM v_transactions WHERE party_id=? AND counted=1", (i,)).fetchone()[0] > 1 for i in ids)
    assert (len(ids), returned) == (c["new"], c["returned"])


def test_clinic_no_shows_and_split_names(engines):
    e, gt = engines["kine_saone"], truth("kine_saone")
    cur = M.period_summary(e.conn, *M.month_bounds("2026-10"))
    assert cur["no_show_rate"] == gt["no_show_rate"]
    assert cur["no_shows"] == gt["no_shows_oct"]
    assert e.find_party("Marie Dubois") is not None or e.find_party("Dubois") is not None


def test_cafe_receipts_overlap_and_loyalty_cards(engines):
    e, gt = engines["kaffeehaus"], truth("kaffeehaus")
    cur = M.period_summary(e.conn, *M.month_bounds("2026-10"))
    assert cur["receipts"] == gt["receipts"] and cur["avg_ticket"] == gt["avg_ticket"]
    assert max(cur["by_hour"], key=cur["by_hour"].get) == gt["busiest_hour"]
    assert e.conn.execute("SELECT COUNT(*) FROM v_transactions").fetchone()[0] == gt["rows_total"]   # overlap week not double counted
    linked = e.conn.execute("SELECT COUNT(DISTINCT party_id) FROM v_transactions WHERE party_id IS NOT NULL").fetchone()[0]
    assert linked == gt["regulars"]                             # sales linked to regulars through the loyalty card


def test_unknown_columns_become_undoable_fields(engines):
    for name in ("salon_aurora", "kine_saone"):
        e, gt = engines[name], truth(name)
        party = schema.primary(e.conn, "party")
        assert schema.field(party, gt["new_field"]) is not None
    e = engines["kine_saone"]
    cid = e.conn.execute("SELECT id FROM schema_changes WHERE op='add_field' AND json_extract(payload,'$.field')='mutuelle'").fetchone()[0]
    schema.undo(e.conn, cid)
    assert schema.field(schema.primary(e.conn, "party"), "mutuelle") is None


def test_reimport_is_idempotent(tmp_path):
    from conftest import loaded, DEMO
    e = loaded(tmp_path, "salon_aurora")
    n = e.conn.execute("SELECT COUNT(*) FROM v_transactions").fetchone()[0]
    e.call_view("add_data", {"paths": [str(DEMO / "salon_aurora")]})
    assert e.conn.execute("SELECT COUNT(*) FROM v_transactions").fetchone()[0] == n
