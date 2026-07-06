from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from zoneinfo._common import ZoneInfoNotFoundError


try:
    BRASILIA_TZ = ZoneInfo("America/Sao_Paulo")
except ZoneInfoNotFoundError:
    BRASILIA_TZ = timezone(timedelta(hours=-3), name="America/Sao_Paulo")


def brasilia_timestamp() -> str:
    return datetime.now(BRASILIA_TZ).isoformat(timespec="seconds")


def console_line(message: str) -> str:
    return f"{brasilia_timestamp()} {message}"
