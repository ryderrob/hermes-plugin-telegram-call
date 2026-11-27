"""Localhost HTTP API (token-gated). Telegram work runs on the service's asyncio loop."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .service import BridgeService

log = logging.getLogger("tg_user_bridge.server")
KEEPALIVE_S = 15.0


def make_handler(svc: BridgeService, token: str) -> type[BaseHTTPRequestHandler]:
    def run(coro: Any, timeout: float = 60) -> Any:
        assert svc.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, svc.loop).result(timeout)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def _reply(self, code: int, obj: dict[str, Any]) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            given = self.headers.get("X-Sink-Token", "")
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                given = auth[7:]
            return bool(given) and hmac.compare_digest(given.strip(), token)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        # -- GET -----------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                return self._reply(401, {"error": "unauthorized"})
            url = urlparse(self.path)
            if url.path == "/api/health":
                return self._reply(200, {
                    "ok": True, "account": svc.me, "owner_id": svc.owner_id,
                    "allowed": [{"id": i, "name": n} for i, n in svc.allowed.items()],
                    "calls": svc.calls is not None, "calls_error": svc.calls_error or None,
                    "last_event_id": svc.hub.last_id})
            if url.path == "/api/call/last":
                last = svc.calls.last if svc.calls else None
                live = [s.snapshot() for s in svc.calls.sessions.values()] if svc.calls else []
                return self._reply(200, {"live": live, "last": last})
            if url.path == "/api/events":
                since = int(parse_qs(url.query).get("since", ["0"])[0] or 0)
                return self._stream(since)
            return self._reply(404, {"error": "not found"})

        def _stream(self, since: int) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    events = svc.hub.after(since, KEEPALIVE_S)
                    if not events:
                        self.wfile.write(b": keepalive\n\n")
                    for event in events:
                        since = event["id"]
                        self.wfile.write(f"id: {since}\ndata: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        # -- POST ----------------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return self._reply(401, {"error": "unauthorized"})
            route: dict[str, Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]] = {
                "/api/send": self._send, "/api/edit": self._edit, "/api/pin": self._pin,
                "/api/typing": self._typing, "/api/call": self._call, "/notify": self._notify,
            }
            handler = route.get(urlparse(self.path).path)
            if handler is None:
                return self._reply(404, {"error": "not found"})
            try:
                code, obj = handler(self._body())
            except PermissionError as exc:
                code, obj = 403, {"error": str(exc)}
            except OverflowError as exc:
                code, obj = 429, {"error": str(exc)}
            except (ValueError, TypeError) as exc:
                code, obj = 400, {"error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                log.exception("request failed")
                code, obj = 502, {"error": f"{type(exc).__name__}: {exc}"}
            self._reply(code, obj)

        def _send(self, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            chat = svc.check_target(data.get("chat_id"))
            text, file = str(data.get("text") or ""), str(data.get("file") or "")
            if not (text.strip() or file):
                raise ValueError("text or file required")
            reply_to = int(data["reply_to"]) if data.get("reply_to") else None
            mid = run(svc.send(chat, text, file, bool(data.get("voice_note")), reply_to), timeout=180)
            return 200, {"ok": True, "message_id": mid}

        def _edit(self, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            chat = svc.check_target(data.get("chat_id"))
            run(svc.edit(chat, int(data["message_id"]), str(data.get("text") or "")))
            return 200, {"ok": True}

        def _pin(self, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            chat = svc.check_target(data.get("chat_id"))
            run(svc.pin(chat, int(data["message_id"])))
            return 200, {"ok": True}

        def _typing(self, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            run(svc.typing(svc.check_target(data.get("chat_id"))), timeout=15)
            return 200, {"ok": True}

        def _call(self, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            chat = svc.check_call_target(data.get("chat_id"))
            ok, why = svc.try_reserve_call()
            if not ok:
                return 429, {"ok": False, "error": why}
            asyncio.run_coroutine_threadsafe(svc.call(chat, str(data.get("greeting") or ""),
                                                      str(data.get("instructions") or ""),
                                                      data.get("max_minutes")), svc.loop)
            return 200, {"ok": True, "calling": chat}

        def _notify(self, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            """Legacy owner-only sink: {text, call?, instructions?} (watch-api compatible)."""
            text = str(data.get("text", "")).strip()[:3500]
            if not text:
                raise ValueError("missing text")
            sent: dict[str, Any] = {"text": False, "call": False, "suppressed": []}
            try:
                run(svc.send(svc.owner_id, text))
                sent["text"] = True
            except OverflowError as exc:
                sent["suppressed"].append(f"text:{exc}")
            if data.get("call"):
                ok, why = svc.try_reserve_call()
                if ok:
                    asyncio.run_coroutine_threadsafe(svc.call(svc.owner_id, text[:300],
                                                              str(data.get("instructions") or "")[:1500], 2.0),
                                                     svc.loop)
                    sent["call"] = True
                else:
                    sent["suppressed"].append(f"call:{why}")
            return 200, {"ok": True, "sent": sent}

    return Handler


def serve(svc: BridgeService, token: str) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((svc.cfg.host, svc.cfg.port), make_handler(svc, token))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    log.info("API on http://%s:%s", svc.cfg.host, svc.cfg.port)
    return srv
