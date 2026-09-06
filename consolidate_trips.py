"""One-time trip consolidation — merge rows that describe the same journey.

add_trip was a bare insert with no dedup, so a trip logged twice became two
rows. That is not just cosmetic: the Seoul trip had its flights in one row and
its hotel in the other, so neither could answer a question about the trip on its
own, and the travel view listed Seoul twice.

It also had no date validation, which is how the Bangkok bachelor trip vanished:
stored 2026-12-10 to 2026-09-04 for flights that fly 10-13 Sep, so it sorted
into December and never appeared as "next week".

    python consolidate_trips.py            # dry run
    python consolidate_trips.py --apply
"""
import re
import sys

from dotenv import load_dotenv

load_dotenv("/Users/ansen/wedding-agent/.env")

from tools.db import get_client                                    # noqa: E402
from tools.trips import find_duplicate_trips, merge_trips          # noqa: E402

# "SQ720 depart SIN 10 Sep" / "SQ721 depart BKK 13 Sep"
_DATE_IN_NOTE = re.compile(
    r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def dates_from_notes(notes: str, year: int = 2026) -> tuple[str | None, str | None]:
    """Earliest and latest date mentioned in the notes — the flights are the
    most reliable record of when a trip actually is."""
    found = []
    for day, mon in _DATE_IN_NOTE.findall(notes or ""):
        m = _MONTHS.get(mon[:3].lower())
        if m:
            try:
                found.append(f"{year}-{m:02d}-{int(day):02d}")
            except ValueError:
                pass
    if not found:
        return None, None
    return min(found), max(found)


def main() -> int:
    apply = "--apply" in sys.argv
    c = get_client()

    pairs = find_duplicate_trips()
    print(f"=== DUPLICATE TRIPS: {len(pairs)} pair(s) ===\n")
    for a, b in pairs:
        # Keep the row with more detail; the other folds into it.
        keep, drop = (a, b) if len(a.get("notes") or "") >= len(b.get("notes") or "") else (b, a)
        print(f"  {keep.get('destination')}  {keep.get('start_date')} → {keep.get('end_date')}")
        print(f"     keep  {keep['id'][:8]}  {len(keep.get('notes') or '')} chars  "
              f"visibility={keep.get('visibility')}")
        print(f"     fold  {drop['id'][:8]}  {len(drop.get('notes') or '')} chars  "
              f"visibility={drop.get('visibility')}")
        if apply:
            ok = merge_trips(keep["id"], drop["id"])
            print(f"     -> {'merged' if ok else 'MERGE FAILED'}")
        print()

    # Impossible date ranges. The notes carry the truth; the columns do not.
    print("=== IMPOSSIBLE DATE RANGES ===\n")
    broken = 0
    for t in c.table("trips").select("*").execute().data or []:
        s, e = t.get("start_date"), t.get("end_date")
        if not (s and e and e < s):
            continue
        broken += 1
        ns, ne = dates_from_notes(t.get("notes") or "")
        print(f"  {t.get('destination')}  stored {s} → {e}  (status={t.get('status')})")
        print(f"     notes say: {ns} → {ne}")
        legs = set(re.findall(r"\b[A-Z]{2}\s?\d{2,4}\b", t.get("notes") or ""))
        multi = len(legs) > 2 or "cancelled" in (t.get("notes") or "").lower()
        if multi:
            # min/max across the notes is meaningless when the notes describe
            # more than one journey. This row holds a bachelor trip, a separate
            # 31 Aug trip and a cancellation about a babymoon.
            print(f"     -> NOT correcting: the notes describe more than one trip "
                  f"({len(legs)} flight numbers: {', '.join(sorted(legs))}). Needs splitting by hand.")
        elif ns and ne and ne >= ns:
            if apply:
                c.table("trips").update({"start_date": ns, "end_date": ne}).eq("id", t["id"]).execute()
                print("     -> dates corrected from the notes")
            else:
                print("     -> would correct the dates from the notes")
        else:
            print("     -> cannot infer; needs a human")
        if t.get("status") == "cancelled":
            print("     NOTE: status is 'cancelled', so it stays hidden even once the dates "
                  "are right. Only you know whether that is correct — not changing it.")
        print()
    if not broken:
        print("  none\n")

    if not apply:
        print("Dry run. Re-run with --apply to make these changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
