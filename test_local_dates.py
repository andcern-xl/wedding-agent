"""Every date the bot shows is an SGT date, never the server's UTC date.

1 Oct 2026 — Ansen: "the time zone is still always wrong ... it is all SGT".
Railway runs UTC, and `created_at[:10]` on a timestamptz is its UTC date, so
anything logged between 00:00 and 08:00 SGT was dated the day before. This
checks the helper and that the UTC slice and server-clock dates are gone.

    python test_local_dates.py
"""
import pathlib
import re

from tools.tz import local_date_of

fails = 0


def check(label, ok):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


check("1:58am SGT is that SGT day, not the UTC day before",
      local_date_of("2026-09-30T17:58:55.07+00:00") == "2026-10-01")
check("7:59am SGT still the same SGT day", local_date_of("2026-09-30T23:59:00Z") == "2026-10-01")
check("8am SGT", local_date_of("2026-10-01T00:00:00+00:00") == "2026-10-01")
check("an SGT-offset timestamp keeps its day", local_date_of("2026-10-01T23:30:00+08:00") == "2026-10-01")
check("a bare date passes through", local_date_of("2026-10-01") == "2026-10-01")
check("empty stays empty", local_date_of(None) == "" and local_date_of("") == "")

slice_pat = re.compile(r'''get\(["'](created_at|updated_at|completed_at|received_at|modifiedTime|ts)["']\) or ["']["']\)\[:10\]''')
clock_pat = re.compile(r'(?<![\w.])(date|ddate|_date)\.today\(\)|datetime\.utcnow\(\)')
for f in ["main.py", "agent.py", "webhook.py", *map(str, pathlib.Path("tools").glob("*.py"))]:
    if f.endswith("tz.py"):
        continue
    code = "\n".join(l for l in pathlib.Path(f).read_text().splitlines() if not l.strip().startswith("#"))
    check(f"{f}: no UTC date slice of a timestamp", not slice_pat.search(code))
    check(f"{f}: no server-clock date", not clock_pat.search(code))

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
raise SystemExit(1 if fails else 0)
