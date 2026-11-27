"""Hermes platform adapter for a Telegram user account, via the local tg-user-bridge daemon.

Inbound: Server-Sent Events from ``GET /api/events`` (replay with ``since`` after reconnects).
Outbound: JSON POSTs (``/api/send``, ``/api/typing``, ``/api/pin``). The bridge confines
outbound traffic to allowed users, so the agent cannot message strangers from the account.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from .common import bridge_url, read_token
from .welcome import send_welcome_once

logger = logging.getLogger(__name__)

PLATFORM = "telegram_user"
MAX_MESSAGE_LENGTH = 4000  # Telegram caps at 4096; leave room for markdown entities
_KIND_TO_TYPE = {
    "voice": MessageType.VOICE, "audio": MessageType.AUDIO, "photo": MessageType.PHOTO,
    "video": MessageType.VIDEO, "video_note": MessageType.VIDEO, "gif": MessageType.VIDEO,
    "sticker": MessageType.STICKER, "document": MessageType.DOCUMENT,
}


class TelegramUserAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    SUPPORTS_MESSAGE_EDITING = False  # replies go out whole; no streaming edits

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM))
        self.extra = config.extra or {}
        self.url = bridge_url(self.extra)
        self.client: Optional[httpx.AsyncClient] = None
        self.health: dict[str, Any] = {}
        self._since = 0
        self._listener: Optional[asyncio.Task] = None
        self._welcome: Optional[asyncio.Task] = None
        self._running = False

    # -- lifecycle ---------------------------------------------------------
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        token = read_token(self.extra)
        if not token:
            logger.error("telegram_user: no bridge token (run `tg-user-bridge token`)")
            return False
        self.client = httpx.AsyncClient(base_url=self.url, timeout=30.0,
                                        headers={"Authorization": f"Bearer {token}"})
        try:
            resp = await self.client.get("/api/health", timeout=10.0)
            resp.raise_for_status()
            self.health = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("telegram_user: bridge not reachable at %s: %s", self.url, exc)
            await self.client.aclose()
            self.client = None
            return False
        # Only messages that arrive from now on: an old backlog must not trigger fresh agent turns.
        if not self._since:
            self._since = int(self.health.get("last_event_id") or 0)
        self._running = True
        self._listener = asyncio.create_task(self._listen())
        self._welcome = asyncio.create_task(send_welcome_once(self, int(self.health["owner_id"])))
        self._mark_connected()
        logger.info("telegram_user: connected as %s via %s", self.health.get("account", {}).get("name"), self.url)
        return True

    async def disconnect(self) -> None:
        self._running = False
        for task in (self._listener, self._welcome):
            if task and not task.done():
                task.cancel()
        if self.client:
            await self.client.aclose()
            self.client = None
        self._mark_disconnected()

    # -- inbound -----------------------------------------------------------
    async def _listen(self) -> None:
        backoff = 1.0
        while self._running and self.client:
            try:
                async with self.client.stream("GET", "/api/events", params={"since": self._since},
                                              timeout=httpx.Timeout(10.0, read=60.0)) as resp:
                    resp.raise_for_status()
                    backoff = 1.0
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            await self._dispatch(line[5:].strip())
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - bridge restarts are normal; reconnect
                logger.warning("telegram_user: event stream dropped (%s); retrying in %.0fs", exc, backoff)
            await asyncio.sleep(backoff + random.uniform(0, 0.5))
            backoff = min(backoff * 2, 30.0)

    async def _dispatch(self, raw: str) -> None:
        try:
            event = json.loads(raw)
        except ValueError:
            return
        self._since = max(self._since, int(event.get("id", 0)))
        if event.get("type") == "message":
            try:
                await self.handle_message(self._to_event(event))
            except Exception:  # noqa: BLE001
                logger.exception("telegram_user: failed to handle message")

    def _to_event(self, ev: dict[str, Any]) -> MessageEvent:
        source = self.build_source(chat_id=str(ev["chat_id"]), chat_name=ev.get("sender_name"), chat_type="dm",
                                   user_id=str(ev["sender_id"]), user_name=ev.get("sender_name"),
                                   message_id=str(ev["message_id"]))
        media = ev.get("media") or []
        reply = ev.get("reply_to") or {}
        return MessageEvent(
            text=ev.get("text") or "",
            message_type=_KIND_TO_TYPE.get(media[0]["kind"], MessageType.DOCUMENT) if media else MessageType.TEXT,
            user_id=str(ev["sender_id"]), user_name=ev.get("sender_name"), source=source, raw_message=ev,
            message_id=str(ev["message_id"]),
            media_urls=[m["path"] for m in media], media_types=[m["mime"] for m in media],
            reply_to_message_id=str(reply["message_id"]) if reply.get("message_id") else None,
            reply_to_text=reply.get("text") or None, reply_to_is_own_message=bool(reply.get("is_own")),
            timestamp=datetime.fromtimestamp(float(ev["date"]), tz=UTC) if ev.get("date") else datetime.now(tz=UTC),
        )

    # -- outbound ----------------------------------------------------------
    async def _post(self, path: str, body: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        if not self.client:
            return {"ok": False, "error": "not connected"}
        try:
            resp = await self.client.post(path, json=body, timeout=timeout)
            data = resp.json()
            if resp.status_code >= 400:
                data.setdefault("ok", False)
            return data
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _result(data: dict[str, Any]) -> SendResult:
        if data.get("ok"):
            mid = data.get("message_id")
            return SendResult(success=True, message_id=str(mid) if mid else None, raw_response=data)
        return SendResult(success=False, error=str(data.get("error") or "send failed"), raw_response=data)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        if not content or not content.strip():
            return SendResult(success=True, message_id=None)
        body: dict[str, Any] = {"chat_id": int(chat_id), "text": content}
        if reply_to and str(reply_to).isdigit():
            body["reply_to"] = int(reply_to)
        return self._result(await self._post("/api/send", body))

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        await self._post("/api/typing", {"chat_id": int(chat_id)}, timeout=10.0)

    async def _send_file(self, chat_id: str, path: str, caption: Optional[str], voice: bool = False) -> SendResult:
        if not path.startswith(("http://", "https://")) and not Path(path).is_file():
            return SendResult(success=False, error=f"file not found: {path}")
        body = {"chat_id": int(chat_id), "file": path, "text": caption or "", "voice_note": voice}
        return self._result(await self._post("/api/send", body, timeout=300.0))

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None) -> SendResult:
        return await self._send_file(chat_id, image_url, caption)

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, **kwargs) -> SendResult:
        return await self._send_file(chat_id, image_path, caption)

    async def send_document(self, chat_id, file_path, caption=None, filename=None, **kwargs) -> SendResult:
        return await self._send_file(chat_id, file_path, caption)

    async def send_video(self, chat_id, video_path, caption=None, reply_to=None, **kwargs) -> SendResult:
        return await self._send_file(chat_id, video_path, caption)

    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None, **kwargs) -> SendResult:
        return await self._send_file(chat_id, audio_path, caption, voice=True)

    async def pin(self, chat_id: int, message_id: int) -> bool:
        return bool((await self._post("/api/pin", {"chat_id": chat_id, "message_id": message_id})).get("ok"))

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        names = {str(a["id"]): a["name"] for a in self.health.get("allowed", [])}
        return {"name": names.get(str(chat_id), str(chat_id)), "type": "dm", "chat_id": str(chat_id)}
