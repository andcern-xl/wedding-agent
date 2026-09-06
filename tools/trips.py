import re
from datetime import date, timedelta
from tools.db import get_client
from tools.tz import local_today

# One trip, logged twice, is how Seoul ended up as two rows — one holding the
# flights (SQ608/SQ611), the other the hotel (Moxy, confirmation, PIN) — so
# neither row could answer a question about the trip on its own, and the travel
# view listed it twice. Same for Phuket 17 Dec. add_trip was a bare insert.

_FLIGHT_RE = re.compile(r"\b([A-Z]{2})\s?(\d{2,4})\b")
# Destinations are written loosely: "Bangkok" vs "Bangkok (Wonderfruit 2026)".
_PAREN_RE = re.compile(r"\s*\(.*?\)\s*")


def _dest_key(name: str) -> str:
    return _PAREN_RE.sub(" ", (name or "")).strip().lower()


def _flights(text: str) -> set:
    return {f"{a}{b}" for a, b in _FLIGHT_RE.findall(text or "")}


def _overlaps(a_start, a_end, b_start, b_end, slack_days: int = 2) -> bool:
    """Do two date ranges describe the same window? Slack absorbs the day either
    side that creeps in when one row was logged off a flight and the other off a
    hotel booking."""
    def d(v):
        try:
            return date.fromisoformat(v) if v else None
        except (TypeError, ValueError):
            return None
    a1, a2, b1, b2 = d(a_start), d(a_end) or d(a_start), d(b_start), d(b_end) or d(b_start)
    if not a1 or not b1:
        return False
    slack = timedelta(days=slack_days)
    return a1 - slack <= (b2 or b1) and b1 - slack <= (a2 or a1)


def find_similar_trip(destination: str, start_date: str | None = None,
                      end_date: str | None = None, notes: str | None = None) -> dict | None:
    """An existing row describing the same journey, or None.

    Same place OR a shared flight number — but in both cases the dates must be
    compatible. A flight number alone is not identifying: SQ720 flies Singapore
    to Bangkok every day, and the existing Bangkok row proves the cost of
    assuming otherwise. It holds a bachelor trip on SQ720 10 Sep and a separate
    31 Aug trip on SQ0720, merged into one row that then reported neither."""
    key, new_flights = _dest_key(destination), _flights(notes or "")
    for t in get_client().table("trips").select("*").execute().data or []:
        if t.get("status") in ("cancelled", "merged"):
            continue
        same_place = key and key == _dest_key(t.get("destination") or "")
        shared_flight = bool(new_flights & _flights(t.get("notes") or ""))
        if not (same_place or shared_flight):
            continue
        dates_unknown = not (start_date and t.get("start_date"))
        if dates_unknown or _overlaps(start_date, end_date,
                                      t.get("start_date"), t.get("end_date")):
            return t
    return None


def _merge_notes(existing: str | None, incoming: str | None) -> str:
    """Keep both halves. Lines already present are not repeated — the Seoul rows
    would otherwise concatenate into the same flight details twice."""
    have = [ln.strip() for ln in (existing or "").split("\n") if ln.strip()]
    seen = {ln.lower() for ln in have}
    for ln in (incoming or "").split("\n"):
        ln = ln.strip()
        if ln and ln.lower() not in seen:
            have.append(ln)
            seen.add(ln.lower())
    return "\n".join(have)


_TRAVELLER_VISIBILITY = {"both": "shared", "ansen": "ansen", "jess": "jess"}


def add_trip(
    destination: str,
    country: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    status: str = "planning",
    notes: str | None = None,
    visibility: str = "shared",
    travellers: str | None = None,
    merge: bool = True,
) -> dict:
    """Add a trip, or fold it into the matching one that already exists.

    Returns the row, with `_merged` true when it joined an existing trip so the
    caller can say "added to your Seoul trip" rather than claim a new one.
    `_date_warning` is set when the range was impossible.
    """
    if travellers:
        visibility = _TRAVELLER_VISIBILITY.get(travellers.lower(), visibility)

    # An impossible range is how the Bangkok bachelor trip disappeared: it was
    # stored 2026-12-10 to 2026-09-04 for flights that fly 10-13 Sep, so it
    # sorted into December and never showed up as "next week". Keep the trip
    # visible, drop the end date that cannot be true, and say so.
    warning = None
    if start_date and end_date and end_date < start_date:
        warning = (f"end date {end_date} is before start {start_date} — kept the start, "
                   f"dropped the end. Confirm the real dates.")
        end_date = None

    if merge:
        existing = find_similar_trip(destination, start_date, end_date, notes)
        if existing:
            updates: dict = {}
            merged_notes = _merge_notes(existing.get("notes"), notes)
            if merged_notes != (existing.get("notes") or ""):
                updates["notes"] = merged_notes
            for field, value in (("country", country), ("start_date", start_date),
                                 ("end_date", end_date)):
                if value and not existing.get(field):
                    updates[field] = value
            # Only narrow when the caller actually named a traveller; never
            # widen a one-person trip to shared just because shared is default.
            if travellers and visibility != "shared" and existing.get("visibility") != visibility:
                updates["visibility"] = visibility
            if status == "booked" and existing.get("status") != "booked":
                updates["status"] = "booked"
            if updates:
                get_client().table("trips").update(updates).eq("id", existing["id"]).execute()
            row = {**existing, **updates, "_merged": True}
            if warning:
                row["_date_warning"] = warning
            return row

    row: dict = {"destination": destination, "status": status, "visibility": visibility}
    if country:
        row["country"] = country
    if start_date:
        row["start_date"] = start_date
    if end_date:
        row["end_date"] = end_date
    if notes:
        row["notes"] = notes
    created = get_client().table("trips").insert(row).execute().data[0]
    if warning:
        created["_date_warning"] = warning
    return created


def merge_trips(keep_id: str, drop_id: str) -> bool:
    """Fold one row into another and delete the leftover. Used by the one-time
    consolidation and by anything that spots a duplicate later."""
    keep, drop = get_trip_by_id(keep_id), get_trip_by_id(drop_id)
    if not keep or not drop:
        return False
    updates = {"notes": _merge_notes(keep.get("notes"), drop.get("notes"))}
    for field in ("country", "start_date", "end_date", "visa_ansen", "visa_jess"):
        if not keep.get(field) and drop.get(field):
            updates[field] = drop[field]
    # Prefer the SPECIFIC visibility, not the wider one. "shared" is the default
    # value a trip gets when nobody said who is going, so it carries less
    # information than "ansen" or "jess" — widening on merge is how the December
    # Phuket trip became a couple's trip in the view while its own notes said
    # "Ansen only, Jess staying in SG (week 32+, no-fly)".
    specific = [v for v in (keep.get("visibility"), drop.get("visibility"))
                if v and v != "shared"]
    if specific:
        updates["visibility"] = specific[0]
    if "booked" in (keep.get("status"), drop.get("status")):
        updates["status"] = "booked"
    get_client().table("trips").update(updates).eq("id", keep_id).execute()
    # Marked, not deleted. Its details are now in the surviving row, but a merge
    # is a judgement and judgements need a reverse gear — the row stays, filtered
    # out of every view, and can be revived by clearing the status.
    get_client().table("trips").update(
        {"status": "merged", "notes": (drop.get("notes") or "")
         + f"\n[merged into {keep_id} — its details now live there]"}
    ).eq("id", drop_id).execute()
    return True


def find_duplicate_trips() -> list[tuple[dict, dict]]:
    """Pairs of rows that describe the same journey."""
    rows = [t for t in (get_client().table("trips").select("*").execute().data or [])
            if t.get("status") not in ("cancelled", "merged")]
    pairs, used = [], set()
    for i, a in enumerate(rows):
        if a["id"] in used:
            continue
        for b in rows[i + 1:]:
            if b["id"] in used:
                continue
            same_place = _dest_key(a.get("destination") or "") == _dest_key(b.get("destination") or "")
            shared_flight = bool(_flights(a.get("notes") or "") & _flights(b.get("notes") or ""))
            if (same_place or shared_flight) and _overlaps(
                    a.get("start_date"), a.get("end_date"),
                    b.get("start_date"), b.get("end_date")):
                pairs.append((a, b))
                used.add(b["id"])
    return pairs


def get_upcoming_trips() -> list[dict]:
    today = local_today().isoformat()
    return (
        get_client().table("trips").select("*")
        .gte("end_date", today)
        .neq("status", "cancelled")
        .neq("status", "merged")
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
