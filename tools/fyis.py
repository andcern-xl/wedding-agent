from datetime import date
from tools.db import get_client


def log_fyi(user_id: int, content: str, category: str | None = None) -> dict:
    row = {"user_id": user_id, "content": content}
    if category:
        row["category"] = category
    return get_client().table("fyis").insert(row).execute().data[0]


def get_fyis(limit: int = 20) -> list[dict]:
    return (
        get_client().table("fyis")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data or []
    )


def get_fyis_today() -> list[dict]:
    today_start = f"{date.today().isoformat()}T00:00:00+00:00"
    return (
        get_client().table("fyis")
        .select("*")
        .gte("created_at", today_start)
        .order("created_at")
        .execute()
        .data or []
    )
