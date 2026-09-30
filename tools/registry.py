"""Baby registry events from the baby-registry bot (Supabase `registry_events`).

Same pattern as tools/signals.py: the bot POSTs batches to webhook.py
(/webhooks/registry), validation lives here so the rules are testable without
a server, and a repeated event id is ignored for 24h.
"""
from datetime import datetime, timedelta, timezone
from tools.db import get_client

TYPES = {"research_update", "list_update", "price_update", "timeline_update", "question", "note"}
PRIORITIES = {"P0", "P1", "P2", "info"}
MAX_EVENTS = 200   # per batch
DEDUPE_HOURS = 24


def _str(v, limit: int) -> str:
    return v.strip()[:limit] if isinstance(v, str) else ""


def validate(payload) -> tuple[dict | None, str | None]:
    """Return (clean batch, None) or (None, error message for a 400)."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    source = _str(payload.get("source"), 80)
    if not source:
        return None, "source is required"
    as_of = payload.get("as_of")
    if as_of is not None:
        try:
            datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        except ValueError:
            return None, "as_of must be an ISO 8601 timestamp"
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        return None, "events must be a non-empty array"
    if len(events) > MAX_EVENTS:
        return None, f"at most {MAX_EVENTS} events per batch"

    clean = []
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            return None, f"events[{i}] must be an object"
        eid = _str(e.get("id"), 200)
        if not eid:
            return None, f"events[{i}].id is required"
        if e.get("type") not in TYPES:
            return None, f"events[{i}].type must be one of {sorted(TYPES)}"
        if e.get("priority") not in PRIORITIES:
            return None, f"events[{i}].priority must be one of {sorted(PRIORITIES)}"
        body = e.get("payload") or {}
        if not isinstance(body, dict):
            return None, f"events[{i}].payload must be an object"
        links = e.get("links") or []
        if not isinstance(links, list) or not all(isinstance(u, str) for u in links):
            return None, f"events[{i}].links must be an array of strings"
        clean.append({
            "event_id": eid, "type": e["type"], "priority": e["priority"],
            "summary": _str(e.get("summary"), 500), "payload": body,
            "links": [u[:500] for u in links[:10]],
        })
    return {"source": source, "as_of": as_of, "events": clean}, None


def save_batch(batch: dict) -> list[dict]:
    """Insert the batch, skipping any event_id this source already sent in the
    last 24h (and repeats within the batch). Returns the rows stored."""
    db = get_client()
    since = (datetime.now(timezone.utc) - timedelta(hours=DEDUPE_HOURS)).isoformat()
    ids = list({e["event_id"] for e in batch["events"]})
    seen = {r["event_id"] for r in (
        db.table("registry_events").select("event_id")
        .eq("source", batch["source"]).in_("event_id", ids)
        .gte("received_at", since).execute().data or [])}
    rows = []
    for e in batch["events"]:
        if e["event_id"] in seen:
            continue
        seen.add(e["event_id"])
        rows.append({**e, "source": batch["source"], "as_of": batch["as_of"]})
    if rows:
        db.table("registry_events").insert(rows).execute()
    return rows


def recent_events(hours: int = 72) -> list[dict]:
    """Events received in the last `hours`, newest first."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return (get_client().table("registry_events")
            .select("source,event_id,type,priority,summary,payload,links,as_of,received_at")
            .gte("received_at", since).order("received_at", desc=True)
            .limit(500).execute().data or [])
