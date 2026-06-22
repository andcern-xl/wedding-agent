from datetime import datetime, timezone, timedelta
from dateutil.relativedelta import relativedelta
from tools.db import get_client


def schedule_notification(user_id: int, message: str, scheduled_at: datetime, recurrence: str = "none") -> dict:
    row = {
        "user_id": user_id,
        "message": message,
        "scheduled_at": scheduled_at.isoformat(),
        "sent": False,
        "recurrence": recurrence,
    }
    return get_client().table("scheduled_notifications").insert(row).execute().data[0]


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
        old_dt = datetime.fromisoformat(row["scheduled_at"])
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


def list_notifications(user_id: int) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    return (
        get_client()
        .table("scheduled_notifications")
        .select("*")
        .eq("user_id", user_id)
        .eq("sent", False)
        .gte("scheduled_at", now)
        .order("scheduled_at")
        .execute()
        .data or []
    )


def cancel_notification(notification_id: str, user_id: int) -> bool:
    result = (
        get_client()
        .table("scheduled_notifications")
        .delete()
        .eq("id", notification_id)
        .eq("user_id", user_id)
        .execute()
    )
    return bool(result.data)
