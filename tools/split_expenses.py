"""Read-only view into the Split app (split.frontleft.group) — Ansen & Jess's
expense tracker. Uses Split's public per-group API (only needs the group code);
Split runs on its own Supabase project, so no direct DB access from here."""
import html
import os
from datetime import datetime, timedelta

import httpx
from zoneinfo import ZoneInfo

from tools.tz import local_today

SGT = ZoneInfo("Asia/Singapore")

SPLIT_API_BASE = os.getenv("SPLIT_API_BASE", "https://split.frontleft.group")
SPLIT_GROUP_CODE = os.getenv("SPLIT_GROUP_CODE", "56GC22")
# Only needed if the Split group has a password set (Group password in the menu)
SPLIT_HEADERS = {"x-group-password": os.environ["SPLIT_GROUP_PASSWORD"]} if os.environ.get("SPLIT_GROUP_PASSWORD") else {}

# Split's category ids (src/categories.ts in the splitwise-clone repo)
CATEGORIES = ["food", "groceries", "transport", "accommodation", "entertainment",
              "shopping", "utilities", "health", "travel", "baby", "wedding",
              "pet", "home", "other"]


def _get(path: str, **params) -> dict:
    resp = httpx.get(f"{SPLIT_API_BASE}/api/groups/{SPLIT_GROUP_CODE}{path}",
                     params=params or None, headers=SPLIT_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _fetch_group() -> dict:
    return _get("").get("group") or {}


def _expense_day(e: dict) -> str:
    """The SGT day an expense counts in: the date it happened (Split's happenedOn)
    or the SGT day it was logged. Never the UTC day of createdAt."""
    if e.get("happenedOn"):
        return e["happenedOn"]
    return datetime.fromtimestamp((e.get("createdAt") or 0) / 1000, tz=SGT).date().isoformat()


def get_expenses(days: int = 30, category: str | None = None) -> dict:
    """Recent expenses + totals by category for the couple's Split group."""
    group = _fetch_group()
    members = {m["id"]: m["name"] for m in group.get("members", [])}
    accounts = {a["id"]: a["name"] for a in group.get("accounts", [])}
    cutoff = (local_today() - timedelta(days=days)).isoformat()

    expenses = []
    totals_by_category: dict = {}
    total = 0.0
    for e in group.get("expenses", []):
        day = _expense_day(e)
        if day < cutoff:
            continue
        cat = e.get("category") or "other"
        if category and cat != category:
            continue
        amount = float(e.get("amount") or 0)
        paid_by = e.get("paidBy")
        expenses.append({
            "date": day,
            "description": e.get("description"),
            "amount": amount,
            "category": cat,
            "paid_by": members.get(paid_by) or accounts.get(paid_by) or paid_by,
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


def get_recap(month: str | None = None) -> dict:
    """One month in the app's own numbers: spending, savings, who owes whom.
    Computed by Split itself (GET /recap), so nothing here recomputes money.
    `month` is YYYY-MM; default is last month, SGT. Balances are as of today."""
    return _get("/recap", **({"month": month} if month else {}))


def get_joint_accounts() -> dict:
    """The couple's joint bank, card and investments from Split, as the app shows
    them (the bank estimate follows Split's rules, not a copy of them). A bank or
    investment has last_balance / estimated_now; a card has last_owed / owed_now.
    kind is "bank", "card" or "investment"."""
    recap = get_recap()
    out = []
    for a in (recap.get("money") or {}).get("accounts", []):
        row = {"name": a["name"], "kind": a["kind"], "id": a["id"], "last_checked": a.get("lastChecked")}
        if a["kind"] == "card":
            row.update({"last_owed": a.get("lastCheckAmount"), "owed_now": a.get("amount")})
        else:
            row.update({"last_balance": a.get("lastCheckAmount"), "estimated_now": a.get("amount")})
        out.append(row)
    return {"currency": recap.get("currency") or "SGD", "accounts": out}


def _fmt(cur: str, n: float) -> str:
    return f"{cur} {n:,.2f}"


def human_day(iso: str) -> str:
    """2026-10-13 -> 13 Oct"""
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d.day} {d:%b}"


def _month_name(label: str, year: str) -> str:
    """'August 2026' -> 'August' when it is the same year as the recap's month."""
    return label.rsplit(" ", 1)[0] if label.endswith(year) else label


def format_recap(r: dict) -> str:
    """The monthly recap as a Telegram message (HTML parse mode). No model call:
    every figure is copied from Split's recap, nothing is computed here."""
    cur = r.get("currency") or "SGD"
    sp = r.get("spending") or {}
    mo = r.get("money") or {}
    esc = lambda t: html.escape(str(t or ""), quote=False)
    lines = [f"📊 <b>{esc(r.get('label'))} recap</b>", ""]

    lines.append("💸 <b>Spending</b>")
    if not sp.get("total"):
        lines.append(f"No expenses were logged in {esc(r.get('label'))}.")
    else:
        head = f"{_fmt(cur, sp['total'])} across {sp['count']} expenses"
        prev, pct = sp.get("previousTotal"), sp.get("changePct")
        if prev and pct is not None:
            head += f", {'up' if pct >= 0 else 'down'} {abs(pct)}% on {esc(_month_name(sp.get('previousLabel') or '', (r.get('month') or '')[:4]))} ({_fmt(cur, prev)})"
        lines.append(head)
        lines.append("")
        for c in sp.get("categories", []):
            lines.append(f"• {esc(c['label'])} {_fmt(cur, c['total'])} ({c['sharePct']}%)")
        paid = sp.get("paidBy", [])
        if paid:
            lines += ["", "Paid by " + ", ".join(f"{esc(p['name'])} {_fmt(cur, p['total'])}" for p in paid)]
        if sp.get("biggest"):
            lines += ["", "Biggest:"]
            lines += [f"• {esc(b['description'])} {_fmt(cur, b['amount'])}" for b in sp["biggest"]]

    money_lines = []
    if mo.get("cash") is not None:
        money_lines.append(f"• Cash in the bank {_fmt(cur, mo['cash'])}")
    if mo.get("invested") is not None:
        line = f"• Invested {_fmt(cur, mo['invested'])}"
        if mo.get("gain") is not None:
            pct = f", {abs(mo['gainPct'])}%" if mo.get("gainPct") is not None else ""
            line += f" ({_fmt(cur, abs(mo['gain']))} {'up' if mo['gain'] >= 0 else 'down'}{pct})"
        money_lines.append(line)
    if mo.get("cardOwed"):
        money_lines.append(f"• Owed on the card {_fmt(cur, mo['cardOwed'])}")
    if money_lines:
        lines += ["", "🏦 <b>Savings</b>"] + money_lines
        if mo.get("asOf"):
            lines.append(f"Balances as of {human_day(mo['asOf'])}.")

    lines += ["", "🤝 <b>Who owes whom</b>"]
    owes = r.get("owes") or []
    if owes:
        lines += [f"{esc(o['from'])} owes {esc(o['to'])} {_fmt(cur, o['amount'])}" for o in owes]
    else:
        lines.append("All square.")
    return "\n".join(lines)


def record_balance(amount: float, account: str | None = None,
                   note: str | None = None, by: str | None = None) -> dict:
    """Write a balance check-in in Split. For a bank, `amount` is the balance the
    app shows; for a card it is how much the card app says is OWED. `account` is
    matched by name; with a single bank it can be omitted."""
    group = _fetch_group()
    all_accounts = group.get("accounts", [])
    banks = [a for a in all_accounts if a.get("kind") == "bank"]
    if account and not any(account.strip().lower() in a["name"].lower() for a in banks):
        banks = [a for a in all_accounts if account.strip().lower() in a["name"].lower()]
    if account:
        banks = [a for a in banks if account.strip().lower() in a["name"].lower()] or banks
    if not banks:
        return {"status": "no_bank_account", "detail": "No joint bank account in Split yet."}
    if len(banks) > 1:
        return {"status": "ambiguous", "accounts": [b["name"] for b in banks]}
    body = {"amount": amount}
    if note:
        body["note"] = note
    if by:
        body["recordedBy"] = by
    resp = httpx.post(
        f"{SPLIT_API_BASE}/api/groups/{SPLIT_GROUP_CODE}/accounts/{banks[0]['id']}/balance",
        json=body, headers=SPLIT_HEADERS, timeout=15)
    resp.raise_for_status()
    return {"status": "recorded", "account": banks[0]["name"], "amount": amount}
