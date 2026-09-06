"""Regression test: the agent asks when it doesn't know who something is for.

Ansen raised this three times — a card with no option that fit, stale items
neither asked about nor dropped, and a trip filed to one person that the other
couldn't see. Each time it was fixed in whichever tool had come up, so it kept
coming back. The mechanism is now central, in _execute_tool, and these cases
lock it: fire on unknown, stay silent when the answer is known, and never fire
on a legitimate False.

    python test_clarify.py
"""
import asyncio

from dotenv import load_dotenv

load_dotenv("/Users/ansen/wedding-agent/.env")

import agent as A  # noqa: E402

CASES = [
    # (tool, inputs, should_ask, what it is)
    ("save_trip", {"destination": "Bangkok", "travellers": "unknown"}, True,
     "a trip with no traveller named"),
    ("save_trip", {"destination": "Bangkok"}, True,
     "a trip with the field missing entirely"),
    ("save_trip", {"destination": "Bangkok", "travellers": "both"}, False,
     "a trip they said is for both"),
    ("save_trip", {"destination": "Phuket", "travellers": "ansen"}, False,
     "a trip they said is Ansen's"),
    ("add_daily_task", {"task": "call the venue", "visibility": "unknown"}, True,
     "a task with no owner named"),
    ("add_daily_task", {"task": "call the venue", "visibility": "private"}, False,
     "a task they said is private"),
    ("save_to_brain", {"content": "x", "audience": "unknown"}, True,
     "a fact with no audience named"),
    ("save_to_brain", {"content": "x", "audience": "shared"}, False,
     "a fact they said is shared"),
    # A boolean false is an ANSWER, not an absence. `value or "unknown"` read it
    # as unknown and would have asked a question nobody needed.
    ("add_daily_task", {"task": "x", "visibility": "shared", "for_all_users": False}, False,
     "a legitimate False on an unrelated field"),
    ("log_contact", {"person": "Elenna"}, False,
     "a tool that is not in the registry at all"),
]


async def run() -> int:
    agent = A.UnifiedAgent()

    async def stub(name, inputs, user_id, flags):
        return {"status": "saved"}

    agent._execute_tool_inner = stub  # never touch the database

    failures = 0
    for tool, inputs, should_ask, label in CASES:
        result = await agent._execute_tool(tool, inputs, 63756531, {})
        asked = "ask_them" in (result or {})
        ok = asked == should_ask
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {'asks' if asked else 'quiet':5} "
              f"(want {'asks' if should_ask else 'quiet':5}) — {label}")

    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
