"""Storage layer: the engine works in memory and moves the whole file through a Store."""
import pytest

from conftest import DEMO, SETUP, TODAY
from intheknow.context import ContextEngine
from intheknow.store import Conflict, LocalStore, MemoryStore


def goal(e: ContextEngine, text: str) -> None:
    e.call_view("capture", {"items": [{"type": "goal", "quote": text, "text": text}]})


def goals(e: ContextEngine) -> set[str]:
    return {r["text"] for r in e.conn.execute("SELECT text FROM knowledge")}


def fresh(store) -> ContextEngine:
    e = ContextEngine(store, TODAY)
    e.call_view("setup_business", SETUP["salon_aurora"])
    return e


def test_memory_round_trip():
    s = MemoryStore()
    a = fresh(s)
    a.call_view("add_data", {"paths": [str(DEMO / "salon_aurora")]})
    goal(a, "Open a second chair")
    b = ContextEngine(s, TODAY)                          # e.g. the server after a restart
    assert "Open a second chair" in goals(b) and goals(b) == goals(a)
    assert b.call("get_business_brief", {}) == a.call("get_business_brief", {})


def test_reads_do_not_save():
    s = MemoryStore()
    e = fresh(s)
    goal(e, "x")
    for tool in ("get_context", "get_metrics", "search_knowledge"):
        e.call_view(tool, {"query": "x"} if tool == "search_knowledge" else {})      # first use is logged once
    n = s.n
    for tool in ("get_context", "get_metrics", "search_knowledge", "get_data_status"):
        for _ in range(2):
            e.call_view(tool, {"query": "x"} if tool == "search_knowledge" else {})
    assert s.n <= n + 1                                  # only get_data_status's first use


def test_second_device_sees_changes():
    s = MemoryStore()
    phone = fresh(s)
    desktop = ContextEngine(s, TODAY)
    goal(phone, "from phone")
    goal(desktop, "from desktop")                        # desktop picks up the phone's save first
    goal(phone, "phone again")
    assert goals(ContextEngine(s, TODAY)) == {"from phone", "from desktop", "phone again"}


class RacingStore(MemoryStore):
    """Another device saves between our load and our save, once."""
    other = None

    def save(self, data, base_version):
        if self.other is not None:
            other, self.other = self.other, None
            goal(other, "other device")
        return super().save(data, base_version)


def test_conflict_is_replayed_without_losing_either_change():
    s = RacingStore()
    a = fresh(s)
    b = ContextEngine(s, TODAY)
    s.other = b
    goal(a, "this device")
    assert goals(ContextEngine(s, TODAY)) == {"this device", "other device"}
    assert goals(a) == {"this device", "other device"}


class FailingStore(MemoryStore):
    fail = False

    def save(self, data, base_version):
        if self.fail:
            raise OSError("network down")
        return super().save(data, base_version)


def test_failed_save_leaves_no_unsaved_change_in_memory():
    s = FailingStore()
    e = fresh(s)
    goal(e, "kept")
    s.fail = True
    with pytest.raises(OSError):
        goal(e, "lost")
    assert goals(e) == {"kept"}                          # memory matches what is stored
    s.fail = False
    goal(e, "after")
    assert goals(ContextEngine(s, TODAY)) == {"kept", "after"}


def test_local_store_file(tmp_path):
    path = tmp_path / "biz" / "business.db"
    e = fresh(path)
    goal(e, "on disk")
    assert path.exists() and not list(path.parent.glob(".itk-*"))      # atomic write, no temp left
    assert goals(ContextEngine(path, TODAY)) == {"on disk"}
    st = LocalStore(path)
    with pytest.raises(Conflict):
        st.save(b"", "stale")
    path.unlink()
    with pytest.raises(RuntimeError, match="missing"):                  # never silently start empty
        goal(e, "after delete")


def test_today_follows_business_timezone():
    import datetime as dt
    from zoneinfo import ZoneInfo
    e = ContextEngine(MemoryStore())
    e.call_view("setup_business", SETUP["salon_aurora"])          # country RO
    assert e.today == dt.datetime.now(ZoneInfo("Europe/Bucharest")).date()
