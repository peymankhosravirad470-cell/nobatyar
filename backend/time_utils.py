"""Time helpers for appointment datetimes stored as naive Tehran wall time."""

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    _TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # pragma: no cover - fallback for minimal Python environments
    _TEHRAN = timezone(timedelta(hours=3, minutes=30))


def tehran_now_naive() -> datetime:
    """Return current Tehran wall time without tzinfo, matching stored slot values."""
    return datetime.now(_TEHRAN).replace(tzinfo=None)
