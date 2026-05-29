from __future__ import annotations

from datetime import datetime, timedelta, timezone


LOCAL_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def to_local_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LOCAL_TIMEZONE)


def local_datetime(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    local_value = to_local_datetime(value)
    if local_value is None:
        return ""
    return local_value.strftime(fmt)
