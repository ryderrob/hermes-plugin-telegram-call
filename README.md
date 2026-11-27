# hermes-plugin-telegram-call

Run [Hermes Agent](https://github.com/NousResearch/hermes-agent) on a **Telegram user account** instead of a bot.

Chat, every Hermes slash command, files, voice notes and **live two-way voice calls**, all through an ordinary Telegram account. No bot token needed.

```
You (Telegram) ⇄ Telegram user account ⇄ tg-user-bridge (owns the session, localhost API) ⇄ Hermes plugin "telegram-user" ⇄ Hermes
```

## Features

- **A real Hermes platform** (`telegram_user`). Messages, replies, photos, documents, voice notes, the typing indicator and cron delivery to your chat.
- **All commands work.** User accounts have no bot command menu, so on first connect the plugin sends you the full command list and pins the first message. `/commands` shows the list again any time.
- **Voice calls** (`telegram_call`, `telegram_call_status` tools):
  - Hermes can call you. A live conversation follows (speech-to-text → LLM → text-to-speech).
  - Your calls to the account are answered.
  - It can join a group's voice chat.
- **Owner-only voice.** Only the owner (the first allowed user) can be called, and only the owner's calls are answered. In group voice chats, only the owner's voice is listened to; everyone else is ignored.
- **Uses Hermes's own settings.** By default the voice-call brain and voice come from your Hermes `config.yaml`: the same provider and model, the same TTS voice (`edge` voices supported), and local Whisper when Hermes uses local STT.
- **Safety:**
  - The agent can only message allowed users.
  - Outgoing calls are rate-limited (default: at least 30 min apart, at most 3 per day).
  - The session is locked so two processes can never share it.
  - The API listens on localhost only and requires a token.
- **Update-safe.** It installs as a user plugin (`~/.hermes/plugins/telegram-user`) and never patches Hermes.

## Install

Requirements: Linux with systemd (user services), Python ≥ 3.11, `ffmpeg` (for calls and voice notes), and Telegram API credentials from <https://my.telegram.org>.

```bash
git clone https://github.com/ryderrob/hermes-plugin-telegram-call
cd hermes-plugin-telegram-call
./setup.sh            # --dry-run to preview, --bridge-only / --plugin-only to split
hermes gateway restart
```

`setup.sh` is non-destructive:
- It never overwrites an existing file, config key, plugin or systemd unit.
- It never touches `~/.hermes/.env`.
- It asks before turning off your Telegram bot platform. The default answer is no, so you can run both side by side first.

What it does:

1. It installs `tg-user-bridge` into `~/.local/share/tg-user-bridge/venv`.
2. It writes `~/.config/tg-user-bridge/config.toml` (mode 600).
3. It offers the interactive Telegram login: phone number, code, and 2FA if you use it.
4. It installs and starts the `tg-user-bridge` user service.
5. It runs `hermes plugins install ryderrob/hermes-plugin-telegram-call --enable`.
6. It adds only the missing keys under `platforms.telegram_user` in `config.yaml`.

Manual alternative: `hermes plugins install ryderrob/hermes-plugin-telegram-call --enable`, then run `~/.hermes/plugins/telegram-user/setup.sh --bridge-only`.

## Configuration

### Bridge: `~/.config/tg-user-bridge/config.toml`

```toml
[telegram]
api_id = 123456
api_hash = "…"
session = "~/.config/tg-user-bridge/user"
allowed_users = ["@you", "@partner"]   # first = owner (calls + group voice); others may chat

[server]
port = 7599                    # 127.0.0.1 only
# token_file = "~/.config/tg-user-bridge/token"

[calls]
answer_calls = true            # pick up the owner's calls
max_minutes = 15
assistant_name = "Hermes"

[hermes]
config = "~/.hermes/config.yaml"   # inherit LLM / TTS / STT from Hermes

[llm]                          # anything here overrides what was inherited
reasoning_effort = "low"       # extra keys are passed to /chat/completions

[limits]
min_call_gap_s = 1800
max_calls_per_day = 3
```

API keys stay out of the file. Set them as `TG_BRIDGE_LLM_API_KEY`, `TG_BRIDGE_STT_API_KEY` and `TG_BRIDGE_TTS_API_KEY`, for example in `~/.config/tg-user-bridge/env`, which the systemd unit loads.

### Hermes: `config.yaml`

```yaml
platforms:
  telegram_user:
    enabled: true
    extra:
      allowed_users: ["123456789"]   # your numeric Telegram id (Hermes-side allowlist)
      home_channel: 123456789        # where cron jobs deliver
      # bridge_url: http://127.0.0.1:7599
      # token_file: ~/.config/tg-user-bridge/token
telegram:
  enabled: false                     # optional: retire the bot
```

## Bridge API (localhost, `Authorization: Bearer <token>`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | account, owner, allowed users, whether calls are available |
| GET | `/api/events?since=N` | Server-Sent Events: `message`, `call_ended`, `call_failed` |
| POST | `/api/send` | `{chat_id, text?, file?, voice_note?, reply_to?}` (allowed users only) |
| POST | `/api/typing`, `/api/pin`, `/api/edit` | typing indicator, pin, edit |
| POST | `/api/call` | `{chat_id?, greeting?, instructions?, max_minutes?}`: owner, or a negative group id |
| GET | `/api/call/last` | live calls plus the last transcript |
| POST | `/notify` | `{text, call?, instructions?}`: simple owner notification (also accepts the `X-Sink-Token` header) |

## Notes

- Telegram allows one active connection per session. Don't run another client on the same session file; the bridge refuses to start if one holds the lock.
- Automating a user account is subject to Telegram's terms. The default rate limits are conservative, and calls are never retried automatically.
- Approval prompts arrive as text. Answer them with the commands Hermes shows.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e "bridge[calls]" pytest
.venv/bin/pytest -q tests
hermes plugins validate . && hermes plugins doctor .
```

MIT licensed. The call engine is adapted from the openjarvis-telegram plugin.
