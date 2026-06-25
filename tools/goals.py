from datetime import datetime, timezone
from tools.db import get_client


def create_goal(user_id: int, title: str, visibility: str = "shared", category: str | None = None) -> dict:
    row = {"user_id": user_id, "title": title, "visibility": visibility, "status": "active"}
    if category:
        row["category"] = category
    return get_client().table("goals").insert(row).execute().data[0]


def add_step(goal_id: str, title: str, sort_order: int = 0, blocked_by: str | None = None,
             due_date: str | None = None, assigned_to: int | None = None) -> dict:
    row = {"goal_id": goal_id, "title": title, "sort_order": sort_order, "status": "open"}
    if blocked_by:
        row["blocked_by"] = blocked_by
    if due_date:
        row["due_date"] = due_date
    if assigned_to:
        row["assigned_to"] = assigned_to
    return get_client().table("goal_steps").insert(row).execute().data[0]


def get_goals(status: str = "active") -> list[dict]:
    goals = (
        get_client().table("goals").select("*, goal_steps(*)")
        .eq("status", status)
        .order("created_at")
        .execute().data or []
    )
    for g in goals:
        g["goal_steps"] = sorted(g.get("goal_steps") or [], key=lambda s: s.get("sort_order", 0))
    return goals


def get_goal_by_id(goal_id: str) -> dict | None:
    rows = get_client().table("goals").select("*, goal_steps(*)").eq("id", goal_id).execute().data
    if not rows:
        return None
    g = rows[0]
    g["goal_steps"] = sorted(g.get("goal_steps") or [], key=lambda s: s.get("sort_order", 0))
    return g


def get_next_steps(goal_id: str) -> list[dict]:
    """Open steps not blocked by any incomplete step."""
    steps = get_client().table("goal_steps").select("*").eq("goal_id", goal_id).execute().data or []
    done_ids = {s["id"] for s in steps if s["status"] == "done"}
    result = []
    for s in steps:
        if s["status"] != "open":
            continue
        blocker = s.get("blocked_by")
        if blocker is None or blocker in done_ids:
            result.append(s)
    return sorted(result, key=lambda s: s.get("sort_order", 0))


def complete_step(step_id: str) -> dict:
    """Mark step done. Returns newly unblocked steps and whether the whole goal is done."""
    get_client().table("goal_steps").update({
        "status": "done",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", step_id).execute()

    step_rows = get_client().table("goal_steps").select("*").eq("id", step_id).execute().data
    if not step_rows:
        return {"error": "step not found"}
    step = step_rows[0]
    goal_id = step["goal_id"]

    all_steps = get_client().table("goal_steps").select("*").eq("goal_id", goal_id).execute().data or []

    # Steps that were directly waiting on this one
    newly_unblocked = [s for s in all_steps if s["status"] == "open" and s.get("blocked_by") == step_id]

    remaining_open = [s for s in all_steps if s["status"] == "open"]
    goal_complete = len(remaining_open) == 0

    if goal_complete:
        get_client().table("goals").update({"status": "done"}).eq("id", goal_id).execute()

    return {
        "step_completed": step["title"],
        "goal_id": str(goal_id),
        "newly_unblocked": [{"id": str(s["id"]), "title": s["title"]} for s in newly_unblocked],
        "goal_complete": goal_complete,
        "remaining_steps": len(remaining_open),
    }


def update_goal_status(goal_id: str, status: str) -> bool:
    result = get_client().table("goals").update({"status": status}).eq("id", goal_id).execute()
    return bool(result.data)


def find_step_by_title(goal_id: str, title_fragment: str) -> dict | None:
    steps = get_client().table("goal_steps").select("*").eq("goal_id", goal_id).execute().data or []
    frag = title_fragment.lower()
    # Exact substring match first, then first token match
    for s in steps:
        if frag in (s.get("title") or "").lower():
            return s
    for s in steps:
        first_word = frag.split()[0] if frag.split() else frag
        if first_word in (s.get("title") or "").lower():
            return s
    return None
