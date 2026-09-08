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
    """Comparable words from a title.

    Two things this has to get right, both found by a real miss: a possessive
    must collapse ("Lucille's" has to match "Lucille"), and the floor cannot be
    four characters or "vet" — the only distinctive word in "Lucille's vet
    appointment" — is thrown away, leaving nothing to match on."""
    out = set()
    for raw in (text or "").replace("/", " ").split():
        w = raw.lower().strip(",.!?()[]:;\"")
        for suffix in ("'s", "\u2019s"):
            if w.endswith(suffix):
                w = w[:-len(suffix)]
        w = w.strip("'\u2019")
        if len(w) >= 3 and w not in _STOP:
            out.add(w)
    return out


_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_FULL = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]


def date_needles(iso_date: str) -> list[str]:
    """The ways this date might actually be written in stored text.

    Episodes are written in prose — "8 Sep 2026", not "2026-09-08" — so looking
    only for the ISO form finds nothing. This is why the first version of the
    deletion sweep would have cleared the task and left the episodes behind.
    """
    from datetime import date as _d
    try:
        d = _d.fromisoformat(iso_date[:10])
    except (TypeError, ValueError):
        return []
    ab, full = _MONTH_ABBR[d.month - 1], _MONTH_FULL[d.month - 1]
    return [
        iso_date[:10], iso_date[:7],
        f"{d.day} {ab} {d.year}", f"{d.day} {full} {d.year}",
        f"{ab} {d.day}, {d.year}", f"{full} {d.day}, {d.year}",
        f"{d.day} {ab}", f"{d.day} {full}",
        f"{ab} {d.year}", f"{full} {d.year}",
    ]


def mentions_date(text: str, iso_date: str) -> bool:
    blob = (text or "").replace("/", "-").lower()
    return any(n.lower() in blob for n in date_needles(iso_date))


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


# ── Deletions ───────────────────────────────────────────────────────────────
# Calendar READS are live — get_events hits the API every call, nothing is
# cached. What goes stale are the copies: a task whose due_date came off an
# event, a vault fact asserting an appointment. reconcile_task_dates above
# follows an event that MOVES, but when an event is deleted
# find_event_for_task simply returns None and the loop continues, so the copies
# outlive the event silently.
#
# Detecting a deletion needs memory of what was there before, so each run
# snapshots the calendar and the next run diffs against it.

def snapshot_events(events: list[dict]) -> dict:
    """The bit of each event worth remembering between runs."""
    return {
        e["id"]: {"title": e.get("title") or "", "start": (e.get("start") or "")[:10]}
        for e in events if e.get("id")
    }


def detect_deletions(previous: dict, events: list[dict], today: str) -> list[dict]:
    """Events that were on the calendar last run and are not there now.

    Two things are NOT deletions and must not be reported as such:

    - an event that has simply happened. get_events passes timeMin=now, so past
      events drop out of the window on their own.
    - an event beyond the truncation point. get_events takes max_results, so a
      busy 90 days can cut the tail off the list, and everything past the cut
      would look deleted. Only events inside the range actually returned count,
      which is why the horizon below is the LAST event we got back.
    """
    if not previous or not events:
        return []
    current_ids = {e["id"] for e in events if e.get("id")}
    starts = sorted((e.get("start") or "")[:10] for e in events if e.get("start"))
    horizon = starts[-1] if starts else today

    gone = []
    for event_id, meta in previous.items():
        if event_id in current_ids:
            continue
        start = (meta.get("start") or "")[:10]
        if not start or start < today:
            continue          # already happened
        if start > horizon:
            continue          # past where the fetch reached; unknowable
        gone.append({"id": event_id, "title": meta.get("title") or "", "start": start})
    return sorted(gone, key=lambda g: g["start"])


def find_stale_copies(deleted: dict, tasks: list[dict]) -> list[dict]:
    """Open tasks that look like they came from this now-deleted event.

    Same title-overlap test the move path uses, but anchored to the deleted
    event's own date rather than a 45-day window — a task due the day of the
    event is evidence; a task sharing two words a month away is not.
    """
    from datetime import date as _date

    event_words = _words(deleted.get("title") or "")
    if not event_words:
        return []
    try:
        event_date = _date.fromisoformat(deleted["start"])
    except (ValueError, KeyError):
        return []

    out = []
    for t in tasks:
        if t.get("done") or t.get("category") in _SKIP_CATEGORIES:
            continue
        due = t.get("due_date")
        if not due:
            continue
        try:
            if abs((_date.fromisoformat(due) - event_date).days) > 2:
                continue
        except ValueError:
            continue
        if len(_words(t.get("task") or "") & event_words) >= 2:
            out.append(t)
    return out
