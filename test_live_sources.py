"""Readers must read where data is written now, not where it used to be.

29 Sep 2026 audit: the same bug in a dozen places. Episodes replaced FYIs and
nothing writes the fyis table, but six views still read it alone. Baby facts
moved to the vault, but every baby reader went through a function that read
only the old table (frozen 26 Aug). The /wedding views never read the vault's
wedding facts. Each one "worked" — it just showed a world that stopped moving.

This locks the sources statically. A new direct read of a legacy store fails
here until it is either routed through the union helper or added to ALLOWED
with a reason.

    python test_live_sources.py
"""
import re

SRC = {f: open(f"/Users/ansen/wedding-agent/{f}").read() for f in ("agent.py", "main.py")}

# Direct legacy-FYI reads that are fine, and why. Keyed by the enclosing def.
ALLOWED = {
    "_execute_tool": "read_fyis tool — merges episodes itself",
    "_get_fyis": "chat context — merges get_episodes(21) itself",
    "_run_image_loop": "image context — merges get_episodes(21) itself",
    "handle_image": "image context — merges get_episodes(21) itself",
    "knowledge_sweep": "extracts INTO the vault; episodes are already there",
    "morning_brief": "unread acks need fyis rows; episodes appended right after",
    "fyi_story": "merges get_episodes(35) itself",
}
fails = 0


def enclosing_def(src: str, pos: int) -> str:
    defs = [m for m in re.finditer(r"^\s*(?:async\s+)?def\s+(\w+)", src[:pos], re.M)]
    return defs[-1].group(1) if defs else "?"


for fname, src in SRC.items():
    for m in re.finditer(r"\b_?get_fyis(?:_for_context|_unacked)?\(", src):
        line = src[:m.start()].count("\n") + 1
        if re.match(r".*\bdef\s", src[src.rfind("\n", 0, m.start()) + 1:m.start()]):
            continue
        if src[m.end():m.end() + 1] == ")":
            continue  # _get_fyis() — the chat-context wrapper, which merges episodes
        fn = enclosing_def(src, m.start())
        if fn in ALLOWED:
            continue
        # a call inside a function that also reads episodes is a merge, not a silo
        body_start = src.rfind("def " + fn, 0, m.start())
        nxt = re.search(r"^\s*(?:async\s+)?def\s", src[m.start():], re.M)
        body = src[body_start:m.start() + (nxt.start() if nxt else 4000)]
        if "get_episodes" in body or "_eps" in body:
            continue
        print(f"FAIL {fname}:{line} {fn}() reads legacy FYIs only — use tools.fyis.recent_updates()")
        fails += 1

agent = SRC["agent.py"]
checks = [
    ("baby readers union the vault", "def _brain_entries" in open("/Users/ansen/wedding-agent/tools/baby_knowledge.py").read()),
    ("wedding views read vault facts",
     all(agent.count("wedding_facts_block()") >= 3 for _ in [0])),
    ("wedding views read all drops, not a 100/150 window",
     "get_recent_drops(limit=100)" not in agent and "get_recent_drops(limit=150)" not in agent),
    ("trip card follows merges", "canonical_trip(get_trip_by_id(trip_id))" in SRC["main.py"]),
]
for name, ok in checks:
    print(("PASS " if ok else "FAIL ") + name)
    fails += 0 if ok else 1

# Generators that show dated data must carry the date table.
for fn in ("brain_synthesis", "brain_search", "trip_milestone_brief", "jess_checkin",
           "appointment_pre_brief", "combined_daily_brief", "compress_shared_brain",
           "baby_knowledge_brief", "category_status", "bring_me_up_to_speed"):
    m = re.search(rf"async def {fn}\(.*?(?=\n    async def |\n    def |\Z)", agent, re.S)
    ok = bool(m) and "date_block()" in m.group(0)
    print(("PASS " if ok else "FAIL ") + f"{fn} has date_block()")
    fails += 0 if ok else 1

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
raise SystemExit(1 if fails else 0)
