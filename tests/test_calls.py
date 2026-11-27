"""Call conversation rules that must hold without a real call: owner-only listening."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np

from tg_user_bridge.audio import FRAME_BYTES, speech_chunks
from tg_user_bridge.calls import CallSession, persona

OWNER, OTHER = 111, 222


def _loud(seconds: float) -> bytes:
    n = int(48000 * seconds)
    return (np.sin(np.arange(n) / 5) * 8000).astype(np.int16).tobytes()


def _frames(ssrc: int, pcm: bytes) -> list[SimpleNamespace]:
    return [SimpleNamespace(ssrc=ssrc, frame=pcm[i:i + FRAME_BYTES]) for i in range(0, len(pcm), FRAME_BYTES)]


class FakeSpeech:
    def __init__(self) -> None:
        self.heard: list[bytes] = []

    def transcribe(self, pcm: bytes) -> str:
        self.heard.append(pcm)
        return "hello"

    def reply(self, messages):
        return "hi there"

    def synthesize(self, text: str) -> bytes:
        return b"\x00" * FRAME_BYTES


async def _run(chat_id: int, speakers: dict[int, int]) -> FakeSpeech:
    speech = FakeSpeech()

    async def speaker_of(_chat: int, ssrc: int):
        return speakers.get(ssrc)

    async def leave(*_):
        return None

    session = CallSession(chat_id, "t", "sys", 1, speech, leave, OWNER, speaker_of)  # type: ignore[arg-type]
    task = asyncio.ensure_future(session._conversation())
    silence = b"\x00" * int(48000 * 2 * 1.0)
    for ssrc in speakers:
        session.on_frames(_frames(ssrc, _loud(0.6) + silence))
    await asyncio.sleep(0.3)
    session.finish("done")
    task.cancel()
    return speech


def test_group_call_listens_only_to_owner() -> None:
    speech = asyncio.run(_run(-100, {1: OWNER, 2: OTHER, 3: None}))
    assert len(speech.heard) == 1  # only ssrc 1 (the owner) reached STT


def test_private_call_hears_the_other_side() -> None:
    speech = asyncio.run(_run(OWNER, {7: None}))
    assert len(speech.heard) == 1


def test_persona_mentions_group_rule_and_task() -> None:
    text = persona("Hermes", "Alex", "check the NAS", group=True)
    assert "only hear Alex" in text and "check the NAS" in text


def test_speech_chunks_short_first() -> None:
    chunks = speech_chunks("Hi. " + "This is a longer sentence that goes on. " * 4)
    assert chunks[0] == "Hi." and len(chunks) <= 4
