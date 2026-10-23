from datetime import datetime, timedelta

FRIDAY = 4
COMMANDS = {
    "resumen_diario": "daily",
    "resumen-diario": "daily",
    "resumen_semanal": "weekly",
    "resumen-semanal": "weekly",
}


def parse_clock(value: str) -> tuple[int, int] | None:
    raw = (value or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid clock time: {value}")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid clock time: {value}")
    return hour, minute


def clock_due(
    now: datetime,
    clock: str,
    sent_on: str | None,
    weekday: int | None = None,
) -> bool:
    parsed = parse_clock(clock)
    if parsed is None:
        return False
    if weekday is not None and now.weekday() != weekday:
        return False
    if (sent_on or "") == now.date().isoformat():
        return False
    return (now.hour, now.minute) >= parsed


def job_ready(
    now: datetime,
    clock: str,
    sent_on: str | None,
    retry_after: str | None,
    weekday: int | None = None,
) -> bool:
    if not clock_due(now, clock, sent_on, weekday):
        return False
    if not (retry_after or "").strip():
        return True
    retry = datetime.fromisoformat(retry_after)
    if retry.tzinfo is None:
        retry = retry.replace(tzinfo=now.tzinfo)
    return now >= retry


def burst_notice(alerting: bool, count: int, threshold: int) -> tuple[bool, bool]:
    """Return whether the group stays latched, and whether this check should post."""
    if threshold <= 0 or count <= threshold:
        return False, False
    if alerting:
        return True, False
    return True, True


def groups_to_post(groups: list[dict], skip_empty: bool) -> list[dict]:
    if not skip_empty:
        return list(groups)
    return [group for group in groups if not group.get("empty")]


def parse_text_command(text: str) -> tuple[str, str | None] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    parts = raw.split(None, 1)
    name = parts[0].lstrip("/").split("@", 1)[0].lower()
    if name == "grupos":
        return ("grupos", None)
    period = COMMANDS.get(name)
    if period is None:
        return None
    group = parts[1].strip() if len(parts) > 1 else ""
    return (period, group or None)


def burst_elapsed(now: datetime, checked_at: str | None, minutes: int) -> bool:
    if minutes <= 0 or not (checked_at or "").strip():
        return False
    previous = datetime.fromisoformat(checked_at)
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=now.tzinfo)
    return now - previous >= timedelta(minutes=minutes)
