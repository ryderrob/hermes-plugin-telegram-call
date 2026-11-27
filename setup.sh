#!/usr/bin/env bash
# Easy, non-destructive setup for hermes-plugin-telegram-call.
#
#   ./setup.sh               bridge + Hermes plugin
#   ./setup.sh --bridge-only only the tg-user-bridge daemon
#   ./setup.sh --plugin-only only the Hermes side
#   ./setup.sh --dry-run     print what would change, change nothing
#
# Guarantees: never overwrites an existing file, config key, plugin or systemd unit;
# never touches ~/.hermes/.env; never disables your Telegram bot unless you say yes.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
BRIDGE_HOME="${TG_USER_BRIDGE_HOME:-$HOME/.config/tg-user-bridge}"
VENV="${TG_USER_BRIDGE_VENV:-$HOME/.local/share/tg-user-bridge/venv}"
UNIT="$HOME/.config/systemd/user/tg-user-bridge.service"
GITHUB_REPO="ryderrob/hermes-plugin-telegram-call"
DO_BRIDGE=1 DO_PLUGIN=1 DRY=0

for arg in "$@"; do
  case "$arg" in
    --bridge-only) DO_PLUGIN=0 ;;
    --plugin-only) DO_BRIDGE=0 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
skip() { printf '\033[0;33m -\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$*" >&2; }
run()  { if [ "$DRY" = 1 ]; then echo "   [dry-run] $*"; else "$@"; fi; }
ask()  { local q="$1" d="${2:-}" a; read -r -p "    $q${d:+ [$d]}: " a </dev/tty || true; echo "${a:-$d}"; }
yes_no() { local a; a="$(ask "$1 (y/n)" "${2:-n}")"; [[ "$a" =~ ^[Yy] ]]; }

find_hermes() {
  for c in "${HERMES_BIN:-}" "$(command -v hermes 2>/dev/null || true)" \
           "$HERMES_HOME/hermes-agent/.hermes/bin/hermes"; do
    [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return; }
  done
}

# ---------------------------------------------------------------------------------- bridge
setup_bridge() {
  say "tg-user-bridge"
  local py; py="$(command -v python3)"
  "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { warn "python >= 3.11 required"; exit 1; }
  command -v ffmpeg >/dev/null || warn "ffmpeg not found: voice calls and voice notes need it"

  if [ -x "$VENV/bin/tg-user-bridge" ]; then
    skip "already installed in $VENV (upgrade: $VENV/bin/pip install -U '$REPO/bridge[calls]')"
  else
    run "$py" -m venv "$VENV"
    run "$VENV/bin/pip" install -q --upgrade pip
    run "$VENV/bin/pip" install -q "$REPO/bridge[calls]"
    ok "installed into $VENV"
  fi

  local cfg="$BRIDGE_HOME/config.toml"
  if [ -e "$cfg" ]; then
    skip "config exists: $cfg (left untouched)"
  else
    write_config "$cfg"
  fi

  local session
  session="$("$VENV/bin/python" -c 'from tg_user_bridge import config; print(config.load().session)' 2>/dev/null || true)"
  if [ -n "$session" ] && [ ! -e "$session.session" ]; then
    if [ "$DRY" = 0 ] && yes_no "No Telegram login yet. Log in now (phone number + code)?" y; then
      "$VENV/bin/tg-user-bridge" login
    else
      warn "log in later with: $VENV/bin/tg-user-bridge login"
    fi
  fi
  [ "$DRY" = 1 ] || "$VENV/bin/tg-user-bridge" token >/dev/null

  if [ -e "$UNIT" ]; then
    skip "systemd unit exists: $UNIT"
  else
    run mkdir -p "$(dirname "$UNIT")"
    if [ "$DRY" = 0 ]; then
      sed "s|@VENV@|$VENV|g" "$REPO/systemd/tg-user-bridge.service" > "$UNIT"
    fi
    ok "wrote $UNIT"
  fi

  if systemctl --user -q is-active tg-user-bridge 2>/dev/null; then
    skip "tg-user-bridge already running"
    return
  fi
  [ "$DRY" = 1 ] && { echo "   [dry-run] systemctl --user enable --now tg-user-bridge"; return; }
  local port
  port="$("$VENV/bin/python" -c 'from tg_user_bridge import config; print(config.load().port)' 2>/dev/null || echo 7599)"
  if ss -ltn 2>/dev/null | grep -q "127.0.0.1:$port " && ! systemctl --user -q is-active tg-user-bridge; then
    warn "port $port is taken by another program; set [server] port in $cfg, then start the unit"
    return
  fi
  if [ "$DRY" = 0 ] && "$VENV/bin/tg-user-bridge" check >/dev/null 2>&1; then
    systemctl --user daemon-reload
    systemctl --user enable --now tg-user-bridge.service
    ok "tg-user-bridge running (logs: journalctl --user -u tg-user-bridge -f)"
  else
    warn "not started yet: fix what '$VENV/bin/tg-user-bridge check' reports, then: systemctl --user enable --now tg-user-bridge"
  fi
}

write_config() {
  local cfg="$1" api_id api_hash owner session hermes_line=""
  say "creating $cfg"
  echo "    Telegram API credentials come from https://my.telegram.org (API development tools)."
  api_id="$(ask "api_id")"; api_hash="$(ask "api_hash")"
  owner="$(ask "Your Telegram @username (the owner: only you can call it / be heard)")"
  session="$BRIDGE_HOME/user"
  if [ -f "$HOME/.openjarvis/telegram/user.session" ] && \
     yes_no "Found an OpenJarvis Telegram login. Reuse it in place (OpenJarvis must stay stopped)?" n; then
    session="$HOME/.openjarvis/telegram/user"
  fi
  [ -f "$HERMES_HOME/config.yaml" ] && hermes_line=$'[hermes]\n# Voice-call LLM / TTS / STT: same provider, model and voice Hermes uses.\nconfig = "'"$HERMES_HOME"'/config.yaml"\n'
  [ "$DRY" = 1 ] && { echo "   [dry-run] write $cfg"; return; }
  mkdir -p "$BRIDGE_HOME"; chmod 700 "$BRIDGE_HOME"
  ( umask 077; cat > "$cfg" <<EOF
[telegram]
api_id = ${api_id:-0}
api_hash = "${api_hash}"
session = "${session}"
allowed_users = ["${owner}"]   # first entry = owner; add more people who may chat with Hermes

[server]
host = "127.0.0.1"
port = 7599

[calls]
answer_calls = true
max_minutes = 15
assistant_name = "Hermes"

${hermes_line}
# Override or add endpoints (OpenAI-compatible). Keys can also come from the environment:
# TG_BRIDGE_LLM_API_KEY, TG_BRIDGE_STT_API_KEY, TG_BRIDGE_TTS_API_KEY.
# [llm]
# base_url = "http://localhost:9999/v1"
# model = "auto"
EOF
  )
  ok "wrote $cfg (mode 600)"
}

# ---------------------------------------------------------------------------------- plugin
setup_plugin() {
  say "Hermes plugin"
  local hermes; hermes="$(find_hermes)"
  [ -n "$hermes" ] || { warn "hermes CLI not found (set HERMES_BIN)"; exit 1; }

  if [ -d "$HERMES_HOME/plugins/telegram-user" ]; then
    skip "plugin already installed: $HERMES_HOME/plugins/telegram-user"
  else
    run "$hermes" plugins install "$GITHUB_REPO" --enable
  fi

  set_if_missing() {
    local key="$1" value="$2" current
    current="$("$hermes" config get "$key" 2>/dev/null | tail -1 || true)"
    if [ -n "$current" ] && [ "$current" != "None" ] && [ "$current" != "null" ]; then
      skip "$key already set"
    else
      run "$hermes" config set "$key" "$value" >/dev/null; ok "$key = $value"
    fi
  }

  set_if_missing platforms.telegram_user.enabled true
  local token_file owner_id=""
  token_file="$("$VENV/bin/python" -c 'from tg_user_bridge import config; print(config.load().token_file)' 2>/dev/null || true)"
  [ -n "$token_file" ] && [ "$token_file" != "$HOME/.config/tg-user-bridge/token" ] && {
    set_if_missing platforms.telegram_user.extra.token_file "$token_file"
    set_if_missing plugins.entries.telegram-user.settings.token_file "$token_file"
  }
  if [ -n "$token_file" ] && [ -f "$token_file" ]; then
    owner_id="$(curl -fsS -H "Authorization: Bearer $(cat "$token_file")" http://127.0.0.1:7599/api/health 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["owner_id"])' 2>/dev/null || true)"
  fi
  if [ -n "$owner_id" ]; then
    set_if_missing platforms.telegram_user.extra.allowed_users "[\"$owner_id\"]"
    set_if_missing platforms.telegram_user.extra.home_channel "$owner_id"
  else
    warn "bridge not reachable: set platforms.telegram_user.extra.allowed_users to your numeric Telegram id later"
  fi

  local bot; bot="$("$hermes" config get telegram.enabled 2>/dev/null | tail -1 || true)"
  if [ "$bot" = "True" ] || [ "$bot" = "true" ]; then
    if [ "$DRY" = 0 ] && yes_no "Turn off the Telegram *bot* platform now (the account replaces it)?" n; then
      "$hermes" config set telegram.enabled false >/dev/null; ok "bot platform disabled (undo: hermes config set telegram.enabled true)"
    else
      skip "bot platform left on; switch later with: hermes config set telegram.enabled false"
    fi
  fi
  say "done. Restart the gateway to connect: hermes gateway restart"
}

[ "$DO_BRIDGE" = 1 ] && setup_bridge
[ "$DO_PLUGIN" = 1 ] && setup_plugin
exit 0
