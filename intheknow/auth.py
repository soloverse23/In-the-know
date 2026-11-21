"""Sign-in for the hosted connector, with nothing stored on our side.

Claude / ChatGPT  ──OAuth 2.1 + PKCE──▶  this server  ──OAuth + PKCE──▶  Google / Microsoft

- Client registration (RFC 7591): the client's metadata is sealed into its own client_id.
- /authorize sends the owner to our one-screen sign-in page ("Continue with Google / Microsoft").
- The provider grants access to files this app creates (Drive `drive.file`, OneDrive app folder)
  and returns a refresh token. That token is sealed into the authorization code, then into the
  access and refresh tokens we issue. The server can open them; nobody else can.
- Every token is sealed for one purpose and bound to one client (hash of its client_id).

The only server-side state is an in-memory set of used authorization codes (single use, 2 minutes).
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import anyio.to_thread
import httpx2 as httpx
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken,
                                      TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .seal import Sealer, SealError

ACCESS_TTL = 3600
REFRESH_TTL = 90 * 86400
CODE_TTL = 120
LOGIN_TTL = 15 * 60


# ---------------------------------------------------------------- identity providers
@dataclass(frozen=True)
class IdentityProvider:
    name: str                      # google | microsoft
    label: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_id: str
    client_secret: str
    authorize_extra: dict[str, str] = field(default_factory=dict)
    send_scope_on_token: bool = False


def google(client_id: str, client_secret: str) -> IdentityProvider:
    return IdentityProvider(
        "google", "Google", "https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token",
        ("openid", "email", "https://www.googleapis.com/auth/drive.file"), client_id, client_secret,
        # offline + consent: Google returns a refresh token every time, also when the owner reconnects
        {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"})


def microsoft(client_id: str, client_secret: str, tenant: str = "common") -> IdentityProvider:
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    return IdentityProvider(
        "microsoft", "Microsoft", f"{base}/authorize", f"{base}/token",
        ("openid", "email", "offline_access", "https://graph.microsoft.com/Files.ReadWrite.AppFolder"),
        client_id, client_secret, {"prompt": "select_account"}, send_scope_on_token=True)


def providers_from_env(env: dict[str, str]) -> dict[str, IdentityProvider]:
    out = {}
    if env.get("ITK_GOOGLE_CLIENT_ID"):
        out["google"] = google(env["ITK_GOOGLE_CLIENT_ID"], env.get("ITK_GOOGLE_CLIENT_SECRET", ""))
    if env.get("ITK_MICROSOFT_CLIENT_ID"):
        out["microsoft"] = microsoft(env["ITK_MICROSOFT_CLIENT_ID"], env.get("ITK_MICROSOFT_CLIENT_SECRET", ""),
                                     env.get("ITK_MICROSOFT_TENANT", "common"))
    return out


class ProviderRefused(Exception):
    """The provider no longer accepts the owner's grant (access removed, password changed, expired)."""


def _id_claims(id_token: str) -> dict[str, Any]:
    # Received straight from the provider's token endpoint over TLS, so the signature check may be
    # skipped (OpenID Connect Core 3.1.3.7). We read `sub`, and the email to prefill checkout.
    payload = id_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


async def provider_token(http: httpx.AsyncClient, p: IdentityProvider, form: dict[str, str]) -> dict[str, Any]:
    form = {**form, "client_id": p.client_id, "client_secret": p.client_secret}
    if p.send_scope_on_token:
        form["scope"] = " ".join(p.scopes)
    r = await http.post(p.token_url, data=form, headers={"Accept": "application/json"})
    body = r.json() if r.content else {}
    if r.status_code >= 400 or "access_token" not in body:
        if body.get("error") in ("invalid_grant", "unauthorized_client", "interaction_required", "consent_required"):
            raise ProviderRefused(body.get("error"))
        raise RuntimeError(f"{p.name} token endpoint failed ({r.status_code}, {body.get('error')})")
    return body


async def refresh_provider_access(http: httpx.AsyncClient, p: IdentityProvider, refresh_token: str) -> dict[str, Any]:
    """Fresh provider access token for one owner (used for storage calls and to confirm access is still granted)."""
    return await provider_token(http, p, {"grant_type": "refresh_token", "refresh_token": refresh_token})


# ---------------------------------------------------------------- token models
class ITKCode(AuthorizationCode):
    identity: dict[str, Any]
    jti: str


class ITKRefresh(RefreshToken):
    identity: dict[str, Any]


def client_hash(client_id: str) -> str:
    return hashlib.sha256(client_id.encode()).hexdigest()[:24]


def subject_of(identity: dict[str, Any]) -> str:
    return f"{identity['p']}:{identity['sub']}"


# ---------------------------------------------------------------- authorization server
class StatelessAuthProvider:
    """OAuthAuthorizationServerProvider for the MCP SDK, keeping no records."""

    def __init__(self, sealer: Sealer, base_url: str, providers: dict[str, IdentityProvider],
                 http_factory: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(timeout=15),
                 after_sign_in: Optional[Callable[[dict, dict, str], Optional[str]]] = None):
        """`after_sign_in(pending, identity, lang)` may return a URL to show before finishing (billing's plan page);
        that page later calls `finish(pending, identity)`. It runs in a worker thread (it may call a database)."""
        if not providers:
            raise ValueError("configure at least one identity provider (Google or Microsoft)")
        self.sealer, self.base, self.providers, self.http_factory = sealer, base_url.rstrip("/"), providers, http_factory
        self.after_sign_in = after_sign_in
        self.resource = f"{self.base}/mcp"
        self._used: dict[str, float] = {}
        self._used_lock = threading.Lock()

    # ---- clients
    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        try:
            body = self.sealer.open("client", client_id)
        except SealError:
            return None
        return OAuthClientInformationFull.model_validate({**body["c"], "client_id": client_id})

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        meta = client_info.model_dump(mode="json", exclude={"client_id"}, exclude_none=True)
        client_info.client_id = self.sealer.seal("client", {"c": meta}, ttl=None)

    # ---- authorize -> our sign-in page
    def _resource(self, requested: Optional[str]) -> str:
        if requested is None or requested.rstrip("/") in (self.resource, self.base):
            return self.resource
        raise AuthorizeError("invalid_target", "unknown resource")

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        pending = {"ch": client_hash(client.client_id or ""), "r": str(params.redirect_uri),
                   "x": params.redirect_uri_provided_explicitly, "cc": params.code_challenge, "st": params.state,
                   "sc": params.scopes or [], "res": self._resource(params.resource)}
        return f"{self.base}/login?" + urlencode({"req": self.sealer.seal("req", pending, LOGIN_TTL)})

    # ---- codes
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> Optional[ITKCode]:
        try:
            b = self.sealer.open("code", authorization_code)
        except SealError:
            return None
        if b["ch"] != client_hash(client.client_id or "") or self._is_used(b["jti"]):
            return None
        return ITKCode(code=authorization_code, scopes=b["sc"], expires_at=b["exp"], client_id=client.client_id or "",
                       code_challenge=b["cc"], redirect_uri=AnyUrl(b["r"]), redirect_uri_provided_explicitly=b["x"],
                       resource=b["res"], subject=subject_of(b["u"]), identity=b["u"], jti=b["jti"])

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: ITKCode) -> OAuthToken:
        if not self._use(authorization_code.jti, authorization_code.expires_at):
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(client, authorization_code.identity, authorization_code.scopes, authorization_code.resource)

    def _is_used(self, jti: str) -> bool:
        with self._used_lock:
            return jti in self._used

    def _use(self, jti: str, exp: float) -> bool:
        with self._used_lock:
            now = time.time()
            for k in [k for k, e in self._used.items() if e < now]:
                del self._used[k]
            if jti in self._used:
                return False
            self._used[jti] = exp + 1
            return True

    # ---- tokens
    def _issue(self, client: OAuthClientInformationFull, identity: dict, scopes: list[str], resource: Optional[str]) -> OAuthToken:
        body = {"ch": client_hash(client.client_id or ""), "u": identity, "sc": scopes, "res": resource or self.resource}
        return OAuthToken(access_token=self.sealer.seal("access", body, ACCESS_TTL), token_type="Bearer",
                          expires_in=ACCESS_TTL, refresh_token=self.sealer.seal("refresh", body, REFRESH_TTL),
                          scope=" ".join(scopes) or None)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> Optional[ITKRefresh]:
        try:
            b = self.sealer.open("refresh", refresh_token)
        except SealError:
            return None
        if b["ch"] != client_hash(client.client_id or ""):
            return None
        return ITKRefresh(token=refresh_token, client_id=client.client_id or "", scopes=b["sc"], expires_at=b["exp"],
                          resource=b["res"], subject=subject_of(b["u"]), identity=b["u"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: ITKRefresh, scopes: list[str]) -> OAuthToken:
        identity = dict(refresh_token.identity)
        p = self.providers.get(identity["p"])
        if p is None:
            raise TokenError("invalid_grant", "sign-in provider no longer available")
        async with self.http_factory() as http:          # confirms the owner has not removed access
            try:
                got = await refresh_provider_access(http, p, identity["rt"])
            except ProviderRefused:
                raise TokenError("invalid_grant", "access was removed; connect again") from None
        if got.get("refresh_token"):                     # Microsoft rotates its refresh tokens
            identity["rt"] = got["refresh_token"]
        return self._issue(client, identity, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        try:
            b = self.sealer.open("access", token)
        except SealError:
            return None
        return AccessToken(token=token, client_id=b["ch"], scopes=b["sc"], expires_at=b["exp"], resource=b["res"],
                           subject=subject_of(b["u"]), claims={"identity": b["u"]})

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        return None                                      # stateless: owners revoke at Google / Microsoft

    # ---------------------------------------------------------------- sign-in pages
    def routes(self) -> list[tuple[str, list[str], Callable]]:
        return [("/login", ["GET"], self.login_page), ("/login/{provider}", ["GET"], self.login_start),
                ("/callback/{provider}", ["GET"], self.callback)]

    async def login_page(self, request: Request) -> Response:
        lang = pick_lang(request.headers.get("accept-language", ""))
        req = request.query_params.get("req", "")
        try:
            self.sealer.open("req", req)
        except SealError:
            return page(lang, error=True, status=400)
        buttons = [(p.name, p.label, f"/login/{p.name}?" + urlencode({"req": req})) for p in self.providers.values()]
        return page(lang, buttons=buttons)

    async def login_start(self, request: Request) -> Response:
        lang = pick_lang(request.headers.get("accept-language", ""))
        p = self.providers.get(request.path_params["provider"])
        req = request.query_params.get("req", "")
        try:
            self.sealer.open("req", req)
        except SealError:
            return page(lang, error=True, status=400)
        if p is None:
            return page(lang, error=True, status=404)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = self.sealer.seal("state", {"req": req, "p": p.name, "v": verifier, "l": lang}, LOGIN_TTL)
        q = {"client_id": p.client_id, "redirect_uri": f"{self.base}/callback/{p.name}", "response_type": "code",
             "scope": " ".join(p.scopes), "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
             **p.authorize_extra}
        return RedirectResponse(f"{p.authorize_url}?{urlencode(q)}", status_code=302, headers=NO_STORE)

    async def callback(self, request: Request) -> Response:
        lang = pick_lang(request.headers.get("accept-language", ""))
        q = request.query_params
        try:
            st = self.sealer.open("state", q.get("state", ""))
            pending = self.sealer.open("req", st["req"])
        except SealError:
            return page(lang, error=True, status=400)
        p = self.providers.get(request.path_params["provider"])
        if p is None or p.name != st["p"]:
            return page(st.get("l", lang), error=True, status=400)
        if q.get("error") or not q.get("code"):          # owner cancelled: tell the client, as OAuth expects
            return RedirectResponse(construct_redirect_uri(pending["r"], error="access_denied", state=pending["st"]),
                                    status_code=302, headers=NO_STORE)
        try:
            async with self.http_factory() as http:
                tok = await provider_token(http, p, {"grant_type": "authorization_code", "code": q["code"],
                                                     "redirect_uri": f"{self.base}/callback/{p.name}", "code_verifier": st["v"]})
            claims = _id_claims(tok["id_token"])
            sub = claims["sub"]
        except Exception:  # noqa: BLE001 - never show provider details to the owner
            return page(st.get("l", lang), error=True, status=502)
        if not tok.get("refresh_token"):
            return page(st.get("l", lang), error=True, status=502)
        identity = {"p": p.name, "sub": sub, "rt": tok["refresh_token"]}
        email = claims.get("email") or (claims.get("preferred_username") if "@" in str(claims.get("preferred_username", "")) else "")
        if email:                                        # sealed in the tokens, never stored: prefills checkout
            identity["em"] = str(email)
        if self.after_sign_in is not None:
            hook = self.after_sign_in
            try:
                detour = await anyio.to_thread.run_sync(lambda: hook(pending, identity, st.get("l", lang)))
            except Exception:  # noqa: BLE001
                return page(st.get("l", lang), error=True, status=502)
            if detour:
                return RedirectResponse(detour, status_code=302, headers=NO_STORE)
        return RedirectResponse(self.finish(pending, identity), status_code=302, headers=NO_STORE)

    def finish(self, pending: dict, identity: dict) -> str:
        """Mint the single-use authorization code and return the redirect back to the client."""
        code = self.sealer.seal("code", {**pending, "u": identity, "jti": secrets.token_urlsafe(16)}, CODE_TTL)
        return construct_redirect_uri(pending["r"], code=code, state=pending["st"])


# ---------------------------------------------------------------- page
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
LANGS = ("en", "fr", "ro", "de")
TEXT = {
    "en": {"title": "Connect In The Know", "lead": "Your business memory is saved in your own Google Drive or OneDrive. We keep no copy.",
           "with": "Continue with {}", "fine": "In The Know can only see the files it creates. You can remove access at any time.",
           "error": "Sign-in did not finish. Close this window and connect In The Know again."},
    "fr": {"title": "Connecter In The Know", "lead": "La mémoire de votre entreprise est enregistrée dans votre propre Google Drive ou OneDrive. Nous n'en gardons aucune copie.",
           "with": "Continuer avec {}", "fine": "In The Know ne voit que les fichiers qu'il crée. Vous pouvez retirer l'accès à tout moment.",
           "error": "La connexion n'a pas abouti. Fermez cette fenêtre et connectez In The Know à nouveau."},
    "ro": {"title": "Conectează In The Know", "lead": "Memoria afacerii tale este salvată în propriul tău Google Drive sau OneDrive. Nu păstrăm nicio copie.",
           "with": "Continuă cu {}", "fine": "In The Know vede doar fișierele pe care le creează. Poți retrage accesul oricând.",
           "error": "Conectarea nu s-a finalizat. Închide această fereastră și conectează din nou In The Know."},
    "de": {"title": "In The Know verbinden", "lead": "Das Gedächtnis Ihres Geschäfts wird in Ihrem eigenen Google Drive oder OneDrive gespeichert. Wir behalten keine Kopie.",
           "with": "Weiter mit {}", "fine": "In The Know sieht nur die Dateien, die es selbst anlegt. Sie können den Zugriff jederzeit entziehen.",
           "error": "Die Anmeldung wurde nicht abgeschlossen. Schließen Sie dieses Fenster und verbinden Sie In The Know erneut."},
}
LOGOS = {
    "google": ('<svg viewBox="0 0 48 48" width="20" height="20" aria-hidden="true"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z"/>'
               '<path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/>'
               '<path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z"/>'
               '<path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z"/></svg>'),
    "microsoft": ('<svg viewBox="0 0 21 21" width="20" height="20" aria-hidden="true"><rect x="1" y="1" width="9" height="9" fill="#F25022"/>'
                  '<rect x="11" y="1" width="9" height="9" fill="#7FBA00"/><rect x="1" y="11" width="9" height="9" fill="#00A4EF"/>'
                  '<rect x="11" y="11" width="9" height="9" fill="#FFB900"/></svg>'),
}
MARK = ('<svg viewBox="0 0 32 32" width="44" height="44" aria-hidden="true">'
        '<rect width="32" height="32" rx="9" fill="#F5F3EE"/><path d="M6.52 22.37V13.24H9.05V22.37ZM15.59 13.24V15.14H13.87V19.57Q13.87 19.92 13.98 20.11Q14.09 20.3 14.28 20.38Q14.48 20.46 14.73 20.46Q14.91 20.46 15.09 20.42Q15.27 20.39 15.36 20.37L15.76 22.26Q15.57 22.32 15.22 22.4Q14.88 22.48 14.39 22.5Q13.47 22.53 12.78 22.25Q12.1 21.97 11.72 21.38Q11.34 20.8 11.34 19.9V15.14H10.09V13.24H11.34V11.05H13.87V13.24ZM16.9 22.37V10.2H19.44V16.71H19.57L22.5 13.24H25.41L22.02 17.19L25.58 22.37H22.61L20.12 18.69L19.44 19.47V22.37Z" fill="#14161F"/><path d="M7.79 12.06Q7.22 12.06 6.82 11.68Q6.42 11.31 6.42 10.78Q6.42 10.26 6.82 9.88Q7.22 9.5 7.79 9.5Q8.35 9.5 8.75 9.88Q9.16 10.26 9.16 10.78Q9.16 11.31 8.75 11.68Q8.35 12.06 7.79 12.06Z" fill="#4F46E5"/></svg>')
CSS = """
:root{--bg:#FAF8F4;--card:#fff;--ink:#14161F;--muted:#5d6070;--line:#e6e2da;--accent:#4F46E5}
@media (prefers-color-scheme:dark){:root{--bg:#101218;--card:#181b23;--ink:#f3f1ec;--muted:#a3a6b3;--line:#2a2e3a}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px}
main{width:100%;max-width:400px;background:var(--card);border:1px solid var(--line);border-radius:20px;padding:32px 24px;text-align:center}
h1{font-size:22px;margin:16px 0 8px}p{margin:0 0 24px;color:var(--muted)}
a.btn{display:flex;align-items:center;justify-content:center;gap:12px;min-height:52px;margin:0 0 12px;border:1px solid var(--line);
border-radius:12px;color:var(--ink);text-decoration:none;font-weight:600;background:var(--card)}
a.btn:hover,a.btn:focus-visible{border-color:var(--ink);outline:none}
small{display:block;margin-top:12px;color:var(--muted);font-size:13px}
"""


def pick_lang(accept: str) -> str:
    for part in accept.split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGS:
            return code
    return "en"


def page(lang: str, buttons: Optional[list[tuple[str, str, str]]] = None, error: bool = False, status: int = 200) -> HTMLResponse:
    t = TEXT.get(lang, TEXT["en"])
    if error:
        inner = f"<h1>{html.escape(t['title'])}</h1><p>{html.escape(t['error'])}</p>"
    else:
        links = "".join(f'<a class="btn" href="{html.escape(href)}">{LOGOS.get(name, "")}<span>{html.escape(t["with"].format(label))}</span></a>'
                        for name, label, href in buttons or [])
        inner = f"<h1>{html.escape(t['title'])}</h1><p>{html.escape(t['lead'])}</p>{links}<small>{html.escape(t['fine'])}</small>"
    doc = (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<meta name="robots" content="noindex"><title>{html.escape(t["title"])}</title><style>{CSS}</style></head>'
           f'<body><main>{MARK}{inner}</main></body></html>')
    return HTMLResponse(doc, status_code=status, headers={**NO_STORE, "X-Frame-Options": "DENY",
                                                          "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'"})
