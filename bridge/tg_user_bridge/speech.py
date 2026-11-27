"""STT -> LLM -> TTS over OpenAI-compatible HTTP endpoints (stdlib only; all blocking)."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from typing import Any
import urllib.error
import urllib.request
import uuid

from .audio import SAMPLE_RATE, STT_RATE, downsample_for_stt, pcm_to_wav, wav_to_pcm
from .config import Config, Endpoint

log = logging.getLogger("tg_user_bridge.speech")


def _request(ep: Endpoint, path: str, body: bytes, content_type: str, timeout: float = 60) -> bytes:
    headers = {"Content-Type": content_type}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    for attempt in (1, 2):
        req = urllib.request.Request(f"{ep.base_url}{path}", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 2:  # one quick retry: gateways rotate rate-limited routes
                raise
            time.sleep(1.5)
    raise AssertionError("unreachable")


def _edge_tts(text: str, voice: str) -> bytes:
    """Microsoft Edge neural TTS (same engine Hermes' ``tts.provider: edge`` uses); returns MP3."""
    import asyncio

    import edge_tts

    async def collect() -> bytes:
        audio = bytearray()
        async for chunk in edge_tts.Communicate(text, voice).stream():
            if chunk.get("type") == "audio":
                audio += chunk["data"]
        return bytes(audio)

    return asyncio.run(collect())


class Speech:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._whisper: Any = None

    def transcribe(self, pcm: bytes) -> str:
        ep = self.cfg.stt
        if ep.engine == "local":
            try:
                return self._local_transcribe(pcm)
            except Exception as exc:  # noqa: BLE001
                if not ep.base_url:
                    raise
                log.warning("local Whisper failed (%s); using %s", exc, ep.base_url)
        if not ep.base_url:
            raise RuntimeError("no [stt] endpoint configured")
        try:
            return self._http_transcribe(pcm, "auto" if ep.engine == "local" else ep.model)
        except Exception as exc:
            if ep.engine == "local":
                raise
            log.warning("STT endpoint failed (%s); trying local Whisper", exc)
            return self._local_transcribe(pcm)

    def _local_transcribe(self, pcm: bytes) -> str:
        """faster-whisper, loaded on first use and dropped by ``release()`` when calls end."""
        import numpy as np

        with self._lock:
            if self._whisper is None:
                from faster_whisper import WhisperModel

                size = self.cfg.stt.model if self.cfg.stt.engine == "local" and self.cfg.stt.model else "base"
                self._whisper = WhisperModel(size, device="cpu", compute_type="int8")
            model = self._whisper
        audio = np.frombuffer(downsample_for_stt(pcm), dtype=np.int16).astype(np.float32) / 32768.0
        language = str(self.cfg.stt.extra.get("language") or "") or None
        segments, _ = model.transcribe(audio, language=language, beam_size=1, vad_filter=False)
        return " ".join(seg.text.strip() for seg in segments).strip()

    def release(self) -> None:
        with self._lock:
            self._whisper = None

    def _http_transcribe(self, pcm: bytes, model: str) -> str:
        wav = pcm_to_wav(downsample_for_stt(pcm), STT_RATE)
        boundary = uuid.uuid4().hex
        parts = [
            (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n'
             f"{model or 'whisper-1'}\r\n").encode(),
            (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
             "Content-Type: audio/wav\r\n\r\n").encode() + wav + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        raw = _request(self.cfg.stt, "/audio/transcriptions", b"".join(parts),
                       f"multipart/form-data; boundary={boundary}")
        try:
            return str(json.loads(raw).get("text", "")).strip()
        except ValueError:
            return raw.decode(errors="replace").strip()

    def reply(self, messages: list[dict[str, str]]) -> str:
        ep = self.cfg.llm
        if not ep.configured:
            raise RuntimeError("no [llm] endpoint configured")
        body = {"model": ep.model or "auto", "messages": messages, "max_tokens": 300, "temperature": 0.6,
                **ep.extra}
        raw = _request(ep, "/chat/completions", json.dumps(body).encode(), "application/json", timeout=90)
        return str(json.loads(raw)["choices"][0]["message"].get("content") or "").strip()

    def synthesize(self, text: str) -> bytes:
        """48 kHz mono PCM; falls back to espeak-ng when no TTS endpoint answers."""
        ep = self.cfg.tts
        if ep.engine == "edge":
            try:
                return wav_to_pcm(_edge_tts(text, ep.voice or "en-US-AriaNeural"))
            except Exception as exc:  # noqa: BLE001
                log.warning("edge-tts failed (%s); trying espeak", exc)
        elif ep.configured:
            body = {"model": ep.model or "tts-1", "input": text, "voice": ep.voice or "alloy",
                    "response_format": "wav", **ep.extra}
            try:
                return wav_to_pcm(_request(ep, "/audio/speech", json.dumps(body).encode(), "application/json"))
            except Exception as exc:  # noqa: BLE001 - a silent call is worse than a robotic one
                log.warning("TTS endpoint failed (%s); trying espeak", exc)
        espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        if espeak is None:
            raise RuntimeError("no TTS available: configure [tts] or install espeak-ng")
        wav = subprocess.run([espeak, "--stdout", text], capture_output=True, timeout=60, check=True).stdout
        return wav_to_pcm(wav)


__all__ = ["Speech", "SAMPLE_RATE"]
