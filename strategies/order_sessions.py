"""Exchange-session boundaries shared by execution and order monitoring."""
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


@lru_cache(maxsize=1024)
def session_bounds(day: date, market: str = "NYSE") -> tuple[datetime, datetime] | None:
    """Return the actual regular session, including holidays and early closes."""
    import pandas_market_calendars as mcal

    schedule = mcal.get_calendar(market).schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return row["market_open"].to_pydatetime(), row["market_close"].to_pydatetime()


def day_session_ended(created_at: datetime | str | None, now: datetime, *, market: str = "NYSE") -> bool:
    """A DAY order belongs to the first regular session closing after creation.

    Unknown creation times cannot establish expiry. Orders queued after close
    belong to the next session. Naive ledger/broker timestamps are UTC.
    """
    try:
        created = created_at if isinstance(created_at, datetime) else datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    created = created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    if created > now:
        return False
    day = created.astimezone(EASTERN).date()
    for offset in range(10):
        bounds = session_bounds(day + timedelta(days=offset), market)
        if bounds and created < bounds[1]:
            return now >= bounds[1]
    return False
