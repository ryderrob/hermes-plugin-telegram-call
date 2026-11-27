"""The single owner of the Telegram user session: inbound events, sends, pins, typing, calls."""
from __future__ import annotations

import asyncio
import fcntl
import logging
import mimetypes
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from .config import Config
from .limits import RateLimiter
from .speech import Speech

log = logging.getLogger("tg_user_bridge")


class EventHub:
    """Numbered ring buffer that HTTP threads long-poll/stream from (replay via ``since``)."""

    def __init__(self, size: int = 500):
        self._events: deque[dict[str, Any]] = deque(maxlen=size)
        self._next = 1
        self._cond = threading.Condition()

    def publish(self, event: dict[str, Any]) -> int:
        with self._cond:
            event = {"id": self._next, **event}
            self._next += 1
            self._events.append(event)
            self._cond.notify_all()
            return event["id"]

    def after(self, since: int, timeout: float) -> list[dict[str, Any]]:
        with self._cond:
            newer = [e for e in self._events if e["id"] > since]
            if not newer:
                self._cond.wait(timeout)
                newer = [e for e in self._events if e["id"] > since]
            return newer

    @property
    def last_id(self) -> int:
        return self._next - 1


class SessionLock:
    """Exclusive flock: Telegram kills sessions used from two connections (AUTH_KEY_DUPLICATED)."""

    def __init__(self, path: Path):
        self.path, self._fh = path, None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise SystemExit(f"Telegram session is in use by another process (lock {self.path}). Stop it first.")
        self._fh = fh


def _media_kind(msg: Any) -> str:
    for kind in ("voice", "photo", "video_note", "video", "audio", "sticker", "gif"):
        if getattr(msg, kind, None):
            return kind
    return "document"


class BridgeService:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.hub = EventHub()
        self.limiter = RateLimiter(cfg.limits, cfg.state_file)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client: Any = None
        self.me: dict[str, Any] = {}
        self.allowed: dict[int, str] = {}  # user id -> display name; first = owner
        self.calls: Any = None
        self.calls_error = ""
        self._last_send = 0.0
        self._send_lock: Optional[asyncio.Lock] = None

    @property
    def owner_id(self) -> int:
        return next(iter(self.allowed))

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        from telethon import TelegramClient, events, utils

        cfg = self.cfg
        if not (cfg.api_id and cfg.api_hash):
            raise SystemExit("api_id/api_hash missing in config.toml [telegram]")
        if not cfg.allowed_users:
            raise SystemExit("[telegram] allowed_users is empty: nobody could use the bridge")
        SessionLock(cfg.lock_file).acquire()
        self.loop, self._send_lock = asyncio.get_running_loop(), asyncio.Lock()
        self.client = TelegramClient(str(cfg.session), cfg.api_id, cfg.api_hash)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise SystemExit("Telegram session is not logged in. Run: tg-user-bridge login")
        me = await self.client.get_me()
        self.me = {"id": me.id, "name": utils.get_display_name(me), "username": me.username}
        for ref in cfg.allowed_users:
            entity = await self.client.get_entity(int(ref) if ref.lstrip("-").isdigit() else ref)
            self.allowed[utils.get_peer_id(entity)] = utils.get_display_name(entity)
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        await self._start_calls()
        log.info("bridge up as %s; allowed: %s; calls: %s", self.me["name"], ", ".join(self.allowed.values()),
                 "on" if self.calls else f"off ({self.calls_error})")

    async def _start_calls(self) -> None:
        from .calls import CallManager

        try:
            manager = CallManager(self.client, Speech(self.cfg), self._call_ended, self.owner_id)
            await manager.start()
        except Exception as exc:  # noqa: BLE001 - messaging must work without py-tgcalls
            self.calls_error = str(exc) or type(exc).__name__
            log.warning("calls disabled: %s", self.calls_error)
            return
        if self.cfg.answer_calls:
            manager.auto_answer = self._call_kwargs(self.owner_id, greeting="", instructions="")
        self.calls = manager

    async def run_forever(self) -> None:
        await self.client.run_until_disconnected()

    # -- inbound -----------------------------------------------------------
    async def _on_message(self, event: Any) -> None:
        if not event.is_private or event.sender_id not in self.allowed:
            return
        msg = event.message
        payload: dict[str, Any] = {
            "type": "message", "chat_id": event.chat_id, "message_id": msg.id, "sender_id": event.sender_id,
            "sender_name": self.allowed[event.sender_id], "text": msg.message or "", "date": msg.date.timestamp(),
            "media": [],
        }
        if msg.reply_to_msg_id:
            reply = await msg.get_reply_message()
            payload["reply_to"] = {"message_id": msg.reply_to_msg_id,
                                   "text": (reply.message or "") if reply else "",
                                   "is_own": bool(reply and reply.out)}
        if msg.media and (item := await self._download(msg)):
            payload["media"].append(item)
        if payload["text"] or payload["media"]:
            self.hub.publish(payload)

    async def _download(self, msg: Any) -> Optional[dict[str, Any]]:
        size = getattr(msg.file, "size", 0) or 0
        if size > self.cfg.max_media_mb * 1024 * 1024:
            log.warning("skipping %d-byte attachment (over max_media_mb)", size)
            return None
        self.cfg.media_dir.mkdir(parents=True, exist_ok=True)
        try:
            path = await msg.download_media(file=str(self.cfg.media_dir / f"{msg.chat_id}_{msg.id}"))
        except Exception:  # noqa: BLE001
            log.exception("media download failed")
            return None
        if not path:
            return None
        mime = getattr(msg.file, "mime_type", None) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        return {"path": str(path), "mime": mime, "kind": _media_kind(msg)}

    # -- outbound ----------------------------------------------------------
    def check_target(self, chat_id: Optional[int]) -> int:
        """Outbound is confined to allowed users' private chats: the agent cannot message strangers."""
        chat_id = int(chat_id) if chat_id else self.owner_id
        if chat_id not in self.allowed:
            raise PermissionError(f"chat {chat_id} is not an allowed user")
        return chat_id

    async def _paced(self) -> None:
        """Keep >= min_msg_gap_s between sends (waits instead of dropping chunked replies)."""
        assert self._send_lock is not None
        async with self._send_lock:
            ok, why = self.limiter.check("msg")
            if not ok and "hourly" in why:
                raise OverflowError(why)
            wait = self.cfg.limits.min_msg_gap_s - (time.monotonic() - self._last_send)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send = time.monotonic()
            self.limiter.record("msg")

    async def send(self, chat_id: int, text: str = "", file: str = "", voice_note: bool = False,
                   reply_to: Optional[int] = None) -> int:
        await self._paced()
        if file:
            path = _as_voice(file) if voice_note else file
            msg = await self.client.send_file(chat_id, path, caption=text or None, voice_note=voice_note,
                                              reply_to=reply_to)
        else:
            try:
                msg = await self.client.send_message(chat_id, text, reply_to=reply_to, parse_mode="md",
                                                     link_preview=False)
            except Exception:  # noqa: BLE001 - markdown the parser rejects goes out plain
                msg = await self.client.send_message(chat_id, text, reply_to=reply_to, parse_mode=None,
                                                     link_preview=False)
        return msg.id

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        await self.client.edit_message(chat_id, message_id, text, parse_mode="md", link_preview=False)

    async def pin(self, chat_id: int, message_id: int) -> None:
        await self.client.pin_message(chat_id, message_id, notify=False)

    async def typing(self, chat_id: int) -> None:
        from telethon.tl.functions.messages import SetTypingRequest
        from telethon.tl.types import SendMessageTypingAction

        await self.client(SetTypingRequest(await self.client.get_input_entity(chat_id), SendMessageTypingAction()))

    # -- calls -------------------------------------------------------------
    def check_call_target(self, chat_id: Optional[int]) -> int:
        """Private calls go only to the owner; a negative id joins that group's voice chat
        (where only the owner's voice is listened to)."""
        chat_id = int(chat_id) if chat_id else self.owner_id
        if chat_id > 0 and chat_id != self.owner_id:
            raise PermissionError("private calls are only placed to the owner")
        return chat_id

    def _call_kwargs(self, chat_id: int, *, greeting: str, instructions: str,
                     max_minutes: Optional[float] = None) -> dict[str, Any]:
        owner = self.allowed[self.owner_id]
        return {"title": self.allowed.get(chat_id, str(chat_id)), "owner": owner,
                "name": self.cfg.assistant_name, "greeting": greeting, "instructions": instructions,
                "max_minutes": min(float(max_minutes or self.cfg.call_max_minutes), self.cfg.call_max_minutes)}

    def try_reserve_call(self) -> tuple[bool, str]:
        if self.calls is None:
            return False, f"calls unavailable: {self.calls_error}"
        ok, why = self.limiter.check("call")
        if ok:
            self.limiter.record("call")  # the ring counts even if nobody answers
        return ok, why

    async def call(self, chat_id: int, greeting: str, instructions: str, max_minutes: Optional[float]) -> None:
        await asyncio.sleep(1.5)  # small human-looking delay; calls are never retried
        try:
            await self.calls.call(chat_id, **self._call_kwargs(chat_id, greeting=greeting,
                                                               instructions=instructions, max_minutes=max_minutes))
        except Exception as exc:  # noqa: BLE001
            log.warning("call to %s failed: %s", chat_id, exc)
            self.hub.publish({"type": "call_failed", "chat_id": chat_id, "error": str(exc)})

    def _call_ended(self, session: Any) -> None:
        self.hub.publish({"type": "call_ended", **session.snapshot()})


def _as_voice(path: str) -> str:
    """Telegram voice notes must be OGG/Opus; convert anything else with ffmpeg."""
    if Path(path).suffix.lower() in {".ogg", ".oga", ".opus"} or not shutil.which("ffmpeg"):
        return path
    out = Path(tempfile.mkdtemp(prefix="tgvoice-")) / "voice.ogg"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-c:a", "libopus", "-b:a", "48k",
                    str(out)], check=True, timeout=120)
    return str(out)
