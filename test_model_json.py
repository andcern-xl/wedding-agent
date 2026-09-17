"""Regression test: a model reply shaped as an array must not kill the turn.

17 Sep 2026 — Ansen sent a screenshot and got
"[DEBUG] AttributeError: 'list' object has no attribute 'get'", with no reply
and nothing retrievable afterwards.

_extract_payment (live, reached from UnifiedAgent.handle_image) did:

    data = json.loads(text)
    if data.get("skip"): ...
    except (json.JSONDecodeError, IndexError): return None

A screenshot holding two payments makes the model answer with an ARRAY.
json.loads returns a list, .get() raises AttributeError, and the except clause
did not cover it — so it escaped and took the whole turn down. save_history runs
after handle_message returns, so the message never reached history either: that
is why nothing could be dug back up afterwards.

    python test_model_json.py
"""
import asyncio

from dotenv import load_dotenv

load_dotenv("/Users/ansen/wedding-agent/.env")

import agent as A  # noqa: E402

SHAPES = [
    ('{"vendor":"Moxy","amount":1078}', dict, "a single object"),
    ('[{"vendor":"Moxy","amount":1078},{"vendor":"Gimpo","amount":321}]', dict,
     "an ARRAY — the exact crash shape"),
    ('[{"vendor":"Moxy"}]', dict, "an array wrapping one object"),
    ('```json\n{"skip": true}\n```', dict, "a fenced object"),
    ('["just","strings"]', type(None), "an array of non-objects"),
    ("I could not find any payments.", type(None), "prose with no JSON"),
    ("[]", type(None), "an empty array"),
    ("", type(None), "an empty reply"),
]


class _Blk:
    def __init__(self, t):
        self.text, self.type = t, "text"


class _Resp:
    def __init__(self, t):
        self.content = [_Blk(t)]


class _Msgs:
    def __init__(self, p):
        self.payload = p

    async def create(self, **kw):
        return _Resp(self.payload)


class _Client:
    def __init__(self, p):
        self.messages = _Msgs(p)


async def run() -> int:
    failures = 0

    for raw, want, label in SHAPES:
        got = A.as_json_object(raw)
        ok = type(got) is want
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} as_json_object on {label:34} -> {type(got).__name__}")

    # The live path: WeddingAgent._extract_payment, called by handle_image.
    wedding = A.WeddingAgent()
    for raw, _want, label in SHAPES:
        wedding.client = _Client(raw)
        try:
            await wedding._extract_payment(b"img", "screenshot")
            print(f"ok   _extract_payment survives {label}")
        except Exception as e:
            failures += 1
            print(f"FAIL _extract_payment on {label}: {type(e).__name__}: {e}")

    total = len(SHAPES) * 2
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
