"""Per-thread "seen" cursor store (seen_messages.json)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi import Request


class SeenStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except (FileNotFoundError, ValueError):
            raw = {}
        self._data = {str(k): str(v) for k, v in raw.items()}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(self._data))
        tmp.replace(self._path)

    def get(self, thread_key: str) -> str | None:
        with self._lock:
            return self._data.get(thread_key)

    def set(self, thread_key: str, cursor: str) -> None:
        with self._lock:
            self._data[thread_key] = cursor
            self._save()

    def clear(self, thread_key: str) -> None:
        with self._lock:
            if thread_key in self._data:
                del self._data[thread_key]
                self._save()


def get_seen_store(request: Request) -> SeenStore:
    return request.app.state.seen_store