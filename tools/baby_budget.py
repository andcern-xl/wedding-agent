from tools.db import get_client


def add_item(item: str, amount: float | None = None, category: str | None = None,
             status: str = "planned", currency: str = "SGD", notes: str | None = None) -> dict:
    row = {"item": item, "status": status, "currency": currency}
    if amount is not None:
        row["amount"] = amount
    if category:
        row["category"] = category
    if notes:
        row["notes"] = notes
    return get_client().table("baby_budget").insert(row).execute().data[0]


def get_all() -> list[dict]:
    try:
        return get_client().table("baby_budget").select("*").order("logged_at").execute().data or []
    except Exception:
        return []


def summary() -> dict:
    items = get_all()
    total_spent = sum(i.get("amount") or 0 for i in items if i.get("status") in ("bought", "deposit"))
    total_planned = sum(i.get("amount") or 0 for i in items if i.get("status") == "planned")
    by_category: dict = {}
    for i in items:
        cat = i.get("category") or "other"
        by_category.setdefault(cat, []).append(i)
    return {
        "total_spent": total_spent,
        "total_planned": total_planned,
        "by_category": by_category,
        "items": items,
    }
