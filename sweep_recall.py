"""Recall-coverage sweep — proves no information is stranded.

The 'my DJ plans are gone' bug was not a bad diff; it was an emergent gap —
recall narrowed to brain_entries over many commits while wedding_drops stayed a
silo nothing read. A diff-review can't see that. THIS can: for every store that
holds knowledge, sample a real row and assert the unified recall surfaces it.

Run anytime (esp. after any memory/recall change):
    python sweep_recall.py
Exits non-zero if any store with data is unreachable from query_brain.
"""
import re
import sys

from dotenv import load_dotenv

load_dotenv("/Users/ansen/wedding-agent/.env")

from tools.db import get_client                    # noqa: E402
import agent as agent_mod                          # noqa: E402

_STOP = {"the", "and", "for", "with", "this", "that", "your", "you", "wedding",
         "screenshot", "details", "summary", "event", "https", "http", "com"}


def _keywords(text: str, n: int = 3) -> list[str]:
    words = [w for w in re.findall(r"[a-zA-Z]{4,}", (text or "").lower()) if w not in _STOP]
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w); out.append(w)
        if len(out) >= n:
            break
    return out


def _recall_surfaces(keywords: list[str], needle: str) -> bool:
    """Does unified recall bring back a row containing the needle?"""
    if not keywords:
        return True  # nothing distinctive to test with
    res = agent_mod._query_brain_sync(" ".join(keywords))
    blob = " ".join(
        str(v) for section in res.values() if isinstance(section, list)
        for item in section for v in item.values()
    ).lower()
    return needle.lower()[:40] in blob


def check_store(name: str, rows: list[dict], text_key, sample: int = 3) -> tuple[bool, str]:
    if not rows:
        return True, f"{name}: empty (skip)"
    step = max(1, len(rows) // sample)
    tested = rows[::step][:sample]
    misses = 0
    for r in tested:
        text = text_key(r)
        kws = _keywords(text)
        if not _recall_surfaces(kws, text.strip()[:40]):
            misses += 1
    ok = misses == 0
    return ok, f"{name}: {len(rows)} rows, sampled {len(tested)}, {'ALL reachable' if ok else f'{misses} UNREACHABLE'}"


def main():
    c = get_client()
    results = []

    # Vault
    be = c.table("brain_entries").select("*").eq("status", "active").limit(2000).execute().data or []
    results.append(check_store("brain_entries (vault)", be, lambda r: r.get("fact", "")))

    # Wedding drops (the silo that broke)
    wd = [r for r in (c.table("wedding_drops").select("*").limit(2000).execute().data or [])
          if len((r.get("content") or "")) > 20]
    results.append(check_store("wedding_drops", wd, lambda r: r.get("content", "")))

    # Baby knowledge
    bk = c.table("baby_knowledge").select("*").limit(500).execute().data or []
    results.append(check_store("baby_knowledge", bk, lambda r: r.get("summary", "")))

    print("=== RECALL COVERAGE SWEEP ===")
    all_ok = True
    for ok, msg in results:
        print(f"  {'✅' if ok else '❌'} {msg}")
        all_ok = all_ok and ok

    # Personal summaries are chat-injected, not query_brain-reachable — note it
    print("\n  ℹ️  user_summaries reach the CHAT via injection, not query_brain "
          "(passport/PII path). Not covered by this sweep — verify in chat if changed.")

    print("\n" + ("✅ PASS — every knowledge store is reachable from unified recall"
                  if all_ok else "❌ FAIL — a store with data is stranded; wire it into _query_brain_sync"))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
