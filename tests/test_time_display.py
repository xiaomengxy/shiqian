from datetime import datetime, timezone

from app.services.time_display import local_datetime, to_local_datetime


def test_local_datetime_treats_naive_values_as_utc():
    value = datetime(2026, 5, 29, 20, 0, 0)

    assert local_datetime(value) == "2026-05-30 04:00"


def test_local_datetime_handles_none():
    assert local_datetime(None) == ""
    assert to_local_datetime(None) is None


def test_local_datetime_converts_aware_values():
    value = datetime(2026, 5, 29, 20, 0, 0, tzinfo=timezone.utc)

    assert local_datetime(value) == "2026-05-30 04:00"
