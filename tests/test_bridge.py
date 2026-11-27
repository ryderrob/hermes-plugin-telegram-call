"""Bridge behaviour without Telegram: rate limits, event replay, HTTP auth and target rules."""
from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tg_user_bridge.config import Config, Limits, load, read_or_create_token
from tg_user_bridge.limits import RateLimiter
from tg_user_bridge.server import serve
from tg_user_bridge.service import BridgeService, EventHub

OWNER, FRIEND, STRANGER, GROUP = 111, 222, 333, -100500


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def test_call_limits_cooldown_and_daily_cap(tmp_path: Path) -> None:
    clock = Clock()
    lim = RateLimiter(Limits(min_call_gap_s=60, max_calls_per_day=2), tmp_path / "s.json", clock)
    assert lim.check("call") == (True, "")
    lim.record("call")
    assert not lim.check("call")[0]  # cooldown
    clock.t += 61
    lim.record("call")
    clock.t += 61
    ok, why = lim.check("call")
    assert not ok and "daily" in why
    clock.t += 86400
    assert lim.check("call")[0]


def test_limits_survive_restart(tmp_path: Path) -> None:
    clock, state = Clock(), tmp_path / "s.json"
    RateLimiter(Limits(min_call_gap_s=60), state, clock).record("call")
    assert not RateLimiter(Limits(min_call_gap_s=60), state, clock).check("call")[0]


def test_event_hub_replays_after_id() -> None:
    hub = EventHub(size=3)
    for n in range(5):
        hub.publish({"n": n})
    assert [e["n"] for e in hub.after(3, 0)] == [3, 4]
    assert [e["n"] for e in hub.after(0, 0)] == [2, 3, 4]  # ring buffer keeps the last 3
    assert hub.after(5, 0.01) == []


def test_token_created_once_with_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "token"
    first = read_or_create_token(path)
    assert read_or_create_token(path) == first and len(first) == 64
    assert path.stat().st_mode & 0o777 == 0o600


def test_config_load(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[telegram]\napi_id = 1\napi_hash = "h"\nsession = "~/s/user"\n'
                        'allowed_users = ["@me", "42"]\n[limits]\nmax_calls_per_day = 5\n'
                        '[llm]\nbase_url = "http://x/v1/"\nmodel = "m"\nreasoning_effort = "low"\n')
    cfg = load(cfg_file)
    assert cfg.allowed_users == ["@me", "42"] and cfg.limits.max_calls_per_day == 5
    assert cfg.llm.base_url == "http://x/v1" and cfg.llm.extra == {"reasoning_effort": "low"}
    assert cfg.lock_file.name == "session.lock"


# -- HTTP API against a fake service --------------------------------------------------------------

class FakeService(BridgeService):
    def __init__(self, cfg: Config, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(cfg)
        self.loop = loop
        self.allowed = {OWNER: "Owner", FRIEND: "Friend"}
        self.me = {"id": 9, "name": "Acct", "username": None}
        self.sent: list[tuple] = []
        self.calls = object()  # truthy: calls "available"
        self.placed: list[tuple] = []

    async def send(self, chat_id, text="", file="", voice_note=False, reply_to=None):  # type: ignore[override]
        self.sent.append((chat_id, text, file, voice_note, reply_to))
        return len(self.sent)

    async def pin(self, chat_id, message_id):  # type: ignore[override]
        self.sent.append(("pin", chat_id, message_id))

    async def call(self, chat_id, greeting, instructions, max_minutes):  # type: ignore[override]
        self.placed.append((chat_id, greeting, instructions))


@pytest.fixture()
def api(tmp_path: Path):
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    cfg = Config(port=0, limits=Limits(min_call_gap_s=3600, max_calls_per_day=3, min_msg_gap_s=0))
    cfg.session = tmp_path / "user"
    svc = FakeService(cfg, loop)
    svc.limiter = RateLimiter(cfg.limits, tmp_path / "state.json")
    srv = serve(svc, "secret")
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def req(method: str, path: str, body=None, token="secret"):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = urllib.request.Request(base + path, method=method, headers=headers,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    yield req, svc
    srv.shutdown()
    loop.call_soon_threadsafe(loop.stop)


def test_requires_token(api) -> None:
    req, _ = api
    assert req("GET", "/api/health", token=None)[0] == 401
    assert req("GET", "/api/health", token="wrong")[0] == 401
    code, body = req("GET", "/api/health")
    assert code == 200 and body["owner_id"] == OWNER


def test_send_only_to_allowed_users(api) -> None:
    req, svc = api
    assert req("POST", "/api/send", {"chat_id": FRIEND, "text": "hi"})[0] == 200
    assert req("POST", "/api/send", {"chat_id": STRANGER, "text": "hi"})[0] == 403
    assert req("POST", "/api/send", {"chat_id": GROUP, "text": "hi"})[0] == 403
    assert req("POST", "/api/send", {"chat_id": FRIEND})[0] == 400
    assert [s[0] for s in svc.sent] == [FRIEND]


def test_private_calls_only_to_owner_groups_allowed(api) -> None:
    req, svc = api
    assert req("POST", "/api/call", {"chat_id": FRIEND})[0] == 403
    assert req("POST", "/api/call", {"chat_id": STRANGER})[0] == 403
    code, body = req("POST", "/api/call", {"greeting": "hey"})
    assert code == 200 and body["calling"] == OWNER
    code, body = req("POST", "/api/call", {"chat_id": GROUP})
    assert code == 429  # cooldown applies to every outgoing call


def test_legacy_notify_targets_owner(api) -> None:
    req, svc = api
    code, body = req("POST", "/notify", {"text": "done", "call": True, "instructions": "ask"})
    assert code == 200 and body["sent"]["text"] and body["sent"]["call"]
    assert svc.sent[0][0] == OWNER
    code, body = req("POST", "/notify", {"text": "again", "call": True})
    assert body["sent"]["call"] is False and body["sent"]["suppressed"]


def test_pin(api) -> None:
    req, svc = api
    assert req("POST", "/api/pin", {"chat_id": OWNER, "message_id": 7})[0] == 200
    assert svc.sent[-1] == ("pin", OWNER, 7)


def test_inherits_hermes_model_voice_and_stt(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes.yaml"
    hermes.write_text(
        "model: {default: auto, provider: 'custom:local', base_url: 'http://ignored/v1'}\n"
        "custom_providers: [{name: local, base_url: 'http://gw:9999/v1/'}]\n"
        "tts: {provider: edge, edge: {voice: en-US-AriaNeural}}\n"
        "stt: {provider: local, local: {model: base, language: ''}}\n")
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'[hermes]\nconfig = "{hermes}"\n[llm]\nreasoning_effort = "low"\n')
    cfg = load(cfg_file)
    assert (cfg.llm.base_url, cfg.llm.model) == ("http://gw:9999/v1", "auto")
    assert cfg.llm.extra == {"reasoning_effort": "low"}  # bridge settings layer on top
    assert (cfg.tts.engine, cfg.tts.voice) == ("edge", "en-US-AriaNeural")
    assert (cfg.stt.engine, cfg.stt.model) == ("local", "base")
