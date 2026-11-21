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

    def legal(request: Request, page: str) -> Response:
        lang = legal_lang(request)
        title, render = LEGAL[lang][page]
        return _doc(lang, f"In The Know: {title}", render(base) if page == "privacy" else render(), https)

    async def privacy(request: Request) -> Response:
        return legal(request, "privacy")

    async def pricing(request: Request) -> Response:
        return legal(request, "pricing")

    async def terms(request: Request) -> Response:
        return legal(request, "terms")

    async def refunds(request: Request) -> Response:
        return legal(request, "refunds")

    return [("/", ["GET"], home), ("/privacy", ["GET"], privacy), ("/pricing", ["GET"], pricing),
            ("/terms", ["GET"], terms), ("/refunds", ["GET"], refunds)]


def legal_lang(request: Request) -> str:
    """Legal pages exist in English and Romanian: ?lang= wins (the landing page links with it), then the browser language."""
    asked = request.query_params.get("lang", "").lower()
    lang = asked if asked in ("en", "ro") else pick_lang(request.headers.get("accept-language", ""))
    return lang if lang in ("en", "ro") else "en"


def pricing_html() -> str:
    contact = html.escape(_env("ITK_CONTACT", "the contact address on the home page"))
    return f"""<h1>Pricing</h1><p class="lead">One plan, everything included.</p>
<ul><li><b>€4.99 per month</b>, or <b>€49 per year</b> (two months free). Prices include VAT.</li>
<li><b>14 days free</b> when you add a card. Cancel before the 14 days end and you are never charged.</li>
<li>No limits on customers, records or files.</li></ul>
<h2>Have a code?</h2><p>A promo code gives 3 months free with no card. Enter it when you connect In The Know to Claude or ChatGPT.</p>
<h2>If you stop</h2><p>Your memory is a file in your own Google Drive or OneDrive, so it stays yours. Without an active plan, your assistant can
still read everything; saving new things is paused until you subscribe again.</p>
<p>Payments are handled by Paddle, the seller of record. Questions: {contact}.</p>
<footer><a href="/">In The Know</a> · <a href="/terms">Terms</a> · <a href="/refunds">Refunds</a> · <a href="/privacy">Privacy</a></footer>"""


def terms_html() -> str:
    op = html.escape(_env("ITK_OPERATOR", "the operator of In The Know"))
    contact = html.escape(_env("ITK_CONTACT", "the contact address on the home page"))
    date = html.escape(_env("ITK_POLICY_DATE", "2026-11-20"))
    return f"""<h1>Terms of service</h1><p class="lead">Last updated {date}. In The Know is operated by {op}. Contact: {contact}.</p>
<h2>The service</h2><p>In The Know is a connector that gives AI assistants such as Claude and ChatGPT a lasting, exact memory of your
business. The memory is a file in your own Google Drive or OneDrive. You need an account with one of them and with your AI assistant.</p>
<h2>Your data</h2><p>Your data stays yours. We do not claim any rights over it. You are responsible for what you put in it, including having a
lawful basis to keep your customers' personal data. See the <a href="/privacy">privacy policy</a>.</p>
<h2>Plans, trial and renewal</h2>
<ul><li>The plan costs €4.99 per month or €49 per year, VAT included, and renews automatically until you cancel.</li>
<li>Adding a card starts a 14-day free trial, available once per person. If you do not cancel before it ends, the first payment is taken.</li>
<li>A promo code gives 3 months free without a card, once per person. When the period ends nothing is charged; saving pauses until you subscribe.</li>
<li>You can cancel at any time from your account page; the plan then runs to the end of the period you paid for.</li>
<li>Payments, invoices and VAT are handled by Paddle as the seller of record. Paddle's terms apply to the purchase.</li>
<li>We may change prices for future renewals and will tell you in advance. Early users may keep an agreed price.</li></ul>
<h2>Without an active plan</h2><p>Reading and exporting your memory keeps working. Saving new information is paused. Nothing is deleted.</p>
<h2>Acceptable use</h2><p>Do not misuse the service: no attempts to break, overload or reverse-engineer it, no unlawful use, and no sharing of one
plan by several businesses.</p>
<h2>Availability and liability</h2><p>The service is provided “as is”. We work to keep it available and your data safe, but we cannot promise it will never be
interrupted. Please keep your own backup; the file is in your Drive. To the extent the law allows, our liability is limited to the
amount you paid in the last 12 months. Nothing here limits rights you have under mandatory consumer law.</p>
<h2>AI answers</h2><p>The answers come from your AI assistant using your data. Check important decisions yourself.</p>
<h2>Changes and law</h2><p>We will update these terms when needed and change the date above. Romanian law applies, without affecting the mandatory consumer
protections of your country of residence.</p>
<footer><a href="/">In The Know</a> · <a href="/pricing">Pricing</a> · <a href="/refunds">Refunds</a> · <a href="/privacy">Privacy</a></footer>"""


def refunds_html() -> str:
    contact = html.escape(_env("ITK_CONTACT", "the contact address on the home page"))
    return f"""<h1>Refund policy</h1><p class="lead">Simple and fair.</p>
<ul><li><b>The 14-day trial is free.</b> Cancel before it ends and you are not charged.</li>
<li><b>Monthly plan:</b> if you were charged and did not mean to continue, ask within 14 days of the payment and we refund it in full.</li>
<li><b>Yearly plan:</b> ask within 30 days of the payment for a full refund. After that, you can cancel at any time and keep the service until the year ends.</li></ul>
<p>To ask, write to {contact} from the address you used to buy, or use the link in your Paddle receipt. Refunds are made by Paddle to the
original payment method. Where the law gives you a longer withdrawal right, that right applies.</p>
<footer><a href="/">In The Know</a> · <a href="/pricing">Pricing</a> · <a href="/terms">Terms</a> · <a href="/privacy">Privacy</a></footer>"""


def privacy_html(base: str) -> str:
    op = html.escape(_env("ITK_OPERATOR", "the operator of In The Know"))
    contact = html.escape(_env("ITK_CONTACT", "the contact address on the home page"))
    hosting = html.escape(_env("ITK_HOSTING", "our hosting provider in the European Union"))
    date = html.escape(_env("ITK_POLICY_DATE", "2026-11-20"))
    return f"""<h1>Privacy policy</h1><p class="lead">Last updated {date}. In The Know is operated by {op}. Contact: {contact}.</p>

<h2>In short</h2>
<ul><li>Your business memory is a single file in <b>your own</b> Google Drive (folder “In The Know”) or OneDrive (folder “Apps/In The Know”).</li>
<li>We keep <b>no copy</b> of it and no database of your customers or business data. The only thing we record about you is a small
billing record (see “Plans and payments”).</li>
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

<h2>Plans and payments</h2>
<p>In The Know has one paid plan (see <a href="/pricing">pricing</a>). Payments are handled by <b>Paddle</b>, which is the seller of
record: it collects your card details, charges you, handles VAT and sends your invoices. We never see or store your card number.</p>
<p>To know whether saving is switched on for you, we keep a small record in a database in the EU (Google Cloud Firestore): a
one-way hash of your account id, your plan status and its end date, whether you already used the free trial, Paddle's
subscription and customer reference numbers, and which promo code you used. It contains <b>no name, no email and nothing from your
business memory</b>. We delete it on request. Promo codes are stored only as hashes.</p>

<h2>Your AI assistant</h2>
<p>The answers In The Know gives to your assistant (for example a customer's history when you ask about them) are processed by
that assistant's provider (such as Anthropic or OpenAI) under their own privacy terms.</p>

<h2>Who else is involved</h2>
<p>Our server and billing record run at {hosting}. Paddle processes payments. We do not sell or share your data, and we use no
advertising or analytics trackers.</p>

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


# --------------------------------------------------------------------------- Romanian
RO_FOOT = '<footer><a href="/">In The Know</a> · {links}</footer>'
RO_LINKS = {"pricing": "Prețuri", "terms": "Termeni", "refunds": "Rambursări", "privacy": "Confidențialitate"}


def _ro_footer(skip: str) -> str:
    return RO_FOOT.format(links=" · ".join(f'<a href="/{k}?lang=ro">{v}</a>' for k, v in RO_LINKS.items() if k != skip))


def pricing_html_ro() -> str:
    contact = html.escape(_env("ITK_CONTACT", "adresa de contact de pe pagina principală"))
    return f"""<h1>Prețuri</h1><p class="lead">Un singur plan, totul inclus.</p>
<ul><li><b>4,99 € pe lună</b> sau <b>49 € pe an</b> (două luni gratuite). Prețurile includ TVA.</li>
<li><b>14 zile gratuit</b> când adaugi un card. Dacă anulezi înainte să treacă cele 14 zile, nu plătești nimic.</li>
<li>Fără limite pentru clienți, înregistrări sau fișiere.</li></ul>
<h2>Ai un cod?</h2><p>Un cod promoțional îți dă 3 luni gratuit, fără card. Introdu-l când conectezi In The Know la Claude sau ChatGPT.</p>
<h2>Dacă te oprești</h2><p>Memoria ta este un fișier în propriul tău Google Drive sau OneDrive, deci rămâne a ta. Fără un plan activ, asistentul tău
poate citi în continuare totul; salvarea lucrurilor noi se oprește până te abonezi din nou.</p>
<p>Plățile sunt gestionate de Paddle, care este vânzătorul înregistrat (seller of record). Întrebări: {contact}.</p>
{_ro_footer("pricing")}"""


def terms_html_ro() -> str:
    op = html.escape(_env("ITK_OPERATOR", "operatorul In The Know"))
    contact = html.escape(_env("ITK_CONTACT", "adresa de contact de pe pagina principală"))
    date = html.escape(_env("ITK_POLICY_DATE", "2026-11-20"))
    return f"""<h1>Termeni și condiții</h1><p class="lead">Ultima actualizare: {date}. In The Know este operat de {op}. Contact: {contact}.</p>
<h2>Serviciul</h2><p>In The Know este un conector care le oferă asistenților AI, precum Claude și ChatGPT, o memorie durabilă și exactă a
afacerii tale. Memoria este un fișier în propriul tău Google Drive sau OneDrive. Ai nevoie de un cont la unul dintre acestea și la asistentul tău AI.</p>
<h2>Datele tale</h2><p>Datele tale rămân ale tale. Nu revendicăm niciun drept asupra lor. Ești responsabil pentru ce pui în memorie, inclusiv
pentru existența unui temei legal pentru păstrarea datelor personale ale clienților tăi. Vezi <a href="/privacy?lang=ro">politica de confidențialitate</a>.</p>
<h2>Planuri, perioadă de probă și reînnoire</h2>
<ul><li>Planul costă 4,99 € pe lună sau 49 € pe an, cu TVA inclus, și se reînnoiește automat până când îl anulezi.</li>
<li>Adăugarea unui card pornește o perioadă de probă gratuită de 14 zile, disponibilă o singură dată de persoană. Dacă nu anulezi înainte să se
încheie, se încasează prima plată.</li>
<li>Un cod promoțional oferă 3 luni gratuit fără card, o singură dată de persoană. La finalul perioadei nu se încasează nimic; salvarea se
oprește până te abonezi.</li>
<li>Poți anula oricând din pagina contului tău; planul rămâne activ până la sfârșitul perioadei plătite.</li>
<li>Plățile, facturile și TVA-ul sunt gestionate de Paddle, ca vânzător înregistrat. Pentru cumpărare se aplică termenii Paddle.</li>
<li>Putem modifica prețurile pentru reînnoirile viitoare și te vom anunța din timp. Primii utilizatori pot păstra un preț convenit.</li></ul>
<h2>Fără un plan activ</h2><p>Citirea și exportul memoriei tale funcționează în continuare. Salvarea informațiilor noi se oprește. Nu se șterge nimic.</p>
<h2>Utilizare acceptabilă</h2><p>Nu folosi serviciul în mod abuziv: fără încercări de a-l strica, supraîncărca sau decompila, fără utilizări
ilegale și fără folosirea aceluiași plan de către mai multe afaceri.</p>
<h2>Disponibilitate și răspundere</h2><p>Serviciul este oferit „ca atare”. Ne străduim să îl menținem disponibil și să îți păstrăm datele în
siguranță, dar nu putem promite că nu va fi niciodată întrerupt. Te rugăm să îți păstrezi propria copie de siguranță; fișierul este în Drive-ul
tău. În măsura permisă de lege, răspunderea noastră este limitată la suma plătită de tine în ultimele 12 luni. Nimic din acești termeni nu
limitează drepturile pe care le ai conform legislației obligatorii privind protecția consumatorilor.</p>
<h2>Răspunsurile AI</h2><p>Răspunsurile vin de la asistentul tău AI, pe baza datelor tale. Verifică singur deciziile importante.</p>
<h2>Modificări și legea aplicabilă</h2><p>Vom actualiza acești termeni când va fi nevoie și vom schimba data de mai sus. Se aplică legea română,
fără a afecta protecțiile obligatorii pentru consumatori din țara ta de reședință.</p>
{_ro_footer("terms")}"""


def refunds_html_ro() -> str:
    contact = html.escape(_env("ITK_CONTACT", "adresa de contact de pe pagina principală"))
    return f"""<h1>Politica de rambursare</h1><p class="lead">Simplă și corectă.</p>
<ul><li><b>Perioada de probă de 14 zile este gratuită.</b> Dacă anulezi înainte să se încheie, nu plătești nimic.</li>
<li><b>Planul lunar:</b> dacă ți s-a încasat o plată și nu voiai să continui, cere rambursarea în cel mult 14 zile de la plată și îți
returnăm toată suma.</li>
<li><b>Planul anual:</b> cere rambursarea în cel mult 30 de zile de la plată și primești toată suma înapoi. După aceea, poți anula oricând
și păstrezi serviciul până la finalul anului.</li></ul>
<p>Pentru o cerere, scrie la {contact} de pe adresa folosită la cumpărare sau folosește linkul din chitanța Paddle. Rambursările sunt făcute
de Paddle, pe metoda de plată inițială. Acolo unde legea îți dă un drept de retragere mai lung, se aplică acel drept.</p>
{_ro_footer("refunds")}"""


def privacy_html_ro(base: str) -> str:
    op = html.escape(_env("ITK_OPERATOR", "operatorul In The Know"))
    contact = html.escape(_env("ITK_CONTACT", "adresa de contact de pe pagina principală"))
    hosting = html.escape(_env("ITK_HOSTING", "furnizorul nostru de găzduire din Uniunea Europeană"))
    date = html.escape(_env("ITK_POLICY_DATE", "2026-11-20"))
    return f"""<h1>Politica de confidențialitate</h1><p class="lead">Ultima actualizare: {date}. In The Know este operat de {op}. Contact: {contact}.</p>

<h2>Pe scurt</h2>
<ul><li>Memoria afacerii tale este un singur fișier în <b>propriul tău</b> Google Drive (folderul „In The Know”) sau OneDrive (folderul „Apps/In The Know”).</li>
<li>Nu păstrăm <b>nicio copie</b> a lui și nicio bază de date cu clienții sau datele afacerii tale. Singurul lucru pe care îl înregistrăm despre
tine este o mică înregistrare de facturare (vezi „Planuri și plăți”).</li>
<li>Atingem acel fișier doar atunci când tu, prin asistentul tău AI, îi ceri lui In The Know să facă ceva.</li></ul>

<h2>Ce face In The Know</h2>
<p>In The Know este un conector pentru asistenți AI precum Claude și ChatGPT. Păstrează o memorie exactă a unei afaceri mici (clienți,
vizite, vânzări, prețuri, costuri, sarcini, obiective, decizii) și îi calculează cifrele. Îl adaugi în asistentul tău la {html.escape(base)}/mcp.</p>

<h2>Unde sunt stocate datele tale</h2>
<p>Într-un fișier numit memory.itk din propriul tău spațiu de stocare în cloud, pe care îl poți vedea, descărca, copia sau șterge oricând.
Când adaugi fișiere (liste de clienți, exporturi, notițe, un export al conversațiilor tale vechi), acestea sunt citite o singură dată pe
serverul nostru, iar conținutul lor este adăugat în acel fișier; fișierele încărcate nu sunt păstrate.</p>

<h2>Ce procesează serverul nostru și cât timp</h2>
<ul><li>Cât timp gestionează o cerere, serverul ține fișierul memoriei tale în memoria de lucru (RAM). Nu este scris niciodată pe discurile
noastre. Rămâne în RAM cel mult 10 minute după ultima ta cerere, ca cererile consecutive să fie rapide, apoi este eliminat.</li>
<li>Fișierele pe care le încarci sunt procesate în RAM în același fel și eliminate după import.</li>
<li>Jurnalele operaționale înregistrează ce funcție a rulat, cât a durat, dacă a funcționat și un hash unidirecțional al identificatorului
contului tău. Nu conțin niciodată conținutul memoriei tale, fișierele, mesajele tale sau vreun token de acces.</li></ul>

<h2>Accesul la contul tău Google sau Microsoft</h2>
<ul><li>Google: cerem permisiunea <i>drive.file</i>, care îi permite lui In The Know să vadă și să modifice doar fișierele pe care le creează
și fișierele pe care le alegi explicit în selectorul de fișiere Google. Nu putem vedea restul Drive-ului tău. Primim și identificatorul și
adresa de e-mail ale contului tău Google, ca să știm care memorie este a ta.</li>
<li>Microsoft: cerem acces la folderul propriu al aplicației In The Know din OneDrive-ul tău (<i>Files.ReadWrite.AppFolder</i>), plus
identificatorul și adresa de e-mail ale contului tău.</li>
<li>Autorizarea primită de la furnizorul tău este criptată și stocată doar în tokenul de conectare pe care asistentul tău AI îl păstrează
pentru tine. Serverul nostru o poate citi când faci o cerere; nu o stocăm nicăieri altundeva.</li>
<li>Poți retrage accesul oricând: pentru Google la myaccount.google.com/permissions, pentru Microsoft la account.live.com/consent/Manage
(sau pe pagina „My Apps” a organizației tale).</li></ul>

<h2>Google API Services: Limited Use</h2>
<p>Utilizarea de către In The Know a informațiilor primite prin API-urile Google și transferul lor către orice altă aplicație respectă
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data Policy</a>, inclusiv
cerințele Limited Use. Folosim datele utilizatorilor Google doar pentru a-ți oferi funcțiile In The Know. Nu le vindem, nu le folosim
pentru publicitate, nu le transferăm altora decât dacă este necesar pentru furnizarea serviciului sau dacă legea o cere și nu le folosim
pentru antrenarea modelelor AI. Nicio persoană din echipa In The Know nu îți citește datele, cu excepția cazurilor în care ne dai
permisiunea explicită pentru suport, din motive de securitate sau acolo unde legea o cere.</p>

<h2>Planuri și plăți</h2>
<p>In The Know are un singur plan plătit (vezi <a href="/pricing?lang=ro">prețurile</a>). Plățile sunt gestionate de <b>Paddle</b>, care este
vânzătorul înregistrat: colectează datele cardului tău, face încasarea, se ocupă de TVA și îți trimite facturile. Nu vedem și nu stocăm
niciodată numărul cardului tău.</p>
<p>Ca să știm dacă salvarea este activă pentru tine, păstrăm o mică înregistrare într-o bază de date din UE (Google Cloud Firestore): un
hash unidirecțional al identificatorului contului tău, starea planului și data de expirare, dacă ai folosit deja perioada de probă
gratuită, numerele de referință Paddle pentru abonament și client și codul promoțional folosit. Nu conține <b>niciun nume, nicio adresă de
e-mail și nimic din memoria afacerii tale</b>. O ștergem la cerere. Codurile promoționale sunt stocate doar ca hash-uri.</p>

<h2>Asistentul tău AI</h2>
<p>Răspunsurile pe care In The Know le dă asistentului tău (de exemplu istoricul unui client când întrebi de el) sunt procesate de
furnizorul acelui asistent (precum Anthropic sau OpenAI), conform propriilor lui condiții de confidențialitate.</p>

<h2>Cine mai este implicat</h2>
<p>Serverul nostru și înregistrarea de facturare rulează la {hosting}. Paddle procesează plățile. Nu vindem și nu partajăm datele tale și
nu folosim trackere de publicitate sau de analiză.</p>

<h2>Drepturile tale</h2>
<p>Conform GDPR, poți cere accesul la datele tale personale, rectificarea, ștergerea, restricționarea sau portabilitatea lor și poți
depune o plângere la autoritatea de protecție a datelor (în România, ANSPDCP). Pentru că memoria ta stă în propriul tău spațiu de
stocare, poți exercita direct majoritatea acestor drepturi: deschizi, editezi, exporți sau ștergi fișierul. În In The Know îi poți cere
și asistentului tău să exporte sau să uite tot ce se știe despre o persoană. Pentru orice altceva, scrie la {contact}.</p>

<h2>Copii</h2>
<p>In The Know este un instrument pentru afaceri și nu este destinat persoanelor sub 16 ani.</p>

<h2>Modificări</h2>
<p>Vom actualiza această pagină când se schimbă ce face serviciul cu datele și vom modifica data de sus.</p>
{_ro_footer("privacy")}"""


LEGAL = {
    "en": {"pricing": ("Pricing", pricing_html), "terms": ("Terms", terms_html),
           "refunds": ("Refunds", refunds_html), "privacy": ("Privacy policy", privacy_html)},
    "ro": {"pricing": ("Prețuri", pricing_html_ro), "terms": ("Termeni", terms_html_ro),
           "refunds": ("Rambursări", refunds_html_ro), "privacy": ("Politica de confidențialitate", privacy_html_ro)},
}
