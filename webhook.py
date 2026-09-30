"""Inbound HTTP for the bot: the Growth Research agent POSTs trade + X signals.

Runs inside the bot's own event loop (started from post_init in main.py) — the
bot stays a single Railway service. It only starts when SIGNALS_WEBHOOK_SECRET
is set, so a missing secret means no endpoint at all, never an open one.

  POST /webhooks/signals   Authorization: Bearer <SIGNALS_WEBHOOK_SECRET>
  GET  /health
"""
import asyncio
import hmac
import logging
import os
import time
from collections import defaultdict, deque

from aiohttp import web

from tools.signals import validate, save_batch

logger = logging.getLogger("webhook")

ALERT_MIN = float(os.environ.get("SIGNALS_ALERT_MIN", "0.9"))  # instant ping at or above
RATE_LIMIT = 60          # requests per hour, per client IP
RATE_WINDOW = 3600
MAX_BODY = 512 * 1024

_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(request: web.Request) -> str:
    # Railway's edge sets X-Forwarded-For; the socket peer is the proxy.
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or (request.remote or "?")


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def _authorized(request: web.Request) -> bool:
    secret = os.environ.get("SIGNALS_WEBHOOK_SECRET", "")
    header = request.headers.get("Authorization", "")
    if not secret or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):].encode(), secret.encode())


def _err(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


async def post_signals(request: web.Request) -> web.Response:
    if _rate_limited(_client_ip(request)):
        return _err(429, "rate limit: 60 requests per hour")
    if not _authorized(request):
        return _err(401, "missing or invalid bearer token")
    try:
        payload = await request.json()
    except Exception:
        return _err(400, "body must be valid JSON")
    batch, error = validate(payload)
    if error:
        return _err(400, error)
    try:
        stored = await asyncio.to_thread(save_batch, batch)
    except Exception:
        logger.exception("signals: save failed")
        return _err(500, "could not store signals")
    logger.info(f"signals: {batch['source']} sent {len(batch['signals'])}, accepted {len(stored)}")
    # Strong long/short signals ping Ansen now instead of waiting for 8pm.
    # Fire-and-forget: the sender's response never waits on Telegram.
    strong = [s for s in stored if s["direction"] in ("long", "short") and s["strength"] >= ALERT_MIN]
    on_strong = request.app.get("on_strong")
    if strong and on_strong:
        task = asyncio.create_task(on_strong(strong))
        request.app["alert_tasks"].add(task)
        task.add_done_callback(request.app["alert_tasks"].discard)
    return web.json_response({"ok": True, "accepted": len(stored)})


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def build_app(on_strong=None) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)
    app["on_strong"] = on_strong
    app["alert_tasks"] = set()   # keep refs so pending alerts aren't GC'd
    app.router.add_post("/webhooks/signals", post_signals)
    app.router.add_get("/health", health)
    return app


async def start(on_strong=None) -> web.AppRunner | None:
    """on_strong: async callable(list of stored signal rows) for instant alerts."""
    if not os.environ.get("SIGNALS_WEBHOOK_SECRET"):
        logger.info("webhook: SIGNALS_WEBHOOK_SECRET not set — not listening")
        return None
    runner = web.AppRunner(build_app(on_strong), access_log=None)
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"webhook: listening on :{port}")
    return runner
