"""Both inbound webhooks: each route has its own secret, and neither breaks the other.

1 Oct 2026 — /webhooks/registry (baby-registry bot) was added next to
/webhooks/signals (Growth Research). Each checks its own env var, so one
sender's secret never opens the other route, and a route whose secret is unset
answers 401 (not 404, not open). Drives the real aiohttp app with the database
stubbed out.

    python test_webhooks.py
"""
import asyncio
import os

os.environ["SIGNALS_WEBHOOK_SECRET"] = "sig-secret"
os.environ.pop("REGISTRY_WEBHOOK_SECRET", None)

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import webhook  # noqa: E402
from tools import registry  # noqa: E402

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got}, want {want}")


seen = set()


def fake_save(kind):
    def save(batch):
        items = batch["signals" if kind == "signals" else "events"]
        key = "signal_id" if kind == "signals" else "event_id"
        rows = [{**x, "source": batch["source"]} for x in items if (kind, x[key]) not in seen]
        seen.update((kind, x[key]) for x in items)
        return rows
    return save


webhook.save_batch = fake_save("signals")
registry.save_batch = fake_save("registry")

SIG = {"source": "test", "signals": [{"id": "s1", "type": "x_social", "symbol": "BTC",
                                      "direction": "watch", "strength": 0.5}]}
REG = {"source": "baby-registry-bot", "as_of": "2026-10-01T09:00:00+08:00",
       "events": [{"id": "e1", "type": "price_update", "summary": "Stroller down 15%",
                   "priority": "P1", "payload": {"item": "stroller"}, "links": ["https://example.com"]}]}


urgent_calls = []


async def on_urgent(events):
    urgent_calls.append([e["event_id"] for e in events])


async def main():
    async with TestClient(TestServer(webhook.build_app(on_registry_urgent=on_urgent))) as c:
        async def post(path, body, secret=None):
            h = {"Authorization": f"Bearer {secret}"} if secret else {}
            r = await c.post(path, json=body, headers=h)
            return r.status, await r.json()

        check("health", (await c.get("/health")).status, 200)
        check("registry, secret unset, no token -> 401 not 404",
              (await post("/webhooks/registry", REG))[0], 401)
        check("registry, secret unset, signals token -> 401",
              (await post("/webhooks/registry", REG, "sig-secret"))[0], 401)

        os.environ["REGISTRY_WEBHOOK_SECRET"] = "reg-secret"
        check("registry, own token -> accepted 1",
              await post("/webhooks/registry", REG, "reg-secret"), (200, {"ok": True, "accepted": 1}))
        check("registry, same id again -> accepted 0",
              await post("/webhooks/registry", REG, "reg-secret"), (200, {"ok": True, "accepted": 0}))
        check("registry, signals token -> 401",
              (await post("/webhooks/registry", REG, "sig-secret"))[0], 401)
        bad = {**REG, "events": [{**REG["events"][0], "id": "e2", "priority": "P9"}]}
        check("registry, bad priority -> 400",
              (await post("/webhooks/registry", bad, "reg-secret"))[0], 400)
        check("registry, signals-shaped body -> 400",
              (await post("/webhooks/registry", SIG, "reg-secret"))[0], 400)

        mixed = {**REG, "events": [{**REG["events"][0], "id": "p0", "priority": "P0"},
                                   {**REG["events"][0], "id": "p1", "priority": "P1"}]}
        await post("/webhooks/registry", mixed, "reg-secret")
        await asyncio.sleep(0.05)
        check("P0 alerts fire for the P0 event only", urgent_calls, [["p0"]])
        await post("/webhooks/registry", mixed, "reg-secret")
        await asyncio.sleep(0.05)
        check("a repeated P0 does not alert again", urgent_calls, [["p0"]])

        check("signals still works with its token",
              await post("/webhooks/signals", SIG, "sig-secret"), (200, {"ok": True, "accepted": 1}))
        check("signals, registry token -> 401",
              (await post("/webhooks/signals", SIG, "reg-secret"))[0], 401)

        # Rate limits are per route: filling registry's hour leaves signals alone.
        webhook._hits.clear()
        for _ in range(60):
            await post("/webhooks/registry", REG, "reg-secret")
        check("registry 61st request -> 429", (await post("/webhooks/registry", REG, "reg-secret"))[0], 429)
        check("signals unaffected by registry's limit",
              (await post("/webhooks/signals", {**SIG, "signals": [{**SIG["signals"][0], "id": "s2"}]}, "sig-secret"))[0], 200)


asyncio.run(main())
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
raise SystemExit(1 if fails else 0)
