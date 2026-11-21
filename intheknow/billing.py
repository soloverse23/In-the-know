"""Billing for the hosted connector: one plan, a 14-day card trial, and 3-month promo codes.

- Paid plans run through Paddle (merchant of record): €4.99 / month or €49 / year, VAT included.
  Adding a card starts 14 free days (once per owner); Paddle charges after that.
- A promo code gives 3 months free with no card. Codes are single use; only their hashes are stored.
- When nothing is active the memory turns read-only: reading works, saving asks the owner to subscribe.

What is stored (Firestore, EU): per owner a random-looking key (hash of the sign-in subject), the plan
status and end date, whether the trial was used, Paddle's subscription and customer ids, and which code
was redeemed. No name, no email, no business data.

Operator CLI:
    uv run python -m intheknow.billing codes --n 5 --note "Ana salon"    # prints new codes
    uv run python -m intheknow.billing codes --list                      # used / unused
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Protocol

import httpx2 as httpx

from .seal import Sealer, SealError

DAY = 86400
TRIAL_DAYS = 14
PROMO_DAYS = 90
GRACE = 7 * DAY                   # past_due: Paddle retries the card; keep saving meanwhile
CACHE_TTL = 300
ACCOUNT_TTL = 24 * 3600
PLAN_TTL = 30 * 60
SIGNATURE_TOLERANCE = 300
PRICES_EUR = {"monthly": "4.99", "yearly": "49"}
PAID = ("trialing", "active", "past_due")
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"     # no 0/O/1/I


# ---------------------------------------------------------------- owner state
@dataclass
class Owner:
    status: str = "none"          # none | trialing | active | past_due | canceled | paused | promo
    ends_at: float = 0
    had_trial: bool = False
    paddle_sub_id: str = ""
    paddle_customer_id: str = ""
    promo_code_id: str = ""
    event_at: str = ""            # occurred_at of the last Paddle event applied (ignore older ones)
    exists: bool = False

    @classmethod
    def from_doc(cls, doc: Optional[dict]) -> "Owner":
        if not doc:
            return cls()
        known = {k: doc[k] for k in ("status", "ends_at", "had_trial", "paddle_sub_id", "paddle_customer_id",
                                     "promo_code_id", "event_at") if k in doc}
        return cls(**known, exists=True)

    def entitled(self, now: float) -> bool:
        if self.status in ("trialing", "active", "promo"):
            return self.ends_at > now
        if self.status == "past_due":
            return self.ends_at + GRACE > now
        return False

    def days_left(self, now: float) -> int:
        return max(0, int((self.ends_at - now + DAY - 1) // DAY))

    def subscribed(self, now: float) -> bool:
        return bool(self.paddle_sub_id) and self.status in PAID and self.entitled(now)


def owner_key(subject: str) -> str:
    return hashlib.sha256(f"itk-billing:{subject}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------- codes
def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"ITK-{raw[:4]}-{raw[4:]}"


def normalize_code(code: str) -> str:
    """'itk 7f3k-9qpa' -> 'ITK-7F3K-9QPA'; '' when it can't be one of our codes."""
    c = "".join(ch for ch in code.upper() if ch.isalnum()).removeprefix("ITK")
    return f"ITK-{c[:4]}-{c[4:]}" if len(c) == 8 and all(ch in CODE_ALPHABET for ch in c) else ""


def code_id(code: str) -> str:
    return hashlib.sha256(f"itk-code:{normalize_code(code)}".encode()).hexdigest()


# ---------------------------------------------------------------- storage
class BillingStore(Protocol):
    def get_owner(self, key: str) -> Optional[dict]: ...
    def put_owner(self, key: str, fields: dict) -> None: ...
    def add_code(self, cid: str, note: str, now: float) -> None: ...
    def redeem(self, cid: str, key: str, now: float) -> str: ...
    def list_codes(self) -> list[dict]: ...


def _redeem_rules(code: Optional[dict], owner: Owner, key: str, now: float) -> str:
    if code is None:
        return "invalid"
    if code.get("used_by"):
        return "used"
    if owner.promo_code_id:
        return "already"
    if owner.subscribed(now):
        return "subscribed"
    return "ok"


def _promo_fields(cid: str, now: float) -> dict:
    return {"status": "promo", "ends_at": now + PROMO_DAYS * DAY, "promo_code_id": cid, "updated_at": now}


class MemoryBillingStore:
    """For tests and local development. Same rules as Firestore."""

    def __init__(self):
        self.owners: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get_owner(self, key: str) -> Optional[dict]:
        with self._lock:
            d = self.owners.get(key)
            return dict(d) if d else None

    def put_owner(self, key: str, fields: dict) -> None:
        with self._lock:
            self.owners.setdefault(key, {}).update(fields)

    def add_code(self, cid: str, note: str, now: float) -> None:
        with self._lock:
            self.codes[cid] = {"created_at": now, "note": note, "used_by": None, "used_at": None}

    def redeem(self, cid: str, key: str, now: float) -> str:
        with self._lock:
            code = self.codes.get(cid)
            result = _redeem_rules(code, Owner.from_doc(self.owners.get(key)), key, now)
            if result == "ok":
                code.update(used_by=key, used_at=now)
                self.owners.setdefault(key, {}).update(_promo_fields(cid, now))
            return result

    def list_codes(self) -> list[dict]:
        with self._lock:
            return [dict(v, id=k) for k, v in self.codes.items()]


class FirestoreBillingStore:
    """Two small collections: owners/{key} and codes/{sha256}. Credentials come from the Cloud Run service account."""

    def __init__(self, project: Optional[str] = None, database: str = "(default)"):
        from google.cloud import firestore           # imported here: only the hosted server needs it
        self._fs = firestore
        self.db = firestore.Client(project=project, database=database)

    def get_owner(self, key: str) -> Optional[dict]:
        snap = self.db.collection("owners").document(key).get()
        return snap.to_dict() if snap.exists else None

    def put_owner(self, key: str, fields: dict) -> None:
        self.db.collection("owners").document(key).set(fields, merge=True)

    def add_code(self, cid: str, note: str, now: float) -> None:
        self.db.collection("codes").document(cid).create({"created_at": now, "note": note, "used_by": None, "used_at": None})

    def redeem(self, cid: str, key: str, now: float) -> str:
        code_ref = self.db.collection("codes").document(cid)
        owner_ref = self.db.collection("owners").document(key)

        @self._fs.transactional
        def run(tx) -> str:
            code = code_ref.get(transaction=tx)
            owner = owner_ref.get(transaction=tx)
            result = _redeem_rules(code.to_dict() if code.exists else None,
                                   Owner.from_doc(owner.to_dict() if owner.exists else None), key, now)
            if result == "ok":
                tx.update(code_ref, {"used_by": key, "used_at": now})
                tx.set(owner_ref, _promo_fields(cid, now), merge=True)
            return result
        return run(self.db.transaction())

    def list_codes(self) -> list[dict]:
        return [dict(d.to_dict(), id=d.id) for d in self.db.collection("codes").stream()]


# ---------------------------------------------------------------- Paddle
@dataclass(frozen=True)
class PaddleConfig:
    env: str                      # sandbox | production
    client_token: str             # public, used by Paddle.js on the plan page
    api_key: str                  # secret, server only (customer portal links)
    webhook_secret: str           # secret, verifies webhooks
    prices: dict[str, str]        # monthly, yearly, monthly_notrial, yearly_notrial -> Paddle price ids

    @property
    def api_base(self) -> str:
        return "https://sandbox-api.paddle.com" if self.env == "sandbox" else "https://api.paddle.com"

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "PaddleConfig":
        need = ["ITK_PADDLE_CLIENT_TOKEN", "ITK_PADDLE_API_KEY", "ITK_PADDLE_WEBHOOK_SECRET",
                "ITK_PADDLE_PRICE_MONTHLY", "ITK_PADDLE_PRICE_YEARLY"]
        missing = [n for n in need if not env.get(n)]
        if missing:
            raise SystemExit("Billing is on but these are missing: " + ", ".join(missing))
        prices = {"monthly": env["ITK_PADDLE_PRICE_MONTHLY"], "yearly": env["ITK_PADDLE_PRICE_YEARLY"]}
        prices["monthly_notrial"] = env.get("ITK_PADDLE_PRICE_MONTHLY_NOTRIAL") or prices["monthly"]
        prices["yearly_notrial"] = env.get("ITK_PADDLE_PRICE_YEARLY_NOTRIAL") or prices["yearly"]
        return cls(env.get("ITK_PADDLE_ENV", "sandbox"), env["ITK_PADDLE_CLIENT_TOKEN"], env["ITK_PADDLE_API_KEY"],
                   env["ITK_PADDLE_WEBHOOK_SECRET"], prices)


def verify_signature(secret: str, header: str, raw: bytes, now: float) -> bool:
    """Paddle-Signature: ts=<unix>;h1=<hex hmac-sha256 of "ts:body">."""
    pairs = [p.strip().split("=", 1) for p in header.split(";") if "=" in p]
    ts = next((v for k, v in pairs if k == "ts"), "")
    sigs = [v for k, v in pairs if k == "h1"]
    if not ts.isdigit() or not sigs or abs(now - int(ts)) > SIGNATURE_TOLERANCE:
        return False
    want = hmac.new(secret.encode(), f"{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(want, s) for s in sigs)


def _ts(iso: Optional[str]) -> float:
    if not iso:
        return 0
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------- billing service
@dataclass
class Billing:
    store: BillingStore
    sealer: Sealer
    base_url: str
    paddle: Optional[PaddleConfig] = None
    now: Callable[[], float] = time.time
    _cache: dict[str, tuple[Owner, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- state
    def owner(self, key: str, fresh: bool = False) -> Owner:
        t = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
        if hit and not fresh and t - hit[1] < CACHE_TTL:
            return hit[0]
        o = Owner.from_doc(self.store.get_owner(key))
        with self._lock:
            self._cache[key] = (o, t)
        return o

    def forget(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def needs_plan(self, subject: str) -> bool:
        """New owners choose a plan before the connection finishes; known owners (even expired) pass."""
        return not self.owner(owner_key(subject), fresh=True).exists

    # ---- links
    def account_link(self, subject: str, email: str = "") -> str:
        t = self.sealer.seal("account", {"k": owner_key(subject), "e": email}, ACCOUNT_TTL)
        return f"{self.base_url.rstrip('/')}/account?t={t}"

    def owner_token(self, key: str) -> str:
        """Sealed owner id sent through Paddle checkout as custom data, so webhooks can't be pointed at another owner."""
        return self.sealer.seal("billing-owner", {"k": key}, None)

    # ---- the read-only gate
    def guard(self, subject: str, email: str = "") -> Optional[str]:
        o = self.owner(owner_key(subject))
        if o.entitled(self.now()):
            return None
        link = self.account_link(subject, email)
        if o.status == "promo":
            head = "The 3 free months have ended."
        elif o.status in ("trialing",):
            head = "The 14-day free trial has ended."
        elif o.exists:
            head = "The In The Know subscription is not active."
        else:
            head = "In The Know needs a plan to save new things."
        return (f"Not saved. {head} Everything already in the memory can still be read and shown. "
                f"To save again: 14 days free with a card, then €4.99 a month or €49 a year, or enter a code. "
                f"Show the owner this link: {link}")

    def notice(self, subject: str, email: str = "") -> Optional[str]:
        """A heads-up when the 3 free months end soon (a card trial turns into a paid plan by itself)."""
        o = self.owner(owner_key(subject))
        now = self.now()
        if o.status == "promo" and o.entitled(now) and o.days_left(now) <= 3:
            return (f"Note for the owner: the 3 free months end in {o.days_left(now)} day(s). "
                    f"To keep saving, choose a plan: {self.account_link(subject, email)}")
        return None

    # ---- codes
    def redeem(self, key: str, code: str) -> str:
        if not normalize_code(code):
            return "invalid"
        result = self.store.redeem(code_id(code), key, self.now())
        self.forget(key)
        return result

    def create_codes(self, n: int, note: str = "") -> list[str]:
        out = []
        for _ in range(n):
            c = new_code()
            self.store.add_code(code_id(c), note, self.now())
            out.append(c)
        return out

    # ---- Paddle
    def price_for(self, key: str, period: str) -> str:
        assert self.paddle is not None
        period = "yearly" if period == "yearly" else "monthly"
        had = self.owner(key, fresh=True).had_trial
        return self.paddle.prices[f"{period}_notrial" if had else period]

    def apply_webhook(self, raw: bytes, signature: str) -> bool:
        """Verify and apply one Paddle event. Returns False when the signature is wrong."""
        if self.paddle is None or not verify_signature(self.paddle.webhook_secret, signature, raw, self.now()):
            return False
        ev = json.loads(raw)
        kind = ev.get("event_type", "")
        if not kind.startswith("subscription."):
            return True
        data = ev.get("data") or {}
        try:
            key = self.sealer.open("billing-owner", (data.get("custom_data") or {}).get("o", ""))["k"]
        except SealError:
            return True                                   # not ours (or tampered): ignore, don't retry
        occurred = ev.get("occurred_at", "")
        current = Owner.from_doc(self.store.get_owner(key))
        if current.event_at and occurred and occurred < current.event_at:
            return True                                   # out of order: a newer event was already applied
        status = data.get("status", "")
        period = data.get("current_billing_period") or {}
        ends = _ts(period.get("ends_at")) or _ts(data.get("next_billed_at"))
        if status == "canceled":
            ends = min(ends or self.now(), _ts(data.get("canceled_at")) or self.now())
        fields = {"status": status, "ends_at": ends, "paddle_sub_id": data.get("id", ""),
                  "paddle_customer_id": data.get("customer_id", ""), "event_at": occurred, "updated_at": self.now()}
        if status == "trialing" or current.had_trial:
            fields["had_trial"] = True
        if current.status == "promo" and current.entitled(self.now()) and status in ("trialing", "active"):
            fields["ends_at"] = max(ends, current.ends_at)  # paying during a promo never shortens it
        self.store.put_owner(key, fields)
        self.forget(key)
        return True

    async def portal_url(self, key: str, http: Optional[httpx.AsyncClient] = None) -> Optional[str]:
        o = self.owner(key, fresh=True)
        if self.paddle is None or not o.paddle_customer_id:
            return None
        body = {"subscription_ids": [o.paddle_sub_id]} if o.paddle_sub_id else {}
        client = http or httpx.AsyncClient(timeout=15)
        try:
            r = await client.post(f"{self.paddle.api_base}/customers/{o.paddle_customer_id}/portal-sessions", json=body,
                                  headers={"Authorization": f"Bearer {self.paddle.api_key}"})
            if r.status_code >= 400:
                return None
            return ((r.json().get("data") or {}).get("urls") or {}).get("general", {}).get("overview")
        finally:
            if http is None:
                await client.aclose()


def from_env(env: dict[str, str], sealer: Sealer, base_url: str) -> Optional[Billing]:
    if env.get("ITK_BILLING", "").lower() not in ("on", "1", "true", "yes"):
        return None
    store = FirestoreBillingStore(env.get("ITK_FIRESTORE_PROJECT") or None, env.get("ITK_FIRESTORE_DATABASE", "(default)"))
    return Billing(store, sealer, base_url, PaddleConfig.from_env(env))


# ---------------------------------------------------------------- CLI
def main() -> None:
    import os
    ap = argparse.ArgumentParser(description="In The Know billing: promo codes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("codes", help="Create or list 3-month promo codes")
    c.add_argument("--n", type=int, default=1, help="How many codes to create")
    c.add_argument("--note", default="", help="Who the codes are for (only you see this)")
    c.add_argument("--list", action="store_true", help="List codes instead of creating")
    args = ap.parse_args()
    store = FirestoreBillingStore(os.environ.get("ITK_FIRESTORE_PROJECT") or None,
                                  os.environ.get("ITK_FIRESTORE_DATABASE", "(default)"))
    if args.list:
        rows = sorted(store.list_codes(), key=lambda r: r.get("created_at") or 0)
        used = sum(1 for r in rows if r.get("used_by"))
        for r in rows:
            when = time.strftime("%Y-%m-%d", time.localtime(r["created_at"])) if r.get("created_at") else "?"
            print(f"{when}  {'used  ' if r.get('used_by') else 'unused'}  {r.get('note') or ''}")
        print(f"{len(rows)} codes, {used} used, {len(rows) - used} unused")
        return
    billing = Billing(store, Sealer([("cli", secrets.token_bytes(32))]), "")
    for code in billing.create_codes(max(1, min(args.n, 500)), args.note):
        print(code)
    print(f"Each code gives 3 months free, once. Only a hash is stored: save these codes now.")


if __name__ == "__main__":
    main()
