"""Where one business's memory file lives.

The engine always works on an in-memory SQLite database. A Store only moves the whole file in
and out as bytes, so the same engine runs on a local file (desktop extension, tests) or on a file
in the owner's own Google Drive / OneDrive (hosted connector) without anything touching our disk.

Versions are opaque strings. `save` takes the version the caller loaded and raises Conflict if
the file changed since, so two devices writing at once never silently overwrite each other.
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Optional


class Conflict(Exception):
    """The stored file changed since it was loaded."""


class Store:
    def load(self) -> tuple[Optional[bytes], Optional[str]]:
        """(data, version); (None, None) when there is no memory yet."""
        raise NotImplementedError

    def version(self) -> Optional[str]:
        """Current version without downloading the file; None when there is no file."""
        raise NotImplementedError

    def save(self, data: bytes, base_version: Optional[str]) -> str:
        """Write `data` if the stored version is still `base_version`; return the new version."""
        raise NotImplementedError


class LocalStore(Store):
    """A file on this machine. Writes are atomic (temp file + rename)."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()

    def _stat_version(self) -> Optional[str]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return f"{st.st_mtime_ns}:{st.st_size}"

    def version(self) -> Optional[str]:
        return self._stat_version()

    def load(self) -> tuple[Optional[bytes], Optional[str]]:
        with self._lock:
            v = self._stat_version()
            if v is None:
                return None, None
            return self.path.read_bytes(), v

    def save(self, data: bytes, base_version: Optional[str]) -> str:
        with self._lock:
            if self._stat_version() != base_version:
                raise Conflict(str(self.path))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".itk-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return self._stat_version()  # type: ignore[return-value]


class MemoryStore(Store):
    """Bytes in RAM with a counter version. For tests and for simulating remote providers."""

    def __init__(self, data: Optional[bytes] = None):
        self.data = data
        self.n = 0 if data is None else 1
        self._lock = threading.Lock()

    def version(self) -> Optional[str]:
        return None if self.data is None else str(self.n)

    def load(self) -> tuple[Optional[bytes], Optional[str]]:
        with self._lock:
            return self.data, self.version()

    def save(self, data: bytes, base_version: Optional[str]) -> str:
        with self._lock:
            if self.version() != base_version:
                raise Conflict("memory")
            self.data, self.n = data, self.n + 1
            return str(self.n)
