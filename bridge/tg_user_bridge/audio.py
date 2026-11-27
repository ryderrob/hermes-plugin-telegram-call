"""PCM helpers for calls: 48 kHz mono s16le in 10 ms frames (what py-tgcalls exchanges)."""
from __future__ import annotations

import io
import math
import re
import shutil
import subprocess
import wave

import numpy as np

SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_MS = 10
FRAME_BYTES = SAMPLE_RATE * CHANNELS * 2 * FRAME_MS // 1000  # 960
STT_RATE = 16000

_SENTENCE_END = re.compile(r"(?<=[.!?…;:])\s+")
_MIN_CHUNK_CHARS = 60
_MAX_TTS_CHUNKS = 4


def rms(pcm: bytes) -> float:
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)
    if samples.size == 0:
        return 0.0
    floats = samples.astype(np.float32)
    return math.sqrt(float(floats.dot(floats)) / floats.size)


def downsample_for_stt(pcm: bytes) -> bytes:
    """48 kHz -> 16 kHz (3-sample average as a cheap low-pass)."""
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 6], dtype=np.int16)
    if samples.size == 0:
        return b""
    return samples.reshape(-1, SAMPLE_RATE // STT_RATE).astype(np.int32).mean(axis=1).astype(np.int16).tobytes()


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def decode_to_pcm(data: bytes) -> bytes:
    """Any audio bytes -> 48 kHz mono s16le via ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required for call audio but is not on PATH")
    proc = subprocess.run(
        [ffmpeg, "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le",
         "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "pipe:1"],
        input=data, capture_output=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg could not decode audio: {proc.stderr.decode(errors='replace')[:300]}")
    return proc.stdout


def wav_to_pcm(data: bytes) -> bytes:
    """Plain 16-bit WAV is resampled in numpy; anything else goes through ffmpeg."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
                raise wave.Error("not 16-bit PCM")
            rate, channels, frames = wav.getframerate(), wav.getnchannels(), wav.readframes(wav.getnframes())
    except (wave.Error, EOFError):
        return decode_to_pcm(data)
    samples = np.frombuffer(frames[: len(frames) - len(frames) % 2], dtype=np.int16)
    if channels > 1:
        samples = samples[: samples.size - samples.size % channels].reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE and samples.size:
        target = int(samples.size * SAMPLE_RATE / rate)
        samples = np.interp(np.linspace(0, samples.size - 1, target), np.arange(samples.size), samples)
    return np.asarray(samples).astype(np.int16).tobytes()


def speech_chunks(text: str) -> list[str]:
    """Short first chunk (speech starts sooner), then a few larger ones."""
    sentences = [s for s in _SENTENCE_END.split(text.strip()) if s]
    if len(sentences) <= 1:
        return sentences or [text]
    chunks, current = [sentences[0]], ""
    for sentence in sentences[1:]:
        current = f"{current} {sentence}".strip()
        if len(current) >= _MIN_CHUNK_CHARS:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    if len(chunks) > _MAX_TTS_CHUNKS:
        chunks[_MAX_TTS_CHUNKS - 1:] = [" ".join(chunks[_MAX_TTS_CHUNKS - 1:])]
    return chunks
