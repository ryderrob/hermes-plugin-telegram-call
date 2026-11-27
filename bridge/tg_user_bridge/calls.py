"""Private voice calls with a live conversation loop (energy VAD -> STT -> LLM -> TTS).

Adapted from the openjarvis-telegram plugin's voice module, trimmed to 1:1 calls.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .audio import CHANNELS, FRAME_BYTES, FRAME_MS, SAMPLE_RATE, rms, speech_chunks
from .speech import Speech

log = logging.getLogger("tg_user_bridge.calls")

SPEECH_RMS = 500  # Telegram applies AGC, so a fixed floor works
END_SILENCE_S = 0.8
MIN_SPEECH_S = 0.35
MAX_UTTERANCE_S = 30.0
KEEP_TRAILING_S = 0.2
END_CALL_TOKEN = "[END_CALL]"


def persona(name: str, owner: str, instructions: str, group: bool = False) -> str:
    prompt = (
        f"You are {name}, an AI assistant speaking out loud on a live Telegram voice call with "
        f"{owner}. Replies are converted to speech: answer in one to three short, natural spoken "
        "sentences, with no markdown, lists, emojis or URLs. If the conversation is finished or "
        f"you are asked to hang up, say a brief goodbye and end your reply with {END_CALL_TOKEN}."
    )
    if group:
        prompt += (f"\n\nThis is a group voice chat. You only hear {owner}; other participants are muted "
                   "for you. Answer and follow only them.")
    if instructions:
        prompt += f"\n\nYour task on this call: {instructions}"
    return prompt


@dataclass
class _Voice:
    buffer: bytearray = field(default_factory=bytearray)
    speech_s: float = 0.0
    silence_s: float = 0.0
    talking: bool = False


class CallSession:
    def __init__(self, chat_id: int, title: str, system_prompt: str, max_minutes: float, speech: Speech,
                 leave: Callable[[int, str], Any], owner_id: int,
                 speaker_of: Callable[[int, int], Awaitable[Optional[int]]]):
        self.chat_id, self.title, self.system_prompt = chat_id, title, system_prompt
        self.deadline = time.monotonic() + max(0.5, max_minutes) * 60
        self.started_at = time.time()
        self.transcript: list[dict[str, str]] = []
        self.end_reason = ""
        self.ended = asyncio.Event()
        self._speech, self._leave = speech, leave
        self._out: asyncio.Queue[bytes] = asyncio.Queue()
        self._voices: dict[int, _Voice] = {}  # per audio source (ssrc)
        self._utterances: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        self.owner_id, self._speaker_of = owner_id, speaker_of
        self._who: dict[int, Optional[int]] = {}  # ssrc -> user id (cached)
        self._tasks: list[asyncio.Task[Any]] = []
        self._hang_up_after_speech = False
        self._gen = 0

    def snapshot(self) -> dict[str, Any]:
        return {"chat_id": self.chat_id, "title": self.title, "active": not self.ended.is_set(),
                "duration_s": round(time.time() - self.started_at, 1), "end_reason": self.end_reason or None,
                "transcript": self.transcript}

    # -- outgoing ----------------------------------------------------------
    def _enqueue(self, pcm: bytes) -> None:
        for i in range(0, len(pcm), FRAME_BYTES):
            chunk = pcm[i:i + FRAME_BYTES]
            self._out.put_nowait(chunk + b"\x00" * (FRAME_BYTES - len(chunk)))

    def _drop_audio(self) -> None:
        self._gen += 1
        while not self._out.empty():
            self._out.get_nowait()

    async def _sender(self, calls: Any) -> None:
        from pytgcalls.types import Device

        silence, next_at = b"\x00" * FRAME_BYTES, time.monotonic()
        while not self.ended.is_set():
            try:
                chunk = self._out.get_nowait()
            except asyncio.QueueEmpty:
                chunk = silence
                if self._hang_up_after_speech:
                    await self._leave(self.chat_id, "assistant ended call")
                    return
            try:
                await calls.send_frame(self.chat_id, Device.MICROPHONE, chunk)
            except Exception:  # noqa: BLE001 - one dropped frame is not fatal
                log.debug("send_frame failed", exc_info=True)
            next_at += FRAME_MS / 1000
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))
            if time.monotonic() > self.deadline:
                await self._leave(self.chat_id, "time limit reached")
                return

    async def say(self, text: str) -> None:
        loop, gen = asyncio.get_running_loop(), self._gen
        pending = [loop.run_in_executor(None, self._speech.synthesize, c) for c in speech_chunks(text)]
        try:
            for fut in pending:
                pcm = await fut
                if self._gen != gen:
                    return  # barge-in
                self._enqueue(pcm)
        finally:
            for fut in pending:
                fut.cancel()

    # -- incoming ----------------------------------------------------------
    def on_frames(self, frames: list[Any]) -> None:
        if self.ended.is_set():
            return
        for frame in frames:
            pcm = frame.frame
            if not pcm:
                continue
            ssrc = frame.ssrc & 0xFFFFFFFF
            v = self._voices.setdefault(ssrc, _Voice())
            seconds = len(pcm) / (SAMPLE_RATE * CHANNELS * 2)
            if rms(pcm) >= SPEECH_RMS:
                if not v.talking and not self._out.empty():
                    self._drop_audio()  # caller talks over the assistant
                v.talking, v.speech_s, v.silence_s = True, v.speech_s + seconds, 0.0
            elif v.talking:
                v.silence_s += seconds
            if v.talking:
                v.buffer += pcm
            if v.talking and (v.silence_s >= END_SILENCE_S or v.speech_s >= MAX_UTTERANCE_S):
                if v.speech_s >= MIN_SPEECH_S:
                    trim = int(max(0.0, v.silence_s - KEEP_TRAILING_S) * SAMPLE_RATE) * CHANNELS * 2
                    self._utterances.put_nowait((ssrc, bytes(v.buffer[: len(v.buffer) - trim or None])))
                self._voices[ssrc] = _Voice()

    async def _conversation(self) -> None:
        loop = asyncio.get_running_loop()
        while not self.ended.is_set():
            ssrc, pcm = await self._utterances.get()
            if not await self._is_owner(ssrc):
                continue
            try:
                text = await loop.run_in_executor(None, self._speech.transcribe, pcm)
            except Exception as exc:  # noqa: BLE001
                log.warning("transcription failed: %s", exc)
                continue
            if not text:
                continue
            log.info("call %s heard: %s", self.title, text[:120])
            self.transcript.append({"role": "user", "content": text})
            messages = [{"role": "system", "content": self.system_prompt}, *self.transcript[-40:]]
            try:
                reply = await loop.run_in_executor(None, self._speech.reply, messages)
            except Exception as exc:  # noqa: BLE001
                log.warning("reply failed: %s", exc)
                continue
            hang_up = END_CALL_TOKEN in reply
            reply = reply.replace(END_CALL_TOKEN, "").strip()
            if reply:
                self.transcript.append({"role": "assistant", "content": reply})
                try:
                    await self.say(reply)
                except Exception as exc:  # noqa: BLE001
                    log.warning("speech synthesis failed: %s", exc)
            self._hang_up_after_speech = self._hang_up_after_speech or hang_up

    async def _is_owner(self, ssrc: int) -> bool:
        """Private call: the other side is the owner (only their calls are placed/answered).
        Group voice chat: map the audio source to a participant and drop everyone but the owner."""
        if self.chat_id > 0:
            return True
        if ssrc not in self._who:
            self._who[ssrc] = await self._speaker_of(self.chat_id, ssrc)
        return self._who[ssrc] == self.owner_id

    def start(self, calls: Any) -> None:
        self._tasks.append(asyncio.ensure_future(self._sender(calls)))
        self._tasks.append(asyncio.ensure_future(self._conversation()))

    def finish(self, reason: str) -> None:
        if self.ended.is_set():
            return
        self.end_reason = reason
        self.ended.set()
        for task in self._tasks:
            if task is not asyncio.current_task():
                task.cancel()


class CallManager:
    """Owns the PyTgCalls instance; one live session per chat."""

    def __init__(self, client: Any, speech: Speech, on_ended: Callable[[CallSession], None], owner_id: int):
        self.client, self.speech, self.on_ended, self.owner_id = client, speech, on_ended, owner_id
        self.sessions: dict[int, CallSession] = {}
        self.last: Optional[dict[str, Any]] = None
        # Incoming calls are answered only from the owner; anyone else just rings out.
        self.auto_answer: Optional[dict[str, Any]] = None
        self._calls: Any = None

    async def start(self) -> None:
        from pytgcalls import PyTgCalls, filters
        from pytgcalls.types import ChatUpdate, Device, Direction

        calls = PyTgCalls(self.client)
        await calls.start()

        @calls.on_update(filters.stream_frame(Direction.INCOMING, Device.MICROPHONE | Device.SPEAKER))
        async def _frames(_: Any, update: Any) -> None:
            if (session := self.sessions.get(update.chat_id)) is not None:
                session.on_frames(update.frames)

        @calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def _left(_: Any, update: Any) -> None:
            if (session := self.sessions.pop(update.chat_id, None)) is not None:
                self._end(session, f"call ended ({update.status.name.lower()})")

        @calls.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
        async def _incoming(_: Any, update: Any) -> None:
            if self.auto_answer is None or update.chat_id in self.sessions:
                return
            if update.chat_id != self.owner_id:
                log.info("ignoring call from %s (only the owner is answered)", update.chat_id)
                return
            try:
                await self.call(update.chat_id, **self.auto_answer)
            except Exception:  # noqa: BLE001
                log.exception("could not answer incoming call")

        self._calls = calls

    async def speaker_of(self, chat_id: int, ssrc: int) -> Optional[int]:
        try:
            for participant in await self._calls.get_participants(chat_id) or []:
                if participant.source & 0xFFFFFFFF == ssrc:
                    return int(participant.user_id)
        except Exception:  # noqa: BLE001 - unknown speaker = not the owner = ignored
            log.debug("participant lookup failed", exc_info=True)
        return None

    def _end(self, session: CallSession, reason: str) -> None:
        session.finish(reason)
        self.last = session.snapshot()
        if not self.sessions:
            self.speech.release()  # free the local Whisper model between calls
        self.on_ended(session)

    async def leave(self, chat_id: int, reason: str = "left") -> None:
        session = self.sessions.pop(chat_id, None)
        try:
            await self._calls.leave_call(chat_id)
        except Exception:  # noqa: BLE001
            log.debug("leave_call failed", exc_info=True)
        if session is not None:
            self._end(session, reason)

    async def call(self, chat_id: int, *, title: str, owner: str, name: str, greeting: str = "",
                   instructions: str = "", max_minutes: float = 15.0) -> CallSession:
        from pytgcalls.types import CallConfig, ExternalMedia, GroupCallConfig, MediaStream, RecordStream
        from pytgcalls.types.raw import AudioParameters

        if chat_id in self.sessions:
            return self.sessions[chat_id]
        group = chat_id < 0
        session = CallSession(chat_id, title, persona(name, owner, instructions, group), max_minutes, self.speech,
                              self.leave, self.owner_id, self.speaker_of)
        self.sessions[chat_id] = session
        params = AudioParameters(SAMPLE_RATE, CHANNELS)
        try:
            await self._calls.play(chat_id, MediaStream(ExternalMedia.AUDIO, params,
                                                        video_flags=MediaStream.Flags.IGNORE),
                                   GroupCallConfig() if group else CallConfig(timeout=60))
            await self._calls.record(chat_id, RecordStream(True, params))
        except Exception:
            self.sessions.pop(chat_id, None)
            raise
        log.info("call with %s connected", title)
        session.start(self._calls)
        if greeting:
            session.transcript.append({"role": "assistant", "content": greeting})
            await session.say(greeting)
        return session
