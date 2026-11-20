"""Owner storage in Google Drive / OneDrive, against in-memory fakes of both APIs."""
import pytest

from clouds import PROVIDERS, FakeCloud
from conftest import SETUP, TODAY
from intheknow.cloud import GoogleDriveStore, OneDriveStore, cloud_store_factory
from intheknow.context import ContextEngine
from intheknow.store import Conflict

BOTH = ["google", "microsoft"]


def store(cloud: FakeCloud, p: str, rt: str = "rt-1"):
    return cloud_store_factory(PROVIDERS, cloud.client())({"p": p, "sub": "owner", "rt": rt})


def goal(e: ContextEngine, text: str) -> None:
    e.call_view("capture", {"items": [{"type": "goal", "quote": text, "text": text}]})


def goals(e: ContextEngine) -> set[str]:
    return {r["text"] for r in e.conn.execute("SELECT text FROM knowledge")}


def files(cloud: FakeCloud, p: str) -> list[dict]:
    return cloud.drive_memory() if p == "google" else list(cloud.graph.values())


@pytest.mark.parametrize("p", BOTH)
def test_file_created_only_at_setup(p):
    cloud = FakeCloud()
    e = ContextEngine(store(cloud, p), TODAY)
    e.call_view("get_context", {})
    e.call_view("get_started", {})
    assert files(cloud, p) == []                       # nothing appears in the owner's Drive before setup
    e.call_view("setup_business", SETUP["salon_aurora"])
    [f] = files(cloud, p)
    if p == "google":
        folder = cloud.drive[f["parents"][0]]
        assert folder["name"] == "In The Know" and f["name"] == "memory.itk"
    else:
        assert f["name"] == "memory.itk"


@pytest.mark.parametrize("p", BOTH)
def test_two_devices_share_one_memory(p):
    cloud = FakeCloud()
    phone = ContextEngine(store(cloud, p), TODAY)
    phone.call_view("setup_business", SETUP["salon_aurora"])
    goal(phone, "from phone")
    desktop = ContextEngine(store(cloud, p), TODAY)    # separate session, as after a server restart
    assert goals(desktop) == {"from phone"}
    goal(desktop, "from desktop")
    goal(phone, "phone again")                         # phone first picks up the desktop's save
    assert goals(ContextEngine(store(cloud, p), TODAY)) == {"from phone", "from desktop", "phone again"}
    assert len(files(cloud, p)) == 1


@pytest.mark.parametrize("p", BOTH)
def test_reads_do_not_upload(p):
    cloud = FakeCloud()
    e = ContextEngine(store(cloud, p), TODAY)
    e.call_view("setup_business", SETUP["salon_aurora"])
    goal(e, "x")
    for t in ("get_context", "get_metrics", "get_data_status"):
        e.call_view(t, {})                             # first use of each tool is logged once
    cloud.requests.clear()
    for _ in range(3):
        for t in ("get_context", "get_metrics", "get_data_status"):
            e.call_view(t, {})
    assert not [r for r in cloud.requests if r.startswith(("PUT", "PATCH", "POST"))]


@pytest.mark.parametrize("p", BOTH)
def test_stale_save_is_refused(p):
    cloud = FakeCloud()
    a, b = store(cloud, p), store(cloud, p)
    v1 = a.save(b"one", None)
    _, seen = b.load()
    a.save(b"two", v1)
    with pytest.raises(Conflict):
        b.save(b"three", seen)


def test_onedrive_conflict_during_upload_is_replayed():
    cloud = FakeCloud()
    a = ContextEngine(store(cloud, "microsoft"), TODAY)
    a.call_view("setup_business", SETUP["salon_aurora"])
    b = ContextEngine(store(cloud, "microsoft"), TODAY)
    cloud.before_put = lambda: goal(b, "other device")  # lands between a's check and a's upload
    goal(a, "this device")
    assert goals(ContextEngine(store(cloud, "microsoft"), TODAY)) == {"this device", "other device"}


def test_google_finds_file_after_rename_and_move():
    cloud = FakeCloud()
    e = ContextEngine(store(cloud, "google"), TODAY)
    e.call_view("setup_business", SETUP["salon_aurora"])
    goal(e, "kept")
    [f] = cloud.drive_memory()
    f["name"], f["parents"] = "old salon backup.itk", ["somewhere-else"]
    assert goals(ContextEngine(store(cloud, "google"), TODAY)) == {"kept"}


def test_google_trash_and_restore():
    cloud = FakeCloud()
    e = ContextEngine(store(cloud, "google"), TODAY)
    e.call_view("setup_business", SETUP["salon_aurora"])
    goal(e, "kept")
    [f] = cloud.drive_memory()
    f["trashed"] = True
    with pytest.raises(RuntimeError, match="trash"):
        e.call_view("get_context", {})
    f["trashed"] = False                               # owner restores it from the Drive trash
    goal(e, "after restore")
    assert goals(e) == {"kept", "after restore"}


def test_onedrive_deleted_file():
    cloud = FakeCloud()
    e = ContextEngine(store(cloud, "microsoft"), TODAY)
    e.call_view("setup_business", SETUP["salon_aurora"])
    cloud.graph.clear()
    with pytest.raises(RuntimeError, match="recycle bin"):
        e.call_view("get_context", {})


@pytest.mark.parametrize("p", BOTH)
def test_access_token_cached_and_renewed(p):
    cloud = FakeCloud()
    e = ContextEngine(store(cloud, p), TODAY)
    e.call_view("setup_business", SETUP["salon_aurora"])
    for _ in range(5):
        e.call_view("get_context", {})
    assert cloud.refreshes == 1
    cloud.expire_tokens()                              # provider ended the token early
    goal(e, "still works")
    assert cloud.refreshes == 2 and "still works" in goals(e)


@pytest.mark.parametrize("p", BOTH)
def test_access_removed(p):
    cloud = FakeCloud()
    e = ContextEngine(store(cloud, p), TODAY)
    e.call_view("setup_business", SETUP["salon_aurora"])
    cloud.revoked.update({"rt-1", "rt-1+"})
    cloud.expire_tokens()
    with pytest.raises(Exception, match="reconnect"):
        e.call_view("get_context", {})


def test_microsoft_refresh_token_rotation():
    cloud = FakeCloud()
    s = store(cloud, "microsoft")
    s.version()
    assert isinstance(s, OneDriveStore) and s.s.refresh_token == "rt-1+"
    g = store(cloud, "google")
    g.version()
    assert isinstance(g, GoogleDriveStore) and g.s.refresh_token == "rt-1"
