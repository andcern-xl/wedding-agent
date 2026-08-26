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

## Wedding recall — the vault is not the archive
The wedding has 160+ drops going back to April 2026, but `brain_entries` only started capturing wedding facts mid-June, because `knowledge_sweep` read "the last 20 drops" — a count window that silently loses whatever overflows it (April alone logged 50). So the substance — day-of running order, food stations, lighting plan, DJ — lived only in `wedding_drops`, while the agent answered wedding questions from the vault slice and reported the wedding as a blank page ("No day-of timeline yet"). Three fixes, Aug 2026:

1. **Retrieval is content-first.** `read_wedding_drops` takes a `query` that searches the whole archive via `search_drops` (scores by query coverage, not raw hit count, so a long screenshot no longer outranks the short note that answers the question). Category filters never return a cliff: a thin category auto-widens to a content search and says so in a `note`. `get_drops` was ordering ascending *before* the limit, so it returned the OLDEST rows — fixed.
2. **The sweep is watermark-based.** `get_drops_since(ts)` + `loop_state["knowledge_sweep_drops"]` — it processes everything since the last successful run instead of a fixed count, advances the watermark only after a successful write, and logs (never silently drops) anything beyond the per-run cap.
3. **The pre-vault archive was backfilled once** via `backfill_wedding_brain.py` (36 facts, `source='backfill:wedding_drops'`). Multi-pass extraction unioned and deduped by containment, then an LLM verifier gate drops stale point-in-time snapshots and expired deadlines. Re-runnable; `--from-cache` re-tunes dedupe without re-extracting.

Prompt section WEDDING RECALL forbids "no X yet" / "all TBD" about the wedding until a content search has come back empty. Categories mislead: the day-of plan is under `ceremony`, lunch timings under `budget`, the event schedule and DJ timeline under `venue`, and `timeline` holds only a question. Wedding day is **Sat 7 Nov 2026** at FYSH, The Singapore EDITION; **5–10 Nov is the guest room block**, not the wedding date.

## Dates — looked up, never computed
The model does not do date arithmetic; it looks dates up. `date_block()` in agent.py
returns the rule plus a resolved table that runs both ways — weekday name → ISO date
(so "Friday" schedules on the right day) and ISO date → weekday name (so a dinner on
the 17th is called Monday), across a 35-day horizon. **Every prompt that can write a
date or a day name must interpolate it**, the same way they all interpolate
`FORMAT_RULES`. The Aug 2026 bug: the table existed only in the chat system prompt and
only spanned 7 days, so the briefs and check-in cards — which had nothing but "today is
X" — announced the Mon 17 Aug Alyssa dinner as "Sunday" for days, and check-in rows were
written with the wrong day baked into their text.

Calendar dates come from the users' timezone, not the server's: `local_today()` in
`tools/tz.py`, never bare `date.today()`. Railway runs UTC, so `date.today()` returns
yesterday for anything between 00:00 and 08:00 SGT — a task added at 1am got yesterday's
due_date. Instants stay `datetime.now(timezone.utc)`; `created_at`/`updated_at` are
timestamptz and UTC is right for those.

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

## Nothing becomes invisible without a record and a reverse gear

This is the rule that generalises every memory regression this bot has shipped.
All three had the same shape — information silently stopped being visible,
nothing crashed, and the only detector was Ansen noticing bad answers weeks or
months later:

| | What broke |
|---|---|
| Jul 2026 | recall narrowed while `wedding_drops` stayed a silo — "my DJ plans are gone" |
| Aug 2026 | `knowledge_sweep` read "the last 20 drops"; April's 50 overflowed and were lost |
| Aug 2026 | supersession retired facts with no containment check — 43 of 85 dropped a specific |

So: **any operation that makes information invisible — supersede, expire,
archive, dedupe, consolidate, truncate, "last N", top-k — must record what
replaced it, be reversible, and log what it dropped.** Truncation with no log is
the same bug as deletion. If a limit is applied, say so in the output.

Two corollaries that follow from the same reasoning:

- **Put the invariant in the write path, not in a review.** `schedule_notification`
  got dedup in Aug 2026, so Lucille's 13 daily pushes structurally cannot recur.
  `create_check_in` was left to prompt instructions and is still duplicating —
  Bangkok was carded three times on 19, 20 and 21 Aug; Jess answered the third
  and the first two kept nagging for days. Where a rule can be code at the point
  of writing, it must never be a prompt line or a checklist item.
- **Failing open beats failing closed.** A blocked merge costs a duplicate row. A
  wrong merge costs a fact, permanently, with nothing to notice it. Prefer the
  duplicate every time.

### The supersession guard

`supersession_is_safe(old, new)` in `tools/user_memory.py` gates every retirement.
Entities and governing **terms** must carry over literally; money and dates may
change value but must not vanish entirely. Wired into all four write paths:
`_upsert_shared`, `_upsert_shared_batch`, the whole-domain `compress` (which used
to retire every row in a domain against an LLM paraphrase — the main erosion
source), and `consolidate_episodes`.

The terms dimension is why Emily's row is the canonical case: `$350`, `$525` and
`Emily` all survived in other facts, so every specifics check said nothing was
lost — while "no upfront deposit, payment due after the event" was gone, which is
the only part that decides whether to chase a payment in August for a November
set. `test_supersession_guard.py` locks this behaviour; run it after any change
to `fact_specifics`.

`restore_entries()` is the reverse gear. `restore_eroded.py` is the one-time
recovery (dry-run by default; 24 facts reactivated 26 Aug 2026).

### The weekly self-audit

`self_audit.py`, wired to `send_self_audit` at **Monday 8:20am** — before the 9am
brief is built on the memory it checks. It messages Ansen **only on failure**; a
quiet Monday means the invariants hold. If the audit itself cannot run, it says
so, because a silent audit failure is the exact thing it exists to end.

Five invariants: **reachability** (every store surfaceable through unified
recall), **preservation** (no recent retirement dropped a specific),
**loop closure** (no open question whose answer is already known, no question
asked twice), **freshness** (no store or scheduled loop gone quiet — this is what
catches a job dying silently, as Jess's `proactive_check` did for two weeks),
**contradiction** (no active fact arguing with another).

Why this and not the QA checklist: `sweep_recall.py` was written in July for
invariant 1 and the checklist said to run it after any memory change. It had been
failing for weeks and nobody ran it. **A check a human must remember during
unrelated work is not a guarantee.** New invariants go here, as code, not as
prose. When a complaint turns out to be a class of bug, add the assertion.

## Pending / future work
- Add `category` column to Supabase `daily_tasks` table (would enable proper wedding task filtering)
- Individual brain architecture: how Ansen's personal context interacts with shared brain
- `/budget` command
- Vendor directory
- Guest list management
