from datetime import datetime, timezone
from tools.db import get_client


def drop(category: str | None, kind: str, content: str, user_id: int):
    get_client().table("wedding_drops").insert({
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "category": category,
        "kind": kind,
        "content": content,
    }).execute()


def get_drops(category: str | None = None, limit: int = 60) -> list[dict]:
    q = get_client().table("wedding_drops").select("*").order("ts", desc=False)
    if category:
        q = q.eq("category", category)
    q = q.limit(limit)
    return q.execute().data or []


def get_recent_drops(limit: int = 30) -> list[dict]:
    return (
        get_client().table("wedding_drops")
        .select("*")
        .order("ts", desc=True)
        .limit(limit)
        .execute()
        .data or []
    )[::-1]
