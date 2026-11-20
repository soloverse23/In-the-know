"""Logging for the hosted connector: operations only, never business content.

- Libraries that log payloads or URLs (sse_starlette response chunks, httpx request URLs that can carry
  pre-authenticated download links, MCP internals) are capped at WARNING whatever level is asked for.
- A redaction filter on every handler masks sealed tokens, bearer tokens and secrets in query strings.
- Each tool call writes one line: tool, duration, ok/error, hashed owner. No arguments, no results.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sys

CAPPED = ("httpx2", "httpx", "httpcore", "sse_starlette", "mcp", "uvicorn.access", "asyncio", "openpyxl")
CALLS = logging.getLogger("intheknow.calls")

_PATTERNS = [
    (re.compile(r"itk1\.[A-Za-z0-9]+\.[A-Za-z0-9_\-]+"), "itk1.[sealed]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-~+/=]+"), "Bearer [redacted]"),
    (re.compile(r"(?i)\b(code|t|state|req|token|access_token|refresh_token|id_token|client_secret|tempauth|sig)=[^&\s\"']+"), r"\1=[redacted]"),
]


def redact(text: str) -> str:
    for rx, sub in _PATTERNS:
        text = rx.sub(sub, text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        clean = redact(msg)
        if clean != msg or record.args:
            record.msg, record.args = clean, ()
        if record.exc_info and record.exc_info[1] is not None:   # exception text may quote a URL
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def configure(level: str = "INFO", stream=None) -> logging.Handler:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in CAPPED:
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


def owner_hash(subject: str) -> str:
    return hashlib.sha256(f"itk-owner:{subject}".encode()).hexdigest()[:12]


def log_call(tool: str, ms: float, ok: bool, owner: str = "") -> None:
    CALLS.info("tool=%s ms=%d ok=%s owner=%s", tool, ms, "yes" if ok else "no", owner or "-")
