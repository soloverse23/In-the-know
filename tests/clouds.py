"""In-memory fakes of the Google Drive v3 and Microsoft Graph endpoints cloud.py uses.

They follow the real behaviour that matters to us: bearer tokens that expire, refresh tokens that
can be revoked, Drive versions and trash, appProperties search, Graph eTags with If-Match and
conflictBehavior=fail, and pre-authenticated download URLs.
"""
import itertools
import json
from dataclasses import replace
from typing import Callable, Optional
from urllib.parse import parse_qs

import httpx2 as httpx

from intheknow.auth import google, microsoft

GOOGLE = replace(google("gid", "gsecret"), token_url="https://oauth2.google.test/token")
MICROSOFT = replace(microsoft("mid", "msecret"), token_url="https://login.microsoft.test/token")
PROVIDERS = {"google": GOOGLE, "microsoft": MICROSOFT}


class FakeCloud:
    def __init__(self):
        self.valid: set[str] = set()
        self.revoked: set[str] = set()
        self.refreshes = 0
        self.ids = itertools.count(1)
        self.drive: dict[str, dict] = {}
        self.graph: dict[str, dict] = {}
        self.before_put: Optional[Callable[[], None]] = None
        self.requests: list[str] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport)

    # ---- helpers for tests
    def expire_tokens(self) -> None:
        self.valid.clear()

    def add_drive_file(self, name: str, mime: str, data: bytes = b"", export: Optional[bytes] = None) -> str:
        """A file the owner already has in Drive (reachable once they pick it in the Picker)."""
        f = self._new_drive({"name": name, "mimeType": mime}, data)
        if export is not None:
            f["export"] = export
        return f["id"]

    def drive_memory(self) -> list[dict]:
        return [f for f in self.drive.values() if f.get("appProperties", {}).get("itk") == "memory"]

    # ---- dispatcher
    def handle(self, req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        self.requests.append(f"{req.method} {host}{path}")
        if path.endswith("/token"):
            return self.token(req)
        if host == "download.test":
            item = self.graph.get(path.strip("/"))
            return httpx.Response(200, content=item["data"]) if item else httpx.Response(404)
        if req.headers.get("authorization", "").removeprefix("Bearer ") not in self.valid:
            return httpx.Response(401, json={"error": "invalid token"})
        if host == "www.googleapis.com":
            return self.drive_api(req, path)
        if host == "graph.microsoft.com":
            return self.graph_api(req, path)
        return httpx.Response(404)

    def token(self, req: httpx.Request) -> httpx.Response:
        f = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        if f.get("grant_type") != "refresh_token" or f["refresh_token"] in self.revoked:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.refreshes += 1
        tok = f"at-{self.refreshes}"
        self.valid.add(tok)
        body = {"access_token": tok, "expires_in": 3600}
        if "login.microsoft" in req.url.host:
            body["refresh_token"] = f["refresh_token"] + "+"   # Microsoft rotates
            self.revoked.discard(body["refresh_token"])
        return httpx.Response(200, json=body)

    # ---- Google Drive v3
    def drive_api(self, req: httpx.Request, path: str) -> httpx.Response:
        p = dict(req.url.params)
        if req.method == "GET" and path == "/drive/v3/files":
            q = p["q"]
            if "appProperties has" in q:
                hits = [f for f in self.drive_memory() if not f["trashed"]]
            else:
                hits = [f for f in self.drive.values() if f.get("mimeType") == "application/vnd.google-apps.folder"
                        and f["name"] == "In The Know" and not f["trashed"]]
            hits.sort(key=lambda f: f["modified"], reverse=True)
            return httpx.Response(200, json={"files": [{"id": f["id"], "version": str(f["version"]),
                                                        "modifiedTime": f["modified"]} for f in hits]})
        if req.method == "GET" and path.startswith("/drive/v3/files/") and path.endswith("/export"):
            f = self.drive.get(path.split("/")[-2])
            if f is None or "export" not in f:
                return httpx.Response(404)
            return httpx.Response(200, content=f["export"])
        if req.method == "GET" and path.startswith("/drive/v3/files/"):
            f = self.drive.get(path.rsplit("/", 1)[1])
            if f is None:
                return httpx.Response(404)
            if p.get("alt") == "media":
                return httpx.Response(200, content=f["data"])
            return httpx.Response(200, json={"id": f["id"], "version": str(f["version"]), "trashed": f["trashed"],
                                             "name": f.get("name"), "mimeType": f.get("mimeType"), "size": str(len(f["data"]))})
        if req.method == "POST" and path == "/drive/v3/files":
            meta = json.loads(req.content)
            return httpx.Response(200, json={"id": self._new_drive(meta, b"")["id"]})
        if req.method == "POST" and path == "/upload/drive/v3/files":
            boundary = req.headers["content-type"].split("boundary=")[1].encode()
            parts = req.content.split(b"--" + boundary)
            def body(seg: bytes) -> bytes:
                return seg[seg.index(b"\r\n\r\n") + 4:-2]
            meta, data = json.loads(body(parts[1])), body(parts[2])
            f = self._new_drive(meta, data)
            return httpx.Response(200, json={"id": f["id"], "version": str(f["version"])})
        if req.method == "PATCH" and path.startswith("/upload/drive/v3/files/"):
            if self.before_put:
                hook, self.before_put = self.before_put, None
                hook()
            f = self.drive[path.rsplit("/", 1)[1]]
            f["data"], f["version"], f["modified"] = req.content, f["version"] + 1, next(self.ids)
            return httpx.Response(200, json={"id": f["id"], "version": str(f["version"])})
        return httpx.Response(400)

    def _new_drive(self, meta: dict, data: bytes) -> dict:
        fid = f"g{next(self.ids)}"
        f = {**meta, "id": fid, "data": data, "version": 1, "trashed": False, "modified": next(self.ids)}
        self.drive[fid] = f
        return f

    # ---- Microsoft Graph
    def _item(self, it: dict) -> dict:
        return {"id": it["id"], "name": it["name"], "eTag": f'"{it["id"]},{it["v"]}"',
                "@microsoft.graph.downloadUrl": f"https://download.test/{it['id']}"}

    def graph_api(self, req: httpx.Request, path: str) -> httpx.Response:
        approot = "/v1.0/me/drive/special/approot:/memory.itk"
        by_name = next((i for i in self.graph.values() if i["name"] == "memory.itk"), None)
        if req.method == "GET" and path == approot:
            return httpx.Response(200, json=self._item(by_name)) if by_name else httpx.Response(404)
        if req.method == "GET" and path.startswith("/v1.0/me/drive/items/"):
            it = self.graph.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=self._item(it)) if it else httpx.Response(404)
        if req.method == "PUT" and path == approot + ":/content":
            if by_name and req.url.params.get("@microsoft.graph.conflictBehavior") == "fail":
                return httpx.Response(409, json={"error": {"code": "nameAlreadyExists"}})
            iid = f"m{next(self.ids)}"
            self.graph[iid] = {"id": iid, "name": "memory.itk", "data": req.content, "v": 1}
            return httpx.Response(201, json=self._item(self.graph[iid]))
        if req.method == "PUT" and path.startswith("/v1.0/me/drive/items/") and path.endswith("/content"):
            if self.before_put:
                hook, self.before_put = self.before_put, None
                hook()
            it = self.graph.get(path.split("/")[-2])
            if it is None:
                return httpx.Response(404)
            if req.headers.get("if-match") and req.headers["if-match"] != self._item(it)["eTag"]:
                return httpx.Response(412, json={"error": {"code": "resourceModified"}})
            it["data"], it["v"] = req.content, it["v"] + 1
            return httpx.Response(200, json=self._item(it))
        return httpx.Response(400)
