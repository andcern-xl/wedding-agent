import asyncio
import base64
import json
import os
import re
from html import escape as _html_escape
from datetime import datetime, date, timezone, timedelta
from zoneinfo import ZoneInfo
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
from tools.baby_knowledge import save_entry as save_baby_entry, get_entries as get_baby_entries, search_entries as search_baby_entries
from tools.baby_budget import add_item as add_baby_budget_item, summary as baby_budget_summary
from tools.shared_budget import add_item as add_shared_budget_item, summary as shared_budget_summary

_LOCAL_TZ = ZoneInfo(os.getenv("REMINDER_TZ", "Asia/Singapore"))


def _local_today() -> date:
    """Today's date in the configured local timezone (not Railway UTC)."""
    return datetime.now(_LOCAL_TZ).date()


def _trip_gap_check(trip: dict) -> str | None:
    """Return a proactive gap question if something critical is missing before departure."""
    notes = (trip.get("notes") or "").lower()
    status = trip.get("status") or "planning"
    start = trip.get("start_date")
    if not start or status in ("completed", "cancelled"):
        return None
    try:
        days_until = (date.fromisoformat(start) - _local_today()).days
    except Exception:
        return None
    if days_until < 0 or days_until > 90:
        return None
    dest = trip.get("destination", "the trip")
    depart_label = date.fromisoformat(start).strftime("%-d %b")
    gaps = []
    has_flights = any(k in notes for k in ["flight", "sg →", "→ sg", "fly", "airline", "depart", "transit", "stopover"])
    if not has_flights and days_until <= 60:
        gaps.append("flights")
    has_hotel = any(k in notes for k in ["hotel", "airbnb", "hostel", "accommodation", "check-in", "resort", "stay", "booking"])
    if not has_hotel and days_until <= 30:
        gaps.append("accommodation")
    if not gaps:
        return None
    gap_str = " and ".join(gaps)
    return f"⚠️ {dest} is {days_until} days away ({depart_label}) — {gap_str} not captured yet. Want me to look up options or set a reminder?"


# Model constants — swap here to change globally
CHAT_MODEL = "claude-haiku-4-5-20251001"      # conversations, briefs, tool routing
SYNTHESIS_MODEL = "claude-sonnet-4-6"          # knowledge synthesis, compression, complex reasoning

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
    # Strip pipe-table rows — Telegram renders them as raw pipe characters
    def _strip_pipe_table(m: re.Match) -> str:
        cells = [c.strip() for c in m.group(0).split('|') if c.strip() and not re.match(r'^[-:]+$', c.strip())]
        return ' • '.join(cells) if cells else ''
    text = re.sub(r'^\|.+\|[ \t]*$', _strip_pipe_table, text, flags=re.MULTILINE)
    # Remove separator lines (|---|---|)
    text = re.sub(r'^\|[-| :]+\|[ \t]*$', '', text, flags=re.MULTILINE)
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
- Use Telegram HTML formatting: <b>Section Title</b> for headers, • for bullet points
- Start every bullet with a relevant emoji (🏨 venue, 💰 budget, 📸 photography, 🎵 entertainment, 🍽️ catering, 💒 ceremony, 🌸 decor, ✅ confirmed, 🔍 in progress, etc.)
- Put a blank line between EVERY bullet — never stack bullets with no gap. Dense walls of text are unreadable on mobile.
- Use emoji as section headers (e.g. 💰 Budget, 📅 This week, ✅ Done, 🔍 In progress) — not plain bold text alone
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
            model=CHAT_MODEL,
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
            model=CHAT_MODEL,
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
            model=CHAT_MODEL,
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

FORMATTING RULES — follow exactly:
- Use Telegram HTML. <b> for headers only. No markdown, no **asterisks**.
- Start every bullet with a relevant emoji: 🏨 venue, 💰 budget, 📸 photography, 💄 hair/makeup, 👗 attire, 🎵 entertainment/DJ, 🍽️ catering, 💒 ceremony, 🌸 decor/flowers, 🗓️ logistics, 🥂 party, ✅ confirmed booking, 🔍 still researching
- Put a blank line between each bullet — not just between sections. This is essential for readability."""

        response = await self.client.messages.create(
            model=SYNTHESIS_MODEL,
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

FORMATTING RULES — follow exactly:
- Use Telegram HTML. <b> for headers only. No markdown, no **asterisks**.
- Start every bullet with a relevant emoji: 🏨 venue, 💰 budget, 📸 photography, 💄 hair/makeup, 👗 attire, 🎵 entertainment/DJ, 🍽️ catering, 💒 ceremony, 🌸 decor/flowers, 🗓️ logistics, 🥂 after-party, ✅ confirmed, 🔍 in progress, ❌ untouched
- Put a blank line between each bullet — not just between sections. This is essential for readability in Telegram."""

        response = await self.client.messages.create(
            model=SYNTHESIS_MODEL,
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

FORMATTING RULES — follow exactly:
- Use Telegram HTML. <b> for headers only. No markdown, no **asterisks**.
- Start every bullet with a relevant emoji: 🏨 venue, 💰 budget, 📸 photography, 💄 hair/makeup, 👗 attire, 🎵 entertainment/DJ, 🍽️ catering, 💒 ceremony, 🌸 decor/flowers, 🗓️ logistics, 🥂 after-party, 🔥 urgent, ✅ going well
- Put a blank line between each bullet — not just between sections. This is essential for readability in Telegram."""

        response = await self.client.messages.create(
            model=SYNTHESIS_MODEL,
            max_tokens=1500,
            system=self._build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        return _fix_md(response.content[0].text)


_PREFERENCE_VERBS = (
    " likes ", " like ", " loves ", " prefers ", " prefer ",
    " hates ", " hate ", " dislikes ", " dislike ",
    " is allergic", " are allergic",
    " enjoys ", " enjoy ", " wants ", " want ",
)
_JUNK_PREFIXES = (
    "fyi", "ansen deposited", "jess deposited",
    "ansen paid", "jess paid", "approved ",
)
_PREFERENCE_NAMES = ("jess ", "jessica ", "ansen ")


def _is_junk_task(t: dict) -> bool:
    """Return True if this daily_task entry is not a real actionable task."""
    raw = (t.get("task") or "").strip().lower()
    if raw.startswith("• ") or raw.startswith("- "):
        raw = raw[2:]
    if t.get("category") == "wedding":
        return True
    if any(raw.startswith(p) for p in _JUNK_PREFIXES):
        return True
    if len(raw) > 300:
        return True
    if any(raw.startswith(n) for n in _PREFERENCE_NAMES):
        if any(verb in raw for verb in _PREFERENCE_VERBS):
            return True
    return False


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


_CAL_STOP = {"with", "the", "and", "for", "our", "from", "this", "that", "have", "will", "dinner", "lunch", "brunch", "meet", "catch"}

def _is_calendar_covered(task: dict, cal_events: list) -> bool:
    """True if a calendar event already represents this task (same date + name overlap)."""
    due = task.get("due_date")
    if not due:
        return False
    task_text = (task.get("task") or "").lower()
    task_words = {w for w in task_text.split() if len(w) >= 4 and w not in _CAL_STOP}
    if not task_words:
        return False
    for e in cal_events:
        event_date = (e.get("start") or "")[:10]
        if event_date != due:
            continue
        event_words = {w.lower() for w in (e.get("title") or "").split() if len(w) >= 4 and w.lower() not in _CAL_STOP}
        if task_words & event_words:
            return True
    return False


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
        today = _local_today()
        cats = get_all_categories()
        cat_list = ", ".join(f"{slug} ({v['emoji']} {v['name']})" for slug, v in cats.items())
        prompt = TASK_PARSE_PROMPT.format(
            message=text,
            today=today.isoformat(),
            weekday=today.strftime("%A"),
            categories=cat_list,
        )
        response = await self.client.messages.create(
            model=CHAT_MODEL,
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
        today_str = _local_today().isoformat()
        tasks = get_tasks(user_id, include_done=False)
        task_lines = [_task_label(t, today_str) for t in tasks[:20]]
        context = "CURRENT TASKS:\n" + "\n".join(task_lines) if task_lines else "No open tasks."

        messages = history + [{"role": "user", "content": f"[Context]\n{context}\n\n[Message]\n{text}"}]
        response = await self.client.messages.create(
            model=CHAT_MODEL,
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
        today_str = _local_today().isoformat()
        weekday = _local_today().strftime("%A")
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
            model=CHAT_MODEL,
            max_tokens=1000,
            system=DAILY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return _fix_md(response.content[0].text)

    async def combined_daily_brief(self, user_ids: list[int], user_names: dict[int, str] | None = None) -> tuple:
        """Generate one combined daily brief for all users, sent to both.
        Returns (text, open_tasks) where open_tasks is the full deduped task list."""
        today_str = _local_today().isoformat()
        weekday = _local_today().strftime("%A")
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

        # Google Calendar events — fetch first so we can filter tasks against them
        try:
            events = await asyncio.to_thread(get_events, 7)
        except Exception:
            events = []

        # Drop tasks already represented by a calendar event (same date + name overlap)
        merged = [t for t in merged if not _is_calendar_covered(t, events)]

        overdue = sorted([t for t in merged if t.get("due_date") and t["due_date"] < today_str], key=lambda t: t["due_date"])
        due_today = [t for t in merged if t.get("due_date") == today_str]
        upcoming = sorted([t for t in merged if t.get("due_date") and t["due_date"] > today_str], key=lambda t: t["due_date"])
        no_date = sorted([t for t in merged if not t.get("due_date")], key=lambda t: t["task"].lower())

        def _owner_label(t: dict) -> str:
            owner = t.get("_owner", "")
            if owner and owner != "shared":
                return f" [{owner}]"
            return ""

        if not merged and not events:
            return "✅ Nothing on the list today. Add tasks by telling me — \"remind us to X on Friday\".", []

        # Only show today's events in the daily brief — keep full list for task dedup only
        today_events = [e for e in events if e["start"].startswith(today_str)]

        parts = [f"TODAY IS {weekday.upper()}, {today_str}"]

        if today_events:
            ev_lines = []
            for e in today_events:
                start = e["start"]
                if "T" in start:
                    try:
                        dt = datetime.fromisoformat(start)
                        start = dt.strftime("%-I:%M %p")
                    except ValueError:
                        pass
                ev_lines.append(f"  • {start} — {e['title']}")
            parts.append("TODAY'S CALENDAR:\n" + "\n".join(ev_lines))

        if overdue:
            lines = [f"  • {_task_label(t, today_str)}{_owner_label(t)}" for t in overdue]
            parts.append("OVERDUE:\n" + "\n".join(lines))

        if due_today:
            lines = [f"  • {_task_label(t, today_str)}{_owner_label(t)}" for t in due_today]
            parts.append("DUE TODAY:\n" + "\n".join(lines))

        if upcoming:
            lines = [f"  • {_task_label(t, today_str)}{_owner_label(t)}" for t in upcoming[:7]]
            parts.append("COMING UP (next 7 days):\n" + "\n".join(lines))

        if no_date:
            lines = [f"  • {_task_label(t, today_str)}{_owner_label(t)}" for t in no_date[:8]]
            parts.append("ON THE LIST (no date):\n" + "\n".join(lines))

        context = "\n\n".join(parts)
        person_list = " and ".join(names.values()) if names else "both of you"
        prompt = f"""{context}

Write a morning brief for {person_list}. Be a smart friend, not a secretary — synthesise, don't dump.

Rules:
- Max 5 bullets total. Each bullet = one clear action or heads-up.
- Lead with anything happening TODAY (calendar events, things due today, overdue items). Use ⚠️ for overdue.
- Then 1-2 most urgent upcoming items if space allows.
- Skip undated backlog entirely unless something is critically overdue.
- Never list calendar AND the same task — calendar wins.
- Where a task belongs to one person, note [Name] in brackets.
- End with one line: → /tasks /reminders for the full picture

Use • for bullets. <b> tags for bold. Emojis welcome. NEVER use **asterisks**. Keep it tight — this is a phone notification, not a report."""

        response = await self.client.messages.create(
            model=SYNTHESIS_MODEL,
            max_tokens=500,
            system=DAILY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return _fix_md(response.content[0].text), merged

    async def evening_brief(self, user_ids: list[int], user_names: dict[int, str] | None = None) -> str:
        """End-of-day recap: what was done today, what's coming tomorrow."""
        today_str = _local_today().isoformat()
        tomorrow_str = (_local_today() + timedelta(days=1)).isoformat()
        tomorrow_weekday = (_local_today() + timedelta(days=1)).strftime("%A")
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
            model=SYNTHESIS_MODEL,
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

WHAT YOU KNOW ABOUT THIS PERSON (persistent memory — survives restarts)
{user_summary}

This is your memory from all past conversations, compressed every 2 messages by a high-quality summariser. It is the ground truth for what you know about this person. Read it before every response and use it to:
- Reference things they've shared without them having to repeat themselves — proactively bring up relevant context
- Match their communication style exactly (length, tone, directness, emoji use)
- Anticipate what they probably want based on their patterns
- Skip explanations they don't need — they're not new users
- When they say "you know that thing I mentioned" or "like last time" — check here first

If someone says "do you remember X" and it's in the profile: confirm and use it. If it's not: say honestly "I don't have that noted — tell me again and I'll remember it."
If it contains a PREFERENCES section, follow those as standing orders without being asked again.

SHARED BRAIN — confirmed couple decisions and permanent memories:
{shared_summary}

RECENT FYIs — notes and updates from the last 30 days. Reference naturally when relevant — don't quote back verbatim:
{recent_fyis}

BABY — current pregnancy status and saved knowledge. The CURRENT PREGNANCY week/trimester at the top is calculated live and is always correct. Never override it with recalled memories or guesses:
{baby_context}

RECALLED MEMORIES — discrete facts retrieved from all past conversations, relevant to this message:
{mem0_context}
These are extracted facts, not summaries. Reference them naturally if useful. They may overlap with the profile above — treat as reinforcing signals.

PEOPLE
- Ansen: user_id 63756531
- Jess / Jessica: user_id 6927468999

PROACTIVE NOTIFICATIONS — YOU CAN DO THIS
You are running inside a Telegram bot with a job queue. You CAN send messages at specific times. When someone asks for a time-based reminder, call schedule_notification — a background job fires it automatically at the right moment. Never tell the user you can't send proactive messages or push alerts. You can. Use the tool.

NOTIFICATION MESSAGE STYLE — always write notification messages with:
- A relevant emoji at the start (🐾 for pets, 🏥 for health, 💍 for wedding, 🍼 for baby, ✈️ for travel, 💰 for money, 📅 for calendar, ⏰ for general reminders)
- Short, warm, direct phrasing — like a helpful friend, not a calendar alert
- Any useful context (what to bring, what to prepare) in 1–2 sentences max
- No "Reminder:" prefix — the emoji does that job

MESSAGING THE PARTNER — DO THIS PROPERLY
When asked to "notify", "tell", "message", "ping", "let Jess know", "tell Ansen" etc → call message_partner immediately. It fires within 30 seconds.
NEVER claim to have notified someone without calling message_partner. Do not say "Done", "Sent", or "Jess got the notification" unless the tool returned {{"status": "sent"}}. If you haven't called the tool, you haven't sent anything.

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
- Monthly recurring reminders ("every 1st", "each month", "monthly") → schedule_notification with recurrence="monthly", scheduled for the next occurrence. Just do it — don't ask which approach they prefer.
- Budget/spending → read_payments + read_wedding_drops("budget")
- "what should I do" / "what's on" → call both read_wedding_drops and read_daily_tasks, synthesise one answer
- Adding a task about a wedding vendor → read relevant drops first, bake context into the task description. Always set category="wedding" so it stays out of the daily reminders list.
- New category request → add_custom_category
- Decisions / confirmed bookings → read_memory
- "what's on the calendar" / "what's happening this week" → read_calendar
- "book", "schedule", "add to calendar" → create_calendar_event; then immediately call read_daily_tasks and mark_task_done on any open task that matches the same event (by name or date) — never leave a calendar event AND an open task for the same thing
- "cancel", "remove from calendar" → delete_calendar_event (read_calendar first to get the event ID)
- "what reminders are scheduled" → list_notifications
- "cancel that reminder" → cancel_notification (list_notifications first to get the ID)
- Shared update / past-tense info / "FYI" / "just so you know" / "heads up" / completed action → classify first (see CONTENT CLASSIFICATION below), then use the right tool
- "any FYIs?" / "what did we share recently?" → read_fyis
- "going forward always do X" / "remember that I prefer X" / "from now on X" → save_preference (this persists across sessions)
- "search for", "look up", "find X", "what's the weather", "what is X", "who is X", any real-time or internet question → search_web first, then answer with real results. NEVER say you can't search — you have the search_web tool.
- Vendor recommendations / price research / "find X in Y" / "what does X cost" / any question needing current market info → search_web first, then answer with real results
- Anything both people should always know → save_shared_context. This is your long-term memory about this couple — call it proactively for:
  • Confirmed decisions ("we're going with X venue", "we chose the buffet menu")
  • Preferences about either person ("Jess wants an unmedicated birth", "Ansen hates formal dress codes")
  • Facts about their life together ("due date is Feb 2027", "wedding is in Bali", "they want to move before the baby comes")
  • Cross-domain insights you notice ("wedding is 3 months after the due date — planning is tight")
  • Patterns you observe ("they tend to decide things quickly once they've both seen the option")
  • Anything where you'd think "I wish I had known this earlier"
  The bar is: would this be useful context in 3 months? If yes, save it. Don't wait to be asked.

PROACTIVE MEMORY — save facts without being asked
You notice things people say in passing and file them. Do this silently (no need to announce every save):

save_preference for things about THIS person only:
  • Dietary: allergies, intolerances, dislikes, strong preferences ("Ansen doesn't eat cilantro", "Jess is lactose intolerant")
  • Sizes, specs, logistics: ring size, dress size, shoe size, passport number, ID details
  • Work & schedule patterns: working hours, commute, recurring meetings, travel frequency
  • Health context: medications, conditions, recurring symptoms mentioned casually
  • Personal quirks worth knowing: sleep preferences, temperature sensitivity, how they handle stress
  • Financial habits: how they like to split things, savings goals mentioned, spending style
  • Anything prefaced with "I always", "I never", "I hate when", "I love", "my go-to is"

save_shared_context for things BOTH should know:
  • Any fact about their relationship, home, finances, or plans that affects both
  • Decisions made together — even small ones ("we agreed to keep the guest list under 80")
  • Constraints they're working within ("the venue deposit is non-refundable")
  • Key dates, numbers, contacts that keep coming up

SILENCE RULE: save silently — don't say "I've saved that to your profile" after every fact. Only mention it if they explicitly asked you to remember something, or if it's a significant decision worth confirming. The goal is to feel like a person who just remembers things, not a bot announcing saves.

CONTENT CLASSIFICATION — DO THIS BEFORE FILING ANYTHING
When someone drops a note, link, screenshot, or update, scan it for domain signals before choosing a tool:

🔑 Strong signals → file confidently without asking:
• pregnancy / birth / trimester / scan / OB / midwife / epidural / breastfeeding / newborn / postpartum / motherhood / parenting / baby sleep / formula / pram / nursery → save_baby_knowledge (knowledge) or log_baby_expense (if a purchase/cost)
• venue / caterer / florist / photographer / wedding dress / guest list / RSVP / seating plan / honeymoon → log_wedding_drop; if it's a cost/payment → also log via wedding_payments tool
• travel / flight / hotel / itinerary / airport / booking ref → log_fyi with category="travel"; if it's a cost → log_shared_expense with category="travel"
• restaurant / food / café / reservation / dinner → log_fyi with category="social" or "food"
• home / lease / renovation / moving / landlord / cleaning → log_fyi with category="home"; if it's a cost → log_shared_expense with category="home"

BUDGET ROUTING — three buckets, mutually exclusive:
• Wedding cost (venue, catering, photography, DJ, flowers, attire, transport for guests, honeymoon) → wedding budget via existing wedding_payments
• Baby cost (pram, car seat, crib, scans, hospital package, vitamins, maternity clothes) → log_baby_expense
• Everything else financial (rent, cleaning, Airbnb, subscriptions, utilities, car, dining, travel) → log_shared_expense

When someone mentions money/costs: classify which bucket first, then log to the right one. Never put life expenses in wedding budget. Never put wedding costs in shared budget.

⚠️ Ambiguous signals → ask before filing:
If content could fit more than one domain, or the signal is weak, ask:
"Should I save this to [best guess]? Or somewhere else?"
Example: a Substack link — check the title/description first. "Hacking Motherhood" → baby_knowledge (clear). A generic productivity newsletter → FYI personal (clear). An article about postpartum finance → ask: "Save this to baby knowledge or finance FYI?"

Never silently misfile. A quick confirm is better than wrong storage.

BABY TO-DOS
Any action item related to pregnancy, birth prep, hospital, scans, appointments, or baby gear → add_daily_task with category="baby" and visibility="shared". Baby tasks are always shared — both need to know.
Examples: "book viability scan", "research hospitals", "buy prenatal vitamins", "find a paediatrician"

BABY BUDGET
Any baby/pregnancy purchase, quote, or planned spend → log_baby_expense. Categories: gear (pram, crib, car seat), medical (scans, tests, consultations), clothing (maternity, baby clothes), hospital (delivery package, room), nutrition (vitamins, supplements), other.
Triggered by: prices mentioned, "I bought", "we ordered", "how much is", "looking at [item]", or any baby product/service cost.

BABY QUESTIONS (for the doctor / OB / midwife)
Any question they want to ask at an appointment → add_daily_task with category="baby_questions" and visibility="shared".
Triggered by: "add to OB questions", "ask the doctor", "remind us to ask", "question for the midwife", or any question phrased as something to clarify at an appointment.
Examples: "ask about iron levels", "check if we need the flu jab", "ask when to start kick counts"

SAVING YOUR OWN ANALYSIS — DO THIS PROACTIVELY
When you generate a substantive recommendation, decision framework, or "my take" on any topic — especially baby, medical, insurance, financial, or legal questions — save the key insight to the knowledge base AFTER responding. Don't just give advice and let it disappear.

Baby/pregnancy/medical/hospital/insurance topics → save_baby_knowledge
  summary: the key decision or takeaway in 2-3 sentences
  tags: relevant tags
  raw_text: include the full reasoning so they can refer back

Couple decisions ("we decided X", "going with Y", "our position is Z") → save_shared_context
  One clear sentence stating the decision.

Triggers for saving your own analysis:
- You gave a "my take" or concrete recommendation
- You worked through a decision with pros/cons and landed somewhere
- The user asked "what should we do about X" and you gave a real answer
- You explained a nuanced topic (insurance, hospital billing, legal) with practical implications

Do not save generic explanations or information that didn't result in a recommendation. Save decisions and insights, not encyclopaedia entries.

TRAVEL
Ansen and Jess travel frequently. Track trips in the shared trips list.

Saving a new trip: "we're going to X", "I booked flights to X", "planning a trip to X" → save_trip, then AUTOMATICALLY (without being asked):
1. search_web for visa requirements for Singapore passport (Ansen) AND US passport (Jess) → update_trip with visa_ansen and visa_jess
2. search_web for practical tips (best areas, transport, weather for the travel month) → update_trip notes with key findings
3. Check if any critical info is missing — flights booked? hotel? — and ask about gaps if departure is within 60 days
4. Offer to set a reminder for key pre-trip actions (e.g. "Want me to set a reminder to book the hotel 8 weeks out?")

This is the same proactive pattern used for the baby brain — research, save, flag gaps, offer reminders.

Updating: booked flights/hotel, dates changed, adding context → update_trip. After updating, check for remaining gaps and flag them.
Viewing: "what trips do we have", "travel plans" → get_trips
Visa check: "what visa do we need for X" → search_web for both passports, update_trip with results

Visa format to save: "Visa-free, 30 days" / "e-Visa required — apply at [url], ~$30, processing 3 days" / "Visa on arrival, USD 35" / "Visa required — Singapore embassy"

UPCOMING SHOWS (Ansen only)
Ansen tracks concerts, gigs, festivals, and events he has tickets for.

Adding: ticket screenshot or "I got tickets to X" → save_show (extract name, venue, date, time) → confirm: "🎟 Got it — [Show] at [Venue], [Date]"
Removing: "I can't go to X", "remove X", "sold my tickets to X" → delete_show
Updating: "might not make X", "sold the ticket", "got upgraded" → update_show with appropriate status (going/maybe/cant_go/sold) and/or notes

CORRECTIONS — ALWAYS PERSIST, NEVER JUST VERBALLY ACKNOWLEDGE
When the user corrects something you stated — "no that's wrong", "actually it's X", "that's not right", "you got that wrong", "look at internal database to reconcile" — you MUST call correct_knowledge immediately. Never just say "Got it!" or "Reconciled" without writing the fix to persistent storage. Verbal acknowledgement alone means the same mistake reappears every future session. The tool searches baby_knowledge, shared_summary, user_summary, and trips for stale data and replaces it. After calling it, confirm exactly what was found and updated: "✅ Fixed in [store] — removed: [old]. Now stored: [correct]."

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
- Trackers and balances: "pedicure package — 6 sessions remaining", "10 manicure sessions, used 3" → log_fyi with category="personal", NEVER add_daily_task
- "Ask X about Y" with no specific deadline or urgency → log_fyi, NOT a task
- Anything that's just good to know, even if it has a soft "might want to" action attached

RESOLVE STALE FYIs — when something gets confirmed or completed, archive the old pending FYI:
- Appointment confirmed → call read_fyis, find any "awaiting response / awaiting clinic / sent enquiry" FYIs for that same doctor/venue → archive them immediately
- Booking confirmed → archive any "looking into / pending / waiting to hear back" FYIs for that vendor
- Always clean up behind yourself: new confirmed info makes old pending info junk

add_daily_task when:
- Concrete future action with real intent to do it: "remind me to call", "we need to book", "don't forget to pay"
- Request directed at the other person: "can you follow up with the venue?", "Jess can you call the florist?"
- Has a clear owner and should appear on a to-do list

When in doubt: if it's something that already happened or is just good to know → FYI. If it needs someone to act → task. A soft curiosity ("I wonder if X", "might be worth asking about Y") is always an FYI, never a task.

TASK QUALITY RULES — enforce these strictly:
- Task names must be SHORT (under 80 chars). The action only — not the backstory. If you need to include context, log it as an FYI or wedding drop separately, then create a short task.
  WRONG: "Look into getting an OCBC credit card (any card) so you don't lose points. When ready, transfer $10k..."
  RIGHT: "Look into OCBC credit card for points"
- Social events / dinners with a confirmed date and time → create_calendar_event, NOT add_daily_task. If a task already exists for it, mark it done after creating the event.
- Package trackers / running balances ("10 manicure sessions, 7 remaining") → save to personal summary via save_preference, NOT add_daily_task
- Facts or preferences about either person ("Jess likes kaya waffle", "Ansen prefers window seats", "Jess is allergic to X") → save_preference for that person, NEVER add_daily_task or log_fyi. These are memory, not tasks.
- Items someone already owns or knows about ("AirPods are in the car") → log_fyi, NOT add_daily_task
- NEVER create a task that starts with "FYI" — that is always a log_fyi call
- If a statement describes a fact, trait, or preference about Ansen or Jess with no action required → save_preference, full stop. Do not create a task.
- Work documents, tool lists, policy docs, email summaries, "approved X list" — these are NEVER tasks. Do not create tasks from them. If anything, log_fyi or save_baby_knowledge if relevant.
- Never create a task with a body longer than a single clear sentence. If the content is a list or paragraph, it's not a task.

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
- Blank line between every bullet — not just between sections. Dense walls of text are unreadable on mobile.
- Start every bullet with a relevant emoji: 🏨 venue, 💰 budget, 📸 photography, 💄 hair/makeup, 👗 attire, 🎵 entertainment, 🍽️ catering, 💒 ceremony, 🌸 decor/flowers, 🥂 after-party, 🗓️ logistics, 🔥 urgent, ✅ confirmed, 🔍 in progress, ❌ untouched, 📅 calendar, 🎉 social/parties, 💪 task, 🚨 overdue

NEVER USE MARKDOWN TABLES — Telegram does not render them. Pipe characters (|) and dashes show up as raw ugly text.
WRONG (do not do this):
  | Okinawa | Nepal | Uzbekistan |
  |---------|-------|------------|
  | beaches | hiking | culture  |

For comparisons, ALWAYS use per-option sections instead:
  🇯🇵 <b>Okinawa</b>
  • 🏖 Vibe: beach resort, laid-back
  • 💰 Cost: high
  • ✈️ Visa: none

  🇳🇵 <b>Nepal</b>
  • 🏔 Vibe: adventure, trekking
  • 💰 Cost: mid
  • ✈️ Visa: on arrival

- Example of correct formatting:

  🚨 <b>Overdue</b>

  • 🏨 Venue — deposit due yesterday

  • 📸 Photographer — contract still unsigned

NOT this (wrong — dense, no emojis per bullet, asterisks show as raw text):
  **Overdue task**
  • Venue — deposit due yesterday
  • Photographer — contract still unsigned"""

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
        "name": "message_partner",
        "description": "Send an immediate message to the other person (Ansen or Jess). Use this whenever asked to 'notify', 'tell', 'message', 'ping', or 'let know' the partner. This is the ONLY reliable way to reach them right now — it fires within 30 seconds. Never claim you've notified someone without calling this tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The message to send. Write it naturally, as if from you — include context so they know what it's about."},
            },
            "required": ["message"],
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
                "recurrence": {"type": "string", "enum": ["none", "daily", "weekly", "monthly"], "description": "Repeat cadence. Default none. Use monthly for things like medication, subscriptions, bills."},
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
    {
        "name": "save_baby_knowledge",
        "description": "Save a piece of baby/pregnancy knowledge to the knowledge base. Use this when someone shares a screenshot, tip, advice from a friend, article snippet, or any useful info about pregnancy, birth, feeding, newborns, or parenting. Summarise the key point clearly, add relevant tags. This is for tacit knowledge — things you'd want to look up later.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Clear 1-3 sentence summary of the key knowledge. What's the actual advice/insight?",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-5 tags from: nutrition, symptoms, nausea, sleep, birth, epidural, hospital, feeding, breastfeeding, mental-health, exercise, medications, scans, trimester-1, trimester-2, trimester-3, newborn, finances, friends-advice",
                },
                "raw_text": {
                    "type": "string",
                    "description": "The original text extracted from the screenshot or message, if available.",
                },
            },
            "required": ["summary", "tags"],
        },
    },
    {
        "name": "log_baby_expense",
        "description": "Log a baby-related expense or planned purchase to the baby budget. Use when someone mentions buying, ordering, or planning to buy something for the baby, or shares a quote/price for baby gear, medical, or hospital costs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "What was bought or planned (e.g. 'Bugaboo Fox pram', 'Gleneagles delivery package', 'prenatal vitamins')"},
                "amount": {"type": "number", "description": "Cost in SGD (or leave out if unknown)"},
                "currency": {"type": "string", "description": "Currency code, default SGD"},
                "category": {"type": "string", "description": "One of: gear, medical, clothing, hospital, nutrition, other"},
                "status": {"type": "string", "enum": ["planned", "bought", "deposit", "quoted"], "description": "planned = intending to buy, bought = purchased, deposit = partial payment made, quoted = got a price"},
                "notes": {"type": "string", "description": "Any extra context"},
            },
            "required": ["item"],
        },
    },
    {
        "name": "read_baby_budget",
        "description": "Read the baby budget — all logged expenses and planned purchases with totals.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "log_shared_expense",
        "description": "Log a shared life expense — anything financial that is NOT wedding-specific or baby-specific. Examples: rent, cleaning, Airbnb accommodation, subscriptions, utilities, restaurant bills, home repairs, travel costs, car expenses.",
        "input_schema": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "What the expense is (e.g. 'Instant Cleaning', 'Airbnb TML 3 rooms', 'Netflix subscription')"},
                "amount": {"type": "number", "description": "Amount in SGD (or leave out if unknown)"},
                "currency": {"type": "string", "description": "Currency code, default SGD"},
                "category": {"type": "string", "description": "One of: home, travel, food, subscriptions, transport, medical, other"},
                "status": {"type": "string", "enum": ["owing", "paid", "pending", "quoted"], "description": "owing = still to pay, paid = settled, pending = awaiting invoice, quoted = got a price"},
                "notes": {"type": "string", "description": "Any extra context"},
            },
            "required": ["item"],
        },
    },
    {
        "name": "read_shared_budget",
        "description": "Read the shared life budget — all logged non-wedding, non-baby expenses with totals by category.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_baby_knowledge",
        "description": "Search the baby knowledge base for saved tips, advice, and resources. Use when someone asks what they know about a pregnancy/baby topic, or asks a question that might be answered by something they've previously saved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Topic or question to search for (e.g. 'epidurals', 'supplements', 'hospital')"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "correct_knowledge",
        "description": "ALWAYS call this when the user corrects something you stated ('no that's wrong', 'actually it's X', 'that's not right', 'you got that wrong'). Finds stale data across all persistent stores and replaces it with the correct version. Never just verbally acknowledge a correction without calling this — verbal acknowledgement alone means the same mistake will reappear next session.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Short label for what's being corrected, e.g. 'Dr Janice appointment date', 'pregnancy week count'"},
                "wrong_claim": {"type": "string", "description": "The incorrect information that was stated or stored. Use key phrases that would appear in stored text."},
                "correct_claim": {"type": "string", "description": "The accurate information the user provided. This will be written to persistent storage."},
                "stores": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["baby_knowledge", "shared_summary", "user_summary", "trips"]},
                    "description": "Which stores to search and correct. If unsure, include all relevant ones.",
                },
            },
            "required": ["topic", "correct_claim"],
        },
    },
    {
        "name": "save_show",
        "description": "Save a concert, show, gig, festival, or event to Ansen's upcoming shows list. Use when Ansen drops a ticket screenshot or mentions a show/event he has tickets for. Extract show name, venue, date, and time from the image or message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "show_name": {"type": "string", "description": "Name of the show, artist, event, or performance"},
                "venue": {"type": "string", "description": "Venue name and location"},
                "show_date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "show_time": {"type": "string", "description": "Time e.g. '8:00 PM' or '20:00'"},
                "notes": {"type": "string", "description": "Any extra details — support acts, door time, ticket ref, seat, etc."},
            },
            "required": ["show_name"],
        },
    },
    {
        "name": "save_trip",
        "description": "Save a new trip to the shared travel list. Use when either person mentions going somewhere — 'we're going to Japan', 'booked flights to Bali', 'planning a trip to Seoul'. After saving, search visa requirements for both Singapore and US passports and call update_trip to store the visa info.",
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string", "description": "City or country name"},
                "country": {"type": "string", "description": "Country name if different from destination"},
                "start_date": {"type": "string", "description": "Departure date in YYYY-MM-DD format"},
                "end_date": {"type": "string", "description": "Return date in YYYY-MM-DD format"},
                "status": {"type": "string", "enum": ["planning", "booked", "completed", "cancelled"], "description": "Trip status. Default: planning"},
                "notes": {"type": "string", "description": "Any initial notes — flight refs, hotel, purpose of trip"},
                "visibility": {"type": "string", "enum": ["shared", "ansen", "jess"], "description": "Who this trip belongs to. Default shared. Use 'ansen' or 'jess' if only one person is going."},
            },
            "required": ["destination"],
        },
    },
    {
        "name": "update_trip",
        "description": "Update an existing trip — status, visa info, notes, dates. Use after searching visa requirements to store the result. Also use when flights/hotels are booked, dates change, or any new info comes in.",
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string", "description": "Destination name to look up the trip (partial match)"},
                "status": {"type": "string", "enum": ["planning", "booked", "completed", "cancelled"]},
                "visa_ansen": {"type": "string", "description": "Visa requirement for Ansen (Singapore passport) e.g. 'Visa-free 90 days' or 'e-Visa required, apply at...'"},
                "visa_jess": {"type": "string", "description": "Visa requirement for Jess (US passport)"},
                "notes": {"type": "string", "description": "New note to append to the trip (will be added to existing notes)"},
                "start_date": {"type": "string", "description": "Updated start date YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "Updated end date YYYY-MM-DD"},
            },
            "required": ["destination"],
        },
    },
    {
        "name": "get_trips",
        "description": "List upcoming trips. Use when asked about travel plans, upcoming trips, or to check visa status for a destination.",
        "input_schema": {
            "type": "object",
            "properties": {
                "include_past": {"type": "boolean", "description": "Include completed/past trips. Default false."},
            },
        },
    },
    {
        "name": "delete_show",
        "description": "Remove a show from Ansen's list. Use when he says he can't go, sold the tickets, or wants it removed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "show_name": {"type": "string", "description": "Name of the show to remove (partial match is fine)"},
            },
            "required": ["show_name"],
        },
    },
    {
        "name": "update_show",
        "description": "Update a show's status or notes. Use when Ansen says he might not make it, sold a ticket, got an upgrade, or adds any context about a show.",
        "input_schema": {
            "type": "object",
            "properties": {
                "show_name": {"type": "string", "description": "Name of the show to update (partial match is fine)"},
                "status": {"type": "string", "enum": ["going", "maybe", "cant_go", "sold"], "description": "Attendance status: going (default), maybe, cant_go, sold"},
                "notes": {"type": "string", "description": "Additional context to save with the show"},
            },
            "required": ["show_name"],
        },
    },
    {
        "name": "read_stocks_history",
        "description": "Read past investment briefs — what newsletters have been saying about stocks, crypto, and ETFs over recent weeks. Use for any finance/investment question: 'is now a good time to buy X', 'what's the outlook on BTC', 'what have the newsletters said about Y'. Always call this before giving investment opinions so your answer reflects actual newsletter signals, not just general knowledge.",
        "input_schema": {
            "type": "object",
            "properties": {
                "asset": {
                    "type": "string",
                    "description": "Optional: filter to a specific asset name or ticker (e.g. 'bitcoin', 'BTC', 'Apple', 'ETH'). Omit to get the full recent briefs.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of past briefs to read. Default 4 (last ~4 weeks).",
                },
            },
        },
    },
    {
        "name": "get_grocery_lists",
        "description": "Read all active grocery shopping lists with their items. Call whenever the user asks what's on the grocery list, what needs to be bought, or wants to see the shopping list.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_grocery_items",
        "description": "Add one or more items to a grocery list. Creates the list if it doesn't exist. Use whenever user says 'add X to grocery list', 'we need X', 'get X from the store', 'pick up X', 'buy X', 'add to groceries', 'prenatal vitamins', 'hand warmers' etc. Always use this when shopping-related items are mentioned.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "string"}, "description": "List of items to add"},
                "list_name": {"type": "string", "description": "Name of the grocery list. Default: 'Groceries'. Use a descriptive name if user specifies a trip (e.g. 'Baby supplies', 'IKEA run', 'Weekend shop')."},
            },
            "required": ["items"],
        },
    },
    {
        "name": "remove_grocery_item",
        "description": "Remove an item from a grocery list. Use when user says 'remove X', 'take X off the list', 'we don't need X anymore'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "Item name to remove"},
                "list_name": {"type": "string", "description": "Name of the list. Default: 'Groceries'."},
            },
            "required": ["item"],
        },
    },
    {
        "name": "check_off_grocery_item",
        "description": "Mark a grocery item as bought/got. Use when user says 'got X', 'bought X', 'picked up X', 'found X'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "item": {"type": "string"},
                "list_name": {"type": "string", "description": "Default: 'Groceries'."},
            },
            "required": ["item"],
        },
    },
    {
        "name": "close_grocery_list",
        "description": "Mark a grocery shopping trip as done (all bought). Use when user says 'done with shopping', 'grocery run complete', 'finished the shop'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "list_name": {"type": "string"},
            },
            "required": ["list_name"],
        },
    },
    {
        "name": "create_goal",
        "description": "Create a multi-step goal with sequential steps and dependencies. Use when someone describes a complex task that has distinct stages (e.g. 'book the venue', 'apply for Korea visa', 'plan the babymoon'). Prefer this over a pile of individual tasks when steps must happen in order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What you're trying to achieve"},
                "visibility": {"type": "string", "enum": ["private", "shared"]},
                "category": {"type": "string", "description": "Category slug: wedding, baby, travel, finance, health, work, social, personal"},
                "steps": {
                    "type": "array",
                    "description": "Ordered steps. List in execution order — step 0 first.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "due_date": {"type": "string", "description": "YYYY-MM-DD or omit"},
                            "assigned_to": {"type": "integer", "description": "Ansen=63756531, Jess=6927468999"},
                            "blocked_by_index": {"type": "integer", "description": "0-based index of the step in this list that must complete before this one starts. Omit if this step can start immediately."},
                        },
                        "required": ["title"],
                    },
                },
            },
            "required": ["title", "visibility"],
        },
    },
    {
        "name": "get_goals",
        "description": "List goals and their next available steps. Call when user asks about their goals, what's in progress, or what to do next on a multi-step project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["active", "paused", "done"], "description": "Default: active"},
            },
        },
    },
    {
        "name": "complete_goal_step",
        "description": "Mark a step in a goal as done. Call when the user says they've completed a specific step. Returns what's newly unblocked.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_title": {"type": "string", "description": "Goal title — used to find the right goal"},
                "step_title": {"type": "string", "description": "The step to mark done — fuzzy matched against step titles"},
            },
            "required": ["goal_title", "step_title"],
        },
    },
    {
        "name": "update_goal",
        "description": "Change a goal's status — pause it, reactivate it, or mark it done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_title": {"type": "string"},
                "status": {"type": "string", "enum": ["active", "paused", "done"]},
            },
            "required": ["goal_title", "status"],
        },
    },
]


class UnifiedAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self._wedding = WeddingAgent()
        self._daily = DailyAgent()

    _USER_NAMES = {63756531: "Ansen", 6927468999: "Jess"}

    def _build_system(self, user_summary: str = "", shared_summary: str = "", user_id: int = 0, recent_fyis: str = "", baby_context: str = "", mem0_context: str = "") -> str:
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
            today=_local_today().isoformat(),
            timezone=os.getenv("REMINDER_TZ", "Asia/Singapore"),
            user_summary=(current_user_line + "\n\n") + (user_summary or "Nothing yet — this is the start of our history together."),
            shared_summary=shared_summary or "Nothing shared yet.",
            recent_fyis=recent_fyis or "No recent FYIs.",
            baby_context=baby_context or "No baby knowledge saved yet.",
            mem0_context=mem0_context or "No specific memories recalled for this query.",
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

        if name == "message_partner":
            import os
            from zoneinfo import ZoneInfo
            from datetime import timezone as _tz, timedelta as _td
            tz = ZoneInfo(os.getenv("REMINDER_TZ", "Asia/Singapore"))
            partner_ids = [int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip() and int(x.strip()) != user_id]
            if not partner_ids:
                return {"error": "No partner found in ALLOWED_USER_IDS"}
            fire_at = datetime.now(_tz.utc) + _td(seconds=30)
            ids_sent = []
            for pid in partner_ids:
                notif = _sched_notif(pid, inputs["message"], fire_at, "none")
                ids_sent.append(str(notif["id"]))
            partner_name = next((n for uid, n in self._USER_NAMES.items() if uid != user_id), "your partner")
            return {"status": "sent", "to": partner_name, "fires_in": "30 seconds"}

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
            await self._upsert_shared(content)
            return {"status": "saved", "content": content}

        if name == "save_baby_knowledge":
            entry = save_baby_entry(
                summary=inputs["summary"],
                tags=inputs.get("tags", []),
                raw_text=inputs.get("raw_text", ""),
                user_id=user_id,
            )
            flags["baby_drop"] = True
            return {"status": "saved", "id": entry.get("id"), "tags": inputs.get("tags", [])}

        if name == "log_baby_expense":
            entry = add_baby_budget_item(
                item=inputs["item"],
                amount=inputs.get("amount"),
                category=inputs.get("category"),
                status=inputs.get("status", "planned"),
                currency=inputs.get("currency", "SGD"),
                notes=inputs.get("notes"),
            )
            flags["baby_drop"] = True
            return {"status": "saved", "item": inputs["item"], "amount": inputs.get("amount")}

        if name == "read_baby_budget":
            return baby_budget_summary()

        if name == "log_shared_expense":
            add_shared_budget_item(
                item=inputs["item"],
                amount=inputs.get("amount"),
                category=inputs.get("category"),
                status=inputs.get("status", "owing"),
                currency=inputs.get("currency", "SGD"),
                notes=inputs.get("notes"),
            )
            return {"status": "saved", "item": inputs["item"], "amount": inputs.get("amount")}

        if name == "read_shared_budget":
            return shared_budget_summary()

        if name == "search_baby_knowledge":
            results = search_baby_entries(inputs["query"])
            if not results:
                return {"found": 0, "entries": []}
            return {
                "found": len(results),
                "entries": [{"summary": e["summary"], "tags": e.get("tags", [])} for e in results],
            }

        if name == "correct_knowledge":
            from tools.baby_knowledge import (
                get_entries as _bk_get, delete_entry as _bk_del,
                update_entry as _bk_update, save_entry as _bk_save,
            )
            from tools.user_memory import (
                get_summary as _um_get, save_summary as _um_save,
                get_shared_summary as _shared_get, append_shared_summary as _shared_append,
                get_message_count as _um_count,
            )
            topic = inputs["topic"]
            wrong = (inputs.get("wrong_claim") or "").lower()
            correct = inputs["correct_claim"]
            stores = inputs.get("stores") or ["baby_knowledge", "shared_summary", "user_summary", "trips"]
            report: dict = {"topic": topic, "fixed_in": [], "removed": [], "added": []}

            # --- baby_knowledge ---
            if "baby_knowledge" in stores:
                all_entries = _bk_get(limit=200)
                wrong_words = [w for w in wrong.split() if len(w) > 3]
                stale = []
                for e in all_entries:
                    text = (e.get("summary") or "") + " " + (e.get("raw_text") or "")
                    if wrong and any(w in text.lower() for w in wrong_words):
                        stale.append(e)
                for e in stale:
                    _bk_del(str(e["id"]))
                    report["removed"].append(e["summary"][:120])
                if stale or "baby_knowledge" in stores:
                    new_entry = _bk_save(
                        summary=f"[CORRECTION — {topic}] {correct}",
                        tags=["correction", topic.lower().replace(" ", "_")],
                        source="correction",
                    )
                    report["added"].append(f"baby_knowledge: {correct[:120]}")
                    report["fixed_in"].append("baby_knowledge")

            # --- shared_summary ---
            if "shared_summary" in stores:
                shared = _shared_get()
                if wrong and any(w in shared.lower() for w in (wrong.split() if wrong else [])):
                    # Replace the wrong line(s) in the summary
                    lines = shared.split("\n")
                    wrong_words_set = set(w for w in wrong.split() if len(w) > 3)
                    cleaned = [l for l in lines if not any(w in l.lower() for w in wrong_words_set)]
                    if len(cleaned) < len(lines):
                        new_shared = "\n".join(cleaned).strip()
                        from tools.db import get_client as _gc
                        from datetime import datetime as _dt2, timezone as _tz2
                        _gc().table("user_summaries").upsert({
                            "user_id": 0, "summary": new_shared,
                            "updated_at": _dt2.now(_tz2.utc).isoformat(), "message_count": 0,
                        }).execute()
                        report["fixed_in"].append("shared_summary")
                await self._upsert_shared(f"[CORRECTION — {topic}] {correct}")
                report["added"].append(f"shared_summary: {correct[:120]}")

            # --- user_summary for both users ---
            if "user_summary" in stores:
                for uid in self._USER_NAMES:
                    summary = _um_get(uid)
                    if wrong and summary and any(w in summary.lower() for w in (wrong.split() if wrong else [])):
                        wrong_words_set = set(w for w in wrong.split() if len(w) > 3)
                        lines = summary.split("\n")
                        cleaned = [l for l in lines if not any(w in l.lower() for w in wrong_words_set)]
                        if len(cleaned) < len(lines):
                            _um_save(uid, "\n".join(cleaned).strip(), _um_count(uid))
                            report["fixed_in"].append(f"user_summary:{uid}")

            # --- trips ---
            if "trips" in stores and wrong:
                from tools.trips import get_all_trips, append_trip_note as _atn
                all_trips = get_all_trips()
                wrong_words_set = set(w for w in wrong.split() if len(w) > 3)
                for t in all_trips:
                    notes = (t.get("notes") or "").lower()
                    if any(w in notes for w in wrong_words_set):
                        _atn(str(t["id"]), f"[CORRECTION — {topic}] {correct}")
                        report["fixed_in"].append(f"trip:{t['destination']}")

            if not report["fixed_in"]:
                _bk_save(
                    summary=f"[CORRECTION — {topic}] {correct}",
                    tags=["correction", topic.lower().replace(" ", "_")],
                    source="correction",
                )
                report["added"].append(f"baby_knowledge (new): {correct[:120]}")
                report["fixed_in"].append("baby_knowledge")

            return report

        if name == "save_trip":
            from tools.trips import add_trip
            trip = add_trip(
                destination=inputs["destination"],
                country=inputs.get("country"),
                start_date=inputs.get("start_date"),
                end_date=inputs.get("end_date"),
                status=inputs.get("status", "planning"),
                notes=inputs.get("notes"),
                visibility=inputs.get("visibility", "shared"),
            )
            result: dict = {"status": "saved", "id": str(trip["id"]), "destination": inputs["destination"]}
            gap = _trip_gap_check(trip)
            if gap:
                result["gap_warning"] = gap
            return result

        if name == "update_trip":
            from tools.trips import find_trips_by_destination, update_trip as _update_trip, append_trip_note, get_trip_by_id as _get_trip_by_id
            matches = find_trips_by_destination(inputs["destination"])
            if not matches:
                return {"status": "not_found", "destination": inputs["destination"]}
            trip = matches[0]
            kwargs = {}
            for field in ("status", "visa_ansen", "visa_jess", "start_date", "end_date"):
                if inputs.get(field):
                    kwargs[field] = inputs[field]
            if kwargs:
                _update_trip(trip["id"], **kwargs)
            if inputs.get("notes"):
                append_trip_note(trip["id"], inputs["notes"])
            updated = _get_trip_by_id(str(trip["id"])) or trip
            result: dict = {"status": "updated", "destination": updated["destination"]}
            gap = _trip_gap_check(updated)
            if gap:
                result["gap_warning"] = gap
            return result

        if name == "get_trips":
            from tools.trips import get_upcoming_trips, get_all_trips
            trips = get_all_trips() if inputs.get("include_past") else get_upcoming_trips()
            return [
                {
                    "id": str(t["id"]),
                    "destination": t["destination"],
                    "start_date": t.get("start_date"),
                    "end_date": t.get("end_date"),
                    "status": t.get("status"),
                    "visa_ansen": t.get("visa_ansen"),
                    "visa_jess": t.get("visa_jess"),
                    "notes": t.get("notes"),
                }
                for t in trips
            ]

        if name == "save_show":
            from tools.shows import add_show
            show = add_show(
                show_name=inputs["show_name"],
                venue=inputs.get("venue"),
                show_date=inputs.get("show_date"),
                show_time=inputs.get("show_time"),
                notes=inputs.get("notes"),
            )
            return {"status": "saved", "id": str(show["id"]), "show_name": inputs["show_name"]}

        if name == "delete_show":
            from tools.shows import find_shows_by_name, delete_show as _delete_show
            matches = find_shows_by_name(inputs["show_name"])
            if not matches:
                return {"status": "not_found", "show_name": inputs["show_name"]}
            show = matches[0]
            _delete_show(show["id"])
            return {"status": "deleted", "show_name": show["show_name"]}

        if name == "update_show":
            from tools.shows import find_shows_by_name, update_show as _update_show
            matches = find_shows_by_name(inputs["show_name"])
            if not matches:
                return {"status": "not_found", "show_name": inputs["show_name"]}
            show = matches[0]
            _update_show(show["id"], status=inputs.get("status"), notes=inputs.get("notes"))
            return {"status": "updated", "show_name": show["show_name"], "new_status": inputs.get("status")}

        if name == "read_stocks_history":
            from tools.stocks_knowledge import get_recent_briefs, search_asset
            asset = inputs.get("asset", "").strip()
            limit = int(inputs.get("limit") or 4)
            if asset:
                return search_asset(asset, limit=limit)
            rows = get_recent_briefs(limit=limit)
            # Return structured data without the full brief_text (too large for tool result)
            return [
                {
                    "brief_date": r["brief_date"],
                    "assets": r.get("assets") or [],
                }
                for r in rows
            ]

        if name == "get_grocery_lists":
            from tools.groceries import get_active_lists
            lists = get_active_lists()
            return [
                {
                    "id": str(lst["id"]),
                    "name": lst["name"],
                    "item_count": len(lst["items"]),
                    "items": [{"id": str(it["id"]), "item": it["item"], "quantity": it.get("quantity")} for it in lst["items"]],
                }
                for lst in lists
            ]

        if name == "add_grocery_items":
            from tools.groceries import get_or_create_list, add_items
            list_name = (inputs.get("list_name") or "Groceries").strip()
            lst = get_or_create_list(list_name, user_id)
            added = add_items(lst["id"], inputs.get("items", []), user_id)
            flags["grocery_update"] = {
                "action": "add",
                "items": [it["item"] for it in added],
                "list_name": lst["name"],
            }
            return {"status": "added", "list": lst["name"], "items": [it["item"] for it in added]}

        if name == "remove_grocery_item":
            from tools.groceries import get_list_by_name, remove_item_by_text
            list_name = (inputs.get("list_name") or "Groceries").strip()
            lst = get_list_by_name(list_name)
            if not lst:
                return {"error": f"No active list named '{list_name}'"}
            removed = remove_item_by_text(lst["id"], inputs["item"])
            if removed:
                flags["grocery_update"] = {
                    "action": "remove",
                    "items": [inputs["item"]],
                    "list_name": lst["name"],
                }
            return {"status": "removed" if removed else "not_found", "item": inputs["item"]}

        if name == "check_off_grocery_item":
            from tools.groceries import get_list_by_name, check_off_item
            list_name = (inputs.get("list_name") or "Groceries").strip()
            lst = get_list_by_name(list_name)
            if not lst:
                return {"error": f"No active list named '{list_name}'"}
            ok = check_off_item(lst["id"], inputs["item"])
            return {"status": "checked_off" if ok else "not_found", "item": inputs["item"]}

        if name == "close_grocery_list":
            from tools.groceries import get_list_by_name, close_list
            lst = get_list_by_name(inputs["list_name"])
            if not lst:
                return {"error": f"No active list named '{inputs['list_name']}'"}
            close_list(lst["id"])
            return {"status": "closed", "list": lst["name"]}

        if name == "create_goal":
            from tools.goals import create_goal as _create_goal, add_step as _add_step
            goal = _create_goal(
                user_id=user_id,
                title=inputs["title"],
                visibility=inputs.get("visibility", "shared"),
                category=inputs.get("category"),
            )
            goal_id = goal["id"]
            steps_input = inputs.get("steps") or []
            step_ids: list[str] = []
            created_steps = []
            for i, s in enumerate(steps_input):
                bi = s.get("blocked_by_index")
                blocked_by_id = step_ids[bi] if (bi is not None and 0 <= bi < len(step_ids)) else None
                step = _add_step(
                    goal_id=goal_id,
                    title=s["title"],
                    sort_order=i,
                    blocked_by=blocked_by_id,
                    due_date=s.get("due_date"),
                    assigned_to=s.get("assigned_to"),
                )
                step_ids.append(step["id"])
                created_steps.append({"title": s["title"], "blocked_by_index": bi})
            return {"status": "created", "goal_id": str(goal_id), "title": inputs["title"], "steps_created": len(created_steps), "steps": created_steps}

        if name == "get_goals":
            from tools.goals import get_goals as _get_goals, get_next_steps as _get_next
            goals = _get_goals(status=inputs.get("status", "active"))
            result = []
            for g in goals:
                all_steps = g.get("goal_steps", [])
                done_count = sum(1 for s in all_steps if s["status"] == "done")
                next_steps = _get_next(g["id"])
                result.append({
                    "id": str(g["id"]),
                    "title": g["title"],
                    "category": g.get("category"),
                    "visibility": g["visibility"],
                    "progress": f"{done_count}/{len(all_steps)} steps done",
                    "next_steps": [{"id": str(s["id"]), "title": s["title"], "due_date": s.get("due_date")} for s in next_steps],
                })
            return result

        if name == "complete_goal_step":
            from tools.goals import get_goals as _get_goals, complete_step as _complete_step, find_step_by_title as _find_step
            goals = _get_goals(status="active")
            goal_title_lower = inputs["goal_title"].lower()
            matched = next((g for g in goals if goal_title_lower in g["title"].lower() or g["title"].lower() in goal_title_lower), None)
            if not matched:
                return {"error": f"No active goal matching '{inputs['goal_title']}'"}
            step = _find_step_by_title(matched["id"], inputs["step_title"])
            if not step:
                return {"error": f"No step matching '{inputs['step_title']}' in goal '{matched['title']}'"}
            result = _complete_step(step["id"])
            flags.setdefault("completed_tasks", []).append(f"[{matched['title']}] {step['title']}")
            return result

        if name == "update_goal":
            from tools.goals import get_goals as _get_goals, update_goal_status as _upd
            all_goals = _get_goals("active") + _get_goals("paused") + _get_goals("done")
            title_lower = inputs["goal_title"].lower()
            matched = next((g for g in all_goals if title_lower in g["title"].lower()), None)
            if not matched:
                return {"error": f"No goal matching '{inputs['goal_title']}'"}
            _upd(matched["id"], inputs["status"])
            return {"status": "updated", "goal": matched["title"], "new_status": inputs["status"]}

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

    @staticmethod
    def _sanitize_history(messages: list) -> list:
        """Ensure history starts at a clean boundary — no orphaned tool_result blocks.

        Trimming to the last N messages can split a tool_use/tool_result pair, leaving
        a tool_result with no matching tool_use in the previous message. The Anthropic API
        returns a 400 in that case. We scan forward to the first message that is either:
        - a plain-text user message (string content), or
        - a user message whose content list contains no tool_result blocks
        and drop everything before it.
        """
        for i, m in enumerate(messages):
            if m.get("role") != "user":
                continue
            content = m.get("content", "")
            if isinstance(content, str):
                return messages[i:]
            if isinstance(content, list):
                has_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                )
                if not has_tool_result:
                    return messages[i:]
        return []  # nothing clean — start fresh

    async def _upsert_shared(self, new_content: str) -> str:
        """Write to shared brain with conflict resolution — replaces stale entries on the same topic."""
        from tools.user_memory import get_shared_summary as _get_shared, save_summary as _save_sum, get_message_count as _mc
        from tools.db import get_client as _gc
        from datetime import datetime as _dt2, timezone as _tz2

        existing = _get_shared()
        today = _local_today().isoformat()
        new_bullet = f"• {today}: {new_content}"

        if not existing:
            _gc().table("user_summaries").upsert({
                "user_id": 0, "summary": new_bullet,
                "updated_at": _dt2.now(_tz2.utc).isoformat(), "message_count": 0,
            }).execute()
            return new_bullet

        prompt = f"""You maintain a shared brain — a list of confirmed facts for a couple. Each entry is a bullet: • YYYY-MM-DD: [fact]

Current shared brain:
{existing}

New information to incorporate:
{new_content}

Task: Return the updated shared brain.
Rules:
1. If the new info updates something already present (same topic, same entity, new value) → REPLACE the old bullet with the new one. Do NOT keep both.
2. If the new info contradicts an existing entry → keep ONLY the new one.
3. If the new info is genuinely new (no related entry exists) → ADD it as a new bullet with today's date ({today}).
4. Keep entries lean — one fact per bullet, no commentary.
5. Return ONLY the bullets, nothing else. No headers, no explanation."""

        try:
            response = await self.client.messages.create(
                model=CHAT_MODEL,
                max_tokens=1200,
                messages=[{"role": "user", "content": prompt}],
            )
            updated = response.content[0].text.strip()
        except Exception:
            # Fallback: plain append
            updated = f"{existing}\n{new_bullet}".strip()

        _gc().table("user_summaries").upsert({
            "user_id": 0, "summary": updated,
            "updated_at": _dt2.now(_tz2.utc).isoformat(), "message_count": 0,
        }).execute()
        return updated

    async def _run_loop(self, user_content, user_id: int, history: list, user_summary: str, shared_summary: str = "", recent_fyis: str = "", baby_context: str = "", mem0_context: str = "") -> dict:
        import logging as _logging
        flags = {"wedding_drop": False, "fyi": False, "baby_drop": False, "summary_updated": False, "completed_tasks": [], "grocery_update": None}
        messages = self._sanitize_history(history) + [{"role": "user", "content": user_content}]
        system_prompt = self._build_system(user_summary, shared_summary, user_id, recent_fyis, baby_context, mem0_context)
        last_response = None

        for _ in range(10):
            last_response = await self.client.messages.create(
                model=CHAT_MODEL,
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
                updated_history = self._sanitize_history(self._strip_image_data(messages[-40:]))

                try:
                    msg_count = get_message_count(user_id) + 1
                    if msg_count % 2 == 0:
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

                return {"text": reply, "history": updated_history, "notify_partner": flags["wedding_drop"] or flags["fyi"] or flags["baby_drop"], "completed_tasks": flags["completed_tasks"], "grocery_update": flags["grocery_update"]}

            if last_response.stop_reason == "max_tokens":
                reply = next((b.text for b in last_response.content if hasattr(b, "text")), "Got it.")
                messages.append({"role": "assistant", "content": reply})
                return {"text": reply, "history": self._sanitize_history(self._strip_image_data(messages[-40:])), "notify_partner": flags["wedding_drop"] or flags["fyi"] or flags["baby_drop"], "grocery_update": flags["grocery_update"]}

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
        today = _local_today()

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
            model=CHAT_MODEL,
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
            model=CHAT_MODEL,
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
            await self._upsert_shared(f"📊 Stocks {today}: {', '.join(sigs)}")
        except Exception:
            pass

        # ── STEP 7: persist brief for conversational recall ───────────────
        try:
            from tools.stocks_knowledge import save_brief as _save_brief
            _brief_assets = [
                {
                    "name": a["name"],
                    "ticker": a.get("ticker", ""),
                    "type": a.get("type", "other"),
                    "sentiment": a.get("sentiment", "neutral"),
                    "thesis": a.get("thesis", ""),
                }
                for a in researched
            ]
            await asyncio.to_thread(_save_brief, today, _brief_assets, brief_text)
        except Exception:
            pass

        return brief_text

    async def handle_message(self, text: str, user_id: int, history: list[dict] | None = None) -> dict:
        if history is None:
            history = []

        # Fetch all context concurrently
        async def _get_user_summary():
            try:
                return get_summary(user_id)
            except Exception:
                return ""

        async def _get_shared():
            try:
                return get_shared_summary()
            except Exception:
                return ""

        async def _get_fyis():
            try:
                from tools.fyis import get_fyis_for_context
                _fyis = get_fyis_for_context(limit=15)
                return "\n".join(
                    f"[{f.get('category', 'misc')}] ({(f.get('created_at') or '')[:10]}) {f['content']}"
                    for f in _fyis
                )
            except Exception:
                return ""

        async def _get_baby():
            try:
                from tools.baby import pregnancy_summary as _ps
                ps = _ps()
                header = (
                    f"CURRENT PREGNANCY: Week {ps['week']}, Day {ps['day']} of this week. "
                    f"Trimester {ps['trimester']}. Due date: {ps['due_date']}. "
                    f"{ps['days_until_due']} days until due. "
                    f"LMP: {ps['lmp']}. "
                    f"ALWAYS use this week number — never guess or recall a different one."
                )
            except Exception:
                header = ""
            try:
                from tools.baby_knowledge import get_entries as _gb
                _baby = _gb(limit=10)
                entries = "\n".join(
                    f"[{', '.join(e.get('tags') or [])}] {e['summary']}"
                    for e in _baby
                )
            except Exception:
                entries = ""
            return (header + "\n\n" + entries).strip() if header else entries

        async def _get_mem0():
            try:
                from tools.mem0_memory import search_memories as _search
                return await asyncio.to_thread(_search, text, user_id, 6)
            except Exception:
                return ""

        user_summary, shared_summary, recent_fyis, baby_context, mem0_context = await asyncio.gather(
            _get_user_summary(), _get_shared(), _get_fyis(), _get_baby(), _get_mem0()
        )

        result = await self._run_loop(
            text, user_id, history, user_summary, shared_summary, recent_fyis, baby_context, mem0_context
        )

        # Async: store this exchange in mem0 for future recall (non-blocking)
        reply_text = result.get("text", "")
        if reply_text and text:
            try:
                from tools.mem0_memory import add_exchange as _add
                asyncio.create_task(asyncio.to_thread(_add, text, reply_text, user_id))
            except Exception:
                pass

        return result

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
                model=SYNTHESIS_MODEL,
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
        """Agentic proactive intelligence check — can use tools to research and surface insights."""
        import os, json as _json
        from datetime import date as _date, datetime as _datetime

        today = __local_today()
        today_str = today.isoformat()
        tz_name = os.getenv("REMINDER_TZ", "Asia/Singapore")
        wedding_days = (_date(2026, 11, 7) - today).days

        # --- Gather base context ---
        profile = ""
        try:
            profile = get_summary(user_id) or ""
        except Exception:
            pass

        shared = ""
        try:
            shared = get_shared_summary() or ""
        except Exception:
            pass

        # Tasks with staleness flags
        task_lines = []
        try:
            for t in get_tasks(user_id, include_done=False):
                created = (t.get("created_at") or today_str)[:10]
                try:
                    age_days = (today - _date.fromisoformat(created)).days
                except ValueError:
                    age_days = 0
                due = t.get("due_date") or "no date"
                overdue = due != "no date" and due < today_str
                flags = []
                if overdue:
                    flags.append("OVERDUE")
                if age_days >= 14:
                    flags.append(f"stale {age_days}d")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                task_lines.append(f"  • {t['task']} (due: {due}){flag_str}")
        except Exception:
            pass
        tasks_block = ("OPEN TASKS:\n" + "\n".join(task_lines)) if task_lines else "OPEN TASKS: none"

        # Recent FYIs
        fyi_lines = []
        try:
            from tools.fyis import get_fyis as _get_fyis
            for f in _get_fyis(limit=20):
                when = (f.get("created_at") or "")[:10]
                cat = f.get("category") or "misc"
                fyi_lines.append(f"  [{cat}] {when}: {f['content']}")
        except Exception:
            pass
        fyis_block = ("RECENT FYIs:\n" + "\n".join(fyi_lines)) if fyi_lines else "RECENT FYIs: none"

        # Calendar — next 21 days, bucketed by proximity so Claude prioritises correctly
        _within_48h: list[str] = []
        _within_7d:  list[str] = []
        _beyond:     list[str] = []
        try:
            for e in (await asyncio.to_thread(get_events, 21))[:20]:
                raw_start = e["start"]
                event_date = raw_start[:10]
                try:
                    days_until = (_date.fromisoformat(event_date) - today).days
                except ValueError:
                    days_until = 99
                if "T" in raw_start:
                    try:
                        label = _datetime.fromisoformat(raw_start).strftime("%-d %b %H:%M")
                    except ValueError:
                        label = raw_start
                else:
                    try:
                        label = _datetime.strptime(raw_start, "%Y-%m-%d").strftime("%-d %b")
                    except ValueError:
                        label = raw_start
                loc = f" @ {e['location']}" if e.get("location") else ""
                entry = f"  • {label} — {e['title']}{loc}"
                if days_until <= 2:
                    _within_48h.append(entry)
                elif days_until <= 7:
                    _within_7d.append(entry)
                else:
                    _beyond.append(entry)
        except Exception:
            pass
        cal_parts = []
        if _within_48h:
            cal_parts.append("⚡ IMMINENT (0–2 days):\n" + "\n".join(_within_48h))
        if _within_7d:
            cal_parts.append("📅 THIS WEEK (3–7 days):\n" + "\n".join(_within_7d))
        if _beyond:
            cal_parts.append("🗓 UPCOMING (8–21 days):\n" + "\n".join(_beyond))
        cal_block = "CALENDAR:\n\n" + "\n\n".join(cal_parts) if cal_parts else "CALENDAR: none"

        # Wedding category activity
        wedding_lines = []
        try:
            all_drops = get_recent_drops(limit=200)
            last_drop_by_cat: dict[str, str] = {}
            for d in all_drops:
                cat = d.get("category") or "general"
                if cat not in last_drop_by_cat:
                    last_drop_by_cat[cat] = d["ts"][:10]
            for cat_key, cat_info in CATEGORIES.items():
                last = last_drop_by_cat.get(cat_key)
                if last:
                    try:
                        days_ago = (today - _date.fromisoformat(last)).days
                        wedding_lines.append(f"  • {cat_info['name']}: last activity {days_ago}d ago")
                    except ValueError:
                        wedding_lines.append(f"  • {cat_info['name']}: {last}")
                else:
                    wedding_lines.append(f"  • {cat_info['name']}: NO ACTIVITY YET")
        except Exception:
            pass
        wedding_block = ("WEDDING CATEGORIES:\n" + "\n".join(wedding_lines)) if wedding_lines else "WEDDING CATEGORIES: unavailable"

        # Baby context
        baby_block = ""
        try:
            info = pregnancy_summary()
            milestones = upcoming_milestones(within_weeks=4)
            baby_block = f"PREGNANCY: Week {info['week']} of {info['total_weeks']}, due {info['due_date']}\n"
            if milestones:
                baby_block += "UPCOMING MILESTONES:\n" + "\n".join(f"  • {m}" for m in milestones[:5])
        except Exception:
            pass

        # Upcoming trips
        trips_block = ""
        try:
            from tools.trips import get_upcoming_trips as _get_trips
            upcoming_trips = _get_trips()
            if upcoming_trips:
                trip_lines = []
                for t in upcoming_trips:
                    dest = t["destination"]
                    dates = " – ".join(x for x in [t.get("start_date") or "", t.get("end_date") or ""] if x) or "dates TBC"
                    va = t.get("visa_ansen") or "not checked"
                    vj = t.get("visa_jess") or "not checked"
                    trip_lines.append(f"  • {dest} ({dates}) | Ansen visa: {va} | Jess visa: {vj} | status: {t.get('status','planning')}")
                trips_block = "UPCOMING TRIPS:\n" + "\n".join(trip_lines)
        except Exception:
            pass

        # Upcoming shows — Ansen only
        shows_block = ""
        if user_id == 63756531:
            try:
                from tools.shows import get_upcoming_shows as _get_shows
                from datetime import timedelta as _td
                cutoff = (today + _td(days=21)).isoformat()
                shows = [s for s in _get_shows() if (s.get("show_date") or "9999") <= cutoff]
                if shows:
                    show_lines = []
                    for s in shows:
                        dt_raw = s.get("show_date") or "TBC"
                        if dt_raw != "TBC":
                            try:
                                dt_raw = _datetime.strptime(dt_raw, "%Y-%m-%d").strftime("%-d %b")
                            except Exception:
                                pass
                        tm = s.get("show_time") or ""
                        venue = s.get("venue") or ""
                        cal = " [in calendar]" if s.get("calendar_added") else " [NOT in calendar]"
                        show_lines.append(f"  • {s['show_name']} — {dt_raw} {tm} {venue}{cal}")
                    shows_block = "UPCOMING SHOWS (next 21 days):\n" + "\n".join(show_lines)
            except Exception:
                pass

        # --- Load previous state for gap de-duplication ---
        prev_output = ""
        prev_date = ""
        try:
            from tools.proactive_state import load_state as _load_proactive_state
            _prev = _load_proactive_state(user_id)
            prev_output = _prev.get("last_output") or ""
            prev_date = _prev.get("last_run_date") or ""
        except Exception:
            pass

        prev_block = ""
        if prev_output and prev_date:
            prev_block = f"""PREVIOUSLY FLAGGED — you said this on {prev_date}:
{prev_output}

OPEN GAP RULES — apply before deciding what to surface:
• If a previous gap now has an OPEN TASK addressing it → being handled, skip it
• If a previous gap is resolved (visa field now set, booking confirmed in FYIs/brain) → skip it
• Only re-surface a previous gap if: (a) event is now ≤7 days away, or (b) genuinely new information changes the picture
• Gaps NOT in the previous list → surface as normal if actionable"""

        # --- Proactive tools available to this check ---
        proactive_tools = [t for t in TOOLS if t["name"] in ("search_web", "read_calendar", "read_daily_tasks", "read_fyis")]

        system = f"""You are a proactive intelligence agent for {user_name}. Today is {today_str} ({tz_name}).

IDENTITIES:
- Ansen: Singaporean passport
- Jess / Jessica: US passport (American)
- Wedding: 7 November 2026 ({wedding_days} days away)
- Baby due: 18 February 2027

YOUR CONTEXT:
{profile or "(no profile)"}

SHARED BRAIN:
{shared or "(empty)"}

{tasks_block}

{fyis_block}

{cal_block}

{wedding_block}

{baby_block}

{trips_block}

{shows_block}

{prev_block}

---

YOUR JOB: Scan all context above and surface anything genuinely worth flagging to {user_name}. Work in priority order — imminent events first, then this week, then general intelligence.

PRIORITY ORDER:
1. ⚡ IMMINENT (calendar events within 48h): check EACH imminent event against every data source — do they need to bring anything? Are there questions to ask? Visa issues? Related tasks still open? Pregnancy considerations?
2. 📅 THIS WEEK (events in next 3–7 days): flag prep items, visa applications needing lead time, booking deadlines, OBGYN sign-off for travel
3. 🔍 GENERAL: wedding urgency, baby milestones, stale tasks, finance

INTELLIGENCE TRIGGERS — actively look for these:

🌍 TRAVEL
- Any destination mentioned in FYIs, calendar, or tasks → search visa requirements for BOTH Singapore passport AND US passport, flag if either needs a visa/e-visa/action
- Entry requirements (health declarations, onward ticket, insurance mandates)
- Visa application lead times — flag early if it takes weeks

👶 BABY
- What's medically happening this week in the pregnancy
- Upcoming milestones or appointments that need prep
- Things that should be booked/done by this gestational week but aren't
- Cross-domain: wedding is 3 months before due date — flag planning conflicts

💒 WEDDING
- Categories with no activity that book out fast (venues, photographers, caterers)
- Vendor follow-ups that have gone quiet
- Decisions that should be made by now given {wedding_days} days remaining

💰 FINANCE
- Bills or payments mentioned in FYIs that are approaching
- Outstanding amounts that haven't moved

🎟 SHOWS (Ansen)
- Show in next 7 days not yet in calendar → flag it
- Show logistics worth surfacing (transport, timing, etc.)

📋 TASKS
- Overdue tasks not cleared
- Tasks open 14+ days with no progress

🔗 CROSS-DOMAIN
- Conflicts between wedding timeline and baby timeline
- Anything where one domain affects another

RULES:
- Use search_web when you detect a travel destination, visa question, or anything needing real-time info — don't guess
- Lead with imminent events if any — give each one a named header: ⚡ <b>Tomorrow: [Event Name]</b>
- Be selective — max 5 bullets total. If nothing is genuinely worth flagging, say NOTHING
- Don't repeat what the morning brief already covers (today's due tasks)
- Sound like a sharp friend who notices things, not a notification bot
- FORMATTING: Telegram HTML only — <b>bold</b>, • bullets, emojis. Never use ** or _

CALENDAR IS SOURCE OF TRUTH:
- If a task date and a calendar event date differ — the calendar is correct, full stop. Do NOT flag this as a question or ask for confirmation. Simply note "Task updated to match calendar" if relevant, and move on. Never say "one of these is wrong" or ask which date is right.

TRIPS — only surface a trip if it's within 28 days OR has a specific open gap (visa not confirmed, accommodation missing, health clearance needed). Do not surface trips just because they exist on the calendar.

If nothing is worth flagging: respond with exactly: NOTHING"""

        # --- Run agentic loop (max 4 tool calls) ---
        messages: list[dict] = [{"role": "user", "content": "Run your proactive intelligence check now. Do not add an intro header or greeting — jump straight into the findings."}]
        for _ in range(4):
            try:
                response = await self.client.messages.create(
                    model=SYNTHESIS_MODEL,
                    max_tokens=800,
                    system=system,
                    tools=proactive_tools,
                    messages=messages,
                )
            except Exception:
                return None

            if response.stop_reason == "end_turn":
                result = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
                if not result or result.upper().startswith("NOTHING"):
                    return None
                fixed = _fix_md(result)
                try:
                    from tools.proactive_state import save_state as _save_proactive_state
                    _save_proactive_state(user_id, result, today_str)
                except Exception:
                    pass
                return fixed

            if response.stop_reason == "tool_use":
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for tu in tool_uses:
                    try:
                        res = await self._execute_tool(tu.name, tu.input, user_id, {})
                    except Exception as e:
                        res = {"error": str(e)}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": _json.dumps(res),
                    })
                messages.append({"role": "user", "content": tool_results})
            else:
                break

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
                model=CHAT_MODEL,
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
        try:
            from tools.fyis import get_fyis_for_context
            _fyis = get_fyis_for_context(limit=15)
            recent_fyis = "\n".join(
                f"[{f.get('category', 'misc')}] ({(f.get('created_at') or '')[:10]}) {f['content']}"
                for f in _fyis
            )
        except Exception:
            recent_fyis = ""
        try:
            from tools.baby_knowledge import get_entries as _get_baby
            _baby = _get_baby(limit=10)
            baby_context = "\n".join(
                f"[{', '.join(e.get('tags') or [])}] {e['summary']}"
                for e in _baby
            )
        except Exception:
            baby_context = ""
        result, payment = await asyncio.gather(
            self._run_loop(user_content, user_id, history, user_summary, shared_summary, recent_fyis, baby_context),
            self._wedding._extract_payment(image_bytes, caption),
        )
        if payment:
            try:
                add_payment(payment)
            except Exception:
                pass
        return result

    # Command methods — delegate to existing agents
    async def baby_knowledge_brief(self, query: str = "") -> str:
        """Synthesise baby knowledge base — grouped by topic or answering a specific question."""
        entries = search_baby_entries(query) if query else get_baby_entries(limit=50)
        if not entries:
            msg = "No baby knowledge saved yet." if not query else f"Nothing saved matching <b>{_html_escape(query)}</b>."
            return f"📚 {msg}\n\n<i>Send any tip, advice, or screenshot — I'll save it automatically.</i>"

        knowledge_text = "\n\n".join(
            f"[{i+1}] {e['summary']}" + (f"\nTags: {', '.join(e.get('tags') or [])}" if e.get('tags') else "")
            for i, e in enumerate(entries)
        )

        if query:
            prompt = f"""You are a pregnancy knowledge assistant. The couple has saved {len(entries)} pieces of knowledge.

SAVED KNOWLEDGE:
{knowledge_text}

QUESTION: {query}

Answer the question directly using what they've saved. Synthesise — don't just quote back the entries. If the saved knowledge doesn't fully answer the question, say so clearly.
Write in a warm, practical tone. Use Telegram HTML: <b>headers</b>, bullet points with blank lines between each. Emoji headers encouraged."""
        else:
            prompt = f"""You are a pregnancy knowledge assistant. The couple has saved {len(entries)} pieces of knowledge across various topics.

SAVED KNOWLEDGE:
{knowledge_text}

Synthesise this into a clear, organised summary. Group by topic (e.g. 🍎 Nutrition, 💊 Supplements, 🏥 Hospital & Birth, 😴 Sleep & Symptoms, 🤱 Feeding, 🧠 Mental Health, etc. — only include topics that have content).

For each topic, write 2-4 sentences of coherent advice drawn from the entries — not a raw list of what was saved. Make it feel like a personal knowledge base they built together.

Format: Telegram HTML only. <b>headers</b>. Blank line between every bullet. Emoji section headers."""

        resp = await self.client.messages.create(
            model=SYNTHESIS_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()

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
            model=SYNTHESIS_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        return _fix_md(resp.content[0].text)

    async def trip_card(self, trip: dict) -> str:
        """Render a trip's notes as structured Telegram HTML sections using Claude."""
        from html import escape as _esc
        dest = trip.get("destination", "Trip")
        status = trip.get("status") or "planning"
        start = trip.get("start_date") or ""
        end = trip.get("end_date") or ""
        notes = (trip.get("notes") or "").strip()
        visa_a = trip.get("visa_ansen") or ""
        visa_j = trip.get("visa_jess") or ""

        STATUS_ICON = {"planning": "🗓", "booked": "✅", "completed": "🏁", "cancelled": "❌"}
        icon = STATUS_ICON.get(status, "🗓")

        def _fmt(d: str) -> str:
            try:
                return datetime.strptime(d, "%Y-%m-%d").strftime("%-d %b %Y")
            except Exception:
                return d

        date_str = f"{_fmt(start)} – {_fmt(end)}" if start and end else (_fmt(start) if start else "Dates TBC")
        header = f"✈️ <b>{_esc(dest)}</b>  {icon} {status.title()}\n📅 {date_str}"

        sections: list[str] = []
        if notes:
            prompt = f"""Format these trip notes into structured Telegram HTML sections.

Trip: {dest}
Notes:
{notes}

Rules:
- Use ONLY sections that have actual data (no empty sections, no placeholders)
- Section header format: emoji <b>Label</b>
  🛫 <b>Flights</b> — routes, times, flight numbers, transit
  🏨 <b>Hotel</b> — name, check-in time, booking ref
  🚗 <b>Car Rental</b> — provider, pickup, return
  💰 <b>Budget</b> — costs, deposits, refundability
  📅 <b>Itinerary</b> — day plans, detours, timeline highlights
  📝 <b>Notes</b> — anything else worth keeping
- Each section: header on its own line, then • bullets (one fact per bullet)
- Telegram HTML only: <b>bold</b>, <i>italic</i>, • bullets — NO markdown, NO asterisks, NO pipes
- Blank line between sections
- Keep it tight — no filler text, just the facts"""

            response = await self.client.messages.create(
                model=CHAT_MODEL,
                max_tokens=700,
                messages=[{"role": "user", "content": prompt}],
            )
            body = _fix_md(response.content[0].text.strip())
            sections.append(body)

        if visa_a or visa_j:
            if "🛂" not in (sections[0] if sections else ""):
                visa_lines = ["🛂 <b>Visa</b>"]
                if visa_a:
                    visa_lines.append(f"• Ansen: {_esc(visa_a)}")
                if visa_j:
                    visa_lines.append(f"• Jess: {_esc(visa_j)}")
                sections.append("\n".join(visa_lines))

        if not sections:
            return header + "\n\n<i>No details captured yet — just mention anything about the trip and I'll save it.</i>"

        return header + "\n\n" + "\n\n".join(sections)

    async def trip_milestone_brief(self, trip: dict, days_until: int) -> str | None:
        """Pre-trip intelligence fired at milestone days (56/28/14/7/2 before departure)."""
        import json as _json
        from tools.fyis import get_fyis as _get_fyis

        dest = trip.get("destination", "")
        start = trip.get("start_date") or "TBC"
        end = trip.get("end_date") or "TBC"
        visa_ansen = trip.get("visa_ansen") or "not checked"
        visa_jess = trip.get("visa_jess") or "not checked"
        notes = trip.get("notes") or ""

        milestone_label = {56: "8 weeks out", 28: "4 weeks out", 14: "2 weeks out", 7: "1 week out", 2: "2 days out"}.get(days_until, f"{days_until} days out")

        # Pregnancy context
        baby_block = ""
        try:
            info = pregnancy_summary()
            baby_block = f"Week {info['week']}, due {info['due_date']}"
        except Exception:
            pass

        # FYIs mentioning this destination or travel
        fyi_lines = []
        try:
            for f in _get_fyis(limit=25):
                content = f.get("content", "")
                if dest.lower() in content.lower() or "travel" in (f.get("category") or "").lower():
                    fyi_lines.append(f"  [{(f.get('created_at') or '')[:10]}] {content}")
        except Exception:
            pass

        # Tasks mentioning this destination or travel category
        task_lines = []
        try:
            for uid in self._USER_NAMES:
                for t in get_tasks(uid, include_done=False):
                    task_text = (t.get("task") or "").lower()
                    if dest.lower() in task_text or t.get("category") == "travel":
                        task_lines.append(f"  • {t['task']} (due: {t.get('due_date') or 'no date'})")
        except Exception:
            pass

        shared = ""
        try:
            shared = get_shared_summary() or ""
        except Exception:
            pass

        # Categorise known state vs open gaps before building the prompt
        _confirmed: list[str] = []
        _open_gaps: list[str] = []

        # Visa
        _va_clean = (visa_ansen or "").lower().strip()
        _vj_clean = (visa_jess or "").lower().strip()
        if _va_clean and _va_clean not in ("not checked", ""):
            _confirmed.append(f"Ansen visa: {visa_ansen}")
        else:
            _open_gaps.append("Ansen visa (Singapore passport) — not yet checked")
        if _vj_clean and _vj_clean not in ("not checked", ""):
            _confirmed.append(f"Jess visa: {visa_jess}")
        else:
            _open_gaps.append("Jess visa (US passport) — not yet checked")

        # Accommodation — infer from FYIs / tasks / notes
        accom_keywords = {"hotel", "airbnb", "hostel", "accommodation", "stay", "room", "villa", "apartment"}
        _accom_mentioned = any(
            any(kw in (line or "").lower() for kw in accom_keywords)
            for line in fyi_lines + task_lines + [notes]
        )
        if _accom_mentioned:
            _confirmed.append("Accommodation mentioned in notes/FYIs")
        else:
            _open_gaps.append("Accommodation — nothing confirmed in notes or FYIs")

        # Insurance — flag if not mentioned and pregnancy involved
        _insur_mentioned = any("insur" in (line or "").lower() for line in fyi_lines + task_lines + [notes])
        if baby_block and not _insur_mentioned:
            _open_gaps.append("Travel insurance with maternity cover — not mentioned")

        # If all gaps are resolved and it's an early milestone, nothing to surface
        if not _open_gaps and days_until > 14:
            return None

        confirmed_block = "\n".join(f"  ✅ {c}" for c in _confirmed) if _confirmed else "  none yet"
        gaps_block = "\n".join(f"  ⚠️ {g}" for g in _open_gaps) if _open_gaps else "  none — all clear"
        need_visa_search = any("visa" in g.lower() for g in _open_gaps)

        system = f"""You are a proactive travel intelligence agent for Ansen and Jess.

TRIP: {dest}
DATES: {start} → {end}
MILESTONE: {milestone_label} until departure
{f"PREGNANCY: {baby_block}" if baby_block else ""}

WHAT'S ALREADY CONFIRMED (do NOT re-flag these):
{confirmed_block}

OPEN GAPS (these need attention — focus here):
{gaps_block}

SHARED BRAIN:
{shared or "(empty)"}

RELATED FYIs:
{chr(10).join(fyi_lines) if fyi_lines else "  none"}

RELATED TASKS:
{chr(10).join(task_lines) if task_lines else "  none"}

IDENTITIES:
- Ansen: Singaporean passport (visa-free for most countries)
- Jess: US passport (American — requirements often differ)
- Wedding: 7 November 2026  |  Baby due: 18 February 2027

YOUR JOB: Surface only the OPEN GAPS above. {"Use search_web to check current entry requirements for any open visa gaps — look for recent system changes (ETA schemes, biometric requirements, health declarations)." if need_visa_search else "Visa is already confirmed — skip the visa search."}

For each open gap: give a specific, actionable step (not "book accommodation" but "consider checking Booking.com — {dest} in peak season can fill fast").

RULES:
- Only flag genuine open gaps — not things already confirmed above
- Specific steps, not vague advice
- 3–5 bullets max. Telegram HTML only: <b>bold</b>, • bullets, emojis
- If no gaps remain after checking: respond with exactly NOTHING"""

        tools = [t for t in TOOLS if t["name"] == "search_web"]
        messages: list[dict] = [{"role": "user", "content": f"Run pre-trip intelligence for {dest} ({milestone_label})."}]
        first_uid = next(iter(self._USER_NAMES))

        for _ in range(3):
            try:
                response = await self.client.messages.create(
                    model=SYNTHESIS_MODEL,
                    max_tokens=700,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
            except Exception:
                return None

            if response.stop_reason == "end_turn":
                result = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
                if not result or result.upper().startswith("NOTHING"):
                    return None
                return f"✈️ <b>{dest} — {milestone_label}</b>\n\n" + _fix_md(result)

            if response.stop_reason == "tool_use":
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for tu in tool_uses:
                    try:
                        res = await self._execute_tool(tu.name, tu.input, first_uid, {})
                    except Exception as exc:
                        res = {"error": str(exc)}
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": _json.dumps(res)})
                messages.append({"role": "user", "content": tool_results})
            else:
                break
        return None

    async def appointment_pre_brief(self, medical_events: list[dict]) -> str | None:
        """Night-before synthesis for medical/health appointments — questions, what to bring, relevant knowledge."""
        from tools.baby_knowledge import get_entries as _get_baby, search_entries as _search_baby
        from tools.fyis import get_fyis as _get_fyis

        if not medical_events:
            return None

        # Questions saved to ask at appointments
        question_lines = []
        seen_q: set = set()
        for uid in self._USER_NAMES:
            try:
                for t in get_tasks(uid, include_done=False):
                    if t.get("category") == "baby_questions":
                        q = (t.get("task") or "").strip()
                        if q.upper().startswith("TASK:"):
                            q = q[5:].strip()
                        if q and q not in seen_q:
                            seen_q.add(q)
                            question_lines.append(f"  • {q}")
            except Exception:
                pass

        # Baby knowledge relevant to this appointment type
        search_query = " ".join(e.get("title", "") for e in medical_events)
        baby_entries: list[dict] = []
        try:
            baby_entries = _search_baby(search_query)[:6] if search_query else []
        except Exception:
            pass
        if not baby_entries:
            try:
                baby_entries = _get_baby(limit=6)
            except Exception:
                pass
        baby_knowledge_text = "\n".join(
            f"  [{', '.join(e.get('tags') or [])}] {e['summary']}" for e in baby_entries
        ) if baby_entries else "  none"

        # Recent health/baby FYIs
        fyi_lines = []
        try:
            health_kw = {"doctor", "scan", "blood", "results", "appointment", "test", "hospital", "clinic"}
            for f in _get_fyis(limit=20):
                cat = f.get("category") or ""
                content = (f.get("content") or "").lower()
                if cat in ("health", "baby") or any(kw in content for kw in health_kw):
                    fyi_lines.append(f"  [{(f.get('created_at') or '')[:10]}] {f['content']}")
        except Exception:
            pass

        # Pregnancy context
        baby_block = ""
        try:
            info = pregnancy_summary()
            milestones = upcoming_milestones(within_weeks=4)
            baby_block = f"Week {info['week']} of {info['total_weeks']}, due {info['due_date']}"
            if milestones:
                baby_block += " — upcoming: " + "; ".join(milestones[:2])
        except Exception:
            pass

        event_summaries = "\n".join(
            f"  • {e.get('title', 'Appointment')} — {e.get('start', 'tomorrow')}"
            for e in medical_events
        )

        prompt = f"""You are a practical medical prep assistant for Ansen and Jess (first-time parents, {baby_block}).

TOMORROW'S APPOINTMENT(S):
{event_summaries}

OPEN QUESTIONS TO ASK (saved by them):
{chr(10).join(question_lines) if question_lines else "  none saved yet"}

RELEVANT KNOWLEDGE BASE:
{baby_knowledge_text}

RELEVANT HEALTH FYIs:
{chr(10).join(fyi_lines) if fyi_lines else "  none"}

Write a concise tonight reminder for tomorrow's appointment. Cover:
1. <b>❓ Questions to ask</b> — pull from their saved list, prioritise by relevance to this appointment type. Add 1-2 smart ones they might have missed.
2. <b>📋 Bring</b> — ID, referral letters, test results they've mentioned, vitamins list if relevant, insurance card
3. <b>💡 Heads up</b> — one practical note (eat before if it's a long appointment, wear loose clothing for scans, etc.)

RULES:
- Warm and practical — like a helpful friend reminding them tonight
- Tight — 6–8 bullets max total across all sections
- Telegram HTML only: <b>bold headers</b>, • bullets, blank line between sections, no asterisks
- If there's genuinely nothing useful to surface (no questions, no relevant knowledge, basic appointment): respond NOTHING"""

        try:
            response = await self.client.messages.create(
                model=SYNTHESIS_MODEL,
                max_tokens=700,
                messages=[{"role": "user", "content": prompt}],
            )
            result = response.content[0].text.strip()
            if not result or result.upper().startswith("NOTHING"):
                return None
            return _fix_md(result)
        except Exception:
            return None

    async def goals_brief(self) -> tuple[str, list[dict]]:
        """All active goals with next unblocked steps. Returns (text, steps_for_buttons)."""
        from tools.goals import get_goals, get_next_steps
        from datetime import date as _date
        today_str = __local_today().isoformat()

        goals = get_goals(status="active")
        if not goals:
            return (
                "🎯 <b>Goals</b>\n\nNo active goals.\n\nTell me about a multi-step project — like \"break down booking the venue into steps\" — and I'll track it for you.",
                [],
            )

        lines = ["🎯 <b>Goals</b>\n"]
        button_steps: list[dict] = []

        for g in goals:
            all_steps = g.get("goal_steps", [])
            total = len(all_steps)
            done = sum(1 for s in all_steps if s["status"] == "done")
            next_steps = get_next_steps(g["id"])
            vis = "👥" if g["visibility"] == "shared" else "🔒"
            progress_bar = "▓" * done + "░" * (total - done)

            lines.append(f"{vis} <b>{g['title']}</b>")
            lines.append(f"  {progress_bar}  {done}/{total} done")

            if not next_steps:
                lines.append("  ✅ All steps complete — mark goal done")
            else:
                for s in next_steps[:3]:
                    due = s.get("due_date") or ""
                    try:
                        from datetime import date as _d
                        d = _d.fromisoformat(due)
                        if due < today_str:
                            due_str = f" 🔴 <i>({d.strftime('%-d %b')})</i>"
                        else:
                            due_str = f" <i>({d.strftime('%-d %b')})</i>"
                    except Exception:
                        due_str = ""
                    lines.append(f"  → {s['title']}{due_str}")
                    button_steps.append({"id": str(s["id"]), "label": s["title"][:38], "goal": g["title"]})

            lines.append("")

        return "\n".join(lines).rstrip(), button_steps

    async def capability_gap_sweep(self) -> str:
        """Self-skill discovery: identify capability gaps and research integrations that fill them.

        Phase 1 — Audit: Claude reviews its tool inventory and proposes gaps.
        Phase 2 — Research: parallel web searches per gap.
        Phase 3 — Synthesize: actionable proposals for Ansen.
        """
        current_tools = """CURRENT TOOLS:
- Google Calendar: read/create/delete events
- Gmail: search + read emails
- Supabase: tasks, reminders, FYIs, trips, shared brain, budget, groceries, shows
- Tavily web search: general queries
- OpenAI Whisper: voice message transcription
- Baby tracking: milestones, knowledge base, budget
- Stocks: newsletter digest + buy/hold/skip analysis
- Trip planning: destination, dates, visa, flights, notes, visibility
- Notifications: one-time + recurring (daily/weekly/monthly) via Telegram
- Grocery lists with inline done buttons"""

        user_context = """WHO THEY ARE:
- Ansen + Jess — Singapore-based couple, 20s–30s
- Getting married (planning wedding), expecting first baby (due Feb 2027)
- Frequent travelers: Tomorrowland Belgium (Jul 2025), Korea planned
- Ansen: entrepreneur, runs EDM/rave event app (Front Left), active in crypto/stocks
- Jess: WhatsApp Business account owner
- Primary interface: Telegram — they drop voice memos, screenshots, quick notes"""

        gap_prompt = f"""You are a self-aware AI assistant. Audit your own capabilities and identify where you're weakest relative to your users' needs.

{current_tools}

{user_context}

Think about what Ansen and Jess need daily and for their big life events: wedding, baby, travel, finances.

Identify the 5 most impactful capability gaps — things you genuinely cannot do today that would make you meaningfully more useful. Prioritise by frequency of need + pain of the missing capability.

For each gap, return a JSON object:
{{
  "gap": "one sentence: what you can't do",
  "example": "concrete situation where this would help",
  "search_query": "specific web search query to find the best API or integration"
}}

Return ONLY a JSON array of 5 objects. No other text."""

        try:
            resp = await self.client.messages.create(
                model=SYNTHESIS_MODEL,
                max_tokens=800,
                messages=[{"role": "user", "content": gap_prompt}],
            )
            import re as _re
            raw = resp.content[0].text.strip()
            match = _re.search(r'\[.*?\]', raw, _re.DOTALL)
            gaps: list[dict] = json.loads(match.group()) if match else []
        except Exception:
            return "⚠️ Capability gap audit failed — couldn't parse the response."

        if not gaps:
            return "⚠️ No capability gaps identified."

        # Phase 2: parallel web search per gap
        async def _research(gap: dict) -> dict:
            query = gap.get("search_query", gap.get("gap", ""))
            if not query:
                return {**gap, "research": []}
            try:
                results = await asyncio.to_thread(web_search, query, 3)
                return {**gap, "research": [r for r in results if "error" not in r]}
            except Exception:
                return {**gap, "research": []}

        researched = await asyncio.gather(*[_research(g) for g in gaps[:5]])

        # Phase 3: synthesize proposals
        research_block = ""
        for i, g in enumerate(researched, 1):
            research_block += f"\nGAP {i}: {g['gap']}\nExample: {g['example']}\n"
            for r in g.get("research", [])[:2]:
                title = r.get("title", "")
                content = (r.get("content") or "")[:200]
                if title or content:
                    research_block += f"  Found: {title} — {content}\n"

        synth_prompt = f"""You audited an AI assistant's capabilities and researched solutions for each gap. Now write a crisp proposal card for Ansen (the developer / power user).

{research_block}

Format as Telegram HTML:
- Header: 🔍 <b>Skill Gaps — what I could learn next</b>
- For each gap: one emoji, <b>what's missing</b>, then a • bullet with the best integration found + why it fits
- After all gaps: a short "🗳 Which should we build first?" line
- Use <b>, <i>, •, blank lines between sections. Never markdown asterisks.
- Be concrete — name actual APIs, not categories. Max 5 proposals."""

        try:
            s_resp = await self.client.messages.create(
                model=SYNTHESIS_MODEL,
                max_tokens=1200,
                messages=[{"role": "user", "content": synth_prompt}],
            )
            text = _fix_md(s_resp.content[0].text)
        except Exception:
            text = "⚠️ Synthesis step failed."

        # Return structured data so callers can attach build buttons
        return {"text": text, "gaps": [g for g in researched]}

    async def developer_build(self, request: str) -> str:
        """Generate implementation code for a new feature/integration.

        Returns Telegram-formatted message with code blocks, file edit instructions,
        and any env vars or DB changes needed.
        """
        bot_context = """You are a senior developer assistant for a Python Telegram bot called wedding-agent.

STACK:
- Python 3.11, python-telegram-bot 21.10 (async), Supabase PostgREST, Claude API (anthropic SDK)
- Deployed on Railway (auto-deploy on git push to main)
- Key files: main.py (handlers + jobs), agent.py (LLM methods), tools/*.py (data access)
- Formatting: Telegram HTML only (<b>, <i>, •, code), parse_mode=HTML

PATTERNS:
- New data tool → new file in tools/, import in agent.py
- New agent capability → new async method on UnifiedAgent class in agent.py
- New command → async cmd_X() in main.py, registered via app.add_handler(CommandHandler("x", cmd_x))
- New scheduled job → async send_X() in main.py, registered via app.job_queue.run_daily/run_monthly
- Supabase: get_client().table("x").select("*").execute().data — DDL via SQL Editor only

When asked to build something:
1. Identify the approach (new tool file / agent method / command / job)
2. Show complete, runnable code — no placeholders, no pseudocode
3. List exact file edits needed (file path, what to add and where)
4. List any new env vars required
5. Any Supabase SQL to run in the SQL Editor
6. Estimated complexity: S / M / L"""

        response = await self.client.messages.create(
            model=SYNTHESIS_MODEL,
            max_tokens=2500,
            system=bot_context,
            messages=[{"role": "user", "content": request}],
        )
        return response.content[0].text

    async def knowledge_sweep(self) -> dict:
        """Three-phase maker-checker knowledge sweep.

        Phase 1 — Extract: four parallel domain specialists propose candidate facts.
        Phase 2 — Verify: one checker gates each fact against the existing brain.
        Phase 3 — Write: only approved facts are written; report includes rejection stats.

        Returns {approved: {category: [facts]}, rejected_count: int}
        """
        import re as _re
        from tools.fyis import get_fyis as _get_fyis
        from tools.baby_knowledge import get_entries as _get_baby
        from tools.log import get_recent_drops
        from tools.daily import get_tasks

        # ── Gather raw data ──────────────────────────────────────────
        fyis = _get_fyis(limit=40)
        baby_entries = _get_baby(limit=30)
        wedding_drops = get_recent_drops(limit=20)

        all_user_ids = list(self._USER_NAMES.keys())
        completed_tasks = []
        for uid in all_user_ids:
            try:
                done = get_tasks(uid, include_done=True)
                completed_tasks += [t for t in done if t.get("done")][-10:]
            except Exception:
                pass

        existing_brain = get_shared_summary() or ""

        def _fmt(items, key_fn):
            return "\n".join(key_fn(i) for i in items) if items else "None this period."

        fyi_text     = _fmt(fyis, lambda f: f"[{f.get('category','misc')}] {f['content']}")
        baby_text    = _fmt(baby_entries, lambda e: f"[{', '.join(e.get('tags') or [])}] {e['summary']}")
        wedding_text = _fmt(wedding_drops, lambda d: f"[{d.get('category','wedding')}] {d['content'][:200]}")
        tasks_text   = _fmt(completed_tasks, lambda t: f"✓ {t.get('task','')[:100]}")

        context_block = f"""EXISTING SHARED BRAIN (do NOT re-propose anything already here):
{existing_brain or "(empty)"}

FYIs this period:
{fyi_text}

Baby knowledge this period:
{baby_text}

Wedding drops this period:
{wedding_text}

Completed tasks this period:
{tasks_text}"""

        DOMAINS = [
            ("baby",    "pregnancy, health, OBGYN appointments, baby gear, parenting decisions"),
            ("wedding", "venue, vendors, guest list, ceremony, logistics, confirmed bookings"),
            ("travel",  "trips, flights, hotels, visas, destinations, travel dates"),
            ("money",   "payments, investments, budgets, accounts, bills, financial decisions"),
            ("life",    "preferences, habits, cross-domain insights, anything that doesn't fit above"),
        ]

        EXTRACTOR_TMPL = """You are a specialist fact extractor for Ansen and Jess's shared brain. Your domain: {domain} ({scope}).

Your job: read the raw data below and propose facts worth permanently remembering — things that are NEW (not in the existing brain), CONFIRMED (not speculative), and MEANINGFUL (future conversations should know this).

One sentence per fact, max 120 chars. Output a JSON array of strings. If nothing new in your domain: output [].

{context}

Output only the JSON array."""

        # ── Phase 1: Parallel domain extractors ─────────────────────
        async def _extract(domain: str, scope: str) -> tuple[str, list[str]]:
            prompt = EXTRACTOR_TMPL.format(domain=domain, scope=scope, context=context_block)
            try:
                resp = await self.client.messages.create(
                    model=SYNTHESIS_MODEL,
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = resp.content[0].text.strip()
                match = _re.search(r'\[.*?\]', raw, _re.DOTALL)
                if not match:
                    return domain, []
                return domain, json.loads(match.group())
            except Exception:
                return domain, []

        extract_results = await asyncio.gather(*[_extract(d, s) for d, s in DOMAINS])

        candidates: dict[str, list[str]] = {}
        all_candidates: list[tuple[str, str]] = []  # (domain, fact)
        for domain, facts in extract_results:
            valid = [f for f in facts if isinstance(f, str) and f.strip()]
            if valid:
                candidates[domain] = valid
                for f in valid:
                    all_candidates.append((domain, f))

        if not all_candidates:
            return {"approved": {}, "rejected_count": 0}

        # ── Phase 2: Verifier (the gate) ─────────────────────────────
        candidate_lines = "\n".join(
            f"[{i}] ({dom}) {fact}" for i, (dom, fact) in enumerate(all_candidates)
        )

        verifier_prompt = f"""You are a strict fact verifier for Ansen and Jess's shared brain. Your job: gate each proposed fact before it enters permanent memory.

For each fact, output one of:
- NEW       — genuinely new, accurate, worth keeping
- DUPLICATE — already in the brain (same or equivalent information)
- STALE     — contradicted by newer confirmed info in the brain (e.g. 'awaiting response' when brain shows confirmed booking)
- WEAK      — too vague, too obvious, or not worth permanent storage

EXISTING SHARED BRAIN:
{existing_brain or "(empty)"}

PROPOSED FACTS:
{candidate_lines}

Output a JSON array with one object per fact, in order:
[
  {{"index": 0, "verdict": "NEW"}},
  {{"index": 1, "verdict": "DUPLICATE"}},
  ...
]

Be strict. When in doubt, reject."""

        approved_grouped: dict[str, list[str]] = {{}}
        rejected_count = 0

        try:
            v_resp = await self.client.messages.create(
                model=SYNTHESIS_MODEL,
                max_tokens=600,
                messages=[{"role": "user", "content": verifier_prompt}],
            )
            v_raw = v_resp.content[0].text.strip()
            match = _re.search(r'\[.*?\]', v_raw, _re.DOTALL)
            verdicts: list[dict] = json.loads(match.group()) if match else []

            for v in verdicts:
                idx = v.get("index")
                verdict = v.get("verdict", "WEAK")
                if idx is None or idx >= len(all_candidates):
                    continue
                domain, fact = all_candidates[idx]
                if verdict == "NEW":
                    approved_grouped.setdefault(domain, []).append(fact)
                else:
                    rejected_count += 1
        except Exception:
            # Verifier failed — fall back to writing all candidates
            for domain, fact in all_candidates:
                approved_grouped.setdefault(domain, []).append(fact)

        # ── Phase 3: Write approved facts ────────────────────────────
        for facts in approved_grouped.values():
            for fact in facts:
                try:
                    await self._upsert_shared(fact)
                except Exception:
                    pass

        return {"approved": approved_grouped, "rejected_count": rejected_count}

    async def brain_synthesis(self) -> str:
        """Synthesise shared brain + all budget buckets + recent FYIs into a unified knowledge base."""
        from tools.fyis import get_fyis as _get_fyis
        from tools.baby_budget import summary as _baby_budget
        shared = get_shared_summary() or ""
        fyis = _get_fyis(limit=50)

        fyi_text = "\n".join(
            f"[{f.get('category', 'misc')}] ({(f.get('created_at') or '')[:10]}) {f['content']}"
            for f in fyis
        ) if fyis else "None."

        # Budget snapshot across all three buckets
        try:
            baby_b = _baby_budget()
            baby_budget_lines = [f"Baby — Spent: SGD {baby_b['total_spent']:,.0f} | Planned: SGD {baby_b['total_planned']:,.0f}"]
            for cat, items in baby_b.get("by_category", {}).items():
                for i in items:
                    amt = f" SGD {i['amount']:,.0f}" if i.get("amount") else ""
                    baby_budget_lines.append(f"  • [{i.get('status','?')}] {i['item']}{amt}")
            baby_budget_text = "\n".join(baby_budget_lines)
        except Exception:
            baby_budget_text = "Baby budget: unavailable."

        try:
            shared_b = shared_budget_summary()
            shared_budget_lines = [f"Life — Owing: SGD {shared_b['total_owing']:,.0f} | Paid: SGD {shared_b['total_paid']:,.0f}"]
            for cat, items in shared_b.get("by_category", {}).items():
                for i in items:
                    amt = f" SGD {i['amount']:,.0f}" if i.get("amount") else ""
                    shared_budget_lines.append(f"  • [{i.get('status','?')}] {i['item']}{amt}")
            shared_budget_text = "\n".join(shared_budget_lines)
        except Exception:
            shared_budget_text = "Life budget: unavailable."

        if not shared.strip() and not fyis:
            return (
                "🧠 <b>Shared Brain</b>\n\n"
                "Nothing saved yet. Drop notes, FYIs, and decisions and I'll build this up over time."
            )

        prompt = f"""You are building a shared knowledge base for Ansen and Jess — a couple planning a wedding and expecting their first baby.

PERMANENT MEMORIES:
{shared or "Nothing yet."}

RECENT FYIs (last 30 days):
{fyi_text}

BABY BUDGET:
{baby_budget_text}

LIFE / SHARED BUDGET:
{shared_budget_text}

Synthesise everything into a clear, organised knowledge base. Group by theme — only include themes that have content:
🧑‍🤝‍🧑 About Us, 💒 Wedding, 👶 Baby, 🏠 Home & Life, ✈️ Travel & Plans, 💰 Money & Finance, 🍽️ Food & Preferences, 🐾 Pets, 💼 Work

For 💰 Money & Finance: give a holistic picture across wedding, baby, and life budgets — total outstanding, what's paid, what's coming. Make it feel like a family finance snapshot.

For each other theme, write 2-5 concise bullets of facts. Make it feel like a living document about this couple — not a list of raw messages.

Format: Telegram HTML only. <b>bold headers</b>. Bullets •. Blank line between sections. Emoji headers. No asterisks."""

        resp = await self.client.messages.create(
            model=SYNTHESIS_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()

    async def bring_me_up_to_speed(self) -> str:
        return await self._wedding.bring_me_up_to_speed()

    async def category_status(self, category: str) -> str:
        return await self._wedding.category_status(category)

    async def priority_brief(self) -> str:
        from datetime import date as _date, datetime as _dt
        wedding_brief = await self._wedding.priority_brief()

        # Prepend full-week calendar to the Sunday brief
        try:
            events = await asyncio.to_thread(get_events, 7)
        except Exception:
            events = []

        if not events:
            return wedding_brief

        today_str = __local_today().isoformat()
        ev_lines = []
        for e in events[:14]:
            start = e["start"]
            day_label = ""
            if "T" in start:
                try:
                    dt = _dt.fromisoformat(start)
                    day_label = dt.strftime("%a %-d %b, %-I:%M %p")
                except Exception:
                    day_label = start[:10]
            else:
                try:
                    dt = _dt.strptime(start, "%Y-%m-%d")
                    day_label = dt.strftime("%a %-d %b")
                except Exception:
                    day_label = start
            is_today = start.startswith(today_str)
            prefix = "📍 " if is_today else "• "
            ev_lines.append(f"{prefix}{day_label} — {e['title']}")

        cal_block = "📅 <b>This Week's Calendar</b>\n\n" + "\n".join(ev_lines)
        return cal_block + "\n\n---\n\n" + wedding_brief

    async def daily_brief(self, user_id: int) -> str:
        return await self._daily.daily_brief(user_id)

    async def morning_brief(self, user_id: int, user_name: str = "") -> str:
        """Unified personalized morning brief — narrative prose, not a bullet dump."""
        from tools.fyis import get_fyis_unacked as _get_fyis_unacked
        from datetime import date as _date, datetime as _dt
        today_str = __local_today().isoformat()
        weekday = __local_today().strftime("%A")
        _ANSEN_ID = 63756531

        parts = [f"TODAY: {weekday}, {today_str}", f"USER: {user_name}"]

        # Today's calendar
        try:
            events = await asyncio.to_thread(get_events, 7)
            today_events = [e for e in events if e["start"].startswith(today_str)]
        except Exception:
            events = []
            today_events = []

        if today_events:
            ev_lines = []
            for e in today_events:
                start = e["start"]
                if "T" in start:
                    try:
                        dt = _dt.fromisoformat(start)
                        start = dt.strftime("%-I:%M %p")
                    except Exception:
                        pass
                ev_lines.append(f"  {start} — {e['title']}")
            parts.append("TODAY'S CALENDAR:\n" + "\n".join(ev_lines))

        # Tasks: overdue + today + next 7 days
        try:
            all_tasks = get_tasks(user_id, include_done=False)
            visible = [t for t in all_tasks if not _is_junk_task(t) and not _is_calendar_covered(t, events)]
            overdue = [t for t in visible if t.get("due_date") and t["due_date"] < today_str]
            due_today = [t for t in visible if t.get("due_date") == today_str]
            upcoming = sorted([t for t in visible if t.get("due_date") and t["due_date"] > today_str], key=lambda x: x["due_date"])[:5]
            if overdue:
                parts.append("OVERDUE: " + "; ".join(t["task"] for t in overdue))
            if due_today:
                parts.append("DUE TODAY: " + "; ".join(t["task"] for t in due_today))
            if upcoming:
                parts.append("COMING UP: " + "; ".join(f"{t['task']} ({t['due_date']})" for t in upcoming))
        except Exception:
            pass

        # Unread FYIs — only recent ones (last 7 days), skip pending/awaiting items superseded by confirmed info
        try:
            from datetime import timedelta as _td
            cutoff = (__local_today() - _td(days=7)).isoformat()
            fyis = [f for f in _get_fyis_unacked(user_id, limit=20)
                    if (f.get("created_at") or "")[:10] >= cutoff]
            if fyis:
                fyi_lines = "\n".join(f"  [{f.get('category','misc')}] {f['content']}" for f in fyis[:8])
                parts.append("UNREAD FYIs (last 7 days only):\n" + fyi_lines)
        except Exception:
            pass

        # Baby
        try:
            baby = pregnancy_summary()
            milestones = upcoming_milestones(within_weeks=4)
            baby_line = f"Baby: Week {baby['week']}, due {baby['due_date']}"
            if milestones:
                baby_line += ". Upcoming: " + "; ".join(milestones[:2])
            parts.append(baby_line)
        except Exception:
            pass

        # Upcoming shows (Ansen only)
        if user_id == _ANSEN_ID:
            try:
                from tools.shows import get_shows_in_n_days as _shows_soon
                soon = _shows_soon(14)
                if soon:
                    parts.append("SHOWS SOON: " + "; ".join(f"{s['show_name']} ({s.get('show_date','TBC')})" for s in soon))
            except Exception:
                pass

        # Upcoming trips
        try:
            from tools.trips import get_upcoming_trips as _upcoming_trips
            trips = _upcoming_trips()
            if trips:
                parts.append("TRIPS: " + "; ".join(f"{t['destination']} ({t.get('start_date','TBC')})" for t in trips[:3]))
        except Exception:
            pass

        # Active goals — next unblocked step per goal
        try:
            from tools.goals import get_goals as _get_goals, get_next_steps as _get_next
            goals = _get_goals(status="active")
            if goals:
                goal_lines = []
                for g in goals:
                    all_steps = g.get("goal_steps", [])
                    done = sum(1 for s in all_steps if s["status"] == "done")
                    nxt = _get_next(g["id"])
                    if nxt:
                        goal_lines.append(f"  {g['title']} ({done}/{len(all_steps)}): next → {nxt[0]['title']}")
                    else:
                        goal_lines.append(f"  {g['title']}: all steps done, mark complete")
                if goal_lines:
                    parts.append("ACTIVE GOALS (next step per goal):\n" + "\n".join(goal_lines))
        except Exception:
            pass

        context = "\n\n".join(parts)

        prompt = f"""{context}

Write a morning update for {user_name}. You are a smart, proactive personal assistant who knows their full life context.

STRUCTURE: Use emoji section headers to break the brief into 3–5 scannable sections. Only include sections that have actual content. Suggested sections (adapt as needed):

📅 <b>Today</b> — what's on the calendar, what needs doing today
🍼 <b>Baby</b> — week milestone, upcoming appointment, anything relevant (only if there's something worth saying)
💍 <b>Wedding</b> — active wedding tasks or upcoming decisions (only if relevant)
✈️ <b>Trips</b> — upcoming travel, open gaps (only if relevant)
🎵 <b>Shows</b> — upcoming events (Ansen only, only if relevant)
🎯 <b>Goals</b> — next step on any active multi-step project (only if there's an active goal)
💰 <b>Money</b> — financial items, payments, DBS/investments (only if relevant)
⚠️ <b>Heads up</b> — overdue items, expiring things, urgent flags (only if there's something)

WHAT TO DO:
- Under each header, write 2–4 short sentences max. Natural, friendly tone — not a bullet dump.
- Look for connections: a FYI that relates to a task, a trip that links to a visa question. Weave them within the relevant section.
- Skip sections entirely if nothing meaningful to say — don't pad.
- Last line (outside sections): → /tasks /fyis for the full picture

STALENESS — before surfacing any FYI, cross-check it:
- If a FYI says "awaiting / enquiry sent / pending / looking into" AND the shared brain or calendar confirms that thing is now booked/confirmed → skip the stale FYI, use the confirmed version only
- Never surface both the pending and confirmed version of the same thing

FORMATTING: Pure HTML. Section headers as: <b>emoji Title</b> on its own line. <b>bold</b> key names/terms inline. No bullet points. No **asterisks**. This appears as a Telegram message on a phone — keep it readable at a glance."""

        response = await self.client.messages.create(
            model=SYNTHESIS_MODEL,
            max_tokens=600,
            system=DAILY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return _fix_md(response.content[0].text)

    async def combined_daily_brief(self, user_ids: list[int], user_names: dict[int, str] | None = None) -> tuple:
        return await self._daily.combined_daily_brief(user_ids, user_names)

    async def evening_brief(self, user_ids: list[int], user_names: dict[int, str] | None = None) -> str:
        return await self._daily.evening_brief(user_ids, user_names)

    async def personal_brief(self, user_id: int, user_name: str = "") -> tuple:
        """Private tasks grouped by daily category, urgency-sorted within each."""
        from datetime import date as _date, datetime as _dt
        today_str = __local_today().isoformat()
        try:
            all_tasks = get_tasks(user_id, include_done=False)
        except Exception:
            return "Couldn't load your tasks right now.", []

        try:
            cal_events = await asyncio.to_thread(get_events, 30)
        except Exception:
            cal_events = []

        mine = []
        seen: set = set()
        for t in all_tasks:
            tid = t.get("id")
            if tid in seen:
                continue
            if _is_junk_task(t):
                continue
            cat = t.get("category") or ""
            if cat in ("wedding", "baby"):
                continue
            assigned = t.get("assigned_to")
            is_mine = (assigned == user_id) or (not assigned and t.get("visibility") == "private" and t.get("user_id") == user_id)
            if is_mine and not _is_calendar_covered(t, cal_events):
                seen.add(tid)
                mine.append(t)

        name_str = f" — {user_name}" if user_name else ""
        if not mine:
            return f"<b>👤 My Tasks{name_str}</b>\n\nNothing personal on your list.", []

        # Category metadata — emoji + display name
        try:
            all_cats = get_all_categories()
        except Exception:
            all_cats = {}

        _CAT_META = {slug: (v.get("emoji", "📌"), v.get("name", slug.title())) for slug, v in all_cats.items()}
        _CAT_META.setdefault("personal", ("🙋", "Personal"))

        def _urgency_key(t: dict) -> tuple:
            due = t.get("due_date") or ""
            if not due:
                return (2, "")
            if due < today_str:
                return (0, due)  # overdue first
            return (1, due)  # future, sorted by date

        def _fmt(t: dict) -> str:
            raw = (t.get("task") or "").strip()
            if raw.upper().startswith("TASK:"):
                raw = raw[5:].strip()
            due = t.get("due_date") or ""
            if not due:
                return f"• {raw}"
            if due < today_str:
                try:
                    d = _dt.strptime(due, "%Y-%m-%d")
                    return f"• 🔴 {raw} <i>({d.strftime('%-d %b')})</i>"
                except Exception:
                    return f"• 🔴 {raw}"
            if due == today_str:
                return f"• {raw} <i>(today)</i>"
            try:
                d = _dt.strptime(due, "%Y-%m-%d")
                return f"• {raw} <i>({d.strftime('%-d %b')})</i>"
            except Exception:
                return f"• {raw}"

        # Group by category
        by_cat: dict[str, list] = {}
        for t in mine:
            cat = (t.get("category") or "personal").lower()
            by_cat.setdefault(cat, []).append(t)

        # Sort within each category by urgency
        for cat in by_cat:
            by_cat[cat].sort(key=_urgency_key)

        # Preferred category order; uncategorised buckets go last
        _ORDER = ["work", "finance", "health", "social", "travel", "home", "personal"]
        ordered_cats = [c for c in _ORDER if c in by_cat]
        ordered_cats += [c for c in sorted(by_cat) if c not in _ORDER and c in by_cat]

        blocks = [f"<b>👤 My Tasks{name_str}</b>"]
        ordered: list = []

        for cat in ordered_cats:
            tasks = by_cat[cat]
            emoji, cat_name = _CAT_META.get(cat, ("📌", cat.title()))
            header = f"{emoji} <b>{cat_name}</b>"
            lines = "\n\n".join(_fmt(t) for t in tasks)
            blocks.append(f"{header}\n\n{lines}")
            ordered += tasks

        return "\n\n".join(blocks), ordered

    async def baby_reminders_brief(self, user_ids: list[int], user_names: dict[int, str] | None = None) -> tuple:
        """All open tasks tagged category='baby', grouped by urgency."""
        from datetime import date as _date
        today_str = __local_today().isoformat()
        if user_names is None:
            user_names = {uid: str(uid) for uid in user_ids}
        seen: set = set()
        tasks: list[dict] = []
        for uid in user_ids:
            try:
                for t in get_tasks(uid, include_done=False):
                    tid = t.get("id")
                    if t.get("category") == "baby" and tid not in seen:
                        seen.add(tid)
                        tasks.append(t)
            except Exception:
                pass

        if not tasks:
            return "No baby reminders yet.\n\nAdd one by saying something like <i>\"remind us to book the viability scan\"</i>.", []

        def _fmt(t: dict) -> str:
            name = (t.get("task") or "").strip()
            if name.upper().startswith("TASK:"):
                name = name[5:].strip()
            assigned = t.get("assigned_to")
            suffix = f" → <i>{user_names.get(assigned, str(assigned))}</i>" if assigned else ""
            due = t.get("due_date")
            if not due:
                return f"• {name}{suffix}"
            try:
                d = _date.fromisoformat(due)
                due_label = f"{d.day} {d.strftime('%b')}"
            except ValueError:
                due_label = due
            if due < today_str:
                return f"🔴 {name}{suffix}"
            elif due == today_str:
                return f"📅 {name}{suffix}"
            else:
                return f"• {name} — {due_label}{suffix}"

        overdue  = sorted([t for t in tasks if t.get("due_date") and t["due_date"] < today_str],  key=lambda x: x["due_date"])
        today_t  = [t for t in tasks if t.get("due_date") and t["due_date"] == today_str]
        upcoming = sorted([t for t in tasks if t.get("due_date") and t["due_date"] > today_str],  key=lambda x: x["due_date"])
        someday  = [t for t in tasks if not t.get("due_date")]

        blocks = ["<b>👶 Baby Reminders</b>"]
        if overdue:
            blocks.append("\n🔴 <b>Overdue</b>")
            blocks += [_fmt(t) for t in overdue]
        if today_t:
            blocks.append("\n📅 <b>Today</b>")
            blocks += [_fmt(t) for t in today_t]
        if upcoming:
            blocks.append("\n📆 <b>Upcoming</b>")
            blocks += [_fmt(t) for t in upcoming]
        if someday:
            blocks.append("\n🗒 <b>No date</b>")
            blocks += [_fmt(t) for t in someday]

        return "\n".join(blocks), tasks

    async def baby_questions_brief(self, user_ids: list[int], user_names: dict[int, str] | None = None) -> tuple:
        """Questions to ask at appointments — tasks tagged category='baby_questions'."""
        if user_names is None:
            user_names = {uid: str(uid) for uid in user_ids}
        seen: set = set()
        tasks: list[dict] = []
        for uid in user_ids:
            try:
                for t in get_tasks(uid, include_done=False):
                    tid = t.get("id")
                    if t.get("category") == "baby_questions" and tid not in seen:
                        seen.add(tid)
                        tasks.append(t)
            except Exception:
                pass

        if not tasks:
            return "No questions saved yet.\n\nAdd one by saying <i>\"add to OB questions: ask about iron levels\"</i>", []

        added_by: dict = {}
        for t in tasks:
            uid = t.get("user_id")
            added_by.setdefault(uid, []).append(t)

        blocks = ["<b>❓ Questions for the Doctor</b>", "\n<i>Tap ✅ once asked.</i>"]
        for uid, qtasks in added_by.items():
            name = user_names.get(uid, "") if user_names else ""
            header = f"\n🙋 <b>{name}</b>" if name else "\n🙋 <b>Questions</b>"
            blocks.append(header)
            for t in qtasks:
                q = (t.get("task") or "").strip()
                if q.upper().startswith("TASK:"):
                    q = q[5:].strip()
                blocks.append(f"• {q}")

        return "\n".join(blocks), tasks

    async def baby_budget_brief(self) -> str:
        data = baby_budget_summary()
        items = data.get("items", [])
        if not items:
            return "💰 <b>Baby Budget</b>\n\nNothing logged yet.\n\nMention a price or purchase and I'll track it automatically."

        spent = data.get("total_spent", 0)
        planned = data.get("total_planned", 0)
        by_cat = data.get("by_category", {})

        STATUS_EMOJI = {"bought": "✅", "deposit": "💳", "planned": "🗒", "quoted": "💬"}
        CAT_EMOJI = {"gear": "🛒", "medical": "🏥", "clothing": "👕", "hospital": "🏨", "nutrition": "💊", "other": "📦"}

        lines = [
            "💰 <b>Baby Budget</b>\n",
            f"✅ Spent: <b>SGD {spent:,.0f}</b>",
            f"🗒 Planned: <b>SGD {planned:,.0f}</b>",
            f"📊 Total committed: <b>SGD {spent + planned:,.0f}</b>",
        ]

        for cat, cat_items in sorted(by_cat.items()):
            emoji = CAT_EMOJI.get(cat, "📦")
            lines.append(f"\n{emoji} <b>{cat.title()}</b>")
            for i in cat_items:
                status_icon = STATUS_EMOJI.get(i.get("status", "planned"), "•")
                amt = f" — SGD {i['amount']:,.0f}" if i.get("amount") else ""
                notes = f" <i>({i['notes']})</i>" if i.get("notes") else ""
                lines.append(f"{status_icon} {i['item']}{amt}{notes}")

        return "\n\n".join(lines) if len(lines) > 4 else "\n".join(lines)

    async def reminders_brief(self, user_ids: list[int], user_names: dict[int, str] | None = None) -> tuple:
        """Two-column view: each person's private tasks + a shared section.
        Returns (text, ordered_tasks) where ordered_tasks is the flat list in display order."""
        from datetime import date as _date
        today_str = __local_today().isoformat()
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
                # Shared with no specific assignee → shared section (exclude baby tasks — they live in /baby)
                elif t.get("visibility") == "shared" and t.get("category") not in ("baby", "baby_questions"):
                    if tid not in seen_shared:
                        seen_shared.add(tid)
                        shared.append(t)
                # Private → creator's section
                else:
                    owner = t.get("user_id")
                    if owner in personal and tid not in seen_personal:
                        seen_personal.add(tid)
                        personal[owner].append(t)

        _is_junk = _is_junk_task

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

        def _urgency_blocks(tasks: list[dict]) -> list[str]:
            """Render tasks grouped by urgency with emoji sub-headers."""
            overdue_t = _sort([t for t in tasks if t.get("due_date") and t["due_date"] < today_str])
            today_t   = _sort([t for t in tasks if t.get("due_date") and t["due_date"] == today_str])
            upcoming_t = _sort([t for t in tasks if t.get("due_date") and t["due_date"] > today_str])
            someday_t  = [t for t in tasks if not t.get("due_date")]
            out = []
            if overdue_t:
                out.append("🔴 <b>Overdue</b>")
                out += [_fmt(t) for t in overdue_t]
            if today_t:
                out.append("\n📅 <b>Today</b>")
                out += [_fmt(t) for t in today_t]
            if upcoming_t:
                out.append("\n📆 <b>Upcoming</b>")
                out += [_fmt(t) for t in upcoming_t]
            if someday_t:
                out.append("\n🗒 <b>No date</b>")
                out += [_fmt(t) for t in someday_t]
            return out

        blocks: list[str] = ["<b>📋 Reminders</b>"]
        ordered_tasks: list[dict] = []

        for uid in user_ids:
            person_name = user_names.get(uid, str(uid))
            tasks = _sort([t for t in personal.get(uid, []) if not _is_junk(t)])
            blocks.append(f"\n👤 <b>{person_name}</b>")
            if tasks:
                blocks += _urgency_blocks(tasks)
                ordered_tasks += tasks
            else:
                blocks.append("• Nothing on the list ✓")

        # Shared: dedup by text, filter junk, urgency sort
        seen_text: set = set()
        clean_shared = []
        for t in _sort(shared):
            if _is_junk(t):
                continue
            key = (t.get("task") or "").strip().lower()[:60]
            if key and key not in seen_text:
                seen_text.add(key)
                clean_shared.append(t)

        blocks.append("\n👥 <b>Shared</b>")
        if clean_shared:
            blocks += _urgency_blocks(clean_shared)
            ordered_tasks += clean_shared
        else:
            blocks.append("• Nothing shared ✓")

        return "\n".join(blocks), ordered_tasks
