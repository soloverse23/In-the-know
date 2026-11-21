"""Billing: promo codes, entitlement, Paddle webhooks, the plan step while connecting, and the read-only gate."""
import base64
import hashlib
import hmac
import html
import json
import secrets
import threading
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx2 as httpx
import pytest
from starlette.testclient import TestClient

from conftest import SETUP, TODAY
from test_hosted import BASE, MCP, REDIRECT, FakeGoogle, register
from intheknow.auth import google
from intheknow.billing import (DAY, GRACE, PROMO_DAYS, Billing, MemoryBillingStore, Owner, PaddleConfig, code_id,
                               normalize_code, owner_key, verify_signature)
from intheknow.hosted import create_app, dev_store_factory
from intheknow.seal import Sealer, new_key, _b64d

SECRET = "whsec-test"
PADDLE = PaddleConfig("sandbox", "test_client_token", "api-key", SECRET,
                      {"monthly": "pri_m", "yearly": "pri_y", "monthly_notrial": "pri_m0", "yearly_notrial": "pri_y0"})


class Clock:
    def __init__(self):
        self.t = 1_795_000_000.0          # 2026-11-18

    def __call__(self) -> float:
        return self.t


def make_billing(clock=None) -> Billing:
    return Billing(MemoryBillingStore(), Sealer([("k1", _b64d(new_key()))]), BASE, PADDLE, now=clock or Clock())


def signed(body: dict, now: float, secret: str = SECRET) -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    ts = str(int(now))
    return raw, f"ts={ts};h1=" + hmac.new(secret.encode(), f"{ts}:".encode() + raw, hashlib.sha256).hexdigest()


def sub_event(billing: Billing, key: str, status: str, ends: str = "2027-01-01T00:00:00Z", occurred: str = "2026-11-21T10:00:00Z") -> dict:
    return {"event_type": f"subscription.{'created' if status == 'trialing' else 'updated'}", "occurred_at": occurred,
            "data": {"id": "sub_1", "customer_id": "ctm_1", "status": status, "custom_data": {"o": billing.owner_token(key)},
                     "current_billing_period": {"starts_at": "2026-11-21T00:00:00Z", "ends_at": ends}}}


# ---------------------------------------------------------------- codes and entitlement
def test_code_format_and_normalizing():
    b = make_billing()
    [code] = b.create_codes(1)
    assert code.startswith("ITK-") and len(code) == 13
    assert normalize_code(code.lower().replace("-", " ")) == code
    assert code_id(code.lower()) == code_id(code)
    assert normalize_code("ITK-0000-1111") == ""                 # 0 and 1 are never in codes
    assert code_id(code) in b.store.codes and code not in json.dumps(b.store.codes)   # only hashes are stored


def test_redeem_rules():
    clock = Clock()
    b = make_billing(clock)
    c1, c2 = b.create_codes(2)
    assert b.redeem("owner-a", "ITK-ZZZZ-ZZZZ") == "invalid"
    assert b.redeem("owner-a", "nonsense") == "invalid"
    assert b.redeem("owner-a", c1) == "ok"
    o = b.owner("owner-a")
    assert o.status == "promo" and o.ends_at == clock.t + PROMO_DAYS * DAY and o.entitled(clock.t)
    assert b.redeem("owner-b", c1) == "used"                     # single use
    assert b.redeem("owner-a", c2) == "already"                  # one free code per owner
    b.store.put_owner("owner-c", {"status": "active", "ends_at": clock.t + 30 * DAY, "paddle_sub_id": "sub_9"})
    assert b.redeem("owner-c", c2) == "subscribed"
    clock.t += PROMO_DAYS * DAY + 1
    assert not b.owner("owner-a", fresh=True).entitled(clock.t)


def test_redeem_race_one_winner():
    b = make_billing()
    [code] = b.create_codes(1)
    results: list[str] = []
    threads = [threading.Thread(target=lambda i=i: results.append(b.redeem(f"o{i}", code))) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1 and results.count("used") == 19


def test_entitlement_windows():
    now = 1_800_000_000.0
    assert Owner("trialing", now + 10).entitled(now)
    assert not Owner("trialing", now - 10).entitled(now)
    assert Owner("past_due", now - GRACE + 60).entitled(now)       # grace while Paddle retries the card
    assert not Owner("past_due", now - GRACE - 60).entitled(now)
    assert not Owner("canceled", now + 1000).entitled(now)
    assert not Owner().entitled(now)
    assert Owner("promo", now + 2.5 * DAY).days_left(now) == 3


# ---------------------------------------------------------------- webhooks
def test_signature():
    raw, sig = signed({"a": 1}, 1000)
    assert verify_signature(SECRET, sig, raw, 1000)
    assert not verify_signature(SECRET, sig, raw + b" ", 1000)
    assert not verify_signature("other", sig, raw, 1000)
    assert not verify_signature(SECRET, sig, raw, 1000 + 301)       # too old: replay window closed
    assert not verify_signature(SECRET, "garbage", raw, 1000)


def test_webhook_applies_in_order_and_only_for_our_owners():
    clock = Clock()
    b = make_billing(clock)
    key = owner_key("google:x")
    assert b.apply_webhook(*signed(sub_event(b, key, "trialing"), clock.t))
    o = b.owner(key, fresh=True)
    assert o.status == "trialing" and o.had_trial and o.paddle_customer_id == "ctm_1" and o.entitled(clock.t)
    assert b.apply_webhook(*signed(sub_event(b, key, "active", occurred="2026-12-05T00:00:00Z"), clock.t))
    assert b.owner(key, fresh=True).status == "active"
    assert b.apply_webhook(*signed(sub_event(b, key, "past_due", occurred="2026-11-30T00:00:00Z"), clock.t))
    assert b.owner(key, fresh=True).status == "active"              # older event arrived late: ignored
    raw, sig = signed(sub_event(b, key, "canceled", occurred="2026-12-31T00:00:00Z"), clock.t)
    assert not b.apply_webhook(raw, sig.replace("h1=", "h1=0"))      # bad signature
    forged = sub_event(b, key, "canceled", occurred="2026-12-31T00:00:00Z")
    forged["data"]["custom_data"] = {"o": "itk1.k1.forged"}
    assert b.apply_webhook(*signed(forged, clock.t))                # accepted (no retry) but ignored
    assert b.owner(key, fresh=True).status == "active"
    assert b.apply_webhook(*signed({"event_type": "transaction.completed", "data": {}}, clock.t))


def test_second_card_has_no_new_trial():
    b = make_billing()
    key = owner_key("google:y")
    assert b.price_for(key, "yearly") == "pri_y"
    b.apply_webhook(*signed(sub_event(b, key, "trialing"), b.now()))
    assert b.price_for(key, "yearly") == "pri_y0" and b.price_for(key, "monthly") == "pri_m0"


# ---------------------------------------------------------------- hosted: connecting, plan page, gate
@pytest.fixture()
def env(tmp_path):
    g = FakeGoogle()
    clock = Clock()
    sealer = Sealer([("k1", _b64d(new_key()))])
    billing = Billing(MemoryBillingStore(), sealer, BASE, PADDLE, now=clock)
    prov = {"google": replace(google("cid", "csecret"), authorize_url="https://accounts.google.test/auth",
                              token_url="https://oauth2.google.test/token")}
    app = create_app(BASE, sealer, prov, dev_store_factory(tmp_path / "mem"), today=TODAY, billing=billing,
                     http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(g.handler)))
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        yield c, g, billing, clock


def callback(c, client: dict, sub: str):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = c.get("/authorize", params={"response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT,
                                    "code_challenge": challenge, "code_challenge_method": "S256", "state": "s-1",
                                    "resource": f"{BASE}/mcp"})
    start = c.get(r.headers["location"].replace("/login?", "/login/google?"))
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return c.get("/callback/google", params={"code": f"good-{sub}", "state": state}), verifier


def token(c, client: dict, location: str, verifier: str) -> str:
    cb = urlparse(location)
    assert f"{cb.scheme}://{cb.netloc}{cb.path}" == REDIRECT
    code = parse_qs(cb.query)["code"][0]
    tok = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                 "client_id": client["client_id"], "code_verifier": verifier, "resource": f"{BASE}/mcp"})
    assert tok.status_code == 200, tok.text
    return tok.json()["access_token"]


def test_new_owner_picks_plan_with_code(env):
    c, g, billing, _ = env
    client = register(c)
    [code] = billing.create_codes(1)
    back, verifier = callback(c, client, "owner-1")
    assert back.status_code == 302 and back.headers["location"].startswith(f"{BASE}/plan?s=")
    page = c.get(back.headers["location"], headers={"Accept-Language": "ro"})
    assert page.status_code == 200 and "€49" in page.text and "€4.99" in page.text and "cdn.paddle.com" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    blob = parse_qs(urlparse(back.headers["location"]).query)["s"][0]
    bad = c.post("/plan/code", data={"s": blob, "code": "ITK-ZZZZ-ZZZZ"})
    assert bad.status_code == 400 and "doesn't exist" in html.unescape(bad.text)
    ok = c.post("/plan/code", data={"s": blob, "code": code.lower()})
    assert ok.status_code == 303
    m = MCP(c, token(c, client, ok.headers["location"], verifier))
    m.call("setup_business", SETUP["salon_aurora"])
    m.call("capture", {"items": [{"type": "goal", "quote": "40k by Christmas", "text": "40k by Christmas"}]})
    again, _ = callback(c, client, "owner-1")                       # known owner: straight back to Claude
    assert again.headers["location"].startswith(REDIRECT)


def test_new_owner_starts_trial_via_paddle(env):
    c, g, billing, clock = env
    client = register(c)
    back, verifier = callback(c, client, "owner-2")
    blob = parse_qs(urlparse(back.headers["location"]).query)["s"][0]
    assert c.get("/plan/status", params={"s": blob}).json() == {"ready": False}
    early = c.get("/plan/continue", params={"s": blob})
    assert early.status_code == 200 and "Choose your plan" in early.text   # not paid yet: no connection
    key = owner_key("google:owner-2")
    raw, sig = signed(sub_event(billing, key, "trialing"), clock.t)
    assert c.post("/billing/paddle", content=raw, headers={"Paddle-Signature": sig}).status_code == 200
    assert c.post("/billing/paddle", content=raw, headers={"Paddle-Signature": "ts=1;h1=00"}).status_code == 401
    assert c.get("/plan/status", params={"s": blob}).json() == {"ready": True}
    done = c.get("/plan/continue", params={"s": blob})
    assert done.status_code == 302
    m = MCP(c, token(c, client, done.headers["location"], verifier))
    m.call("setup_business", SETUP["kaffeehaus"])


def test_read_only_after_free_period(env):
    c, g, billing, clock = env
    client = register(c)
    [code] = billing.create_codes(1)
    back, verifier = callback(c, client, "owner-3")
    blob = parse_qs(urlparse(back.headers["location"]).query)["s"][0]
    m = MCP(c, token(c, client, c.post("/plan/code", data={"s": blob, "code": code}).headers["location"], verifier))
    m.call("setup_business", SETUP["kine_saone"])
    clock.t += (PROMO_DAYS - 2) * DAY
    billing.forget(owner_key("google:owner-3"))
    assert "end in 2 day(s)" in m.call("get_data_status", {})
    clock.t += 3 * DAY
    billing.forget(owner_key("google:owner-3"))
    blocked = m.call("capture", {"items": [{"type": "goal", "quote": "more patients", "text": "more patients"}]})
    assert blocked.startswith("Not saved.") and "/account?t=" in blocked
    assert "more patients" not in m.call("search_knowledge", {"query": "patients"})
    assert "Kiné" in m.call("get_context", {})                      # reading still works
    assert m.call("capture", {"items": [{"type": "goal", "quote": "x", "text": "x"}], "dry_run": True})  # preview allowed
    link = blocked.split("link: ")[1].strip()
    acc = c.get(link.removeprefix(BASE))
    assert acc.status_code == 200 and "No active plan" in acc.text and "€49" in acc.text
    t = parse_qs(urlparse(link).query)["t"][0]
    [code2] = billing.create_codes(1)
    assert "already used a free code" in c.post("/account/code", data={"t": t, "code": code2}).text
    raw, sig = signed(sub_event(billing, owner_key("google:owner-3"), "active", ends="2027-12-01T00:00:00Z"), clock.t)
    c.post("/billing/paddle", content=raw, headers={"Paddle-Signature": sig})
    assert m.call("capture", {"items": [{"type": "goal", "quote": "more patients", "text": "more patients"}]}).startswith("Not saved.") is False
    assert "Paid plan" in c.get(link.removeprefix(BASE)).text and "Manage or cancel" in c.get(link.removeprefix(BASE)).text


def test_expired_links(env):
    c, *_ = env
    assert c.get("/plan", params={"s": "itk1.k1.nope"}).status_code == 400
    assert c.get("/account", params={"t": "itk1.k1.nope"}).status_code == 400
    assert c.get("/plan/status", params={"s": "x"}).json()["ready"] is False
