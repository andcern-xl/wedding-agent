"""Investment holdings — the couple's portfolio. Units for countable assets
(crypto/stocks), value for fund/cash positions; as_of anchors drift."""
from datetime import date, datetime, timezone

from tools.db import get_client
from tools.tz import local_today

STALE_DAYS = 30


def add_holding(asset: str, asset_type: str = "stock", owner: str = "joint",
                ticker: str | None = None, platform: str | None = None,
                units: float | None = None, avg_cost: float | None = None,
                value: float | None = None, currency: str = "SGD",
                notes: str | None = None, as_of: str | None = None) -> dict:
    row = {
        "asset": asset.strip(),
        "asset_type": asset_type,
        "owner": owner,
        "currency": currency,
        "as_of": as_of or local_today().isoformat(),
    }
    for k, v in (("ticker", ticker), ("platform", platform), ("units", units),
                 ("avg_cost", avg_cost), ("value", value), ("notes", notes)):
        if v is not None:
            row[k] = v
    return get_client().table("holdings").insert(row).execute().data[0]


def find_holding(asset_or_ticker: str, owner: str | None = None) -> dict | None:
    """Match an active holding by ticker (exact, case-insensitive) or asset name substring."""
    needle = asset_or_ticker.strip().lower()
    for h in get_holdings(owner=owner):
        if (h.get("ticker") or "").lower() == needle:
            return h
    for h in get_holdings(owner=owner):
        if needle in (h.get("asset") or "").lower():
            return h
    return None


def update_holding(holding_id: str, units: float | None = None,
                   avg_cost: float | None = None, value: float | None = None,
                   notes: str | None = None, as_of: str | None = None) -> dict | None:
    payload: dict = {
        "as_of": as_of or local_today().isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    for k, v in (("units", units), ("avg_cost", avg_cost), ("value", value), ("notes", notes)):
        if v is not None:
            payload[k] = v
    rows = get_client().table("holdings").update(payload).eq("id", holding_id).execute().data
    return rows[0] if rows else None


def close_holding(holding_id: str) -> bool:
    rows = (
        get_client().table("holdings")
        .update({"status": "closed", "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", holding_id).execute().data
    )
    return bool(rows)


def get_holdings(owner: str | None = None, include_closed: bool = False) -> list[dict]:
    try:
        q = get_client().table("holdings").select("*")
        if not include_closed:
            q = q.eq("status", "active")
        if owner:
            q = q.eq("owner", owner)
        return q.order("asset").execute().data or []
    except Exception:
        return []


def summary() -> dict:
    items = get_holdings()
    by_type: dict = {}
    totals_by_currency: dict = {}
    stale = []
    today = local_today()
    for h in items:
        by_type.setdefault(h.get("asset_type") or "other", []).append(h)
        if h.get("value") is not None:
            cur = h.get("currency") or "SGD"
            totals_by_currency[cur] = totals_by_currency.get(cur, 0) + float(h["value"])
        try:
            age = (today - date.fromisoformat(h["as_of"])).days
            if age > STALE_DAYS:
                stale.append({**h, "days_stale": age})
        except (ValueError, TypeError, KeyError):
            pass
    return {
        "count": len(items),
        "by_type": by_type,
        "totals_by_currency": totals_by_currency,
        "stale": stale,
        "items": items,
    }
