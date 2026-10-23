import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from telegram import BotCommand, Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from schedule import (
    FRIDAY,
    burst_elapsed,
    burst_notice,
    groups_to_post,
    job_ready,
    parse_clock,
    parse_text_command,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-bot")

TELEGRAM_MESSAGE_LIMIT = 4096
AGENT_TIMEOUT_SECONDS = 900
CHECK_INTERVAL_SECONDS = 30
HEALTH_INTERVAL = timedelta(minutes=1)
RETRY_AFTER_FAILURE = timedelta(minutes=15)
LOGOUT_NOTICE = "WhatsApp is logged out. Link it again from the QR page."
COMMANDS = {
    "resumen_diario": "daily",
    "resumen_semanal": "weekly",
}


class AgentError(Exception):
    """The agent did not return a digest."""


def env_or_exit(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        logger.error("%s is required", name)
        sys.exit(1)
    return value


def parse_channel_id(raw: str):
    if raw.startswith("@"):
        return raw
    try:
        return int(raw)
    except ValueError:
        logger.error("TELEGRAM_CHANNEL_ID must be a numeric id like -100... or a @channel name")
        sys.exit(1)


def parse_allowed_user_ids() -> set[int] | None:
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return None
    allowed = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            allowed.add(int(part))
        except ValueError:
            logger.error("TELEGRAM_ALLOWED_USER_IDS must be a comma-separated list of numeric user ids")
            sys.exit(1)
    return allowed


def agent_base() -> str:
    return (os.environ.get("AGENT_URL", "http://agent:8081").strip() or "http://agent:8081").rstrip("/")


def digest_timezone() -> ZoneInfo:
    name = os.environ.get("DIGEST_TIMEZONE", "Europe/Madrid").strip() or "Europe/Madrid"
    return ZoneInfo(name)


def state_path() -> Path:
    return Path(os.environ.get("BOT_STATE_PATH", "/state/bot-state.json"))


def default_state() -> dict:
    return {
        "daily_sent_on": "",
        "weekly_sent_on": "",
        "daily_retry_after": "",
        "weekly_retry_after": "",
        "daily_error_posted": False,
        "weekly_error_posted": False,
        "daily_error_day": "",
        "weekly_error_day": "",
        "burst_checked_at": "",
        "burst_retry_after": "",
        "burst_alerting": {},
        "whatsapp_alerted": False,
        "health_checked_at": "",
    }


def load_state() -> dict:
    state = default_state()
    path = state_path()
    if not path.exists():
        return state
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return state
    if isinstance(loaded, dict):
        state.update(loaded)
    if not isinstance(state.get("burst_alerting"), dict):
        state["burst_alerting"] = {}
    return state


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(path)


def positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.error("%s must be an integer", name)
        return default


def configured_clock(name: str) -> str:
    raw = os.environ.get(name, "")
    try:
        parse_clock(raw)
    except ValueError as exc:
        logger.error("%s", exc)
        return ""
    return raw


def split_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    parts = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return parts


async def request_json(method: str, path: str, payload: dict | None = None, timeout_seconds: float = 30) -> dict:
    timeout = httpx.Timeout(timeout_seconds, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, agent_base() + path, json=payload)
    except httpx.TimeoutException as exc:
        raise AgentError("The agent timed out.") from exc
    except httpx.HTTPError as exc:
        raise AgentError(f"The agent is unreachable: {exc}") from exc
    if response.status_code != 200:
        reason = f"The agent returned HTTP {response.status_code}."
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("error"):
            reason = str(body["error"])
        raise AgentError(reason)
    try:
        body = response.json()
    except ValueError as exc:
        raise AgentError("The agent returned a response that is not JSON.") from exc
    if not isinstance(body, dict):
        raise AgentError("The agent returned an unexpected response.")
    return body


def digest_entries(body: dict) -> list[dict]:
    groups = body.get("groups")
    if not isinstance(groups, list) or not groups:
        raise AgentError("The agent returned an empty digest.")
    entries = []
    for item in groups:
        if not isinstance(item, dict):
            raise AgentError("The agent returned an unexpected digest.")
        name = item.get("name")
        text = item.get("text")
        if not isinstance(name, str) or not name.strip():
            raise AgentError("The agent returned an unexpected digest.")
        if not isinstance(text, str) or not text.strip():
            raise AgentError("The agent returned an empty digest.")
        entries.append({"name": name.strip(), "text": text.strip(), "empty": bool(item.get("empty"))})
    return entries


async def request_digest(period: str, group: str | None = None) -> list[dict]:
    payload = {"period": period}
    if group:
        payload["group"] = group
    body = await request_json("POST", "/digests", payload, AGENT_TIMEOUT_SECONDS)
    return digest_entries(body)


async def request_groups() -> list[str]:
    body = await request_json("GET", "/groups")
    groups = body.get("groups")
    if not isinstance(groups, list):
        raise AgentError("The agent returned an unexpected group list.")
    names = []
    for item in groups:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
            names.append(item["name"].strip())
    return names


async def request_activity(since: str) -> list[dict]:
    body = await request_json("POST", "/activity", {"since": since}, 60)
    groups = body.get("groups")
    if not isinstance(groups, list):
        raise AgentError("The agent returned an unexpected activity list.")
    activity = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        jid = item.get("jid")
        count = item.get("count")
        if not isinstance(name, str) or not isinstance(jid, str) or not isinstance(count, int):
            continue
        activity.append({"name": name, "jid": jid, "count": count})
    return activity


def user_is_allowed(update: Update, allowed: set[int] | None) -> bool:
    if allowed is None:
        return True
    user = update.effective_user
    return user is not None and user.id in allowed


async def post_entries(bot, channel_id, groups: list[dict]) -> None:
    for group in groups:
        for part in split_text(f"{group['name']}\n\n{group['text']}"):
            await bot.send_message(chat_id=channel_id, text=part)


async def deliver_digest(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    period: str,
    group: str | None = None,
) -> None:
    message = update.effective_message
    if message is None:
        return
    allowed = context.application.bot_data["allowed_user_ids"]
    if not user_is_allowed(update, allowed):
        await message.reply_text("You are not allowed to request a digest.")
        return

    await message.reply_text("Asking the agent for the digest.")
    try:
        groups = groups_to_post(await request_digest(period, group), skip_empty=False)
        channel_id = context.application.bot_data["channel_id"]
        await post_entries(context.bot, channel_id, groups)
    except AgentError as exc:
        logger.warning("Digest request failed: %s", exc)
        await message.reply_text(str(exc))
        return
    except TelegramError as exc:
        logger.error("Failed to post to the channel: %s", exc)
        await message.reply_text(f"Could not post to the channel: {exc}")
        return

    if message.chat_id != channel_id:
        await message.reply_text("Digest sent to the channel.")


async def deliver_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not user_is_allowed(update, context.application.bot_data["allowed_user_ids"]):
        await message.reply_text("You are not allowed to request a digest.")
        return
    try:
        names = await request_groups()
    except AgentError as exc:
        await message.reply_text(str(exc))
        return
    await message.reply_text("\n".join(names) if names else "No allowed groups are available.")


async def on_grupos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await deliver_groups(update, context)


def command_handler(period: str):
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        group = " ".join(context.args).strip() if context.args else None
        await deliver_digest(update, context, period, group or None)

    return handle


async def on_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    parsed = parse_text_command(message.text if message else "")
    if parsed is None:
        return
    kind, group = parsed
    if kind == "grupos":
        await deliver_groups(update, context)
        return
    await deliver_digest(update, context, kind, group)


async def register_commands(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("resumen_diario", "Pedir el resumen diario"),
        BotCommand("resumen_semanal", "Pedir el resumen semanal"),
        BotCommand("grupos", "Listar los grupos incluidos"),
    ])
    logger.info("Registered Telegram commands")


async def post_failure(application: Application, state: dict, period: str, now: datetime, reason: str) -> None:
    key = "daily" if period == "daily" else "weekly"
    today = now.date().isoformat()
    if state.get(f"{key}_error_day") != today:
        state[f"{key}_error_posted"] = False
    if not state.get(f"{key}_error_posted"):
        try:
            await application.bot.send_message(
                chat_id=application.bot_data["channel_id"],
                text=f"Could not post the {period} digest: {reason}",
            )
            state[f"{key}_error_posted"] = True
            state[f"{key}_error_day"] = today
        except TelegramError as exc:
            logger.error("Failed to post the digest error: %s", exc)
    state[f"{key}_retry_after"] = (now + RETRY_AFTER_FAILURE).isoformat()


async def run_digest_job(application: Application, state: dict, period: str, now: datetime) -> None:
    key = "daily" if period == "daily" else "weekly"
    try:
        groups = groups_to_post(await request_digest(period), skip_empty=True)
        if groups:
            await post_entries(application.bot, application.bot_data["channel_id"], groups)
    except AgentError as exc:
        logger.warning("Scheduled %s digest failed: %s", period, exc)
        await post_failure(application, state, period, now, str(exc))
        save_state(state)
        return
    except TelegramError as exc:
        logger.error("Scheduled %s digest could not be posted: %s", period, exc)
        await post_failure(application, state, period, now, str(exc))
        save_state(state)
        return
    state[f"{key}_sent_on"] = now.date().isoformat()
    state[f"{key}_retry_after"] = ""
    state[f"{key}_error_posted"] = False
    save_state(state)


async def run_burst_check(application: Application, state: dict, now: datetime) -> None:
    threshold = positive_int("MESSAGE_BURST_THRESHOLD", 50)
    interval = positive_int("MESSAGE_BURST_INTERVAL_MINUTES", 15)
    if threshold <= 0 or interval <= 0:
        return
    checked_at = state.get("burst_checked_at") or ""
    if not checked_at:
        state["burst_checked_at"] = now.isoformat()
        save_state(state)
        return
    retry_after = state.get("burst_retry_after") or ""
    if retry_after:
        retry_at = datetime.fromisoformat(retry_after)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=now.tzinfo)
        if now < retry_at:
            return
    if not burst_elapsed(now, checked_at, interval):
        return
    try:
        activity = await request_activity(checked_at)
    except AgentError as exc:
        logger.warning("Burst check failed: %s", exc)
        state["burst_retry_after"] = (now + timedelta(minutes=1)).isoformat()
        save_state(state)
        return
    alerting = state.get("burst_alerting") or {}
    lines = []
    updated = {}
    for group in activity:
        latched, notify = burst_notice(bool(alerting.get(group["jid"])), group["count"], threshold)
        if latched:
            updated[group["jid"]] = True
        if notify:
            lines.append(f"{group['name']}: {group['count']} new messages.")
    if lines:
        try:
            await application.bot.send_message(
                chat_id=application.bot_data["channel_id"],
                text="\n".join(lines),
            )
        except TelegramError as exc:
            logger.error("Failed to post the burst alert: %s", exc)
            state["burst_checked_at"] = now.isoformat()
            save_state(state)
            return
    state["burst_alerting"] = updated
    state["burst_checked_at"] = now.isoformat()
    state["burst_retry_after"] = ""
    save_state(state)


async def fetch_whatsapp_status() -> str:
    url = os.environ.get("WHATSAPP_HEALTH_URL", "http://whatsapp-bridge:8080/health").strip()
    url = url or "http://whatsapp-bridge:8080/health"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            response = await client.get(url)
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("WhatsApp health check failed: %s", exc)
        return "unreachable"
    status = body.get("status") if isinstance(body, dict) else None
    return status if isinstance(status, str) else "unreachable"


async def run_health_check(application: Application, state: dict, now: datetime) -> None:
    checked_at = state.get("health_checked_at") or ""
    if checked_at:
        previous = datetime.fromisoformat(checked_at)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=now.tzinfo)
        if now - previous < HEALTH_INTERVAL:
            return
    status = await fetch_whatsapp_status()
    if status == "connected":
        state["whatsapp_alerted"] = False
    elif not state.get("whatsapp_alerted"):
        try:
            await application.bot.send_message(
                chat_id=application.bot_data["channel_id"],
                text=LOGOUT_NOTICE,
            )
        except TelegramError as exc:
            logger.error("Failed to post the WhatsApp logout notice: %s", exc)
            state["health_checked_at"] = now.isoformat()
            save_state(state)
            return
        state["whatsapp_alerted"] = True
    state["health_checked_at"] = now.isoformat()
    save_state(state)


async def run_scheduled_checks(application: Application) -> None:
    now = datetime.now(digest_timezone())
    state = load_state()
    daily_clock = configured_clock("DIGEST_DAILY_AT")
    weekly_clock = configured_clock("DIGEST_WEEKLY_AT")
    if job_ready(now, daily_clock, state.get("daily_sent_on"), state.get("daily_retry_after")):
        await run_digest_job(application, state, "daily", now)
        state = load_state()
    if job_ready(now, weekly_clock, state.get("weekly_sent_on"), state.get("weekly_retry_after"), FRIDAY):
        await run_digest_job(application, state, "weekly", now)
        state = load_state()
    await run_burst_check(application, state, now)
    state = load_state()
    await run_health_check(application, state, now)


async def scheduler_loop(application: Application) -> None:
    while True:
        try:
            await run_scheduled_checks(application)
        except Exception:
            logger.exception("Scheduled check failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def start(application: Application) -> None:
    await register_commands(application)
    asyncio.create_task(scheduler_loop(application))


def main() -> None:
    token = env_or_exit("TELEGRAM_BOT_TOKEN")
    channel_id = parse_channel_id(env_or_exit("TELEGRAM_CHANNEL_ID"))
    allowed_user_ids = parse_allowed_user_ids()

    application = (
        Application.builder()
        .token(token)
        .post_init(start)
        .build()
    )
    application.bot_data["channel_id"] = channel_id
    application.bot_data["allowed_user_ids"] = allowed_user_ids

    for name, period in COMMANDS.items():
        application.add_handler(CommandHandler(name, command_handler(period)))
    application.add_handler(CommandHandler("grupos", on_grupos))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        on_plain_text,
    ))

    logger.info("Telegram bot polling; agent endpoint %s", agent_base())
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
