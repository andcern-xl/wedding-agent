# Wedding Agent — Project Context

## What this is
A Telegram bot for Ansen and Jess. Two purposes:
1. **Wedding brain** — drop notes/screenshots, bot categorises and tracks wedding planning
2. **Daily brain** — shared and personal task/reminder management with inline Done buttons

## How to run
```bash
source venv/bin/activate
python main.py
```
Deployed on Railway (auto-deploys on push to `main`).

## Key files
- `main.py` — Telegram bot handlers, commands, inline keyboard logic
- `agent.py` — LLM agent, system prompt, tool execution, brief generation
- `tools/daily.py` — task CRUD (Supabase `daily_tasks` table)
- `tools/user_memory.py` — per-user summaries + shared brain (`user_id=0` sentinel)
- `tools/fyis.py` — FYI log (lifecycle: active → archived/promoted, per-user acks, 30-day TTL)
- `tools/check_ins.py` — check-ins: decision questions the agent asks via button cards (lifecycle: open → answered/dismissed/snoozed/expired)
- `tools/notifications.py` — scheduled notifications

## Database (Supabase)
Tables: `daily_tasks`, `user_summaries`, `wedding_drops`, `scheduled_notifications`, `fyis`, `check_ins`

`daily_tasks` columns: `id, user_id, task, due_date, repeat, visibility, done, created_at, completed_at, assigned_to, category`

## Task categories (domains)
Every task gets exactly one: `baby` / `baby_questions` / `wedding` / `life` (enum on the `add_daily_task` tool; `normalize_category()` in tools/daily.py maps legacy slugs). Agent picks `unsure` when ambiguous → user gets 👶/💍/🏠 tap buttons (`taskcat:` callback). `/reminders` groups domain-first: Baby, Wedding, then per-person, then Shared. One-time backfill: `backfill_categories.py` (dry-run by default, `--apply` to write — already applied Jul 2026).

## Check-ins (agent feedback loop)
When the agent needs a decision, it calls `ask_check_in` (chat loop + proactive check) → card with tap buttons via `_send_check_in_cards` in main.py. Callbacks: `ci:{id}:{idx}` answers (atomic first-tap-wins for audience=both), `cisnz:{id}` snoozes 3 days. Actions: `save_decision` (→ shared brain, baby also → baby knowledge), `create_task`, `remind`, `dismiss`. Open check-ins are injected into system prompts so the agent never re-asks; lapsed snoozes re-send cards and 7-day-old unanswered ones expire to FYIs (housekeeping runs in `send_proactive_checks`).

`user_summaries`: stores per-user compressed memory. `user_id=0` = shared brain (visible to both users).

## Users
Two users in `ALLOWED_USER_IDS` env var. Ansen and Jess.

## Commands
- `/reminders` — tasks split by person (Cas / Jess / Shared) with inline ✅ Done buttons
- `/tasks` — combined daily brief with buttons
- `/shared` — shared brain (confirmed couple decisions)
- `/fyis` — recent FYIs
- `/commands` — full command list
- `/plan` — wedding priorities this week
- `/bringmeuptospeed` — full wedding overview

## Inline Done buttons
- `_reminders_keyboard(tasks, user_id)` in main.py — shows buttons for all tasks the user can complete (up to 12)
- `_can_complete(t, user_id)` — assigned tasks: assignee only; shared+no assignee: anyone; private: creator only
- `handle_callback` — instant button removal on tap, partner gets `✅ [Name] checked off: [task]` notification
- `complete_task(task_id, user_id)` in tools/daily.py — enforces same permission rules, returns False if not allowed

## Task routing logic (`reminders_brief` in agent.py)
- `assigned_to` set → task goes under assignee's section
- `visibility=shared` + no assignee → Shared section
- private → creator's section

## Junk filter
Tasks starting with: `fyi`, `• fyi`, `ansen deposited`, `jess deposited`, `ansen paid`, `jess paid`

## Shared brain
`get_shared_summary()` / `append_shared_summary()` in tools/user_memory.py.
Agent tool `save_shared_context` writes to it. Injected into both users' system prompts.

## Pending / future work
- Add `category` column to Supabase `daily_tasks` table (would enable proper wedding task filtering)
- Individual brain architecture: how Ansen's personal context interacts with shared brain
- `/budget` command
- Vendor directory
- Guest list management
