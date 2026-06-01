from datetime import datetime, timezone
from tools.db import get_client


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


def get_message_count(user_id: int) -> int:
    rows = (
        get_client().table("user_summaries")
        .select("message_count")
        .eq("user_id", user_id)
        .execute()
        .data or []
    )
    return rows[0]["message_count"] if rows else 0
