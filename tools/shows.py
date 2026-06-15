from datetime import date, timedelta
from tools.db import get_client

ANSEN_ID = 63756531


def add_show(
    show_name: str,
    venue: str | None = None,
    show_date: str | None = None,
    show_time: str | None = None,
    notes: str | None = None,
) -> dict:
    row: dict = {"user_id": ANSEN_ID, "show_name": show_name, "calendar_added": False}
    if venue:
        row["venue"] = venue
    if show_date:
        row["show_date"] = show_date
    if show_time:
        row["show_time"] = show_time
    if notes:
        row["notes"] = notes
    return get_client().table("shows").insert(row).execute().data[0]


def get_upcoming_shows(include_past: bool = False) -> list[dict]:
    today = date.today().isoformat()
    q = get_client().table("shows").select("*").eq("user_id", ANSEN_ID).order("show_date", desc=False)
    if not include_past:
        q = q.gte("show_date", today)
    return q.execute().data


def get_show_by_id(show_id: str) -> dict | None:
    rows = get_client().table("shows").select("*").eq("id", show_id).execute().data
    return rows[0] if rows else None


def get_shows_in_n_days(n: int) -> list[dict]:
    target = (date.today() + timedelta(days=n)).isoformat()
    return get_client().table("shows").select("*").eq("show_date", target).execute().data


def mark_calendar_added(show_id: str) -> bool:
    result = get_client().table("shows").update({"calendar_added": True}).eq("id", show_id).execute()
    return bool(result.data)
