"""Persistent rate limits: calls and messages from a user account must never look like spam."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from .config import Limits

_DAY, _HOUR = 86400.0, 3600.0


class RateLimiter:
    def __init__(self, limits: Limits, state_file: Path, clock: Callable[[], float] = time.time):
        self.limits, self.state_file, self.clock = limits, state_file, clock
        self._lock = threading.Lock()
        try:
            self._state = json.loads(state_file.read_text())
        except (OSError, ValueError):
            self._state = {}

    def _hist(self, kind: str, window: float) -> list[float]:
        now = self.clock()
        hist = [t for t in self._state.get(kind, {}).get("hist", []) if now - t < window]
        self._state.setdefault(kind, {})["hist"] = hist
        return hist

    def check(self, kind: str) -> tuple[bool, str]:
        """``(allowed, reason)`` for one more ``"call"`` or ``"msg"``; does not record it."""
        with self._lock:
            lim, now = self.limits, self.clock()
            last = self._state.get(kind, {}).get("last", 0.0)
            if kind == "call":
                if now - last < lim.min_call_gap_s:
                    return False, f"call cooldown ({int(lim.min_call_gap_s - (now - last))}s left)"
                if len(self._hist(kind, _DAY)) >= lim.max_calls_per_day:
                    return False, "daily call limit reached"
            else:
                if now - last < lim.min_msg_gap_s:
                    return False, "message rate"
                if len(self._hist(kind, _HOUR)) >= lim.max_msgs_per_hour:
                    return False, "hourly message limit reached"
            return True, ""

    def record(self, kind: str) -> None:
        with self._lock:
            now = self.clock()
            entry = self._state.setdefault(kind, {})
            entry["hist"] = [*self._hist(kind, _DAY), now]
            entry["last"] = now
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state))
            tmp.replace(self.state_file)
