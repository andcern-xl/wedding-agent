"""Check-ins: structured questions the agent asks via Telegram buttons.

Lifecycle mirrors FYIs: open → answered / dismissed / snoozed / expired.
Open check-ins are injected into the agent's system prompt so it never
re-asks a question it already asked, and can chase stale ones.
"""
from datetime import date, datetime, timezone, timedelta
from tools.db import get_client

VALID_ACTIONS = ("save_decision", "create_task", "remind", "dismiss")
MAX_OPEN = 10  # hard cap so unanswered questions can't pile up


def create_check_in(
    created_by: int,
    question: str,
    options: list[dict],
    category: str = "life",
    audience: str = "me",
    context: str = "",
) -> dict | None:
    if category not in ("baby", "wedding", "life"):
        category = "life"
    if audience not in ("me", "both"):
        audience = "me"
    clean_options = []
    for opt in options[:4]:
        label = (opt.get("label") or "").strip()[:40]
        action = opt.get("action") if opt.get("action") in VALID_ACTIONS else "save_decision"
        if label:
            clean_options.append({"label": label, "action": action, "payload": opt.get("payload") or {}})
    if not clean_options:
        return None
    try:
        if len(get_open_check_ins()) >= MAX_OPEN:
            return None
        return (
            get_client().table("check_ins")
            .insert({
                "created_by": created_by,
                "audience": audience,
                "question": question.strip()[:300],
                "context": (context or "").strip()[:300],
                "category": category,
                "options": clean_options,
            })
            .execute()
            .data[0]
        )
    except Exception:
        return None


def get_check_in(check_in_id: str) -> dict | None:
    try:
        rows = get_client().table("check_ins").select("*").eq("id", check_in_id).execute().data
        return rows[0] if rows else None
    except Exception:
        return None


def get_open_check_ins(limit: int = 20) -> list[dict]:
    try:
        return (
            get_client().table("check_ins")
            .select("*")
            .eq("status", "open")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data or []
        )
    except Exception:
        return []


def answer_check_in(check_in_id: str, answer: str, answered_by: int) -> bool:
    try:
        result = (
            get_client().table("check_ins")
            .update({
                "status": "answered",
                "answer": answer[:300],
                "answered_by": answered_by,
                "answered_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", check_in_id)
            .eq("status", "open")
            .execute()
        )
        return bool(result.data)
    except Exception:
        return False


def dismiss_check_in(check_in_id: str, user_id: int) -> bool:
    try:
        result = (
            get_client().table("check_ins")
            .update({
                "status": "dismissed",
                "answered_by": user_id,
                "answered_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", check_in_id)
            .eq("status", "open")
            .execute()
        )
        return bool(result.data)
    except Exception:
        return False


def snooze_check_in(check_in_id: str, days: int = 3) -> bool:
    try:
        until = (date.today() + timedelta(days=days)).isoformat()
        result = (
            get_client().table("check_ins")
            .update({"status": "snoozed", "snooze_until": until})
            .eq("id", check_in_id)
            .eq("status", "open")
            .execute()
        )
        return bool(result.data)
    except Exception:
        return False


def reopen_due_snoozed() -> list[dict]:
    """Snoozed check-ins whose snooze has lapsed go back to open. Returns reopened rows."""
    try:
        today = date.today().isoformat()
        return (
            get_client().table("check_ins")
            .update({"status": "open", "snooze_until": None})
            .eq("status", "snoozed")
            .lte("snooze_until", today)
            .execute()
            .data or []
        )
    except Exception:
        return []


def expire_stale(days: int = 7) -> list[dict]:
    """Open check-ins older than `days` expire. Returns expired rows so the caller can downgrade them to FYIs."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return (
            get_client().table("check_ins")
            .update({"status": "expired"})
            .eq("status", "open")
            .lte("created_at", cutoff)
            .execute()
            .data or []
        )
    except Exception:
        return []
