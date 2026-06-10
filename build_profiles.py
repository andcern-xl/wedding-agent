"""
One-time script: retrospectively build rich user profiles for Ansen and Jess
from all existing data in the DB.

Usage:
    source venv/bin/activate
    python build_profiles.py
"""
import asyncio
from anthropic import AsyncAnthropic
from tools.db import get_client
from tools.log import get_drops
from tools.fyis import get_fyis
from tools.user_memory import get_summary, save_summary, get_message_count

USERS = {
    63756531: "Ansen",
    6927468999: "Jess",
}

OTHER = {uid: next(n for oid, n in USERS.items() if oid != uid) for uid in USERS}


def get_all_tasks_for_user(user_id: int) -> list[dict]:
    return (
        get_client().table("daily_tasks")
        .select("task, due_date, visibility, done, completed_at, assigned_to, created_at")
        .eq("user_id", user_id)
        .order("created_at")
        .execute()
        .data or []
    )


def get_fyis_for_user(user_id: int) -> list[dict]:
    return (
        get_client().table("fyis")
        .select("content, category, created_at")
        .eq("user_id", user_id)
        .order("created_at")
        .execute()
        .data or []
    )


def build_data_block(user_id: int, name: str) -> str:
    other = OTHER[user_id]
    parts = []

    # Wedding drops by this user
    all_drops = get_drops(limit=200)
    user_drops = [d for d in all_drops if d.get("user_id") == user_id]
    if user_drops:
        lines = [f"  [{d['ts'][:10]}] [{d.get('category', 'general')}] {d['content'][:200]}" for d in user_drops]
        parts.append(f"WEDDING NOTES DROPPED BY {name.upper()} ({len(user_drops)} total):\n" + "\n".join(lines))

    # Tasks created by this user
    tasks = get_all_tasks_for_user(user_id)
    if tasks:
        done = [t for t in tasks if t.get("done")]
        open_tasks = [t for t in tasks if not t.get("done")]
        task_lines = []
        for t in tasks:
            status = "✓ done" if t.get("done") else "open"
            vis = t.get("visibility", "private")
            task_lines.append(f"  [{t['created_at'][:10]}] {t['task']} ({status}, {vis})")
        parts.append(f"TASKS CREATED BY {name.upper()} ({len(done)} done, {len(open_tasks)} open):\n" + "\n".join(task_lines))

    # FYIs by this user
    fyis = get_fyis_for_user(user_id)
    if fyis:
        lines = [f"  [{f['created_at'][:10]}] {f['content'][:200]}" for f in fyis]
        parts.append(f"FYIS SHARED BY {name.upper()} ({len(fyis)} total):\n" + "\n".join(lines))

    # Existing summary (may be shallow)
    existing = get_summary(user_id)
    if existing:
        parts.append(f"EXISTING SUMMARY (shallow — to be replaced):\n{existing}")

    return "\n\n".join(parts) if parts else "(no data found)"


async def build_profile(client: AsyncAnthropic, user_id: int, name: str) -> str:
    other = OTHER[user_id]
    data = build_data_block(user_id, name)

    prompt = f"""You are building an initial memory profile for {name}, a user of a personal assistant Telegram bot they share with their partner {other}.

You have access to everything {name} has ever sent to the bot — wedding notes, tasks, FYIs — plus any existing summary. Use this to build a rich, specific profile that will make the assistant smarter from the first message onwards.

DATA:
{data}

---

Produce a profile using EXACTLY these section headers. Be specific and behavioural — infer from patterns in the data, not just what was explicitly stated.

## Identity
1-2 sentences: who they are, what they do, their life context, relationship to {other}.

## Communication style
How they write — length, tone, directness, formality, emoji use. What kind of responses likely land well based on how they phrase things.

## Current focus
What topics appear most recently or repeatedly in their messages. What they seem most preoccupied with right now.

## Habits & patterns
Specific recurring behaviours observable in the data — topics they return to, things they keep not finishing, how they tend to phrase requests, what they drop vs what they action. Be concrete.

## Important facts
Specific facts extractable from the data: events, finances, relationships, travel, upcoming commitments, anything personally significant. Facts only.

## What works / what to avoid
Based on the data, infer what kind of assistant behaviour would suit this person — brevity vs detail, proactive suggestions vs waiting to be asked, etc. Note anything they've corrected or complained about.

---

Rules:
- Specific beats generic. "Has dropped 3 venue notes but never followed up" beats "interested in venues".
- Infer from the pattern of what they've shared, not just individual messages.
- Under 600 words total.
- Third person throughout.
- Output the profile only — no preamble."""

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def main():
    client = AsyncAnthropic()

    for user_id, name in USERS.items():
        print(f"\n{'='*60}")
        print(f"Building profile for {name} (uid={user_id})...")
        print('='*60)

        profile = await build_profile(client, user_id, name)
        print(profile)

        # Preserve any existing PREFERENCES block
        existing = get_summary(user_id)
        pref_marker = "\n\nPREFERENCES:\n"
        if pref_marker in existing:
            _, pref_block = existing.split(pref_marker, 1)
            profile = profile + pref_marker + pref_block

        count = get_message_count(user_id)
        save_summary(user_id, profile, count)
        print(f"\n✓ Saved profile for {name}")

    print("\n\nDone. Both profiles built and saved.")


if __name__ == "__main__":
    asyncio.run(main())
