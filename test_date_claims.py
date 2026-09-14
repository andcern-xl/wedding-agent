"""Regression test: the agent must not call tomorrow "tonight".

On Mon 14 Sep 2026 the proactive brief led with "Tonight: Sanwraps call at 8pm."
The call was Tue 15 Sep. Nothing was wrong with the data — the thread said
"Confirmed 10-min call Tuesday 15 Sep at 8pm" — and nothing was wrong with the
timezone: date_block() correctly said today was Monday the 14th.

The rule forbade writing "tomorrow" without checking the table, but said nothing
about "tonight", "today" or "this evening" — the words that assert an event is
TODAY were the ones left unguarded.

    python test_date_claims.py
"""
from datetime import date

from dotenv import load_dotenv

load_dotenv("/Users/ansen/wedding-agent/.env")

import agent as A  # noqa: E402

TODAY = date(2026, 9, 14)   # the Monday it happened

ANNOTATION = [
    ("Confirmed 10-min call Tuesday 15 Sep at 8pm", "TOMORROW", "the Sanwraps call itself"),
    ("Vet appointment booked Tuesday 8 Sep 2026 at 11:30 AM", "6 DAYS AGO", "an explicit past year"),
    ("family trip 20 May 2027 to 20 Aug 2027", "in 248 days", "a year away, year written"),
    ("wedding 2026-11-07 at FYSH", "in 54 days", "ISO form"),
    ("Sept 15 at 8pm", "TOMORROW", "month-first, abbreviated"),
]

CLAIMS = [
    # (output, dates present in context, should_flag, what it is)
    ("<b>Tonight: Sanwraps call at 8pm.</b>", {"2026-09-15"}, True,
     "the real failure — tomorrow's call called tonight"),
    ("<b>Tonight: Sanwraps call at 8pm.</b>", {"2026-09-14"}, False,
     "same words, but something really is today"),
    ("Nothing due today, all clear.", {"2026-09-15"}, False,
     "benign prose must not trip it"),
    ("• Today: dentist at 3pm", {"2026-09-16"}, True,
     "bulleted header form"),
    ("Her appointment is this evening, so pack tonight.", {"2026-09-15"}, False,
     "mid-sentence use is out of scope, not a header claim"),
]


def main() -> int:
    failures = 0

    for text, expected, label in ANNOTATION:
        got = A.annotate_dates(text, TODAY)
        ok = expected in got
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} annotate — {label}")
        if not ok:
            print(f"       wanted {expected!r} in: {got}")

    for output, ctx, should_flag, label in CLAIMS:
        flagged = bool(A.today_claim_violations(output, ctx, TODAY))
        ok = flagged == should_flag
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {'flags' if flagged else 'quiet':5} "
              f"(want {'flags' if should_flag else 'quiet':5}) — {label}")

    # The rule text itself must keep naming the today-words.
    rule = A.date_block()
    for word in ("tonight", "today", "this evening"):
        ok = word in rule.lower()
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} rule still forbids {word!r} without the table")

    total = len(ANNOTATION) + len(CLAIMS) + 3
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
