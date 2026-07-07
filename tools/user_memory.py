from datetime import date, datetime, timezone
from tools.db import get_client

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


def get_active_entries(domain: str | None = None) -> list[dict]:
    q = (
        get_client().table("brain_entries")
        .select("id,domain,fact,fact_date,source")
        .eq("status", "active")
    )
    if domain:
        q = q.eq("domain", domain)
    rows = q.order("fact_date", desc=False).execute().data or []
    order = {d: i for i, d in enumerate(DOMAINS)}
    rows.sort(key=lambda r: (order.get(r.get("domain"), len(DOMAINS)), r.get("fact_date") or ""))
    return rows


def add_brain_entry(fact: str, domain: str = "life", source: str = "chat",
                    fact_date: str | None = None) -> dict:
    row = (
        get_client().table("brain_entries")
        .insert({
            "fact": fact.strip(),
            "domain": normalize_domain(domain),
            "source": source,
            "fact_date": fact_date or date.today().isoformat(),
        })
        .execute()
        .data or [{}]
    )
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
    try:
        entries = get_active_entries()
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
