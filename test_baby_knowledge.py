"""The baby knowledge view must read where baby facts are actually written.

29 Sep 2026 — Ansen: "the baby knowledge is super obsolete, information i sent
to the chat were not captured — instagram links, baby products". They WERE
captured, into brain_entries (domain 'baby'). Every reader went through
tools.baby_knowledge.get_entries(), which read only the baby_knowledge table —
last written 26 Aug. So the 📚 view froze on that date, and with no date in its
prompt it announced "You are currently in Week 7" at week 19.

    python test_baby_knowledge.py
"""
import inspect
from unittest import mock

from dotenv import load_dotenv

load_dotenv("/Users/ansen/wedding-agent/.env")

import agent as A  # noqa: E402
from tools import baby_knowledge as BK  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + name)
    fails += 0 if ok else 1


LEGACY = [{"id": "L1", "summary": "Pregnancy: Week 7, NT scan before 26 Aug appointment",
           "created_at": "2026-06-20T00:00:00", "tags": []}]
BRAIN = [
    {"id": f"S{i}", "fact": f"Symptom (week 19): Feeling good {i}", "source": "symptom",
     "created_at": f"2026-09-2{i}T00:00:00", "fact_date": None} for i in range(6)
] + [
    {"id": "B1", "fact": "Stroller option found on Instagram: https://www.instagram.com/reel/DY5iJEvx-0c/",
     "source": "chat", "created_at": "2026-09-16T00:00:00", "fact_date": "2026-09-16"},
]

with mock.patch.object(BK, "_legacy_entries", lambda limit: list(LEGACY)), \
        mock.patch.object(BK, "_brain_entries", wraps=BK._brain_entries), \
        mock.patch.object(BK, "get_client") as gc:
    gc.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value \
        .order.return_value.limit.return_value.execute.return_value.data = BRAIN
    e = BK.get_entries(limit=50)
    texts = [x["summary"] for x in e]
    check("September Instagram stroller link is visible", any("instagram.com/reel" in t for t in texts))
    check("legacy June row still visible", any("Week 7" in t for t in texts))
    check("newest first", e[0]["created_at"] >= e[-1]["created_at"])
    check("symptom logs collapsed to 3", sum(t.startswith("Symptom (") for t in texts) == 3)
    check("include_brain=False is legacy only (correct_knowledge edits by id)",
          [x["id"] for x in BK.get_entries(limit=50, include_brain=False)] == ["L1"])
    check("search finds the stroller by one word", len(BK.search_entries("stroller")) == 1)
    check("search: phrase not verbatim still matches by word",
          len(BK.search_entries("instagram stroller ideas")) >= 1)

src = inspect.getsource(A.UnifiedAgent.baby_knowledge_brief)
check("view prompt carries date_block()", "date_block()" in src)
check("view prompt carries the computed week", "pregnancy_summary" in src)
check("stored dates are annotated relative to today", "annotate_dates" in src)
check("correct_knowledge edits legacy rows only",
      "_bk_get(limit=200, include_brain=False)" in inspect.getsource(A.UnifiedAgent._execute_tool)
      or "_bk_get(limit=200, include_brain=False)" in open("/Users/ansen/wedding-agent/agent.py").read())

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
raise SystemExit(1 if fails else 0)
