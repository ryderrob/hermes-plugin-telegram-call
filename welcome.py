"""First-run welcome: send the owner every available command, pin the first message.

A bot gets a native command menu; a user account cannot, so the list is sent once and
pinned. ``/commands`` shows it again any time. State lives under the active Hermes home.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)
CHUNK = 3800


def _state_file():
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "telegram_user" / "welcome.json"


def command_list() -> list[tuple[str, str]]:
    """Every gateway command (core + plugins + skills), as the Telegram bot menu would show it."""
    try:
        from hermes_cli.commands_platforms import telegram_menu_commands

        commands, _hidden = telegram_menu_commands(max_commands=10_000)
        return commands
    except Exception:  # noqa: BLE001 - internal helper moved: fall back to core commands only
        logger.debug("telegram_menu_commands unavailable", exc_info=True)
    try:
        from hermes_cli.commands_platforms import telegram_bot_commands

        return list(telegram_bot_commands())
    except Exception:  # noqa: BLE001
        return [("help", "Show help"), ("commands", "Browse all commands and skills")]


def render(commands: list[tuple[str, str]], name: str) -> list[str]:
    header = (f"👋 **{name} now runs on this Telegram account.**\n"
              "Every command works by typing it here. Send /commands to browse them again.\n\n")
    lines = [f"/{cmd} — {desc}" if desc else f"/{cmd}" for cmd, desc in commands]
    chunks, current = [], header
    for line in lines:
        if len(current) + len(line) + 1 > CHUNK:
            chunks.append(current.rstrip())
            current = ""
        current += line + "\n"
    chunks.append(current.rstrip())
    return chunks


async def send_welcome_once(adapter: Any, owner_id: int) -> None:
    path = _state_file()
    if path.exists():
        return
    import asyncio

    chunks = render(await asyncio.to_thread(command_list), "Hermes")
    first_id = None
    for chunk in chunks:
        result = await adapter._post("/api/send", {"chat_id": owner_id, "text": chunk})
        if not result.get("ok"):
            logger.warning("telegram_user: welcome message failed: %s", result.get("error"))
            return  # retried on next connect: no state written
        first_id = first_id or int(result["message_id"])
    pinned = await adapter.pin(owner_id, first_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"owner_id": owner_id, "message_id": first_id, "pinned": pinned,
                                "chunks": len(chunks)}))
    logger.info("telegram_user: sent command list (%d messages), pinned=%s", len(chunks), pinned)
