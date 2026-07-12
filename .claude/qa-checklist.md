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
| `brain_entries` | `supabase_brain_entries.sql` |
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

Optional with defaults: `REMINDER_TZ` (Asia/Singapore), `EVENING_BRIEF_HOUR`
(21), `PROACTIVE_HOUR` (14), `STOCKS_BRIEF_HOUR` (9).

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
