# Wedding Agent — Pre-deploy QA checklist

Run through EVERY item before declaring any change ready to deploy.
History: repeated production crashes from missing DB tables/columns, missing
env vars, and unhandled exceptions that only surfaced on Railway.

## 1. DB table check
For every `get_client().table("X")` call touched or near changed code, verify
the table exists in its SQL file:

| Table | SQL file |
|-------|---------|
| `daily_tasks` | `supabase_daily_tasks.sql` |
| `baby_knowledge` | `supabase_baby_knowledge.sql` |
| `daily_categories` | `supabase_daily_categories.sql` |
| `fyis` | `supabase_fyis.sql` |
| `scheduled_notifications` | `supabase_notifications.sql` |
| `wedding_drops` | `supabase_wedding.sql` |
| `wedding_memory` | `supabase_wedding.sql` |
| `user_summaries` | `supabase_wedding.sql` |
| `wedding_payments` | `supabase_wedding.sql` |
| `check_ins` | `supabase_check_ins.sql` |
| `brain_entries` | `supabase_brain_entries.sql` (+ `kind` col: `supabase_brain_kind.sql`) |
| `loop_state` | `supabase_loop_state.sql` |
| `threads` | `supabase_threads.sql` |

New table → create the SQL file AND tell the user to run it in Supabase
BEFORE deploying (Railway deploys code instantly; Supabase must go first).

## 2. Column check
For any insert/update, verify the columns exist in the SQL schema file.
Known history: `daily_tasks.category` was missing until Jun 2026 and caused
silent data loss. Always check the SQL file matches what the code sends.

## 3. Env var check
Required in Railway (crash or broken behavior if missing):
`TELEGRAM_BOT_TOKEN`, `SUPABASE_URL`, `SUPABASE_KEY` (crash at startup);
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` (crash on
calendar/gmail use); `ALLOWED_USER_IDS` (else bot allows anyone);
`GOOGLE_CALENDAR_ID` (else calendar reads return empty); `TAVILY_API_KEY`
(else web search silently errors); `RAILWAY_ENVIRONMENT` (else bot exits
immediately — see main.py).

Also required: `ANTHROPIC_API_KEY` (crash on any agent call), `OPENAI_API_KEY`
(voice transcription degrades to error reply). `MEM0_API_KEY` — if unset, mem0
recall silently no-ops (check Railway; audit Jul 2026 flagged it as possibly
never configured).

Optional with defaults: `REMINDER_TZ` (Asia/Singapore), `EVENING_BRIEF_HOUR`
(21). (`PROACTIVE_HOUR` / `STOCKS_BRIEF_HOUR` are NOT read by any code — do
not add them expecting behavior change.)

Any NEW env var → list it as a pre-deploy step for the user.

## 4. Import check
Tools files must import `from tools.db import get_client`.
`tools.supabase_client` does NOT exist — importing it crashes at startup.

## 5. Graceful fallback check
Every NEW DB/tool call must be wrapped in try/except with a sensible default
at its call site in `_execute_tool()`. Already wrapped: `get_summary()` and
`get_shared_summary()` (agent.py), `get_all_categories()`
(daily_categories.py).

## 6. Commit completeness
`git status` must show no unstaged edits the change depends on. Common miss:
editing multiple files, then `git add` only one.

## 7. SQL migrations for new features
1. Write the SQL file first.
2. User runs it in Supabase BEFORE deploy.
3. Then push code.

## 8. Store-reachability check (write ↔ read contract)
For every write path touched (a save/log tool, a new store), verify a READ
path the agent actually uses can surface what was written — and trace it, do
not assume. History: Jess's birthday sat in `user_summaries` (written by
save_preference) while recall only queried `brain_entries` — stored correctly,
invisible forever. A store nothing reads is a fake feature.

## 9. Tool-branch smoke check
Any touched `_execute_tool` branch must be actually EXECUTED once (locally,
with .env) — not just compiled. History: message_partner raised
UnboundLocalError on every call since inception (a local `import os` below it
shadowed the module import); py_compile passed the whole time. Never add a
local `import os` inside `_execute_tool`.

## 10. No-silent-drop check
Every input the bot can receive must either be handled or produce an explicit
"I can't do this yet" reply. History: PDFs and file-images were silently
ignored for weeks (handler filter only matched TEXT|PHOTO|VOICE). Grep the
MessageHandler filters against every message type a user can plausibly send;
check new callback_data prefixes are routed in handle_callback.

## 11b. Recall-coverage sweep (RUN THE SCRIPT — don't eyeball it)
After ANY change to memory, recall, query_brain, a store's schema, or a new
knowledge store: run `python sweep_recall.py`. It samples real rows from every
knowledge store and asserts unified recall surfaces them. History: 'my DJ plans
are gone' — recall narrowed to brain_entries over many commits while
wedding_drops (160 rows) stayed a silo nothing read. NO single diff caused it,
so diff-review (this whole checklist) structurally could not catch it. Only a
reachability sweep does. New knowledge store → wire it into `_query_brain_sync`
AND add it to `sweep_recall.py`.

## 11. Dead-feature check (fake impressions)
When a feature is retired or replaced, hunt down every surface that still
LOOKS alive — buttons, commands, prompts mentioning it, tools writing to its
store — and either remove them or route them to the replacement. A button that
writes to a store nothing reads gives the user a fake "saved ✅".
