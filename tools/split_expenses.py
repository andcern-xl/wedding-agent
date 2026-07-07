"""Read-only view into the Split app (split.frontleft.group) — Ansen & Jess's
expense tracker. Uses Split's public per-group API (only needs the group code);
Split runs on its own Supabase project, so no direct DB access from here."""
import os
from datetime import datetime, timedelta, timezone

import httpx

SPLIT_API_BASE = os.getenv("SPLIT_API_BASE", "https://split.frontleft.group")
SPLIT_GROUP_CODE = os.getenv("SPLIT_GROUP_CODE", "56GC22")

# Split's category ids (src/categories.ts in the splitwise-clone repo)
CATEGORIES = ["food", "groceries", "transport", "accommodation", "entertainment",
              "shopping", "utilities", "health", "travel", "baby", "wedding",
              "pet", "home", "other"]


def get_expenses(days: int = 30, category: str | None = None) -> dict:
    """Recent expenses + totals by category for the couple's Split group."""
    resp = httpx.get(f"{SPLIT_API_BASE}/api/groups/{SPLIT_GROUP_CODE}", timeout=15)
    resp.raise_for_status()
    group = resp.json().get("group") or {}

    members = {m["id"]: m["name"] for m in group.get("members", [])}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ms = cutoff.timestamp() * 1000

    expenses = []
    totals_by_category: dict = {}
    total = 0.0
    for e in group.get("expenses", []):
        created = e.get("createdAt") or 0
        if created < cutoff_ms:
            continue
        cat = e.get("category") or "other"
        if category and cat != category:
            continue
        amount = float(e.get("amount") or 0)
        expenses.append({
            "date": datetime.fromtimestamp(created / 1000, tz=timezone.utc).date().isoformat(),
            "description": e.get("description"),
            "amount": amount,
            "category": cat,
            "paid_by": members.get(e.get("paidBy"), e.get("paidBy")),
        })
        totals_by_category[cat] = round(totals_by_category.get(cat, 0) + amount, 2)
        total += amount

    expenses.sort(key=lambda x: x["date"], reverse=True)
    return {
        "group_name": group.get("name"),
        "currency": group.get("currency") or "SGD",
        "days": days,
        "total": round(total, 2),
        "totals_by_category": totals_by_category,
        "expenses": expenses[:40],
    }
