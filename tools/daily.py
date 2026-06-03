from datetime import date, datetime, timezone
from tools.db import get_client


def add_task(
    user_id: int,
    task: str,
    due_date: date | None = None,
    repeat: str = "none",
    visibility: str = "private",
    category: str | None = None,
    assigned_to: int | None = None,
) -> dict:
    row = {
        "user_id": user_id,
        "task": task,
        "repeat": repeat,
        "visibility": visibility,
        "done": False,
    }
    if due_date:
        row["due_date"] = due_date.isoformat()
    if category:
        row["category"] = category
    if assigned_to:
        row["assigned_to"] = assigned_to
    try:
        return get_client().table("daily_tasks").insert(row).execute().data[0]
    except Exception as e:
        if "category" in str(e) and category:
            row.pop("category")
            return get_client().table("daily_tasks").insert(row).execute().data[0]
        raise


def get_tasks(user_id: int, include_done: bool = False) -> list[dict]:
    """Return tasks visible to this user: their own, assigned to them, or shared."""
    q = get_client().table("daily_tasks").select("*")
    if not include_done:
        q = q.eq("done", False)
    rows = q.order("due_date", desc=False, nullsfirst=False).execute().data or []
    return [
        r for r in rows
        if r["visibility"] == "shared"
        or r["user_id"] == user_id
        or r.get("assigned_to") == user_id
    ]


def get_due_today(user_id: int) -> list[dict]:
    today = date.today().isoformat()
    rows = (
        get_client().table("daily_tasks")
        .select("*")
        .eq("done", False)
        .lte("due_date", today)
        .order("due_date")
        .execute()
        .data or []
    )
    return [r for r in rows if r["visibility"] == "shared" or r["user_id"] == user_id]


def complete_task(task_id: str, user_id: int) -> bool:
    """Mark a task done. Only owner can complete a private task; anyone can complete shared."""
    row = (
        get_client().table("daily_tasks")
        .select("*")
        .eq("id", task_id)
        .execute()
        .data or []
    )
    if not row:
        return False
    r = row[0]
    if r["visibility"] == "private" and r["user_id"] != user_id:
        return False
    get_client().table("daily_tasks").update({
        "done": True,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", task_id).execute()
    # Reschedule repeating tasks
    if r["repeat"] != "none" and r.get("due_date"):
        from datetime import timedelta
        old_due = date.fromisoformat(r["due_date"])
        delta = timedelta(days=1 if r["repeat"] == "daily" else 7)
        add_task(r["user_id"], r["task"], old_due + delta, r["repeat"], r["visibility"], r.get("category"))
    return True


def get_completed_today(user_id: int) -> list[dict]:
    """Return tasks completed since UTC midnight today."""
    today_start = f"{date.today().isoformat()}T00:00:00+00:00"
    rows = (
        get_client().table("daily_tasks")
        .select("*")
        .eq("done", True)
        .gte("completed_at", today_start)
        .order("completed_at")
        .execute()
        .data or []
    )
    return [r for r in rows if r["visibility"] == "shared" or r["user_id"] == user_id]


def get_all_tasks_for_brief(user_id: int) -> dict:
    """Return structured task data for brief generation."""
    today = date.today().isoformat()
    all_tasks = get_tasks(user_id, include_done=False)
    overdue = [t for t in all_tasks if t.get("due_date") and t["due_date"] < today]
    due_today = [t for t in all_tasks if t.get("due_date") == today]
    upcoming = [t for t in all_tasks if t.get("due_date") and t["due_date"] > today]
    no_date = [t for t in all_tasks if not t.get("due_date")]
    return {
        "overdue": overdue,
        "due_today": due_today,
        "upcoming": upcoming,
        "no_date": no_date,
    }
