from datetime import date, datetime, timezone
from tools.db import get_client

# Canonical task domains. baby_questions is kept distinct because the
# appointment prebrief and /baby → Questions views filter on it.
TASK_DOMAINS = ("baby", "baby_questions", "wedding", "life")

_LEGACY_CATEGORY_MAP = {
    "finance": "life", "health": "life", "home": "life", "work": "life",
    "social": "life", "travel": "life", "personal": "life", "misc": "life",
    "pregnancy": "baby", "medical": "baby",
}


def normalize_category(category: str | None) -> str | None:
    """Collapse legacy/free-form slugs into the canonical domain set.
    Returns None for 'unsure' so the task stays uncategorised until the user taps a bucket."""
    if not category:
        return None
    cat = category.strip().lower()
    if cat in ("unsure", "unknown", ""):
        return None
    if cat in TASK_DOMAINS:
        return cat
    return _LEGACY_CATEGORY_MAP.get(cat, "life")


def task_domain(task: dict) -> str | None:
    """Domain of an existing task row, tolerating legacy slugs. None = uncategorised."""
    return normalize_category(task.get("category"))


def set_task_category(task_id: str, category: str) -> bool:
    cat = normalize_category(category)
    if not cat:
        return False
    try:
        result = get_client().table("daily_tasks").update({"category": cat}).eq("id", task_id).execute()
        return bool(result.data)
    except Exception:
        return False


def get_task_by_id(task_id: str) -> dict | None:
    rows = get_client().table("daily_tasks").select("*").eq("id", task_id).execute().data
    return rows[0] if rows else None


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
    category = normalize_category(category)
    if category:
        row["category"] = category
    if assigned_to:
        row["assigned_to"] = assigned_to
    return get_client().table("daily_tasks").insert(row).execute().data[0]


def is_iceboxed(task: dict) -> bool:
    """Parked in the backlog: hidden from briefs, reminders, and nagging until
    iceboxed_until passes, then it resurfaces automatically."""
    until = task.get("iceboxed_until")
    return bool(until) and until > date.today().isoformat()


def get_tasks(user_id: int, include_done: bool = False,
              include_iceboxed: bool = False) -> list[dict]:
    """Return tasks visible to this user: their own, assigned to them, or shared."""
    q = get_client().table("daily_tasks").select("*")
    if not include_done:
        q = q.eq("done", False)
    rows = q.order("due_date", desc=False, nullsfirst=False).execute().data or []
    return [
        r for r in rows
        if (r["visibility"] == "shared"
            or r["user_id"] == user_id
            or r.get("assigned_to") == user_id)
        and (include_iceboxed or not is_iceboxed(r))
    ]


_CLOSE_STOP = {"the", "and", "for", "with", "into", "your", "you", "send", "log",
               "confirm", "confirmation", "number", "book", "follow", "details",
               "task", "get", "sort", "check", "this", "that", "have", "need"}


def close_tasks_matching(subject: str, min_overlap: int = 2) -> list[str]:
    """Mark open tasks done when their text overlaps the subject strongly — used
    when a decision resolves an open loop (e.g. 'Hyatt confirmed' closes the
    'Book Amsterdam' and 'Log Hyatt conf#' tasks). Returns closed task labels."""
    subj_words = {w for w in _kw(subject) if w not in _CLOSE_STOP}
    if not subj_words:
        return []
    closed = []
    rows = get_client().table("daily_tasks").select("*").eq("done", False).execute().data or []
    for t in rows:
        t_words = {w for w in _kw(t.get("task") or "") if w not in _CLOSE_STOP}
        if len(subj_words & t_words) >= min_overlap:
            try:
                get_client().table("daily_tasks").update({
                    "done": True,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", t["id"]).execute()
                closed.append((t.get("task") or "")[:60])
            except Exception:
                pass
    return closed


def _kw(text: str) -> set:
    import re
    return {w for w in re.findall(r"[a-z0-9#]{3,}", (text or "").lower())}


def find_duplicate_open_task(task_text: str, min_overlap: int = 3) -> dict | None:
    """An open task that's substantially the same as task_text, so we don't
    stack near-identical to-dos ('Book Amsterdam' x2)."""
    words = _kw(task_text)
    if len(words) < min_overlap:
        return None
    rows = get_client().table("daily_tasks").select("*").eq("done", False).execute().data or []
    for t in rows:
        if len(words & _kw(t.get("task") or "")) >= min_overlap:
            return t
    return None


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
    return [r for r in rows
            if (r["visibility"] == "shared" or r["user_id"] == user_id)
            and not is_iceboxed(r)]


def icebox_task(task_id: str, days: int) -> bool:
    """Park a task in the backlog for N days."""
    from datetime import timedelta
    try:
        result = get_client().table("daily_tasks").update({
            "iceboxed_until": (date.today() + timedelta(days=days)).isoformat(),
        }).eq("id", task_id).execute()
        return bool(result.data)
    except Exception:
        return False


def bump_task(task_id: str, days: int = 7) -> bool:
    """Re-commit to a stale task: due in N days, out of the icebox."""
    from datetime import timedelta
    try:
        result = get_client().table("daily_tasks").update({
            "due_date": (date.today() + timedelta(days=days)).isoformat(),
            "iceboxed_until": None,
        }).eq("id", task_id).execute()
        return bool(result.data)
    except Exception:
        return False


def get_stale_tasks(user_id: int, overdue_days: int = 7, undated_days: int = 14,
                    reoffer_days: int = 5) -> list[dict]:
    """Tasks ripe for an icebox decision: overdue 7+ days, or undated and
    untouched for 14+. Skips tasks offered in the last reoffer_days."""
    from datetime import timedelta
    today = date.today()
    overdue_cutoff = (today - timedelta(days=overdue_days)).isoformat()
    created_cutoff = (today - timedelta(days=undated_days)).isoformat()
    reoffer_cutoff = (today - timedelta(days=reoffer_days)).isoformat()
    stale = []
    for t in get_tasks(user_id):
        offered = t.get("icebox_offered_at")
        if offered and offered > reoffer_cutoff:
            continue
        due = t.get("due_date")
        created = (t.get("created_at") or "")[:10]
        if (due and due <= overdue_cutoff) or (not due and created and created <= created_cutoff):
            stale.append(t)
    stale.sort(key=lambda t: t.get("due_date") or (t.get("created_at") or "")[:10])
    return stale


def mark_icebox_offered(task_id: str) -> None:
    try:
        get_client().table("daily_tasks").update({
            "icebox_offered_at": date.today().isoformat(),
        }).eq("id", task_id).execute()
    except Exception:
        pass


def get_resurfaced_today(user_id: int) -> list[dict]:
    """Tasks whose icebox period just ended (came back within the last day)."""
    from datetime import timedelta
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    return [
        t for t in get_tasks(user_id)
        if t.get("iceboxed_until") and yesterday <= t["iceboxed_until"] <= today
    ]


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
    if r.get("assigned_to") and r["assigned_to"] != user_id and r["visibility"] == "shared":
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


def update_task_date(task_id: str, new_date: str) -> bool:
    result = get_client().table("daily_tasks").update({"due_date": new_date}).eq("id", task_id).execute()
    return bool(result.data)


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
