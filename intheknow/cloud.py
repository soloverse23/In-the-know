"""The owner's memory file in their own Google Drive or Microsoft OneDrive.

- Google Drive (scope drive.file): a visible folder "In The Know" holding "memory.itk". The file is
  tagged with appProperties itk=memory, so it is found again even if the owner renames or moves it.
- OneDrive (scope Files.ReadWrite.AppFolder): "memory.itk" in the app folder, which the owner sees
  as Apps/In The Know. Uploads use If-Match on the eTag, so a stale write is refused by Microsoft.

Each owner gets a ProviderSession that turns their refresh token into short-lived access tokens,
kept in RAM only. Nothing here touches our disk.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Optional

import httpx2 as httpx

from .auth import IdentityProvider
from .store import Conflict, Store

FILE_NAME = "memory.itk"
FOLDER_NAME = "In The Know"
MIME = "application/x-sqlite3"


class StorageAccessLost(Exception):
    """The owner removed In The Know's access at Google / Microsoft."""

    def __init__(self, where: str):
        super().__init__(f"In The Know can no longer reach the owner's {where} storage (access was removed or expired). "
                         "Ask them to reconnect In The Know in their assistant's connector settings; their data is safe in their account.")


class ProviderSession:
    """One owner's authenticated HTTP access to their provider. Thread-safe."""

    def __init__(self, provider: IdentityProvider, refresh_token: str, http: httpx.Client):
        self.provider, self.refresh_token, self.http = provider, refresh_token, http
        self._token: Optional[str] = None
        self._exp = 0.0
        self._lock = threading.Lock()

    def _refresh(self) -> str:
        form = {"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                "client_id": self.provider.client_id, "client_secret": self.provider.client_secret}
        if self.provider.send_scope_on_token:
            form["scope"] = " ".join(self.provider.scopes)
        r = self.http.post(self.provider.token_url, data=form, headers={"Accept": "application/json"})
        body = r.json() if r.content else {}
        if r.status_code >= 400 or "access_token" not in body:
            if body.get("error") in ("invalid_grant", "unauthorized_client", "interaction_required", "consent_required"):
                raise StorageAccessLost(self.provider.label)
            raise RuntimeError(f"{self.provider.label} sign-in service failed ({r.status_code})")
        if body.get("refresh_token"):                      # Microsoft rotates refresh tokens
            self.refresh_token = body["refresh_token"]
        self._token = body["access_token"]
        self._exp = time.monotonic() + int(body.get("expires_in", 3600)) - 60
        return self._token

    def token(self, force: bool = False) -> str:
        with self._lock:
            if force or self._token is None or time.monotonic() >= self._exp:
                return self._refresh()
            return self._token

    def request(self, method: str, url: str, *, auth: bool = True, **kw) -> httpx.Response:
        """Authenticated request: refreshes once on 401, retries once on 429/5xx."""
        headers = dict(kw.pop("headers", None) or {})
        force = False
        for attempt in range(3):
            h = dict(headers)
            if auth:
                h["Authorization"] = f"Bearer {self.token(force=force)}"
            r = self.http.request(method, url, headers=h, **kw)
            force = False
            if r.status_code == 401 and auth and attempt == 0:
                force = True                             # token revoked or expired early: get a new one once
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            return r
        return r


def _check(r: httpx.Response, what: str) -> httpx.Response:
    if r.status_code == 401:
        raise StorageAccessLost(what)
    if r.status_code >= 400:
        raise RuntimeError(f"{what} failed ({r.status_code})")
    return r


# ---------------------------------------------------------------- Google Drive
class GoogleDriveStore(Store):
    API = "https://www.googleapis.com/drive/v3"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3"
    where = "Google Drive"

    def __init__(self, session: ProviderSession):
        self.s = session
        self.file_id: Optional[str] = None
        self.trashed = False

    def _find(self) -> Optional[dict]:
        q = "appProperties has { key='itk' and value='memory' } and trashed=false"
        r = _check(self.s.request("GET", f"{self.API}/files", params={
            "q": q, "spaces": "drive", "orderBy": "modifiedTime desc", "pageSize": "10",
            "fields": "files(id,version,modifiedTime)"}), "Google Drive search")
        files = r.json().get("files", [])
        return files[0] if files else None               # several copies: the most recently changed one

    def _meta(self) -> Optional[dict]:
        if self.file_id is None:
            f = self._find()
            if f is None:
                return None
            self.file_id = f["id"]
        r = self.s.request("GET", f"{self.API}/files/{self.file_id}", params={"fields": "id,version,trashed"})
        if r.status_code == 404:
            self.trashed = False
            return None
        meta = _check(r, "Google Drive").json()
        self.trashed = bool(meta.get("trashed"))
        return None if self.trashed else meta

    def version(self) -> Optional[str]:
        m = self._meta()
        return str(m["version"]) if m else None

    def load(self) -> tuple[Optional[bytes], Optional[str]]:
        m = self._meta()                                 # version first: a change in between only causes a safe retry
        if m is None:
            return None, None
        r = _check(self.s.request("GET", f"{self.API}/files/{self.file_id}", params={"alt": "media"}), "Google Drive download")
        return r.content, str(m["version"])

    def _folder(self) -> str:
        q = f"name='{FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        r = _check(self.s.request("GET", f"{self.API}/files", params={"q": q, "spaces": "drive", "fields": "files(id)"}),
                   "Google Drive search")
        found = r.json().get("files", [])
        if found:
            return found[0]["id"]
        r = _check(self.s.request("POST", f"{self.API}/files", params={"fields": "id"},
                                  json={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"}),
                   "Google Drive folder")
        return r.json()["id"]

    def save(self, data: bytes, base_version: Optional[str]) -> str:
        if self.version() != base_version:
            raise Conflict(self.where)
        if self.file_id is None or self.trashed:
            meta = {"name": FILE_NAME, "parents": [self._folder()], "mimeType": MIME, "appProperties": {"itk": "memory"},
                    "description": "The memory of your business, kept by In The Know. Do not edit by hand."}
            boundary = uuid.uuid4().hex
            body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                    f"--{boundary}\r\nContent-Type: {MIME}\r\n\r\n").encode() + data + f"\r\n--{boundary}--".encode()
            r = _check(self.s.request("POST", f"{self.UPLOAD}/files", params={"uploadType": "multipart", "fields": "id,version"},
                                      content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"}),
                       "Google Drive upload")
        else:
            r = _check(self.s.request("PATCH", f"{self.UPLOAD}/files/{self.file_id}", params={"uploadType": "media", "fields": "id,version"},
                                      content=data, headers={"Content-Type": MIME}), "Google Drive upload")
        out = r.json()
        self.file_id, self.trashed = out["id"], False
        return str(out["version"])

    def missing_message(self) -> str:
        if self.trashed:
            return ("The business memory file (In The Know/memory.itk) is in the owner's Google Drive trash. "
                    "Ask them to restore it from the trash; nothing was lost.")
        return "The business memory file was removed from the owner's Google Drive."


# ---------------------------------------------------------------- OneDrive
class OneDriveStore(Store):
    API = "https://graph.microsoft.com/v1.0"
    where = "OneDrive"

    def __init__(self, session: ProviderSession):
        self.s = session
        self.item_id: Optional[str] = None

    def _meta(self) -> Optional[dict]:
        url = (f"{self.API}/me/drive/items/{self.item_id}" if self.item_id
               else f"{self.API}/me/drive/special/approot:/{FILE_NAME}")
        r = self.s.request("GET", url)
        if r.status_code == 404:
            return None
        m = _check(r, "OneDrive").json()
        self.item_id = m["id"]
        return m

    def version(self) -> Optional[str]:
        m = self._meta()
        return m["eTag"] if m else None

    def load(self) -> tuple[Optional[bytes], Optional[str]]:
        m = self._meta()
        if m is None:
            return None, None
        url = m.get("@microsoft.graph.downloadUrl")      # pre-authenticated, short-lived
        r = (self.s.request("GET", url, auth=False) if url
             else self.s.request("GET", f"{self.API}/me/drive/items/{self.item_id}/content", follow_redirects=True))
        return _check(r, "OneDrive download").content, m["eTag"]

    def save(self, data: bytes, base_version: Optional[str]) -> str:
        if base_version is None:
            r = self.s.request("PUT", f"{self.API}/me/drive/special/approot:/{FILE_NAME}:/content",
                               params={"@microsoft.graph.conflictBehavior": "fail"}, content=data,
                               headers={"Content-Type": MIME})
        else:
            if self.item_id is None and self._meta() is None:
                raise Conflict(self.where)
            r = self.s.request("PUT", f"{self.API}/me/drive/items/{self.item_id}/content", content=data,
                               headers={"Content-Type": MIME, "If-Match": base_version})
        if r.status_code in (409, 412):
            raise Conflict(self.where)
        m = _check(r, "OneDrive upload").json()
        self.item_id = m["id"]
        return m["eTag"]

    def missing_message(self) -> str:
        return ("The business memory file (Apps/In The Know/memory.itk) is gone from the owner's OneDrive. "
                "Ask them to restore it from the OneDrive recycle bin; nothing was lost.")


# ---------------------------------------------------------------- factory
def cloud_store_factory(providers: dict[str, IdentityProvider], http: Optional[httpx.Client] = None) -> Callable[[dict[str, Any]], Store]:
    client = http or httpx.Client(timeout=30)
    kinds = {"google": GoogleDriveStore, "microsoft": OneDriveStore}

    def make(identity: dict[str, Any]) -> Store:
        p = providers.get(identity["p"])
        if p is None or p.name not in kinds:
            raise StorageAccessLost(identity["p"])
        return kinds[p.name](ProviderSession(p, identity["rt"], client))
    return make
