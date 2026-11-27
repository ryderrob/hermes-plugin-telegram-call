"""Client tools: place a call to the owner / join a group voice chat, and read call results."""
from __future__ import annotations

import json
from typing import Any

from .common import call_bridge, token_available

_CALL = {
    "name": "telegram_call",
    "description": (
        "Start a live two-way voice call on Telegram. With no chat_id it calls the owner (the user) "
        "privately; with a negative group chat_id it joins that group's voice chat, where only the "
        "owner's voice is listened to. The call speaks `greeting` first, then converses following "
        "`instructions`. Outgoing calls are rate-limited (default: 30 min apart, 3 per day) — only "
        "call when the user asked for it or something is urgent. Returns immediately; use "
        "telegram_call_status for the transcript."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {"type": "integer", "description": "Omit to call the owner; negative id = group voice chat."},
            "greeting": {"type": "string", "description": "First thing to say when the call connects."},
            "instructions": {"type": "string", "description": "Goal for the conversation."},
            "max_minutes": {"type": "number", "minimum": 0.5, "maximum": 120},
        },
    },
}

_STATUS = {
    "name": "telegram_call_status",
    "description": "Live call(s) and the transcript of the most recent finished Telegram call.",
    "parameters": {"type": "object", "properties": {}},
}


def _call(args: dict, **_: Any) -> str:
    body = {k: args[k] for k in ("chat_id", "greeting", "instructions", "max_minutes") if args.get(k)}
    return json.dumps(call_bridge("POST", "/api/call", body))


def _status(args: dict, **_: Any) -> str:
    return json.dumps(call_bridge("GET", "/api/call/last"))


def register_tools(ctx: Any) -> None:
    from .common import remember_settings

    remember_settings(ctx)
    for schema, handler, emoji in ((_CALL, _call, "📞"), (_STATUS, _status, "📋")):
        ctx.register_tool(name=schema["name"], toolset="telegram_user", schema=schema, handler=handler,
                          check_fn=token_available, description=schema["description"], emoji=emoji)
