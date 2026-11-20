"""Public pages of the hosted connector: home and privacy policy.

Google's brand verification needs both on the app's own domain. The policy describes what the code
actually does; the operator's name and contact come from the environment:
ITK_OPERATOR, ITK_CONTACT, ITK_HOSTING (the EU hosting provider), ITK_POLICY_DATE.
"""
from __future__ import annotations

import html
import os
from typing import Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from .auth import CSS, MARK, NO_STORE, pick_lang

HOME = {
    "en": ("The memory of your business, inside Claude and ChatGPT.",
           "Customers, visits, sales, prices, costs, goals and decisions in one exact memory. It lives in your own Google Drive or OneDrive; we keep no copy.",
           "Connect: in Claude or ChatGPT, add a custom connector with this address:", "Privacy policy"),
    "fr": ("La mémoire de votre entreprise, dans Claude et ChatGPT.",
           "Clients, visites, ventes, prix, coûts, objectifs et décisions dans une seule mémoire exacte. Elle vit dans votre propre Google Drive ou OneDrive ; nous n'en gardons aucune copie.",
           "Connexion : dans Claude ou ChatGPT, ajoutez un connecteur personnalisé avec cette adresse :", "Politique de confidentialité"),
    "ro": ("Memoria afacerii tale, în Claude și ChatGPT.",
           "Clienți, vizite, vânzări, prețuri, costuri, obiective și decizii într-o singură memorie exactă. Stă în propriul tău Google Drive sau OneDrive; nu păstrăm nicio copie.",
           "Conectare: în Claude sau ChatGPT, adaugă un conector personalizat cu această adresă:", "Politica de confidențialitate"),
    "de": ("Das Gedächtnis Ihres Geschäfts, in Claude und ChatGPT.",
           "Kunden, Besuche, Verkäufe, Preise, Kosten, Ziele und Entscheidungen in einem genauen Gedächtnis. Es liegt in Ihrem eigenen Google Drive oder OneDrive; wir behalten keine Kopie.",
           "Verbinden: Fügen Sie in Claude oder ChatGPT einen benutzerdefinierten Connector mit dieser Adresse hinzu:", "Datenschutzerklärung"),
}
SITE_CSS = CSS + """
main{max-width:640px;text-align:left}main>svg{display:block}h1{font-size:26px}h2{font-size:18px;margin:28px 0 8px}
main p,main li{color:var(--ink)}main p.lead{color:var(--muted)}ul{padding-left:20px}li{margin:4px 0}
code{display:block;padding:12px 14px;border:1px solid var(--line);border-radius:10px;word-break:break-all;margin:8px 0 16px}
a{color:var(--ink)}footer{margin-top:28px;color:var(--muted);font-size:14px}
"""


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _doc(lang: str, title: str, inner: str, https: bool) -> HTMLResponse:
    headers = {**NO_STORE, "X-Frame-Options": "DENY",
               "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'"}
    headers["Cache-Control"] = "public, max-age=600"
    if https:
        headers["Strict-Transport-Security"] = "max-age=31536000"
    body = (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{SITE_CSS}</style></head><body><main>{MARK}{inner}</main></body></html>')
    return HTMLResponse(body, headers=headers)


def routes(base_url: str, show_connector: bool = True) -> list[tuple[str, list[str], Callable]]:
    """`show_connector=False` (personal mode): the connector address holds a secret and is never shown."""
    base = base_url.rstrip("/")
    https = base.startswith("https://")

    async def home(request: Request) -> Response:
        lang = pick_lang(request.headers.get("accept-language", ""))
        tagline, lead, connect, privacy = HOME[lang]
        contact = _env("ITK_CONTACT", "")
        inner = (f"<h1>In The Know</h1><p>{html.escape(tagline)}</p><p class=\"lead\">{html.escape(lead)}</p>"
                 + (f"<p>{html.escape(connect)}</p><code>{html.escape(base)}/mcp</code>" if show_connector else "")
                 + f'<footer><a href="/privacy">{html.escape(privacy)}</a>'
                 + (f' · <a href="mailto:{html.escape(contact)}">{html.escape(contact)}</a>' if contact else "") + "</footer>")
        return _doc(lang, "In The Know", inner, https)

    async def privacy(request: Request) -> Response:
        return _doc("en", "In The Know: Privacy policy", privacy_html(base), https)

    return [("/", ["GET"], home), ("/privacy", ["GET"], privacy)]


def privacy_html(base: str) -> str:
    op = html.escape(_env("ITK_OPERATOR", "the operator of In The Know"))
    contact = html.escape(_env("ITK_CONTACT", "the contact address on the home page"))
    hosting = html.escape(_env("ITK_HOSTING", "our hosting provider in the European Union"))
    date = html.escape(_env("ITK_POLICY_DATE", "2026-11-20"))
    return f"""<h1>Privacy policy</h1><p class="lead">Last updated {date}. In The Know is operated by {op}. Contact: {contact}.</p>

<h2>In short</h2>
<ul><li>Your business memory is a single file in <b>your own</b> Google Drive (folder “In The Know”) or OneDrive (folder “Apps/In The Know”).</li>
<li>We keep <b>no copy</b> of it and no database of users or businesses.</li>
<li>We only touch that file while you, through your AI assistant, ask In The Know to do something.</li></ul>

<h2>What In The Know does</h2>
<p>In The Know is a connector for AI assistants such as Claude and ChatGPT. It keeps an exact memory of a small business (customers,
visits, sales, prices, costs, tasks, goals, decisions) and computes its numbers. You add it to your assistant at {html.escape(base)}/mcp.</p>

<h2>Where your data is stored</h2>
<p>In a file named memory.itk in your own cloud storage, which you can see, download, back up or delete at any time. When you add
files (customer lists, exports, notes, an export of your old chats), they are read once on our server and their content is added to
that file; the uploaded files themselves are not kept.</p>

<h2>What our server processes, and for how long</h2>
<ul><li>While handling a request, the server holds your memory file in its working memory (RAM). It is never written to our disks.
It stays in RAM for up to 10 minutes after your last request, so that consecutive requests are fast, and is then dropped.</li>
<li>Files you upload are processed in RAM in the same way and discarded after the import.</li>
<li>Operational logs record which function ran, how long it took, whether it worked, and a one-way hash of your account id. They
never contain the content of your memory, your files, your messages, or any access token.</li></ul>

<h2>Access to your Google or Microsoft account</h2>
<ul><li>Google: we request the <i>drive.file</i> permission, which lets In The Know see and change only the files it creates and the
files you explicitly choose in the Google file picker. We cannot see the rest of your Drive. We also receive your Google account's
identifier and email to know which memory is yours.</li>
<li>Microsoft: we request access to In The Know's own app folder in your OneDrive (<i>Files.ReadWrite.AppFolder</i>), plus your
account's identifier and email.</li>
<li>The authorization your provider gives us is encrypted and stored only inside the sign-in token that your AI assistant keeps for
you. Our server can read it when you make a request; we do not store it anywhere else.</li>
<li>You can remove access at any time: Google at myaccount.google.com/permissions, Microsoft at account.live.com/consent/Manage (or
your organisation's “My Apps” page).</li></ul>

<h2>Google API Services: Limited Use</h2>
<p>In The Know's use and transfer to any other app of information received from Google APIs will adhere to the
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data Policy</a>, including the
Limited Use requirements. We use Google user data only to provide In The Know's features to you. We do not sell it, do not use it
for advertising, do not transfer it to others except as needed to provide the service or required by law, and do not use it to
train AI models. No person at In The Know reads your data, except with your explicit permission for support, for security
reasons, or where the law requires it.</p>

<h2>Your AI assistant</h2>
<p>The answers In The Know gives to your assistant (for example a customer's history when you ask about them) are processed by
that assistant's provider (such as Anthropic or OpenAI) under their own privacy terms.</p>

<h2>Who else is involved</h2>
<p>Our server runs at {hosting}. We do not sell or share your data, and we use no advertising or analytics trackers.</p>

<h2>Your rights</h2>
<p>Under the GDPR you may ask for access, correction, deletion, restriction or portability of your personal data, and you may
complain to your data protection authority. Because your memory lives in your own storage, you can exercise most of these rights
directly: open, edit, export or delete the file. Inside In The Know you can also ask your assistant to export or forget everything
held about a person. For anything else, contact {contact}.</p>

<h2>Children</h2>
<p>In The Know is a tool for businesses and is not meant for people under 16.</p>

<h2>Changes</h2>
<p>We will update this page when what the service does with data changes, and change the date at the top.</p>
<footer><a href="/">In The Know</a></footer>"""
