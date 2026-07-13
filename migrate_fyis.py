"""One-off: drain all active FYIs into the brain — durable ones become facts,
ambiguous ones become dated episodes, dead ones archive. After this the fyis
table is legacy (kept read-only one release as the rollback path).

Dry-run by default:  python migrate_fyis.py
Write:               python migrate_fyis.py --apply
"""
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv("/Users/ansen/wedding-agent/.env")

from tools.fyis import get_fyis, promote_fyi, archive_fyi          # noqa: E402
from tools.user_memory import add_brain_entry, normalize_domain    # noqa: E402
from agent import UnifiedAgent                                     # noqa: E402


async def main():
    apply = "--apply" in sys.argv
    fyis = get_fyis(limit=100)
    if not fyis:
        print("No active FYIs — nothing to migrate.")
        return
    agent = UnifiedAgent()
    triage = await agent.triage_expiring_fyis(fyis)

    for f in triage["promote"]:
        fact = f.get("_fact") or f["content"]
        print(f"FACT     → {fact[:110]}")
        if apply:
            row = promote_fyi(f["id"])
            if row:
                add_brain_entry(fact, normalize_domain(f.get("_domain")), "fyi_migration")

    for f in triage.get("episode", []) + triage["ask"]:
        # full drain: still-relevant and ambiguous items become dated episodes, not cards
        when = (f.get("created_at") or "")[:10] or None
        print(f"EPISODE  → ({when}) {f['content'][:100]}")
        if apply:
            add_brain_entry(f["content"], normalize_domain(f.get("category")), "fyi_migration", when, "episode")
            archive_fyi(f["id"])

    for f in triage["archive"]:
        print(f"ARCHIVE  → {f['content'][:100]}")
        if apply:
            archive_fyi(f["id"])

    mode = "APPLIED" if apply else "DRY RUN — rerun with --apply to write"
    n_eps = len(triage.get("episode", [])) + len(triage["ask"])
    print(f"\n{mode}: {len(triage['promote'])} facts, {n_eps} episodes, {len(triage['archive'])} archived")


if __name__ == "__main__":
    asyncio.run(main())
