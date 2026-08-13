from datetime import date
from tools.db import get_client
from tools.tz import local_today


def add_trip(
    destination: str,
    country: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    status: str = "planning",
    notes: str | None = None,
    visibility: str = "shared",
) -> dict:
    row: dict = {"destination": destination, "status": status, "visibility": visibility}
    if country:
        row["country"] = country
    if start_date:
        row["start_date"] = start_date
    if end_date:
        row["end_date"] = end_date
    if notes:
        row["notes"] = notes
    return get_client().table("trips").insert(row).execute().data[0]


def get_upcoming_trips() -> list[dict]:
    today = local_today().isoformat()
    return (
        get_client().table("trips").select("*")
        .gte("end_date", today)
        .neq("status", "cancelled")
        .order("start_date")
        .execute().data or []
    )


def get_all_trips() -> list[dict]:
    return get_client().table("trips").select("*").order("start_date", desc=True).execute().data or []


def get_trip_by_id(trip_id: str) -> dict | None:
    rows = get_client().table("trips").select("*").eq("id", trip_id).execute().data
    return rows[0] if rows else None


def find_trips_by_destination(destination: str) -> list[dict]:
    all_trips = get_client().table("trips").select("*").order("start_date").execute().data or []
    dest_lower = destination.lower()
    return [t for t in all_trips if dest_lower in (t.get("destination") or "").lower()]


def update_trip(trip_id: str, **kwargs) -> bool:
    allowed = {"destination", "country", "start_date", "end_date", "status", "visa_ansen", "visa_jess", "notes", "visibility"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return False
    result = get_client().table("trips").update(updates).eq("id", trip_id).execute()
    return bool(result.data)


def append_trip_note(trip_id: str, note: str) -> bool:
    trip = get_trip_by_id(trip_id)
    if not trip:
        return False
    existing = trip.get("notes") or ""
    updated = (existing + "\n" + note).strip()
    result = get_client().table("trips").update({"notes": updated}).eq("id", trip_id).execute()
    return bool(result.data)


def delete_trip(trip_id: str) -> bool:
    result = get_client().table("trips").delete().eq("id", trip_id).execute()
    return bool(result.data)
