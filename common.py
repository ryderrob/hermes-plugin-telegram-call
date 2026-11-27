"""Where the bridge lives and how to authenticate — shared by the adapter and the tools.

Precedence: ``platforms.telegram_user.extra`` (adapter only) -> plugin settings
(``plugins.entries.telegram-user.settings``) -> defaults.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

DEFAULT_URL = "http://127.0.0.1:7599"
DEFAULT_TOKEN_FILE = "~/.config/tg-user-bridge/token"

_settings: dict[str, Any] = {}


def remember_settings(ctx: Any) -> None:
    """Capture this plugin's settings namespace at registration time."""
    for key in ("bridge_url", "token_file"):
        try:
            value = ctx.get_config(key)
        except Exception:  # noqa: BLE001 - older cores without plugin settings
            value = None
        if value:
            _settings[key] = value


def bridge_url(extra: Optional[dict[str, Any]] = None) -> str:
    return str((extra or {}).get("bridge_url") or _settings.get("bridge_url") or DEFAULT_URL).rstrip("/")


def token_path(extra: Optional[dict[str, Any]] = None) -> Path:
    return Path(str((extra or {}).get("token_file") or _settings.get("token_file") or DEFAULT_TOKEN_FILE)
                ).expanduser()


def read_token(extra: Optional[dict[str, Any]] = None) -> str:
    try:
        return token_path(extra).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def call_bridge(method: str, path: str, body: Optional[dict[str, Any]] = None, timeout: float = 40) -> dict:
    """Blocking JSON call for tools (the adapter uses httpx on the gateway loop)."""
    req = urllib.request.Request(
        f"{bridge_url()}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {read_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return {"ok": False, "status": exc.code, **json.loads(exc.read() or b"{}")}
        except ValueError:
            return {"ok": False, "status": exc.code, "error": exc.reason}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "error": f"tg-user-bridge unreachable at {bridge_url()}: {exc}",
                "hint": "systemctl --user status tg-user-bridge"}


def token_available() -> bool:
    return bool(read_token())
