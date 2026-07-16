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


def search_drops(query: str, limit: int = 8, scan: int = 250) -> list[dict]:
    """Content search across ALL wedding drops, ignoring category — the DJ
    schedule was filed under 'venue', so category filtering loses it. Scores by
    keyword overlap on the drop text and returns the best matches, newest-first
    among ties."""
    rows = (
        get_client().table("wedding_drops")
        .select("*")
        .order("ts", desc=True)
        .limit(scan)
        .execute()
        .data or []
    )
    q_words = {w for w in query.lower().split() if len(w) > 2}
    if not q_words:
        return []
    scored = []
    for r in rows:
        text = (r.get("content") or "").lower()
        if len(text) < 4:
            continue
        score = sum(1 for w in q_words if w in text)
        if score:
            scored.append((score, r))
    scored.sort(key=lambda sr: (sr[0], sr[1].get("ts") or ""), reverse=True)
    return [r for _, r in scored[:limit]]
