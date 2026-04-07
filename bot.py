#!/usr/bin/env python3
"""Home Assistant Telegram Bot powered by Claude AI with full tool calling."""

import json
import logging
import os
from datetime import datetime, timezone

import httpx
import anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
HA_URL: str = os.environ["HA_URL"].rstrip("/")
HA_TOKEN: str = os.environ["HA_TOKEN"]
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_HISTORY: int = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))

_raw_ids = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "")
ALLOWED_USERS: set[int] = (
    {int(uid.strip()) for uid in _raw_ids.split(",") if uid.strip()}
    if _raw_ids.strip()
    else set()
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
ha_headers = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}
anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# Per-user conversation history: {user_id: [{"role": ..., "content": ...}]}
conversation_history: dict[int, list[dict]] = {}

# ---------------------------------------------------------------------------
# HA REST helpers
# ---------------------------------------------------------------------------

async def ha_get(path: str, params: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(headers=ha_headers, timeout=30) as client:
        r = await client.get(f"{HA_URL}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def ha_post(path: str, body: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(headers=ha_headers, timeout=30) as client:
        r = await client.post(f"{HA_URL}{path}", json=body or {})
        r.raise_for_status()
        return r.json()


async def ha_put(path: str, body: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(headers=ha_headers, timeout=30) as client:
        r = await client.put(f"{HA_URL}{path}", json=body or {})
        r.raise_for_status()
        return r.json()


async def ha_delete(path: str) -> dict | list | str:
    async with httpx.AsyncClient(headers=ha_headers, timeout=30) as client:
        r = await client.delete(f"{HA_URL}{path}")
        r.raise_for_status()
        return r.text or "{}"

# ---------------------------------------------------------------------------
# Tool definitions (Anthropic schema)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "get_states",
        "description": (
            "Read the current state of Home Assistant entities. "
            "Always provide entity_id OR domain — never call this with no arguments. "
            "Fetching all states at once is very expensive and will cause rate limit errors. "
            "Use domain filter (e.g. 'light', 'sensor') when you don't know the exact entity_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "e.g. 'light.living_room'. Omit to list all.",
                },
                "domain": {
                    "type": "string",
                    "description": "Filter by domain when listing all, e.g. 'light', 'switch'.",
                },
            },
        },
    },
    {
        "name": "call_service",
        "description": (
            "Call a Home Assistant service to control a device or trigger an action. "
            "Examples: domain='light', service='turn_on'; domain='climate', service='set_temperature'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Service domain, e.g. 'light'."},
                "service": {"type": "string", "description": "Service name, e.g. 'turn_on'."},
                "service_data": {
                    "type": "object",
                    "description": "Payload, e.g. {\"entity_id\": \"light.desk\", \"brightness\": 200}.",
                },
            },
            "required": ["domain", "service"],
        },
    },
    {
        "name": "get_history",
        "description": "Retrieve state history for an entity over the past N hours.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "Entity to query."},
                "hours": {
                    "type": "number",
                    "description": "How many hours back to fetch. Defaults to 24.",
                },
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "list_automations",
        "description": "List all automation entities with their id, alias, state, and last_triggered.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_automation",
        "description": "Fetch the full YAML/JSON config of a specific automation by its config id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {
                    "type": "string",
                    "description": "The automation config id (from list_automations unique_id).",
                }
            },
            "required": ["automation_id"],
        },
    },
    {
        "name": "create_automation",
        "description": (
            "Create a new automation. Provide the full automation config object "
            "(alias, description, trigger, condition, action, mode). "
            "A unique id will be generated if omitted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "description": "Full automation config dict.",
                }
            },
            "required": ["config"],
        },
    },
    {
        "name": "update_automation",
        "description": "Update an existing automation. Merges supplied fields with current config.",
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "config": {
                    "type": "object",
                    "description": "Fields to update (partial or full config).",
                },
            },
            "required": ["automation_id", "config"],
        },
    },
    {
        "name": "delete_automation",
        "description": "Delete an automation by its config id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"}
            },
            "required": ["automation_id"],
        },
    },
    {
        "name": "manage_config_flow",
        "description": (
            "Interact with the Home Assistant config flow API to set up integrations. "
            "Use action='init' to start a flow, 'get' to read the current step, "
            "'submit' to send step data, 'abort' to cancel. "
            "For OAuth integrations, the response will contain an auth URL to send the user. "
            "For discovery-based integrations, returns a deep link to open HA."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["init", "get", "submit", "abort"],
                    "description": "Operation to perform.",
                },
                "handler": {
                    "type": "string",
                    "description": "Integration domain name (required for action='init'), e.g. 'hue', 'spotify'.",
                },
                "flow_id": {
                    "type": "string",
                    "description": "Flow ID returned by 'init' (required for get/submit/abort).",
                },
                "data": {
                    "type": "object",
                    "description": "Form data to submit (for action='submit').",
                },
            },
            "required": ["action"],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def tool_get_states(entity_id: str | None = None, domain: str | None = None) -> str:
    if entity_id:
        data = await ha_get(f"/api/states/{entity_id}")
        return json.dumps(data, indent=2)
    data = await ha_get("/api/states")
    if domain:
        data = [s for s in data if s["entity_id"].startswith(f"{domain}.")]
    # Slim down: keep entity_id, state, attributes, last_changed
    slim = [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "attributes": s.get("attributes", {}),
            "last_changed": s.get("last_changed"),
        }
        for s in data
    ]
    return json.dumps(slim, indent=2)


async def tool_call_service(domain: str, service: str, service_data: dict | None = None) -> str:
    result = await ha_post(f"/api/services/{domain}/{service}", service_data or {})
    return json.dumps(result, indent=2)


async def tool_get_history(entity_id: str, hours: float = 24) -> str:
    from datetime import timedelta
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    start_str = start.strftime("%Y-%m-%dT%H:%M:%S%z")
    data = await ha_get(
        f"/api/history/period/{start_str}",
        params={"filter_entity_id": entity_id, "minimal_response": "true"},
    )
    return json.dumps(data, indent=2)


async def tool_list_automations() -> str:
    data = await ha_get("/api/states")
    automations = [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "alias": s["attributes"].get("friendly_name"),
            "last_triggered": s["attributes"].get("last_triggered"),
            "unique_id": s["attributes"].get("id"),
        }
        for s in data
        if s["entity_id"].startswith("automation.")
    ]
    return json.dumps(automations, indent=2)


async def tool_get_automation(automation_id: str) -> str:
    data = await ha_get(f"/api/config/automation/config/{automation_id}")
    return json.dumps(data, indent=2)


async def tool_create_automation(config: dict) -> str:
    # HA requires a unique id; generate one if missing
    if "id" not in config:
        import uuid
        config["id"] = uuid.uuid4().hex
    automation_id = config["id"]
    result = await ha_post(f"/api/config/automation/config/{automation_id}", config)
    return json.dumps({"automation_id": automation_id, "result": result}, indent=2)


async def tool_update_automation(automation_id: str, config: dict) -> str:
    # Fetch current config and merge
    current = await ha_get(f"/api/config/automation/config/{automation_id}")
    merged = {**current, **config, "id": automation_id}
    result = await ha_post(f"/api/config/automation/config/{automation_id}", merged)
    return json.dumps(result, indent=2)


async def tool_delete_automation(automation_id: str) -> str:
    result = await ha_delete(f"/api/config/automation/config/{automation_id}")
    return json.dumps({"deleted": automation_id, "response": result})


async def tool_manage_config_flow(
    action: str,
    handler: str | None = None,
    flow_id: str | None = None,
    data: dict | None = None,
) -> str:
    base = "/api/config/config_entries/flow"

    if action == "init":
        if not handler:
            return json.dumps({"error": "handler required for action=init"})
        result = await ha_post(base, {"handler": handler})
        # Enrich response with helpful hints
        step_type = result.get("type")
        out: dict = {"flow_id": result.get("flow_id"), "step_type": step_type, "raw": result}
        if step_type == "external":
            step_id = result.get("step_id")
            url = result.get("url") or result.get("description_placeholders", {}).get("url")
            out["auth_url"] = url
            out["hint"] = (
                f"OAuth step detected. Send this URL to the user to authorize: {url}"
                if url
                else f"External step '{step_id}'. Check raw for details."
            )
        elif step_type == "form":
            out["schema"] = result.get("data_schema")
            out["hint"] = "Fill in the form and call action=submit with the data."
        elif step_type == "abort":
            out["reason"] = result.get("reason")
        return json.dumps(out, indent=2)

    if action == "get":
        if not flow_id:
            return json.dumps({"error": "flow_id required for action=get"})
        result = await ha_get(f"{base}/{flow_id}")
        return json.dumps(result, indent=2)

    if action == "submit":
        if not flow_id:
            return json.dumps({"error": "flow_id required for action=submit"})
        result = await ha_post(f"{base}/{flow_id}", data or {})
        return json.dumps(result, indent=2)

    if action == "abort":
        if not flow_id:
            return json.dumps({"error": "flow_id required for action=abort"})
        result = await ha_delete(f"{base}/{flow_id}")
        return json.dumps({"aborted": flow_id, "response": result})

    return json.dumps({"error": f"Unknown action: {action}"})


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

async def execute_tool(name: str, inputs: dict) -> str:
    try:
        match name:
            case "get_states":
                return await tool_get_states(**inputs)
            case "call_service":
                return await tool_call_service(**inputs)
            case "get_history":
                return await tool_get_history(**inputs)
            case "list_automations":
                return await tool_list_automations()
            case "get_automation":
                return await tool_get_automation(**inputs)
            case "create_automation":
                return await tool_create_automation(**inputs)
            case "update_automation":
                return await tool_update_automation(**inputs)
            case "delete_automation":
                return await tool_delete_automation(**inputs)
            case "manage_config_flow":
                return await tool_manage_config_flow(**inputs)
            case _:
                return json.dumps({"error": f"Unknown tool: {name}"})
    except httpx.HTTPStatusError as e:
        log.warning("HA API error for tool %s: %s", name, e)
        return json.dumps({"error": f"HA API {e.response.status_code}: {e.response.text}"})
    except Exception as e:
        log.exception("Tool %s failed", name)
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Claude agentic loop
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a helpful Home Assistant controller. You have access to the user's \
Home Assistant instance via a set of tools. Use them to read states, control \
devices, inspect history, and manage automations.

## Token efficiency — IMPORTANT
You are running under a strict API token budget. Violating these rules causes \
rate limit errors that break the bot:

- NEVER call get_states without an entity_id or domain filter. Fetching all \
  states at once is forbidden unless the user explicitly asks for a full dump.
- When the user asks about a device, infer the most likely entity_id or domain \
  and query only that. Examples:
    - "lights" → domain="light"
    - "bedroom temperature" → entity_id="sensor.bedroom_temperature" (try the \
      most obvious slug; if it errors, try domain="sensor" next)
    - "front door" → entity_id="binary_sensor.front_door" or "lock.front_door"
- For list_automations, the response is already slim — that is fine to call freely.
- Prefer one targeted call over multiple broad ones.
- If a targeted call returns an error (entity not found), broaden to the domain \
  filter, never to a full state dump.

## Behaviour
- Confirm before irreversible actions (delete automation, etc.) unless the user \
  already said to go ahead.
- When creating/updating automations, show a summary before saving.
- For OAuth flows: show the auth URL as a clickable link and tell the user to \
  complete it in their browser.
- Keep replies concise. Use bullet points for entity lists.
- Use UTC for times and say so if the user's timezone is unknown.
"""


async def ask_claude(user_id: int, user_message: str) -> str:
    """Run the full agentic loop and return the final text response."""
    history = conversation_history.setdefault(user_id, [])
    history.append({"role": "user", "content": user_message})

    # Trim history to avoid token bloat
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]

    # Track how many entries we add so we can roll back on error
    entries_added = 1  # the user message above

    max_iterations = 10
    try:
        for _ in range(max_iterations):
            response = await anthropic_client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,  # type: ignore[arg-type]
                messages=history,
            )

            # Collect all content blocks
            assistant_content = response.content
            history.append({"role": "assistant", "content": assistant_content})
            entries_added += 1

            if response.stop_reason == "end_turn":
                text_parts = [b.text for b in assistant_content if b.type == "text"]
                return "\n".join(text_parts) or "(no response)"

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in assistant_content:
                    if block.type != "tool_use":
                        continue
                    log.info("Tool call: %s(%s)", block.name, block.input)
                    result = await execute_tool(block.name, block.input)
                    log.info("Tool result: %s…", result[:200])
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
                history.append({"role": "user", "content": tool_results})
                entries_added += 1
                continue

            # Unexpected stop reason
            break

    except Exception:
        # Roll back history to avoid corrupted state (orphaned tool_use blocks)
        del history[-entries_added:]
        raise

    return "Sorry, I hit the maximum number of steps. Please try a simpler request."


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

def is_authorized(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    await update.message.reply_text(
        f"Hi {user.first_name}! I'm your Home Assistant AI assistant.\n"
        "Ask me anything about your home — I can read states, control devices, "
        "view history, and manage automations.\n\n"
        "Use /clear to reset the conversation."
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    conversation_history.pop(user_id, None)
    await update.message.reply_text("Conversation history cleared.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "*Commands*\n"
        "/start — Welcome message\n"
        "/clear — Reset conversation context\n"
        "/help — This message\n\n"
        "*Example prompts*\n"
        "• _Turn off all lights in the living room_\n"
        "• _What's the temperature in the bedroom?_\n"
        "• _Show me the history of motion sensor 1 for the last 6 hours_\n"
        "• _Create an automation that turns on the porch light at sunset_\n"
        "• _Set up the Spotify integration_",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("Unauthorized.")
        return

    text = update.message.text or ""
    if not text.strip():
        return

    log.info("User %d: %s", user.id, text[:100])

    # Show typing indicator
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    try:
        reply = await ask_claude(user.id, text)
    except anthropic.RateLimitError:
        await update.message.reply_text(
            "Rate limit reached — please wait a moment and try again."
        )
        return
    except anthropic.BadRequestError as e:
        log.error("Anthropic bad request (history cleared): %s", e)
        conversation_history.pop(user.id, None)
        await update.message.reply_text(
            "Something went wrong with the conversation state — I've reset it. Please try again."
        )
        return
    except Exception as e:
        log.exception("Unexpected error in ask_claude")
        await update.message.reply_text(f"Unexpected error: {e}")
        return

    # Telegram max message length is 4096; split if needed
    for chunk in _split_message(reply):
        await update.message.reply_text(chunk, parse_mode="Markdown")


def _split_message(text: str, limit: int = 4096) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Starting ha-telegram-bot (model=%s)", CLAUDE_MODEL)

    request = HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30, pool_timeout=30)
    app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
