"""Regression test for the supersession guard.

The vault eroded silently for months because nothing checked that a merge kept
what it replaced. These cases are the real ones from the Aug 2026 audit — if a
future change to fact_specifics/supersession_is_safe breaks them, the erosion
comes back and nothing else will notice.

    python test_supersession_guard.py
"""
from tools.user_memory import fact_specifics, supersession_is_safe

CASES = [
    # (old fact, new fact, expected_safe, what is at stake)
    ("A 50% deposit of SGD $940 was paid to Solulu Pte. Ltd. (APT bar vendor) on "
     "7 May 2026, balance due 31 October 2026.",
     "Happen bar deposit is paid.",
     False, "vendor name + balance due date"),

    ("Emily DJ is confirmed with no upfront deposit required, payment due after the "
     "event, at a rate of $350/hour for a 1.5-hour set (approximately $525 total).",
     "Emily DJ confirmed for a 1:00-3:00 AM set at the Happen after-party on 7 Nov 2026.",
     False, "payment terms — the reason not to chase her in August"),

    ("Videographer Kayue is confirmed to be handling lighting on the wedding night "
     "and therefore cannot also handle video.",
     "Kayue is handling lighting on the wedding night.",
     False, "the constraint that rules out video"),

    ("Shared Gmail for Lucille: lucillemg02@gmail.com; phone 80301392",
     "Jess's main phone number in Singapore: +6580301392",
     False, "an unrelated fact must never retire this one"),

    # Legitimate merges must still go through, or the vault fills with duplicates.
    ("Jess is taking Ritual Prenatal Vitamins (Folate), ordered Amazon.sg S$96.99",
     "Jess takes Ritual Prenatal Vitamins (Folate), ordered via Amazon.sg (~S$97 per order).",
     True, "a rounded value is still a value"),

    ("Happen drinks 50% deposit $940 paid.",
     "Happen drinks deposit now fully paid: $1,880 total.",
     True, "50% legitimately becomes fully paid"),

    ("Guest room block at The Singapore Edition: 6 rooms confirmed, guests paying individually.",
     "The Singapore EDITION room block for FYSH Wedding (7 Nov 2026): 6 rooms confirmed, "
     "guests book individually.",
     True, "a strictly more complete restatement"),
]


def main() -> int:
    failures = 0
    for old, new, want, stake in CASES:
        got, why = supersession_is_safe(old, new)
        ok = got == want
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {'allow' if got else 'block'} "
              f"(want {'allow' if want else 'block'}) — {stake}")
        if not ok:
            print(f"       reason given: {why}")

    # the terms extractor is what catches Emily; assert it directly
    terms = fact_specifics("no upfront deposit required, payment due after the event")["terms"]
    if not terms:
        print("FAIL governing terms not extracted")
        failures += 1
    else:
        print(f"ok   terms extracted: {sorted(terms)}")

    print(f"\n{len(CASES) + 1 - failures}/{len(CASES) + 1} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
