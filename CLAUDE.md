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
- `tools/fyis.py` — LEGACY FYI log (draining; replaced by brain episodes — kept one release as rollback)
- `tools/check_ins.py` — check-ins: decision questions the agent asks via button cards (lifecycle: open → answered/dismissed/snoozed/expired)
- `tools/notifications.py` — scheduled notifications (search, bulk cancel, series stop, duplicate guard)

## Database (Supabase)
Tables: `daily_tasks`, `user_summaries`, `wedding_drops`, `scheduled_notifications`, `fyis`, `check_ins`, `brain_entries`, `loop_state`, `threads`

## Episodic vs semantic memory (replaces FYIs)
`brain_entries.kind` (`supabase_brain_kind.sql`): `fact` = timeless knowledge ("Jess likes kaya waffles from Rice Bakehouse"), `episode` = dated life event ("paid condo fee $837, 2 Jul") logged via `log_episode` (partner still pushed instantly at capture). Episodes sit dormant as context, surface via RECALL discipline (all briefs + chat query the brain for every person/occasion in scope — the "it's Jess's birthday, she likes X" behavior), and consolidate Sundays: 45-day-old episodes → pattern facts or fade (`consolidate_episodes`). `log_fyi` aliases to `log_episode`; `read_fyis` reads episodes + draining legacy FYIs. One-off migration: `migrate_fyis.py` (dry-run default).

## Thread ledger
`threads` table (`supabase_threads.sql`): dated contact tracking per person/topic — the ONLY legitimate source for "last contact" / "day N" claims. Tools: `log_contact` (silent, on any mentioned contact; whole-name-token + topic-keyword thread matching), `read_threads` (computed `days_since_contact`), `resolve_thread`. Date corrections from users ("last contact was NOT 2 days ago") route to `log_contact` with the corrected `contact_date`, not `correct_knowledge`.

`daily_tasks` columns: `id, user_id, task, due_date, repeat, visibility, done, created_at, completed_at, assigned_to, category`

## Task categories (domains)
Every task gets exactly one: `baby` / `baby_questions` / `wedding` / `life` (enum on the `add_daily_task` tool; `normalize_category()` in tools/daily.py maps legacy slugs). Agent picks `unsure` when ambiguous → user gets 👶/💍/🏠 tap buttons (`taskcat:` callback). `/reminders` groups domain-first: Baby, Wedding, then per-person, then Shared. One-time backfill: `backfill_categories.py` (dry-run by default, `--apply` to write — already applied Jul 2026).

## Scheduled notifications — everything switchable from Telegram
`scheduled_notifications` are the timed pushes (distinct from `daily_tasks`, which is what `/reminders` shows). Nothing the agent schedules should ever need a code change to switch off, so there are three ways to kill one, all in-chat:
1. **In words** — "turn off the Lucille reminders" → `find_notifications(subject)` fuzzy-matches upcoming rows household-wide, then `cancel_notifications([ids])` kills them all in one call. The system prompt (TURNING REMINDERS OFF) forbids two failure modes that shipped in Aug 2026: claiming "I don't have a record" before searching, and asking the user for a notification UUID.
2. **🔕 on the reminder itself** — recurring notifications ship with a Stop button next to ✅ Got it (`notifstop:` → `stop_series`, deletes every pending copy of that text for that person).
3. **`/notifications`** (aliases `/notifs`, `/alerts`) — the full upcoming list grouped by day, ❌ per occurrence + 🔕 Stop all per recurring series, with a duplicate warning banner.

Scope is **household**, not per-user: a reminder Jess set is one Ansen can find and cancel. `list_notifications`/`find_notifications` take `scope="mine"` to narrow.

`schedule_notification` dedupes on insert — an identical message at the same slot (same clock time for recurring, same instant for one-offs) returns the existing row instead of stacking a copy, and flags `_similar` when the same text already fires at a *different* time so the agent can ask rather than silently duplicate. This is what stopped Lucille's 3 meds becoming 13 daily pushes.

## Check-ins (agent feedback loop)
When the agent needs a decision, it calls `ask_check_in` (chat loop + proactive check) → card with tap buttons via `_send_check_in_cards` in main.py. Callbacks: `ci:{id}:{idx}` answers (atomic first-tap-wins for audience=both), `cisnz:{id}` snoozes 3 days. Actions: `save_decision` (→ shared brain, baby also → baby knowledge), `create_task`, `remind`, `dismiss`. Open check-ins are injected into system prompts so the agent never re-asks; lapsed snoozes re-send cards and 7-day-old unanswered ones expire to FYIs (housekeeping runs in `send_proactive_checks`).

`user_summaries`: stores per-user compressed memory. `user_id=0` = shared brain (visible to both users).

## Users
Two users in `ALLOWED_USER_IDS` env var. Ansen and Jess.

## Commands
- `/reminders` — tasks split by person (Cas / Jess / Shared) with inline ✅ Done buttons
- `/notifications` (`/notifs`, `/alerts`) — scheduled timed pushes, ❌ to cancel one, 🔕 to stop a whole recurring series
- `/tasks` — combined daily brief with buttons
- `/shared` — shared brain story (facts + recent episodes; Brain/Tasks/Reminders menu — no FYIs button, the concept is retired)
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

## Shared brain (vault)
Structured rows in `brain_entries`: one fact per row with `domain` (baby/wedding/travel/money/life), `fact_date`, `status` (active/superseded), `source`. Writers supersede stale rows instead of rewriting — `_upsert_shared(fact, domain, source)` and `_upsert_shared_batch` in agent.py, `add_brain_entry`/`supersede_entries` in tools/user_memory.py. `get_shared_summary()` renders active entries in the legacy bullet grammar `• YYYY-MM-DD: [domain] fact` (falls back to the frozen legacy blob on `user_summaries` user_id=0 if the table is empty — that blob is the rollback path, never write it). Injected into chat, proactive check, and morning/evening briefs (relevance-filtered via `_relevant_bullets`). One-time migration: `migrate_brain.py` (dry-run default).

## Loop state (delta briefs)
`loop_state` table, one row per (loop_name, user_id); `tools/loop_state.py` (`load_state`/`save_state`/`already_sent`, `COUPLE=0` for couple-wide loops). Every scheduled sender loads what it already sent and generates delta-only output: `morning_brief` (per-user), `nightly_wrap`, `baby_weekly`, `priority_brief`, `appointment_prebrief` (couple-wide), `proactive_check` (per-user). Old `proactive_state` table/tool kept one release for rollback.

## Pending / future work
- Add `category` column to Supabase `daily_tasks` table (would enable proper wedding task filtering)
- Individual brain architecture: how Ansen's personal context interacts with shared brain
- `/budget` command
- Vendor directory
- Guest list management
