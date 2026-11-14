"""Single source of 'now' so agents and tests agree on time (local time, naive)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings


def now() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone)).replace(tzinfo=None, microsecond=0)


def parse_dt(value) -> datetime:
    """Accept datetime or ISO string ('2026-11-20T18:00')."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    return datetime.fromisoformat(str(value)).replace(tzinfo=None, microsecond=0)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="minutes") if value else None
