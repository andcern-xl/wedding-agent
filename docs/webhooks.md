# Inbound webhooks

Other agents POST into the bot over HTTPS. The server (`webhook.py`) runs in
the same process as the Telegram bot on the Railway `worker` service.

Base URL: `https://worker-production-2a03.up.railway.app`

| Route | Sender | Secret env var | Stored in |
|---|---|---|---|
| `POST /webhooks/signals` | Growth Research (Grok bot) | `SIGNALS_WEBHOOK_SECRET` | `trade_signals` |
| `POST /webhooks/registry` | baby-registry bot | `REGISTRY_WEBHOOK_SECRET` | `registry_events` |
| `GET /health` | anyone | none | |

## Rules for both routes

- Auth header: `Authorization: Bearer <secret>`. Each route checks only its own
  env var, so one sender's secret never opens the other route.
- A route whose secret is not set answers `401` to every request. It is never open.
- The server starts listening when at least one of the two secrets is set.
- The same `id` from the same `source` is ignored for 24 hours. A retry is safe.
- At most 200 items per batch.
- Rate limit: 60 requests per hour per IP, counted separately per route.

## Response codes

| Code | Body | When |
|---|---|---|
| 200 | `{"ok": true, "accepted": N}` | Stored. `N` excludes duplicates, so it can be 0. |
| 400 | `{"ok": false, "error": "..."}` | Bad JSON or a bad field. The error names the field. |
| 401 | `{"ok": false, "error": "missing or invalid bearer token"}` | No token, wrong token, or that route's secret is unset. |
| 429 | `{"ok": false, "error": "rate limit: 60 requests per hour"}` | Over the hourly limit. |
| 500 | `{"ok": false, "error": "could not store ..."}` | Database write failed. Safe to retry. |

`GET /health` returns `200 {"ok": true}`.

## POST /webhooks/registry

```json
{
  "source": "baby-registry-bot",
  "as_of": "2026-10-01T09:00:00+08:00",
  "events": [{
    "id": "unique-id",
    "type": "research_update|list_update|price_update|timeline_update|question|note",
    "summary": "one line",
    "priority": "P0|P1|P2|info",
    "payload": {},
    "links": []
  }]
}
```

Required: `source`, `events[]`, and per event `id`, `type`, `priority`.
`as_of` is optional ISO 8601. `summary` is capped at 500 characters, `links`
at 10 URLs. `payload` is any JSON object and is stored as is.

```bash
curl -X POST "https://worker-production-2a03.up.railway.app/webhooks/registry" \
  -H "Authorization: Bearer $REGISTRY_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"source":"test","as_of":"2026-10-01T00:00:00Z","events":[{"id":"test-1","type":"note","summary":"ping","priority":"info","payload":{},"links":[]}]}'
```

The bot stores these events. It does not act on them yet: no alert, no brief.

## POST /webhooks/signals

```json
{
  "source": "growth-research",
  "as_of": "2026-10-01T01:00:00+08:00",
  "signals": [{
    "id": "unique-id",
    "type": "crypto|stock|x_social",
    "symbol": "BTC",
    "direction": "long|short|neutral|watch",
    "strength": 0.0,
    "summary": "one line",
    "proof_urls": [],
    "raw": {}
  }]
}
```

`strength` is 0 to 1. What the bot does with a signal:

- The 8pm SGT brief reads the last 36 hours and flags a symbol only when 3 or
  more independent sources lean buy (`long` at strength 0.5 or more). Each
  (source, type) pair is one source.
- A new `long` or `short` at strength 0.9 or more pings Ansen right away.

```bash
curl -X POST "https://worker-production-2a03.up.railway.app/webhooks/signals" \
  -H "Authorization: Bearer $SIGNALS_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"source":"test","as_of":"2026-10-01T00:00:00Z","signals":[{"id":"test-1","type":"x_social","symbol":"BTC","direction":"watch","strength":0.5,"summary":"ping","proof_urls":[]}]}'
```

## Deploying

1. Run the table SQL once in the Supabase SQL editor:
   `supabase_trade_signals.sql`, `supabase_registry_events.sql`.
2. Set the secret on Railway (service `worker`). `PORT=8080` is already set.
3. Merge to `main`. Railway deploys on push.
4. Check: `curl -i -X POST .../webhooks/registry` with no token returns `401`.
