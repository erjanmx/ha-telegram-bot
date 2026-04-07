# ha-telegram-bot

A Telegram bot that lets you control Home Assistant through natural language, powered by Claude AI and the Anthropic tool-calling API.

## Features

- **Natural language control** — "Turn off all lights downstairs", "What's the bedroom temperature?"
- **Device control** — calls any HA service via `call_service`
- **State reading** — single entity or bulk, with optional domain filter
- **History queries** — "Show me the motion sensor history for the last 6 hours"
- **Automation CRUD** — list, read, create, update, and delete automations via the HA config API
- **Integration setup** — start config flows, handle OAuth redirects, send HA deep links for discovery integrations
- **Per-user conversation history** — Claude remembers context within a session
- **Access control** — allowlist of Telegram user IDs
- **systemd service** — runs as a hardened background service on your Proxmox LXC

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Available on Debian 12 (Bookworm) |
| Home Assistant | Accessible via HTTPS (reverse proxy recommended) |
| HA Long-Lived Access Token | Created in HA profile settings |
| Telegram Bot Token | Created via @BotFather |
| Anthropic API key | [console.anthropic.com](https://console.anthropic.com) |

---

## Setup

### 1. Create the Telegram bot

1. Open Telegram and message **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy the **bot token** (format: `123456789:ABC-...`)
4. Optionally disable group usage: `/setjoingroups` → Disable

To find your Telegram user ID, message **@userinfobot**.

### 2. Generate a HA Long-Lived Access Token

1. In Home Assistant, go to your **Profile** (bottom-left avatar)
2. Scroll to **Security** → **Long-Lived Access Tokens**
3. Click **Create Token**, give it a name (e.g. `telegram-bot`), copy the token

### 3. Install on the Proxmox LXC

SSH into your LXC and run the following as root:

```bash
# Install Python and git
apt update && apt install -y python3 python3-venv python3-pip git

# Create a dedicated user
useradd -r -s /bin/false -d /opt/ha-telegram-bot habot

# Clone the repo
git clone https://github.com/erjanmx/ha-telegram-bot.git /opt/ha-telegram-bot
cd /opt/ha-telegram-bot

# Set up virtualenv
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

# Configure environment
cp .env.example .env
nano .env          # Fill in all values (see Configuration below)

# Fix ownership
chown -R habot:habot /opt/ha-telegram-bot
chmod 600 /opt/ha-telegram-bot/.env
```

### 4. Configuration

Edit `/opt/ha-telegram-bot/.env`:

```dotenv
TELEGRAM_BOT_TOKEN=your_token_here
ALLOWED_TELEGRAM_USER_IDS=123456789        # your Telegram user ID
HA_URL=https://ha.yourdomain.com           # no trailing slash
HA_TOKEN=your_ha_long_lived_token_here
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-sonnet-4-6             # or claude-opus-4-6
MAX_HISTORY_MESSAGES=20
```

> **Security note:** Leave `ALLOWED_TELEGRAM_USER_IDS` empty to allow all users — only do this on a private bot. For a personal HA bot always restrict it.

### 5. Install the systemd service

```bash
cp /opt/ha-telegram-bot/ha-telegram-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ha-telegram-bot

# Check it started
systemctl status ha-telegram-bot
journalctl -u ha-telegram-bot -f
```

---

## Updating

```bash
cd /opt/ha-telegram-bot
git pull
venv/bin/pip install -r requirements.txt
systemctl restart ha-telegram-bot
```

---

## Usage

Send any natural language message to the bot. Examples:

### Device control
```
Turn off all the lights in the living room
Set the thermostat to 21 degrees
Lock the front door
```

### State queries
```
What lights are currently on?
What's the temperature and humidity in the bedroom?
Is the washing machine running?
```

### History
```
Show me the motion sensor history for the last 3 hours
When was the front door last opened today?
```

### Automations
```
List all my automations
Show me the "Evening lights" automation config
Create an automation that turns on the porch light at sunset
Disable the "Morning alarm" automation
Delete the automation with id abc123
```

### Integration setup
```
Set up the Spotify integration
Add the Philips Hue bridge
Start a config flow for the Nest thermostat
```

For OAuth-based integrations, Claude will return an authorization URL — open it in your browser, complete auth, then tell the bot to continue.

### Commands
| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/help` | Usage examples |
| `/clear` | Reset your conversation history |

---

## Architecture

```
Telegram user
    │  (message)
    ▼
bot.py (python-telegram-bot)
    │
    ▼
Anthropic API (Claude claude-sonnet-4-6)
    │  tool_use blocks
    ▼
Tool dispatcher
    ├── get_states          → GET  /api/states[/{entity_id}]
    ├── call_service        → POST /api/services/{domain}/{service}
    ├── get_history         → GET  /api/history/period/{timestamp}
    ├── list_automations    → GET  /api/states (domain=automation)
    ├── get_automation      → GET  /api/config/automation/config/{id}
    ├── create_automation   → POST /api/config/automation/config/{id}
    ├── update_automation   → POST /api/config/automation/config/{id}
    ├── delete_automation   → DELETE /api/config/automation/config/{id}
    └── manage_config_flow  → POST/GET/DELETE /api/config/config_entries/flow/...
```

Claude runs in an **agentic loop**: it may call multiple tools in sequence before returning a final answer. The loop is capped at 10 iterations per message.

---

## HA API notes

### Automation config IDs

The HA automation config API uses an internal config ID (not the entity ID). When you list automations with `list_automations`, the `unique_id` field is what you pass to `get_automation`, `update_automation`, and `delete_automation`.

### Config flows

- **OAuth integrations** (Spotify, Google, Nest, etc.) return a `type: external` step with an `auth_url`. The bot will present this URL to the user.
- **Discovery integrations** (Hue, Chromecast, etc.) may show a `type: form` or `type: confirm` step.
- **Zeroconf/mDNS discoveries** already in progress can be found at `GET /api/config/config_entries/flow`.

---

## Troubleshooting

**Bot doesn't respond**
```bash
journalctl -u ha-telegram-bot -n 50
```

**HA API 401 errors** — Regenerate your Long-Lived Access Token and update `.env`.

**HA API unreachable** — Verify `HA_URL` is reachable from the LXC:
```bash
curl -H "Authorization: Bearer $HA_TOKEN" $HA_URL/api/
```

**Anthropic rate limits** — Switch to `claude-haiku-4-5-20251001` for lower cost/higher throughput, or reduce `MAX_HISTORY_MESSAGES`.

**"Unauthorized" in Telegram** — Make sure your numeric user ID is in `ALLOWED_TELEGRAM_USER_IDS`.

---

## License

MIT
