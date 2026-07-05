"""One-time backfill: give every open task a canonical domain (baby / baby_questions / wedding / life).

Legacy slugs are mapped deterministically; uncategorised tasks are classified
in a single LLM call. Dry-run by default — pass --apply to write.

Usage:
    source venv/bin/activate
    python backfill_categories.py          # preview
    python backfill_categories.py --apply  # write to Supabase
"""
import json
import sys

from dotenv import load_dotenv

load_dotenv()

from tools.db import get_client
from tools.daily import TASK_DOMAINS, normalize_category

APPLY = "--apply" in sys.argv


def classify_with_llm(tasks: list[dict]) -> dict[str, str]:
    """One call: task id → domain for tasks with no usable category."""
    import anthropic

    numbered = "\n".join(f"{t['id']}: {t['task']}" for t in tasks)
    prompt = f"""Classify each task into exactly one domain:
- baby: pregnancy, birth, hospital/delivery plans and costs, OBGYN, scans, baby gear, baby insurance/admin (hospital plans, delivery packages, maternity anything → baby, NOT life)
- baby_questions: a question to ask at a medical appointment
- wedding: venue, vendors, guests, gifts, attire, room blocks, wedding payments
- life: everything else (errands, home, social, finance, work, travel)

Context: Ansen and Jess are getting married 7 Nov 2026 and expecting a baby 18 Feb 2027. Mt Alvernia and Thomson are delivery hospitals; Alyssa is their insurance agent for baby/hospital plans.

Tasks (id: text):
{numbered}

Reply with ONLY a JSON object mapping every id to its domain, no other text."""

    resp = anthropic.Anthropic().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    mapping = json.loads(raw)
    return {k: v for k, v in mapping.items() if v in TASK_DOMAINS}


def main():
    rows = (
        get_client().table("daily_tasks")
        .select("id,task,category")
        .eq("done", False)
        .execute()
        .data or []
    )
    print(f"{len(rows)} open tasks")

    # Slugs that are unambiguously daily-life. Anything else (health, personal,
    # misc, none) gets classified from the task text — "health" is often pregnancy.
    DEFINITE_LIFE = {"finance", "home", "work", "social", "travel"}

    updates: dict[str, str] = {}
    needs_llm: list[dict] = []
    for r in rows:
        current = r.get("category")
        if current in TASK_DOMAINS:
            continue  # already canonical
        if current in DEFINITE_LIFE:
            updates[str(r["id"])] = "life"
        else:
            needs_llm.append({"id": str(r["id"]), "task": r["task"]})

    if needs_llm:
        print(f"Classifying {len(needs_llm)} uncategorised tasks via LLM...")
        updates.update(classify_with_llm(needs_llm))

    if not updates:
        print("Nothing to do — all open tasks already categorised.")
        return

    by_task = {str(r["id"]): r["task"] for r in rows}
    for tid, cat in sorted(updates.items(), key=lambda kv: kv[1]):
        print(f"  {cat:15s} ← {by_task.get(tid, tid)[:70]}")

    if not APPLY:
        print(f"\nDry run: {len(updates)} tasks would be updated. Re-run with --apply to write.")
        return

    ok = 0
    for tid, cat in updates.items():
        try:
            get_client().table("daily_tasks").update({"category": cat}).eq("id", tid).execute()
            ok += 1
        except Exception as e:
            print(f"  FAILED {tid}: {e}")
    print(f"\nUpdated {ok}/{len(updates)} tasks.")


if __name__ == "__main__":
    main()
