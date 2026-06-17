from datetime import date
from tools.db import get_client


def save_brief(brief_date: date, assets: list[dict], brief_text: str) -> None:
    """Upsert today's brief. assets = [{name, ticker, type, sentiment, thesis, verdict}]."""
    get_client().table("stocks_knowledge").upsert({
        "brief_date": brief_date.isoformat(),
        "assets": assets,
        "brief_text": brief_text,
    }, on_conflict="brief_date").execute()


def get_recent_briefs(limit: int = 4) -> list[dict]:
    """Return the last `limit` briefs, newest first."""
    rows = (
        get_client().table("stocks_knowledge")
        .select("brief_date,assets,brief_text")
        .order("brief_date", desc=True)
        .limit(limit)
        .execute()
        .data or []
    )
    return rows


def search_asset(query: str, limit: int = 6) -> list[dict]:
    """Return recent brief entries mentioning this asset name or ticker (case-insensitive)."""
    rows = get_recent_briefs(limit=limit)
    q = query.lower().strip()
    hits = []
    for row in rows:
        matched = [
            a for a in (row.get("assets") or [])
            if q in (a.get("name") or "").lower()
            or q in (a.get("ticker") or "").lower()
        ]
        if matched:
            hits.append({"brief_date": row["brief_date"], "assets": matched})
    return hits
