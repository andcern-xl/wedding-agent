import asyncio
import base64
import json
from datetime import datetime, date, timezone, timedelta
from anthropic import AsyncAnthropic
from categories import CATEGORIES, detect_category
from tools.memory import get_all_memory, get_category_memory
from tools.google_docs import fetch_docs_for_category, extract_doc_id
from tools.log import get_drops, get_recent_drops
from tools.payments import add_payment, summary as payment_summary
from tools.daily import add_task, get_all_tasks_for_brief, get_tasks
from tools.daily_categories import get_all_categories, add_custom_category, detect_daily_category, BUILT_IN_CATEGORIES
from tools.user_memory import get_summary, save_summary, get_message_count
from tools.gcal import get_events, create_event, delete_event

SYSTEM_PROMPT = """You are a wedding planning assistant for a couple planning their wedding. They drop notes, screenshots, and discussions into this chat as they go — treat everything they've sent as your source of truth.

Your job is to help them make sense of what they've gathered, answer questions, spot gaps, and keep things moving.

WEDDING CATEGORIES
{categories}

HOW TO RESPOND
- Be concise and practical
- Reference specific things they've actually dropped — quotes, details, numbers
- If a screenshot contains a quote, venue, menu, or price — extract and summarise it clearly
- Use Telegram HTML formatting: <b>Section Title</b> for headers, • for bullet points, blank lines between sections
- Never use asterisks, underscores, or markdown symbols — HTML tags only
- Sound like a sharp friend helping them plan, not a robot"""


class WeddingAgent:
    def __init__(self):
        self.client = AsyncAnthropic()

    def _build_system_prompt(self) -> str:
        cat_lines = "\n".join(
            f"- {v['emoji']} {k}: {v['name']} — {v['description']}"
            for k, v in CATEGORIES.items()
        )
        return SYSTEM_PROMPT.format(categories=cat_lines)

    def _drops_block(self, drops: list[dict], label: str = "") -> str:
        if not drops:
            return ""
        lines = [f"{label}\n"] if label else []
        for d in drops:
            ts = d["ts"][:10]
            icon = "📸" if d["kind"] == "image" else "💬"
            cat_tag = f"[{d.get('category', '')}] " if d.get("category") else ""
            lines.append(f"{icon} {ts} {cat_tag}{d['content']}")
        return "\n".join(lines)

    async def handle_message(self, text: str, history: list[dict] | None = None) -> dict:
        if history is None:
            history = []

        category = detect_category(text)
        drops = get_drops(category=category, limit=40) if category else get_recent_drops(limit=30)
        context = self._drops_block(drops, "WHAT YOUVE SHARED SO FAR:")

        doc_note = ""
        if "docs.google.com" in text:
            doc_id = extract_doc_id(text)
            if doc_id and category:
                from tools.memory import link_doc_to_category
                link_doc_to_category(category, doc_id)
                cat_name = CATEGORIES.get(category, {}).get("name", category)
                doc_note = f"\n\n[Doc auto-linked to {cat_name}]"
            docs = fetch_docs_for_category(category) if category else ""
            if docs:
                context += f"\n\nLINKED DOC CONTENT:\n{docs}"

        user_content = f"[Context]\n{context}\n\n[Message]\n{text}" if context else text

        messages = history + [{"role": "user", "content": user_content}]

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=self._build_system_prompt(),
            messages=messages,
        )

        reply = response.content[0].text + doc_note
        updated_history = messages + [{"role": "assistant", "content": reply}]
        if len(updated_history) > 40:
            updated_history = updated_history[-40:]

        return {
            "text": reply,
            "detected_category": category,
            "history": updated_history,
        }

    async def _extract_payment(self, image_bytes: bytes, caption: str) -> dict | None:
        """Try to extract structured payment data from a financial screenshot."""
        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        prompt = """Look at this image. If it contains financial information (invoice, quote, payment confirmation, bank transfer, receipt, bill), extract the details as JSON.

Return ONLY a JSON object with these fields (omit fields you can't determine):
{
  "vendor": "who is being paid e.g. Molenvliet Venue",
  "amount": 45000,
  "currency": "ZAR",
  "paid_by": "name of person who paid, or null if unknown",
  "status": "paid OR owing OR deposit OR quote",
  "date": "YYYY-MM-DD or null",
  "notes": "one line description e.g. 50% deposit for reception venue"
}

If this image has no financial content, return: {"skip": true}"""

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                    {"type": "text", "text": (caption + "\n\n" if caption else "") + prompt},
                ],
            }],
        )

        try:
            text = response.content[0].text.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text.strip())
            if data.get("skip"):
                return None
            return data
        except (json.JSONDecodeError, IndexError):
            return None

    async def handle_image(self, image_bytes: bytes, caption: str, history: list[dict] | None = None) -> dict:
        import asyncio
        if history is None:
            history = []

        category = detect_category(caption) if caption else None
        drops = get_drops(category=category, limit=30) if category else get_recent_drops(limit=20)
        context = self._drops_block(drops, "WHAT YOUVE SHARED SO FAR:")

        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

        content = []
        if context:
            content.append({"type": "text", "text": f"[Context]\n{context}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}})
        content.append({
            "type": "text",
            "text": caption or "What's in this image? Extract anything relevant to our wedding planning.",
        })

        messages = history + [{"role": "user", "content": content}]

        main_call = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=self._build_system_prompt(),
            messages=messages,
        )
        payment_call = self._extract_payment(image_bytes, caption)

        response, payment = await asyncio.gather(main_call, payment_call)

        suffix = ""
        if payment:
            add_payment(payment)
            status_label = {"paid": "paid", "deposit": "deposit paid", "owing": "still owed", "quote": "quoted"}.get(payment.get("status", ""), payment.get("status", ""))
            currency = payment.get("currency", "")
            amount = payment.get("amount", "")
            vendor = payment.get("vendor", "")
            paid_by = payment.get("paid_by")
            by_str = f" by {paid_by}" if paid_by else ""
            suffix = f"\n\n💰 Logged: {currency} {amount:,} {status_label}{by_str} — {vendor}"

        reply = response.content[0].text + suffix
        updated_history = messages + [{"role": "assistant", "content": reply}]
        if len(updated_history) > 40:
            updated_history = updated_history[-40:]

        return {
            "text": reply,
            "detected_category": category or "budget",
            "history": updated_history,
        }

    async def category_status(self, category: str) -> str:
        cat = CATEGORIES[category]
        drops = get_drops(category=category, limit=60)
        decisions = get_all_memory().get(category, {}).get("decisions", [])
        docs = fetch_docs_for_category(category)

        parts = []

        # Budget category gets a financial summary up front
        if category == "budget":
            fin = payment_summary()
            if fin["payments"]:
                lines = ["PAYMENTS LOGGED:"]
                for p in fin["payments"]:
                    status_label = {"paid": "paid", "deposit": "deposit", "owing": "owing", "quote": "quote"}.get(p.get("status", ""), p.get("status", ""))
                    cur = p.get("currency", "")
                    amt = p.get("amount", 0)
                    by = f" — {p['paid_by']}" if p.get("paid_by") else ""
                    lines.append(f"  {p.get('vendor', 'unknown')}: {cur} {amt:,} ({status_label}){by}")
                lines.append(f"\nTotal paid/deposited: {list(fin['by_person'].items())[0][1] if fin['by_person'] else 0:,}")
                for person, amt in fin["by_person"].items():
                    lines.append(f"  {person}: {amt:,}")
                if fin["total_owing"]:
                    lines.append(f"Still owing: {fin['total_owing']:,}")
                parts.append("\n".join(lines))

        if drops:
            parts.append(self._drops_block(drops, "DROPS:"))
        if decisions:
            parts.append("LOCKED DECISIONS:\n" + "\n".join(f"• {d}" for d in decisions))
        if docs:
            parts.append(f"LINKED DOCS:\n{docs}")

        if not parts:
            return f"{cat['emoji']} Nothing dropped for {cat['name']} yet.\n\nJust start talking about it — I'll pick it up."

        context = "\n\n".join(parts)
        prompt = f"""{context}

Give me a status brief for {cat['name']}. Use exactly this structure — put --- on its own line between each section:

<b>What's Confirmed</b>
Anything that looks like a firm decision or booking. One bullet per item using •

---

<b>What's Being Considered</b>
Options discussed, quotes seen, things in the running. One bullet per item using •

---

<b>Still Open</b>
Key decisions not made yet for this area. One bullet per item using •

---

<b>Next Step</b>
One concrete thing to do next.

Use Telegram HTML formatting. <b> for headers only. No markdown."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            system=self._build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        header = f"{cat['emoji']} <b>{cat['name'].upper()}</b>"
        return f"{header}\n\n{response.content[0].text}"

    async def bring_me_up_to_speed(self) -> str:
        all_drops = get_recent_drops(limit=100)
        all_decisions = get_all_memory()

        parts = []

        if all_drops:
            lines = []
            for d in all_drops:
                ts = d["ts"][:10]
                icon = "📸" if d["kind"] == "image" else "💬"
                cat_tag = f"[{d.get('category', 'general')}] "
                lines.append(f"{icon} {ts} {cat_tag}{d['content']}")
            parts.append("ALL DROPS:\n" + "\n".join(lines))

        locked = []
        for cat_key, data in all_decisions.items():
            for dec in data.get("decisions", []):
                cat_name = CATEGORIES.get(cat_key, {}).get("name", cat_key)
                locked.append(f"[{cat_name}] {dec}")
        if locked:
            parts.append("LOCKED DECISIONS:\n" + "\n".join(locked))

        if not parts:
            return "Nothing dropped yet. Just start talking — about venue, budget, guests, anything — and I'll start building the picture."

        context = "\n\n".join(parts)
        prompt = f"""{context}

Give a catch-up brief across all wedding planning. Use exactly this structure — put --- on its own line between each section:

<b>What's Been Sorted</b>
Categories with real progress or confirmed decisions. One bullet per item using •

---

<b>What's In Motion</b>
Things discussed or being considered but not locked in. One bullet per item using •

---

<b>What's Untouched</b>
Wedding categories with nothing dropped yet. One bullet per item using •

---

<b>One Thing To Do Next</b>
The single most useful next action right now.

Use Telegram HTML formatting. <b> for headers only. No markdown."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=self._build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    async def priority_brief(self) -> str:
        all_drops = get_recent_drops(limit=150)
        all_decisions = get_all_memory()
        fin = payment_summary()

        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        two_weeks_ago = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

        by_category: dict[str, list] = {}
        for d in all_drops:
            cat = d.get("category") or "general"
            by_category.setdefault(cat, []).append(d)

        untouched = [
            CATEGORIES[c]["name"]
            for c in CATEGORIES
            if c not in by_category
        ]

        stale = [
            CATEGORIES[c]["name"]
            for c, drops in by_category.items()
            if c in CATEGORIES and all(d["ts"] < two_weeks_ago for d in drops)
        ]

        context_parts = []

        category_lines = ["PLANNING ACTIVITY BY CATEGORY:"]
        for cat_key, cat_info in CATEGORIES.items():
            drops = by_category.get(cat_key, [])
            recent = [d for d in drops if d["ts"] >= week_ago]
            if not drops:
                category_lines.append(f"\n{cat_info['name']}: nothing yet")
            else:
                category_lines.append(f"\n{cat_info['name']} ({len(drops)} drops, {len(recent)} this week):")
                for d in drops[-3:]:
                    category_lines.append(f"  {d['ts'][:10]}: {d['content'][:120]}")
        context_parts.append("\n".join(category_lines))

        if untouched:
            context_parts.append(f"COMPLETELY UNTOUCHED: {', '.join(untouched)}")

        if stale:
            context_parts.append(f"NO ACTIVITY IN 2+ WEEKS: {', '.join(stale)}")

        locked = []
        for cat_key, data in all_decisions.items():
            for dec in data.get("decisions", []):
                cat_name = CATEGORIES.get(cat_key, {}).get("name", cat_key)
                locked.append(f"[{cat_name}] {dec}")
        if locked:
            context_parts.append("CONFIRMED DECISIONS:\n" + "\n".join(locked))

        if fin["payments"]:
            context_parts.append(
                f"BUDGET LOGGED: {fin['total_paid']:,} paid/deposited"
                + (f", {fin['total_owing']:,} still owing" if fin["total_owing"] else "")
            )

        context = "\n\n".join(context_parts)

        prompt = f"""{context}

You are a proactive wedding planning coordinator. The couple gets this weekly briefing automatically — they haven't asked a question, you're initiating contact to keep them on track.

Analyse their planning data and generate an opinionated, prioritised action brief. Consider:
- Venue, photographer, catering, and entertainment book out fast — flag if these lack confirmed decisions
- Categories untouched or stale for 2+ weeks likely need a nudge
- Be specific: name the category, the gap, and the exact action they should take
- If something is going well, acknowledge it briefly

Use exactly this structure with --- between sections:

<b>This Week's Priorities</b>
2-3 specific tasks to tackle this week. Tell them exactly what to do — which vendors to contact, which decisions to lock in, what to research. Be direct.

---

<b>Don't Let This Slip</b>
Categories with no progress or that have gone quiet. Name the risk and the action. Venue, photographer, catering going unaddressed is urgent.

---

<b>Momentum Check</b>
1-2 lines on what's going well. Keep it brief.

---

<b>Blockers & Open Questions</b>
Key unresolved decisions that are holding up other planning. What needs to be decided before they can move forward elsewhere.

Use • for bullets. <b> tags for headers only. No markdown."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=self._build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


DAILY_SYSTEM_PROMPT = """You are a personal assistant managing tasks and reminders for a couple (Ansen and Jess). You handle their day-to-day tasks — both shared and personal.

PRIVACY RULES
- Private tasks belong only to the person who created them. Never reveal them to anyone else.
- Shared tasks (visibility: shared) are visible to both.

HOW TO RESPOND
- Be concise and direct
- When adding a task, confirm what you logged: the task, due date, and whether it's shared or personal
- Use Telegram HTML formatting: <b>bold</b> for emphasis, • for lists
- Never use asterisks, underscores, or markdown — HTML only
- Sound like a sharp personal assistant, not a robot

PARSING TASKS
- "remind me" / "my" / "I need to" → visibility: private
- "remind us" / "we need to" / "both" → visibility: shared
- Extract due dates from natural language: "tomorrow", "Friday", "next Monday", etc.
- If no date is given, store without a due date"""

TASK_PARSE_PROMPT = """Extract task details from this message and return JSON only.

Message: {message}
Today's date: {today}
Day of week: {weekday}
Available categories: {categories}

Return JSON with these fields:
{{
  "is_task": true or false,
  "is_new_category": false,
  "task": "clean task description",
  "due_date": "YYYY-MM-DD or null",
  "repeat": "none or daily or weekly",
  "visibility": "private or shared",
  "category": "slug from available categories or null"
}}

Rules:
- is_task: true if the message is asking to create a reminder or task
- is_new_category: true if the message is asking to add/create a new category (not a task)
- visibility: "shared" if message says "us", "we", "both" — otherwise "private"
- due_date: resolve relative dates using today's date provided; null if no date mentioned
- category: pick the best matching slug from available categories, or null if unclear"""


def _task_label(t: dict, today_str: str) -> str:
    icon = "👥" if t["visibility"] == "shared" else "🔒"
    due = t.get("due_date")
    if not due:
        date_str = "no date"
    elif due < today_str:
        date_str = f"overdue ({due})"
    elif due == today_str:
        date_str = "today"
    else:
        date_str = due
    return f"{icon} {t['task']} — {date_str}"


class DailyAgent:
    def __init__(self):
        self.client = AsyncAnthropic()

    async def _parse_task(self, text: str) -> dict | None:
        today = date.today()
        cats = get_all_categories()
        cat_list = ", ".join(f"{slug} ({v['emoji']} {v['name']})" for slug, v in cats.items())
        prompt = TASK_PARSE_PROMPT.format(
            message=text,
            today=today.isoformat(),
            weekday=today.strftime("%A"),
            categories=cat_list,
        )
        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
        except (json.JSONDecodeError, IndexError):
            return None

    async def handle_message(self, text: str, user_id: int, history: list[dict] | None = None) -> dict:
        if history is None:
            history = []

        parsed = await self._parse_task(text)

        if parsed and parsed.get("is_new_category"):
            # Extract name and optional emoji from the message using a quick parse
            import re
            emoji_match = re.search(r'[\U00010000-\U0010ffff☀-➿]', text)
            emoji = emoji_match.group(0) if emoji_match else "📌"
            # Strip common phrases to get the category name
            name_raw = re.sub(r"(?i)(add|create|new|a |an |category|called|named|for)\s*", " ", text).strip()
            name_raw = re.sub(r"[^\w\s]", "", name_raw).strip().title() or "New Category"
            add_custom_category(name=name_raw, emoji=emoji, created_by=user_id)
            reply = f"{emoji} Category <b>{name_raw}</b> added — you can now assign tasks to it."
            updated_history = history + [
                {"role": "user", "content": text},
                {"role": "assistant", "content": reply},
            ]
            return {"text": reply, "history": updated_history}

        if parsed and parsed.get("is_task"):
            due = None
            if parsed.get("due_date"):
                try:
                    due = date.fromisoformat(parsed["due_date"])
                except ValueError:
                    pass
            category = parsed.get("category") or detect_daily_category(text)
            add_task(
                user_id=user_id,
                task=parsed["task"],
                due_date=due,
                repeat=parsed.get("repeat", "none"),
                visibility=parsed.get("visibility", "private"),
                category=category,
            )
            cats = get_all_categories()
            cat_info = cats.get(category, {}) if category else {}
            cat_str = f" [{cat_info.get('emoji', '')} {cat_info.get('name', category)}]" if category else ""
            due_str = f" — due {parsed['due_date']}" if parsed.get("due_date") else ""
            shared_str = " (shared)" if parsed.get("visibility") == "shared" else ""
            reply = f"✅ Logged{cat_str}: {parsed['task']}{due_str}{shared_str}"
            updated_history = history + [
                {"role": "user", "content": text},
                {"role": "assistant", "content": reply},
            ]
            return {"text": reply, "history": updated_history}

        # General daily chat — show task context
        today_str = date.today().isoformat()
        tasks = get_tasks(user_id, include_done=False)
        task_lines = [_task_label(t, today_str) for t in tasks[:20]]
        context = "CURRENT TASKS:\n" + "\n".join(task_lines) if task_lines else "No open tasks."

        messages = history + [{"role": "user", "content": f"[Context]\n{context}\n\n[Message]\n{text}"}]
        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=DAILY_SYSTEM_PROMPT,
            messages=messages,
        )
        reply = response.content[0].text
        updated_history = messages + [{"role": "assistant", "content": reply}]
        if len(updated_history) > 40:
            updated_history = updated_history[-40:]
        return {"text": reply, "history": updated_history}

    async def daily_brief(self, user_id: int) -> str:
        today_str = date.today().isoformat()
        weekday = date.today().strftime("%A")
        data = get_all_tasks_for_brief(user_id)
        cats = get_all_categories()

        all_tasks = data["overdue"] + data["due_today"] + data["upcoming"] + data["no_date"]
        if not all_tasks:
            return "✅ Nothing on your task list. Add tasks by just telling me — \"remind me to X on Friday\"."

        parts = [f"TODAY IS {weekday.upper()}, {today_str}"]

        if data["overdue"]:
            lines = [f"  • {_task_label(t, today_str)}" for t in data["overdue"]]
            parts.append("OVERDUE:\n" + "\n".join(lines))

        if data["due_today"]:
            lines = [f"  • {_task_label(t, today_str)}" for t in data["due_today"]]
            parts.append("DUE TODAY:\n" + "\n".join(lines))

        if data["upcoming"]:
            lines = [f"  • {_task_label(t, today_str)}" for t in data["upcoming"][:10]]
            parts.append("COMING UP:\n" + "\n".join(lines))

        # Group remaining tasks by category
        by_cat: dict[str, list] = {}
        for t in all_tasks:
            cat = t.get("category") or "personal"
            by_cat.setdefault(cat, []).append(t)

        cat_lines = ["BY CATEGORY:"]
        for cat_slug, tasks in by_cat.items():
            info = cats.get(cat_slug, {"emoji": "📌", "name": cat_slug.title()})
            cat_lines.append(f"\n{info['emoji']} {info['name']}:")
            for t in tasks:
                cat_lines.append(f"  • {_task_label(t, today_str)}")
        parts.append("\n".join(cat_lines))

        context = "\n\n".join(parts)
        prompt = f"""{context}

Generate a sharp daily brief grouped by urgency then category. Structure:

<b>Today & Overdue</b>
Tasks due today plus any overdue. Flag overdue ones urgently. Skip if none.

---

<b>Coming Up</b>
Tasks due in the next 7 days. One bullet each. Skip if none.

---

<b>By Category</b>
All open tasks grouped by category. Use the category emoji and name as a sub-header.

Use • for bullets. <b> tags for headers only. No markdown. Keep it tight."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=DAILY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


DAILY_KEYWORDS = {
    "remind me", "remind us", "reminder", "don't forget", "dont forget",
    "remember to", "to-do", "todo", "task", "errand", "appointment",
    "meeting", "call ", "my tasks", "what's on", "whats on", "what do i",
    "schedule", "book ", "dentist", "doctor", "gym", "pick up", "drop off",
    "add a category", "create a category", "new category", "add category",
}


def route_intent(text: str) -> str:
    """Return 'wedding', 'daily', or 'both'."""
    lower = text.lower()
    is_daily = any(kw in lower for kw in DAILY_KEYWORDS)
    is_wedding = detect_category(text) is not None

    overview_phrases = ["what's on this week", "whats on this week", "what do i have", "weekly overview", "this week"]
    is_overview = any(p in lower for p in overview_phrases)

    if is_overview:
        return "both"
    if is_daily and not is_wedding:
        return "daily"
    return "wedding"


# ---------------------------------------------------------------------------
# Unified agentic agent
# ---------------------------------------------------------------------------

UNIFIED_SYSTEM_PROMPT = """You are a personal assistant for Ansen and Jess. You help with two things:

1. WEDDING PLANNING — they drop notes, screenshots, quotes, and discussions. You track everything by category and help them make decisions, spot gaps, and keep moving.

2. DAILY LIFE — personal tasks, reminders, and shared to-dos for everyday life.

WEDDING CATEGORIES
{categories}

DAILY TASK CATEGORIES
finance 💳, health 🏥, home 🏠, work 💼, social 🎉, travel ✈️, personal 🙋 — plus any custom ones they've created.

PRIVACY RULE
Tasks with visibility "private" belong only to the person who created them. Never reveal private tasks from one person to the other. Tasks with visibility "shared" are visible to both.

TODAY'S DATE: {today}

WHAT YOU KNOW ABOUT THIS PERSON
{user_summary}

PEOPLE
- Ansen: user_id 63756531
- Jess / Jessica: user_id 6927468999

HOW TO USE TOOLS
- Always fetch context with tools before answering — never guess from memory
- Incoming wedding message → call log_wedding_drop to save it, then respond
- Wedding questions → read_wedding_drops (filter by category when relevant)
- Task / reminder → add_daily_task
- Budget/spending → read_payments + read_wedding_drops("budget")
- "what should I do" / "what's on" → call both read_wedding_drops and read_daily_tasks, synthesise one answer
- Adding a task about a wedding vendor → read relevant drops first, bake context into the task description
- New category request → add_custom_category
- Decisions / confirmed bookings → read_memory
- "what's on the calendar" / "what's happening this week" → read_calendar
- "book", "schedule", "add to calendar" → create_calendar_event
- "cancel", "remove from calendar" → delete_calendar_event (read_calendar first to get the event ID)

WHEN TO LOG VS NOT LOG
- log_wedding_drop: wedding venues, vendors, budget, guests, catering, decor, attire, ceremony, photography, honeymoon
- Do NOT log: personal tasks, daily life, health, manicures, errands, anything clearly not about the wedding

HOW TO RESPOND
- Be concise and practical — reference specific details from what they've shared
- Cross-reference both brains naturally — no need to label responses as "Wedding Brain" or "Daily Brain"
- Use Telegram HTML: <b>bold</b> for headers, • for bullets
- Never use asterisks, underscores, or markdown — HTML only
- Sound like a sharp friend who knows everything they've told you"""

TOOLS = [
    {
        "name": "log_wedding_drop",
        "description": "Save a wedding-relevant message as a shared drop. ONLY call this when the message is clearly about wedding planning — venues, budget, catering, guests, photography, decor, attire, ceremony, vendors, timeline, honeymoon. Do NOT call for personal tasks, daily life, or anything private.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Category slug: venue, budget, guests, catering, photography, decor, entertainment, attire, ceremony, logistics, vendors, timeline, honeymoon"},
                "content": {"type": "string", "description": "The message content to log"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "read_wedding_drops",
        "description": "Read wedding planning notes, messages and screenshots stored by the couple. Use for any wedding-related question.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Filter by category slug: venue, budget, guests, catering, photography, decor, entertainment, attire, ceremony, logistics, vendors, timeline, honeymoon. Omit for recent drops across all.",
                },
                "limit": {"type": "integer", "description": "Max results. Default 40."},
            },
        },
    },
    {
        "name": "read_daily_tasks",
        "description": "Read open tasks and reminders for the current user — their private tasks plus all shared tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "include_done": {"type": "boolean", "description": "Include completed tasks. Default false."},
            },
        },
    },
    {
        "name": "add_daily_task",
        "description": "Create a new task or reminder for the current user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Clear task description"},
                "due_date": {"type": "string", "description": "Due date YYYY-MM-DD, or null"},
                "visibility": {
                    "type": "string",
                    "enum": ["private", "shared"],
                    "description": "private = only this user sees it. shared = both see it. Infer from me/I (private) vs us/we/both (shared).",
                },
                "category": {"type": "string", "description": "Category slug: finance, health, home, work, social, travel, personal, or a custom slug"},
                "repeat": {"type": "string", "enum": ["none", "daily", "weekly"]},
                "assigned_to": {"type": "integer", "description": "User ID to assign this task to. Use when the sender says 'remind Jess to X' or 'remind Ansen to X'. Ansen=63756531, Jess=6927468999."},
            },
            "required": ["task", "visibility"],
        },
    },
    {
        "name": "read_payments",
        "description": "Read wedding payment records — deposits paid, amounts owing, vendor costs.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_memory",
        "description": "Read locked wedding decisions and saved notes by category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Category slug to filter. Omit for all."},
            },
        },
    },
    {
        "name": "add_custom_category",
        "description": "Create a new custom daily task category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Category name"},
                "emoji": {"type": "string", "description": "Single emoji"},
            },
            "required": ["name", "emoji"],
        },
    },
    {
        "name": "read_calendar",
        "description": "Read upcoming events from the shared Google Calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "How many days ahead to look. Default 7."},
            },
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Create an event on the shared Google Calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "Start time in ISO 8601 format e.g. 2026-06-07T14:00:00"},
                "end": {"type": "string", "description": "End time in ISO 8601 format e.g. 2026-06-07T15:00:00"},
                "description": {"type": "string", "description": "Optional event description"},
                "location": {"type": "string", "description": "Optional location"},
            },
            "required": ["title", "start", "end"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": "Delete or cancel an event from the shared Google Calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The event ID from read_calendar"},
            },
            "required": ["event_id"],
        },
    },
]


class UnifiedAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self._wedding = WeddingAgent()
        self._daily = DailyAgent()

    def _build_system(self, user_summary: str = "") -> str:
        cat_lines = "\n".join(
            f"- {v['emoji']} {k}: {v['name']} — {v['description']}"
            for k, v in CATEGORIES.items()
        )
        return UNIFIED_SYSTEM_PROMPT.format(
            categories=cat_lines,
            today=date.today().isoformat(),
            user_summary=user_summary or "Nothing yet — this is the start of our history together.",
        )

    async def _execute_tool(self, name: str, inputs: dict, user_id: int):
        if name == "log_wedding_drop":
            category = inputs.get("category") or detect_category(inputs["content"])
            drop(category, "text", inputs["content"], user_id)
            self._logged_wedding_drop = True
            return {"status": "logged", "category": category}

        if name == "read_wedding_drops":
            category = inputs.get("category")
            limit = inputs.get("limit", 40)
            drops = get_drops(category=category, limit=limit) if category else get_recent_drops(limit=limit)
            return [{"ts": d["ts"][:10], "category": d.get("category"), "kind": d["kind"], "content": d["content"]} for d in drops]

        if name == "read_daily_tasks":
            tasks = get_tasks(user_id, include_done=inputs.get("include_done", False))
            return [{"id": str(t["id"]), "task": t["task"], "due_date": t.get("due_date"), "category": t.get("category"), "visibility": t["visibility"], "done": t["done"]} for t in tasks]

        if name == "add_daily_task":
            due = None
            if inputs.get("due_date"):
                try:
                    due = date.fromisoformat(inputs["due_date"])
                except ValueError:
                    pass
            assigned_to = inputs.get("assigned_to")
            task = add_task(
                user_id=user_id,
                task=inputs["task"],
                due_date=due,
                repeat=inputs.get("repeat", "none"),
                visibility=inputs.get("visibility", "private"),
                category=inputs.get("category"),
                assigned_to=assigned_to,
            )
            return {"status": "created", "id": str(task["id"]), "task": inputs["task"], "assigned_to": assigned_to}

        if name == "read_payments":
            fin = payment_summary()
            return {
                "total_paid": fin["total_paid"],
                "total_owing": fin["total_owing"],
                "by_person": fin["by_person"],
                "payments": [{"vendor": p.get("vendor"), "amount": p.get("amount"), "currency": p.get("currency"), "status": p.get("status"), "paid_by": p.get("paid_by")} for p in fin["payments"]],
            }

        if name == "read_memory":
            category = inputs.get("category")
            return {category: get_category_memory(category)} if category else get_all_memory()

        if name == "add_custom_category":
            result = add_custom_category(name=inputs["name"], emoji=inputs["emoji"], created_by=user_id)
            return {"status": "created", "slug": result["slug"], "name": result["name"]}

        if name == "read_calendar":
            return get_events(days_ahead=inputs.get("days_ahead", 7))

        if name == "create_calendar_event":
            return create_event(
                title=inputs["title"],
                start=inputs["start"],
                end=inputs["end"],
                description=inputs.get("description", ""),
                location=inputs.get("location", ""),
            )

        if name == "delete_calendar_event":
            ok = delete_event(inputs["event_id"])
            return {"status": "deleted" if ok else "not_found"}

        return {"error": f"Unknown tool: {name}"}

    async def handle_message(self, text: str, user_id: int, history: list[dict] | None = None) -> dict:
        if history is None:
            history = []

        self._logged_wedding_drop = False
        user_summary = get_summary(user_id)
        messages = history + [{"role": "user", "content": text}]

        for _ in range(10):
            response = await self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=self._build_system(user_summary),
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                reply = next((b.text for b in response.content if hasattr(b, "text")), "")
                messages.append({"role": "assistant", "content": reply})
                updated_history = messages[-40:]

                # Compress in background every 6 messages
                msg_count = get_message_count(user_id) + 1
                if msg_count % 6 == 0:
                    asyncio.create_task(
                        self._compress_and_save(user_id, updated_history, user_summary, msg_count)
                    )
                else:
                    save_summary(user_id, user_summary, msg_count)

                return {"text": reply, "history": updated_history, "notify_partner": self._logged_wedding_drop}

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = await self._execute_tool(block.name, block.input, user_id)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str),
                        })
                messages.append({"role": "user", "content": tool_results})

        return {"text": "Something went wrong, try again.", "history": history}

    async def _compress_and_save(self, user_id: int, messages: list, existing_summary: str, message_count: int):
        lines = []
        for m in messages[-20:]:
            role = m["role"]
            content = m["content"] if isinstance(m["content"], str) else "[tool exchange]"
            lines.append(f"{role}: {content[:300]}")

        prompt = f"""Existing summary:
{existing_summary or "(none yet)"}

Recent conversation:
{chr(10).join(lines)}

Update the summary to capture:
- How this person communicates (style, tone, directness, preferences)
- What they're currently focused on or working through
- Recurring topics, habits, or concerns
- Personal context they've shared (family, pets, work, interests)
- How they like to use this assistant

Be concise (under 300 words). Write in third person. This will be loaded as context at the start of every future conversation."""

        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            save_summary(user_id, response.content[0].text, message_count)
        except Exception:
            pass  # compression failure is non-critical

    async def handle_image(self, image_bytes: bytes, caption: str, user_id: int, history: list[dict] | None = None) -> dict:
        result = await self._wedding.handle_image(image_bytes=image_bytes, caption=caption, history=history or [])
        return result

    # Command methods — delegate to existing agents
    async def bring_me_up_to_speed(self) -> str:
        return await self._wedding.bring_me_up_to_speed()

    async def category_status(self, category: str) -> str:
        return await self._wedding.category_status(category)

    async def priority_brief(self) -> str:
        return await self._wedding.priority_brief()

    async def daily_brief(self, user_id: int) -> str:
        return await self._daily.daily_brief(user_id)
