import json
from datetime import datetime
from pathlib import Path

PAYMENTS_FILE = Path(__file__).parent.parent / "data" / "payments.json"


def _load() -> list:
    PAYMENTS_FILE.parent.mkdir(exist_ok=True)
    if not PAYMENTS_FILE.exists():
        return []
    with open(PAYMENTS_FILE) as f:
        return json.load(f)


def _save(data: list):
    PAYMENTS_FILE.parent.mkdir(exist_ok=True)
    with open(PAYMENTS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def add_payment(entry: dict):
    data = _load()
    entry["logged_at"] = datetime.now().isoformat()
    data.append(entry)
    _save(data)


def get_all_payments() -> list:
    return _load()


def summary() -> dict:
    payments = _load()
    total_paid = 0
    total_owing = 0
    by_person = {}
    by_vendor = {}

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
