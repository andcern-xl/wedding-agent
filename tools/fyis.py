from datetime import date, datetime, timezone, timedelta
from tools.db import get_client


def log_fyi(user_id: int, content: str, category: str | None = None) -> dict:
    row = {"user_id": user_id, "content": content}
    if category:
        row["category"] = category
    return get_client().table("fyis").insert(row).execute().data[0]


def get_fyis(limit: int = 30) -> list[dict]:
    """Active FYIs saved in the last 30 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    try:
        return (
            get_client().table("fyis")
            .select("*")
            .eq("status", "active")
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data or []
        )
    except Exception:
        # Fallback if status column doesn't exist yet
        return (
            get_client().table("fyis")
            .select("*")
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data or []
        )


def get_fyis_for_context(limit: int = 15) -> list[dict]:
    """Recent FYIs for injection into agent system prompt — newest first."""
    return get_fyis(limit=limit)


def get_fyis_expiring(days_threshold: int = 21, limit: int = 15) -> list[dict]:
    """Active FYIs between days_threshold and 30 days old — nearing expiry."""
    now = datetime.now(timezone.utc)
    older_than = (now - timedelta(days=days_threshold)).isoformat()
    within_30 = (now - timedelta(days=30)).isoformat()
    try:
        return (
            get_client().table("fyis")
            .select("*")
            .eq("status", "active")
            .lte("created_at", older_than)
            .gte("created_at", within_30)
            .order("created_at")
            .limit(limit)
            .execute()
            .data or []
        )
    except Exception:
        return []


def get_fyis_today() -> list[dict]:
    today_start = f"{date.today().isoformat()}T00:00:00+00:00"
    try:
        return (
            get_client().table("fyis")
            .select("*")
            .eq("status", "active")
            .gte("created_at", today_start)
            .order("created_at")
            .execute()
            .data or []
        )
    except Exception:
        return (
            get_client().table("fyis")
            .select("*")
            .gte("created_at", today_start)
            .order("created_at")
            .execute()
            .data or []
        )


def archive_fyi(fyi_id: str) -> bool:
    try:
        result = get_client().table("fyis").update({"status": "archived"}).eq("id", fyi_id).execute()
        return bool(result.data)
    except Exception:
        return False


def promote_fyi(fyi_id: str) -> dict | None:
    """Mark as promoted and return {content, category} for saving to shared brain."""
    try:
        rows = get_client().table("fyis").select("content,category").eq("id", fyi_id).execute().data
        if not rows:
            return None
        get_client().table("fyis").update({"status": "promoted"}).eq("id", fyi_id).execute()
        return rows[0]
    except Exception:
        return None


ANSEN_ID = 63756531
JESS_ID = 6927468999

def _ack_field(user_id: int) -> str:
    return "acked_ansen" if user_id == ANSEN_ID else "acked_jess"


def ack_fyi(fyi_id: str, user_id: int) -> bool:
    """Mark this FYI as seen by user_id. Archives when both have acked."""
    try:
        field = _ack_field(user_id)
        get_client().table("fyis").update({field: True}).eq("id", fyi_id).execute()
        row = get_client().table("fyis").select("acked_ansen,acked_jess").eq("id", fyi_id).execute().data
        if row and row[0].get("acked_ansen") and row[0].get("acked_jess"):
            archive_fyi(fyi_id)
        return True
    except Exception:
        return False


def get_fyis_unacked(user_id: int, limit: int = 30) -> list[dict]:
    """FYIs this specific user hasn't yet acked."""
    field = _ack_field(user_id)
    try:
        fyis = get_fyis(limit=limit)
        return [f for f in fyis if not f.get(field)]
    except Exception:
        return []


def get_fyi_by_id(fyi_id: str) -> dict | None:
    try:
        rows = get_client().table("fyis").select("*").eq("id", fyi_id).execute().data
        return rows[0] if rows else None
    except Exception:
        return None


def keep_fyi(fyi_id: str) -> bool:
    """Reset created_at to now, extending the 30-day TTL by another 30 days."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = get_client().table("fyis").update({"created_at": now}).eq("id", fyi_id).execute()
        return bool(result.data)
    except Exception:
        return False
