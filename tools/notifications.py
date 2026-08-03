import os
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta
from tools.db import get_client

try:
    LOCAL_TZ = ZoneInfo(os.getenv("REMINDER_TZ", "Asia/Singapore"))
except Exception:
    LOCAL_TZ = timezone.utc


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def local_time_label(ts: str) -> str:
    """'Tue 4 Aug, 8:00 am' in the household timezone."""
    try:
        return _parse(ts).astimezone(LOCAL_TZ).strftime("%a %-d %b, %-I:%M %p").replace("AM", "am").replace("PM", "pm")
    except Exception:
        return ts


def _local_hhmm(ts) -> str:
    dt = _parse(ts) if isinstance(ts, str) else ts
    return dt.astimezone(LOCAL_TZ).strftime("%H:%M")


# Words that carry no signal when someone says "turn off the X reminder" — a
# query made only of these is too vague to safely match anything.
_MATCH_STOPWORDS = {
    "the", "and", "for", "all", "any", "our", "her", "his", "its", "this",
    "that", "these", "those", "off", "out", "turn", "stop", "cancel", "kill",
    "delete", "remove", "mute", "silence", "disable", "please", "can", "you",
    "notification", "notifications", "notify", "reminder", "reminders",
    "remind", "alert", "alerts", "message", "messages", "ping", "pings",
    "every", "each", "daily", "weekly", "monthly", "scheduled", "schedule",
    "about", "from", "with", "have", "get", "got", "want", "need",
}


def _tokens(text: str) -> list[str]:
    text = re.sub(r"['’]s\b", "", (text or "").lower())
    return [w for w in re.findall(r"[a-z0-9]{3,}", text) if w not in _MATCH_STOPWORDS]


_PREFIX_WEIGHT = 0.6  # a stem match counts, but never as much as the real word


def _match_score(query: str, message: str) -> float:
    """How much of the query the message actually covers. Exact word hits count
    full; stem hits ('vaccine' → 'vaccination') count partial, so a reminder
    that only glances off the query ranks below one that names the subject."""
    q = _tokens(query)
    if not q:
        return 0.0
    m = set(_tokens(message))
    score = 0.0
    for t in q:
        if t in m:
            score += 1.0
        elif len(t) >= 4 and any(w.startswith(t[:4]) for w in m):
            score += _PREFIX_WEIGHT
    return score / len(q)


def schedule_notification(user_id: int, message: str, scheduled_at: datetime,
                          recurrence: str = "none", dedupe: bool = True) -> dict:
    """Insert a scheduled notification. With dedupe on (default) an identical
    reminder already pending at the same slot is returned instead of stacking a
    second copy — this is what stops 'give Lucille her meds' becoming six rows.
    The returned row carries two in-memory hints for callers:
      _duplicate — True when an existing row was reused, nothing was inserted
      _similar   — other pending rows with the same text at a different time"""
    if dedupe:
        existing = _pending_with_message(user_id, message)
        for r in existing:
            if recurrence == "none":
                if r.get("recurrence", "none") == "none" and _parse(r["scheduled_at"]) == scheduled_at:
                    return {**r, "_duplicate": True}
            elif r.get("recurrence") == recurrence and _local_hhmm(r["scheduled_at"]) == _local_hhmm(scheduled_at):
                return {**r, "_duplicate": True}

    row = {
        "user_id": user_id,
        "message": message,
        "scheduled_at": scheduled_at.isoformat(),
        "sent": False,
        "recurrence": recurrence,
    }
    created = get_client().table("scheduled_notifications").insert(row).execute().data[0]
    similar = [r for r in _pending_with_message(user_id, message) if r["id"] != created["id"]]
    return {**created, "_similar": similar}


def _pending_with_message(user_id: int, message: str) -> list[dict]:
    return (
        get_client()
        .table("scheduled_notifications")
        .select("*")
        .eq("user_id", user_id)
        .eq("message", message)
        .eq("sent", False)
        .execute()
        .data or []
    )


def get_pending_notifications() -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    return (
        get_client()
        .table("scheduled_notifications")
        .select("*")
        .eq("sent", False)
        .lte("scheduled_at", now)
        .order("scheduled_at")
        .execute()
        .data or []
    )


def mark_notification_sent(notification_id: str) -> None:
    rows = (
        get_client()
        .table("scheduled_notifications")
        .select("*")
        .eq("id", notification_id)
        .execute()
        .data or []
    )
    if not rows:
        return
    row = rows[0]
    get_client().table("scheduled_notifications").update({"sent": True}).eq("id", notification_id).execute()
    recurrence = row.get("recurrence", "none")
    if recurrence and recurrence != "none":
        old_dt = _parse(row["scheduled_at"])
        if recurrence == "daily":
            next_dt = old_dt + timedelta(days=1)
        elif recurrence == "weekly":
            next_dt = old_dt + timedelta(weeks=1)
        elif recurrence == "monthly":
            next_dt = old_dt + relativedelta(months=1)
        else:
            next_dt = None
        if next_dt:
            schedule_notification(row["user_id"], row["message"], next_dt, recurrence)


def list_notifications(user_id: int | None = None, user_ids: list[int] | None = None) -> list[dict]:
    """Upcoming (unsent, future) notifications. Pass user_ids to see the whole
    household — a reminder either partner set is a reminder either partner
    should be able to find and switch off."""
    now = datetime.now(timezone.utc).isoformat()
    q = (
        get_client()
        .table("scheduled_notifications")
        .select("*")
        .eq("sent", False)
        .gte("scheduled_at", now)
    )
    if user_ids:
        q = q.in_("user_id", [int(u) for u in user_ids])
    elif user_id is not None:
        q = q.eq("user_id", int(user_id))
    return q.order("scheduled_at").execute().data or []


def find_notifications(query: str, user_ids: list[int] | None = None,
                       min_score: float = 0.34) -> list[dict]:
    """Search upcoming notifications by what they're about ('lucille meds',
    'condo fee'). Each row gets a `score`; results are strongest-first.
    Returns [] for a query with no meaningful words, so a bare 'cancel the
    reminder' can never sweep the whole schedule."""
    if not _tokens(query):
        return []
    scored = []
    for r in list_notifications(user_ids=user_ids):
        score = _match_score(query, r.get("message", ""))
        if score >= min_score:
            scored.append({**r, "score": round(score, 2)})
    scored.sort(key=lambda r: (-r["score"], r["scheduled_at"]))
    return scored


def cancel_notification(notification_id: str, user_id: int | None = None) -> bool:
    q = get_client().table("scheduled_notifications").delete().eq("id", notification_id)
    if user_id is not None:
        q = q.eq("user_id", int(user_id))
    return bool(q.execute().data)


def cancel_notifications(notification_ids: list[str]) -> list[dict]:
    """Cancel several at once and return the rows that were actually removed,
    so the caller can tell the user exactly what got switched off."""
    ids = [str(i) for i in notification_ids if i]
    if not ids:
        return []
    rows = (
        get_client().table("scheduled_notifications")
        .select("*").in_("id", ids).execute().data or []
    )
    if not rows:
        return []
    get_client().table("scheduled_notifications").delete().in_("id", ids).execute()
    return rows


def get_notification(notification_id: str) -> dict | None:
    rows = (
        get_client().table("scheduled_notifications")
        .select("*").eq("id", notification_id).execute().data or []
    )
    return rows[0] if rows else None


def stop_series(notification_id: str) -> dict:
    """Kill a recurring reminder for good: given any one occurrence (including
    one that already fired), delete every pending copy with the same text for
    that person. This is what the 🔕 button on a fired reminder calls."""
    row = get_notification(notification_id)
    if not row:
        return {"status": "not_found", "cancelled": 0}
    pending = _pending_with_message(row["user_id"], row["message"])
    ids = [r["id"] for r in pending]
    if ids:
        get_client().table("scheduled_notifications").delete().in_("id", ids).execute()
    return {
        "status": "stopped",
        "cancelled": len(ids),
        "message": row["message"],
        "user_id": row["user_id"],
        "recurrence": row.get("recurrence", "none"),
    }


def group_duplicates(rows: list[dict]) -> list[dict]:
    """Collapse rows into duplicate groups, flagging any group with more than
    one copy. A recurring reminder is a duplicate when the same text fires at
    the same time of day; a one-off only when the same text fires at the same
    moment — two SGAC alerts for two different trips are not duplicates."""
    groups: dict[tuple, dict] = {}
    for r in rows:
        rec = r.get("recurrence", "none") or "none"
        slot = _local_hhmm(r["scheduled_at"]) if rec != "none" else r["scheduled_at"]
        key = (r["user_id"], r.get("message", ""), rec, slot)
        g = groups.setdefault(key, {
            "user_id": r["user_id"],
            "message": r.get("message", ""),
            "time": _local_hhmm(r["scheduled_at"]),
            "recurrence": r.get("recurrence", "none"),
            "rows": [],
        })
        g["rows"].append(r)
    out = list(groups.values())
    for g in out:
        g["count"] = len(g["rows"])
        g["duplicate"] = g["count"] > 1
    out.sort(key=lambda g: (g["time"], g["message"]))
    return out
