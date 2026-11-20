"""Personal mode: one owner on their own machine, no sign-in, the secret key in the address is the access control."""
import pytest
from starlette.testclient import TestClient

from conftest import SETUP, TODAY
from test_hosted import MCP
from test_uploads import demo_files
from intheknow.context import ContextEngine
from intheknow.hosted import create_personal_app

BASE = "https://me.ngrok.test"
KEY = "k3Y_abcdefghijklmnopqrstuvwxyz012345"


class PersonalMCP(MCP):
    """Same JSON-RPC helper, but no bearer token and the secret path."""

    def __init__(self, c, path):
        self.path = path
        super().__init__(c, "unused")
        self.h.pop("Authorization")

    def rpc(self, method, params, raw=False):
        orig = self.c.post
        self.c.post = lambda url, **kw: orig(self.path if url == "/mcp" else url, **kw)
        try:
            return super().rpc(method, params, raw)
        finally:
            self.c.post = orig


@pytest.fixture()
def app(tmp_path):
    return create_personal_app(BASE, tmp_path / "business.db", KEY, today=TODAY), tmp_path / "business.db"


def test_secret_address_works_and_saves_locally(app):
    a, db = app
    with TestClient(a, base_url=BASE, follow_redirects=False) as c:
        m = PersonalMCP(c, f"/{KEY}/mcp")
        tools = {t["name"] for t in m.rpc("tools/list", {})["result"]["tools"]}
        assert "upload_files" in tools and len(tools) == 32
        m.call("setup_business", SETUP["kine_saone"])
        m.call("capture", {"items": [{"type": "goal", "quote": "10 patients/day", "text": "10 patients/day"}]})
    assert "10 patients/day" in {r["text"] for r in ContextEngine(db, TODAY).conn.execute("SELECT text FROM knowledge")}


def test_nothing_answers_without_the_key(app):
    a, _ = app
    with TestClient(a, base_url=BASE, follow_redirects=False) as c:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        hdr = {"Accept": "application/json, text/event-stream"}
        for path in ("/mcp", f"/{KEY[:-1]}/mcp", f"/{KEY}x/mcp", "/x/mcp"):
            assert c.post(path, json=body, headers=hdr).status_code == 404, path
        assert c.get("/.well-known/oauth-authorization-server").status_code == 404
        assert c.get("/register").status_code in (404, 405)
        home = c.get("/")
        assert home.status_code == 200 and KEY not in home.text and "/mcp" not in home.text


def test_upload_page_in_personal_mode(app):
    a, _ = app
    with TestClient(a, base_url=BASE, follow_redirects=False) as c:
        m = PersonalMCP(c, f"/{KEY}/mcp")
        m.call("setup_business", SETUP["kine_saone"])
        url = m.call("upload_files", {}).split("): ")[1].split()[0]
        assert url.startswith(f"{BASE}/upload?t=") and KEY not in url
        page = c.get(url.replace(BASE, ""))
        assert page.status_code == 200 and "Choisir des fichiers" in page.text
        r = c.post("/upload", data={"t": url.split("t=")[1]}, files=demo_files("kine_saone"))
        assert r.status_code == 200 and "patients.csv" in r.text


def test_weak_key_refused(tmp_path):
    with pytest.raises(ValueError):
        create_personal_app(BASE, tmp_path / "x.db", "short")
