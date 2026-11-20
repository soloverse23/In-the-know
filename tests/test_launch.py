"""Launch readiness: logs carry no business content or secrets, public pages, container settings."""
import io
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from clouds import FakeCloud
from test_hosted import BASE
from test_uploads import U, demo_files, link, make, owner
from intheknow import logs

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def captured():
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, {n: logging.getLogger(n).level for n in logs.CAPPED})
    buf = io.StringIO()
    logs.configure("DEBUG", buf)                     # even at DEBUG, nothing sensitive may come out
    yield buf
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    for n, lvl in saved[2].items():
        logging.getLogger(n).setLevel(lvl)


def test_logs_hold_no_content_or_secrets(tmp_path, captured):
    cloud = FakeCloud()
    app, g = make(tmp_path, cloud, picker=U.PickerConfig("api-key", "123"))
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        m = owner(c, g)
        m.call("capture", {"items": [{"type": "customer_info", "quote": "Ioana Secretescu 0722 111 222",
                                      "customer": "Ioana Secretescu", "phone": "0722111222"}]})
        url = link(m)
        c.get(url)
        c.post("/upload", data={"t": url.split("t=")[1]}, files=demo_files())
        m.call("get_context", {})
    out = captured.getvalue()
    for secret in ("Secretescu", "0722111222", "+40722111222", "Mihaela", "itk1.k1.", "rt-owner", "at-1", "csecret", "api-key"):
        assert secret not in out, secret
    calls = [l for l in out.splitlines() if "intheknow.calls" in l]
    assert any("tool=capture" in l and "ok=yes" in l for l in calls)
    assert all(re.search(r"owner=[0-9a-f]{12}$", l) for l in calls)        # hashed owner, nothing else


def test_redact():
    s = logs.redact("GET https://x/upload?t=itk1.k1.abc_DEF-1&code=xyz&state=s1 Bearer eyJabc.def tempauth=Zm9v")
    assert "abc_DEF" not in s and "xyz" not in s and "s1" not in s and "eyJ" not in s and "Zm9v" not in s


def test_public_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("ITK_OPERATOR", "Aurora Digital SRL")
    monkeypatch.setenv("ITK_CONTACT", "privacy@example.com")
    app, _ = make(tmp_path)
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        home = c.get("/", headers={"Accept-Language": "ro"})
        assert home.status_code == 200 and f"{BASE}/mcp" in home.text and "Memoria afacerii tale" in home.text
        assert 'href="/privacy"' in home.text and "privacy@example.com" in home.text
        assert home.headers["strict-transport-security"].startswith("max-age=")
        p = c.get("/privacy")
        assert "Aurora Digital SRL" in p.text and "Limited Use" in p.text and "drive.file" in p.text
        assert "Files.ReadWrite.AppFolder" in p.text and "never written to our disks" in p.text


def test_container_settings():
    ignore = (ROOT / ".dockerignore").read_text().split()
    assert {".env", ".dev-memories", "data", "tests"} <= set(ignore)
    docker = (ROOT / "Dockerfile").read_text()
    assert "USER itk" in docker and "VOLUME" not in docker
    r = subprocess.run([sys.executable, "-m", "intheknow.hosted"], capture_output=True, text=True, cwd=ROOT,
                       env={"PATH": "/usr/bin:/bin"}, timeout=60)
    assert r.returncode != 0 and "ITK_BASE_URL" in r.stderr
