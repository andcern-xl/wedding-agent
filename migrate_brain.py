"""One-time migration: parse the shared-brain blob (user_summaries user_id=0)
into structured brain_entries rows.

Idempotent — facts already in brain_entries are skipped, so it's safe to re-run
after deploy to sweep up blob writes that landed in the gap. The blob itself is
never modified; it stays frozen as the rollback path.

Usage:
    source venv/bin/activate
    python migrate_brain.py          # preview: parsed bullets + proposed domains
    python migrate_brain.py --apply  # insert into brain_entries
"""
import json
import re
import sys
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from tools.db import get_client
from tools.user_memory import DOMAINS, get_legacy_shared_blob, normalize_domain

APPLY = "--apply" in sys.argv

BULLET_RE = re.compile(r"^•\s*(\d{4}-\d{2}-\d{2}):\s*(.+)$")
TAG_RE = re.compile(r"^\[([^\]]+)\]\s*(.+)$")

# Deterministic domain pre-pass; anything unmatched goes to one LLM call.
KEYWORDS = {
    "wedding": ["venue", "vendor", "guest", "banquet", "bridal", "gown", "suit fitting",
                "room block", "solemni", "wedding", "photograph", "videograph", "makeup",
                "emcee", "band ", "invit"],
    "baby": ["obgyn", "pregnan", "baby", "nipt", "trimester", "scan", "confinement",
             "nanny", "hospital plan", "delivery", "maternity", "stroller", "crib",
             "paediatric", "pediatric"],
    "travel": ["flight", "hotel", "visa", "trip", "airbnb", "itinerary", "airport",
               "passport", "tomorrowland", "token2049"],
    "money": ["📊 stocks", "paid", "deposit", "sgd", "budget", "insurance", "invest",
              "dbs", "cpf", "transfer", "refund", "$"],
}


def classify_with_llm(items: list[dict]) -> dict[str, str]:
    """One call: index → domain for bullets no keyword matched."""
    import anthropic

    numbered = "\n".join(f"{it['idx']}: {it['fact']}" for it in items)
    prompt = f"""Classify each fact into exactly one domain: baby, wedding, travel, money, life.

Context: Ansen and Jess are getting married 7 Nov 2026 and expecting a baby 20 Feb 2027.
- baby: pregnancy, birth, hospital/delivery, OBGYN, baby gear/admin
- wedding: venue, vendors, guests, attire, wedding payments
- travel: trips, flights, hotels, visas
- money: finances, investments, payments, budgets, insurance
- life: everything else

Facts (index: text):
{numbered}

Reply with ONLY a JSON object mapping every index to its domain, no other text."""

    resp = anthropic.Anthropic().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    mapping = json.loads(raw)
    return {str(k): v for k, v in mapping.items() if v in DOMAINS}


def keyword_domain(fact: str) -> str | None:
    lowered = fact.lower()
    for domain, words in KEYWORDS.items():
        if any(w in lowered for w in words):
            return domain
    return None


def main():
    blob = get_legacy_shared_blob()
    lines = [l.strip() for l in blob.split("\n") if l.strip()]
    print(f"{len(lines)} non-empty blob lines")

    parsed: list[dict] = []  # {idx, fact_date, fact, domain|None}
    for i, line in enumerate(lines):
        m = BULLET_RE.match(line)
        if m:
            fact_date, body = m.group(1), m.group(2).strip()
        else:
            fact_date, body = date.today().isoformat(), line.lstrip("•").strip()

        # An existing [tag] prefix wins if it maps to a known domain.
        domain = None
        tag = TAG_RE.match(body)
        if tag:
            candidate = normalize_domain(tag.group(1))
            if tag.group(1).strip().lower() in ("baby", "wedding", "travel", "money", "life",
                                                "stocks", "finance", "trip", "trips", "budget",
                                                "pregnancy", "baby_questions"):
                domain = candidate
                body = tag.group(2).strip()
        parsed.append({"idx": str(i), "fact_date": fact_date, "fact": body, "domain": domain})

    assert len(parsed) == len(lines), "parsed count must match blob line count"

    needs_llm = []
    for p in parsed:
        if p["domain"] is None:
            p["domain"] = keyword_domain(p["fact"])
        if p["domain"] is None:
            needs_llm.append(p)

    if needs_llm:
        print(f"Classifying {len(needs_llm)} bullets via LLM...")
        mapping = classify_with_llm(needs_llm)
        for p in needs_llm:
            p["domain"] = mapping.get(p["idx"], "life")

    # Idempotency: skip facts already in the vault.
    existing = {
        r["fact"]
        for r in (get_client().table("brain_entries").select("fact").execute().data or [])
    }
    to_insert = [p for p in parsed if p["fact"] not in existing]
    skipped = len(parsed) - len(to_insert)

    for p in sorted(to_insert, key=lambda x: (x["domain"], x["fact_date"])):
        print(f"  {p['domain']:8s} {p['fact_date']}  {p['fact'][:80]}")
    if skipped:
        print(f"({skipped} already in brain_entries — skipped)")

    if not to_insert:
        print("Nothing to insert.")
        return

    if not APPLY:
        print(f"\nDry run: {len(to_insert)} entries would be inserted. Re-run with --apply to write.")
        return

    ok = 0
    for p in to_insert:
        try:
            get_client().table("brain_entries").insert({
                "fact": p["fact"],
                "domain": p["domain"],
                "fact_date": p["fact_date"],
                "source": "migration",
            }).execute()
            ok += 1
        except Exception as e:
            print(f"  FAILED: {p['fact'][:60]}: {e}")
    print(f"\nInserted {ok}/{len(to_insert)} entries.")

    from tools.user_memory import get_shared_summary
    print("\nRendered vault preview:\n" + get_shared_summary()[:800])


if __name__ == "__main__":
    main()
