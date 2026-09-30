"""Trade + X signals from the Growth Research agent (Supabase `trade_signals`).

The agent POSTs batches to webhook.py; the stocks brief reads them back as one
more source toward its convergence bar. Validation lives here so the webhook
stays a thin HTTP layer and the rules are testable without a server.
"""
from datetime import datetime, timedelta, timezone
from tools.db import get_client

TYPES = {"crypto", "stock", "x_social"}
DIRECTIONS = {"long", "short", "neutral", "watch"}
MAX_SIGNALS = 200   # per batch
DEDUPE_HOURS = 24


def _str(v, limit: int) -> str:
    return v.strip()[:limit] if isinstance(v, str) else ""


def validate(payload) -> tuple[dict | None, str | None]:
    """Return (clean batch, None) or (None, error message for a 400)."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    source = _str(payload.get("source"), 80)
    if not source:
        return None, "source is required"
    as_of = payload.get("as_of")
    if as_of is not None:
        try:
            datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        except ValueError:
            return None, "as_of must be an ISO 8601 timestamp"
    signals = payload.get("signals")
    if not isinstance(signals, list) or not signals:
        return None, "signals must be a non-empty array"
    if len(signals) > MAX_SIGNALS:
        return None, f"at most {MAX_SIGNALS} signals per batch"

    clean = []
    for i, s in enumerate(signals):
        if not isinstance(s, dict):
            return None, f"signals[{i}] must be an object"
        sid, symbol = _str(s.get("id"), 200), _str(s.get("symbol"), 40).upper()
        typ, direction = s.get("type"), s.get("direction")
        if not sid:
            return None, f"signals[{i}].id is required"
        if not symbol:
            return None, f"signals[{i}].symbol is required"
        if typ not in TYPES:
            return None, f"signals[{i}].type must be one of {sorted(TYPES)}"
        if direction not in DIRECTIONS:
            return None, f"signals[{i}].direction must be one of {sorted(DIRECTIONS)}"
        strength = s.get("strength", 0)
        if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not 0 <= strength <= 1:
            return None, f"signals[{i}].strength must be a number from 0 to 1"
        urls = s.get("proof_urls") or []
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            return None, f"signals[{i}].proof_urls must be an array of strings"
        raw = s.get("raw") or {}
        if not isinstance(raw, dict):
            return None, f"signals[{i}].raw must be an object"
        clean.append({
            "signal_id": sid, "type": typ, "symbol": symbol, "direction": direction,
            "strength": float(strength), "summary": _str(s.get("summary"), 500),
            "proof_urls": [u[:500] for u in urls[:10]], "raw": raw,
        })
    return {"source": source, "as_of": as_of, "signals": clean}, None


def save_batch(batch: dict) -> list[dict]:
    """Insert the batch, skipping any signal_id this source already sent in the
    last 24h (and repeats within the batch). Returns the rows stored, so the
    caller alerts only on signals that are actually new."""
    db = get_client()
    since = (datetime.now(timezone.utc) - timedelta(hours=DEDUPE_HOURS)).isoformat()
    ids = list({s["signal_id"] for s in batch["signals"]})
    seen = {r["signal_id"] for r in (
        db.table("trade_signals").select("signal_id")
        .eq("source", batch["source"]).in_("signal_id", ids)
        .gte("received_at", since).execute().data or [])}
    rows = []
    for s in batch["signals"]:
        if s["signal_id"] in seen:
            continue
        seen.add(s["signal_id"])
        rows.append({**s, "source": batch["source"], "as_of": batch["as_of"]})
    if rows:
        db.table("trade_signals").insert(rows).execute()
    return rows


def recent_signals(hours: int = 36) -> list[dict]:
    """Signals received in the last `hours`, newest first."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return (get_client().table("trade_signals")
            .select("source,signal_id,type,symbol,direction,strength,summary,proof_urls,as_of,received_at")
            .gte("received_at", since).order("received_at", desc=True)
            .limit(500).execute().data or [])
