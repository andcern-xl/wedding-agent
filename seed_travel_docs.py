"""Seed travel_docs from what is already known.

The details were sitting as prose inside user_summaries(user_id=63756531),
where trip_milestone_brief could not see them. This moves them into the store
the reminders actually read.

One correction made in the process: A06688257 is JESS's US passport. It lived in
ANSEN's summary, which is how it came to be recorded as his in an earlier note.
Ansen's own Singapore passport number is nowhere in the database — the only
related fact is "As of July 2026, Ansen still needs to apply for his Singapore
passport" — so it goes in as status='missing' rather than being invented. That
way the reminder asks for it instead of quietly leaving him out.

Requires supabase_travel_docs.sql.

    python seed_travel_docs.py            # dry run
    python seed_travel_docs.py --apply
"""
import sys

from dotenv import load_dotenv

load_dotenv("/Users/ansen/wedding-agent/.env")

from tools.travel_docs import get_docs, render_for_reminder, upsert_doc  # noqa: E402

DOCS = [
    # Jess — from user_summaries(63756531) and the vault
    dict(person="jess", doc_type="passport", nationality="United States",
         number="A06688257", expires="2032-06-17",
         notes="US citizen, DOB 6 Nov 1990"),
    dict(person="jess", doc_type="known_traveler", number="160657668",
         notes="TSA Known Traveler Number"),
    dict(person="jess", doc_type="pass", nationality="Singapore",
         number="M4540704N", issued="2026-03-13", expires="2029-03-13",
         notes="LTVP (FIN). PR application in progress — reference ISC2607SS004960, "
               "submitted 12 Jul 2026"),

    # Ansen. He was right that he had shared this — K5529076A was in his
    # user_summaries row all along. My first search reported it missing because
    # it truncated the output and capped the hits; the number was there.
    # Expiry is genuinely not recorded anywhere, and the six-month check needs
    # it, so it stays blank until he provides it.
    dict(person="ansen", doc_type="passport", nationality="Singapore",
         number="K5529076A",
         notes="Expiry NOT on file — needed for the six-month validity check. "
               "UK ETA approved 20 Jul 2026, ref 2020-0000-5698-8925, valid to 12 Jul 2028."),
    dict(person="ansen", doc_type="nric", nationality="Singapore",
         number="S9219342Z", notes="Singapore Pink IC"),
]


def main() -> int:
    apply = "--apply" in sys.argv
    try:
        existing = get_docs(include_inactive=True)
    except Exception as e:
        print(f"Could not read travel_docs: {e}")
        print("Run supabase_travel_docs.sql first.")
        return 1

    print(f"travel_docs currently holds {len(existing)} row(s)\n")
    for d in DOCS:
        label = f"{d['person']} / {d['doc_type']}"
        detail = d.get("number") or d.get("status", "")
        print(f"  {'writing' if apply else 'would write'}  {label:24} {detail}")
        if apply:
            if not upsert_doc(**d):
                print(f"      FAILED — has supabase_travel_docs.sql been run?")

    print()
    if apply:
        print("=== what a travel reminder will now include ===")
        print(render_for_reminder())
    else:
        print("Dry run. Re-run with --apply once supabase_travel_docs.sql has been run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
