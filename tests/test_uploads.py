"""Upload page of the hosted connector: device upload, Google Drive picker, chat exports, safety."""
import io
import json
import re
import zipfile
from dataclasses import replace
from urllib.parse import urlparse

import httpx2 as httpx
import pytest
from starlette.testclient import TestClient

from clouds import FakeCloud
from conftest import DEMO, SETUP, TODAY
from test_hosted import BASE, MCP, FakeGoogle, register, sign_in
from intheknow import uploads as U
from intheknow.auth import google
from intheknow.cloud import cloud_store_factory
from intheknow.hosted import create_app, dev_store_factory
from intheknow.seal import Sealer, _b64d, new_key


def make(tmp_path, cloud: FakeCloud = None, picker=None):
    g = FakeGoogle()
    prov = {"google": replace(google("cid", "csecret"), authorize_url="https://accounts.google.test/auth",
                              token_url="https://oauth2.google.test/token")}
    stores = cloud_store_factory(prov, cloud.client()) if cloud else dev_store_factory(tmp_path / "mem")
    app = create_app(BASE, Sealer([("k1", _b64d(new_key()))]), prov, stores, today=TODAY, picker=picker,
                     http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(g.handler)))
    return app, g


def owner(c, g, setup=True) -> MCP:
    m = MCP(c, sign_in(c, g, register(c), "owner-1")["access_token"])
    if setup:
        m.call("setup_business", SETUP["salon_aurora"])
    return m


def link(m: MCP) -> str:
    url = re.search(r"https://\S+/upload\?t=\S+", m.call("upload_files", {})).group(0)
    return url.replace(BASE, "")


def demo_files(name="salon_aurora"):
    return [("files", (p.name, p.read_bytes())) for p in sorted((DEMO / name).iterdir())]


def claude_export() -> bytes:
    convs = [{"name": "prices", "created_at": "2026-03-01T10:00:00Z", "chat_messages": [
        {"sender": "human", "created_at": "2026-03-01T10:00:00Z", "text": "From April the keratina treatment costs 450 lei."},
        {"sender": "assistant", "text": "Noted."}]}]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data/conversations.json", json.dumps(convs))
    return buf.getvalue()


def test_tool_shapes(tmp_path):
    app, g = make(tmp_path)
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        tools = {t["name"]: t for t in owner(c, g, setup=False).rpc("tools/list", {})["result"]["tools"]}
    assert "upload_files" in tools
    assert "paths" not in tools["add_data"]["inputSchema"]["properties"]                 # no local paths when hosted
    assert "path" not in tools["import_chat_history"]["inputSchema"]["properties"]


def test_upload_from_device(tmp_path):
    app, g = make(tmp_path)
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        m = owner(c, g)
        m.call("get_context", {})                                                    # Claude reads context at the start of a chat
        url = link(m)
        page = c.get(url)
        assert page.status_code == 200 and "Alege fișiere" in page.text                  # business language (ro)
        assert "set-cookie" in page.headers and "frame-ancestors 'none'" in page.headers["content-security-policy"]
        t = urlparse(url).query[2:]
        r = c.post("/upload", data={"t": t}, files=demo_files())
        assert r.status_code == 200 and "Adăugat în memoria ta" in r.text, r.text[:500]
        assert "clienti.xlsx" in r.text and "noi" in r.text
        ctx = m.call("get_context", {})
        assert "files imported" in ctx                                               # Claude sees what arrived
        assert "Salon Aurora" in ctx


def test_chat_export_upload(tmp_path):
    app, g = make(tmp_path)
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        m = owner(c, g)
        t = urlparse(link(m)).query[2:]
        c.get(f"/upload?t={t}")
        r = c.post("/upload", data={"t": t}, files=[("files", ("claude-export.zip", claude_export()))])
        assert r.status_code == 200 and "Claude / ChatGPT" in r.text
        batch = m.call("import_chat_history", {})
        assert "keratina" in batch and "450 lei" in batch


def test_link_bound_to_first_browser(tmp_path):
    app, g = make(tmp_path)
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        url = link(owner(c, g))
        assert c.get(url).status_code == 200
        assert c.get(url).status_code == 200                                          # same browser may come back
        other = TestClient(app, base_url=BASE, follow_redirects=False)              # another browser, no cookie
        assert other.get(url).status_code == 403
        t = urlparse(url).query[2:]
        assert other.post("/upload", data={"t": t}, files=demo_files()).status_code == 403
        assert c.get("/upload?t=forged").status_code == 410
        fresh = urlparse(link(owner(c, g, setup=False))).query[2:]
        assert other.post("/upload", data={"t": fresh}, files=demo_files()).status_code == 403   # posting cannot claim a link


def test_not_set_up_and_limits(tmp_path, monkeypatch):
    app, g = make(tmp_path)
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        m = owner(c, g, setup=False)
        res = m.rpc("tools/call", {"name": "upload_files", "arguments": {}})["result"]
        assert res["isError"]                                                         # set up first
        m.call("setup_business", SETUP["salon_aurora"])
        t = urlparse(link(m)).query[2:]
        c.get(f"/upload?t={t}")
        r = c.post("/upload", data={"t": t}, files=[("files", ("photo.png", b"\x89PNG..."))])
        assert r.status_code == 200 and "photo.png" in r.text                         # listed as not supported
        monkeypatch.setattr(U, "MAX_FILE", 10)
        r = c.post("/upload", data={"t": t}, files=[("files", ("big.csv", b"a,b\n" * 100))])
        assert r.status_code == 413


def test_google_drive_picker(tmp_path):
    cloud = FakeCloud()
    app, g = make(tmp_path, cloud, picker=U.PickerConfig("api-key", "123456"))
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        m = owner(c, g)
        url = link(m)
        page = c.get(url)
        assert "Alege din Google Drive" in page.text and "apis.google.com" in page.headers["content-security-policy"]
        assert re.search(r'TOKEN="at-\d+"', page.text)                               # short-lived Drive token for the Picker
        prices = (DEMO / "salon_aurora" / "lista_preturi.csv").read_bytes()
        csv_id = cloud.add_drive_file("lista_preturi.csv", "text/csv", prices)
        sheet_id = cloud.add_drive_file("Cheltuieli 2026", "application/vnd.google-apps.spreadsheet",
                                        export=(DEMO / "salon_aurora" / "cheltuieli.csv").read_bytes())
        t = urlparse(url).query[2:]
        r = c.post("/upload/drive", data={"t": t, "ids": f"{csv_id},{sheet_id}"})
        assert r.status_code == 200 and "lista_preturi.csv" in r.text and "Cheltuieli 2026.csv" in r.text, r.text[:400]
        assert len(cloud.drive_memory()) == 1                                         # imported into the one memory file


def test_local_mode_keeps_paths():
    import asyncio
    from intheknow.context import ContextEngine
    from intheknow.mcp_server import build_server
    from intheknow.store import MemoryStore
    tools = {t.name: t for t in asyncio.run(build_server(ContextEngine(MemoryStore(), TODAY)).list_tools())}
    assert "upload_files" not in tools and "paths" in tools["add_data"].input_schema["properties"]
