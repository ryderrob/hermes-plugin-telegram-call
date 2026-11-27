"""Hermes plugin: a Telegram *user account* as a messaging platform, plus voice-call tools.

Keep this module import-light: the adapter (httpx + gateway imports) loads only in register().
"""
from __future__ import annotations

import dataclasses
import logging

logger = logging.getLogger(__name__)

__all__ = ["register"]

ALLOWED_USERS_ENV = "TELEGRAM_USER_ALLOWED_USERS"
HOME_CHANNEL_ENV = "TELEGRAM_USER_HOME_CHANNEL"

_PLATFORM_HINT = (
    "You are chatting on Telegram through the user's own Telegram account (not a bot). "
    "Telegram markdown works: **bold**, __italic__, `code`, ```blocks```, [links](url); no tables. "
    "Send files by writing MEDIA:/absolute/path in your reply. You can start a live voice call "
    "with the owner (telegram_call) when they ask for one or something is urgent."
)

# config.yaml (`platforms.telegram_user.extra` or top-level `telegram_user:`) -> env, env wins.
_YAML_BRIDGE = (
    ("allowed_users", ALLOWED_USERS_ENV, "csv"),
    ("home_channel", HOME_CHANNEL_ENV, "str"),
)


def _platform_block(yaml_cfg: dict) -> dict:
    extra = ((yaml_cfg.get("platforms") or {}).get("telegram_user") or {}).get("extra") or {}
    return {**(yaml_cfg.get("telegram_user") or {}), **extra}


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> dict | None:
    from gateway.platforms._shared import apply_yaml_bridge

    return apply_yaml_bridge({**_platform_block(yaml_cfg), **(platform_cfg or {})}, _YAML_BRIDGE)


def _check() -> bool:
    try:
        import httpx  # noqa: F401
    except ImportError:
        return False
    return True


def _validate(config) -> bool:
    from .common import read_token

    if read_token(getattr(config, "extra", None)):
        return True
    logger.warning("telegram_user: bridge token not found (see `tg-user-bridge token`)")
    return False


def register(ctx) -> None:
    from .common import remember_settings

    remember_settings(ctx)
    try:
        from .tools import register_tools

        register_tools(ctx)
    except Exception:  # noqa: BLE001 - the platform must still load
        logger.warning("telegram_user: failed to register call tools", exc_info=True)

    from .adapter import MAX_MESSAGE_LENGTH, TelegramUserAdapter

    kwargs = dict(
        name="telegram_user", label="Telegram (user account)", adapter_factory=TelegramUserAdapter,
        check_fn=_check, validate_config=_validate, required_env=[],
        install_hint="Install and start tg-user-bridge (see the plugin README)",
        allowed_users_env=ALLOWED_USERS_ENV, allow_all_env="TELEGRAM_USER_ALLOW_ALL_USERS",
        cron_deliver_env_var=HOME_CHANNEL_ENV, max_message_length=MAX_MESSAGE_LENGTH,
        platform_hint=_PLATFORM_HINT, emoji="✈️", pii_safe=True, allow_update_command=True,
        apply_yaml_config_fn=_apply_yaml_config,
    )
    from gateway.platform_registry import PlatformEntry

    known = {f.name for f in dataclasses.fields(PlatformEntry)}
    for key, value in (("allowlist_suppresses_pairing", True), ("display_tier", "low")):
        if key in known:  # older cores reject unknown kwargs
            kwargs[key] = value
    ctx.register_platform(**{k: v for k, v in kwargs.items() if k in known or k == "adapter_factory"})
