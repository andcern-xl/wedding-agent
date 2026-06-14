import asyncio
import base64
import json
import os
import re
from html import escape as _html_escape
from datetime import datetime, date, timezone, timedelta
from anthropic import AsyncAnthropic
from categories import CATEGORIES, detect_category
from tools.memory import get_all_memory, get_category_memory
from tools.google_docs import fetch_docs_for_category, extract_doc_id
from tools.log import get_drops, get_recent_drops, drop
from tools.payments import add_payment, summary as payment_summary
from tools.daily import add_task, get_all_tasks_for_brief, get_tasks, get_completed_today, complete_task, get_task_by_id
from tools.notifications import schedule_notification as _sched_notif, list_notifications as _list_notifs, cancel_notification as _cancel_notif
from tools.fyis import log_fyi, get_fyis, get_fyis_today
from tools.daily_categories import get_all_categories, add_custom_category, detect_daily_category, BUILT_IN_CATEGORIES
from tools.user_memory import get_summary, save_summary, get_message_count, get_shared_summary, append_shared_summary
from tools.gcal import get_events, create_event, delete_event
from tools.search import web_search
from tools.gmail import get_emails
from tools.baby import pregnancy_summary, upcoming_milestones

_TELEGRAM_ALLOWED_TAGS = re.compile(
    r'<(?!/?(b|i|u|s|code|pre|a)(?:\s[^>]*)?>)',
    re.IGNORECASE,
)


def _fix_md(text: str) -> str:
    """Convert stray markdown to Telegram HTML and strip non-Telegram tags."""
    # **bold** → <b>bold</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    # __bold__ → <b>bold</b>
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text, flags=re.DOTALL)
    # _italic_ → <i>italic</i>
    text = re.sub(r'(?<!\w)_([^_\n]+?)_(?!\w)', r'<i>\1</i>', text)
    # Strip any HTML tags that Telegram doesn't support (e.g. <zen:*>, <div>, <p>, <br>)
    # Strategy: escape < that starts an unsupported tag into &lt;
    def _escape_bad_tag(m: re.Match) -> str:
        return '&lt;' + m.group(0)[1:]
    text = re.sub(r'<(?!/?(b|i|u|s|code|pre|a)(\s[^>]*)?>)(?=[^>]*>)', _escape_bad_tag, text, flags=re.IGNORECASE)
    return text


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
        try:
            drops = get_drops(category=category, limit=40) if category else get_recent_drops(limit=30)
        except Exception:
            drops = []
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
        try:
            drops = get_drops(category=category, limit=30) if category else get_recent_drops(limit=20)
        except Exception:
            drops = []
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
            try:
                add_payment(payment)
            except Exception:
                pass  # don't let payment logging kill the reply
            try:
                status_label = {"paid": "paid", "deposit": "deposit paid", "owing": "still owed", "quote": "quoted"}.get(payment.get("status", ""), payment.get("status", ""))
                currency = payment.get("currency", "")
                raw_amount = payment.get("amount", "")
                amount_str = f"{int(float(raw_amount)):,}" if raw_amount != "" else ""
                vendor = payment.get("vendor", "")
                paid_by = payment.get("paid_by")
                by_str = f" by {paid_by}" if paid_by else ""
                suffix = f"\n\n💰 Logged: {currency} {amount_str} {status_label}{by_str} — {vendor}"
            except Exception:
                suffix = "\n\n💰 Payment details logged."

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
        return f"{header}\n\n{_fix_md(response.content[0].text)}"

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
        return _fix_md(response.content[0].text)

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

Use • for bullets. <b> tags for headers only. Emojis welcome. NEVER use **asterisks** — Telegram parse_mode=HTML renders them as literal characters."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=self._build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        return _fix_md(response.content[0].text)


DAILY_SYSTEM_PROMPT = """You are a personal assistant managing tasks and reminders for a couple (Ansen and Jess). You handle their day-to-day tasks — both shared and personal.

PRIVACY RULES
- Private tasks belong only to the person who created them. Never reveal them to anyone else.
- Shared tasks (visibility: shared) are visible to both.

HOW TO RESPOND
- Be concise and direct
- When adding a task, confirm what you logged: the task, due date, and whether it's shared or personal
- Sound like a sharp personal assistant, not a robot

FORMATTING — CRITICAL
Telegram uses parse_mode=HTML. **Asterisks are NOT bold** — they show as literal * characters. Always use:
- <b>text</b> for bold/headers (never **text**)
- • for bullets (never - or *)
- Emojis freely: ✅ done, 🚨 overdue, 📅 date, 💪 task added, 🔔 reminder set

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

        data["overdue"].sort(key=lambda t: t.get("due_date", ""))
        data["upcoming"].sort(key=lambda t: t.get("due_date", ""))
        data["no_date"].sort(key=lambda t: t["task"].lower())

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

Use • for bullets. <b> tags for headers only. Emojis welcome. NEVER use **asterisks** — Telegram renders them as literal characters, not bold. Keep it tight."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=DAILY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return _fix_md(response.content[0].text)

    async def combined_daily_brief(self, user_ids: list[int], user_names: dict[int, str] | None = None) -> tuple:
        """Generate one combined daily brief for all users, sent to both.
        Returns (text, open_tasks) where open_tasks is the full deduped task list."""
        today_str = date.today().isoformat()
        weekday = date.today().strftime("%A")
        cats = get_all_categories()
        names = user_names or {}

        # Collect all tasks across users, deduplicating shared ones
        seen_ids: set = set()
        merged: list[dict] = []
        for uid in user_ids:
            for t in get_tasks(uid, include_done=False):
                if t["id"] in seen_ids:
                    continue
                seen_ids.add(t["id"])
                t = dict(t)
                if t["visibility"] == "shared":
                    t["_owner"] = "shared"
                else:
                    t["_owner"] = names.get(uid, str(uid))
                merged.append(t)

        overdue = sorted([t for t in merged if t.get("due_date") and t["due_date"] < today_str], key=lambda t: t["due_date"])
        due_today = [t for t in merged if t.get("due_date") == today_str]
        upcoming = sorted([t for t in merged if t.get("due_date") and t["due_date"] > today_str], key=lambda t: t["due_date"])
        no_date = sorted([t for t in merged if not t.get("due_date")], key=lambda t: t["task"].lower())

        # Google Calendar events
        try:
            events = await asyncio.to_thread(get_events, 7)
        except Exception:
            events = []

        parts = [f"TODAY IS {weekday.upper()}, {today_str}"]

        if events:
            ev_lines = []
            for e in events[:10]:
                start = e["start"]
                if "T" in start:
                    try:
                        dt = datetime.fromisoformat(start)
                        start = dt.strftime("%-d %b %H:%M")
                    except ValueError:
                        pass
                ev_lines.append(f"  • {start} — {e['title']}")
            parts.append("CALENDAR (next 7 days):\n" + "\n".join(ev_lines))

        def _owner_label(t: dict) -> str:
            owner = t.get("_owner", "")
            if owner and owner != "shared":
                return f" [{owner}]"
            return ""

        if overdue:
            lines = [f"  • {_task_label(t, today_str)}{_owner_label(t)}" for t in overdue]
            parts.append("OVERDUE:\n" + "\n".join(lines))

        if due_today:
            lines = [f"  • {_task_label(t, today_str)}{_owner_label(t)}" for t in due_today]
            parts.append("DUE TODAY:\n" + "\n".join(lines))

        if upcoming:
            lines = [f"  • {_task_label(t, today_str)}{_owner_label(t)}" for t in upcoming[:10]]
            parts.append("COMING UP:\n" + "\n".join(lines))

        if no_date:
            by_cat: dict[str, list] = {}
            for t in no_date:
                cat = t.get("category") or "personal"
                by_cat.setdefault(cat, []).append(t)
            cat_lines = ["NO DATE:"]
            for cat_slug, tasks in by_cat.items():
                info = cats.get(cat_slug, {"emoji": "📌", "name": cat_slug.title()})
                cat_lines.append(f"\n{info['emoji']} {info['name']}:")
                for t in tasks:
                    cat_lines.append(f"  • {_task_label(t, today_str)}{_owner_label(t)}")
            parts.append("\n".join(cat_lines))

        if not merged and not events:
            return "✅ Nothing on the list today. Add tasks by telling me — \"remind us to X on Friday\".", []

        context = "\n\n".join(parts)
        person_list = " and ".join(names.values()) if names else "both of you"
        prompt = f"""{context}

Generate a sharp combined daily brief for {person_list}. Structure:

<b>📅 Today on the Calendar</b>
Calendar events today and the next few days. One line each with date/time. Skip if none.

---

<b>Today & Overdue</b>
Tasks due today plus any overdue. Flag overdue ones urgently. Where tasks belong to one person, note their name in brackets. Skip if none.

---

<b>Coming Up</b>
Tasks due in the next 7 days. One bullet each. Skip if none.

---

<b>On the List</b>
Undated open tasks grouped by category. Use category emoji and name as sub-header. Note whose task it is where relevant.

Use • for bullets. <b> tags for headers only. Emojis welcome. NEVER use **asterisks** — Telegram renders them as literal characters, not bold. Keep it tight."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            system=DAILY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return _fix_md(response.content[0].text), merged

    async def evening_brief(self, user_ids: list[int], user_names: dict[int, str] | None = None) -> str:
        """End-of-day recap: what was done today, what's coming tomorrow."""
        today_str = date.today().isoformat()
        tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
        tomorrow_weekday = (date.today() + timedelta(days=1)).strftime("%A")
        names = user_names or {}

        # Completed tasks today — deduplicated
        seen_ids: set = set()
        completed: list[dict] = []
        for uid in user_ids:
            for t in get_completed_today(uid):
                if t["id"] in seen_ids:
                    continue
                seen_ids.add(t["id"])
                t = dict(t)
                t["_owner"] = "shared" if t["visibility"] == "shared" else names.get(uid, str(uid))
                completed.append(t)

        # Tomorrow's tasks — deduplicated
        seen_ids = set()
        tomorrow_tasks: list[dict] = []
        for uid in user_ids:
            for t in get_tasks(uid, include_done=False):
                if t["id"] in seen_ids:
                    continue
                if t.get("due_date") != tomorrow_str:
                    continue
                seen_ids.add(t["id"])
                t = dict(t)
                t["_owner"] = "shared" if t["visibility"] == "shared" else names.get(uid, str(uid))
                tomorrow_tasks.append(t)

        # Wedding drops today
        today_drops = [d for d in get_recent_drops(limit=30) if d["ts"][:10] == today_str]

        # FYIs shared today
        today_fyis = get_fyis_today()

        # Calendar events tomorrow
        try:
            all_events = await asyncio.to_thread(get_events, 2)
            tomorrow_events = [e for e in all_events if e.get("start", "").startswith(tomorrow_str)]
        except Exception:
            tomorrow_events = []

        parts = [f"END OF DAY — {today_str}"]

        def _owner_label(t: dict) -> str:
            owner = t.get("_owner", "")
            return f" [{owner}]" if owner and owner != "shared" else ""

        if completed:
            lines = [f"  ✓ {t['task']}{_owner_label(t)}" for t in completed]
            parts.append("COMPLETED TODAY:\n" + "\n".join(lines))

        if today_drops:
            lines = []
            for d in today_drops[:8]:
                cat = f"[{d['category']}] " if d.get("category") else ""
                lines.append(f"  • {cat}{d['content'][:120]}")
            parts.append("WEDDING NOTES TODAY:\n" + "\n".join(lines))

        if today_fyis:
            lines = []
            for f in today_fyis:
                owner = names.get(f["user_id"], str(f["user_id"]))
                cat = f"[{f['category']}] " if f.get("category") else ""
                lines.append(f"  • {owner}: {cat}{f['content'][:120]}")
            parts.append("FYIS SHARED TODAY:\n" + "\n".join(lines))

        if tomorrow_tasks or tomorrow_events:
            lines = []
            for e in tomorrow_events:
                start = e["start"]
                if "T" in start:
                    try:
                        start = datetime.fromisoformat(start).strftime("%-I:%M %p")
                    except ValueError:
                        pass
                lines.append(f"  📅 {start} — {e['title']}")
            for t in tomorrow_tasks:
                lines.append(f"  • {t['task']}{_owner_label(t)}")
            parts.append(f"TOMORROW ({tomorrow_weekday.upper()}):\n" + "\n".join(lines))

        context = "\n\n".join(parts)
        person_list = " and ".join(names.values()) if names else "both of you"

        prompt = f"""{context}

Generate a concise end-of-day recap for {person_list}. Structure:

<b>✅ Done today</b>
Tasks completed today. If nothing was done, say so in one line.

---

<b>💬 FYIs</b>
Updates and info shared today by each person — what your partner wants you to know. Show who shared each one in brackets e.g. [Ansen]. Skip this section entirely if none.

---

<b>💒 Wedding today</b>
Any wedding notes or updates dropped today. Skip this section entirely if none.

---

<b>📅 Tomorrow</b>
Calendar events (with times) and tasks due tomorrow. Skip this section entirely if nothing.

Use • for bullets. <b> tags for headers only. Emojis welcome. NEVER use **asterisks** — Telegram renders them as literal characters, not bold. Keep it tight."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=DAILY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return _fix_md(response.content[0].text)


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
CURRENT TIMEZONE: {timezone}

WHAT YOU KNOW ABOUT THIS PERSON
{user_summary}

Read this before every response. Use it to:
- Match their communication style (length, tone, formality)
- Reference things they've shared without them having to repeat themselves
- Anticipate what they probably want based on their patterns
- Skip explanations they don't need
If it contains a PREFERENCES section, follow those as standing orders.

SHARED BRAIN — what Ansen and Jess have told you together (visible in both their conversations):
{shared_summary}

PEOPLE
- Ansen: user_id 63756531
- Jess / Jessica: user_id 6927468999

PROACTIVE NOTIFICATIONS — YOU CAN DO THIS
You are running inside a Telegram bot with a job queue. You CAN send messages at specific times. When someone asks for a time-based reminder, call schedule_notification — a background job fires it automatically at the right moment. Never tell the user you can't send proactive messages or push alerts. You can. Use the tool.

PROACTIVE LONG-TERM REMINDERS — DO THIS WITHOUT BEING ASKED
When someone mentions any of these, immediately schedule a follow-up reminder 1 year out (or the appropriate interval) WITHOUT waiting to be asked:
- Pet vaccinations / vet visits / parasite treatment → 1 year follow-up: "Time for Lucille's [vaccine] again — want me to book the vet?"
- Human health checkups, dental, bloodwork, eye tests → 1 year: "You're due for your [checkup] — want to book?"
- Insurance renewals, subscriptions, annual fees → schedule 1 month before renewal: "Your [thing] renews next month — anything to action?"
- Annual events the couple wants to revisit (yearly trips, anniversaries, traditions)
Tell them you've done it: "I've set a reminder for [date] to check in about [thing]." Keep it in one line, casual.

HOW TO USE TOOLS
- Always fetch context with tools before answering — never guess from memory
- Incoming wedding message → call log_wedding_drop to save it, then respond
- Wedding questions → read_wedding_drops (filter by category when relevant)
- Task / date-based reminder (no specific clock time) → add_daily_task — appears in the morning brief
- Reminder with a specific time ("at 3pm", "in 2 hours", "tonight at 8", "tomorrow at noon") → schedule_notification — fires as a Telegram push at that exact moment. NEVER use add_daily_task for these.
- Budget/spending → read_payments + read_wedding_drops("budget")
- "what should I do" / "what's on" → call both read_wedding_drops and read_daily_tasks, synthesise one answer
- Adding a task about a wedding vendor → read relevant drops first, bake context into the task description. Always set category="wedding" so it stays out of the daily reminders list.
- New category request → add_custom_category
- Decisions / confirmed bookings → read_memory
- "what's on the calendar" / "what's happening this week" → read_calendar
- "book", "schedule", "add to calendar" → create_calendar_event
- "cancel", "remove from calendar" → delete_calendar_event (read_calendar first to get the event ID)
- "what reminders are scheduled" → list_notifications
- "cancel that reminder" → cancel_notification (list_notifications first to get the ID)
- Shared update / past-tense info / "FYI" / "just so you know" / "heads up" / completed action → log_fyi (not add_daily_task)
- "any FYIs?" / "what did we share recently?" → read_fyis
- "going forward always do X" / "remember that I prefer X" / "from now on X" → save_preference (this persists across sessions)
- "search for", "look up", "find X", "what's the weather", "what is X", "who is X", any real-time or internet question → search_web first, then answer with real results. NEVER say you can't search — you have the search_web tool.
- Vendor recommendations / price research / "find X in Y" / "what does X cost" / any question needing current market info → search_web first, then answer with real results
- Couple-level decision or fact that both should always know ("we're going with X vendor", "we decided on Y", "Jess rescheduled the venue tour") → save_shared_context — this lives in both their prompts every message, not just queryable on demand

TOOL ERRORS — BE HONEST
If a tool returns {{"error": "..."}}, tell the user it failed. Never claim success when a tool errored. Say what failed and suggest they try again or check the setup.

WHEN TO LOG VS NOT LOG
- log_wedding_drop: wedding venues, vendors, budget, guests, catering, decor, attire, ceremony, photography, honeymoon — ALL wedding notes go here as the permanent archive
- Do NOT log: personal tasks, daily life, health, manicures, errands, anything clearly not about the wedding

SHARED BRAIN — for confirmed wedding decisions only, not raw notes:
Call save_shared_context (in addition to log_wedding_drop) when something is CONFIRMED or DECIDED — not when it's being explored.
Use it for: "we booked X", "we're going with Y", "venue confirmed for date Z", "guest cap is N", "budget agreed at $X".
Do NOT use it for vendor quotes, early discussions, maybes, or info that's only useful if someone asks.
Keep entries to one clear sentence — the shared brain is injected into every API call and must stay lean.

TASK vs FYI — infer from intent, not keywords
Ask: does this need to be done, or is it sharing something?

log_fyi when:
- Past tense / completed action: "I paid the bill", "I booked the restaurant", "I called the vet"
- Status update: "I'm running late", "the plumber is coming at 3", "the package arrived"
- News or information: "the vet called, results were fine", "the venue confirmed our date"
- Sharing context: "the caterer raised their prices", "Mum is arriving Friday"

add_daily_task when:
- Future action still to be done: "remind me to call", "we need to book", "don't forget to pay"
- Request directed at the other person: "can you follow up with the venue?", "Jess can you call the florist?"
- Anything that would sit on a to-do list

When in doubt: if it's something that already happened or is just good to know → FYI. If it needs someone to act → task.

TASK QUALITY RULES — enforce these strictly:
- Task names must be SHORT (under 80 chars). The action only — not the backstory. If you need to include context, log it as an FYI or wedding drop separately, then create a short task.
  WRONG: "Look into getting an OCBC credit card (any card) so you don't lose points. When ready, transfer $10k..."
  RIGHT: "Look into OCBC credit card for points"
- Social events / dinners with a confirmed date and time → create_calendar_event, NOT add_daily_task
- Package trackers / running balances ("10 manicure sessions, 7 remaining") → save to personal summary via save_preference, NOT add_daily_task
- Facts or preferences about either person ("Jess likes kaya waffle", "Ansen prefers window seats", "Jess is allergic to X") → save_preference for that person, NEVER add_daily_task or log_fyi. These are memory, not tasks.
- Items someone already owns or knows about ("AirPods are in the car") → log_fyi, NOT add_daily_task
- NEVER create a task that starts with "FYI" — that is always a log_fyi call
- If a statement describes a fact, trait, or preference about Ansen or Jess with no action required → save_preference, full stop. Do not create a task.

HOW TO RESPOND
- Be concise and practical — reference specific details from what they've shared
- Cross-reference both brains naturally — no need to label responses as "Wedding Brain" or "Daily Brain"
- Sound like a sharp friend who knows everything they've told you

ORDERING
- Calendar events: always list chronologically (soonest first)
- Tasks with dates: chronological (earliest first)
- Tasks/items without dates: alphabetical by name
- When creating multiple events in one response, confirm them in date order

FORMATTING — THIS IS CRITICAL
Telegram uses parse_mode=HTML. **Asterisks and underscores are NOT rendered** — they show up as literal characters. You MUST use HTML tags.
- Bold/headers: <b>text</b> ONLY — never **text**
- Bullets: • (not - or *)
- Sections: blank line between each section
- Emojis: use freely and naturally — 💍 wedding topics, 💰 money/budget, 📸 photography, 🏨 venue, 🚨 overdue/urgent, ✅ done, 📅 calendar, 🎉 parties/social, 💪 tasks, 🔥 time-sensitive
- Example of correct formatting:
  🚨 <b>Overdue</b>
  • OpenTable writeup — due yesterday

NOT this (wrong — asterisks show as raw text):
  **Overdue task** — OpenTable writeup"""

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
        "description": "Create a date-based task or reminder. Use for to-dos with a due date (or no date). Do NOT use when the user gives a specific clock time ('at 3pm', 'in 2 hours') — use schedule_notification for those instead.",
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
    {
        "name": "log_fyi",
        "description": "Save a shared FYI — information one partner wants the other to know, not an action item. Use for past-tense updates, status shares, 'heads up' messages. Examples: 'FYI I paid the electricity bill', 'just letting you know I booked a table', 'heads up I'll be home late'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The FYI content"},
                "category": {"type": "string", "description": "Optional category: finance, health, home, work, social, travel, personal"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "read_fyis",
        "description": "Read recent shared FYIs — updates and info shared between Ansen and Jess.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max results. Default 20."},
            },
        },
    },
    {
        "name": "schedule_notification",
        "description": "Schedule a Telegram message to be sent at a specific time. Use when the user says things like 'remind me at 3pm', 'notify me at...', 'send me a message tonight at X'. Supports daily/weekly recurrence.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The message text to send"},
                "scheduled_at": {"type": "string", "description": "When to send — ISO 8601 datetime with timezone offset e.g. 2026-06-03T15:00:00+08:00"},
                "recurrence": {"type": "string", "enum": ["none", "daily", "weekly"], "description": "Repeat cadence. Default none."},
                "for_all_users": {"type": "boolean", "description": "If true, send to both Ansen and Jess. Default false (only the current user)."},
            },
            "required": ["message", "scheduled_at"],
        },
    },
    {
        "name": "list_notifications",
        "description": "List the current user's upcoming scheduled notifications.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_notification",
        "description": "Cancel a scheduled notification by its ID. Call list_notifications first to get the ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "notification_id": {"type": "string", "description": "The notification ID from list_notifications"},
            },
            "required": ["notification_id"],
        },
    },
    {
        "name": "mark_task_done",
        "description": "Mark an open task as completed. Use this proactively: after booking a calendar event, paying a vendor, or taking any action that fulfills an open task — call read_daily_tasks, find the matching task by ID, then call mark_task_done. Also use when the user says 'I did X', 'done', 'I've booked X', 'I paid X' and there's a matching open task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID from read_daily_tasks"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "save_preference",
        "description": "Persist a fact, preference, or behavioural instruction about either person to their personal memory. Use for: (1) assistant instructions — 'going forward always do X', 'from now on X'; (2) personal facts — 'Jess likes kaya waffle', 'Ansen prefers window seats', 'Jess is allergic to X', 'Ansen's gym is X'. Anything describing a person's traits, preferences, or habits goes here — NOT into tasks or FYIs. This is permanent memory, loaded every session.",
        "input_schema": {
            "type": "object",
            "properties": {
                "preference": {"type": "string", "description": "The preference to remember, written as a clear instruction e.g. 'Always add to Google Calendar immediately without asking for confirmation'"},
            },
            "required": ["preference"],
        },
    },
    {
        "name": "search_web",
        "description": "Search the internet for current information. Use automatically when the user asks about vendors, prices, options, availability, or anything that would benefit from up-to-date information — e.g. 'find photographers in Singapore', 'what's a reasonable budget for catering', 'florists near KL'. Also use when answering wedding planning questions that benefit from current market info.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query — be specific, include location and price range where relevant"},
                "num_results": {"type": "integer", "description": "Number of results. Default 5, max 10."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "save_shared_context",
        "description": "Save a confirmed fact or decision to the shared brain — injected into BOTH Ansen's and Jess's prompts every single message. Use ONLY for confirmed/decided things: 'we booked X', 'venue confirmed', 'guest cap agreed at N', 'going with vendor Y'. Do NOT use for quotes, maybes, or info that's only useful if asked — those go in log_wedding_drop instead. Keep it to one clear sentence. This should be called IN ADDITION TO log_wedding_drop for wedding decisions, not instead of it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The shared fact or decision to remember. One clear sentence."},
            },
            "required": ["content"],
        },
    },
]


class UnifiedAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self._wedding = WeddingAgent()
        self._daily = DailyAgent()

    _USER_NAMES = {63756531: "Ansen", 6927468999: "Jess"}

    def _build_system(self, user_summary: str = "", shared_summary: str = "", user_id: int = 0) -> str:
        cat_lines = "\n".join(
            f"- {v['emoji']} {k}: {v['name']} — {v['description']}"
            for k, v in CATEGORIES.items()
        )
        import os
        current_name = self._USER_NAMES.get(user_id, "the user")
        other_name = next((n for uid, n in self._USER_NAMES.items() if uid != user_id), "the other person")
        current_user_line = f"CURRENT USER: You are talking to {current_name}. Address them as \"you\". Never refer to them in third person. The other person is {other_name}."
        return UNIFIED_SYSTEM_PROMPT.format(
            categories=cat_lines,
            today=date.today().isoformat(),
            timezone=os.getenv("REMINDER_TZ", "Asia/Singapore"),
            user_summary=(current_user_line + "\n\n") + (user_summary or "Nothing yet — this is the start of our history together."),
            shared_summary=shared_summary or "Nothing shared yet.",
        )

    async def _execute_tool(self, name: str, inputs: dict, user_id: int, flags: dict):
        if name == "log_wedding_drop":
            category = inputs.get("category") or detect_category(inputs["content"])
            drop(category, "text", inputs["content"], user_id)
            flags["wedding_drop"] = True
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
            return await asyncio.to_thread(get_events, inputs.get("days_ahead", 7))

        if name == "create_calendar_event":
            return await asyncio.to_thread(
                create_event,
                inputs["title"],
                inputs["start"],
                inputs["end"],
                inputs.get("description", ""),
                inputs.get("location", ""),
            )

        if name == "delete_calendar_event":
            ok = await asyncio.to_thread(delete_event, inputs["event_id"])
            return {"status": "deleted" if ok else "not_found"}

        if name == "log_fyi":
            result = log_fyi(user_id, inputs["content"], inputs.get("category"))
            flags["fyi"] = True
            return {"status": "logged", "id": str(result["id"])}

        if name == "read_fyis":
            fyis = get_fyis(limit=inputs.get("limit", 20))
            return [{"id": str(f["id"]), "user_id": f["user_id"], "content": f["content"], "category": f.get("category"), "created_at": f["created_at"][:16]} for f in fyis]

        if name == "schedule_notification":
            import os
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(os.getenv("REMINDER_TZ", "Asia/Singapore"))
            try:
                dt = datetime.fromisoformat(inputs["scheduled_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tz)
            except (ValueError, KeyError):
                return {"error": "Invalid scheduled_at — use ISO 8601 e.g. 2026-06-03T15:00:00+08:00"}
            if inputs.get("for_all_users"):
                all_ids = [int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
                target_ids = all_ids if all_ids else [user_id]
            else:
                target_ids = [user_id]
            recurrence = inputs.get("recurrence", "none")
            ids_scheduled = []
            for uid in target_ids:
                notif = _sched_notif(uid, inputs["message"], dt, recurrence)
                ids_scheduled.append(str(notif["id"]))
            display = dt.strftime("%-d %b at %-I:%M %p")
            return {"status": "scheduled", "scheduled_at": dt.isoformat(), "display": display, "ids": ids_scheduled}

        if name == "list_notifications":
            notifs = _list_notifs(user_id)
            return [{"id": str(n["id"]), "message": n["message"], "scheduled_at": n["scheduled_at"], "recurrence": n.get("recurrence", "none")} for n in notifs]

        if name == "cancel_notification":
            ok = _cancel_notif(inputs["notification_id"], user_id)
            return {"status": "cancelled" if ok else "not_found"}

        if name == "save_preference":
            pref = inputs["preference"].strip()
            existing = get_summary(user_id)
            marker = "\n\nPREFERENCES:\n"
            if marker in existing:
                before, prefs_block = existing.split(marker, 1)
                lines = [l for l in prefs_block.splitlines() if l.strip()]
                lines.append(f"- {pref}")
                updated = before + marker + "\n".join(lines)
            else:
                updated = existing + marker + f"- {pref}"
            count = get_message_count(user_id)
            save_summary(user_id, updated, count)
            flags["summary_updated"] = True
            return {"status": "saved", "preference": pref}

        if name == "mark_task_done":
            task_id = inputs.get("task_id", "")
            task = get_task_by_id(task_id)
            task_name = task["task"] if task else task_id
            success = complete_task(task_id, user_id)
            if success:
                flags.setdefault("completed_tasks", []).append(task_name)
                return {"status": "done", "task": task_name}
            return {"status": "not_found_or_not_allowed", "task_id": task_id}

        if name == "search_web":
            results = await asyncio.to_thread(web_search, inputs["query"], inputs.get("num_results", 5))
            return results

        if name == "save_shared_context":
            content = inputs["content"].strip()
            append_shared_summary(content)
            return {"status": "saved", "content": content}

        return {"error": f"Unknown tool: {name}"}

    @staticmethod
    def _strip_image_data(messages: list) -> list:
        """Replace base64 blobs in history with a placeholder to avoid token bloat on subsequent turns."""
        result = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "image" for b in content
            ):
                stripped = [
                    {"type": "text", "text": "[image]"} if isinstance(b, dict) and b.get("type") == "image" else b
                    for b in content
                ]
                result.append({**m, "content": stripped})
            else:
                result.append(m)
        return result

    async def _run_loop(self, user_content, user_id: int, history: list, user_summary: str, shared_summary: str = "") -> dict:
        import logging as _logging
        flags = {"wedding_drop": False, "fyi": False, "summary_updated": False, "completed_tasks": []}
        messages = history + [{"role": "user", "content": user_content}]
        system_prompt = self._build_system(user_summary, shared_summary, user_id)
        last_response = None

        for _ in range(10):
            last_response = await self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )

            if last_response.stop_reason == "end_turn":
                reply = next((b.text for b in last_response.content if hasattr(b, "text")), "")
                if not reply:
                    reply = "Got it."
                reply = _fix_md(reply)
                messages.append({"role": "assistant", "content": reply})
                updated_history = self._strip_image_data(messages[-40:])

                try:
                    msg_count = get_message_count(user_id) + 1
                    if msg_count % 4 == 0:
                        asyncio.create_task(
                            self._compress_and_save(user_id, updated_history, user_summary, msg_count)
                        )
                    elif flags["summary_updated"]:
                        # save_preference already wrote a fresh summary — just bump the count
                        current_summary = get_summary(user_id)
                        save_summary(user_id, current_summary, msg_count)
                    else:
                        save_summary(user_id, user_summary, msg_count)
                except Exception:
                    pass

                return {"text": reply, "history": updated_history, "notify_partner": flags["wedding_drop"] or flags["fyi"], "completed_tasks": flags["completed_tasks"]}

            if last_response.stop_reason == "max_tokens":
                reply = next((b.text for b in last_response.content if hasattr(b, "text")), "Got it.")
                messages.append({"role": "assistant", "content": reply})
                return {"text": reply, "history": self._strip_image_data(messages[-40:]), "notify_partner": flags["wedding_drop"] or flags["fyi"]}

            if last_response.stop_reason == "tool_use":
                tool_use_blocks = [b for b in last_response.content if b.type == "tool_use"]
                if not tool_use_blocks:
                    _logging.getLogger(__name__).error("tool_use stop_reason but no tool_use blocks in response")
                    break
                messages.append({"role": "assistant", "content": last_response.content})
                tool_results = []
                for block in tool_use_blocks:
                    try:
                        result = await self._execute_tool(block.name, block.input, user_id, flags)
                    except Exception as exc:
                        _logging.getLogger(__name__).exception(f"Tool {block.name} failed")
                        result = {"error": str(exc)}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    })
                messages.append({"role": "user", "content": tool_results})

        last_reason = last_response.stop_reason if last_response else "never_reached_api"
        _logging.getLogger(__name__).error(
            f"_run_loop exhausted for user {user_id}. Last stop_reason: {last_reason}. Messages len: {len(messages)}"
        )
        return {"text": f"[DEBUG] loop exhausted — last stop_reason: {last_reason}", "history": self._strip_image_data(messages[-40:])}

    async def stocks_brief(self) -> str:
        """Investment brief: read newsletters → extract assets → web research → analyst brief."""
        import logging as _log
        log = _log.getLogger("stocks_brief")
        today = date.today()

        def _source_label(from_addr: str) -> str:
            lower = from_addr.lower()
            for domain, label in [("milkroad.com","Milkroad"),("tldrnewsletter.com","TLDR"),
                                   ("coinbase.com","Coinbase"),("weeklywizdom.com","Weekly Wizdom")]:
                if domain in lower:
                    return label
            if "substack.com" in lower:
                local = lower.split("@")[0]
                return local.replace("daily","").replace("newsletter","").strip("-").title() or "Substack"
            return from_addr.split("@")[-1].split(".")[0].title()

        # ── STEP 1: fetch emails ───────────────────────────────────────────
        try:
            emails = await asyncio.to_thread(get_emails, None, 7, 14)
        except Exception as e:
            return f"⚠️ Could not read newsletters: {_html_escape(str(e))}"
        if not emails:
            return "📭 No newsletter emails in the last 14 days."
        log.info(f"stocks_brief: fetched {len(emails)} emails")

        # ── STEP 2: build subject + body digest (NO enrichment — too slow) ─
        unique_sources: set = set()
        digest_parts = []
        all_subjects = []
        for em in emails:
            label = _source_label(em["from"])
            unique_sources.add(label)
            all_subjects.append(em["subject"])
            body = em.get("body", "").strip()
            real_lines = [l for l in body.splitlines()
                          if l.strip() and not l.strip().startswith("http") and len(l.strip()) > 20]
            body_snippet = "\n".join(real_lines[:60])[:2500]
            digest_parts.append(
                f"[{label}] SUBJECT: {em['subject']}\n{body_snippet}"
            )
        digest = "\n\n---\n\n".join(digest_parts)
        total_sources = len(unique_sources)
        log.info(f"stocks_brief: {total_sources} sources, subjects: {all_subjects}")

        # ── STEP 3: extract asset names — plain text list, no JSON ─────────
        extract_resp = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content": f"""Today is {today}. Read these newsletter subjects and bodies.
Extract every stock, crypto, ETF, company, or investment topic mentioned.
Include assets from subject lines — e.g. "I'm not buying SpaceX, Anthropic, or OpenAI" means list SpaceX, Anthropic, OpenAI.

NEWSLETTERS:
{digest[:15000]}

List each asset on its own line in this format (nothing else):
Name | ticker or blank | stock/crypto/etf/other | bullish/bearish/neutral | one-line newsletter context

Example:
Bitcoin | BTC | crypto | bullish | newsletter says BTC hitting new highs
SpaceX | | stock | bearish | newsletter says not buying at current valuation
Coinbase | COIN | stock | bullish | AI trading bot launched on Coinbase

Output the list only — no headers, no explanation."""}],
        )
        raw_list = extract_resp.content[0].text.strip()
        log.info(f"stocks_brief: raw extract:\n{raw_list[:500]}")

        assets = []
        for line in raw_list.splitlines():
            line = line.strip().strip("-•").strip()
            if not line or "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 4:
                continue
            assets.append({
                "name": parts[0],
                "ticker": parts[1] if len(parts) > 1 else "",
                "type": parts[2] if len(parts) > 2 else "other",
                "sentiment": parts[3] if len(parts) > 3 else "neutral",
                "thesis": parts[4] if len(parts) > 4 else "",
                "sources": [_source_label(em["from"]) for em in emails[:1]],
            })

        log.info(f"stocks_brief: extracted {len(assets)} assets: {[a['name'] for a in assets]}")

        if not assets:
            subj_list = "\n".join(f"• {_source_label(em['from'])}: {em['subject']}" for em in emails)
            return f"<b>📰 Newsletters this week</b>\n\n{subj_list}\n\n<i>No investment topics identified.</i>"

        # ── STEP 4: web-research top 5 assets (3 searches each, concurrent) ─
        top = assets[:5]

        async def _research(asset: dict) -> dict:
            name = asset["name"]
            ticker = asset.get("ticker", "")
            kind = asset.get("type", "stock")
            q1 = f"{name} {ticker} price performance June 2026".strip()
            q2 = (f"{name} {ticker} market cap TVL on-chain 2026".strip() if kind == "crypto"
                  else f"{name} {ticker} revenue valuation earnings 2026".strip())
            q3 = f"{name} {ticker} buy sell analyst opinion investment 2026".strip()
            try:
                r1, r2, r3 = await asyncio.gather(
                    asyncio.to_thread(web_search, q1, 3),
                    asyncio.to_thread(web_search, q2, 3),
                    asyncio.to_thread(web_search, q3, 3),
                )
                def snip(rs):
                    return " | ".join(r.get("content","")[:300] for r in rs
                                      if isinstance(r, dict) and r.get("content"))[:1800]
                return {**asset, "d_price": snip(r1), "d_fund": snip(r2), "d_news": snip(r3)}
            except Exception as exc:
                log.warning(f"stocks_brief: research failed for {name}: {exc}")
                return {**asset, "d_price": "", "d_fund": "", "d_news": ""}

        researched = list(await asyncio.gather(*[_research(a) for a in top]))
        log.info(f"stocks_brief: research done for {len(researched)} assets")

        # ── STEP 5: generate analyst brief ────────────────────────────────
        research_block = ""
        for a in researched:
            research_block += f"""
━━━ {a['name']} ({a.get('ticker','')}) | {a.get('type','')} | newsletter: {a.get('sentiment','')}
Newsletter context: {a.get('thesis','(subject line mention only)')}
Price/momentum: {a['d_price'] or '(no search data)'}
Fundamentals: {a['d_fund'] or '(no search data)'}
Analyst/news: {a['d_news'] or '(no search data)'}
"""

        brief_resp = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3500,
            messages=[{"role": "user", "content": f"""You are a financial analyst. Today is {today}.
Write an investment brief for these assets. Use the research data below — it's from web searches done right now.

{research_block}

For each asset write a real analyst take. If search data has numbers, use them.
If it says "no search data", still give your best view from what you know + the newsletter signal.

FORMAT (Telegram HTML — no markdown):

<b>📊 This week</b>
2 sentences on the macro theme.

[per asset — use this EXACT spacing, blank line between EVERY element:]

<b>[emoji] Name (TICKER) — 🟢 BUY / 🟡 HOLD / 🔴 SKIP</b>

<i>[newsletter signal in 5 words]</i>

📰 <b>Thesis:</b> Why this, why now. Specific numbers. 2-3 sentences.

📈 <b>Momentum:</b> Price level and trend. One line with numbers.

🏗 <b>Fundamentals:</b> Key metric. One line.

⚠️ <b>Risk:</b> #1 downside. One line.

[blank line here before next asset]

<b>🔥 Best pick this week</b>
One asset, one reason.

RULES: <b>bold</b> only (no **), bullets •, no URLs, numbers required.
SPACING: blank line between every single element — signal, thesis, momentum, fundamentals, risk. Mobile readability is critical."""}],
        )
        brief_text = _fix_md(brief_resp.content[0].text)
        log.info("stocks_brief: brief generated successfully")

        # ── STEP 6: save signals to shared brain ──────────────────────────
        try:
            sigs = [f"{a.get('ticker') or a['name']} {'🟢' if a['sentiment']=='bullish' else '🔴' if a['sentiment']=='bearish' else '🟡'}"
                    for a in researched[:5]]
            await asyncio.to_thread(append_shared_summary,
                f"📊 Stocks {today}: {', '.join(sigs)}")
        except Exception:
            pass

        return brief_text

    async def handle_message(self, text: str, user_id: int, history: list[dict] | None = None) -> dict:
        if history is None:
            history = []
        try:
            user_summary = get_summary(user_id)
        except Exception:
            user_summary = ""
        try:
            shared_summary = get_shared_summary()
        except Exception:
            shared_summary = ""
        return await self._run_loop(text, user_id, history, user_summary, shared_summary)

    async def _compress_and_save(self, user_id: int, messages: list, existing_summary: str, message_count: int):
        # Build readable transcript — include tool exchanges so patterns in tool use are visible
        lines = []
        for m in messages[-30:]:
            role = m["role"]
            content = m["content"]
            if isinstance(content, str):
                lines.append(f"{role}: {content[:400]}")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            lines.append(f"{role}: {block['text'][:400]}")
                        elif block.get("type") == "tool_use":
                            lines.append(f"{role} [tool: {block.get('name')}]: {json.dumps(block.get('input', {}))[:200]}")
                        elif block.get("type") == "tool_result":
                            lines.append(f"tool_result: {str(block.get('content', ''))[:200]}")

        # Split off PREFERENCES block — never overwrite explicit user instructions
        pref_marker = "\n\nPREFERENCES:\n"
        if pref_marker in (existing_summary or ""):
            summary_body, pref_block = existing_summary.split(pref_marker, 1)
        else:
            summary_body = existing_summary or ""
            pref_block = None

        name = self._USER_NAMES.get(user_id, "this person")

        prompt = f"""You are updating the persistent memory profile for {name}, a user of a personal assistant Telegram bot shared with their partner.

EXISTING PROFILE:
{summary_body or "(none yet — build from scratch)"}

RECENT CONVERSATION (oldest first):
{chr(10).join(lines)}

---

Produce an updated profile using EXACTLY these section headers. For each section: merge existing observations with new ones, keep what's still true, update what's changed, add what's new. Be specific and behavioural — infer from what you observe, not just what they stated.

## Identity
1-2 sentences: who they are, what they do, their life context, relationship to the other user.

## Communication style
How they write — length, tone, directness, formality, emoji use. What kind of responses land well with them (short/detailed, casual/structured). Anything inferred from how they react to the assistant's replies.

## Current focus
What they're actively working on or preoccupied with right now. This is the most volatile section — update aggressively.

## Habits & patterns
Specific recurring behaviours observed across conversations. Topics they return to repeatedly. Things they keep deferring or worrying about. Any timing patterns. Things they tend to ask right after other things. Be concrete: "asks about X every few sessions" not "interested in X".

## Important facts
Specific facts that matter: job, living situation, relationships, upcoming events, finances, health, any significant personal context. Facts only — not interpretations.

## What works / what to avoid
Response styles, formats, or approaches this person responds well to — even if never explicitly stated (infer from engagement). Things to avoid. Anything they've corrected the assistant on.

---

Rules:
- Specific beats generic. "Procrastinates on vendor outreach, brings it up then changes subject" beats "interested in vendors".
- Infer patterns from repeated behaviour across the conversation history.
- Under 550 words total.
- Third person throughout.
- Output the profile only — no preamble, no extra commentary, no PREFERENCES section."""

        try:
            response = await self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=700,
                messages=[{"role": "user", "content": prompt}],
            )
            new_summary = response.content[0].text
            if pref_block is not None:
                new_summary = new_summary + pref_marker + pref_block
            save_summary(user_id, new_summary, message_count)
        except Exception:
            pass  # compression failure is non-critical

    async def proactive_check(self, user_id: int, user_name: str) -> str | None:
        """Survey current state for this user and return a message if something is genuinely worth flagging, else None."""
        import os
        from datetime import date as _date, datetime as _datetime, timezone as _tz

        today = _date.today()
        today_str = today.isoformat()
        other_name = next((n for uid, n in self._USER_NAMES.items() if uid != user_id), "partner")

        # --- Gather data ---
        try:
            profile = get_summary(user_id)
        except Exception:
            profile = ""

        try:
            shared = get_shared_summary()
        except Exception:
            shared = ""

        # Open tasks — compute staleness
        try:
            raw_tasks = get_tasks(user_id, include_done=False)
        except Exception:
            raw_tasks = []

        task_lines = []
        for t in raw_tasks:
            created = (t.get("created_at") or today_str)[:10]
            try:
                age_days = (today - _date.fromisoformat(created)).days
            except ValueError:
                age_days = 0
            due = t.get("due_date") or "no date"
            overdue = due != "no date" and due < today_str
            stale = age_days >= 7
            flags = []
            if overdue:
                flags.append("OVERDUE")
            if stale:
                flags.append(f"open {age_days}d")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            vis = "shared" if t.get("visibility") == "shared" else "private"
            task_lines.append(f"  • {t['task']} (due: {due}, {vis}){flag_str}")

        tasks_block = ("OPEN TASKS:\n" + "\n".join(task_lines)) if task_lines else "OPEN TASKS: none"

        # Wedding drops — last activity per category
        try:
            all_drops = get_recent_drops(limit=200)
        except Exception:
            all_drops = []

        last_drop_by_cat: dict[str, str] = {}
        for d in all_drops:
            cat = d.get("category") or "general"
            if cat not in last_drop_by_cat:
                last_drop_by_cat[cat] = d["ts"][:10]

        wedding_lines = []
        for cat_key, cat_info in CATEGORIES.items():
            last = last_drop_by_cat.get(cat_key)
            if last:
                try:
                    days_ago = (today - _date.fromisoformat(last)).days
                    wedding_lines.append(f"  • {cat_info['name']}: last activity {days_ago}d ago ({last})")
                except ValueError:
                    wedding_lines.append(f"  • {cat_info['name']}: last activity {last}")
            else:
                wedding_lines.append(f"  • {cat_info['name']}: NO ACTIVITY YET")

        wedding_block = "WEDDING PLANNING ACTIVITY BY CATEGORY:\n" + "\n".join(wedding_lines)

        # Calendar — next 14 days
        try:
            cal_events = await asyncio.to_thread(get_events, 14)
            cal_lines = []
            for e in cal_events[:10]:
                start = e["start"]
                if "T" in start:
                    try:
                        start = _datetime.fromisoformat(start).strftime("%-d %b %H:%M")
                    except ValueError:
                        pass
                cal_lines.append(f"  • {start} — {e['title']}")
            cal_block = ("CALENDAR (next 14 days):\n" + "\n".join(cal_lines)) if cal_lines else "CALENDAR: no events"
        except Exception:
            cal_block = "CALENDAR: unavailable"

        tz_name = os.getenv("REMINDER_TZ", "Asia/Singapore")

        prompt = f"""You are a proactive personal assistant for {user_name}. Today is {today_str} ({tz_name}).

Their wedding is on 7 November 2026 — {((_date(2026, 11, 7) - today).days)} days away.

THEIR PROFILE:
{profile or "(no profile yet)"}

SHARED BRAIN (confirmed couple decisions):
{shared or "(empty)"}

{tasks_block}

{wedding_block}

{cal_block}

---

Your job: decide if there is anything GENUINELY worth sending {user_name} an unprompted message about right now.

Things worth flagging (be selective — only flag if there's real urgency or a real pattern):
- A wedding category with NO activity that is time-sensitive (venue, photographer, catering book out fast)
- A task that has been open for 2+ weeks with no progress — worth a nudge or a "is this still relevant?"
- An overdue task that hasn't been cleared
- A calendar event in the next 3 days that likely needs prep
- A pattern from their profile that suggests something is being avoided or forgotten
- A deadline or booking window that's closing (e.g. "venue deposits usually required 12 months out")

Things NOT worth flagging:
- Anything already covered in the morning daily brief (today's due tasks, upcoming tasks)
- Generic wedding advice with no personal specificity
- Things that are going fine
- More than 3 bullets — if you have too much to say, pick the top 2-3

If there IS something worth saying, write ONLY the Telegram message itself — no preamble, no "here are my top picks", no analysis. Just the message, starting immediately with the first line of content.

Max 4 bullets. Sound like a sharp friend, not a notification bot.

FORMATTING: Telegram uses parse_mode=HTML — **asterisks are NOT rendered, they show as literal * characters**. Use <b>bold</b> for any headers, • for bullets, and emojis freely (💍 🚨 📸 🏨 💰 📅). Never use ** or _ for formatting.

If there is NOTHING genuinely worth flagging right now, respond with exactly the word: NOTHING"""

        try:
            response = await self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            result = response.content[0].text.strip()
            if result.upper() == "NOTHING" or result.upper().startswith("NOTHING"):
                return None
            return _fix_md(result)
        except Exception:
            return None

    async def handle_image(self, image_bytes: bytes, caption: str, user_id: int, history: list[dict] | None = None) -> dict:
        if history is None:
            history = []
        try:
            user_summary = get_summary(user_id)
        except Exception:
            user_summary = ""
        try:
            shared_summary = get_shared_summary()
        except Exception:
            shared_summary = ""

        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        img_block = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}}

        # Always extract all visible text from the image first.
        # This gives the agent clean text to reason over rather than relying
        # purely on visual understanding — critical for screenshots, newsletters, charts.
        try:
            ocr_resp = await self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": [
                        img_block,
                        {"type": "text", "text": "Extract ALL visible text from this image verbatim. Include every word, number, label, price, ticker, headline, and caption you can see. Output plain text only — no commentary, no formatting."},
                    ],
                }],
            )
            extracted_text = ocr_resp.content[0].text.strip()
        except Exception:
            extracted_text = ""

        # Build the user content: image + extracted text + caption context
        instruction = caption if caption else "Analyse this image. Use the extracted text below to understand the content."
        if extracted_text:
            instruction += f"\n\n[TEXT EXTRACTED FROM IMAGE]\n{extracted_text}"

        user_content = [
            img_block,
            {"type": "text", "text": instruction},
        ]
        result, payment = await asyncio.gather(
            self._run_loop(user_content, user_id, history, user_summary, shared_summary),
            self._wedding._extract_payment(image_bytes, caption),
        )
        if payment:
            try:
                add_payment(payment)
            except Exception:
                pass
        return result

    # Command methods — delegate to existing agents
    async def baby_brief(self) -> str:
        """Weekly pregnancy update — current week, what's developing, upcoming milestones."""
        info = pregnancy_summary()
        milestones = upcoming_milestones(within_weeks=4)
        milestones_text = "\n".join(milestones) if milestones else "No major milestones in the next 4 weeks."

        prompt = f"""You are a practical pregnancy advisor. Write a concise weekly check-in for a first-time parent couple.

PREGNANCY DATA:
• Week {info['week']}, Day {info['day']}
• Trimester: {info['trimester']}
• Due date: {info['due_date']} ({info['days_until_due']} days away)

UPCOMING MILESTONES (next 4 weeks):
{milestones_text}

Focus ONLY on what's practical and actionable. Skip baby size comparisons and development descriptions entirely.

FORMAT (Telegram HTML):

<b>👶 Week {info['week']} · {info['trimester']} Trimester</b>
<i>Due {info['due_date']} · {info['days_until_due']} days to go</i>

<b>🤰 What Jess may feel this week</b>
Symptoms typical for this exact week. What's normal vs what needs a doctor call. Be specific — not "nausea is common" but "nausea usually peaks around week 8-9, should ease by week 12". Bullets •

<b>✅ Actions this week</b>
Concrete things to do or book RIGHT NOW. e.g. "Book viability scan — call clinic, request week 7-8 slot". Bullets •

<b>📅 Upcoming milestones</b>
For each milestone in the list: what it is, what it checks for, when to book it. Bullets •

RULES: <b>bold</b> only, bullets •, no URLs, no asterisks, no baby size comparisons."""

        resp = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        return _fix_md(resp.content[0].text)

    async def bring_me_up_to_speed(self) -> str:
        return await self._wedding.bring_me_up_to_speed()

    async def category_status(self, category: str) -> str:
        return await self._wedding.category_status(category)

    async def priority_brief(self) -> str:
        return await self._wedding.priority_brief()

    async def daily_brief(self, user_id: int) -> str:
        return await self._daily.daily_brief(user_id)

    async def combined_daily_brief(self, user_ids: list[int], user_names: dict[int, str] | None = None) -> tuple:
        return await self._daily.combined_daily_brief(user_ids, user_names)

    async def evening_brief(self, user_ids: list[int], user_names: dict[int, str] | None = None) -> str:
        return await self._daily.evening_brief(user_ids, user_names)

    async def reminders_brief(self, user_ids: list[int], user_names: dict[int, str] | None = None) -> tuple:
        """Two-column view: each person's private tasks + a shared section.
        Returns (text, ordered_tasks) where ordered_tasks is the flat list in display order."""
        from datetime import date as _date
        today_str = _date.today().isoformat()
        if user_names is None:
            user_names = {uid: str(uid) for uid in user_ids}

        per_person: dict[int, list[dict]] = {}
        for uid in user_ids:
            try:
                per_person[uid] = get_tasks(uid, include_done=False)
            except Exception:
                per_person[uid] = []

        seen_shared: set = set()
        shared: list[dict] = []
        personal: dict[int, list[dict]] = {uid: [] for uid in user_ids}
        seen_personal: set = set()

        for uid, tasks in per_person.items():
            for t in tasks:
                tid = t.get("id")
                assigned = t.get("assigned_to")
                # Tasks assigned to a specific person always go in their section
                if assigned and assigned in personal:
                    if tid not in seen_personal:
                        seen_personal.add(tid)
                        personal[assigned].append(t)
                # Shared with no specific assignee → shared section
                elif t.get("visibility") == "shared":
                    if tid not in seen_shared:
                        seen_shared.add(tid)
                        shared.append(t)
                # Private → creator's section
                else:
                    owner = t.get("user_id")
                    if owner in personal and tid not in seen_personal:
                        seen_personal.add(tid)
                        personal[owner].append(t)

        _PREFERENCE_VERBS = (
            " likes ", " like ", " loves ", " prefers ", " prefer ",
            " hates ", " hate ", " dislikes ", " dislike ",
            " is allergic", " are allergic",
            " enjoys ", " enjoy ", " wants ", " want ",
        )

        def _is_junk(t: dict) -> bool:
            """Filter out FYIs, wedding tasks, and non-tasks stored as daily tasks."""
            raw = (t.get("task") or "").strip().lower()
            if t.get("category") == "wedding":
                return True
            # Obvious junk prefixes
            if (
                raw.startswith("fyi")
                or raw.startswith("• fyi")
                or raw.startswith("ansen deposited")
                or raw.startswith("jess deposited")
                or raw.startswith("ansen paid")
                or raw.startswith("jess paid")
            ):
                return True
            # Preference/fact statements masquerading as tasks
            # e.g. "Jess likes kaya waffle", "Ansen prefers window seats"
            names = ("jess ", "jessica ", "ansen ")
            if any(raw.startswith(n) for n in names):
                if any(verb in raw for verb in _PREFERENCE_VERBS):
                    return True
            return False

        def _sort(tasks: list[dict]) -> list[dict]:
            urgency = {"overdue": 0, "today": 1, "upcoming": 2, "none": 3}
            def key(t):
                due = t.get("due_date")
                if not due:
                    return (3, "")
                if due < today_str:
                    return (0, due)
                if due == today_str:
                    return (1, due)
                return (2, due)
            return sorted(tasks, key=key)

        SEP = "───────────────"

        def _fmt(t: dict, limit: int = 120) -> str:
            due = t.get("due_date")
            name = (t.get("task") or "").strip()
            if name.upper().startswith("TASK:"):
                name = name[5:].strip()
            if len(name) > limit:
                name = name[:limit].rsplit(" ", 1)[0] + "…"
            if not due:
                return f"• {name}"
            elif due < today_str:
                return f"🔴 {name}"
            elif due == today_str:
                return f"📅 {name}"
            else:
                try:
                    d = _date.fromisoformat(due)
                    return f"• {name} — {d.day} {d.strftime('%b')}"
                except ValueError:
                    return f"• {name} — {due}"

        CAT_EMOJI = {
            "finance": "💳", "health": "🏥", "home": "🏠", "work": "💼",
            "social": "🎉", "travel": "✈️", "personal": "🙋", "wedding": "💒",
        }

        lines: list[str] = ["<b>📋 Reminders</b>"]
        ordered_tasks: list[dict] = []

        for i, uid in enumerate(user_ids):
            person_name = user_names.get(uid, str(uid))
            tasks = _sort([t for t in personal.get(uid, []) if not _is_junk(t)])
            if i > 0:
                lines.append("")
                lines.append(SEP)
            lines.append("")
            lines.append(f"<b>{person_name}</b>")
            if tasks:
                for t in tasks:
                    lines.append(_fmt(t))
                    ordered_tasks.append(t)
            else:
                lines.append("• Nothing on the list ✓")

        # Shared: dedup by text, filter junk, flat urgency sort
        seen_text: set = set()
        clean_shared = []
        for t in _sort(shared):
            if _is_junk(t):
                continue
            key = (t.get("task") or "").strip().lower()[:60]
            if key and key not in seen_text:
                seen_text.add(key)
                clean_shared.append(t)

        lines.append("")
        lines.append(SEP)
        lines.append("")
        lines.append("<b>👥 Shared</b>")

        if clean_shared:
            # Split into urgency groups and add blank line between them
            urgent = [t for t in clean_shared if t.get("due_date") and t["due_date"] <= today_str]
            upcoming = [t for t in clean_shared if t.get("due_date") and t["due_date"] > today_str]
            no_date = [t for t in clean_shared if not t.get("due_date")]

            for group in [urgent, upcoming, no_date]:
                if group:
                    lines.append("")
                    for t in group:
                        lines.append(_fmt(t))
                        ordered_tasks.append(t)
        else:
            lines.append("• Nothing shared ✓")

        return "\n".join(lines), ordered_tasks
