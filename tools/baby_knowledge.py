import logging
import re

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


def _legacy_entries(limit: int) -> list[dict]:
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


_SYMPTOM_KEEP = 3


def _brain_entries(limit: int) -> list[dict]:
    """Active baby-domain vault facts, shaped like baby_knowledge rows.

    Since late Aug 2026 the chat files baby facts to brain_entries (domain
    'baby'), not here — this table's last write was 26 Aug. Every reader went
    through get_entries(), so every reader froze on that date: the 📚 Knowledge
    view said "Week 7" at week 19 and never showed a product or Instagram link
    sent in September. Daily symptom logs are collapsed to the latest few so
    they cannot crowd out real knowledge.
    """
    try:
        rows = (
            get_client().table("brain_entries")
            .select("id,fact,fact_date,created_at,source")
            .eq("domain", "baby").eq("status", "active")
            .order("created_at", desc=True).limit(max(limit, 200))
            .execute().data or []
        )
    except Exception:
        logging.getLogger(__name__).exception("baby_knowledge: brain read failed")
        return []
    out, symptoms = [], 0
    for r in rows:
        fact = r.get("fact") or ""
        if r.get("source") == "symptom" or fact.lower().startswith("symptom ("):
            symptoms += 1
            if symptoms > _SYMPTOM_KEEP:
                continue
        out.append({
            "id": r.get("id"), "summary": fact, "raw_text": "", "tags": [],
            "source": f"brain:{r.get('source') or ''}",
            "created_at": r.get("fact_date") or r.get("created_at") or "",
            "_store": "brain",
        })
    return out


def get_entries(limit: int = 30, include_brain: bool = True) -> list[dict]:
    """Baby knowledge, newest first, from BOTH stores.

    include_brain=False returns only this table's rows — for callers that edit
    or delete by id (correct_knowledge), since a vault id means nothing here.
    Anything cut by `limit` is logged, never silently dropped.
    """
    legacy = _legacy_entries(max(limit, 200) if include_brain else limit)
    if not include_brain:
        return legacy
    merged = sorted(legacy + _brain_entries(limit),
                    key=lambda e: (e.get("created_at") or "")[:10], reverse=True)
    if len(merged) > limit:
        logging.getLogger(__name__).info(
            "baby_knowledge.get_entries: %d of %d entries beyond limit=%d not returned",
            len(merged) - limit, len(merged), limit)
    return merged[:limit]


def search_entries(query: str) -> list[dict]:
    """Keyword search across both stores. Matches the whole phrase, or failing
    that any distinctive word — a long phrase rarely appears verbatim."""
    try:
        all_entries = get_entries(limit=1000)
        q = (query or "").lower().strip()

        def blob(e):
            return " ".join([(e.get("summary") or ""), (e.get("raw_text") or ""),
                             " ".join(e.get("tags") or [])]).lower()

        hits = [e for e in all_entries if q and q in blob(e)]
        if hits:
            return hits
        words = [w for w in re.findall(r"\w{4,}", q)]
        return [e for e in all_entries if words and any(w in blob(e) for w in words)]
    except Exception:
        return []


def delete_entry(entry_id: str) -> bool:
    try:
        result = get_client().table("baby_knowledge").delete().eq("id", entry_id).execute()
        return bool(result.data)
    except Exception:
        return False


def update_entry(entry_id: str, new_summary: str) -> bool:
    try:
        result = get_client().table("baby_knowledge").update({"summary": new_summary}).eq("id", entry_id).execute()
        return bool(result.data)
    except Exception:
        return False
