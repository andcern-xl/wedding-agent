"""Recall-coverage sweep — proves no information is stranded.

The 'my DJ plans are gone' bug was not a bad diff; it was an emergent gap —
recall narrowed to brain_entries over many commits while wedding_drops stayed a
silo nothing read. A diff-review can't see that. This can.

This is now a thin wrapper over `self_audit.check_reachability`, which asserts
the same invariant with one fix: it samples using the RAREST words in a row
rather than the first three long ones. The old picker gave
['planning','message','music'] for a screenshot headed "Wedding Planning Message
— DJ/Music Details", which matches everything and proves nothing — it reported
wedding_drops as stranded for weeks while real queries resolved fine.

Kept as its own entry point because the QA checklist and muscle memory both
point here. For all five memory invariants, run `python self_audit.py`.

    python sweep_recall.py
Exits non-zero if any store with data is unreachable from query_brain.
"""
import sys

from dotenv import load_dotenv

load_dotenv("/Users/ansen/wedding-agent/.env")

from self_audit import check_reachability  # noqa: E402


def main() -> int:
    print("=== RECALL COVERAGE SWEEP ===")
    r = check_reachability()
    for note in r.notes:
        print(f"  ✅ {note}")
    for f in r.failures:
        print(f"  ❌ {f}")
    print()
    print("  ℹ️  user_summaries reach the CHAT via injection, not query_brain "
          "(passport/PII path). Not covered by this sweep — verify in chat if changed.")
    print()
    if r.ok:
        print("✅ PASS — every store with data is reachable from query_brain")
        return 0
    print("❌ FAIL — a store with data is stranded; wire it into _query_brain_sync")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
