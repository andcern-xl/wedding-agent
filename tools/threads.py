"""Thread ledger — dated contact tracking per person/topic.

The source of truth for "when did we last hear from X". Day-counts in briefs
and chat must come from here; anything else is fabrication.
"""
from datetime import date, datetime, timezone

from tools.db import get_client

_OPEN_STATUSES = ("open", "waiting_them", "waiting_us")


def _days_since(d: str | None) -> int | None:
    if not d:
        return None
    try:
        return (date.today() - date.fromisoformat(d[:10])).days
    except ValueError:
        return None


def read_threads(status: str | None = None, person: str | None = None) -> list[dict]:
    """Threads with computed days_since_contact. Default: all unresolved."""
    q = get_client().table("threads").select("*")
    if person:
        q = q.ilike("person", f"%{person}%")
    if status:
        q = q.eq("status", status)
    else:
        q = q.in_("status", list(_OPEN_STATUSES))
    rows = q.order("last_contact", desc=False).execute().data or []
    for r in rows:
        r["days_since_contact"] = _days_since(r.get("last_contact"))
    return rows


def log_contact(person: str, topic: str, direction: str, note: str = "",
                contact_date: str | None = None, domain: str = "life",
                status: str | None = None) -> dict:
    """Record a dated interaction. Reuses the person's open thread on the same
    topic when one exists (keyword overlap), otherwise starts a new thread."""
    when = (contact_date or date.today().isoformat())[:10]
    if not status:
        status = "waiting_them" if direction == "outbound" else "waiting_us"

    existing = read_threads(person=person)
    topic_words = {w for w in topic.lower().split() if len(w) > 3}
    match = None
    for t in existing:
        t_words = {w for w in (t.get("topic") or "").lower().split() if len(w) > 3}
        if topic_words & t_words:
            match = t
            break

    payload = {
        "last_contact": when,
        "last_direction": direction,
        "last_note": note[:300],
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if match:
        row = (
            get_client().table("threads").update(payload)
            .eq("id", match["id"]).execute().data
        )
        result = row[0] if row else {**match, **payload}
    else:
        result = (
            get_client().table("threads")
            .insert({"person": person, "topic": topic, "domain": domain, **payload})
            .execute().data[0]
        )
    result["days_since_contact"] = _days_since(result.get("last_contact"))
    return result


def resolve_thread(person: str, topic: str = "") -> dict | None:
    """Close out a thread — the reply came, the booking landed, it's done."""
    candidates = read_threads(person=person)
    if topic:
        topic_words = {w for w in topic.lower().split() if len(w) > 3}
        candidates = [
            t for t in candidates
            if topic_words & {w for w in (t.get("topic") or "").lower().split() if len(w) > 3}
        ] or candidates
    if not candidates:
        return None
    t = candidates[0]
    row = (
        get_client().table("threads")
        .update({"status": "resolved", "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", t["id"]).execute().data
    )
    return row[0] if row else t
