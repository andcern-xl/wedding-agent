from datetime import date, datetime, timezone
from tools.db import get_client

SHARED_BRAIN_ID = 0  # sentinel user_id for the couple's shared context


def get_summary(user_id: int) -> str:
    rows = (
        get_client().table("user_summaries")
        .select("summary")
        .eq("user_id", user_id)
        .execute()
        .data or []
    )
    return rows[0]["summary"] if rows else ""


def save_summary(user_id: int, summary: str, message_count: int):
    get_client().table("user_summaries").upsert({
        "user_id": user_id,
        "summary": summary,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "message_count": message_count,
    }).execute()


def get_shared_summary() -> str:
    rows = (
        get_client().table("user_summaries")
        .select("summary")
        .eq("user_id", SHARED_BRAIN_ID)
        .execute()
        .data or []
    )
    return rows[0]["summary"] if rows else ""


def append_shared_summary(content: str) -> str:
    """Append a dated bullet to the shared brain. Returns the full updated summary."""
    existing = get_shared_summary()
    entry = f"• {date.today().isoformat()}: {content}"
    updated = f"{existing}\n{entry}".strip() if existing else entry
    get_client().table("user_summaries").upsert({
        "user_id": SHARED_BRAIN_ID,
        "summary": updated,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "message_count": 0,
    }).execute()
    return updated


def get_message_count(user_id: int) -> int:
    rows = (
        get_client().table("user_summaries")
        .select("message_count")
        .eq("user_id", user_id)
        .execute()
        .data or []
    )
    return rows[0]["message_count"] if rows else 0
