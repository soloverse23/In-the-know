"""The upload page: how files reach a hosted memory.

The assistant cannot hand us the owner's files, and drive.file access cannot read files the
owner puts into our folder. So upload_files gives the owner a link to a page on this server:

- "Choose files" works everywhere: on a phone the file chooser also offers Drive, OneDrive, iCloud.
- "Choose from Google Drive" (Google Picker, when configured) also reads Google Sheets and Docs.
  Picking a file is what grants In The Know access to that one file.

Uploaded bytes are processed in RAM straight into the owner's memory file; nothing is kept.
The link is a sealed ticket (30 minutes) and is bound by cookie to the first browser that opens it.
"""
from __future__ import annotations

import html
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from .auth import CSS, LANGS, MARK, NO_STORE
from .seal import Sealer, SealError

TICKET_TTL = 30 * 60
MAX_FILES = 20
MAX_FILE = 25 * 1024 * 1024
MAX_TOTAL = 60 * 1024 * 1024
ACCEPT = ".csv,.tsv,.xlsx,.xlsm,.md,.txt,.zip,.json"
PICK_MIMES = ",".join([
    "text/csv", "text/tab-separated-values", "text/plain", "text/markdown", "application/zip", "application/json",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.spreadsheet", "application/vnd.google-apps.document"])
EXPORT = {"application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
          "application/vnd.google-apps.document": ("text/plain", ".txt")}


@dataclass(frozen=True)
class PickerConfig:
    api_key: str
    app_id: str                    # Google Cloud project number


TEXT = {
    "en": {"title": "Add files to In The Know", "lead": "Customer lists, bookings or sales exports, price lists, expenses, notes, or your Claude / ChatGPT export (.zip).",
           "choose": "Choose files", "drive": "Choose from Google Drive", "send": "Add to my memory", "busy": "Adding…",
           "fine": "Files are read once and added to the memory in your own storage. In The Know keeps no copy.",
           "done": "Added to your memory", "back": "Go back to Claude and say: I added my files.", "more": "Add more files",
           "rows": "{n} new", "updated": "{n} updated", "dup": "{n} already there", "already": "already imported",
           "skipped": "not a supported file", "chats": "{n} messages from old chats, ready to go through",
           "q": "{n} questions to check in Claude",
           "expired": "This link has expired. Ask Claude for a new upload link.",
           "taken": "This link was opened in another browser. Ask Claude for a new upload link.",
           "setup": "Set up your business in Claude first, then add files.",
           "too_big": "Too many or too large files (up to 20 files, 25 MB each).",
           "failed": "Something went wrong while adding the files. Nothing was changed. Please try again."},
    "fr": {"title": "Ajouter des fichiers à In The Know", "lead": "Listes de clients, exports de rendez-vous ou de ventes, tarifs, dépenses, notes, ou votre export Claude / ChatGPT (.zip).",
           "choose": "Choisir des fichiers", "drive": "Choisir dans Google Drive", "send": "Ajouter à ma mémoire", "busy": "Ajout…",
           "fine": "Les fichiers sont lus une fois et ajoutés à la mémoire dans votre propre stockage. In The Know n'en garde aucune copie.",
           "done": "Ajouté à votre mémoire", "back": "Revenez dans Claude et dites : j'ai ajouté mes fichiers.", "more": "Ajouter d'autres fichiers",
           "rows": "{n} nouveaux", "updated": "{n} mis à jour", "dup": "{n} déjà présents", "already": "déjà importé",
           "skipped": "type de fichier non pris en charge", "chats": "{n} messages d'anciennes conversations, prêts à être parcourus",
           "q": "{n} questions à vérifier dans Claude",
           "expired": "Ce lien a expiré. Demandez un nouveau lien à Claude.",
           "taken": "Ce lien a été ouvert dans un autre navigateur. Demandez un nouveau lien à Claude.",
           "setup": "Configurez d'abord votre entreprise dans Claude, puis ajoutez des fichiers.",
           "too_big": "Trop de fichiers ou fichiers trop volumineux (20 fichiers, 25 Mo chacun maximum).",
           "failed": "Un problème est survenu pendant l'ajout. Rien n'a été modifié. Veuillez réessayer."},
    "ro": {"title": "Adaugă fișiere în In The Know", "lead": "Liste de clienți, exporturi de programări sau vânzări, liste de prețuri, cheltuieli, notițe sau exportul tău din Claude / ChatGPT (.zip).",
           "choose": "Alege fișiere", "drive": "Alege din Google Drive", "send": "Adaugă în memorie", "busy": "Se adaugă…",
           "fine": "Fișierele sunt citite o singură dată și adăugate în memoria din propriul tău spațiu de stocare. In The Know nu păstrează nicio copie.",
           "done": "Adăugat în memoria ta", "back": "Întoarce-te în Claude și spune: am adăugat fișierele.", "more": "Adaugă alte fișiere",
           "rows": "{n} noi", "updated": "{n} actualizate", "dup": "{n} existau deja", "already": "importat deja",
           "skipped": "tip de fișier neacceptat", "chats": "{n} mesaje din conversații vechi, gata de parcurs",
           "q": "{n} întrebări de verificat în Claude",
           "expired": "Linkul a expirat. Cere-i lui Claude un link nou.",
           "taken": "Linkul a fost deschis în alt browser. Cere-i lui Claude un link nou.",
           "setup": "Configurează mai întâi afacerea în Claude, apoi adaugă fișiere.",
           "too_big": "Prea multe fișiere sau fișiere prea mari (maximum 20 de fișiere, 25 MB fiecare).",
           "failed": "Ceva n-a mers la adăugare. Nu s-a schimbat nimic. Încearcă din nou."},
    "de": {"title": "Dateien zu In The Know hinzufügen", "lead": "Kundenlisten, Termin- oder Kassenexporte, Preislisten, Ausgaben, Notizen oder Ihr Claude- / ChatGPT-Export (.zip).",
           "choose": "Dateien auswählen", "drive": "Aus Google Drive auswählen", "send": "Zum Gedächtnis hinzufügen", "busy": "Wird hinzugefügt…",
           "fine": "Die Dateien werden einmal gelesen und dem Gedächtnis in Ihrem eigenen Speicher hinzugefügt. In The Know behält keine Kopie.",
           "done": "Zu Ihrem Gedächtnis hinzugefügt", "back": "Gehen Sie zurück zu Claude und sagen Sie: Ich habe meine Dateien hinzugefügt.", "more": "Weitere Dateien hinzufügen",
           "rows": "{n} neu", "updated": "{n} aktualisiert", "dup": "{n} schon vorhanden", "already": "bereits importiert",
           "skipped": "Dateityp nicht unterstützt", "chats": "{n} Nachrichten aus alten Chats, bereit zum Durchgehen",
           "q": "{n} Fragen zum Prüfen in Claude",
           "expired": "Dieser Link ist abgelaufen. Bitten Sie Claude um einen neuen Link.",
           "taken": "Dieser Link wurde in einem anderen Browser geöffnet. Bitten Sie Claude um einen neuen Link.",
           "setup": "Richten Sie zuerst Ihr Geschäft in Claude ein und fügen Sie dann Dateien hinzu.",
           "too_big": "Zu viele oder zu große Dateien (bis zu 20 Dateien, je 25 MB).",
           "failed": "Beim Hinzufügen ist etwas schiefgegangen. Es wurde nichts geändert. Bitte versuchen Sie es erneut."},
}
PAGE_CSS = CSS + """
main{max-width:460px;text-align:left}h1,main>p{text-align:center}main>svg{display:block;margin:0 auto}
.drop{display:block;border:2px dashed var(--line);border-radius:14px;padding:22px 16px;text-align:center;cursor:pointer;margin:0 0 12px}
.drop:focus-within,.drop.over{border-color:var(--accent)}.drop input{display:none}
.drop b{display:block;font-size:16px;margin-bottom:4px}#names{color:var(--muted);font-size:14px;word-break:break-word}
button{width:100%;min-height:52px;border-radius:12px;font:inherit;font-weight:600;cursor:pointer;margin:0 0 12px}
button.primary{background:var(--ink);color:var(--bg);border:0}button.primary:disabled{opacity:.45;cursor:default}
button.alt{background:var(--card);color:var(--ink);border:1px solid var(--line)}
ul{list-style:none;padding:0;margin:0 0 20px}li{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:0 0 8px}
li b{display:block;word-break:break-word}li span{color:var(--muted);font-size:14px}
a.more{display:block;text-align:center;color:var(--ink);font-weight:600;margin-top:8px}
.note{background:color-mix(in srgb,var(--accent) 12%,transparent);border-radius:12px;padding:12px 14px;margin:0 0 16px;text-align:center}
"""


def _t(lang: str) -> dict:
    return TEXT.get(lang, TEXT["en"])


def _page(lang: str, inner: str, status: int = 200, script: str = "", picker: bool = False) -> HTMLResponse:
    t = _t(lang)
    nonce = secrets.token_urlsafe(12)
    csp = ["default-src 'none'", "style-src 'unsafe-inline'", "img-src data: https:", "form-action 'self'",
           "frame-ancestors 'none'", "base-uri 'none'"]
    if script:
        src = f"'nonce-{nonce}'" + (" https://apis.google.com https://www.gstatic.com" if picker else "")
        csp.append(f"script-src {src}")
    if picker:
        csp += ["frame-src https://docs.google.com https://drive.google.com https://accounts.google.com https://content.googleapis.com",
                "connect-src https://*.googleapis.com https://*.google.com"]
    body = (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex"><title>{html.escape(t["title"])}</title><style>{PAGE_CSS}</style></head>'
            f'<body><main>{MARK}{inner}</main>' + (f'<script nonce="{nonce}">{script}</script>' if script else "") + "</body></html>")
    return HTMLResponse(body, status_code=status, headers={**NO_STORE, "X-Frame-Options": "DENY", "Content-Security-Policy": "; ".join(csp)})


def _message(lang: str, key: str, status: int) -> HTMLResponse:
    t = _t(lang)
    return _page(lang, f"<h1>{html.escape(t['title'])}</h1><p>{html.escape(t[key])}</p>", status)


class Uploads:
    def __init__(self, sealer: Sealer, base_url: str, engine_for: Callable[[dict], Any], picker: Optional[PickerConfig] = None):
        self.sealer, self.base, self.engine_for, self.picker = sealer, base_url.rstrip("/"), engine_for, picker
        self._bound: dict[str, tuple[str, float]] = {}      # ticket id -> (browser nonce, expiry)
        self._lock = threading.Lock()

    # ---- link for the assistant
    def link(self, identity: dict) -> str:
        ticket = self.sealer.seal("upload", {"u": identity, "j": secrets.token_urlsafe(12)}, TICKET_TTL)
        return f"{self.base}/upload?" + urlencode({"t": ticket})

    def routes(self) -> list[tuple[str, list[str], Callable]]:
        return [("/upload", ["GET", "POST"], self.handle), ("/upload/drive", ["POST"], self.handle_drive)]

    # ---- ticket + browser binding
    def _open(self, request: Request, ticket: str, bind: bool = False) -> tuple[Optional[dict], Optional[str], Optional[str]]:
        """(ticket body, cookie value to set, error key). Only opening the page (bind=True) may claim a fresh link."""
        try:
            body = self.sealer.open("upload", ticket)
        except SealError:
            return None, None, "expired"
        jti = body["j"]
        cookie = request.cookies.get(f"itk_up_{jti}")
        now = time.time()
        with self._lock:
            for k in [k for k, (_, e) in self._bound.items() if e < now]:
                del self._bound[k]
            bound = self._bound.get(jti)
            if bound is None:
                if not bind:
                    return None, None, "taken"
                nonce = secrets.token_urlsafe(24)
                self._bound[jti] = (nonce, body["exp"])
                return body, nonce, None
        if cookie and secrets.compare_digest(cookie, bound[0]):
            return body, None, None
        return None, None, "taken"

    def _cookie(self, resp: Response, body: dict, value: Optional[str]) -> Response:
        if value:
            resp.set_cookie(f"itk_up_{body['j']}", value, max_age=TICKET_TTL, path="/upload", httponly=True,
                            secure=self.base.startswith("https://"), samesite="lax")
        return resp

    def _engine(self, body: dict):
        return self.engine_for(body["u"])

    # ---- pages
    async def handle(self, request: Request) -> Response:
        if request.method == "POST":
            return await self._post_files(request)
        ticket = request.query_params.get("t", "")
        body, set_cookie, err = self._open(request, ticket, bind=True)
        lang = await self._lang(body)
        if err:
            return _message(lang, err, 403 if err == "taken" else 410)
        return self._cookie(await self._form(lang, ticket, body), body, set_cookie)

    async def _lang(self, body: Optional[dict]) -> str:
        if not body:
            return "en"
        try:
            lang = await anyio.to_thread.run_sync(lambda: self._engine(body).lang)
        except Exception:  # noqa: BLE001
            return "en"
        return lang if lang in LANGS else "en"

    async def _form(self, lang: str, ticket: str, body: dict) -> HTMLResponse:
        t = _t(lang)
        picker_token = await self._picker_token(body)
        tk = html.escape(ticket)
        drive_btn = f'<button type="button" class="alt" id="drive">{html.escape(t["drive"])}</button>' if picker_token else ""
        inner = (f'<h1>{html.escape(t["title"])}</h1><p>{html.escape(t["lead"])}</p>'
                 f'<form id="f" method="post" action="/upload" enctype="multipart/form-data"><input type="hidden" name="t" value="{tk}">'
                 f'<label class="drop" id="drop"><input type="file" name="files" id="files" multiple accept="{ACCEPT}">'
                 f'<b>{html.escape(t["choose"])}</b><span id="names"></span></label>'
                 f'<button class="primary" id="send" disabled>{html.escape(t["send"])}</button></form>{drive_btn}'
                 f'<form id="d" method="post" action="/upload/drive"><input type="hidden" name="t" value="{tk}"><input type="hidden" name="ids" id="ids"></form>'
                 f'<small>{html.escape(t["fine"])}</small>')
        script = ("const f=document.getElementById('files'),s=document.getElementById('send'),n=document.getElementById('names'),dr=document.getElementById('drop');"
                  "const show=()=>{n.textContent=[...f.files].map(x=>x.name).join(', ');s.disabled=!f.files.length};f.addEventListener('change',show);"
                  "['dragover','dragenter'].forEach(e=>dr.addEventListener(e,v=>{v.preventDefault();dr.classList.add('over')}));"
                  "['dragleave','drop'].forEach(e=>dr.addEventListener(e,()=>dr.classList.remove('over')));"
                  "dr.addEventListener('drop',v=>{v.preventDefault();f.files=v.dataTransfer.files;show()});"
                  f"document.getElementById('f').addEventListener('submit',()=>{{s.disabled=true;s.textContent={json.dumps(t['busy'])}}});")
        if picker_token and self.picker:
            script += (f"const TOKEN={json.dumps(picker_token)},KEY={json.dumps(self.picker.api_key)},APP={json.dumps(self.picker.app_id)},LANG={json.dumps(lang)};"
                       "function pick(){gapi.load('picker',()=>{const v=new google.picker.DocsView(google.picker.ViewId.DOCS)"
                       f".setMimeTypes({json.dumps(PICK_MIMES)}).setIncludeFolders(true);"
                       "new google.picker.PickerBuilder().addView(v).enableFeature(google.picker.Feature.MULTISELECT_ENABLED)"
                       ".setOAuthToken(TOKEN).setDeveloperKey(KEY).setAppId(APP).setLocale(LANG).setCallback(d=>{"
                       "if(d.action===google.picker.Action.PICKED){document.getElementById('ids').value=d.docs.map(x=>x.id).join(',');"
                       "document.getElementById('d').submit()}}).build().setVisible(true)})}"
                       "const g=document.createElement('script');g.src='https://apis.google.com/js/api.js';"
                       "g.onload=()=>document.getElementById('drive').addEventListener('click',pick);document.body.appendChild(g);")
        return _page(lang, inner, script=script, picker=bool(picker_token))

    async def _picker_token(self, body: dict) -> Optional[str]:
        if not self.picker or body["u"].get("p") != "google":
            return None
        from .cloud import GoogleDriveStore
        try:
            store = (await anyio.to_thread.run_sync(lambda: self._engine(body))).store
            if not isinstance(store, GoogleDriveStore):
                return None
            return await anyio.to_thread.run_sync(store.s.token)
        except Exception:  # noqa: BLE001 - no picker; device upload still works
            return None

    # ---- imports
    async def _post_files(self, request: Request) -> Response:
        if int(request.headers.get("content-length") or 0) > MAX_TOTAL:
            return _message("en", "too_big", 413)
        try:
            form = await request.form(max_files=MAX_FILES, max_fields=10, max_part_size=MAX_FILE)
        except Exception:  # noqa: BLE001 - too many / too large parts
            return _message("en", "too_big", 413)
        ticket = str(form.get("t") or "")
        body, _, err = self._open(request, ticket)
        lang = await self._lang(body)
        if err:
            return _message(lang, err, 403 if err == "taken" else 410)
        files = []
        for up in form.getlist("files"):
            if not getattr(up, "filename", None):
                continue
            data = await up.read()
            if len(data) > MAX_FILE:
                return _message(lang, "too_big", 413)
            files.append((up.filename, data))
        if not files:
            return self._back(lang, ticket)
        return await self._import(lang, ticket, body, files)

    async def handle_drive(self, request: Request) -> Response:
        form = await request.form(max_files=0, max_fields=5)
        ticket = str(form.get("t") or "")
        body, _, err = self._open(request, ticket)
        lang = await self._lang(body)
        if err:
            return _message(lang, err, 403 if err == "taken" else 410)
        ids = [i for i in str(form.get("ids") or "").split(",") if i][:MAX_FILES]
        if not ids or body["u"].get("p") != "google":
            return self._back(lang, ticket)
        try:
            files = await anyio.to_thread.run_sync(lambda: self._download(body, ids))
        except _TooBig:
            return _message(lang, "too_big", 413)
        except Exception:  # noqa: BLE001
            return _message(lang, "failed", 502)
        return await self._import(lang, ticket, body, files)

    def _download(self, body: dict, ids: list[str]) -> list[tuple[str, bytes]]:
        from .cloud import GoogleDriveStore, _check
        store = self._engine(body).store
        if not isinstance(store, GoogleDriveStore):
            raise RuntimeError("not a Google Drive memory")
        s, api, out = store.s, GoogleDriveStore.API, []
        for fid in ids:                                  # name and type come from Drive, never from the browser
            meta = _check(s.request("GET", f"{api}/files/{fid}", params={"fields": "id,name,mimeType,size"}), "Google Drive").json()
            if int(meta.get("size") or 0) > MAX_FILE:
                raise _TooBig()
            if meta["mimeType"] in EXPORT:
                mime, ext = EXPORT[meta["mimeType"]]
                r = _check(s.request("GET", f"{api}/files/{fid}/export", params={"mimeType": mime}), "Google Drive export")
                name = meta["name"] if meta["name"].lower().endswith(ext) else meta["name"] + ext
            else:
                r = _check(s.request("GET", f"{api}/files/{fid}", params={"alt": "media"}), "Google Drive download")
                name = meta["name"]
            if len(r.content) > MAX_FILE:
                raise _TooBig()
            out.append((name, r.content))
        return out

    async def _import(self, lang: str, ticket: str, body: dict, files: list[tuple[str, bytes]]) -> Response:
        try:
            engine = await anyio.to_thread.run_sync(lambda: self._engine(body))
            if not engine.is_setup:
                return _message(lang, "setup", 409)
            _, env = await anyio.to_thread.run_sync(lambda: engine.call_view("import_uploads", {"files": files}))
            def pending() -> int:
                with engine._lock:
                    return engine.conn.execute("SELECT COUNT(*) FROM history_queue WHERE status='pending'").fetchone()[0]
            chats = await anyio.to_thread.run_sync(pending)
        except Exception:  # noqa: BLE001 - the call rolled back; nothing changed
            return _message(lang, "failed", 500)
        return self._results(lang, ticket, env, chats)

    def _results(self, lang: str, ticket: str, env: dict, chats: int) -> HTMLResponse:
        t = _t(lang)
        items = next((b["items"] for b in env.get("blocks", []) if b.get("type") == "files"), [])
        lis, questions = [], 0
        for it in items:
            questions += int(it.get("questions") or 0)
            label = (it.get("entity_label") or {}).get(lang) or ""
            if it["entity"] == "skipped":
                detail = t["skipped"]
            elif it.get("already"):
                detail = t["already"]
            else:
                parts = [t["rows"].format(n=it["loaded"])]
                if it.get("updated"):
                    parts.append(t["updated"].format(n=it["updated"]))
                if it.get("duplicate"):
                    parts.append(t["dup"].format(n=it["duplicate"]))
                detail = (label + " · " if label else "") + " · ".join(parts)
            lis.append(f"<li><b>{html.escape(it['name'])}</b><span>{html.escape(detail)}</span></li>")
        if chats:
            lis.append(f"<li><b>Claude / ChatGPT</b><span>{html.escape(t['chats'].format(n=chats))}</span></li>")
        q = f'<div class="note">{html.escape(t["q"].format(n=questions))}</div>' if questions else ""
        more = f'/upload?{urlencode({"t": ticket})}'
        inner = (f'<h1>{html.escape(t["done"])}</h1><ul>{"".join(lis)}</ul>{q}<div class="note">{html.escape(t["back"])}</div>'
                 f'<a class="more" href="{html.escape(more)}">{html.escape(t["more"])}</a>')
        return _page(lang, inner)

    def _back(self, lang: str, ticket: str) -> Response:
        return Response(status_code=303, headers={"Location": f"/upload?{urlencode({'t': ticket})}", **NO_STORE})


class _TooBig(Exception):
    pass
