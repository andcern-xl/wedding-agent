from datetime import datetime, timezone
from tools.db import get_client


def add_payment(entry: dict):
    entry["logged_at"] = datetime.now(timezone.utc).isoformat()
    get_client().table("wedding_payments").insert(entry).execute()


def get_all_payments() -> list:
    return get_client().table("wedding_payments").select("*").order("logged_at").execute().data or []


def summary() -> dict:
    payments = get_all_payments()
    total_paid = 0
    total_owing = 0
    by_person: dict = {}
    by_vendor: dict = {}

    for p in payments:
        amount = p.get("amount", 0)
        status = p.get("status", "unknown")
        paid_by = p.get("paid_by", "unknown")
        vendor = p.get("vendor", "unknown")

        if status in ("paid", "deposit"):
            total_paid += amount
            by_person[paid_by] = by_person.get(paid_by, 0) + amount
        elif status == "owing":
            total_owing += amount

        by_vendor[vendor] = by_vendor.get(vendor, 0) + amount

    return {
        "total_paid": total_paid,
        "total_owing": total_owing,
        "by_person": by_person,
        "by_vendor": by_vendor,
        "payments": payments,
    }
