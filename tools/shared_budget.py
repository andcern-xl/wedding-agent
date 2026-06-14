from tools.db import get_client


def add_item(item: str, amount: float | None = None, category: str | None = None,
             status: str = "owing", currency: str = "SGD", notes: str | None = None) -> dict:
    row = {"item": item, "status": status, "currency": currency}
    if amount is not None:
        row["amount"] = amount
    if category:
        row["category"] = category
    if notes:
        row["notes"] = notes
    return get_client().table("shared_budget").insert(row).execute().data[0]


def get_all() -> list[dict]:
    try:
        return get_client().table("shared_budget").select("*").order("logged_at").execute().data or []
    except Exception:
        return []


def summary() -> dict:
    items = get_all()
    total_owing  = sum(i.get("amount") or 0 for i in items if i.get("status") in ("owing", "pending"))
    total_paid   = sum(i.get("amount") or 0 for i in items if i.get("status") == "paid")
    by_category: dict = {}
    for i in items:
        cat = i.get("category") or "other"
        by_category.setdefault(cat, []).append(i)
    return {
        "total_owing": total_owing,
        "total_paid": total_paid,
        "by_category": by_category,
        "items": items,
    }
