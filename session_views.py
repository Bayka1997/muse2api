"""Authenticated task viewers use the exact isolated server-side Chrome target.

This module never copies login cookies to a viewer and never searches other tabs
when the recorded target is unavailable. Terminal targets have bounded retention.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import logging
import math
import re
import threading
import time
from typing import Callable

from cdp import CDP

log = logging.getLogger("muse2api")
_MAX_SCREENSHOT_BYTES = 8 << 20
_TARGET_ID = re.compile(r"[A-Za-z0-9_-]+")


@dataclass
class _TaskSession:
    task_id: str
    target_id: str
    context_id: str
    account_id: str
    active: bool = True
    finished_at: float | None = None
    expires_at: float | None = None
    closed: bool = False
    cdp: object | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


class TaskSessionViews:
    def __init__(self, cdp_port: int, dispose_context: Callable[[str], None],
                 cdp_factory=CDP, clock=time.monotonic, ttl: float = 900,
                 max_retained: int = 8):
        if isinstance(cdp_port, bool) or not 1 <= int(cdp_port) <= 65535:
            raise ValueError("Invalid CDP port")
        if not math.isfinite(ttl) or ttl <= 0 or max_retained < 0:
            raise ValueError("Invalid session retention limits")
        self.cdp_port = int(cdp_port)
        self.dispose_context = dispose_context
        self.cdp_factory = cdp_factory
        self.clock = clock
        self.ttl = ttl
        self.max_retained = int(max_retained)
        self._lock = threading.RLock()
        self._entries: dict[str, _TaskSession] = {}

    @staticmethod
    def _nonempty(value: str, name: str):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing {name}")

    def _details(self, entry: _TaskSession) -> dict:
        return {
            "task_id": entry.task_id,
            "target_id": entry.target_id,
            "context_id": entry.context_id,
            "account_id": entry.account_id,
            "active": entry.active,
            "retained": not entry.active,
            "expires_in": (max(0, int(entry.expires_at - self.clock()))
                           if entry.expires_at is not None else None),
            "can_scroll": not entry.active,
        }

    def bind(self, task_id: str, target_id: str, context_id: str,
             account_id: str) -> dict:
        for name, value in (("task ID", task_id), ("target ID", target_id),
                            ("isolated context ID", context_id), ("account ID", account_id)):
            self._nonempty(value, name)
        if not _TARGET_ID.fullmatch(target_id):
            raise ValueError("Invalid target ID")
        self.prune()
        with self._lock:
            old = self._entries.get(task_id)
            if (old and not old.closed and old.target_id == target_id
                    and old.context_id == context_id and old.account_id == account_id):
                return self._details(old)
            # A BrowserContext belongs to one task; sharing it would break isolation.
            if any(e.task_id != task_id and e.context_id == context_id
                   for e in self._entries.values()):
                raise ValueError("Browser context is already assigned to another task")
            entry = _TaskSession(task_id, target_id, context_id, account_id)
            self._entries[task_id] = entry
            details = self._details(entry)
        if old:
            self._dispose(old, dispose_context=old.context_id != context_id)
        return details

    def finish(self, task_id: str) -> bool:
        with self._lock:
            entry = self._entries.get(task_id)
            if not entry:
                return False
            if entry.active:
                entry.active = False
                entry.finished_at = self.clock()
                entry.expires_at = entry.finished_at + self.ttl
        self.prune()
        return True

    def owns_context(self, context_id: str | None) -> bool:
        if not context_id:
            return False
        self.prune()
        with self._lock:
            return any(e.context_id == context_id for e in self._entries.values())

    def info(self, task_id: str) -> dict:
        self.prune()
        with self._lock:
            entry = self._entries.get(task_id)
            if not entry:
                raise FileNotFoundError("Phiên trình duyệt đã đóng hoặc chưa được tạo.")
            return self._details(entry)

    def _entry(self, task_id: str) -> _TaskSession:
        self.prune()
        with self._lock:
            entry = self._entries.get(task_id)
        if not entry:
            raise FileNotFoundError("Phiên trình duyệt đã đóng hoặc chưa được tạo.")
        return entry

    def _connection(self, entry: _TaskSession):
        # Called only with entry.lock held. This is separate from the worker CDP.
        if entry.closed:
            raise FileNotFoundError("Phiên trình duyệt đã đóng.")
        if entry.cdp is None:
            try:
                entry.cdp = self.cdp_factory(
                    f"ws://127.0.0.1:{self.cdp_port}/devtools/page/{entry.target_id}",
                    timeout=8)
            except Exception as exc:
                raise RuntimeError("Không kết nối được với phiên trình duyệt của tác vụ này.") from exc
        return entry.cdp

    @staticmethod
    def _disconnect(entry: _TaskSession):
        if entry.cdp is not None:
            try:
                entry.cdp.close()
            except Exception:
                pass
            entry.cdp = None

    def screenshot(self, task_id: str) -> bytes:
        entry = self._entry(task_id)
        with entry.lock:
            cdp = self._connection(entry)
            try:
                # Several accounts keep isolated targets in one Chrome. Activate
                # this task's target before capture so the view follows that browser.
                cdp.send("Page.bringToFront", {}, timeout=8)
                response = cdp.send("Page.captureScreenshot", {
                    "format": "jpeg", "quality": 70,
                    "captureBeyondViewport": False,
                }, timeout=8)
            except Exception as exc:
                self._disconnect(entry)
                raise RuntimeError("Không lấy được ảnh từ đúng phiên trình duyệt của tác vụ này.") from exc
            encoded = response.get("result", {}).get("data") if isinstance(response, dict) else None
            if (not isinstance(encoded, str) or not encoded
                    or len(encoded) > 4 * ((_MAX_SCREENSHOT_BYTES + 2) // 3)):
                raise RuntimeError("Ảnh phiên trình duyệt không hợp lệ hoặc quá lớn.")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise RuntimeError("Ảnh phiên trình duyệt không hợp lệ.") from exc
            if (len(data) > _MAX_SCREENSHOT_BYTES
                    or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9")):
                raise RuntimeError("Ảnh phiên trình duyệt không hợp lệ hoặc quá lớn.")
            return data

    def scroll(self, task_id: str, delta: float) -> None:
        if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(delta):
            raise ValueError("Invalid scroll distance")
        delta = max(-2400, min(2400, delta))
        entry = self._entry(task_id)
        with entry.lock:
            if entry.closed:
                raise FileNotFoundError("Phiên trình duyệt đã đóng.")
            if entry.active:
                raise PermissionError("Không cuộn phiên đang tạo để tránh ảnh hưởng tác vụ.")
            cdp = self._connection(entry)
            try:
                metrics = cdp.send("Page.getLayoutMetrics", timeout=8)
                result = metrics.get("result", {})
                viewport = result.get("cssLayoutViewport") or result.get("layoutViewport") or {}
                width = float(viewport.get("clientWidth", 1440))
                height = float(viewport.get("clientHeight", 2400))
                cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseWheel", "x": max(0, width / 2),
                    "y": max(0, height / 2), "deltaX": 0, "deltaY": delta,
                }, timeout=8)
            except Exception as exc:
                self._disconnect(entry)
                raise RuntimeError("Không cuộn được phiên trình duyệt của tác vụ này.") from exc

    def _dispose(self, entry: _TaskSession, dispose_context: bool = True):
        with entry.lock:
            entry.closed = True
            self._disconnect(entry)
            if dispose_context:
                try:
                    self.dispose_context(entry.context_id)
                except Exception:
                    log.warning("Failed to dispose a retained task browser context", exc_info=True)

    def forget(self, task_id: str) -> bool:
        with self._lock:
            entry = self._entries.pop(task_id, None)
        if entry:
            self._dispose(entry)
        return entry is not None

    def prune(self) -> int:
        now = self.clock()
        with self._lock:
            expired = [e for e in self._entries.values()
                       if not e.active and e.expires_at is not None and e.expires_at <= now]
            retained = sorted((e for e in self._entries.values()
                               if not e.active and e not in expired),
                              key=lambda e: e.finished_at if e.finished_at is not None else 0)
            victims = expired + retained[:max(0, len(retained) - self.max_retained)]
            for entry in victims:
                self._entries.pop(entry.task_id, None)
        for entry in victims:
            self._dispose(entry)
        return len(victims)

    def close(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            self._dispose(entry)
