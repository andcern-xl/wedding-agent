"""The monthly Split recap: figures come from Split, never recomputed here.

13 Oct 2026 — Split got investments and happenedOn, and the agent's Python copy
of the money math drifted: IBKR and Stashaway read as banks, and "30 days" used
the UTC day of createdAt. The recap now comes from Split's own /recap endpoint;
this locks the message built from it and the readers around it.

    python test_split_recap.py
"""
import asyncio
import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

os.environ.setdefault("SPLIT_GROUP_CODE", "TEST00")

from tools import split_expenses as se

fails = 0


def check(label, ok):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


BASE = {
    "currency": "SGD", "month": "2026-09", "label": "September 2026",
    "spending": {
        "total": 4468.92, "previousTotal": 2773.65, "previousLabel": "August 2026",
        "changePct": 61, "count": 32,
        "categories": [{"id": "wedding", "label": "Wedding", "total": 1242.1, "sharePct": 28},
                       {"id": "food", "label": "Food & Drink", "total": 445.2, "sharePct": 10}],
        "paidBy": [{"name": "Ansen", "total": 3484.44}, {"name": "Jess", "total": 984.48}],
        "biggest": [{"description": "Bryan's <art> (50%)", "amount": 750.0, "paidBy": "Ansen", "day": "2026-09-02"}],
    },
    "money": {"asOf": "2026-10-13", "cash": 7519.07, "cardOwed": 1886.72, "invested": 42568.25,
              "gain": 2849.61, "gainPct": 7.2, "accounts": [], "needsCheckIn": []},
    "owes": [{"from": "Jess", "to": "Ansen", "amount": 931.14}],
}


def variant(**over):
    r = {**BASE, "spending": dict(BASE["spending"]), "money": dict(BASE["money"])}
    for k, v in over.items():
        if k in ("spending", "money"):
            r[k].update(v)
        else:
            r[k] = v
    return r


t = se.format_recap(BASE)
check("title carries the month", "September 2026 recap" in t)
check("total, count and change vs last month", "SGD 4,468.92 across 32 expenses, up 61% on August (SGD 2,773.65)" in t)
check("categories are bullets with shares", "• Wedding SGD 1,242.10 (28%)" in t)
check("who paid", "Paid by Ansen SGD 3,484.44, Jess SGD 984.48" in t)
check("balances are dated, humanly", "Balances as of 13 Oct." in t)
check("who owes whom", "Jess owes Ansen SGD 931.14" in t)
check("gain shown as up with its percent", "Invested SGD 42,568.25 (SGD 2,849.61 up, 7.2%)" in t)
check("HTML from descriptions is escaped, apostrophes are not", "Bryan's &lt;art&gt; (50%)" in t)
check("no markdown asterisks, no em dashes", "*" not in t and "—" not in t)
check("fits one Telegram message", len(t) < 3500)

check("a loss reads as down", "SGD 100.00 down, 1.0%" in se.format_recap(variant(money={"gain": -100.0, "gainPct": -1.0})))
check("nothing owed says all square", "All square." in se.format_recap(variant(owes=[])))
z = se.format_recap(variant(spending={"total": 0, "count": 0, "categories": [], "paidBy": [], "biggest": []}))
check("a month with no expenses says so, no empty bullets", "No expenses were logged in September 2026." in z and "• Wedding" not in z)
check("no comparison when last month was 0", "up" not in se.format_recap(variant(spending={"previousTotal": 0, "changePct": None})).splitlines()[3])
nm = se.format_recap(variant(money={"cash": None, "invested": None, "cardOwed": None, "gain": None}))
check("no savings figures, no Savings header", "Savings" not in nm)
check("a different year keeps the year", "on December 2025 (" in se.format_recap(
    variant(month="2026-01", label="January 2026", spending={"previousLabel": "December 2025"})))

# --- get_joint_accounts: kinds come from Split, investments are not banks
fake_recap = {"currency": "SGD", "money": {"accounts": [
    {"id": "b", "name": "Joint DBS", "kind": "bank", "amount": 7519.07, "lastCheckAmount": 8519.07, "lastChecked": "2026-10-09"},
    {"id": "c", "name": "OCBC", "kind": "card", "amount": 1886.72, "lastCheckAmount": None, "lastChecked": None},
    {"id": "i", "name": "IBKR", "kind": "investment", "amount": 23492.96, "lastCheckAmount": 23492.96, "lastChecked": "2026-10-12"},
]}}
with patch.object(se, "get_recap", return_value=fake_recap):
    ja = se.get_joint_accounts()
by = {a["name"]: a for a in ja["accounts"]}
check("investment stays kind investment (it was read as a bank)", by["IBKR"]["kind"] == "investment")
check("bank maps check-in and estimate", by["Joint DBS"]["last_balance"] == 8519.07 and by["Joint DBS"]["estimated_now"] == 7519.07)
check("card maps owed", by["OCBC"]["last_owed"] is None and by["OCBC"]["owed_now"] == 1886.72)
check("only one bank, so a balance is not ambiguous", sum(a["kind"] == "bank" for a in ja["accounts"]) == 1)

# --- get_expenses: counts by the SGT day / happenedOn, not the UTC day of createdAt
SGT = ZoneInfo("Asia/Singapore")
today = datetime(2026, 10, 13, 12, 0, tzinfo=SGT).date()
ms = lambda y, m, d, h, mi: int(datetime(y, m, d, h, mi, tzinfo=SGT).timestamp() * 1000)
group = {"name": "G", "currency": "SGD", "members": [{"id": "m1", "name": "Ansen"}], "accounts": [{"id": "a1", "name": "OCBC"}],
         "expenses": [
             {"description": "late night", "amount": 10, "category": "food", "paidBy": "m1", "createdAt": ms(2026, 10, 1, 1, 30)},
             {"description": "dated back", "amount": 20, "category": "food", "paidBy": "a1", "createdAt": ms(2026, 10, 12, 9, 0), "happenedOn": "2026-08-01"},
         ]}
with patch.object(se, "_fetch_group", return_value=group), patch.object(se, "local_today", return_value=today):
    ex = se.get_expenses(days=30)
days = {e["description"]: e["date"] for e in ex["expenses"]}
check("1:30am SGT on the 1st is the 1st, not 30 Sep (UTC)", days.get("late night") == "2026-10-01")
check("an expense dated into August is not in the last 30 days", "dated back" not in days and ex["total"] == 10)
with patch.object(se, "_fetch_group", return_value=group), patch.object(se, "local_today", return_value=today):
    ex2 = se.get_expenses(days=90)
check("a card-paid expense names the card, not an id", {e["description"]: e["paid_by"] for e in ex2["expenses"]}.get("dated back") == "OCBC")

# --- the monthly job: one message to both, recap first, then the ask; failure alerts Ansen
import main  # noqa: E402

sent = []


class Bot:
    async def send_message(self, chat_id, text, **kw):
        sent.append((chat_id, text))


ctx = MagicMock()
ctx.bot = Bot()
with patch.object(main, "ALLOWED_IDS", [1, 2]), patch.object(main, "ANSEN_ID", 1), \
        patch.object(se, "get_recap", return_value=BASE), patch.object(se, "get_joint_accounts", return_value={
            "currency": "SGD", "accounts": [
                {"name": "Joint DBS", "kind": "bank", "last_balance": 8519.07, "last_checked": "2026-10-09", "estimated_now": 7519.07},
                {"name": "OCBC", "kind": "card", "last_owed": None, "last_checked": None, "owed_now": 1886.72},
                {"name": "IBKR", "kind": "investment", "last_balance": 1.0, "last_checked": "2026-10-12", "estimated_now": 1.0}]}), \
        patch.object(main, "_thread_into_history"):
    asyncio.run(main.send_finance_checkin(ctx))
check("both of you get it, one message each", sorted(c for c, _ in sent) == [1, 2])
body = sent[0][1] if sent else ""
check("recap comes before the check-in", 0 <= body.find("recap") < body.find("Monthly money check-in"))
check("check-in dates are human (9 Oct), not ISO", "on 9 Oct" in body and "2026-10-09" not in body)
check("the ask names the bank and the card, never an investment", "the Joint DBS balance" in body and "IBKR" not in body)

sent.clear()
with patch.object(main, "ALLOWED_IDS", [1, 2]), patch.object(main, "ANSEN_ID", 1), \
        patch.object(se, "get_recap", side_effect=RuntimeError("404 recap")):
    asyncio.run(main.send_finance_checkin(ctx))
check("when Split fails, only Ansen is told, and it says why",
      len(sent) == 1 and sent[0][0] == 1 and "recap failed" in sent[0][1] and "404" in sent[0][1])

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
raise SystemExit(1 if fails else 0)
