from tools.db import get_client


def save_entry(summary: str, tags: list[str], raw_text: str = "", user_id: int = 0, source: str = "screenshot") -> dict:
    row = {
        "user_id": user_id,
        "summary": summary,
        "raw_text": raw_text[:5000],
        "tags": tags,
        "source": source,
    }
    return get_client().table("baby_knowledge").insert(row).execute().data[0]


def get_entries(limit: int = 30) -> list[dict]:
    try:
        return (
            get_client()
            .table("baby_knowledge")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data or []
        )
    except Exception:
        return []


def search_entries(query: str) -> list[dict]:
    """Simple keyword search across summary and raw_text."""
    try:
        all_entries = get_entries(limit=100)
        q = query.lower()
        return [
            e for e in all_entries
            if q in (e.get("summary") or "").lower()
            or q in (e.get("raw_text") or "").lower()
            or any(q in tag.lower() for tag in (e.get("tags") or []))
        ]
    except Exception:
        return []
