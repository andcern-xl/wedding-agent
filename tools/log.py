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
    """Newest `limit` drops, returned oldest-first for reading. Ordering desc
    before the limit matters: ascending + limit silently handed back the OLDEST
    rows, so a 98-row budget category answered every question with April."""
    q = get_client().table("wedding_drops").select("*").order("ts", desc=True)
    if category:
        q = q.eq("category", category)
    return (q.limit(limit).execute().data or [])[::-1]


def get_drops_since(ts: str, limit: int = 150) -> tuple[list[dict], int]:
    """Every drop after `ts`, oldest-first, plus how many were left behind by
    the cap. The weekly sweep used to take 'the last 20' — a busy week (April
    logged 50) silently overflowed and those drops never became facts. Asking
    'what's new since I last looked' is the only version that can't lose any."""
    rows = (
        get_client().table("wedding_drops")
        .select("*")
        .gt("ts", ts)
        .order("ts", desc=False)
        .execute()
        .data or []
    )
    overflow = max(0, len(rows) - limit)
    return rows[:limit], overflow


def total_drops() -> int:
    try:
        res = get_client().table("wedding_drops").select("id", count="exact").limit(1).execute()
        return res.count or 0
    except Exception:
        return 0


def get_recent_drops(limit: int = 30) -> list[dict]:
    return (
        get_client().table("wedding_drops")
        .select("*")
        .order("ts", desc=True)
        .limit(limit)
        .execute()
        .data or []
    )[::-1]


_DROP_STOPWORDS = {
    "the", "and", "for", "our", "any", "all", "was", "are", "with", "what",
    "when", "where", "have", "has", "had", "did", "does", "you", "your", "who",
    "about", "wedding", "info", "information", "details", "detail", "stuff",
    "thing", "things", "plan", "plans", "saved", "know", "tell", "show",
}


def _drop_words(text: str) -> set:
    import re
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower())
            if w not in _DROP_STOPWORDS}


def search_drops(query: str, limit: int = 8, scan: int = 400) -> list[dict]:
    """Content search across ALL wedding drops, ignoring category — the day-of
    plan is filed under 'ceremony', the lunch timings under 'budget' and the DJ
    schedule under 'venue', so category filtering loses every one of them.

    Scored on how much of the QUERY a drop covers, not on raw hit count: a long
    screenshot dump used to outrank the one short note that actually answers the
    question, purely by being long enough to contain a stray keyword."""
    rows = (
        get_client().table("wedding_drops")
        .select("*")
        .order("ts", desc=True)
        .limit(scan)
        .execute()
        .data or []
    )
    q_words = _drop_words(query)
    if not q_words:
        return []
    scored = []
    for r in rows:
        content = r.get("content") or ""
        if len(content.strip()) < 25:
            continue  # "hi", "yes", "Log this as well" — no recall value
        words = _drop_words(content)
        hits = q_words & words
        if not hits:
            continue
        coverage = len(hits) / len(q_words)
        # Mild density bonus so a focused note edges out a sprawling dump that
        # happens to mention the same term once.
        density = len(hits) / (len(words) ** 0.5 or 1)
        scored.append((round(coverage + 0.15 * density, 4), r))
    scored.sort(key=lambda sr: (sr[0], sr[1].get("ts") or ""), reverse=True)

    # Near-duplicate drops (the same forwarded message logged twice) crowd out
    # variety in a short result list.
    out, seen = [], []
    for _, r in scored:
        sig = _drop_words(r.get("content") or "")
        if any(len(sig & s) / max(len(sig | s), 1) > 0.8 for s in seen):
            continue
        seen.append(sig)
        out.append(r)
        if len(out) >= limit:
            break
    return out
