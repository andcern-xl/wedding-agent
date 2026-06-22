"""Calendar reconciliation — detect when Google Calendar events move and sync task due_dates."""
from __future__ import annotations
from datetime import date

_STOP = {
    "with", "the", "and", "for", "our", "from", "this", "that",
    "have", "will", "dinner", "lunch", "brunch", "meet", "catch",
    "call", "book", "time", "date", "plan", "check",
}

# These categories manage their own dates — don't touch them
_SKIP_CATEGORIES = {"baby", "baby_questions", "wedding"}


def _words(text: str) -> set[str]:
    return {w.lower().strip(",.!?()") for w in text.split() if len(w) >= 4 and w.lower().strip(",.!?()") not in _STOP}


def find_event_for_task(task: dict, events: list[dict]) -> dict | None:
    """
    Find the best-matching calendar event for a task by title word overlap.
    Requires ≥2 matching words. Only considers events within 45 days of task due_date.
    """
    task_words = _words(task.get("task") or "")
    if not task_words:
        return None

    task_due = task.get("due_date")
    if not task_due:
        return None

    try:
        task_date = date.fromisoformat(task_due)
    except ValueError:
        return None

    best, best_score = None, 1  # threshold: strictly > 1 means ≥2
    for e in events:
        event_words = _words(e.get("title") or "")
        score = len(task_words & event_words)
        if score <= best_score:
            continue
        event_date_str = (e.get("start") or "")[:10]
        try:
            event_date = date.fromisoformat(event_date_str)
        except ValueError:
            continue
        if abs((event_date - task_date).days) > 45:
            continue
        best_score = score
        best = e
    return best


def reconcile_task_dates(tasks: list[dict], events: list[dict]) -> list[dict]:
    """
    For each open task with a due_date, find a matching calendar event.
    Returns list of {task, old_date, new_date, event} where the date actually differs.
    Each calendar event is matched to at most one task.
    """
    changes = []
    matched_event_ids: set = set()

    for task in tasks:
        if task.get("category") in _SKIP_CATEGORIES:
            continue
        if task.get("done"):
            continue

        event = find_event_for_task(task, events)
        if not event:
            continue

        event_id = event.get("id")
        if event_id in matched_event_ids:
            continue

        old_date = task.get("due_date")
        new_date = (event.get("start") or "")[:10]
        if not new_date or new_date == old_date:
            continue

        matched_event_ids.add(event_id)
        changes.append({
            "task": task,
            "old_date": old_date,
            "new_date": new_date,
            "event": event,
        })

    return changes
