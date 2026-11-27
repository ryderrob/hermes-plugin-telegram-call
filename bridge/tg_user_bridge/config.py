"""Bridge configuration: one TOML file, secrets kept out of the plugin side.

Location: ``$TG_USER_BRIDGE_HOME/config.toml`` (default ``~/.config/tg-user-bridge``).
"""
from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def bridge_home() -> Path:
    return Path(os.environ.get("TG_USER_BRIDGE_HOME", "~/.config/tg-user-bridge")).expanduser()


@dataclass
class Endpoint:
    """An OpenAI-compatible HTTP endpoint (chat, STT or TTS)."""

    base_url: str = ""
    model: str = ""
    api_key: str = ""
    voice: str = ""  # TTS only
    engine: str = "openai"  # "openai" (HTTP); TTS also "edge" (edge-tts), STT also "local" (faster-whisper)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.base_url) or self.engine in ("edge", "local")


@dataclass
class Limits:
    min_call_gap_s: float = 30 * 60
    max_calls_per_day: int = 3
    min_msg_gap_s: float = 1.0
    max_msgs_per_hour: int = 120


@dataclass
class Config:
    api_id: int = 0
    api_hash: str = ""
    session: Path = field(default_factory=lambda: bridge_home() / "user")
    allowed_users: list[str] = field(default_factory=list)  # "@name" or numeric ids; first = owner
    host: str = "127.0.0.1"
    port: int = 7599
    token_file: Path = field(default_factory=lambda: bridge_home() / "token")
    media_dir: Path = field(default_factory=lambda: bridge_home() / "media")
    max_media_mb: int = 50
    answer_calls: bool = True  # pick up calls from allowed users
    call_max_minutes: float = 15.0
    assistant_name: str = "Hermes"
    llm: Endpoint = field(default_factory=Endpoint)
    stt: Endpoint = field(default_factory=Endpoint)
    tts: Endpoint = field(default_factory=Endpoint)
    limits: Limits = field(default_factory=Limits)

    @property
    def state_file(self) -> Path:
        return bridge_home() / "state.json"

    @property
    def lock_file(self) -> Path:
        # Same name OpenJarvis' telegram plugin uses, so the two can never share one session.
        return self.session.parent / "session.lock"


def _path(value: Any, default: Path) -> Path:
    return Path(str(value)).expanduser() if value else default


def _endpoint(raw: dict[str, Any], env_prefix: str) -> Endpoint:
    def pick(key: str) -> str:
        return str(raw.get(key) or os.environ.get(f"{env_prefix}_{key.upper()}", "") or "")

    known = {"base_url", "model", "api_key", "voice", "engine"}
    return Endpoint(pick("base_url").rstrip("/"), pick("model"), pick("api_key"), pick("voice"),
                    pick("engine") or "openai", {k: v for k, v in raw.items() if k not in known})


def _inherit(ep: Endpoint, base: Endpoint) -> Endpoint:
    """Fill fields the bridge config left blank from what Hermes uses (explicit values win)."""
    for name in ("base_url", "model", "api_key", "voice"):
        if not getattr(ep, name):
            setattr(ep, name, getattr(base, name))
    if ep.engine == "openai" and base.engine != "openai":
        ep.engine = base.engine
    ep.extra = {**base.extra, **ep.extra}
    return ep


def hermes_endpoints(path: Path) -> dict[str, Endpoint]:
    """LLM / STT / TTS settings derived from a Hermes ``config.yaml`` (same provider, same voice)."""
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    model = data.get("model") or {}
    base_url = str(model.get("base_url") or "").rstrip("/")
    api_key = str(model.get("api_key") or "")
    provider = str(model.get("provider") or "")
    for entry in data.get("custom_providers") or []:  # "custom:<name>" carries its own url/key
        if provider == f"custom:{entry.get('name')}":
            base_url = str(entry.get("base_url") or base_url).rstrip("/")
            api_key = str(entry.get("api_key") or api_key)
    llm = Endpoint(base_url, str(model.get("default") or ""), api_key)
    stt_cfg = data.get("stt") or {}
    if str(stt_cfg.get("provider") or "") == "local":
        local = stt_cfg.get("local") or {}
        stt = Endpoint(base_url, str(local.get("model") or "base"), api_key, engine="local",
                       extra={"language": local.get("language") or ""})
    else:
        stt = Endpoint(base_url, str((stt_cfg.get("openai") or {}).get("model") or "") or "whisper-1", api_key)
    tts_cfg = data.get("tts") or {}
    tts_provider = str(tts_cfg.get("provider") or "")
    if tts_provider == "edge":
        tts = Endpoint(voice=str((tts_cfg.get("edge") or {}).get("voice") or "en-US-AriaNeural"), engine="edge")
    else:
        section = tts_cfg.get("openai") or {}
        tts = Endpoint(base_url, str(section.get("model") or ""), api_key, str(section.get("voice") or ""))
    return {"llm": llm, "stt": stt, "tts": tts}


def load(path: Path | None = None) -> Config:
    path = path or bridge_home() / "config.toml"
    raw: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    tg, srv, calls = raw.get("telegram", {}), raw.get("server", {}), raw.get("calls", {})
    cfg = Config()
    cfg.api_id = int(tg.get("api_id") or os.environ.get("TG_API_ID") or 0)
    cfg.api_hash = str(tg.get("api_hash") or os.environ.get("TG_API_HASH") or "")
    cfg.session = _path(tg.get("session"), cfg.session)
    cfg.allowed_users = [str(u).strip() for u in tg.get("allowed_users", []) if str(u).strip()]
    cfg.host = str(srv.get("host", cfg.host))
    cfg.port = int(srv.get("port", cfg.port))
    cfg.token_file = _path(srv.get("token_file"), cfg.token_file)
    cfg.media_dir = _path(srv.get("media_dir"), cfg.media_dir)
    cfg.max_media_mb = int(srv.get("max_media_mb", cfg.max_media_mb))
    cfg.answer_calls = bool(calls.get("answer_calls", cfg.answer_calls))
    cfg.call_max_minutes = float(calls.get("max_minutes", cfg.call_max_minutes))
    cfg.assistant_name = str(calls.get("assistant_name", cfg.assistant_name))
    cfg.llm = _endpoint(raw.get("llm", {}), "TG_BRIDGE_LLM")
    cfg.stt = _endpoint(raw.get("stt", {}), "TG_BRIDGE_STT")
    cfg.tts = _endpoint(raw.get("tts", {}), "TG_BRIDGE_TTS")
    hermes_cfg = (raw.get("hermes") or {}).get("config")
    if hermes_cfg and (hpath := Path(str(hermes_cfg)).expanduser()).exists():
        inherited = hermes_endpoints(hpath)
        cfg.llm, cfg.stt, cfg.tts = (_inherit(cfg.llm, inherited["llm"]), _inherit(cfg.stt, inherited["stt"]),
                                     _inherit(cfg.tts, inherited["tts"]))
    lim = raw.get("limits", {})
    cfg.limits = Limits(**{k: type(getattr(Limits(), k))(v) for k, v in lim.items()
                           if hasattr(Limits(), k)})
    return cfg


def read_or_create_token(path: Path) -> str:
    """Shared secret for the HTTP API; created once (0600), never overwritten."""
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    return token
