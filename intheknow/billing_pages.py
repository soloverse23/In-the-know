"""Billing pages of the hosted connector.

/plan             shown once, while a new owner connects: start the 14-day trial (card, Paddle) or enter a code.
/plan/code        redeem a code from that page, then finish connecting.
/plan/status      polled by the page after checkout until Paddle's webhook has landed.
/plan/continue    finish connecting (only once the owner has an active plan).
/account          status, manage/cancel at Paddle, subscribe, redeem a code. Reached by a sealed link (24 h).
/billing/paddle   Paddle webhook.

Everything a page needs travels in a sealed blob (purpose "plan" or "account"); nothing is kept in RAM.
"""
from __future__ import annotations

import html
import json
import secrets
import time
from typing import Callable

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .auth import CSS, MARK, NO_STORE, pick_lang
from .billing import PLAN_TTL, PRICES_EUR, Billing
from .seal import SealError

TEXT = {
    "en": {"title": "Choose your plan", "lead": "14 days free, then one simple plan. Cancel anytime.",
           "monthly": "Monthly", "yearly": "Yearly", "save": "2 months free", "pm": "/ month", "py": "/ year",
           "start": "Start 14 days free", "sub": "Subscribe", "card": "You add a card now and pay nothing for 14 days. Cancel before then and you are never charged.",
           "code_q": "Have a code?", "code_btn": "Use code", "data": "Your memory stays in your own Drive, even if you stop.",
           "invalid": "This code doesn't exist. Check it and try again.", "used": "This code has already been used.",
           "already": "You have already used a free code.", "subscribed": "You already have a plan, so the code wasn't needed.",
           "waiting": "Payment set up. Finishing the connection…", "slow": "This is taking longer than usual.", "retry": "Try again",
           "expired": "This page has expired. Go back to Claude or ChatGPT and connect In The Know again.",
           "acc": "Your plan", "acc_expired": "This link has expired. Ask Claude for a new one.",
           "s_active": "Paid plan · renews {d}", "s_trialing": "Free trial · first payment on {d}", "s_promo": "Free with a code · until {d}",
           "s_past_due": "The last payment failed. Update your card to keep saving.", "s_none": "No active plan. Reading still works; saving is paused.",
           "manage": "Manage or cancel", "promo_ok": "3 free months added, until {d}. Go back to Claude and carry on.",
           "paid_ok": "Done. Go back to Claude and carry on.", "portal_fail": "Couldn't open the billing page. Try again in a minute."},
    "fr": {"title": "Choisissez votre formule", "lead": "14 jours gratuits, puis une seule formule simple. Résiliable à tout moment.",
           "monthly": "Mensuel", "yearly": "Annuel", "save": "2 mois offerts", "pm": "/ mois", "py": "/ an",
           "start": "Commencer 14 jours gratuits", "sub": "S'abonner", "card": "Vous ajoutez une carte maintenant et ne payez rien pendant 14 jours. Résiliez avant et vous ne serez jamais débité.",
           "code_q": "Vous avez un code ?", "code_btn": "Utiliser le code", "data": "Votre mémoire reste dans votre propre Drive, même si vous arrêtez.",
           "invalid": "Ce code n'existe pas. Vérifiez-le et réessayez.", "used": "Ce code a déjà été utilisé.",
           "already": "Vous avez déjà utilisé un code gratuit.", "subscribed": "Vous avez déjà une formule, le code n'était pas nécessaire.",
           "waiting": "Paiement configuré. Finalisation de la connexion…", "slow": "Cela prend plus de temps que d'habitude.", "retry": "Réessayer",
           "expired": "Cette page a expiré. Revenez dans Claude ou ChatGPT et connectez In The Know à nouveau.",
           "acc": "Votre formule", "acc_expired": "Ce lien a expiré. Demandez-en un nouveau à Claude.",
           "s_active": "Formule payante · renouvellement le {d}", "s_trialing": "Essai gratuit · premier paiement le {d}", "s_promo": "Gratuit avec un code · jusqu'au {d}",
           "s_past_due": "Le dernier paiement a échoué. Mettez à jour votre carte pour continuer à enregistrer.", "s_none": "Aucune formule active. La lecture fonctionne ; l'enregistrement est en pause.",
           "manage": "Gérer ou résilier", "promo_ok": "3 mois gratuits ajoutés, jusqu'au {d}. Revenez dans Claude et continuez.",
           "paid_ok": "C'est fait. Revenez dans Claude et continuez.", "portal_fail": "Impossible d'ouvrir la page de facturation. Réessayez dans une minute."},
    "ro": {"title": "Alege planul", "lead": "14 zile gratuit, apoi un singur plan simplu. Anulezi oricând.",
           "monthly": "Lunar", "yearly": "Anual", "save": "2 luni gratuite", "pm": "/ lună", "py": "/ an",
           "start": "Începe 14 zile gratuit", "sub": "Abonează-te", "card": "Adaugi un card acum și nu plătești nimic 14 zile. Dacă anulezi înainte, nu ești taxat niciodată.",
           "code_q": "Ai un cod?", "code_btn": "Folosește codul", "data": "Memoria rămâne în propriul tău Drive, chiar dacă renunți.",
           "invalid": "Acest cod nu există. Verifică-l și încearcă din nou.", "used": "Acest cod a fost deja folosit.",
           "already": "Ai folosit deja un cod gratuit.", "subscribed": "Ai deja un plan, așa că nu era nevoie de cod.",
           "waiting": "Plata a fost configurată. Se finalizează conectarea…", "slow": "Durează mai mult decât de obicei.", "retry": "Încearcă din nou",
           "expired": "Această pagină a expirat. Întoarce-te în Claude sau ChatGPT și conectează din nou In The Know.",
           "acc": "Planul tău", "acc_expired": "Acest link a expirat. Cere-i lui Claude unul nou.",
           "s_active": "Plan plătit · se reînnoiește pe {d}", "s_trialing": "Perioadă gratuită · prima plată pe {d}", "s_promo": "Gratuit cu cod · până pe {d}",
           "s_past_due": "Ultima plată a eșuat. Actualizează cardul ca să poți salva în continuare.", "s_none": "Niciun plan activ. Citirea funcționează; salvarea e oprită.",
           "manage": "Gestionează sau anulează", "promo_ok": "3 luni gratuite adăugate, până pe {d}. Întoarce-te în Claude și continuă.",
           "paid_ok": "Gata. Întoarce-te în Claude și continuă.", "portal_fail": "Pagina de facturare nu s-a putut deschide. Încearcă din nou peste un minut."},
    "de": {"title": "Wählen Sie Ihren Plan", "lead": "14 Tage kostenlos, dann ein einfacher Plan. Jederzeit kündbar.",
           "monthly": "Monatlich", "yearly": "Jährlich", "save": "2 Monate gratis", "pm": "/ Monat", "py": "/ Jahr",
           "start": "14 Tage kostenlos starten", "sub": "Abonnieren", "card": "Sie hinterlegen jetzt eine Karte und zahlen 14 Tage lang nichts. Kündigen Sie vorher, wird nie etwas abgebucht.",
           "code_q": "Haben Sie einen Code?", "code_btn": "Code einlösen", "data": "Ihr Gedächtnis bleibt in Ihrem eigenen Drive, auch wenn Sie aufhören.",
           "invalid": "Diesen Code gibt es nicht. Prüfen Sie ihn und versuchen Sie es erneut.", "used": "Dieser Code wurde bereits verwendet.",
           "already": "Sie haben bereits einen Gratis-Code verwendet.", "subscribed": "Sie haben bereits einen Plan, der Code war nicht nötig.",
           "waiting": "Zahlung eingerichtet. Verbindung wird abgeschlossen…", "slow": "Das dauert länger als üblich.", "retry": "Erneut versuchen",
           "expired": "Diese Seite ist abgelaufen. Gehen Sie zurück zu Claude oder ChatGPT und verbinden Sie In The Know erneut.",
           "acc": "Ihr Plan", "acc_expired": "Dieser Link ist abgelaufen. Bitten Sie Claude um einen neuen.",
           "s_active": "Bezahlter Plan · verlängert sich am {d}", "s_trialing": "Kostenloser Test · erste Zahlung am {d}", "s_promo": "Gratis mit Code · bis {d}",
           "s_past_due": "Die letzte Zahlung ist fehlgeschlagen. Aktualisieren Sie Ihre Karte, um weiter zu speichern.", "s_none": "Kein aktiver Plan. Lesen funktioniert; Speichern ist pausiert.",
           "manage": "Verwalten oder kündigen", "promo_ok": "3 Gratis-Monate hinzugefügt, bis {d}. Gehen Sie zurück zu Claude und machen Sie weiter.",
           "paid_ok": "Erledigt. Gehen Sie zurück zu Claude und machen Sie weiter.", "portal_fail": "Die Abrechnungsseite ließ sich nicht öffnen. Versuchen Sie es in einer Minute erneut."},
}
PAGE_CSS = CSS + """
.seg{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:0 0 16px}
.seg label{border:1px solid var(--line);border-radius:12px;padding:12px 8px;cursor:pointer;display:block}
.seg input{position:absolute;opacity:0;pointer-events:none}
.seg label:has(input:checked){border-color:var(--ink);box-shadow:0 0 0 1px var(--ink)}
.seg b{display:block;font-size:15px}.seg .amt{font-size:22px;font-weight:700;display:block;margin-top:4px}
.seg .amt small{display:inline;font-size:13px;font-weight:500;margin:0}.seg em{font-style:normal;font-size:12px;color:var(--accent);font-weight:600}
button,.go{display:flex;align-items:center;justify-content:center;width:100%;min-height:52px;border:0;border-radius:12px;background:var(--ink);
color:var(--bg);font:inherit;font-weight:600;cursor:pointer;text-decoration:none;margin:0 0 12px}
button.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
@media (prefers-color-scheme:dark){:root{--accent:#A5A0FF}}
details{margin-top:20px;text-align:left}summary{cursor:pointer;color:var(--ink);text-align:center;list-style:none;
text-decoration:underline;text-underline-offset:4px;min-height:44px;display:flex;align-items:center;justify-content:center}
summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:8px}
summary::-webkit-details-marker{display:none}
form.code{display:flex;gap:8px;margin-top:12px}form.code input{flex:1;min-width:0;min-height:48px;border:1px solid var(--line);border-radius:12px;
padding:0 12px;font:inherit;background:var(--card);color:var(--ink);text-transform:uppercase;letter-spacing:.04em}
form.code button{width:auto;min-height:48px;padding:0 16px;margin:0}
.msg{border-radius:12px;padding:12px;margin:0 0 16px;background:color-mix(in srgb,var(--accent) 10%,transparent);color:var(--ink)}
.msg.err{background:color-mix(in srgb,#d33 12%,transparent)}
.status{font-weight:600;color:var(--ink);margin-bottom:16px}
[hidden]{display:none!important}
"""


def _date(ts: float, lang: str) -> str:
    return time.strftime("%d %b %Y" if lang == "en" else "%d.%m.%Y", time.gmtime(ts))


def _js(obj) -> str:
    return json.dumps(obj).replace("<", "\\u003c")


def _doc(lang: str, title: str, inner: str, script: str = "", status: int = 200, paddle: bool = False) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    paddle_src = "https://cdn.paddle.com https://*.paddle.com" if paddle else ""
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}' {paddle_src}; style-src 'unsafe-inline' {paddle_src}; "
           f"img-src data: {paddle_src}; connect-src 'self' {paddle_src}; frame-src {paddle_src or 'none'}; "
           "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
    lib = f'<script nonce="{nonce}" src="https://cdn.paddle.com/paddle/v2/paddle.js"></script>' if paddle else ""
    body = (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex"><title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head>'
            f'<body><main>{MARK}{inner}</main>{lib}'
            + (f'<script nonce="{nonce}">{script}</script>' if script else "") + "</body></html>")
    return HTMLResponse(body, status_code=status, headers={**NO_STORE, "X-Frame-Options": "DENY", "Content-Security-Policy": csp})


def _plans_html(t: dict, start_label: str) -> str:
    return (f'<div class="seg" role="radiogroup">'
            f'<label><input type="radio" name="period" value="yearly" checked><b>{html.escape(t["yearly"])}</b>'
            f'<span class="amt">€{PRICES_EUR["yearly"]} <small>{html.escape(t["py"])}</small></span><em>{html.escape(t["save"])}</em></label>'
            f'<label><input type="radio" name="period" value="monthly"><b>{html.escape(t["monthly"])}</b>'
            f'<span class="amt">€{PRICES_EUR["monthly"]} <small>{html.escape(t["pm"])}</small></span><em>&nbsp;</em></label></div>'
            f'<button type="button" id="pay">{html.escape(start_label)}</button>')


def _code_form(t: dict, action: str, field: str, blob: str, open_: bool) -> str:
    return (f'<details{" open" if open_ else ""}><summary>{html.escape(t["code_q"])}</summary>'
            f'<form class="code" method="post" action="{action}"><input type="hidden" name="{field}" value="{html.escape(blob)}">'
            f'<input name="code" placeholder="ITK-XXXX-XXXX" autocomplete="off" autocapitalize="characters" spellcheck="false" required aria-label="{html.escape(t["code_q"])}">'
            f'<button type="submit">{html.escape(t["code_btn"])}</button></form></details>')


CHECKOUT_JS = """
const C = %s;
if (window.Paddle) {
  if (C.env === "sandbox") Paddle.Environment.set("sandbox");
  Paddle.Initialize({token: C.token, eventCallback: (e) => { if (e.name === "checkout.completed") done(); }});
}
document.getElementById("pay").addEventListener("click", () => {
  const period = document.querySelector('input[name=period]:checked').value;
  const opts = {items: [{priceId: C.prices[period], quantity: 1}], customData: {o: C.owner},
                settings: {displayMode: "overlay", locale: C.lang, allowLogout: false}};
  if (C.email) opts.customer = {email: C.email};
  Paddle.Checkout.open(opts);
});
function done() {
  try { Paddle.Checkout.close(); } catch (_) {}
  const box = document.getElementById("wait"); box.hidden = false; box.textContent = C.t.waiting;
  let tries = 0;
  const tick = async () => {
    tries++;
    try {
      const r = await fetch(C.status, {cache: "no-store"});
      if ((await r.json()).ready) { location.href = C.next; return; }
    } catch (_) {}
    if (tries < 40) { setTimeout(tick, 2000); return; }
    box.textContent = C.t.slow + " ";
    const a = document.createElement("a"); a.href = C.next; a.textContent = C.t.retry; box.appendChild(a);
  };
  tick();
}
"""


def routes(billing: Billing, finish: Callable[[dict, dict], str]) -> list[tuple[str, list[str], Callable]]:
    """`finish(pending, identity)` mints the OAuth code and returns the redirect back to Claude / ChatGPT."""

    def open_plan(blob: str) -> dict:
        return billing.sealer.open("plan", blob)

    def checkout_cfg(key: str, email: str, lang: str, status: str, nxt: str, t: dict) -> str:
        p = billing.paddle
        return CHECKOUT_JS % _js({
            "env": p.env if p else "sandbox", "token": p.client_token if p else "",
            "prices": {"monthly": billing.price_for(key, "monthly"), "yearly": billing.price_for(key, "yearly")} if p else {},
            "owner": billing.owner_token(key), "email": email, "lang": lang, "status": status, "next": nxt,
            "t": {"waiting": t["waiting"], "slow": t["slow"], "retry": t["retry"]}})

    def plan_page(blob: str, b: dict, msg: str = "", err: bool = False, status: int = 200) -> HTMLResponse:
        lang = b.get("l", "en")
        t = TEXT.get(lang, TEXT["en"])
        had = billing.owner(b["k"], fresh=True).had_trial
        inner = (f'<h1>{html.escape(t["title"])}</h1><p>{html.escape(t["lead"])}</p>'
                 + (f'<div class="msg{" err" if err else ""}" role="alert">{html.escape(msg)}</div>' if msg else "")
                 + '<div class="msg" id="wait" role="status" hidden></div>'
                 + _plans_html(t, t["sub"] if had else t["start"])
                 + ("" if had else f'<small>{html.escape(t["card"])}</small>')
                 + _code_form(t, "/plan/code", "s", blob, open_=err)
                 + f'<small>{html.escape(t["data"])}</small>')
        script = checkout_cfg(b["k"], b.get("e", ""), lang, f"/plan/status?s={blob}", f"/plan/continue?s={blob}", t)
        return _doc(lang, t["title"], inner, script, status, paddle=billing.paddle is not None)

    def expired(request: Request, key: str = "expired") -> HTMLResponse:
        lang = pick_lang(request.headers.get("accept-language", ""))
        t = TEXT[lang]
        return _doc(lang, t["title"], f'<h1>{html.escape(t["title"])}</h1><p>{html.escape(t[key])}</p>', status=400)

    # ---------------------------------------------------------------- /plan (while connecting)
    async def plan(request: Request) -> Response:
        blob = request.query_params.get("s", "")
        try:
            b = open_plan(blob)
        except SealError:
            return expired(request)
        return await anyio.to_thread.run_sync(lambda: plan_page(blob, b))

    async def plan_code(request: Request) -> Response:
        form = await request.form()
        blob = str(form.get("s", ""))
        try:
            b = open_plan(blob)
        except SealError:
            return expired(request)
        result = await anyio.to_thread.run_sync(lambda: billing.redeem(b["k"], str(form.get("code", ""))))
        if result in ("ok", "subscribed"):
            return RedirectResponse(finish(b["p"], b["u"]), status_code=303, headers=NO_STORE)
        t = TEXT.get(b.get("l", "en"), TEXT["en"])
        return await anyio.to_thread.run_sync(lambda: plan_page(blob, b, t[result], err=True, status=400))

    async def plan_status(request: Request) -> Response:
        try:
            b = open_plan(request.query_params.get("s", ""))
        except SealError:
            return JSONResponse({"ready": False, "expired": True}, headers=NO_STORE)
        o = await anyio.to_thread.run_sync(lambda: billing.owner(b["k"], fresh=True))
        return JSONResponse({"ready": o.entitled(billing.now())}, headers=NO_STORE)

    async def plan_continue(request: Request) -> Response:
        blob = request.query_params.get("s", "")
        try:
            b = open_plan(blob)
        except SealError:
            return expired(request)
        o = await anyio.to_thread.run_sync(lambda: billing.owner(b["k"], fresh=True))
        if not o.entitled(billing.now()):
            return await anyio.to_thread.run_sync(lambda: plan_page(blob, b))
        return RedirectResponse(finish(b["p"], b["u"]), status_code=302, headers=NO_STORE)

    # ---------------------------------------------------------------- /account
    def account_page(blob: str, a: dict, lang: str, msg: str = "", err: bool = False, status: int = 200) -> HTMLResponse:
        t = TEXT.get(lang, TEXT["en"])
        o = billing.owner(a["k"], fresh=True)
        now = billing.now()
        if o.entitled(now) and o.status in ("active", "trialing", "promo"):
            line = t[f"s_{o.status}"].format(d=_date(o.ends_at, lang))
        elif o.status == "past_due" and o.paddle_sub_id:
            line = t["s_past_due"]
        else:
            line = t["s_none"]
        has_paddle = bool(o.paddle_customer_id) and o.status in ("active", "trialing", "past_due", "paused")
        manage = (f'<form method="post" action="/account/portal"><input type="hidden" name="t" value="{html.escape(blob)}">'
                  f'<button type="submit" class="ghost">{html.escape(t["manage"])}</button></form>') if has_paddle else ""
        buy = "" if (o.subscribed(now) or billing.paddle is None) else _plans_html(t, t["sub"] if o.had_trial else t["start"])
        inner = (f'<h1>{html.escape(t["acc"])}</h1>'
                 + (f'<div class="msg{" err" if err else ""}" role="alert">{html.escape(msg)}</div>' if msg else "")
                 + '<div class="msg" id="wait" role="status" hidden></div>'
                 + f'<p class="status">{html.escape(line)}</p>' + manage + buy
                 + ("" if o.subscribed(now) else _code_form(t, "/account/code", "t", blob, open_=err))
                 + f'<small>{html.escape(t["data"])}</small>')
        script = checkout_cfg(a["k"], a.get("e", ""), lang, f"/account/status?t={blob}", f"/account?t={blob}&paid=1", t) if buy else ""
        return _doc(lang, t["acc"], inner, script, status, paddle=bool(buy))

    def open_account(blob: str) -> dict:
        return billing.sealer.open("account", blob)

    async def account(request: Request) -> Response:
        lang = pick_lang(request.headers.get("accept-language", ""))
        blob = request.query_params.get("t", "")
        try:
            a = open_account(blob)
        except SealError:
            return expired(request, "acc_expired")
        msg = TEXT[lang]["paid_ok"] if request.query_params.get("paid") else ""
        return await anyio.to_thread.run_sync(lambda: account_page(blob, a, lang, msg))

    async def account_status(request: Request) -> Response:
        try:
            a = open_account(request.query_params.get("t", ""))
        except SealError:
            return JSONResponse({"ready": False, "expired": True}, headers=NO_STORE)
        o = await anyio.to_thread.run_sync(lambda: billing.owner(a["k"], fresh=True))
        return JSONResponse({"ready": o.subscribed(billing.now())}, headers=NO_STORE)

    async def account_code(request: Request) -> Response:
        lang = pick_lang(request.headers.get("accept-language", ""))
        form = await request.form()
        blob = str(form.get("t", ""))
        try:
            a = open_account(blob)
        except SealError:
            return expired(request, "acc_expired")
        result = await anyio.to_thread.run_sync(lambda: billing.redeem(a["k"], str(form.get("code", ""))))
        t = TEXT[lang]
        if result == "ok":
            o = await anyio.to_thread.run_sync(lambda: billing.owner(a["k"], fresh=True))
            return await anyio.to_thread.run_sync(lambda: account_page(blob, a, lang, t["promo_ok"].format(d=_date(o.ends_at, lang))))
        return await anyio.to_thread.run_sync(lambda: account_page(blob, a, lang, t[result], err=True, status=400))

    async def account_portal(request: Request) -> Response:
        lang = pick_lang(request.headers.get("accept-language", ""))
        form = await request.form()
        blob = str(form.get("t", ""))
        try:
            a = open_account(blob)
        except SealError:
            return expired(request, "acc_expired")
        url = await billing.portal_url(a["k"])
        if not url:
            return await anyio.to_thread.run_sync(lambda: account_page(blob, a, lang, TEXT[lang]["portal_fail"], err=True, status=502))
        return RedirectResponse(url, status_code=303, headers=NO_STORE)

    # ---------------------------------------------------------------- webhook
    async def paddle_webhook(request: Request) -> Response:
        raw = await request.body()
        ok = await anyio.to_thread.run_sync(lambda: billing.apply_webhook(raw, request.headers.get("paddle-signature", "")))
        return JSONResponse({"ok": ok}, status_code=200 if ok else 401)

    return [("/plan", ["GET"], plan), ("/plan/code", ["POST"], plan_code), ("/plan/status", ["GET"], plan_status),
            ("/plan/continue", ["GET"], plan_continue), ("/account", ["GET"], account),
            ("/account/status", ["GET"], account_status), ("/account/code", ["POST"], account_code),
            ("/account/portal", ["POST"], account_portal), ("/billing/paddle", ["POST"], paddle_webhook)]


def plan_blob(billing: Billing, key: str, email: str, lang: str, pending: dict, identity: dict) -> str:
    return billing.sealer.seal("plan", {"k": key, "e": email, "l": lang, "p": pending, "u": identity}, PLAN_TTL)
