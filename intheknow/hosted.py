"""The hosted connector: one HTTPS server for every owner, holding no business data.

Run:
    ITK_SEAL_KEYS=...  ITK_GOOGLE_CLIENT_ID=...  ITK_GOOGLE_CLIENT_SECRET=... \\
    uv run python -m intheknow.hosted --base-url https://<public-url>

Each request carries a sealed access token. The token says who the owner is and holds their
provider grant; the owner's engine is built from their memory file in their own Google Drive or
OneDrive (cloud.py) and kept in RAM only while they are active.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings

from .auth import IdentityProvider, StatelessAuthProvider, providers_from_env
from . import site
from .cloud import cloud_store_factory
from .context import ContextEngine
from .mcp_server import build_server
from .seal import Sealer
from .logs import configure as configure_logging, log_call, owner_hash
from .store import LocalStore, Store
from .uploads import PickerConfig, Uploads

StoreFactory = Callable[[dict[str, Any]], Store]


class EngineCache:
    """Owner subject -> engine, in RAM, dropped after `idle` seconds without use. Never written to disk."""

    def __init__(self, store_factory: StoreFactory, idle: float = 600, today=None):
        self.store_factory, self.idle, self.today = store_factory, idle, today
        self._items: dict[str, tuple[ContextEngine, float]] = {}
        self._lock = threading.Lock()

    def get(self, token: AccessToken) -> ContextEngine:
        return self.for_identity((token.claims or {})["identity"])

    def for_identity(self, identity: dict[str, Any]) -> ContextEngine:
        key = f"{identity['p']}:{identity['sub']}"
        now = time.monotonic()
        with self._lock:
            for k in [k for k, (_, t) in self._items.items() if now - t > self.idle]:
                del self._items[k]
            hit = self._items.get(key)
            engine = hit[0] if hit else None
        if engine is None:
            engine = ContextEngine(self.store_factory(identity), self.today)
        with self._lock:
            engine = self._items.get(key, (engine, 0))[0]       # another request may have built it first
            self._items[key] = (engine, now)
        return engine

    def __len__(self) -> int:
        return len(self._items)


def dev_store_factory(directory: str | Path) -> StoreFactory:
    """DEVELOPMENT ONLY: one local file per owner on this server's disk. R3 replaces it with the owner's Drive / OneDrive."""
    root = Path(directory).expanduser()

    def make(identity: dict[str, Any]) -> Store:
        name = hashlib.sha256(f"{identity['p']}:{identity['sub']}".encode()).hexdigest()[:24]
        return LocalStore(root / f"{name}.itk")
    return make


def create_app(base_url: str, sealer: Sealer, providers: dict[str, IdentityProvider], store_factory: StoreFactory,
               http_factory=None, today=None, extra_hosts: Optional[list[str]] = None,
               picker: Optional[PickerConfig] = None) -> Starlette:
    base_url = base_url.rstrip("/")
    kw = {"http_factory": http_factory} if http_factory else {}
    auth = StatelessAuthProvider(sealer, base_url, providers, **kw)
    engines = EngineCache(store_factory, today=today)

    uploads = Uploads(sealer, base_url, engines.for_identity, picker)

    def current() -> ContextEngine:
        token = get_access_token()
        if token is None:
            raise PermissionError("not signed in")
        return engines.get(token)

    def upload_link() -> str:
        token = get_access_token()
        if token is None:
            raise PermissionError("not signed in")
        return uploads.link((token.claims or {})["identity"])

    def on_call(tool: str, ms: float, ok: bool) -> None:
        token = get_access_token()
        log_call(tool, ms, ok, owner_hash(token.subject or "") if token else "")

    mcp = build_server(
        current,
        upload_link=upload_link,
        on_call=on_call,
        auth_server_provider=auth,
        auth=AuthSettings(issuer_url=base_url, resource_server_url=auth.resource, validate_token_resource=True,
                          client_registration_options=ClientRegistrationOptions(enabled=True),
                          revocation_options=RevocationOptions(enabled=False)))
    for path, methods, handler in auth.routes() + uploads.routes() + site.routes(base_url):
        mcp.custom_route(path, methods)(handler)

    @mcp.custom_route("/health", ["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True})

    host = urlparse(base_url).netloc
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, "127.0.0.1:*", "localhost:*", *(extra_hosts or [])],
        allowed_origins=[f"{urlparse(base_url).scheme}://{host}", "https://claude.ai", "https://chatgpt.com",
                         "http://127.0.0.1:*", "http://localhost:*"])
    app = mcp.streamable_http_app(transport_security=security, host="0.0.0.0")
    app.state.engines = engines
    return app


def create_personal_app(base_url: str, db: str | Path, key: str, today=None) -> Starlette:
    """Personal mode, for now: one owner, no sign-in, memory in a local file.

    The MCP endpoint is /<key>/mcp: the long random key in the address is the only access control, so the
    address must stay private. No Google/Microsoft accounts, no seal keys to manage (upload links are sealed
    with a key that lives only in RAM and dies with the process).
    """
    if len(key) < 24 or not key.replace("-", "").replace("_", "").isalnum():
        raise ValueError("the personal key must be at least 24 URL-safe characters")
    base_url = base_url.rstrip("/")
    engine = ContextEngine(db, today)
    uploads = Uploads(Sealer([("p1", os.urandom(32))]), base_url, lambda identity: engine)

    def on_call(tool: str, ms: float, ok: bool) -> None:
        log_call(tool, ms, ok, "personal")

    mcp = build_server(lambda: engine, upload_link=lambda: uploads.link({"p": "personal", "sub": "owner"}), on_call=on_call)
    for path, methods, handler in uploads.routes() + site.routes(base_url, show_connector=False):
        mcp.custom_route(path, methods)(handler)

    @mcp.custom_route("/health", ["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True})

    host = urlparse(base_url).netloc
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=[host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[f"{urlparse(base_url).scheme}://{host}", "https://claude.ai", "https://chatgpt.com",
                         "http://127.0.0.1:*", "http://localhost:*"])
    app = mcp.streamable_http_app(streamable_http_path=f"/{key}/mcp", transport_security=security, host="0.0.0.0")
    app.state.engine = engine
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="In The Know hosted connector")
    env = os.environ
    ap.add_argument("--base-url", default=env.get("ITK_BASE_URL"), help="Public https URL of this server (or ITK_BASE_URL)")
    ap.add_argument("--host", default=env.get("ITK_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(env.get("PORT", "8000")))
    ap.add_argument("--personal", action="store_true",
                    help="Personal mode: one owner, no sign-in, memory in --db; the address /<ITK_PERSONAL_KEY>/mcp is the access key")
    ap.add_argument("--db", default=env.get("ITK_DB", "~/InTheKnow/business.db"), help="Personal mode: the memory file")
    ap.add_argument("--dev-store", help="DEVELOPMENT ONLY: keep each owner's memory in this local folder instead of their Drive / OneDrive")
    args = ap.parse_args()
    if not args.base_url:
        raise SystemExit("Set --base-url or ITK_BASE_URL to this server's public https address.")
    configure_logging(os.environ.get("ITK_LOG_LEVEL", "INFO"))
    import uvicorn
    if args.personal:
        key = env.get("ITK_PERSONAL_KEY", "")
        if not key:
            raise SystemExit("Set ITK_PERSONAL_KEY (deploy/personal.sh creates one).")
        db = Path(args.db).expanduser()
        db.parent.mkdir(parents=True, exist_ok=True)
        uvicorn.run(create_personal_app(args.base_url, db, key), host=args.host, port=args.port, proxy_headers=True,
                    forwarded_allow_ips="127.0.0.1", access_log=False, log_config=None)
        return
    providers = providers_from_env(dict(os.environ))
    if not providers:
        raise SystemExit("Set ITK_GOOGLE_CLIENT_ID/SECRET and/or ITK_MICROSOFT_CLIENT_ID/SECRET.")
    stores = dev_store_factory(args.dev_store) if args.dev_store else cloud_store_factory(providers)
    picker = (PickerConfig(os.environ["ITK_GOOGLE_API_KEY"], os.environ["ITK_GOOGLE_APP_ID"])
              if os.environ.get("ITK_GOOGLE_API_KEY") and os.environ.get("ITK_GOOGLE_APP_ID") else None)
    app = create_app(args.base_url, Sealer.from_env(), providers, stores, picker=picker)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*",
                access_log=False, log_config=None)       # no request lines (URLs carry sign-in data); our handlers stay


if __name__ == "__main__":
    main()
