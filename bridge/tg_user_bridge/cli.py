"""``tg-user-bridge serve | login | check | token``."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import config as config_mod
from .service import BridgeService


def _serve(cfg: config_mod.Config) -> None:
    from .server import serve

    token = config_mod.read_or_create_token(cfg.token_file)

    async def main() -> None:
        svc = BridgeService(cfg)
        await svc.start()
        serve(svc, token)
        await svc.run_forever()

    asyncio.run(main())


def _login(cfg: config_mod.Config) -> None:
    """Interactive login (phone + code + 2FA) — run it yourself in a terminal."""
    from telethon import TelegramClient

    if not (cfg.api_id and cfg.api_hash):
        sys.exit("Set [telegram] api_id and api_hash in config.toml first (https://my.telegram.org).")
    cfg.session.parent.mkdir(parents=True, exist_ok=True)
    with TelegramClient(str(cfg.session), cfg.api_id, cfg.api_hash) as client:
        me = client.get_me()
        print(f"Logged in as {me.first_name} (@{me.username}). Session: {cfg.session}.session")


def _check(cfg: config_mod.Config) -> None:
    problems = []
    if not (cfg.api_id and cfg.api_hash):
        problems.append("missing [telegram] api_id/api_hash")
    if not cfg.session.with_suffix(".session").exists():
        problems.append(f"no session at {cfg.session}.session (run: tg-user-bridge login)")
    if not cfg.allowed_users:
        problems.append("[telegram] allowed_users is empty")
    for name, ep in (("llm", cfg.llm), ("stt", cfg.stt), ("tts", cfg.tts)):
        if not ep.configured:
            print(f"note: [{name}] not configured — voice calls need llm+stt (tts falls back to espeak-ng)")
    try:
        import pytgcalls  # noqa: F401
    except ImportError:
        print("note: py-tgcalls not installed — messaging works, calls are disabled")
    if problems:
        sys.exit("\n".join(f"error: {p}" for p in problems))
    print("config OK")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tg-user-bridge", description=__doc__)
    parser.add_argument("command", choices=["serve", "login", "check", "token"])
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    from pathlib import Path

    cfg = config_mod.load(Path(args.config).expanduser() if args.config else None)
    if args.command == "token":
        print(cfg.token_file)
        config_mod.read_or_create_token(cfg.token_file)
        return
    {"serve": _serve, "login": _login, "check": _check}[args.command](cfg)


if __name__ == "__main__":
    main()
