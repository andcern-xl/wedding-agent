"""Recover facts that supersession destroyed.

The Aug 2026 audit found the vault had been eroding: 43 of 85 real retirements
dropped a specific, and 50 had no recorded replacement at all. Nothing was
deleted from the table — the rows are still there with status='superseded' —
so the loss is recoverable.

A row comes back only if BOTH hold:
  1. its information is genuinely absent from active memory, and
  2. it is still true today (a stale snapshot like "Pregnancy week count: Week 7"
     must stay retired — restoring it would be a new bug, not a fix).

    python restore_eroded.py            # dry run, prints the proposal
    python restore_eroded.py --apply    # reactivate the approved rows
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

load_dotenv("/Users/ansen/wedding-agent/.env")

from anthropic import Anthropic                                    # noqa: E402
from tools.db import get_client                                    # noqa: E402
from tools.user_memory import (                                    # noqa: E402
    _norm, fact_specifics, restore_entries, supersession_is_safe,
)

MODEL = "claude-sonnet-4-6"
client = Anthropic()

# Daily churn and expired questions were never durable facts — leave them retired.
# Facts held back by hand after review. A restore is a judgement call and this
# is where the judgement is recorded, rather than in a shell command nobody can
# find later.
HOLD = {
    "remains undecided": "Emily's venue/timing was settled after this was written "
                         "(1-3am at Happen) — restoring it would have the bot "
                         "re-open a closed question.",
}


def held_back(fact: str) -> str | None:
    for needle, why in HOLD.items():
        if needle in fact:
            return why
    return None


def is_noise(row: dict) -> bool:
    f = row.get("fact") or ""
    return (f.startswith("📊")
            or f.startswith("Unanswered check-in")
            or "Newsletter sentiment (unverified)" in f
            or "Latest stocks of interest" in f)


def missing_specifics(fact: str, active_blob_norm: str) -> list[str]:
    """Which of this fact's load-bearing details appear nowhere in active memory?

    Deterministic on purpose. An LLM asked "is this absent?" answers from the
    gist and gets it wrong: shown Emily's retired terms it said "present"
    because $525 appears in the budget tracker — missing that "payment due
    after the event", the part that actually governs behaviour, was gone."""
    sp = fact_specifics(fact)
    out = []
    for kind in ("entities", "terms", "money", "date"):
        for item in sp[kind]:
            if _norm(item) not in active_blob_norm:
                out.append(item)
    return out


# Words that show up as capitalised nouns in half the vault and prove nothing on
# their own. Restoring a fact because "NOTE" or "Personal" went missing is noise.
_WEAK = {"NOTE", "Personal", "Shared", "Citizen", "Application", "Reference",
         "Status", "Plan", "Package", "Account", "Scheme", "Step", "Leave",
         "Singapore", "Gmail", "Premier", "Preferred", "General", "Investing"}


def is_strong(item: str) -> bool:
    """A missing detail worth resurrecting a row for: a value, a code, a
    contact, or a real multi-word term — not one generic capitalised word."""
    if any(ch.isdigit() for ch in item) or "@" in item or "/" in item:
        return True
    if " " in item.strip():
        return True
    return item not in _WEAK and len(item) >= 4


def _words(t: str) -> set:
    # strip trailing punctuation so "2026." and "2026" count as one token
    return {w.strip(".,;:") for w in re.findall(r"[a-z0-9$.]{3,}", (t or "").lower())}


def dedupe(items):
    """Two rows saying the same thing were both retired; bring back one.

    Compared on word overlap, not prefix — "Selected DBS for joint account" and
    "DBS selected for joint account" are the same fact written twice, and a
    prefix check reads them as different."""
    kept = []
    for r, why, missing, v in sorted(items, key=lambda x: -len(x[0].get("fact", ""))):
        w = _words(r.get("fact", ""))
        dup = False
        for k in kept:
            kw = _words(k[0].get("fact", ""))
            if w and kw and len(w & kw) / len(w | kw) >= 0.5:
                dup = True
                break
        if not dup:
            kept.append((r, why, missing, v))
    return kept


def judge(fact: str, missing: list[str], today: str) -> dict:
    """The LLM decides one thing only: is this still true? Absence is measured,
    not guessed."""
    prompt = f"""Today is {today}. A couple's shared fact-memory retired this entry:

RETIRED FACT: {fact}

Is this STILL TRUE today, and worth having back in active memory?

Answer false ONLY when you can point to why it has moved on: a past pregnancy
week count, a dated snapshot balance, a booking for a date already past, or a
question rather than a fact.
Do NOT speculate that a process has since completed — "the PR application was
probably approved by now" is a guess, not evidence, and guessing here deletes a
real fact for a second time.
Answer true for durable facts — payment terms, rates, vendor names, contract
totals, constraints, preferences, specs, birthdays, contact details.

Reply ONLY JSON:
{{"still_true": true/false, "why": "<15 words"}}"""
    try:
        r = client.messages.create(model=MODEL, max_tokens=200,
                                   messages=[{"role": "user", "content": prompt}])
        m = re.search(r"\{.*\}", r.content[0].text, re.DOTALL)
        return json.loads(m.group()) if m else {}
    except Exception as e:
        return {"error": str(e)[:60]}


def main() -> int:
    apply = "--apply" in sys.argv
    c = get_client()
    rows = c.table("brain_entries").select("*").execute().data or []
    byid = {r["id"]: r for r in rows}
    active = [r for r in rows if r.get("status") == "active"]
    active_blob = "\n".join(f"- {r.get('fact','')}" for r in active)
    from tools.tz import local_today
    today = local_today().isoformat()

    # Candidates: retirements that either dropped a specific, or were never linked.
    cands = []
    for r in rows:
        if r.get("status") != "superseded" or is_noise(r):
            continue
        rep = byid.get(r.get("superseded_by"))
        if rep is None:
            cands.append((r, None, "no recorded replacement"))
        else:
            safe, why = supersession_is_safe(r.get("fact", ""), rep.get("fact", ""))
            if not safe:
                cands.append((r, rep, why))

    print(f"active={len(active)}  retired(real)={sum(1 for r in rows if r.get('status')=='superseded' and not is_noise(r))}")
    print(f"candidates for recovery: {len(cands)}\n")

    active_norm = _norm(active_blob)

    def work(item):
        r, rep, why = item
        missing = missing_specifics(r.get("fact", ""), active_norm)
        if rep is not None and not missing:
            # The replacement provably dropped something (that is why this is a
            # candidate); the pieces merely turning up scattered across other
            # facts does not reconstitute the fact.
            missing = [why.split(":", 1)[-1].strip()]
        if not missing:
            return r, rep, why, missing, {"still_true": False, "why": "already covered by active memory"}
        return r, rep, why, missing, judge(r.get("fact", ""), missing, today)

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(work, cands))

    restore, skip, held = [], [], []
    for r, rep, why, missing, v in results:
        strong = [m for m in missing if is_strong(m)]
        hb = held_back(r.get("fact", ""))
        if hb:
            held.append((r, hb))
        elif strong and v.get("still_true"):
            restore.append((r, why, strong, v))
        else:
            skip.append((r, missing, v))
    before = len(restore)
    restore = dedupe(restore)
    if before != len(restore):
        print(f"(deduped {before - len(restore)} near-identical row(s))")

    # Last gate. Bringing back a row that argues with current reality would
    # recreate the failure this is meant to fix — a vault that contradicts
    # itself is worse than one with a hole in it. "Whether Emily plays FYSH or
    # Happen remains undecided" is true of May and false of today.
    def contradicts(item):
        r = item[0]
        p = f"""Today is {today}. This fact is about to be restored to a couple's active memory:

CANDIDATE: {r.get('fact','')}

Their current ACTIVE memory says:
{active_blob[:12000]}

Does the candidate CONTRADICT anything active — assert as open/undecided/pending
something the active memory says is settled, or state a value the active memory
has since changed? Reply ONLY JSON: {{"contradicts": true/false, "why": "<12 words>"}}"""
        try:
            resp = client.messages.create(model=MODEL, max_tokens=150,
                                          messages=[{"role": "user", "content": p}])
            m = re.search(r"\{.*\}", resp.content[0].text, re.DOTALL)
            return json.loads(m.group()) if m else {}
        except Exception:
            return {}

    with ThreadPoolExecutor(max_workers=12) as ex:
        verdicts = list(ex.map(contradicts, restore))
    conflicted = [(it, v) for it, v in zip(restore, verdicts) if v.get("contradicts")]
    restore = [it for it, v in zip(restore, verdicts) if not v.get("contradicts")]
    if conflicted:
        print(f"(held back {len(conflicted)} that argue with active memory)")
        for (r, _, _, _), v in conflicted:
            print(f"    ✋ {r.get('fact','')[:80]} — {v.get('why','')[:44]}")
    print()

    if held:
        print(f"(held back by review: {len(held)})")
        for r, why in held:
            print(f"    ✋ {r.get('fact','')[:74]}\n       {why[:100]}")
        print()

    print(f"=== RESTORE: {len(restore)} ===\n")
    for r, why, missing, v in sorted(restore, key=lambda x: x[0].get("domain") or ""):
        print(f"[{r.get('domain')}] {r.get('fact','')[:118]}")
        print(f"    recovers: {', '.join(missing[:6])}\n")

    print(f"=== LEAVE RETIRED: {len(skip)} ===")
    for r, missing, v in skip[:14]:
        reason = "already in active memory" if not missing else f"no longer true — {v.get('why','')[:40]}"
        print(f"  [{reason}] {r.get('fact','')[:80]}")
    if len(skip) > 12:
        print(f"  ... and {len(skip)-12} more")

    if not apply:
        print(f"\nDry run. Re-run with --apply to reactivate the {len(restore)} above.")
        return 0

    n = restore_entries([r["id"] for r, _, _, _ in restore])
    print(f"\n✅ reactivated {n} fact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
