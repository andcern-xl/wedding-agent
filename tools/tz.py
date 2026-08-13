"""Local calendar dates. Railway runs UTC; the couple lives in Singapore.

`date.today()` on the server returns the UTC date, which is YESTERDAY for
anything between 00:00 and 08:00 SGT — the late-night window this bot actually
gets used in. A task added at 1am SGT was getting yesterday's due_date, "due
today" queries compared against yesterday, and day counts came out one short.

Every calendar DATE the bot stores or compares (due_date, fact_date, as_of,
"due today", day counts) must come from here. Instants stay
`datetime.now(timezone.utc)` — created_at/updated_at are timestamptz, and UTC
is correct for those.
"""
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

try:
    LOCAL_TZ = ZoneInfo(os.getenv("REMINDER_TZ", "Asia/Singapore"))
except Exception:
    LOCAL_TZ = ZoneInfo("UTC")


def local_now() -> datetime:
    """Now, as an aware datetime in the couple's timezone."""
    return datetime.now(LOCAL_TZ)


def local_today() -> date:
    """Today's calendar date where the users are — not where the server is."""
    return local_now().date()


def local_today_iso() -> str:
    return local_today().isoformat()


def days_from_today(n: int) -> str:
    """ISO date n days from the local today (negative for the past)."""
    return (local_today() + timedelta(days=n)).isoformat()
