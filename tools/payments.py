from datetime import datetime, timezone
from tools.db import as_num, get_client


def add_payment(entry: dict):
    entry["logged_at"] = datetime.now(timezone.utc).isoformat()
    get_client().table("wedding_payments").insert(entry).execute()


def get_all_payments() -> list:
    return get_client().table("wedding_payments").select("*").order("logged_at").execute().data or []


def summary() -> dict:
    """Wedding payment totals, per currency.

    Summing across currencies was giving a wedding spend of S$1.24M, because a
    KRW 1,204,280 Seoul hotel row (about S$1,240) was added straight into the
    SGD total. Six currencies are in this table. `total_paid`/`total_owing` stay
    scalars for the callers that format them, but they are SGD ONLY — everything
    else is in the per-currency maps, so a caller can report it rather than
    silently fold it in. Same shape holdings.py already uses.
    """
    payments = get_all_payments()
    paid_by_currency: dict = {}
    owing_by_currency: dict = {}
    by_person: dict = {}
    by_vendor: dict = {}

    for p in payments:
        amount = as_num(p.get("amount"))
        status = p.get("status", "unknown")
        paid_by = p.get("paid_by", "unknown")
        vendor = p.get("vendor", "unknown")
        cur = p.get("currency") or "SGD"

        if status in ("paid", "deposit"):
            paid_by_currency[cur] = paid_by_currency.get(cur, 0) + amount
            by_person.setdefault(cur, {})
            by_person[cur][paid_by] = by_person[cur].get(paid_by, 0) + amount
        elif status == "owing":
            owing_by_currency[cur] = owing_by_currency.get(cur, 0) + amount

        by_vendor.setdefault(cur, {})
        by_vendor[cur][vendor] = by_vendor[cur].get(vendor, 0) + amount

    return {
        # SGD only — see the docstring. Other currencies are in the maps below.
        "total_paid": paid_by_currency.get("SGD", 0),
        "total_owing": owing_by_currency.get("SGD", 0),
        "currency": "SGD",
        "paid_by_currency": paid_by_currency,
        "owing_by_currency": owing_by_currency,
        "other_currencies": {c: v for c, v in paid_by_currency.items() if c != "SGD"},
        "by_person": by_person.get("SGD", {}),
        "by_vendor": by_vendor.get("SGD", {}),
        "payments": payments,
    }
