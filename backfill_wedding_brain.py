"""One-off: promote the pre-vault wedding archive into the shared brain.

The weekly knowledge sweep only ever reads the last 20 drops, so it started
capturing wedding facts in mid-June 2026 and never looked back. Everything from
April and May — the day-of running order, the food stations, the DJ and lighting
plan, the solemnisation agenda — stayed in wedding_drops and never became a
fact. Unprompted surfaces (morning brief, /plan, priority_brief) read the vault
rather than searching the drops, so they kept reporting the wedding as a blank
page.

This closes that hole once. It is not a replacement for the sweep, which stays a
delta job.

    python backfill_wedding_brain.py              # dry run, prints proposals
    python backfill_wedding_brain.py --apply      # writes approved facts
    python backfill_wedding_brain.py --before 2026-06-01 --apply

Idempotent enough to re-run: every proposal is checked against the existing
active vault before writing, and anything close to a fact already in there is
skipped rather than duplicated.
"""
import argparse
import asyncio
import json
import os
import re
import sys

from dotenv import load_dotenv

load_dotenv()

from anthropic import AsyncAnthropic
from tools.db import get_client
from tools.user_memory import add_brain_entry, get_active_entries

MODEL = os.getenv("BACKFILL_MODEL", "claude-sonnet-5")
BATCH = 8           # drops per extraction call — small enough to stay precise
MIN_CONTENT = 60    # below this a drop is chatter ("hi", "yes", "Log this")

# A single extraction pass is unreliable at this scale: two runs over the same
# 70 drops produced 21 facts and 9 facts with barely any overlap. Neither was
# wrong — each just noticed different things. Passes are unioned and deduped, so
# coverage is the union rather than the luck of one run.
PASSES = 3

EXTRACT_PROMPT = """You are rebuilding a couple's wedding knowledge base from their raw planning archive.

Ansen and Jessica are marrying on Saturday 7 November 2026 at FYSH, The Singapore EDITION (38 Cuscaden Rd). The evening after-party is at a venue called Happen. Below are raw drops they sent their assistant between April and May 2026 — typed notes, forwarded messages, and AI-generated summaries of screenshots.

Extract the DURABLE FACTS worth remembering months later. A good fact is specific, self-contained, and still true today.

EXTRACT:
- Schedule, running order and agendas — the single highest-value thing here. Capture the actual times and sequence, in full. A running order labelled "tentative", "draft" or "proposed" IS the current plan of record: extract it and say it's tentative. Do NOT discard it as provisional.
- Menu, food stations, drinks — including shortlists and "what we discussed", written as the options on the table
- Confirmed vendors with names, contacts and prices
- Music, DJ, lighting, entertainment decisions
- Attire, florals, decor decisions
- Guest logistics: room blocks, counts, dress code, RSVP facts, dietary needs
- Decisions they explicitly rejected (valuable — stops them relitigating)

For an agenda or timetable, write ONE fact holding the whole sequence rather than one fact per line — a running order split across ten rows is useless.

DO NOT EXTRACT:
- Questions they asked the assistant ("can you build a timeline?")
- The assistant's own unprompted suggestions or proposals
- Pure speculation with nothing decided ("maybe we do a photo booth?")
- Chit-chat, acknowledgements, duplicate restatements
- Individual line-item expenses (the budget tracker already holds those)

Write each fact as one plain sentence that stands alone with no pronouns pointing outside itself. Include the date it was recorded as fact_date (YYYY-MM-DD, given with each drop).

Return ONLY a JSON array, no prose:
[{{"fact": "...", "fact_date": "YYYY-MM-DD", "confidence": "high|medium"}}]

Return [] if this batch holds nothing durable.

ALREADY IN THE VAULT — do not re-propose these or anything that merely restates them:
{existing}

DROPS:
{drops}"""


async def _call(client: AsyncAnthropic, prompt: str, max_tokens: int, attempts: int = 5) -> str:
    """One message, with backoff — 529s are routine at this fan-out and losing a
    whole batch to one is how coverage silently degrades."""
    for i in range(attempts):
        try:
            resp = await client.messages.create(
                model=MODEL, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        except Exception as e:
            if i == attempts - 1:
                print(f"  ! giving up after {attempts} attempts: {type(e).__name__}")
                return ""
            await asyncio.sleep(2 ** i * 3)
    return ""


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]{4,}", (text or "").lower())}


def _too_similar(fact: str, known: list[str], threshold: float = 0.7) -> str | None:
    """Return the colliding known fact, if this proposal is a restatement.

    Containment, not Jaccard: three passes over the same drop produce the same
    fact at three levels of detail, and Jaccard scores those as *different*
    because the long version carries extra words. Asking 'is the shorter one
    essentially inside the longer one' catches what union-overlap misses."""
    f = _words(fact)
    if not f:
        return None
    for k in known:
        kw = _words(k)
        if not kw:
            continue
        if len(f & kw) / max(min(len(f), len(kw)), 1) >= threshold:
            return k
    return None


VERIFY_PROMPT = """You are the gate on Ansen and Jessica's shared brain. Today is {today}. Their wedding is Saturday 7 November 2026.

Below are candidate facts extracted from their April–May 2026 wedding archive, numbered. Decide which earn a permanent place in the brain.

KEEP a fact if it is still useful to know today: confirmed vendors, prices, deposits paid, the running order, menu and drinks, lighting and music plans, attire, allergies, decisions made, decisions rejected.

DROP a fact if:
- It is a point-in-time snapshot that a later fact has overtaken — "as of 5 May, 11 guests need rooms" is dead once the room block is confirmed. Keep the latest snapshot only if no confirmed outcome exists.
- It is a deadline that has already passed with no lasting consequence ("confirm with Elenna before 14 May 2026").
- It restates another fact in the list. When two say the same thing, keep the index of the more detailed one and drop the other.
- It is trivially small, or an artefact of the assistant rather than the couple.

MERGE: if several facts are fragments of one coherent thing (a running order split across entries, a vendor quote split from its payment terms), keep the single best index and drop the fragments.

List only what to DROP — everything unlisted is kept. Keep reasons to three words.

Return ONLY a JSON object, no prose:
{{"drop": {{"1": "restates 0", "2": "superseded by 5"}}}}

CANDIDATES:
{candidates}"""


async def verify(client: AsyncAnthropic, facts: list[dict]) -> tuple[set, dict]:
    """Second opinion on the whole approved set at once — the only stage that
    can see a fact is stale, because staleness is a relationship between facts."""
    from datetime import date as _date
    listing = "\n".join(
        f'[{i}] ({f.get("fact_date")}) {f["fact"]}' for i, f in enumerate(facts)
    )
    text = await _call(client, VERIFY_PROMPT.format(
        today=_date.today().isoformat(), candidates=listing), 16000)
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        # Fail open here, not closed: this is a dry-run-then-approve flow and a
        # human reads the list before anything is written.
        print(f"  ! verifier returned no JSON: {text[:300]!r}")
        return set(range(len(facts))), {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        print("  ! verifier JSON malformed — keeping everything for manual review")
        return set(range(len(facts))), {}
    dropped = {int(k): v for k, v in (parsed.get("drop") or {}).items()}
    keep = {i for i in range(len(facts)) if i not in dropped}
    return keep, dropped


def load_drops(before: str) -> list[dict]:
    rows = (
        get_client().table("wedding_drops")
        .select("ts,category,content")
        .lt("ts", before)
        .order("ts")
        .execute()
        .data or []
    )
    return [r for r in rows if len((r.get("content") or "").strip()) >= MIN_CONTENT]


async def extract(client: AsyncAnthropic, batch: list[dict], existing: list[str]) -> list[dict]:
    drops_text = "\n\n".join(
        f'[{(d.get("ts") or "")[:10]}] ({d.get("category") or "uncategorised"}) '
        f'{" ".join((d.get("content") or "").split())[:3500]}'
        for d in batch
    )
    prompt = EXTRACT_PROMPT.format(
        existing="\n".join(f"- {e}" for e in existing) or "(nothing yet)",
        drops=drops_text,
    )
    text = await _call(client, prompt, 8000)
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return []
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return []


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to brain_entries (default: dry run)")
    ap.add_argument("--before", default="2026-06-01", help="only drops before this date")
    ap.add_argument("--min-confidence", default="medium", choices=["high", "medium"])
    ap.add_argument("--passes", type=int, default=PASSES,
                    help="extraction passes to union — one pass misses a lot")
    ap.add_argument("--from-cache", action="store_true",
                    help="reuse the last run's raw proposals (re-tune dedupe without re-extracting)")
    args = ap.parse_args()

    drops = load_drops(args.before)
    if not drops:
        print(f"No substantive drops before {args.before}.")
        return 0

    known = [e["fact"] for e in get_active_entries()]
    wedding_known = [e["fact"] for e in get_active_entries(domain="wedding")]
    print(f"{len(drops)} substantive drops before {args.before}")
    print(f"{len(known)} active vault facts ({len(wedding_known)} wedding) to check against\n")

    cache_path = f".backfill_cache_{args.before}.json"
    if args.from_cache and os.path.exists(cache_path):
        with open(cache_path) as fh:
            results = json.load(fh)
        print(f"loaded {sum(len(r) for r in results)} raw proposals from {cache_path}")
    else:
        client = AsyncAnthropic()
        batches = [drops[i:i + BATCH] for i in range(0, len(drops), BATCH)]
        results = []
        for p in range(args.passes):
            print(f"pass {p + 1}/{args.passes} — {len(batches)} batches…")
            results += await asyncio.gather(*(extract(client, b, wedding_known) for b in batches))
        with open(cache_path, "w") as fh:
            json.dump(results, fh)
        print(f"cached raw proposals to {cache_path}")

    # Longest first: when passes describe the same fact at different levels of
    # detail, the fullest version should be the one that survives dedupe.
    flat = [p for proposals in results for p in proposals if (p.get("fact") or "").strip()]
    flat.sort(key=lambda p: len(p.get("fact") or ""), reverse=True)

    approved, skipped = [], []
    running = list(known)
    for p in flat:
        fact = p["fact"].strip()
        if args.min_confidence == "high" and p.get("confidence") != "high":
            skipped.append((fact, "low confidence"))
            continue
        clash = _too_similar(fact, running)
        if clash:
            skipped.append((fact, f"restates: {clash[:60]}"))
            continue
        running.append(fact)
        approved.append(p)

    if approved:
        print(f"\nverifying {len(approved)} candidates…")
        client = locals().get("client") or AsyncAnthropic()
        keep, dropped = await verify(client, approved)
        for i, p in enumerate(approved):
            if i not in keep:
                skipped.append((p["fact"], dropped.get(i, "verifier dropped")))
        approved = [p for i, p in enumerate(approved) if i in keep]

    print(f"\n=== {len(approved)} NEW FACTS ===")
    for p in sorted(approved, key=lambda x: x.get("fact_date") or ""):
        print(f'  {p.get("fact_date")}  [{p.get("confidence")}]  {p["fact"]}')
    print(f"\n=== {len(skipped)} skipped ===")
    for fact, why in skipped[:15]:
        print(f"  - {fact[:70]} — {why}")
    if len(skipped) > 15:
        print(f"  … and {len(skipped) - 15} more")

    if not args.apply:
        print("\n(dry run — pass --apply to write)")
        return 0

    written = 0
    for p in approved:
        try:
            add_brain_entry(p["fact"], domain="wedding", source="backfill:wedding_drops",
                            fact_date=p.get("fact_date"), kind="fact")
            written += 1
        except Exception as e:
            print(f"  FAILED: {p['fact'][:60]} — {type(e).__name__}: {e}")
    print(f"\nWrote {written} wedding facts to the vault.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
