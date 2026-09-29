"""Regression tests from the 27 Sep 2026 brief (Ansen's screenshot, 29 Sep).

The 9am brief read:

    Nothing critical in threads or the brain for today's brief. Writing now.

    Home day. SQ611 ICN → SIN at 11:05 — get to the airport by 9am. ...

Two bugs:

1. The Seoul trip had been cancelled on 24 Sep, but "cancel Seoul" matched two
   rows — the live one and the row merged into it on 5 Aug — and update_trip
   took matches[0], the retired row. The live row stayed "booked".
2. The model's own narration led the message, though the prompt forbids it.

    python test_brief_and_trips.py
"""
from unittest import mock

from dotenv import load_dotenv

load_dotenv("/Users/ansen/wedding-agent/.env")

import agent as A  # noqa: E402
from tools import trips as T  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + name)
    fails += 0 if ok else 1


# ── 1. preamble strip ──────────────────────────────────────────────────────
real = ("Nothing critical in threads or the brain for today's brief. Writing now.\n\n"
        "Home day. <b>SQ611 ICN → SIN at 11:05</b> — get to the airport by 9am.\n\n"
        "• Poker at <b>2pm</b> once you land and settle.")
out = A.strip_preamble(real)
check("real failure: narration removed", out.startswith("Home day."))
check("real failure: content intact", "Poker at" in out and "SQ611" in out)

for label, text in [
    ("ordinary opening kept", "Home day.\n\n• Poker at 2pm."),
    ("single paragraph never stripped", "Nothing new in the brain today."),
    ("'here's the plan' is content, kept", "Here's the plan for Jess's birthday: dinner at 7.\n\n• Cake from Rice."),
    ("'nothing due today' kept", "Nothing due today.\n\n• Wedding: 39 days."),
    ("long first paragraph kept", "Let me " + "x" * 300 + "\n\nrest"),
]:
    check(label, A.strip_preamble(text) == text.strip())

for label, pre in [
    ("'Let me pull this together.'", "Let me pull this together."),
    ("'Here's your morning brief:'", "Here's your morning brief:"),
    ("'The vault came back empty…'", "The vault came back empty on Jess's dinner."),
    ("'I have enough — writing it now.'", "I have enough — writing it now."),
]:
    check(f"strips {label}", A.strip_preamble(pre + "\n\nHome day.") == "Home day.")

# ── 2. trips follow merges ────────────────────────────────────────────────
LIVE, RETIRED = "fb89b742-dd18-4873-a934-7d85427701be", "6efe9948-407b-4066-ba92-08c5cb195a78"
rows = {
    RETIRED: {"id": RETIRED, "destination": "Seoul", "status": "merged",
              "start_date": "2026-09-23", "end_date": "2026-09-27",
              "notes": f"SQ611\n[merged into {LIVE} — its details now live there]"},
    LIVE: {"id": LIVE, "destination": "Seoul", "status": "booked",
           "start_date": "2026-09-23", "end_date": "2026-09-27", "notes": "Moxy"},
    "old": {"id": "old", "destination": "Seoul", "status": "completed",
            "start_date": "2025-04-01", "end_date": "2025-04-05", "notes": ""},
}
writes = []


class _Q:
    def __init__(self):
        self._id = None
        self._upd = None

    def select(self, *_):
        return self

    def order(self, *_a, **_k):
        return self

    def eq(self, _col, v):
        self._id = v
        return self

    def update(self, u):
        self._upd = u
        return self

    def execute(self):
        class R:
            pass
        r = R()
        if self._upd is not None:
            writes.append((self._id, self._upd))
            rows[self._id].update(self._upd)
            r.data = [rows[self._id]]
        elif self._id:
            r.data = [rows[self._id]] if self._id in rows else []
        else:
            r.data = sorted(rows.values(), key=lambda t: t["start_date"])
        return r


class _C:
    def table(self, _):
        return _Q()


import datetime as _dt  # noqa: E402

with mock.patch.object(T, "get_client", lambda: _C()), \
        mock.patch.object(T, "local_today", lambda: _dt.date(2026, 9, 24)):
    m = T.find_trips_by_destination("Seoul")
    check("retired row resolves to the live one (no duplicate)", [t["id"] for t in m].count(LIVE) == 1)
    check("retired row never returned", all(t["id"] != RETIRED for t in m))
    check("live trip ranks first, past Seoul trip last", m[0]["id"] == LIVE and m[-1]["id"] == "old")

    # Even addressed directly, a write to the retired row lands on the live one.
    T.update_trip(RETIRED, status="cancelled")
    check("cancel on retired id lands on the live row", writes[-1][0] == LIVE and rows[LIVE]["status"] == "cancelled")
    check("retired row keeps its merged status", rows[RETIRED]["status"] == "merged")

    # The pointer is the truth even when the status was already clobbered.
    rows[RETIRED]["status"] = "cancelled"
    rows[LIVE]["status"] = "booked"
    check("clobbered status still resolves via the pointer",
          T.canonical_trip(rows[RETIRED])["id"] == LIVE)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
raise SystemExit(1 if fails else 0)
