"""Hosted connector: stateless sign-in (fake Google) and MCP over HTTP, end to end."""
import base64
import hashlib
import json
import secrets
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx2 as httpx
import pytest
from starlette.testclient import TestClient

from conftest import SETUP, TODAY
from intheknow.auth import google
from intheknow.hosted import create_app, dev_store_factory
from intheknow.seal import Sealer, SealError, new_key, _b64d

BASE = "https://itk.test"
REDIRECT = "https://claude.example/api/mcp/auth_callback"


class FakeGoogle:
    def __init__(self):
        self.revoked: set[str] = set()
        self.verifiers: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        f = {k: v[0] for k, v in form.items()}
        if f["grant_type"] == "authorization_code":
            sub = f["code"].removeprefix("good-")
            self.verifiers.append(f.get("code_verifier", ""))
            claims = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=").decode()
            return httpx.Response(200, json={"access_token": "g-at", "refresh_token": f"rt-{sub}", "id_token": f"x.{claims}.y"})
        if f["refresh_token"] in self.revoked:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": "g-at2", "expires_in": 3600})


@pytest.fixture()
def env(tmp_path):
    g = FakeGoogle()
    sealer = Sealer([("k1", _b64d(new_key()))])
    prov = {"google": replace(google("cid", "csecret"), authorize_url="https://accounts.google.test/auth",
                              token_url="https://oauth2.google.test/token")}
    app = create_app(BASE, sealer, prov, dev_store_factory(tmp_path / "mem"), today=TODAY,
                     http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(g.handler)))
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        yield c, g, sealer, tmp_path


def register(c) -> dict:
    r = c.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude", "grant_types": ["authorization_code", "refresh_token"],
                                  "response_types": ["code"], "token_endpoint_auth_method": "none"})
    assert r.status_code == 201, r.text
    return r.json()


def sign_in(c, g, client: dict, sub: str = "owner-1") -> dict:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = c.get("/authorize", params={"response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT,
                                    "code_challenge": challenge, "code_challenge_method": "S256", "state": "s-123",
                                    "resource": f"{BASE}/mcp"})
    assert r.status_code == 302, r.text
    login = r.headers["location"]
    assert login.startswith(f"{BASE}/login?req=")
    page = c.get(login, headers={"Accept-Language": "ro-RO,ro;q=0.9"})
    assert page.status_code == 200 and "Continuă cu Google" in page.text and "X-Frame-Options" in page.headers
    start = c.get(login.replace("/login?", "/login/google?"))
    q = parse_qs(urlparse(start.headers["location"]).query)
    assert start.headers["location"].startswith("https://accounts.google.test/auth")
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"] and q["redirect_uri"] == [f"{BASE}/callback/google"]
    back = c.get("/callback/google", params={"code": f"good-{sub}", "state": q["state"][0]})
    assert back.status_code == 302
    cb = urlparse(back.headers["location"])
    assert f"{cb.scheme}://{cb.netloc}{cb.path}" == REDIRECT
    cq = parse_qs(cb.query)
    assert cq["state"] == ["s-123"]
    assert g.verifiers[-1]                                       # PKCE towards Google too
    tok = c.post("/token", data={"grant_type": "authorization_code", "code": cq["code"][0], "redirect_uri": REDIRECT,
                                 "client_id": client["client_id"], "code_verifier": verifier, "resource": f"{BASE}/mcp"})
    assert tok.status_code == 200, tok.text
    return {**tok.json(), "code": cq["code"][0], "verifier": verifier}


class MCP:
    def __init__(self, c, token: str):
        self.c, self.h = c, {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, raw=True)
        if sid := r.headers.get("mcp-session-id"):
            self.h["mcp-session-id"] = sid
        self.c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=self.h)

    def rpc(self, method: str, params: dict, raw: bool = False):
        r = self.c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, headers=self.h)
        if raw:
            return r
        assert r.status_code == 200, r.text
        if r.headers["content-type"].startswith("text/event-stream"):
            msgs = [json.loads(l[5:]) for l in r.text.splitlines() if l.startswith("data:")]
            return [m for m in msgs if "id" in m][-1]
        return r.json()

    def call(self, tool: str, args: dict) -> str:
        res = self.rpc("tools/call", {"name": tool, "arguments": args})["result"]
        assert not res.get("isError"), res
        return res["content"][0]["text"]


def test_metadata_and_unauthenticated(env):
    c, *_ = env
    prm = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == f"{BASE}/mcp" and prm["authorization_servers"][0].rstrip("/") == BASE
    asm = c.get("/.well-known/oauth-authorization-server").json()
    assert asm["registration_endpoint"] == f"{BASE}/register" and "S256" in asm["code_challenge_methods_supported"]
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", "")
    assert c.get("/health").json() == {"ok": True}


def test_full_flow_and_owner_isolation(env):
    c, g, _, tmp = env
    client = register(c)
    t1 = sign_in(c, g, client, "owner-1")
    m1 = MCP(c, t1["access_token"])
    tools = m1.rpc("tools/list", {})["result"]["tools"]
    assert len(tools) == 31
    m1.call("setup_business", SETUP["salon_aurora"])
    m1.call("capture", {"items": [{"type": "goal", "quote": "40k by Christmas", "text": "40k by Christmas"}]})
    assert "Salon Aurora" in m1.call("get_context", {})
    t2 = sign_in(c, g, client, "owner-2")
    m2 = MCP(c, t2["access_token"])
    assert "Salon Aurora" not in m2.call("get_context", {})      # another owner sees nothing of owner 1
    assert len(list((tmp / "mem").glob("*.itk"))) == 1           # owner 2 has written nothing yet


def test_codes_are_single_use_and_bound(env):
    c, g, _, _ = env
    client = register(c)
    t = sign_in(c, g, client)
    again = c.post("/token", data={"grant_type": "authorization_code", "code": t["code"], "redirect_uri": REDIRECT,
                                   "client_id": client["client_id"], "code_verifier": t["verifier"]})
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    other = register(c)
    r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": other["client_id"]})
    assert r.status_code == 400                                  # a token is bound to the client it was issued to


def test_refresh_and_revocation(env):
    c, g, _, _ = env
    client = register(c)
    t = sign_in(c, g, client)
    r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": client["client_id"]})
    assert r.status_code == 200 and r.json()["access_token"] != t["access_token"]
    MCP(c, r.json()["access_token"]).call("get_context", {})
    g.revoked.add("rt-owner-1")                                  # owner removed access at Google
    r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": client["client_id"]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_forged_and_misused_tokens(env):
    c, g, sealer, _ = env
    client = register(c)
    t = sign_in(c, g, client)
    for bad in (t["refresh_token"], t["access_token"][:-4] + "AAAA", "itk1.k9.xyz", "nonsense"):
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Authorization": f"Bearer {bad}", "Accept": "application/json, text/event-stream"})
        assert r.status_code == 401
    assert c.get("/login", params={"req": "forged"}).status_code == 400
    assert c.get("/callback/google", params={"code": "good-x", "state": "forged"}).status_code == 400
    cancelled = sign_in_cancel(c, client)
    assert parse_qs(urlparse(cancelled).query)["error"] == ["access_denied"]


def sign_in_cancel(c, client) -> str:
    r = c.get("/authorize", params={"response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT,
                                    "code_challenge": "x" * 43, "code_challenge_method": "S256", "state": "s"})
    start = c.get(r.headers["location"].replace("/login?", "/login/google?"))
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return c.get("/callback/google", params={"error": "access_denied", "state": state}).headers["location"]


def test_sealer():
    a = Sealer([("k1", _b64d(new_key()))])
    tok = a.seal("access", {"x": 1}, 60)
    assert a.open("access", tok)["x"] == 1
    with pytest.raises(SealError):
        a.open("refresh", tok)                                   # wrong purpose
    with pytest.raises(SealError):
        a.open("access", a.seal("access", {}, -1))               # expired
    rotated = Sealer([("k2", _b64d(new_key())), ("k1", a._keys and _b64d(new_key()))])
    with pytest.raises(SealError):
        rotated.open("access", tok)                              # same kid, different key material


def test_hosted_with_owner_drive(tmp_path):
    from clouds import FakeCloud
    from intheknow.cloud import cloud_store_factory
    g, cloud = FakeGoogle(), FakeCloud()
    sealer = Sealer([("k1", _b64d(new_key()))])
    prov = {"google": replace(google("cid", "csecret"), authorize_url="https://accounts.google.test/auth",
                              token_url="https://oauth2.google.test/token")}
    app = create_app(BASE, sealer, prov, cloud_store_factory(prov, cloud.client()), today=TODAY,
                     http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(g.handler)))
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        client = register(c)
        phone = MCP(c, sign_in(c, g, client, "owner-1")["access_token"])
        assert "Salon Aurora" not in phone.call("get_context", {})
        assert cloud.drive_memory() == []                            # connecting creates nothing in Drive
        phone.call("setup_business", SETUP["salon_aurora"])
        phone.call("capture", {"items": [{"type": "goal", "quote": "two chairs", "text": "two chairs"}]})
        desktop = MCP(c, sign_in(c, g, register(c), "owner-1")["access_token"])   # same owner, another app
        assert "Salon Aurora" in desktop.call("get_context", {})
        assert len(app.state.engines) == 1                           # one engine per owner: their writes are serialized
        [f] = cloud.drive_memory()
        assert cloud.drive[f["parents"][0]]["name"] == "In The Know"
    assert not list(tmp_path.iterdir())                              # nothing written on the server's disk
