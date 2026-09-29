"""Regression test: a tool-using turn must not stop history from saving.

29 Sep 2026 — Railway logs: "save_history failed ... TypeError: Object of type
TextBlock is not JSON serializable". The chat loop appended response.content
(SDK objects) into history, so once a tool turn sat in the 40-message window
every save for that chat failed. Ansen's history froze at 18 Sep, Jess's at
23 Sep, and the nightly conversation sweep saw nothing after.

    python test_history_persist.py
"""
import json
from unittest import mock

from anthropic.types import TextBlock, ToolUseBlock
from dotenv import load_dotenv

load_dotenv("/Users/ansen/wedding-agent/.env")

import agent as A  # noqa: E402
from tools import conversation as C  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + name)
    fails += 0 if ok else 1


blocks = [
    TextBlock(type="text", text="Let me look that up."),
    ToolUseBlock(type="tool_use", id="toolu_1", name="query_brain", input={"q": "DJ"}),
]

# 1. The failure itself, byte-for-byte.
try:
    json.dumps([{"role": "assistant", "content": blocks}])
    check("raw SDK blocks fail to encode (repro)", False)
except TypeError:
    check("raw SDK blocks fail to encode (repro)", True)

# 2. content_to_dicts produces exactly what the API accepts back.
d = A.content_to_dicts(blocks)
check("converted blocks encode", bool(json.dumps(d)))
check("text block kept", d[0] == {"type": "text", "text": "Let me look that up."})
check("tool_use block kept with id/name/input",
      d[1] == {"type": "tool_use", "id": "toolu_1", "name": "query_brain", "input": {"q": "DJ"}})
check("dicts pass through untouched", A.content_to_dicts([{"type": "text", "text": "x"}]) == [{"type": "text", "text": "x"}])
check("None content is empty", A.content_to_dicts(None) == [])

# 3. save_history survives a stray SDK object instead of dropping the whole window.
sent = {}


class _Q:
    def upsert(self, row, on_conflict=None):
        sent["row"] = row
        return self

    def execute(self):
        json.dumps(sent["row"])  # what the Supabase client does
        return self


class _Client:
    def table(self, _):
        return _Q()


history = [
    {"role": "user", "content": "who's our DJ?"},
    {"role": "assistant", "content": blocks},  # the poisoned turn
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "{}"}]},
    {"role": "assistant", "content": "DJ Kenji."},
]
with mock.patch.object(C, "get_client", lambda: _Client()):
    C.save_history(1, history)
check("save_history writes despite SDK objects", "row" in sent)
if "row" in sent:
    msgs = sent["row"]["messages"]
    check("all 4 messages persisted", len(msgs) == 4)
    check("poisoned turn kept as text", msgs[1]["content"][0]["text"] == "Let me look that up.")

# 4. The live loop no longer puts SDK objects into history.
import inspect  # noqa: E402

src = inspect.getsource(A.UnifiedAgent._run_loop)
check("_run_loop converts response.content before appending",
      '"content": last_response.content}' not in src and "content_to_dicts(last_response.content)" in src)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
raise SystemExit(1 if fails else 0)
