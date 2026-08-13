from datetime import date, datetime, timedelta, timezone
from tools.db import get_client
from tools.tz import local_today

SHARED_BRAIN_ID = 0  # sentinel user_id for the couple's shared context


def get_summary(user_id: int) -> str:
    rows = (
        get_client().table("user_summaries")
        .select("summary")
        .eq("user_id", user_id)
        .execute()
        .data or []
    )
    return rows[0]["summary"] if rows else ""


def save_summary(user_id: int, summary: str, message_count: int):
    get_client().table("user_summaries").upsert({
        "user_id": user_id,
        "summary": summary,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "message_count": message_count,
    }).execute()


# ── Shared brain vault (brain_entries table) ────────────────────────────────
# One fact per row, domain-filed, supersede-not-rewrite. The legacy blob on
# user_summaries user_id=0 is frozen as the rollback path — never write it.

DOMAINS = ("baby", "wedding", "travel", "money", "life")

_DOMAIN_ALIASES = {
    "baby": "baby", "baby_questions": "baby", "pregnancy": "baby",
    "wedding": "wedding",
    "travel": "travel", "trip": "travel", "trips": "travel",
    "money": "money", "finance": "money", "financial": "money",
    "stocks": "money", "budget": "money",
    "life": "life",
}


def normalize_domain(raw: str | None) -> str:
    return _DOMAIN_ALIASES.get((raw or "").strip().lower(), "life")


def get_active_entries(domain: str | None = None, kind: str | None = None) -> list[dict]:
    try:
        q = (
            get_client().table("brain_entries")
            .select("id,domain,fact,fact_date,source,kind")
            .eq("status", "active")
        )
        if domain:
            q = q.eq("domain", domain)
        if kind:
            q = q.eq("kind", kind)
        rows = q.order("fact_date", desc=False).execute().data or []
    except Exception:
        # kind column not migrated yet — treat everything as a fact
        if kind == "episode":
            return []
        q = (
            get_client().table("brain_entries")
            .select("id,domain,fact,fact_date,source")
            .eq("status", "active")
        )
        if domain:
            q = q.eq("domain", domain)
        rows = q.order("fact_date", desc=False).execute().data or []
        for r in rows:
            r["kind"] = "fact"
    order = {d: i for i, d in enumerate(DOMAINS)}
    rows.sort(key=lambda r: (order.get(r.get("domain"), len(DOMAINS)), r.get("fact_date") or ""))
    return rows


def get_episodes(days: int = 45) -> list[dict]:
    """Active episodes (dated life events), newest first, within the window."""
    cutoff = (local_today() - timedelta(days=days)).isoformat()
    rows = [
        e for e in get_active_entries(kind="episode")
        if (e.get("fact_date") or "") >= cutoff
    ]
    rows.sort(key=lambda r: r.get("fact_date") or "", reverse=True)
    return rows


def add_brain_entry(fact: str, domain: str = "life", source: str = "chat",
                    fact_date: str | None = None, kind: str = "fact") -> dict:
    payload = {
        "fact": fact.strip(),
        "domain": normalize_domain(domain),
        "source": source,
        "fact_date": fact_date or local_today().isoformat(),
        "kind": kind,
    }
    try:
        row = get_client().table("brain_entries").insert(payload).execute().data or [{}]
    except Exception:
        # kind column not migrated yet
        payload.pop("kind", None)
        row = get_client().table("brain_entries").insert(payload).execute().data or [{}]
    return row[0]


def supersede_entries(entry_ids: list[str], superseded_by: str | None = None) -> None:
    if not entry_ids:
        return
    payload = {
        "status": "superseded",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if superseded_by:
        payload["superseded_by"] = superseded_by
    get_client().table("brain_entries").update(payload).in_("id", entry_ids).execute()


def render_shared_summary(entries: list[dict]) -> str:
    """Render vault entries in the legacy bullet grammar `• YYYY-MM-DD: [domain] fact`.
    No header lines — _relevant_bullets parses dates from fixed offsets per line."""
    return "\n".join(
        f"• {e['fact_date']}: [{e['domain']}] {e['fact']}" for e in entries
    )


def get_legacy_shared_blob() -> str:
    rows = (
        get_client().table("user_summaries")
        .select("summary")
        .eq("user_id", SHARED_BRAIN_ID)
        .execute()
        .data or []
    )
    return rows[0]["summary"] if rows else ""


def get_shared_summary() -> str:
    """Facts only. Episodes are dated life events — they reach the agent through
    RECALL (query_brain / get_episodes), not by being injected into every brief's
    brain slice. Rendering them here fed dead nags ("Unanswered check-in: travel
    insurance still not done") back in as standing knowledge, which is how briefs
    ended up repeating resolved items."""
    try:
        entries = get_active_entries(kind="fact")
    except Exception:
        entries = []
    return render_shared_summary(entries) if entries else get_legacy_shared_blob()


def append_shared_summary(content: str, domain: str = "life", source: str = "manual") -> str:
    """Add one fact to the shared brain vault. Returns the full updated summary."""
    add_brain_entry(content, domain=domain, source=source)
    return get_shared_summary()


def get_message_count(user_id: int) -> int:
    rows = (
        get_client().table("user_summaries")
        .select("message_count")
        .eq("user_id", user_id)
        .execute()
        .data or []
    )
    return rows[0]["message_count"] if rows else 0
