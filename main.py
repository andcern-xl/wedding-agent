import asyncio
import io
import logging
import os
from html import escape
from datetime import time as dtime, date as ddate
from zoneinfo import ZoneInfo
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from dotenv import load_dotenv
from agent import UnifiedAgent
from categories import CATEGORIES
from tools.notifications import (
    get_pending_notifications, mark_notification_sent, list_notifications as list_scheduled,
    cancel_notification as cancel_scheduled, get_notification as get_scheduled,
    stop_series as stop_notification_series, local_time_label, group_duplicates,
)
from tools.user_memory import get_shared_summary, append_shared_summary
from tools.fyis import get_fyis, get_fyis_expiring, keep_fyi, promote_fyi, archive_fyi, ack_fyi, get_fyis_unacked
from tools.conversation import load_history, save_history
from tools.daily import complete_task, set_task_category, task_domain, add_task
from tools.check_ins import (
    get_check_in, answer_check_in, dismiss_check_in, snooze_check_in,
    reopen_due_snoozed, expire_stale,
)
from tools.shows import get_upcoming_shows, get_shows_in_n_days, get_show_by_id, mark_calendar_added as mark_show_calendar_added, delete_show as _delete_show_by_id

ANSEN_ID = 63756531
JESS_ID = 6927468999

# Per-person nightly Reddit nuggets — his = dad's-eye view, hers = the pregnant
# person's own experience.
_NUGGET_FEEDS = [
    {
        "user_id": ANSEN_ID, "state_key": "daddit_nuggets", "subreddits": ["daddit"],
        "angle": "This goes to Ansen — address him directly as 'you', never in third person. 1-3 nuggets from r/daddit, learning from dads who've been there, to support Jess through pregnancy and prep for the baby. Pick takeaways for him: partner empathy, what actually helps a pregnant partner, newborn prep, dad mindset.",
    },
    {
        "user_id": JESS_ID, "state_key": "babybumps_nuggets", "subreddits": ["BabyBumps", "pregnant"],
        "angle": "This goes to Jess — address her directly as 'you', never in third person (don't call her 'Jess', say 'you'). 1-3 nuggets from r/BabyBumps and r/pregnant, from other pregnant women living it right now. Pick takeaways for her: what to expect this stage, symptom reality, self-advocacy at appointments, things she'd want to know from someone a few weeks ahead. Warm and reassuring, never alarming.",
    },
]

load_dotenv()


async def _transcribe_voice(file_bytes: bytes) -> str:
    """Transcribe a Telegram voice message (ogg/opus) using OpenAI Whisper."""
    import openai
    client = openai.AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    audio_file = io.BytesIO(file_bytes)
    audio_file.name = "voice.ogg"
    result = await client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file,
        language="en",
    )
    return result.text.strip()

try:
    REMINDER_TIMEZONE = ZoneInfo(os.getenv("REMINDER_TZ", "Asia/Singapore"))
except Exception:
    REMINDER_TIMEZONE = ZoneInfo("UTC")
REMINDER_TIME   = dtime(hour=9,  minute=0, tzinfo=REMINDER_TIMEZONE)   # 9am  — tasks, FYIs, baby, shows
_evening_hour   = int(os.getenv("EVENING_BRIEF_HOUR", "21"))
EVENING_TIME    = dtime(hour=_evening_hour, minute=0, tzinfo=REMINDER_TIMEZONE)  # 9pm — recap, knowledge sweep
CRYPTO_TIME     = dtime(hour=20, minute=0, tzinfo=REMINDER_TIMEZONE)               # 8pm — stocks & crypto
BABY_WEEKLY_TIME   = dtime(hour=9, minute=0, tzinfo=REMINDER_TIMEZONE)
JESS_CHECKIN_TIME  = dtime(hour=10, minute=0, tzinfo=REMINDER_TIMEZONE)  # 10am — Jess's daily pregnancy companion
APPOINTMENT_TIME   = dtime(hour=21, minute=0, tzinfo=REMINDER_TIMEZONE)  # 9pm — appointment pre-brief for tomorrow
CAL_SYNC_TIME      = dtime(hour=8, minute=50, tzinfo=REMINDER_TIMEZONE)  # 8:50am — calendar reconciliation before morning brief
SELF_AUDIT_TIME    = dtime(hour=8, minute=20, tzinfo=REMINDER_TIMEZONE)  # 8:20am Mon — memory self-audit, before any brief is built on it

# Medical/appointment keywords for event title detection
APPOINTMENT_KEYWORDS = {
    "appointment", "scan", "obgyn", "ob-gyn", "ob/gyn", "doctor", "clinic",
    "hospital", "midwife", "checkup", "check-up", "blood test", "ultrasound",
    "consult", "consultation", "viability", "dating scan", "nuchal", "anatomy",
    "glucose", "gtt", "nst", "prenatal", "antenatal", "gp", "specialist",
    "physio", "dentist", "dr ", "dr.",
}

# Days before trip departure that trigger a pre-trip milestone brief
TRIP_MILESTONES = {56, 28, 14, 7, 2}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ALLOWED_IDS = [int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
agent = UnifiedAgent()
conversations: dict[int, list] = {}   # in-memory cache; backed by Supabase
chat_locks: dict[int, asyncio.Lock] = {}


def allowed(update: Update) -> bool:
    return not ALLOWED_IDS or update.effective_user.id in ALLOWED_IDS


async def notify_partner(context: ContextTypes.DEFAULT_TYPE, update: Update, text: str = None, photo_bytes: bytes = None, caption: str = None, analysis: str = None, is_fyi: bool = False):
    sender_id = update.effective_user.id
    sender_name = update.effective_user.first_name or "Partner"
    partner_ids = [uid for uid in ALLOWED_IDS if uid != sender_id]
    for uid in partner_ids:
        try:
            if photo_bytes:
                await context.bot.send_photo(
                    chat_id=uid,
                    photo=io.BytesIO(photo_bytes),
                    caption=f"📨 {sender_name}: {caption}" if caption else f"📨 {sender_name} sent a photo",
                )
                if analysis:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=f"<i>({sender_name}'s drop)</i> {analysis}",
                        parse_mode="HTML",
                    )
            elif text:
                msg_text = f"📨 <b>{sender_name}:</b> {escape(text)}\n\n<i>{analysis}</i>" if analysis else f"📨 <b>{sender_name}:</b> {escape(text)}"
                keyboard = None
                if is_fyi:
                    # The episode is already in the brain — the only button that
                    # makes sense is a social ack. "Save to my FYIs" was a fake:
                    # it wrote to the retired fyis table nothing reads.
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Got it", callback_data=f"fyi_ack:{sender_id}:{uid}"),
                    ]])
                await context.bot.send_message(
                    chat_id=uid,
                    text=msg_text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
        except Exception as e:
            logger.error(f"notify_partner failed for uid {uid}: {e}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    lines = [
        "👋 Two brains, one bot.\n",
        "/wedding — planning & categories",
        "/shared — brain, tasks, reminders",
        "/baby — pregnancy updates, milestones, knowledge base",
        "/stocks — newsletter digest + buy/hold/skip",
        "/finances — portfolio & money picture",
        "/me — your personal tasks (includes shows)\n",
        "Or just talk — drop a note, screenshot, or question.",
    ]
    await update.message.reply_text("\n".join(lines))


def _wedding_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Catch Up", callback_data="wedding_bringmeuptospeed"),
         InlineKeyboardButton("📅 Plan", callback_data="wedding_plan")],
        [InlineKeyboardButton("📂 Categories", callback_data="wedding_categories")],
    ])


def _shared_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Brain", callback_data="shared_brain"),
         InlineKeyboardButton("✅ Tasks", callback_data="shared_tasks")],
        [InlineKeyboardButton("⏰ Reminders", callback_data="shared_reminders")],
        [InlineKeyboardButton("💰 Budget", callback_data="shared_budget"),
         InlineKeyboardButton("✈️ Travel", callback_data="shared_travel")],
        [InlineKeyboardButton("🛒 Groceries", callback_data="shared_groceries"),
         InlineKeyboardButton("💼 Finances", callback_data="shared_finances")],
    ])


def _render_finances() -> str:
    """Portfolio + this month's Split spend — the couple's money picture."""
    from tools.holdings import summary as holdings_summary

    OWNER_LABEL = {"ansen": "Ansen", "jess": "Jess", "joint": "Joint"}
    TYPE_EMOJI = {"crypto": "🪙", "stock": "📈", "etf": "📊", "fund": "🏦", "cash": "💵", "other": "📦"}

    s = holdings_summary()
    lines = ["💼 <b>Finances</b>\n"]

    if not s["items"]:
        lines.append("No holdings tracked yet. Just tell me what you own — \"we have $17k in StashAway\", \"bought 0.2 ETH\" — and I'll build the picture.")
    else:
        by_owner: dict = {}
        for h in s["items"]:
            by_owner.setdefault(h.get("owner") or "joint", []).append(h)
        for owner in ("joint", "ansen", "jess"):
            items = by_owner.get(owner)
            if not items:
                continue
            lines.append(f"<b>{OWNER_LABEL[owner]}</b>")
            for h in items:
                emoji = TYPE_EMOJI.get(h.get("asset_type"), "📦")
                if h.get("units") is not None:
                    pos = f"{h['units']:g} units"
                    if h.get("avg_cost") is not None:
                        pos += f" @ {h['avg_cost']:g}"
                else:
                    pos = f"{h.get('currency','SGD')} {float(h.get('value') or 0):,.0f}"
                plat = f" · {h['platform']}" if h.get("platform") else ""
                lines.append(f"{emoji} {h['asset']} — <b>{pos}</b>{plat}  <i>(as of {h.get('as_of')})</i>")
            lines.append("")
        if s["totals_by_currency"]:
            totals = "  •  ".join(f"<b>{cur} {amt:,.0f}</b>" for cur, amt in s["totals_by_currency"].items())
            lines.append(f"📊 Tracked value: {totals}")
        if s["stale"]:
            stale_names = ", ".join(f"{h['asset']} ({h['days_stale']}d)" for h in s["stale"][:4])
            lines.append(f"\n⚠️ Stale values: {stale_names} — tell me the current numbers and I'll update them.")

    try:
        from tools.split_expenses import get_expenses
        spend = get_expenses(days=30)
        if spend["expenses"]:
            top = sorted(spend["totals_by_category"].items(), key=lambda kv: -kv[1])[:4]
            top_str = ", ".join(f"{cat} {amt:,.0f}" for cat, amt in top)
            lines.append(f"\n🧾 <b>Split — last 30 days</b>: {spend['currency']} {spend['total']:,.0f} ({top_str})")
    except Exception:
        pass

    return "\n".join(lines)


def _category_menu() -> InlineKeyboardMarkup:
    cats = list(CATEGORIES.items())
    rows = []
    for i in range(0, len(cats), 3):
        row = [
            InlineKeyboardButton(
                f"{v['emoji']} {v['name'].split(' &')[0].split('—')[0].strip()}",
                callback_data=f"wedding_cat_{k}",
            )
            for k, v in cats[i:i+3]
        ]
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _baby_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Weekly Brief", callback_data="baby_brief"),
         InlineKeyboardButton("📚 Knowledge", callback_data="baby_knowledge")],
        [InlineKeyboardButton("📅 Milestones", callback_data="baby_milestones"),
         InlineKeyboardButton("✅ Reminders", callback_data="baby_reminders")],
        [InlineKeyboardButton("❓ Questions", callback_data="baby_questions"),
         InlineKeyboardButton("💰 Budget", callback_data="baby_budget")],
    ])


async def cmd_wedding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text(
        "💒 <b>Wedding</b>",
        parse_mode="HTML",
        reply_markup=_wedding_menu(),
    )


async def cmd_shared_parent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text(
        "🧠 <b>Shared</b>",
        parse_mode="HTML",
        reply_markup=_shared_menu(),
    )


_JUNK_PREFIXES = ("fyi", "ansen deposited", "jess deposited", "ansen paid", "jess paid", "approved ")

def _is_task(t: dict) -> bool:
    raw = (t.get("task") or "").strip().lower()
    if raw.startswith("• ") or raw.startswith("- "):
        raw = raw[2:]
    if len(raw) > 300:
        return False
    return not any(raw.startswith(p) for p in _JUNK_PREFIXES)

def _can_complete(t: dict, user_id: int) -> bool:
    """Only show a Done button for tasks this user is responsible for."""
    assigned = t.get("assigned_to")
    if assigned:
        return assigned == user_id  # assigned task — only the assignee
    if t.get("visibility") == "shared":
        return True  # shared with no specific assignee — anyone can do it
    return t.get("user_id") == user_id  # private — only the creator

_DOMAIN_EMOJI = {"baby": "👶", "baby_questions": "👶", "wedding": "💍", "life": "✅"}


def _reminders_keyboard(tasks: list[dict], user_id: int) -> InlineKeyboardMarkup | None:
    mine = [t for t in tasks if _is_task(t) and _can_complete(t, user_id)]
    # Cluster by domain (baby → wedding → life) so the button strip reads in
    # groups, then urgency within each (dated before undated)
    _DOMAIN_ORDER = {"baby": 0, "baby_questions": 0, "wedding": 1}
    mine.sort(key=lambda t: (
        _DOMAIN_ORDER.get(task_domain(t) or "life", 2),
        t.get("due_date") or "9999-12-31",
    ))
    rows = []
    for t in mine[:12]:
        raw = (t.get("task") or "").strip()
        if raw.upper().startswith("TASK:"):
            raw = raw[5:].strip()
        label = raw[:35] + "…" if len(raw) > 35 else raw
        emoji = _DOMAIN_EMOJI.get(task_domain(t) or "life", "✅")
        rows.append([InlineKeyboardButton(f"{emoji} {label}", callback_data=f"done:{t['id']}")])
    return InlineKeyboardMarkup(rows) if rows else None


_CHECKIN_EMOJI = {"baby": "👶", "wedding": "💍", "life": "🏠"}


def _check_in_card(ci: dict) -> tuple[str, InlineKeyboardMarkup]:
    emoji = _CHECKIN_EMOJI.get(ci.get("category", "life"), "🏠")
    lines = [f"{emoji} <b>Quick decision</b>", escape(ci["question"])]
    if ci.get("context"):
        lines.append(f"<i>{escape(ci['context'])}</i>")
    rows = [
        [InlineKeyboardButton(opt["label"], callback_data=f"ci:{ci['id']}:{i}")]
        for i, opt in enumerate(ci.get("options") or [])
    ]
    rows.append([InlineKeyboardButton("💤 Later", callback_data=f"cisnz:{ci['id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _send_check_in_cards(context, check_ins: list[dict], asker_id: int):
    """Deliver check-in cards — to the asker, or to both partners when audience='both'."""
    for ci in check_ins[:3]:
        try:
            text, keyboard = _check_in_card(ci)
            targets = ALLOWED_IDS if ci.get("audience") == "both" else [asker_id]
            for uid in targets:
                try:
                    await context.bot.send_message(chat_id=uid, text=text, parse_mode="HTML", reply_markup=keyboard)
                except Exception:
                    logger.exception(f"check-in card delivery failed for {uid}")
        except Exception:
            logger.exception("check-in card build failed")


async def _send_category_asks(context, asks: list[dict], chat_id: int):
    """The agent couldn't classify a task — ask the creator to tap a bucket."""
    for ask in asks[:3]:
        try:
            name = (ask.get("task") or "")[:80]
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("👶 Baby", callback_data=f"taskcat:{ask['id']}:baby"),
                InlineKeyboardButton("💍 Wedding", callback_data=f"taskcat:{ask['id']}:wedding"),
                InlineKeyboardButton("🏠 Life", callback_data=f"taskcat:{ask['id']}:life"),
            ]])
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🗂 Which bucket for: <i>{escape(name)}</i>?",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("category ask delivery failed")


async def _fetch_user_names(context) -> dict[int, str]:
    names = {}
    for uid in ALLOWED_IDS:
        try:
            chat = await context.bot.get_chat(uid)
            names[uid] = chat.first_name or str(uid)
        except Exception:
            names[uid] = str(uid)
    return names


def _split_sections(text: str, limit: int = 4096) -> list[str]:
    sections = [s.strip() for s in text.split("\n---\n") if s.strip()]
    if len(sections) <= 1:
        sections = [s.strip() for s in text.split("---") if s.strip()]
    result = []
    for section in sections:
        while len(section) > limit:
            split_at = section.rfind("\n\n", 0, limit)
            if split_at == -1:
                split_at = section.rfind("\n", 0, limit)
            if split_at == -1:
                split_at = limit
            result.append(section[:split_at].strip())
            section = section[split_at:].strip()
        if section:
            result.append(section)
    return result or [text]


async def cmd_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    lines = [
        "<b>Commands</b>\n",
        "💒 /wedding — catch up, plan, tasks, reminders, shared, categories",
        "👶 /baby — weekly brief, knowledge base, milestones",
        "📊 /stocks — newsletter digest + buy/hold/skip\n",
        "💼 /finances — portfolio & money picture\n",
        "<b>Shortcuts</b>",
        "/bringmeuptospeed — full wedding overview",
        "/plan /tasks /reminders /shared",
        "🔔 /notifications — timed reminders, with a ❌ on each to switch it off",
    ]
    for key, cat in CATEGORIES.items():
        lines.append(f"{cat['emoji']} /{key}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_bringmeuptospeed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("Pulling everything together...")
    try:
        summary = await agent.bring_me_up_to_speed()
        sections = _split_sections(summary)
        await msg.edit_text(sections[0], parse_mode="HTML")
        for section in sections[1:]:
            await update.message.reply_text(section, parse_mode="HTML")
    except Exception as e:
        logger.exception("cmd_bringmeuptospeed failed")
        await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def cmd_category_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    command = update.message.text[1:].split()[0].lower()
    if command not in CATEGORIES:
        return
    msg = await update.message.reply_text("Checking...")
    try:
        status = await agent.category_status(command)
        sections = _split_sections(status)
        await msg.edit_text(sections[0], parse_mode="HTML")
        for section in sections[1:]:
            await update.message.reply_text(section, parse_mode="HTML")
    except Exception as e:
        logger.exception("cmd_category_status failed")
        await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if chat_id not in chat_locks:
        chat_locks[chat_id] = asyncio.Lock()

    async with chat_locks[chat_id]:
        await _process_message(update, context, user_id, chat_id)


def _get_history(chat_id: int) -> list:
    """Conversation history for a chat, hydrating from Supabase on first touch."""
    if chat_id not in conversations:
        conversations[chat_id] = load_history(chat_id)
    return conversations[chat_id]


def _thread_into_history(chat_id: int, user_turn: str, assistant_turn: str) -> None:
    """Fold an out-of-band exchange (a tapped check-in, a seeded prompt) into the
    persisted conversation so the NEXT message continues it instead of starting
    cold. This is what makes button interactions conversational, not just stored."""
    history = _get_history(chat_id)
    history = history + [
        {"role": "user", "content": user_turn},
        {"role": "assistant", "content": assistant_turn},
    ]
    conversations[chat_id] = history[-40:]
    asyncio.create_task(asyncio.to_thread(save_history, chat_id, conversations[chat_id]))


async def _process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int):
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    # Load from Supabase if this is the first message after a restart
    history = _get_history(chat_id)

    # effective_message covers BOTH new and EDITED messages. Without this, an
    # edit (Jess bumping "Log this" to "…add it to shared brain") arrives as
    # update.edited_message, update.message is None, and the instruction is lost.
    msg = update.effective_message
    if msg is None:
        return
    is_edit = update.edited_message is not None
    # A media edit (caption change) would re-download and re-file the same
    # screenshot — only re-process TEXT edits (the "…add to shared brain" case).
    if is_edit and (msg.photo or msg.document or msg.voice):
        return

    try:
        if msg.photo:
            photo = msg.photo[-1]
            photo_file = await photo.get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            caption = msg.caption or ""

            result = await agent.handle_image(image_bytes=bytes(photo_bytes), caption=caption, user_id=user_id, history=history)
            if result.get("notify_partner"):
                await notify_partner(context, update, photo_bytes=bytes(photo_bytes), caption=caption, analysis=result.get("text"))

        elif msg.document:
            doc = msg.document
            mime = (doc.mime_type or "").lower()
            caption = msg.caption or ""
            supported = mime == "application/pdf" or mime in ("image/jpeg", "image/png", "image/gif", "image/webp")
            if not supported:
                await msg.reply_text(
                    "I can read PDFs and images sent as files — this file type I can't open yet."
                )
                return
            if doc.file_size and doc.file_size > 15 * 1024 * 1024:
                await msg.reply_text("That file's too big for me — 15 MB max.")
                return
            doc_file = await doc.get_file()
            doc_bytes = await doc_file.download_as_bytearray()

            result = await agent.handle_image(
                image_bytes=bytes(doc_bytes), caption=caption, user_id=user_id,
                history=history, media_type=mime,
            )
            if result.get("notify_partner"):
                await notify_partner(context, update, text=caption or f"sent a file: {doc.file_name}", analysis=result.get("text"), is_fyi=result.get("fyi", False))

        elif msg.voice:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            voice_file = await msg.voice.get_file()
            voice_bytes = await voice_file.download_as_bytearray()
            try:
                transcript = await _transcribe_voice(bytes(voice_bytes))
            except Exception as e:
                await msg.reply_text(f"Couldn't transcribe that — {e}")
                return
            # Echo transcript so user can see what was heard
            await msg.reply_text(f"🎙 <i>{escape(transcript)}</i>", parse_mode="HTML")
            # Process transcript exactly like a text message
            result = await agent.handle_message(text=transcript, user_id=user_id, history=history)
            if result.get("notify_partner"):
                await notify_partner(context, update, text=transcript, analysis=result.get("text"), is_fyi=result.get("fyi", False))

        else:
            text = msg.text or ""
            if text.startswith("/"):
                return
            # An edit re-runs the agent with the new full text — tell it so it
            # acts on what changed instead of re-greeting.
            if is_edit and text.strip():
                text = f"[They edited their earlier message to this — act on it]\n{text}"

            # Pending value-capture: they tapped "Confirmed — log conf#" and we
            # asked for the number. Catch their reply here before the agent runs.
            if await _try_capture_reply(update, context, user_id, text):
                return

            # Prepend reply context so the agent knows what the user is referring to
            reply = msg.reply_to_message
            if reply:
                replied_text = reply.text or reply.caption or ""
                if replied_text and replied_text.strip():
                    text = f'[Replying to: "{replied_text.strip()}"]\n{text}'

            result = await agent.handle_message(text=text, user_id=user_id, history=history)

            if result.get("notify_partner"):
                await notify_partner(
                    context, update,
                    text=text,
                    analysis=result.get("text"),
                    is_fyi=result.get("fyi", False),
                )

        updated_history = result.get("history", history)
        conversations[chat_id] = updated_history
        # Persist to Supabase so history survives restarts/deploys
        asyncio.create_task(asyncio.to_thread(save_history, chat_id, updated_history))
        await msg.reply_text(result["text"], parse_mode="HTML")

        # Decision cards + category picks from this turn's tool calls
        await _send_check_in_cards(context, result.get("check_ins", []), user_id)
        await _send_category_asks(context, result.get("category_asks", []), chat_id)

        # Direct partner messages from message_partner tool — send immediately
        for partner_id, partner_msg in result.get("partner_messages", []):
            try:
                sender_name = update.effective_user.first_name or "Your partner"
                await context.bot.send_message(
                    chat_id=partner_id,
                    text=f"📨 <b>{escape(sender_name)}:</b> {escape(partner_msg)}",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"partner message delivery failed for {partner_id}: {e}")

        # Notify partner when tasks are marked done by the agent
        for task_name in result.get("completed_tasks", []):
            sender = update.effective_user.first_name or "Your partner"
            for uid in ALLOWED_IDS:
                if uid != user_id:
                    try:
                        await context.bot.send_message(
                            chat_id=uid,
                            text=f"✅ {sender} checked off: <i>{task_name}</i>",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

        # Notify partner on grocery add/remove
        grocery = result.get("grocery_update")
        if grocery:
            sender = update.effective_user.first_name or "Your partner"
            action = grocery.get("action", "updated")
            list_name = grocery.get("list_name", "Groceries")
            items = grocery.get("items", [])
            if items:
                if action == "add":
                    icon = "🛒"
                    verb = "added to"
                else:
                    icon = "🗑"
                    verb = "removed from"
                item_lines = "\n".join(f"  • {it}" for it in items)
                notif_text = f"{icon} <b>{sender}</b> {verb} <b>{list_name}</b>\n\n{item_lines}"
                for uid in ALLOWED_IDS:
                    if uid != user_id:
                        try:
                            await context.bot.send_message(chat_id=uid, text=notif_text, parse_mode="HTML")
                        except Exception:
                            pass

    except Exception as e:
        logger.exception(f"Error handling message: {e}")
        err_type = type(e).__name__
        err_msg = str(e)[:300]
        await (update.effective_message or update.message).reply_text(f"[DEBUG] {err_type}: {err_msg}")


async def _send_or_alert(context, chat_id: int, text: str, job_name: str, **kwargs):
    """Scheduled-send with delivery-failure detection: HTML send → plain-text
    retry → DM Ansen the error. A generated-but-undelivered brief is invisible
    otherwise (the job 'ran', the user got nothing)."""
    import re as _re
    kwargs.setdefault("parse_mode", "HTML")
    try:
        return await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as first_err:
        plain = _re.sub(r"<[^>]+>", "", text)
        kwargs.pop("parse_mode", None)
        try:
            return await context.bot.send_message(chat_id=chat_id, text=plain[:4000], **kwargs)
        except Exception:
            logger.exception(f"{job_name}: delivery failed for chat {chat_id}")
            try:
                await context.bot.send_message(
                    chat_id=ANSEN_ID,
                    text=f"⚠️ {job_name} failed to deliver to {chat_id}: {str(first_err)[:200]}",
                )
            except Exception:
                pass
            return None


async def _safe_send(msg, text: str, update: Update = None):
    """Send text with HTML parse_mode; fall back to plain text if Telegram rejects the HTML."""
    import re as _re
    try:
        await msg.edit_text(text, parse_mode="HTML")
    except Exception:
        # Strip all HTML tags and retry as plain text
        plain = _re.sub(r"<[^>]+>", "", text)
        try:
            await msg.edit_text(plain)
        except Exception:
            await msg.edit_text(plain[:4000])


async def cmd_finances(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        text = await asyncio.to_thread(_render_finances)
        for section in _split_sections(text):
            await update.message.reply_text(section, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Couldn't load finances right now ({type(e).__name__}).")


async def cmd_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("📰 Reading newsletters and running analysis...")
    try:
        brief = await agent.stocks_brief()
        sections = _split_sections(brief)
        await _safe_send(msg, sections[0])
        for section in sections[1:]:
            try:
                await update.message.reply_text(section, parse_mode="HTML")
            except Exception:
                import re as _re
                await update.message.reply_text(_re.sub(r"<[^>]+>", "", section))
    except Exception as e:
        logger.exception("cmd_stocks failed")
        await msg.edit_text(f"⚠️ {escape(str(e)[:300])}", parse_mode="HTML")


async def send_stocks_brief(context: ContextTypes.DEFAULT_TYPE):
    # Stocks/crypto is Ansen's thing — Jess doesn't get the nightly push.
    # (She can still pull it herself with /stocks if she ever wants it.)
    try:
        brief = await agent.stocks_brief()
        sections = _split_sections(brief)
        await _send_or_alert(context, ANSEN_ID, "📊 <b>Daily Stocks & Crypto Brief</b>", "stocks_brief")
        for section in sections:
            await _send_or_alert(context, ANSEN_ID, section, "stocks_brief")
    except Exception:
        logger.exception("Error sending stocks brief")


async def cmd_babyknowledge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    query = " ".join(context.args) if context.args else ""
    msg = await update.message.reply_text("📚 Searching baby knowledge base..." if query else "📚 Loading baby knowledge base...")
    try:
        text = await agent.baby_knowledge_brief(query)
        await _safe_send(msg, text)
    except Exception as e:
        logger.exception("cmd_babyknowledge failed")
        await msg.edit_text(f"⚠️ {escape(str(e)[:200])}", parse_mode="HTML")


async def cmd_baby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text(
        "👶 <b>Baby</b>",
        parse_mode="HTML",
        reply_markup=_baby_menu(),
    )


_STATUS_ICON = {"going": "🎟", "maybe": "❓", "cant_go": "❌", "sold": "💸"}

def _format_shows(shows: list) -> tuple[str, InlineKeyboardMarkup | None]:
    from datetime import datetime as _dt
    if not shows:
        return "🎟 <b>Upcoming Shows</b>\n\nNothing saved yet. Drop a ticket screenshot and I'll add it.", None
    lines = ["🎟 <b>Upcoming Shows</b>\n"]
    del_rows = []
    for s in shows:
        name = s["show_name"]
        venue = s.get("venue") or ""
        dt_raw = s.get("show_date") or ""
        tm = s.get("show_time") or ""
        cal = " ✅" if s.get("calendar_added") else ""
        notes = s.get("notes") or ""
        status = s.get("status") or "going"
        icon = _STATUS_ICON.get(status, "🎟")
        if dt_raw:
            try:
                d = _dt.strptime(dt_raw, "%Y-%m-%d")
                dt_str = d.strftime("%-d %b %Y")
            except Exception:
                dt_str = dt_raw
        else:
            dt_str = "date TBC"
        detail = " · ".join(x for x in [dt_str, tm] if x)
        name_display = f"<s>{name}</s>" if status in ("cant_go", "sold") else f"<b>{name}</b>"
        line = f"{icon} {name_display}{cal}"
        if venue:
            line += f"\n  📍 {venue}"
        if detail:
            line += f"\n  📅 {detail}"
        if notes:
            line += f"\n  <i>{notes}</i>"
        lines.append(line)
        label = name[:28] + "…" if len(name) > 28 else name
        del_rows.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"show_del:{s['id']}")])
    text = "\n\n".join(lines)
    keyboard = InlineKeyboardMarkup(del_rows) if del_rows else None
    return text, keyboard


async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or ""
    msg = await update.message.reply_text("Loading your tasks...")
    try:
        text, tasks = await agent.personal_brief(user_id, user_name)
        keyboard = _reminders_keyboard(tasks, user_id)
        rows = list(keyboard.inline_keyboard) if keyboard else []
        if user_id == ANSEN_ID:
            rows.append([InlineKeyboardButton("🎟 Shows", callback_data="me_shows")])
        keyboard = InlineKeyboardMarkup(rows) if rows else None
        await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.exception("cmd_me failed")
        await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def cmd_shows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    if update.effective_user.id != ANSEN_ID:
        return
    try:
        shows = get_upcoming_shows()
        text, keyboard = _format_shows(shows)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.exception("cmd_shows failed")
        await update.message.reply_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def send_show_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Daily check — DM Ansen about any show 7 days away."""
    try:
        shows = get_shows_in_n_days(7)
        for show in shows:
            if show.get("calendar_added"):
                continue
            name = show["show_name"]
            venue = show.get("venue") or ""
            dt_raw = show.get("show_date") or ""
            tm = show.get("show_time") or ""
            from datetime import datetime as _dt
            if dt_raw:
                try:
                    d = _dt.strptime(dt_raw, "%Y-%m-%d")
                    dt_str = d.strftime("%-d %b")
                except Exception:
                    dt_str = dt_raw
            else:
                dt_str = "date TBC"
            detail = " · ".join(x for x in [dt_str, tm] if x)
            lines = [f"🎟 <b>{name}</b> is one week away!"]
            if venue:
                lines.append(f"📍 {venue}")
            if detail:
                lines.append(f"📅 {detail}")
            lines.append("\nWant to add this to the shared calendar?")
            text = "\n".join(lines)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Add to Calendar", callback_data=f"show_cal_yes:{show['id']}"),
                    InlineKeyboardButton("❌ Not now", callback_data=f"show_cal_no:{show['id']}"),
                ]
            ])
            await context.bot.send_message(
                chat_id=ANSEN_ID, text=text, parse_mode="HTML", reply_markup=keyboard
            )
    except Exception:
        logger.exception("send_show_reminders failed")


async def _handle_show_cal_callback(query, context, data: str):
    chat_id = query.message.chat_id
    if ":" not in data:
        return
    action, show_id = data.split(":", 1)
    await query.edit_message_reply_markup(reply_markup=None)

    if action == "no":
        await context.bot.send_message(chat_id=chat_id, text="👍 No problem — let me know if you want to add it later.")
        return

    show = get_show_by_id(show_id)
    if not show:
        await context.bot.send_message(chat_id=chat_id, text="Couldn't find that show.")
        return

    name = show["show_name"]
    dt_raw = show.get("show_date")
    tm = show.get("show_time") or "20:00"
    venue = show.get("venue") or ""

    if not dt_raw:
        await context.bot.send_message(chat_id=chat_id, text=f"No date saved for {name} — add it first.")
        return

    # Build start/end (default 3h show if no duration known)
    import re as _re
    # Normalise time: "8:00 PM" → "20:00", "20:00" stays
    time_clean = tm.strip()
    match = _re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", time_clean, _re.IGNORECASE)
    if match:
        h, m, period = int(match.group(1)), int(match.group(2)), (match.group(3) or "").upper()
        if period == "PM" and h != 12:
            h += 12
        elif period == "AM" and h == 12:
            h = 0
        start_str = f"{dt_raw}T{h:02d}:{m:02d}:00"
        end_str = f"{dt_raw}T{min(h+3, 23):02d}:{m:02d}:00"
    else:
        start_str = f"{dt_raw}T20:00:00"
        end_str = f"{dt_raw}T23:00:00"

    try:
        from tools.gcal import create_event
        import asyncio
        result = await asyncio.to_thread(create_event, name, start_str, end_str, "", venue)
        mark_show_calendar_added(show_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ <b>{name}</b> added to the shared calendar.",
            parse_mode="HTML",
        )
    except Exception as e:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Couldn't add to calendar: {str(e)[:200]}",
        )


async def send_baby_weekly(context: ContextTypes.DEFAULT_TYPE):
    """Auto-push every Monday morning."""
    if not ALLOWED_IDS:
        return
    try:
        from tools.loop_state import COUPLE, load_state as _load_loop, save_state as _save_loop
        from datetime import datetime as _dt
        _prev = (await asyncio.to_thread(_load_loop, "baby_weekly", COUPLE)).get("last_output") or ""
        brief = await agent.baby_brief(already_sent=_prev)
        if brief:
            _today = _dt.now(REMINDER_TIMEZONE).date().isoformat()
            await asyncio.to_thread(_save_loop, "baby_weekly", COUPLE, brief, _today)
        sections = _split_sections(brief)
        for uid in ALLOWED_IDS:
            for section in sections:
                await _send_or_alert(context, uid, section, "baby_weekly")
    except Exception:
        logger.exception("Error sending baby weekly brief")


def _symptom_keyboard(symptoms: list) -> InlineKeyboardMarkup:
    rows, row = [], []
    for s in symptoms:
        row.append(InlineKeyboardButton(s, callback_data=f"sym:{s[:30]}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("😊 Feeling good", callback_data="sym:Feeling good"),
                 InlineKeyboardButton("💬 Tell you more", callback_data="sym:more")])
    return InlineKeyboardMarkup(rows)


async def send_jess_checkin(context: ContextTypes.DEFAULT_TYPE):
    """Jess's daily pregnancy companion — her own message, interactive. The
    reason she opens the app: it's about her, and it does something with her reply."""
    if JESS_ID not in ALLOWED_IDS:
        return
    try:
        result = await agent.jess_checkin()
        text = result.get("text") if isinstance(result, dict) else result
        if not text:
            return
        symptoms = result.get("symptoms", []) if isinstance(result, dict) else []
        sections = _split_sections(text)
        for i, section in enumerate(sections):
            kb = _symptom_keyboard(symptoms) if (i == len(sections) - 1 and symptoms) else None
            await _send_or_alert(context, JESS_ID, section, "jess_checkin", reply_markup=kb)
        # The check-in is the agent opening the conversation — record it so if she
        # replies in her own words (not a tap), the agent continues from here.
        _thread_into_history(
            JESS_ID,
            "[Daily pregnancy check-in sent to Jess — how is she feeling today?]",
            text,
        )
    except Exception:
        logger.exception("Error sending Jess check-in")


async def send_priority_brief(context: ContextTypes.DEFAULT_TYPE):
    if not ALLOWED_IDS:
        return
    try:
        from tools.loop_state import COUPLE, load_state as _load_loop, save_state as _save_loop
        from datetime import datetime as _dt
        _prev = (await asyncio.to_thread(_load_loop, "priority_brief", COUPLE)).get("last_output") or ""
        brief = await agent.priority_brief(already_sent=_prev)
        if brief:
            _today = _dt.now(REMINDER_TIMEZONE).date().isoformat()
            await asyncio.to_thread(_save_loop, "priority_brief", COUPLE, brief, _today)
        sections = _split_sections(brief)
        for uid in ALLOWED_IDS:
            await _send_or_alert(context, uid, "<b>Weekly Planning Check-in</b>", "priority_brief")
            for section in sections:
                await _send_or_alert(context, uid, section, "priority_brief")
    except Exception:
        logger.exception("Error sending priority brief")


async def cmd_testnotify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    my_id = update.effective_user.id
    partner_ids = [uid for uid in ALLOWED_IDS if uid != my_id]
    if not partner_ids:
        await update.message.reply_text(
            f"No partner IDs found.\nALLOWED_IDS configured: {ALLOWED_IDS}\nYour ID: {my_id}"
        )
        return
    results = []
    for uid in partner_ids:
        try:
            await context.bot.send_message(chat_id=uid, text="📨 Test notification — this is working!")
            results.append(f"✅ Sent to {uid}")
        except Exception as e:
            results.append(f"❌ Failed for {uid}: {e}")
    await update.message.reply_text(
        f"Your ID: {my_id}\nALLOWED_IDS: {ALLOWED_IDS}\n\n" + "\n".join(results)
    )


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("Analysing where things stand...")
    try:
        brief = await agent.priority_brief()
        sections = _split_sections(brief)
        await msg.edit_text(sections[0], parse_mode="HTML")
        for section in sections[1:]:
            await update.message.reply_text(section, parse_mode="HTML")
    except Exception as e:
        logger.exception("cmd_plan failed")
        await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("Checking your tasks...")
    try:
        user_names = await _fetch_user_names(context)
        text, tasks = await agent.combined_daily_brief(ALLOWED_IDS, user_names)
        keyboard = _reminders_keyboard(tasks, update.effective_user.id)
        await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.exception("cmd_tasks failed")
        await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("Pulling reminders...")
    try:
        user_names = await _fetch_user_names(context)
        text, tasks = await agent.reminders_brief(ALLOWED_IDS, user_names)
        keyboard = _reminders_keyboard(tasks, update.effective_user.id)
        await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.exception("cmd_reminders failed")
        await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


_NOTIF_USER_NAMES = {ANSEN_ID: "Ansen", JESS_ID: "Jess"}
_NOTIF_BUTTON_CAP = 20


def _notif_label(row: dict, count: int = 1) -> str:
    """Button text: the time, then enough of the message to recognise it."""
    body = (row.get("message") or "").replace("\n", " ").strip()
    body = " ".join(body.split())
    if len(body) > 34:
        body = body[:33].rstrip() + "…"
    when = local_time_label(row["scheduled_at"]).split(", ")[-1]
    suffix = f" ×{count}" if count > 1 else ""
    return f"❌ {when} · {body}{suffix}"


def _notif_day_header(ts: str) -> str:
    """'Tue 4 Aug' — with the year once we're past this one, so a 2029 visa
    alert doesn't read like next Tuesday."""
    dt = _notif_dt(ts)
    stamp = dt.strftime("%a %-d %b")
    return stamp if dt.year == ddate.today().year else f"{stamp} {dt.year}"


def _notif_dt(ts: str):
    from datetime import datetime as _dt, timezone as _tz
    d = _dt.fromisoformat(ts)
    if not d.tzinfo:
        d = d.replace(tzinfo=_tz.utc)
    return d.astimezone(REMINDER_TIMEZONE)


def _notifications_view(viewer_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Everything scheduled for the household, with a cancel button on each and
    a 'stop all' on anything that recurs. This is the whole point: no reminder
    should need a code change to switch off."""
    rows = list_scheduled(user_ids=ALLOWED_IDS or [viewer_id])
    if not rows:
        return ("🔔 <b>Scheduled reminders</b>\n\nNothing scheduled.\n\n"
                "<i>Ask me to remind you about anything at a specific time.</i>", None)

    groups = group_duplicates(rows)
    extra = sum(g["count"] - 1 for g in groups if g["duplicate"])
    multi_user = len({r["user_id"] for r in rows}) > 1

    lines = [f"🔔 <b>Scheduled reminders</b> ({len(rows)})"]
    if extra:
        lines.append(f"⚠️ {extra} duplicate{'s' if extra != 1 else ''} — tap ❌ on the copies you don't want.")
    lines.append("")

    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(_notif_day_header(r["scheduled_at"]), []).append(r)

    shown, hidden = 0, 0
    for day, day_rows in by_day.items():
        if shown >= 30:
            hidden += len(day_rows)
            continue
        lines.append(f"<b>{escape(day)}</b>")
        for r in day_rows:
            when = local_time_label(r["scheduled_at"]).rsplit(", ", 1)[-1]
            body = " ".join((r.get("message") or "").split())
            if len(body) > 66:
                body = body[:65].rstrip() + "…"
            rec = r.get("recurrence", "none")
            bits = [rec] if rec and rec != "none" else []
            if multi_user:
                bits.append(_NOTIF_USER_NAMES.get(r["user_id"], ""))
            tail = f" <i>· {' · '.join(b for b in bits if b)}</i>" if bits else ""
            lines.append(f"• {when} — {escape(body)}{tail}")
            shown += 1
        lines.append("")
    if hidden:
        lines.append(f"<i>+ {hidden} further out. Ask me to list them if you need to.</i>")

    buttons = [[InlineKeyboardButton(_notif_label(r), callback_data=f"notifdel:{r['id']}")]
               for r in rows[:_NOTIF_BUTTON_CAP]]

    # One tap to kill an entire recurring series rather than occurrence by occurrence.
    seen_series: list[tuple[int, str]] = []
    for g in groups:
        key = (g["user_id"], g["message"])
        if g["recurrence"] and g["recurrence"] != "none" and key not in seen_series:
            seen_series.append(key)
    for uid, msg in seen_series[:8]:
        row = next(r for r in rows if r["user_id"] == uid and r["message"] == msg)
        short = " ".join(msg.split())[:24].rstrip()
        owner = f" ({_NOTIF_USER_NAMES.get(uid, '')})" if multi_user else ""
        buttons.append([InlineKeyboardButton(f"🔕 Stop all “{short}…”{owner}",
                                             callback_data=f"notifstop:{row['id']}")])

    if len(rows) > _NOTIF_BUTTON_CAP:
        lines.append(f"<i>❌ buttons cover the next {_NOTIF_BUTTON_CAP}. For the rest just tell me "
                     f"— e.g. \"turn off the Lucille reminders\".</i>")

    text = "\n".join(lines).strip()
    if len(text) > 3800:
        text = text[:3800].rsplit("\n", 1)[0] + "\n\n<i>…trimmed. Ask me for the full list.</i>"
    return text, InlineKeyboardMarkup(buttons)


async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("Pulling scheduled reminders...")
    try:
        text, keyboard = await asyncio.to_thread(_notifications_view, update.effective_user.id)
        await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.exception("cmd_notifications failed")
        await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the bot's persistent memory — profile + discrete mem0 facts."""
    if not allowed(update):
        return
    from tools.user_memory import get_summary as _get_summary
    from tools.mem0_memory import get_all_memories as _get_mem0
    user_id = update.effective_user.id
    try:
        summary = _get_summary(user_id) or ""
        mem0_facts = await asyncio.to_thread(_get_mem0, user_id)

        parts = []
        if summary.strip():
            parts.append(f"<b>🧠 Profile (behavioral)</b>\n\n{summary}")
        if mem0_facts:
            facts_text = "\n".join(f"• {m['memory']}" for m in mem0_facts if m.get("memory"))
            parts.append(f"<b>💡 Recalled facts ({len(mem0_facts)})</b>\n\n{facts_text}")

        if not parts:
            await update.message.reply_text("Nothing stored yet — talk to me for a bit and I'll start building a picture.")
            return

        for part in parts:
            sections = _split_sections(part)
            for section in sections:
                await update.message.reply_text(section, parse_mode="HTML")
    except Exception as e:
        logger.exception("cmd_memory failed")
        await update.message.reply_text(f"[DEBUG] {type(e).__name__}: {str(e)[:200]}")


_FACTS_BUTTON = InlineKeyboardMarkup([[InlineKeyboardButton("📋 Exact facts", callback_data="shared_facts")]])


async def _send_brain_story(context, chat_id: int, placeholder_msg):
    """Synthesised story view of the shared brain, with a flip to raw facts."""
    story = await agent.brain_synthesis()
    sections = _split_sections("<b>🧠 Your story so far</b>\n\n" + story)
    await placeholder_msg.edit_text(sections[0], parse_mode="HTML",
                                    reply_markup=_FACTS_BUTTON if len(sections) == 1 else None)
    for i, section in enumerate(sections[1:], start=2):
        await context.bot.send_message(
            chat_id=chat_id, text=section, parse_mode="HTML",
            reply_markup=_FACTS_BUTTON if i == len(sections) else None,
        )


async def cmd_shared(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        if not (get_shared_summary() or "").strip():
            await update.message.reply_text("Nothing in the shared brain yet. Confirmed decisions will appear here automatically.")
            return
        msg = await update.message.reply_text("Writing your story...")
        await _send_brain_story(context, update.effective_chat.id, msg)
    except Exception as e:
        logger.exception("cmd_shared failed")
        await update.message.reply_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


_CAT_EMOJI = {
    "health": "🏥", "finance": "💰", "personal": "🙋", "home": "🏠",
    "social": "🎉", "travel": "✈️", "food": "🍽️", "baby": "👶",
    "wedding": "💒", "work": "💼",
}


def _format_groceries() -> tuple[str, InlineKeyboardMarkup | None]:
    from tools.groceries import get_active_lists
    lists = get_active_lists()
    if not lists:
        return "🛒 <b>Groceries</b>\n\nNo active lists. Just say \"add milk to grocery list\" to start one.", None
    blocks = ["🛒 <b>Grocery Lists</b>\n"]
    rows = []
    for lst in lists:
        items = lst.get("items") or []
        count = len(items)
        blocks.append(f"\n<b>{escape(lst['name'])}</b>  ·  {count} item{'s' if count != 1 else ''}")
        for it in items[:12]:
            blocks.append(f"  • {escape(it['item'])}")
        if count > 12:
            blocks.append(f"  <i>+{count - 12} more…</i>")
        rows.append([InlineKeyboardButton(f"✅ Done — {lst['name'][:28]}", callback_data=f"grocery_done:{lst['id']}")])
    text = "\n".join(blocks)
    keyboard = InlineKeyboardMarkup(rows)
    return text, keyboard


async def cmd_groceries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        text, keyboard = _format_groceries()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.exception("cmd_groceries failed")
        await update.message.reply_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


_FYI_FACTS_BUTTON = InlineKeyboardMarkup([[InlineKeyboardButton("📋 Exact facts", callback_data="fyis_facts")]])


async def _send_fyi_story(context, chat_id: int, placeholder_msg):
    """FYIs as a story — context to catch up on, not an inbox to clear."""
    story = await agent.fyi_story()
    if not story:
        await placeholder_msg.edit_text("Nothing shared this month yet — FYIs will build the story as they come in.")
        return
    sections = _split_sections("<b>📨 The month so far</b>\n\n" + story)
    await placeholder_msg.edit_text(sections[0], parse_mode="HTML",
                                    reply_markup=_FYI_FACTS_BUTTON if len(sections) == 1 else None)
    for i, section in enumerate(sections[1:], start=2):
        await context.bot.send_message(
            chat_id=chat_id, text=section, parse_mode="HTML",
            reply_markup=_FYI_FACTS_BUTTON if i == len(sections) else None,
        )


def _format_fyis(fyis: list) -> str:
    grouped: dict = {}
    for f in fyis:
        cat = (f.get("category") or "other").lower()
        grouped.setdefault(cat, []).append(f)
    blocks = ["<b>📨 Recent FYIs</b>\n"]
    for cat, items in grouped.items():
        emoji = _CAT_EMOJI.get(cat, "📌")
        blocks.append(f"\n{emoji} <b>{cat.title()}</b>")
        for f in items:
            when = (f.get("created_at") or "")[:10]
            blocks.append(f"• <i>{when}</i> — {escape(f['content'])}")
    return "\n".join(blocks)


def _format_fyis_with_buttons(fyis: list) -> tuple[str, InlineKeyboardMarkup | None]:
    """FYIs as a compact card list — tap each to expand full content."""
    grouped: dict = {}
    for f in fyis:
        cat = (f.get("category") or "other").lower()
        grouped.setdefault(cat, []).append(f)
    count = len(fyis)
    blocks = [f"<b>📨 FYIs</b>  ·  {count} unread\n"]
    for cat, items in grouped.items():
        emoji = _CAT_EMOJI.get(cat, "📌")
        blocks.append(f"{emoji} <b>{cat.title()}</b>")
        for f in items:
            snippet = f["content"][:60].strip()
            if len(f["content"]) > 60:
                snippet += "…"
            blocks.append(f"• {snippet}")
        blocks.append("")
    text = "\n".join(blocks).rstrip()
    rows = []
    for f in fyis:
        cat = (f.get("category") or "other").lower()
        emoji = _CAT_EMOJI.get(cat, "📌")
        label = f["content"][:38].strip()
        if len(f["content"]) > 38:
            label += "…"
        rows.append([InlineKeyboardButton(f"{emoji} {label} →", callback_data=f"fyi_expand:{f['id']}")])
    keyboard = InlineKeyboardMarkup(rows) if rows else None
    return text, keyboard


async def cmd_fyis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        msg = await update.message.reply_text("Reading the month's FYIs...")
        await _send_fyi_story(context, update.effective_chat.id, msg)
    except Exception as e:
        logger.exception("cmd_fyis failed")
        await update.message.reply_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def _handle_shared_callback(query, context, action: str, user_id: int):
    chat_id = query.message.chat_id

    if action == "brain":
        msg = await context.bot.send_message(chat_id=chat_id, text="Writing your story...")
        try:
            await _send_brain_story(context, chat_id, msg)
        except Exception as e:
            await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif action == "facts":
        # Raw shared brain — the exact bullets behind the story
        try:
            summary = get_shared_summary()
            if not (summary or "").strip():
                await context.bot.send_message(chat_id=chat_id, text="Nothing in the shared brain yet.")
                return
            sections = _split_sections("<b>📋 Exact facts — shared brain</b>\n\n" + summary)
            for section in sections:
                await context.bot.send_message(chat_id=chat_id, text=section, parse_mode="HTML")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif action == "fyis":
        msg = await context.bot.send_message(chat_id=chat_id, text="Reading the month's FYIs...")
        try:
            await _send_fyi_story(context, chat_id, msg)
        except Exception as e:
            await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif action == "tasks":
        msg = await context.bot.send_message(chat_id=chat_id, text="Checking your tasks...")
        try:
            user_names = await _fetch_user_names(context)
            text, tasks = await agent.combined_daily_brief(ALLOWED_IDS, user_names)
            keyboard = _reminders_keyboard(tasks, user_id)
            await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif action == "reminders":
        msg = await context.bot.send_message(chat_id=chat_id, text="Pulling reminders...")
        try:
            user_names = await _fetch_user_names(context)
            text, tasks = await agent.reminders_brief(ALLOWED_IDS, user_names)
            keyboard = _reminders_keyboard(tasks, user_id)
            await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif action == "budget":
        try:
            from tools.shared_budget import summary as shared_budget_summary
            from tools.baby_budget import summary as baby_budget_summary
            data = shared_budget_summary()
            baby = baby_budget_summary()

            STATUS_EMOJI = {"owing": "🔴", "paid": "✅", "pending": "⏳", "quoted": "💬"}
            CAT_EMOJI = {"home": "🏠", "travel": "✈️", "food": "🍽️", "subscriptions": "📱",
                         "transport": "🚗", "medical": "🏥", "other": "📦"}

            lines = ["💰 <b>Shared Budget</b>\n"]

            # Life / shared
            lines.append(f"🏠 <b>Life & Shared</b>")
            lines.append(f"Owing: <b>SGD {data['total_owing']:,.0f}</b>  •  Paid: <b>SGD {data['total_paid']:,.0f}</b>\n")
            for cat, items in sorted(data["by_category"].items()):
                emoji = CAT_EMOJI.get(cat, "📦")
                lines.append(f"{emoji} <b>{cat.title()}</b>")
                for i in items:
                    status_icon = STATUS_EMOJI.get(i.get("status", "owing"), "•")
                    amt = f" — SGD {i['amount']:,.0f}" if i.get("amount") else ""
                    lines.append(f"{status_icon} {i['item']}{amt}")
                lines.append("")

            # Baby
            lines.append(f"\n👶 <b>Baby</b>")
            lines.append(f"Spent: <b>SGD {baby['total_spent']:,.0f}</b>  •  Planned: <b>SGD {baby['total_planned']:,.0f}</b>")

            total_owing = data["total_owing"] + baby["total_planned"]
            total_paid = data["total_paid"] + baby["total_spent"]
            lines.append(f"\n📊 <b>Total picture</b>")
            lines.append(f"Outstanding: <b>SGD {total_owing:,.0f}</b>")
            lines.append(f"Settled: <b>SGD {total_paid:,.0f}</b>")

            text = "\n".join(lines)
            sections = _split_sections(text)
            for section in sections:
                await context.bot.send_message(chat_id=chat_id, text=section, parse_mode="HTML")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif action == "travel":
        try:
            from tools.trips import get_upcoming_trips
            from datetime import datetime as _dt
            trips = get_upcoming_trips()
            if not trips:
                text = "✈️ <b>Travel</b>\n\nNo upcoming trips yet. Just mention where you're going and I'll add it."
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
                return
            STATUS_ICON = {"planning": "🗓", "booked": "✅", "completed": "🏁", "cancelled": "❌"}
            SECTIONS = [
                ("shared", "👫 <b>Together</b>"),
                ("ansen",  "👤 <b>Ansen</b>"),
                ("jess",   "👤 <b>Jess</b>"),
            ]
            def _fmt_date(d):
                try:
                    return _dt.strptime(d, "%Y-%m-%d").strftime("%-d %b '%y")
                except Exception:
                    return d or "TBC"

            lines = ["✈️ <b>Travel</b>\n"]
            rows = []
            for vis, header in SECTIONS:
                section_trips = [t for t in trips if (t.get("visibility") or "shared") == vis]
                if not section_trips:
                    continue
                lines.append(header)
                for t in section_trips:
                    dest = t["destination"]
                    icon = STATUS_ICON.get(t.get("status") or "planning", "🗓")
                    date_str = _fmt_date(t.get("start_date") or "")
                    lines.append(f"{icon} <b>{escape(dest)}</b>  ·  {date_str}")
                    rows.append([InlineKeyboardButton(f"{icon} {dest[:24]}  {date_str} →", callback_data=f"trip_expand:{t['id']}")])
                lines.append("")
            text = "\n".join(lines).rstrip()
            keyboard = InlineKeyboardMarkup(rows)
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif action == "finances":
        try:
            text = await asyncio.to_thread(_render_finances)
            for section in _split_sections(text):
                await context.bot.send_message(chat_id=chat_id, text=section, parse_mode="HTML")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif action == "groceries":
        try:
            text, keyboard = _format_groceries()
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def _handle_wedding_callback(query, context, action: str, user_id: int):
    chat_id = query.message.chat_id

    if action == "categories":
        await context.bot.send_message(
            chat_id=chat_id,
            text="📂 <b>Categories</b>",
            parse_mode="HTML",
            reply_markup=_category_menu(),
        )
        return

    if action.startswith("cat_"):
        cat_key = action[4:]
        if cat_key not in CATEGORIES:
            return
        msg = await context.bot.send_message(chat_id=chat_id, text="Checking...")
        try:
            status = await agent.category_status(cat_key)
            sections = _split_sections(status)
            await msg.edit_text(sections[0], parse_mode="HTML")
            for section in sections[1:]:
                await context.bot.send_message(chat_id=chat_id, text=section, parse_mode="HTML")
        except Exception as e:
            logger.exception("category_status callback failed")
            await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")
        return

    if action == "bringmeuptospeed":
        msg = await context.bot.send_message(chat_id=chat_id, text="Pulling everything together...")
        try:
            summary = await agent.bring_me_up_to_speed()
            sections = _split_sections(summary)
            await msg.edit_text(sections[0], parse_mode="HTML")
            for section in sections[1:]:
                await context.bot.send_message(chat_id=chat_id, text=section, parse_mode="HTML")
        except Exception as e:
            await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif action == "plan":
        msg = await context.bot.send_message(chat_id=chat_id, text="Analysing where things stand...")
        try:
            brief = await agent.priority_brief()
            sections = _split_sections(brief)
            await msg.edit_text(sections[0], parse_mode="HTML")
            for section in sections[1:]:
                await context.bot.send_message(chat_id=chat_id, text=section, parse_mode="HTML")
        except Exception as e:
            await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def _handle_baby_callback(query, context, action: str):
    import re as _re
    chat_id = query.message.chat_id

    if action == "brief":
        msg = await context.bot.send_message(chat_id=chat_id, text="👶 Checking in on the little one...")
        try:
            brief = await agent.baby_brief()
            sections = _split_sections(brief)
            await _safe_send(msg, sections[0])
            for section in sections[1:]:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=section, parse_mode="HTML")
                except Exception:
                    await context.bot.send_message(chat_id=chat_id, text=_re.sub(r"<[^>]+>", "", section))
        except Exception as e:
            await msg.edit_text(f"⚠️ {escape(str(e)[:200])}", parse_mode="HTML")

    elif action == "knowledge":
        msg = await context.bot.send_message(chat_id=chat_id, text="📚 Loading baby knowledge base...")
        try:
            text = await agent.baby_knowledge_brief("")
            await _safe_send(msg, text)
        except Exception as e:
            await msg.edit_text(f"⚠️ {escape(str(e)[:200])}", parse_mode="HTML")

    elif action == "milestones":
        from tools.baby import upcoming_milestones, pregnancy_summary
        summary = pregnancy_summary()
        milestones = upcoming_milestones(within_weeks=8)
        lines = [f"<b>📅 Milestones</b>", f"Week {summary['week']} • due {summary['due_date']}\n"]
        if milestones:
            lines += milestones
        else:
            lines.append("No milestones in the next 8 weeks.")
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")

    elif action == "reminders":
        msg = await context.bot.send_message(chat_id=chat_id, text="Checking baby reminders...")
        try:
            user_names = await _fetch_user_names(context)
            text, tasks = await agent.baby_reminders_brief(ALLOWED_IDS, user_names)
            keyboard = _reminders_keyboard(tasks, query.from_user.id)
            await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif action == "questions":
        msg = await context.bot.send_message(chat_id=chat_id, text="Loading questions...")
        try:
            user_names = await _fetch_user_names(context)
            text, tasks = await agent.baby_questions_brief(ALLOWED_IDS, user_names)
            keyboard = _reminders_keyboard(tasks, query.from_user.id)
            await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif action == "budget":
        msg = await context.bot.send_message(chat_id=chat_id, text="Loading baby budget...")
        try:
            text = await agent.baby_budget_brief()
            await _safe_send(msg, text)
        except Exception as e:
            await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    user_id = query.from_user.id
    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        await query.answer("Not authorised.")
        return

    data = query.data

    if data.startswith("done:"):
        payload = data[5:]
        try:
            success = complete_task(payload, user_id)
        except Exception:
            await query.answer("Couldn't mark done — try again.")
            return
        if not success:
            await query.answer("That's not your task to mark done.")
            return
        await query.answer("✅ Done!")
        try:
            current = query.message.reply_markup
            if current:
                new_rows = [
                    row for row in current.inline_keyboard
                    if not any(btn.callback_data == data for btn in row)
                ]
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup(new_rows) if new_rows else None
                )
        except Exception:
            logger.exception("handle_callback button removal failed")

    elif data.startswith("wedding_"):
        await query.answer()
        await _handle_wedding_callback(query, context, data[8:], user_id)

    elif data.startswith("baby_"):
        await query.answer()
        await _handle_baby_callback(query, context, data[5:])

    elif data == "fyis_facts":
        # Raw FYI list — the exact notes behind the story
        try:
            fyis = get_fyis(limit=30)
            if not fyis:
                await context.bot.send_message(chat_id=update.effective_chat.id, text="No FYIs in the last 30 days.")
            else:
                sections = _split_sections(_format_fyis(fyis))
                for section in sections:
                    await context.bot.send_message(chat_id=update.effective_chat.id, text=section, parse_mode="HTML")
        except Exception as e:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif data.startswith("shared_"):
        await query.answer()
        await _handle_shared_callback(query, context, data[7:], user_id)

    elif data.startswith("fyi_"):
        await query.answer()
        await _handle_fyi_callback(query, context, data[4:])

    elif data.startswith("sym:"):
        symptom = data[4:]
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        chat_id = query.message.chat_id
        if symptom == "more":
            invite = "Tell me how you're feeling — I'll note it and bring it up before your next appointment."
            await context.bot.send_message(chat_id=chat_id, text=invite)
            # Seed the thread so her free-text reply continues THIS check-in
            # (agent sees she's about to describe how she feels) instead of cold.
            _thread_into_history(
                chat_id,
                "[Tapped 'Tell you more' on today's pregnancy check-in — about to describe how she's feeling]",
                invite,
            )
            return
        try:
            history = _get_history(chat_id)
            reply = await agent.symptom_response(symptom, query.from_user.id, history)
            await context.bot.send_message(chat_id=chat_id, text=reply, parse_mode="HTML")
            # Iterative, not fire-and-forget: the tap + our reply become part of
            # the conversation so a follow-up ("is that normal?") lands in context.
            _thread_into_history(
                chat_id,
                f"[Pregnancy check-in — tapped that she's feeling: {symptom}]",
                reply,
            )
        except Exception:
            logger.exception("symptom response failed")
            await context.bot.send_message(chat_id=chat_id, text="Logged that 💛")

    elif data.startswith("ci:") or data.startswith("cisnz:"):
        await _handle_check_in_callback(query, context, data)

    elif data.startswith("ice:"):
        # Icebox card taps: ice:{task_id}:{done|week|w2|m1|drop}
        from tools.daily import bump_task, get_task_by_id, icebox_task
        try:
            _, tid, act = data.split(":", 2)
            t = get_task_by_id(tid)
            label = (t.get("task") or "task")[:60] if t else "task"
            if act == "done":
                ok = complete_task(tid, user_id)
                result = f"✅ Done: {label}" if ok else "Couldn't mark that done."
            elif act == "week":
                ok = bump_task(tid, 7)
                result = f"📅 Re-committed — due in a week: {label}"
            elif act == "w2":
                ok = icebox_task(tid, 14)
                result = f"❄️ Iceboxed 2 weeks: {label}\nIt'll resurface on its own — no nagging until then."
            elif act == "m1":
                ok = icebox_task(tid, 30)
                result = f"🧊 Iceboxed 1 month: {label}\nIt'll resurface on its own — no nagging until then."
            elif act == "drop":
                ok = complete_task(tid, user_id)
                result = f"🗑 Dropped: {label}"
            else:
                result = "Unknown action."
            await query.answer()
            await query.edit_message_text(result)
        except Exception:
            logger.exception("icebox callback failed")
            await query.answer("Something went wrong — try again.")

    elif data.startswith("taskcat:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            task_id, cat = parts[1], parts[2]
            ok = set_task_category(task_id, cat)
            await query.answer("Filed ✅" if ok else "Couldn't update that task.")
            if ok:
                emoji = _CHECKIN_EMOJI.get(cat, "🏠")
                try:
                    await query.edit_message_text(f"{emoji} Filed under <b>{cat.title()}</b>.", parse_mode="HTML")
                except Exception:
                    pass

    elif data == "me_shows":
        await query.answer()
        if user_id != ANSEN_ID:
            return
        try:
            shows = get_upcoming_shows()
            text, keyboard = _format_shows(shows)
            await context.bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"[DEBUG] {type(e).__name__}: {str(e)[:200]}")

    elif data.startswith("goal_step:"):
        await query.answer()
        step_id = data[10:]
        try:
            from tools.goals import complete_step as _complete_step
            result = _complete_step(step_id)
            if "error" in result:
                await context.bot.send_message(chat_id=query.message.chat_id, text=f"⚠️ {result['error']}")
                return
            # Remove the tapped button
            current = query.message.reply_markup
            if current:
                new_rows = [row for row in current.inline_keyboard if not any(btn.callback_data == data for btn in row)]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows) if new_rows else None)
            step_title = result.get("step_completed", "step")
            if result.get("goal_complete"):
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"🎯 <b>Goal complete!</b>\n\n✅ {escape(step_title)}\n\nAll steps done.",
                    parse_mode="HTML",
                )
            else:
                unblocked = result.get("newly_unblocked", [])
                remaining = result.get("remaining_steps", 0)
                msg = f"✅ Done: <i>{escape(step_title)}</i>"
                if unblocked:
                    msg += "\n\n🔓 <b>Now unblocked:</b>\n" + "\n".join(f"• {escape(s['title'])}" for s in unblocked)
                msg += f"\n\n<i>{remaining} step{'s' if remaining != 1 else ''} remaining</i>"
                await context.bot.send_message(chat_id=query.message.chat_id, text=msg, parse_mode="HTML")
        except Exception as e:
            logger.exception("goal_step callback failed")
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"[DEBUG] {type(e).__name__}: {str(e)[:200]}")

    elif data.startswith("skill_build:"):
        await query.answer()
        idx_str = data[12:]
        try:
            idx = int(idx_str)
        except ValueError:
            return
        gaps = _skills_gaps.get(ANSEN_ID, [])
        if idx >= len(gaps):
            await context.bot.send_message(chat_id=query.message.chat_id, text="⚠️ Skill cache expired — run /skills again.")
            return
        gap = gaps[idx]
        request = f"Build this integration for the wedding-agent bot: {gap.get('gap', '')}\n\nExample use case: {gap.get('example', '')}"
        msg = await context.bot.send_message(chat_id=query.message.chat_id, text="📋 Drafting implementation plan...")
        try:
            code = await agent.developer_build(request)
            sections = _split_sections(code)
            await msg.edit_text(sections[0], parse_mode="HTML")
            for section in sections[1:]:
                try:
                    await context.bot.send_message(chat_id=query.message.chat_id, text=section, parse_mode="HTML")
                except Exception:
                    import re as _re
                    await context.bot.send_message(chat_id=query.message.chat_id, text=_re.sub(r"<[^>]+>", "", section))
            await context.bot.send_message(chat_id=query.message.chat_id, text="💡 <i>This plan lives in chat only — paste it to Claude Code to actually deploy it.</i>", parse_mode="HTML")
        except Exception as e:
            await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif data.startswith("notif_ack:"):
        await query.answer("✅ Got it")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    elif data.startswith("notifdel:"):
        # Cancel one upcoming occurrence from the /notifications list.
        notif_id = data[9:]
        try:
            row = await asyncio.to_thread(get_scheduled, notif_id)
            ok = await asyncio.to_thread(cancel_scheduled, notif_id)
        except Exception:
            logger.exception("notifdel failed")
            await query.answer("Couldn't cancel that — try again")
            return
        await query.answer("🔕 Cancelled" if ok else "Already gone")
        current = query.message.reply_markup
        if current:
            new_rows = [r for r in current.inline_keyboard if not any(b.callback_data == data for b in r)]
            try:
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup(new_rows) if new_rows else None)
            except Exception:
                pass
        if ok and row:
            body = " ".join((row.get("message") or "").split())[:80]
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"🔕 Cancelled — <b>{escape(body)}</b>\n<i>{escape(local_time_label(row['scheduled_at']))}</i>",
                parse_mode="HTML")
        return

    elif data.startswith("notifstop:"):
        # Kill a recurring reminder for good — every pending copy of it.
        notif_id = data[10:]
        try:
            result = await asyncio.to_thread(stop_notification_series, notif_id)
        except Exception:
            logger.exception("notifstop failed")
            await query.answer("Couldn't stop that — try again")
            return
        count = result.get("cancelled", 0)
        await query.answer(f"🔕 Stopped ({count})" if count else "Already stopped")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        body = " ".join((result.get("message") or "").split())[:80]
        if count:
            owner = _NOTIF_USER_NAMES.get(result.get("user_id"), "")
            whose = f" on {owner}'s schedule" if owner and query.from_user.id != result.get("user_id") else ""
            text = (f"🔕 Stopped <b>{escape(body)}</b>{whose}.\n"
                    f"<i>{count} upcoming {'copy' if count == 1 else 'copies'} removed — it won't fire again.</i>")
        else:
            text = f"🔕 <b>{escape(body)}</b> was already off — nothing left scheduled."
        await context.bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode="HTML")
        return

    elif data.startswith("trip_del:"):
        await query.answer()
        trip_id = data[9:]
        try:
            from tools.trips import get_trip_by_id, delete_trip as _del_trip
            trip = get_trip_by_id(trip_id)
            name = trip["destination"] if trip else "that trip"
            _del_trip(trip_id)
            current = query.message.reply_markup
            if current:
                new_rows = [row for row in current.inline_keyboard if not any(btn.callback_data == data for btn in row)]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows) if new_rows else None)
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"🗑 Removed <b>{escape(name)}</b>.", parse_mode="HTML")
        except Exception as e:
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"Couldn't remove: {str(e)[:100]}")

    elif data.startswith("trip_expand:"):
        await query.answer()
        trip_id = data[12:]
        try:
            from tools.trips import get_trip_by_id
            t = get_trip_by_id(trip_id)
            if not t:
                await context.bot.send_message(chat_id=query.message.chat_id, text="Trip not found.")
                return
            await context.bot.send_message(chat_id=query.message.chat_id, text="⏳ Building trip card…", parse_mode="HTML")
            text = await agent.trip_card(t)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Delete trip", callback_data=f"trip_del:{trip_id}")]
            ])
            await context.bot.send_message(
                chat_id=query.message.chat_id, text=text, parse_mode="HTML", reply_markup=keyboard
            )
        except Exception as e:
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"[DEBUG] {type(e).__name__}: {str(e)[:200]}")

    elif data.startswith("grocery_done:"):
        await query.answer()
        list_id = data[13:]
        try:
            from tools.groceries import get_active_lists, close_list
            lists = get_active_lists()
            lst = next((l for l in lists if str(l["id"]) == list_id), None)
            name = lst["name"] if lst else "that list"
            close_list(list_id)
            current = query.message.reply_markup
            if current:
                new_rows = [row for row in current.inline_keyboard if not any(btn.callback_data == data for btn in row)]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows) if new_rows else None)
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"🛒 <b>{escape(name)}</b> — shopping done! List archived.", parse_mode="HTML")
        except Exception as e:
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"Couldn't close: {str(e)[:100]}")

    elif data.startswith("show_del:"):
        await query.answer()
        if user_id != ANSEN_ID:
            return
        show_id = data[9:]
        show = get_show_by_id(show_id)
        name = show["show_name"] if show else "that show"
        try:
            _delete_show_by_id(show_id)
            # Remove just this show's button row from the keyboard
            current = query.message.reply_markup
            if current:
                new_rows = [row for row in current.inline_keyboard if not any(btn.callback_data == data for btn in row)]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows) if new_rows else None)
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"🗑 Removed <b>{escape(name)}</b>.", parse_mode="HTML")
        except Exception as e:
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"Couldn't remove: {str(e)[:100]}")

    elif data.startswith("show_cal_yes:") or data.startswith("show_cal_no:"):
        await query.answer()
        action_key = "yes" if data.startswith("show_cal_yes:") else "no"
        show_id = data.split(":", 1)[1]
        await _handle_show_cal_callback(query, context, f"{action_key}:{show_id}")

    else:
        await query.answer()


async def send_morning_brief(context: ContextTypes.DEFAULT_TYPE):
    """The ONE daily driver (9am): action-first brief per user + any decision
    cards + icebox parking offers. Absorbed the old evening wrap — a single
    morning touchpoint instead of morning brief + nightly wrap."""
    if not ALLOWED_IDS:
        return

    # Check-in lifecycle housekeeping (once daily): lapsed snoozes reopen,
    # week-old unanswered questions expire into brain episodes.
    try:
        reopened = await asyncio.to_thread(reopen_due_snoozed)
        for ci in reopened:
            await _send_check_in_cards(context, [ci], ci.get("created_by") or ALLOWED_IDS[0])
        # An unanswered question is not a memory. Writing these into the brain
        # (18 rows by 30 Jul, all "Unanswered check-in (expired): …") duplicated
        # each other, contradicted the facts that later resolved them, and got
        # fed back into every brief as standing knowledge. The check_ins row is
        # already the record — it keeps status='expired'.
        expired = await asyncio.to_thread(expire_stale, 7)
        if expired:
            logger.info("expired %d stale check-in(s): %s", len(expired),
                        "; ".join((ci.get("question") or "")[:60] for ci in expired))
    except Exception:
        logger.exception("check-in housekeeping failed")

    USER_NAMES = {63756531: "Ansen", 6927468999: "Jess"}
    for uid in ALLOWED_IDS:
        name = USER_NAMES.get(uid, "")
        try:
            text = await agent.morning_brief(uid, name)
            delivered = await _send_or_alert(context, uid, text, "morning_brief")
            if delivered is not None:
                try:
                    from tools.loop_state import save_state as _save_loop
                    from datetime import datetime as _dt
                    today = _dt.now(REMINDER_TIMEZONE).date().isoformat()
                    await asyncio.to_thread(_save_loop, "morning_brief", uid, text, today)
                except Exception:
                    logger.exception("morning brief state save failed")

            # Proactive intelligence → forward-looking prose (what's coming, gaps
            # on wedding/baby/travel) + decision cards. The prose used to be
            # discarded here, which is why the proactive pings went silent. It's
            # written to say NOTHING on a quiet day, so this won't re-spam.
            try:
                result = await agent.proactive_check(uid, name)
                if isinstance(result, dict):
                    lookahead = (result.get("text") or "").strip()
                    check_ins = result.get("check_ins", [])
                else:
                    lookahead, check_ins = (result or "").strip(), []
                if lookahead:
                    import re as _re
                    lookahead = _re.sub(r"\n?-{3,}\n?", "\n\n", lookahead).strip()
                    for section in _split_sections("🔮 <b>Looking ahead</b>\n\n" + lookahead):
                        await _send_or_alert(context, uid, section, "proactive_check")
                await _send_check_in_cards(context, check_ins, uid)
            except Exception:
                logger.exception(f"proactive_check failed for {uid}")
        except Exception:
            logger.exception(f"send_morning_brief failed for uid {uid}")

    # ❄️ Icebox offers — stale tasks get a parking decision, max 2 per morning
    try:
        from datetime import date as _d
        from tools.daily import get_stale_tasks, mark_icebox_offered
        seen: set = set()
        stale_all: list[dict] = []
        for uid in ALLOWED_IDS:
            for t in await asyncio.to_thread(get_stale_tasks, uid):
                if t["id"] in seen:
                    continue
                seen.add(t["id"])
                stale_all.append(t)
        for t in stale_all[:2]:
            target = t.get("assigned_to") or t.get("user_id")
            if target not in ALLOWED_IDS:
                target = ALLOWED_IDS[0]
            due = t.get("due_date")
            age = f"day {(_d.today() - _d.fromisoformat(due)).days} overdue" if due \
                else f"sitting untouched since {(t.get('created_at') or '')[:10]}"
            label = (t.get("task") or "").strip()
            if label.upper().startswith("TASK:"):
                label = label[5:].strip()
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Done", callback_data=f"ice:{t['id']}:done"),
                 InlineKeyboardButton("📅 This week", callback_data=f"ice:{t['id']}:week")],
                [InlineKeyboardButton("❄️ 2 weeks", callback_data=f"ice:{t['id']}:w2"),
                 InlineKeyboardButton("🧊 1 month", callback_data=f"ice:{t['id']}:m1")],
                [InlineKeyboardButton("🗑 Drop it", callback_data=f"ice:{t['id']}:drop")],
            ])
            try:
                await context.bot.send_message(
                    chat_id=target,
                    text=f"❄️ <b>Backlog this?</b>\n{escape(label)}\n<i>{age}</i>",
                    parse_mode="HTML", reply_markup=kb,
                )
                await asyncio.to_thread(mark_icebox_offered, t["id"])
            except Exception:
                logger.exception(f"icebox card send failed for task {t['id']}")
    except Exception:
        logger.exception("icebox offer sweep failed")


async def send_daily_brief(context: ContextTypes.DEFAULT_TYPE):
    """Kept for /tasks command — full structured view with Done buttons."""
    if not ALLOWED_IDS:
        return
    try:
        user_names = await _fetch_user_names(context)
        text, tasks = await agent.combined_daily_brief(ALLOWED_IDS, user_names)
        for uid in ALLOWED_IDS:
            keyboard = _reminders_keyboard(tasks, uid)
            await context.bot.send_message(chat_id=uid, text=text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        logger.exception("Error sending combined daily brief")


async def send_calendar_reconciliation(context: ContextTypes.DEFAULT_TYPE):
    """Detect when calendar events move and sync open task due_dates. Runs before morning brief."""
    import asyncio
    from datetime import date as _date
    from tools.gcal import get_events
    from tools.daily import get_tasks, update_task_date
    from tools.calendar_sync import reconcile_task_dates

    try:
        events = await asyncio.to_thread(get_events, 90, 50)
    except Exception:
        logger.exception("calendar_sync: failed to fetch events")
        return

    all_tasks: list[dict] = []
    seen: set = set()
    for uid in ALLOWED_IDS:
        try:
            for t in get_tasks(uid, include_done=False):
                tid = t.get("id")
                if tid not in seen and t.get("due_date"):
                    seen.add(tid)
                    all_tasks.append(t)
        except Exception:
            pass

    changes = reconcile_task_dates(all_tasks, events)
    if not changes:
        return

    def _fmt(d: str) -> str:
        try:
            return _date.fromisoformat(d).strftime("%-d %b")
        except Exception:
            return d

    lines = []
    for c in changes:
        name = (c["task"].get("task") or "")[:60].strip()
        update_task_date(c["task"]["id"], c["new_date"])
        lines.append(f"• {name}  {_fmt(c['old_date'])} → {_fmt(c['new_date'])}")

    msg = "📅 <b>Calendar sync</b>\n\nThese task dates were updated to match your calendar:\n\n" + "\n".join(lines)
    for uid in ALLOWED_IDS:
        try:
            await context.bot.send_message(chat_id=uid, text=msg, parse_mode="HTML")
        except Exception:
            logger.exception(f"calendar_sync: failed to notify {uid}")


async def check_and_send_notifications(context: ContextTypes.DEFAULT_TYPE):
    try:
        pending = get_pending_notifications()
    except Exception:
        logger.exception("Error fetching pending notifications")
        return
    for notif in pending:
        try:
            row = [InlineKeyboardButton("✅ Got it", callback_data=f"notif_ack:{notif['id']}")]
            # Recurring reminders get an off switch right where they annoy you.
            if (notif.get("recurrence") or "none") != "none":
                row.append(InlineKeyboardButton("🔕 Stop these", callback_data=f"notifstop:{notif['id']}"))
            keyboard = InlineKeyboardMarkup([row])
            await context.bot.send_message(
                chat_id=notif["user_id"], text=notif["message"], reply_markup=keyboard
            )
            mark_notification_sent(notif["id"])
        except Exception:
            logger.exception(f"Failed to send notification {notif['id']} to {notif['user_id']}")


async def send_evening_nuggets(context: ContextTypes.DEFAULT_TYPE):
    """Evening: learning nuggets only — his from r/daddit, hers from
    r/BabyBumps + r/pregnant. The action-driven daily brief moved to 9am; this
    slot is purely optional wind-down reading, one message per person."""
    if not ALLOWED_IDS:
        return
    for feed in _NUGGET_FEEDS:
        try:
            nugget = await agent.nightly_nugget(feed["subreddits"], feed["state_key"], feed["angle"])
            if nugget:
                subs = " + ".join(f"r/{s}" for s in feed["subreddits"])
                await context.bot.send_message(
                    chat_id=feed["user_id"],
                    text=f"🌰 <b>Tonight's nuggets — {subs}</b>\n\n" + nugget,
                    parse_mode="HTML",
                )
        except Exception:
            logger.exception(f"nightly nugget failed for {feed['state_key']}")


async def send_fyi_graduation(context: ContextTypes.DEFAULT_TYPE):
    """Sunday sleep cycle: old episodes consolidate into durable facts or fade;
    legacy FYIs still draining get auto-triaged. Only genuine judgment calls
    get a card — memory is brain-building material, not a checklist."""
    if not ALLOWED_IDS:
        return
    try:
        # Episode consolidation runs regardless of legacy FYIs
        try:
            consolidation = await agent.consolidate_episodes()
        except Exception:
            logger.exception("episode consolidation failed")
            consolidation = {}
        if consolidation.get("promoted") or consolidation.get("faded"):
            lines = []
            if consolidation.get("promoted"):
                lines.append("🧠 <b>Consolidated from this month's episodes</b>\n")
                lines += [f"• {escape(p[:180])}" for p in consolidation["promoted"]]
            faded = consolidation.get("faded") or 0
            if faded:
                lines.append(f"\n💤 {faded} old episode{'s' if faded != 1 else ''} faded quietly.")
            text = "\n".join(lines)
            for uid in ALLOWED_IDS:
                try:
                    await context.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
                except Exception:
                    logger.exception(f"consolidation summary send failed for {uid}")

        expiring = get_fyis_expiring(days_threshold=21, limit=10)
        if not expiring:
            return
        triage = await agent.triage_expiring_fyis(expiring)

        promoted_lines = []
        for f in triage["promote"]:
            try:
                # promote_fyi swallows DB errors and returns None — only write to
                # the brain once the FYI row is actually marked promoted, else it
                # re-triages next Sunday and duplicates the brain entry
                row = promote_fyi(f["id"])
                if not row:
                    continue
                from tools.user_memory import normalize_domain
                fact = f.get("_fact") or f["content"]
                append_shared_summary(fact, domain=normalize_domain(f.get("_domain")), source="fyi_graduation")
                promoted_lines.append(fact)
            except Exception:
                logger.exception(f"auto-promote failed for FYI {f.get('id')}")
        for f in triage["archive"]:
            try:
                archive_fyi(f["id"])
            except Exception:
                logger.exception(f"auto-archive failed for FYI {f.get('id')}")
        episoded = 0
        for f in triage.get("episode", []):
            try:
                from tools.user_memory import add_brain_entry as _add_ep, normalize_domain as _nd
                _add_ep(f["content"], _nd(f.get("category")), "fyi_graduation",
                        (f.get("created_at") or "")[:10] or None, "episode")
                archive_fyi(f["id"])
                episoded += 1
            except Exception:
                logger.exception(f"episode conversion failed for FYI {f.get('id')}")

        if promoted_lines or triage["archive"] or episoded:
            lines = []
            if promoted_lines:
                lines.append("🧠 <b>Filed into the brain this week</b>\n")
                lines += [f"• {escape(l[:180])}" for l in promoted_lines]
            if episoded:
                lines.append(f"\n📖 {episoded} still-relevant note{'s' if episoded != 1 else ''} kept as dated episodes.")
            if triage["archive"]:
                lines.append(f"\n🗑 {len(triage['archive'])} expired update{'s' if len(triage['archive']) != 1 else ''} archived quietly.")
            overflow = len(triage["ask"]) - 3
            if overflow > 0:
                lines.append(f"\n🗂 {overflow} more expiring note{'s' if overflow != 1 else ''} still waiting — they'll resurface next Sunday.")
            text = "\n".join(lines)
            for uid in ALLOWED_IDS:
                try:
                    await context.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
                except Exception:
                    logger.exception(f"graduation summary send failed for {uid}")

        for f in triage["ask"][:3]:
            when = (f.get("created_at") or "")[:10]
            cat = f.get("category") or "misc"
            fyi_id = f["id"]
            text = (
                f"🗂 <b>FYI check-in</b>\n\n"
                f"<i>{when} · {escape(cat)}</i>\n\n"
                f"{escape(f['content'])}\n\n"
                f"<i>This note is 3 weeks old. Worth keeping?</i>"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Keep 30 more days", callback_data=f"fyi_keep:{fyi_id}")],
                [
                    InlineKeyboardButton("🧠 Promote to Brain", callback_data=f"fyi_promote:{fyi_id}"),
                    InlineKeyboardButton("🗑 Archive", callback_data=f"fyi_archive:{fyi_id}"),
                ],
            ])
            for uid in ALLOWED_IDS:
                await context.bot.send_message(
                    chat_id=uid, text=text, parse_mode="HTML", reply_markup=keyboard
                )
    except Exception:
        logger.exception("send_fyi_graduation failed")


async def _handle_fyi_callback(query, context, data: str):
    """Handle FYI button responses — graduation (keep/promote/archive) and partner ack/save."""
    chat_id = query.message.chat_id
    if ":" not in data:
        return
    parts = data.split(":", 2)
    action = parts[0]

    # --- Expand: open a focused card for one FYI ---
    if action == "expand":
        fyi_id = parts[1]
        from tools.fyis import get_fyi_by_id
        fyi = get_fyi_by_id(fyi_id)
        if not fyi:
            await query.answer("FYI not found.")
            return
        await query.answer()
        when = (fyi.get("created_at") or "")[:10]
        cat = (fyi.get("category") or "misc").lower()
        cat_emoji = _CAT_EMOJI.get(cat, "📌")
        text = (
            f"{cat_emoji} <b>{cat.title()}</b>\n"
            f"<i>{when}</i>\n\n"
            f"{escape(fyi['content'])}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Got it", callback_data=f"fyi_gotit:{fyi_id}"),
             InlineKeyboardButton("📌 Keep 30d", callback_data=f"fyi_keep:{fyi_id}")],
            [InlineKeyboardButton("🧠 → Brain", callback_data=f"fyi_promote:{fyi_id}"),
             InlineKeyboardButton("🗑 Archive", callback_data=f"fyi_archive:{fyi_id}")],
        ])
        await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard
        )
        return

    # --- Per-FYI checkoff ---
    if action == "gotit":
        fyi_id = parts[1]
        uid = query.from_user.id
        ack_fyi(fyi_id, uid)
        await query.answer("✅ Got it!")
        try:
            current = query.message.reply_markup
            if current:
                cb = f"fyi_gotit:{fyi_id}"
                new_rows = [row for row in current.inline_keyboard if not any(btn.callback_data == cb for btn in row)]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows) if new_rows else None)
        except Exception:
            logger.exception("fyi_gotit button removal failed")
        return

    # --- Graduation actions (fyi_keep / fyi_promote / fyi_archive) ---
    if action in ("keep", "promote", "archive"):
        fyi_id = parts[1]
        if action == "keep":
            keep_fyi(fyi_id)
            await query.edit_message_reply_markup(reply_markup=None)
            await context.bot.send_message(chat_id=chat_id, text="📌 Kept for another 30 days.")
        elif action == "promote":
            row = promote_fyi(fyi_id)
            if row and row.get("content"):
                try:
                    from tools.user_memory import normalize_domain
                    append_shared_summary(
                        row["content"],
                        domain=normalize_domain(row.get("category")),
                        source="fyi_promote",
                    )
                except Exception:
                    pass
            await query.edit_message_reply_markup(reply_markup=None)
            await context.bot.send_message(chat_id=chat_id, text="🧠 Promoted to Shared Brain.")
        elif action == "archive":
            archive_fyi(fyi_id)
            await query.edit_message_reply_markup(reply_markup=None)
            await context.bot.send_message(chat_id=chat_id, text="🗑 Archived.")

    # --- Partner ack / save ---
    elif action in ("ack", "save") and len(parts) >= 3:
        try:
            sender_id = int(parts[1])
            receiver_id = int(parts[2])
        except ValueError:
            return

        _USER_NAMES = {63756531: "Ansen", 6927468999: "Jess"}
        receiver_name = _USER_NAMES.get(receiver_id, "Your partner")
        await query.edit_message_reply_markup(reply_markup=None)

        if action == "ack":
            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ Acknowledged.",
            )
            await context.bot.send_message(
                chat_id=sender_id,
                text=f"✅ <b>{receiver_name}</b> acknowledged your FYI.",
                parse_mode="HTML",
            )

        elif action == "save":
            # Extract original content from the notification message text
            msg_text = query.message.text or ""
            # Format is "📨 Name: [content]\n\n[analysis]" — take first block
            content = msg_text.split("\n\n")[0]
            # Strip the "📨 Name: " prefix
            if ": " in content:
                content = content.split(": ", 1)[1]

            # Legacy button from old messages — the content is already in the
            # brain as an episode, so this is an ack, not a save.
            await context.bot.send_message(
                chat_id=chat_id,
                text="🧠 Already in the shared brain — nothing extra to save.",
            )


def _execute_check_in_action(ci: dict, opt: dict, user_id: int) -> str:
    """Run the tapped option's action. Returns a short confirmation for the card."""
    from datetime import datetime as _dt, date as _date, time as _time, timedelta as _td
    action = opt.get("action", "save_decision")
    payload = opt.get("payload") or {}
    question, label = ci["question"], opt["label"]
    decision = payload.get("decision") or f"{question} → {label}"
    # Per-option category override lets a "which drawer?" card file the SAME
    # content to whichever domain the user picks (screenshot routing).
    category = payload.get("category") or ci.get("category", "life")

    if action == "dismiss":
        return "dropped, nothing saved"

    notes = []
    if action in ("save_decision", "capture"):
        try:
            from tools.user_memory import normalize_domain
            append_shared_summary(decision, domain=normalize_domain(category), source="check_in")
            notes.append("saved to shared brain")
        except Exception:
            logger.exception("check-in decision save failed")
        if category == "baby":
            try:
                from tools.baby_knowledge import save_entry
                save_entry(summary=decision, tags=["decision"], user_id=user_id, source="check_in")
                notes.append("+ baby brain")
            except Exception:
                logger.exception("check-in baby brain save failed")
        # Loop closure — a resolving decision closes the open tasks it settles
        # (confirming Hyatt closes 'Book Amsterdam' + 'Log conf#'), so the same
        # question stops regenerating every day.
        try:
            from tools.daily import close_tasks_matching
            closed = close_tasks_matching(question, acting_user_id=user_id)
            if closed:
                notes.append(f"closed {len(closed)} related task{'s' if len(closed) != 1 else ''}")
        except Exception:
            logger.exception("check-in loop closure failed")
    elif action == "create_task":
        try:
            task_text = payload.get("task") or label
            from tools.daily import find_duplicate_open_task
            if find_duplicate_open_task(task_text):
                notes.append("already on your list")
            else:
                due = None
                if payload.get("due_date"):
                    try:
                        due = _date.fromisoformat(payload["due_date"])
                    except ValueError:
                        pass
                add_task(user_id=user_id, task=task_text, due_date=due,
                         visibility="shared", category=category)
                notes.append("task created")
        except Exception:
            logger.exception("check-in task creation failed")
    elif action == "remind":
        try:
            from tools.notifications import schedule_notification as _sched
            when = None
            if payload.get("due_date"):
                try:
                    when = _dt.combine(_date.fromisoformat(payload["due_date"]), _time(hour=9), tzinfo=REMINDER_TIMEZONE)
                except ValueError:
                    pass
            if when is None:
                when = (_dt.now(REMINDER_TIMEZONE) + _td(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            _sched(user_id, payload.get("message") or question, when)
            notes.append(f"reminder set for {when.strftime('%-d %b')}")
        except Exception:
            logger.exception("check-in reminder failed")
    return " · ".join(notes) or "noted"


_CAPTURE_SKIP = {"skip", "nothing", "none", "no", "nope", "later", "idk",
                 "not yet", "na", "n/a", "don't have it", "dont have it",
                 "don't have", "dont have", "no conf", "no number", "cancel"}


async def _try_capture_reply(update, context, user_id: int, text: str) -> bool:
    """If a value-capture is pending for this user, treat this message as the
    value (or a skip). Returns True if handled — caller then stops. Bails out
    (returns False) if the message clearly isn't the value, so real messages
    are never hijacked."""
    from tools.check_ins import get_pending_capture, clear_pending_capture
    pending = await asyncio.to_thread(get_pending_capture, user_id)
    if not pending:
        return False
    txt = text.strip()
    low = txt.lower().rstrip(".!")

    is_skip = (
        low in _CAPTURE_SKIP
        or any(low.startswith(s + " ") or low == s for s in _CAPTURE_SKIP)
        # short message containing a multi-word skip phrase ("i don't have it")
        or (len(low) <= 30 and any(" " in s and s in low for s in _CAPTURE_SKIP))
    )
    if is_skip:
        await asyncio.to_thread(clear_pending_capture, user_id)
        await update.effective_message.reply_text("👍 Marked done — I'll stop asking. Tell me the number whenever you have it.")
        return True

    # Looks like a real value: short, not a question, not a new command/request.
    # Bail if it reads like a fresh request/question (even without a '?') so a
    # real message never gets silently written to the brain as "the value".
    _REQUESTY = ("what", "when", "where", "who", "why", "how", "can you", "could you",
                 "should", "is ", "are ", "do you", "does ", "add ", "remind", "book ",
                 "tell ", "show ", "give ", "help", "please", "let's", "lets ")
    looks_requesty = any(low.startswith(p) for p in _REQUESTY)
    if len(txt) <= 80 and "?" not in txt and not txt.startswith("/") and not looks_requesty:
        try:
            from tools.user_memory import normalize_domain
            await asyncio.to_thread(
                append_shared_summary, f"{pending['subject']} — {txt}",
                normalize_domain(pending.get("category", "life")), "capture",
            )
        except Exception:
            logger.exception("capture value save failed")
        await asyncio.to_thread(clear_pending_capture, user_id)
        await update.effective_message.reply_text(f"✅ Logged: <b>{escape(txt)}</b>. Closed the related tasks.", parse_mode="HTML")
        return True

    # Message doesn't look like the value — they moved on. Drop the capture and
    # let the normal agent handle whatever they actually said.
    await asyncio.to_thread(clear_pending_capture, user_id)
    return False


async def _handle_check_in_callback(query, context, data: str):
    """ci:{id}:{option_index} answers a check-in; cisnz:{id} snoozes it."""
    user_id = query.from_user.id

    if data.startswith("cisnz:"):
        snooze_check_in(data[6:], days=3)
        await query.answer("💤 I'll ask again in a few days.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    parts = data.split(":", 2)
    if len(parts) < 3:
        return
    ci_id, idx_str = parts[1], parts[2]
    ci = get_check_in(ci_id)
    if not ci:
        await query.answer("This question is gone.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    try:
        opt = (ci.get("options") or [])[int(idx_str)]
    except (ValueError, IndexError):
        await query.answer("Unknown option.")
        return

    # Atomic claim — with audience='both', only the first tap wins
    if opt.get("action") == "dismiss":
        claimed = dismiss_check_in(ci_id, user_id)
    else:
        claimed = answer_check_in(ci_id, opt["label"], user_id)
    if not claimed:
        await query.answer("Already answered.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    confirmation = await asyncio.to_thread(_execute_check_in_action, ci, opt, user_id)
    await query.answer("✅")

    answerer = query.from_user.first_name or "Answered"
    card_text, _ = _check_in_card(ci)
    try:
        await query.edit_message_text(
            f"{card_text}\n\n✅ <b>{escape(answerer)}</b>: {escape(opt['label'])} — <i>{escape(confirmation)}</i>",
            parse_mode="HTML",
        )
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    # Value capture — the button confirmed the loop; now ask for the actual value
    # (conf#, ref number). Their next message is caught in _process_message.
    if opt.get("action") == "capture":
        payload = opt.get("payload") or {}
        prompt = payload.get("capture_prompt") or "Send it over and I'll log it — or reply “skip” and I'll just mark it done."
        try:
            from tools.check_ins import set_pending_capture
            await asyncio.to_thread(set_pending_capture, user_id, ci_id, prompt,
                                    ci["question"], ci.get("category", "life"))
            await context.bot.send_message(chat_id=query.message.chat_id,
                                           text=f"📝 {escape(prompt)}", parse_mode="HTML")
        except Exception:
            logger.exception("pending capture setup failed")

    if ci.get("audience") == "both":
        for uid in ALLOWED_IDS:
            if uid != user_id:
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=f"✅ <b>{escape(answerer)}</b> answered: <i>{escape(ci['question'])}</i> → {escape(opt['label'])}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass


# In-memory cache: Ansen's latest skill gaps for build buttons (keyed by user_id)
_skills_gaps: dict[int, list[dict]] = {}


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("Loading goals...")
    try:
        text, button_steps = await agent.goals_brief()
        rows = [[InlineKeyboardButton(f"✅ {s['label']}", callback_data=f"goal_step:{s['id']}")] for s in button_steps]
        keyboard = InlineKeyboardMarkup(rows) if rows else None
        await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.exception("cmd_goals failed")
        await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ansen-only: self-audit bot capabilities and propose new integrations."""
    if not allowed(update):
        return
    if update.effective_user.id != ANSEN_ID:
        await update.message.reply_text("This command is for Ansen only.")
        return
    msg = await update.message.reply_text("🔍 Auditing my capabilities and researching what I'm missing...")
    try:
        result = await agent.capability_gap_sweep()
        text = result.get("text", "⚠️ No output.")
        gaps = result.get("gaps", [])

        # Cache gaps so build callbacks can retrieve them
        _skills_gaps[ANSEN_ID] = gaps

        # Build one button per gap
        rows = []
        for i, g in enumerate(gaps[:5]):
            label = (g.get("gap") or "")[:38]
            rows.append([InlineKeyboardButton(f"📋 Draft plan: {label}", callback_data=f"skill_build:{i}")])
        keyboard = InlineKeyboardMarkup(rows) if rows else None

        sections = _split_sections(text)
        await _safe_send(msg, sections[0])
        for section in sections[1:]:
            await update.message.reply_text(section, parse_mode="HTML")
        if keyboard:
            await update.message.reply_text("Tap one to generate the implementation code:", reply_markup=keyboard)
    except Exception as e:
        logger.exception("cmd_skills failed")
        await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Natural language search across all knowledge stores — shared brain, baby brain, FYIs, personal."""
    if not allowed(update):
        return
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text(
            "What do you want to find? Try:\n"
            "/search confinement nanny options\n"
            "/search Dr Joycelyn appointment\n"
            "/search babymoon timing\n"
            "/search venue deposit"
        )
        return
    msg = await update.message.reply_text("🔍 Searching...")
    try:
        result = await agent.brain_search(query, update.effective_user.id)
        sections = _split_sections(result)
        await _safe_send(msg, sections[0])
        for section in sections[1:]:
            await update.message.reply_text(section, parse_mode="HTML")
    except Exception as e:
        logger.exception("cmd_search failed")
        await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:200]}")


async def cmd_compress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ansen-only: compress the shared brain — merge related entries, remove stale ones."""
    if not allowed(update):
        return
    if update.effective_user.id != ANSEN_ID:
        return
    msg = await update.message.reply_text("🧠 Compressing shared brain...")
    try:
        result = await agent.compress_shared_brain()
        await msg.edit_text(result, parse_mode="HTML")
    except Exception as e:
        logger.exception("cmd_compress failed")
        await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:200]}")


async def cmd_build(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ansen-only: describe a feature and get full implementation code back in chat."""
    if not allowed(update):
        return
    if update.effective_user.id != ANSEN_ID:
        return
    request = " ".join(context.args) if context.args else ""
    if not request.strip():
        await update.message.reply_text("Usage: /build <what to build>\nExample: /build weather tool for trip pre-briefs")
        return
    msg = await update.message.reply_text("📋 Drafting implementation plan...")
    try:
        code = await agent.developer_build(request)
        sections = _split_sections(code)
        await _safe_send(msg, sections[0])
        for section in sections[1:]:
            try:
                await update.message.reply_text(section, parse_mode="HTML")
            except Exception:
                import re as _re
                await update.message.reply_text(_re.sub(r"<[^>]+>", "", section))
        await update.message.reply_text("💡 <i>This plan lives in chat only — paste it to Claude Code to actually deploy it.</i>", parse_mode="HTML")
    except Exception as e:
        logger.exception("cmd_build failed")
        await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def send_capability_gap_sweep(context: ContextTypes.DEFAULT_TYPE):
    """Monthly: bot self-audits and proposes new integrations to Ansen."""
    try:
        result = await agent.capability_gap_sweep()
        text = result.get("text", "")
        gaps = result.get("gaps", [])
        _skills_gaps[ANSEN_ID] = gaps

        rows = []
        for i, g in enumerate(gaps[:5]):
            label = (g.get("gap") or "")[:38]
            rows.append([InlineKeyboardButton(f"📋 Draft plan: {label}", callback_data=f"skill_build:{i}")])
        keyboard = InlineKeyboardMarkup(rows) if rows else None

        sections = _split_sections(text)
        for section in sections:
            await _send_or_alert(context, ANSEN_ID, section, "capability_gap_sweep")
        if keyboard:
            await context.bot.send_message(
                chat_id=ANSEN_ID,
                text="Tap one to generate the implementation code:",
                reply_markup=keyboard,
            )
    except Exception:
        logger.exception("send_capability_gap_sweep failed")


async def send_brain_compress(context: ContextTypes.DEFAULT_TYPE):
    """Monthly: compress shared brain — merge related entries, remove stale ones."""
    try:
        result = await agent.compress_shared_brain()
        await _send_or_alert(context, ANSEN_ID, f"🧠 <b>Brain compression</b>\n\n{result}", "brain_compress")
    except Exception:
        logger.exception("send_brain_compress failed")


async def send_self_audit(context: ContextTypes.DEFAULT_TYPE):
    """Weekly memory self-audit — the bot checks its own invariants.

    Three memory regressions shipped to this bot and all three were found by
    Ansen noticing bad answers weeks later, because nothing silently invisible
    ever raises. A guard rail for the first one existed (sweep_recall.py, plus a
    QA checklist line saying to run it) and had been failing for weeks with
    nobody running it.

    So this runs itself, before Monday's brief is built on the memory it checks,
    and messages ONLY on failure. A quiet Monday means the invariants hold.
    """
    try:
        import self_audit
        results = await asyncio.to_thread(self_audit.run_all)
        report = self_audit.telegram_report(results)
        if report:
            await _send_or_alert(context, ANSEN_ID, report, "self_audit")
        else:
            logger.info("self-audit: all %d invariants hold", len(results))
    except Exception:
        # A silent audit failure is the exact failure mode this exists to end.
        logger.exception("send_self_audit failed")
        try:
            await context.bot.send_message(
                chat_id=ANSEN_ID,
                text="⚠️ The weekly memory self-audit could not run. Worth a look "
                     "in the Railway logs — while it is down, nothing is watching "
                     "the vault.",
            )
        except Exception:
            logger.exception("self-audit failure alert could not be sent")


async def send_knowledge_sweep(context: ContextTypes.DEFAULT_TYPE):
    """Weekly 3-phase maker-checker knowledge sweep."""
    if not ALLOWED_IDS:
        return
    try:
        result = await agent.knowledge_sweep()
        approved = result.get("approved", {})
        rejected_count = result.get("rejected_count", 0)
        if result.get("verifier_failed"):
            await context.bot.send_message(
                chat_id=ANSEN_ID,
                text="⚠️ Weekly brain sweep: the fact verifier errored, so nothing was written this run. Worth a look in the Railway logs.",
            )
            return
        if not approved:
            return
        _CAT_HEADERS = {
            "baby":    "🍼 <b>Baby</b>",
            "wedding": "💍 <b>Wedding</b>",
            "travel":  "✈️ <b>Travel</b>",
            "money":   "💰 <b>Money</b>",
            "life":    "🌿 <b>Life</b>",
        }
        total = sum(len(v) for v in approved.values())
        lines = [f"🧠 <b>Weekly brain update</b>  ·  {total} added, {rejected_count} filtered\n"]
        for cat, facts in approved.items():
            if not facts:
                continue
            header = _CAT_HEADERS.get(cat, f"📌 <b>{cat.title()}</b>")
            lines.append(header)
            for f in facts:
                lines.append(f"• {f}")
            lines.append("")
        text = "\n".join(lines).rstrip()
        for uid in ALLOWED_IDS:
            await _send_or_alert(context, uid, text, "knowledge_sweep")
    except Exception:
        logger.exception("send_knowledge_sweep failed")


async def send_trip_milestones(context: ContextTypes.DEFAULT_TYPE):
    """Daily check — fire pre-trip intelligence briefs at milestone days before departure."""
    from tools.trips import get_upcoming_trips
    from datetime import datetime as _datetime, date as _date

    if not ALLOWED_IDS:
        return

    today = _datetime.now(REMINDER_TIMEZONE).date()
    try:
        trips = get_upcoming_trips()
    except Exception:
        logger.exception("send_trip_milestones: failed to fetch trips")
        return

    for trip in trips:
        start_str = trip.get("start_date")
        if not start_str:
            continue
        try:
            departure = _date.fromisoformat(start_str)
        except ValueError:
            continue
        days_until = (departure - today).days
        if days_until not in TRIP_MILESTONES:
            continue
        try:
            msg = await agent.trip_milestone_brief(trip, days_until)
            if msg:
                for uid in ALLOWED_IDS:
                    await _send_or_alert(context, uid, msg, "trip_milestones")
        except Exception:
            logger.exception(f"trip_milestone_brief failed for {trip.get('destination')}")


async def send_appointment_prebrief(context: ContextTypes.DEFAULT_TYPE):
    """Nightly check — synthesise pre-brief for tomorrow's medical/health appointments."""
    from tools.gcal import get_events as _get_events
    from datetime import datetime as _datetime, timedelta as _td

    if not ALLOWED_IDS:
        return

    tomorrow_str = (_datetime.now(REMINDER_TIMEZONE).date() + _td(days=1)).isoformat()
    try:
        all_events = await asyncio.to_thread(_get_events, 2)
        tomorrow_events = [e for e in all_events if (e.get("start") or "").startswith(tomorrow_str)]
    except Exception:
        logger.exception("send_appointment_prebrief: failed to fetch calendar")
        return

    if not tomorrow_events:
        return

    medical_events = [
        e for e in tomorrow_events
        if any(kw in (e.get("title") or "").lower() for kw in APPOINTMENT_KEYWORDS)
    ]
    if not medical_events:
        return

    try:
        from tools.loop_state import COUPLE, load_state as _load_loop, save_state as _save_loop
        _prev = (await asyncio.to_thread(_load_loop, "appointment_prebrief", COUPLE)).get("last_output") or ""
        msg = await agent.appointment_pre_brief(medical_events, already_sent=_prev)
        if msg:
            names = ", ".join(e.get("title", "appointment") for e in medical_events)
            header = f"📅 <b>Tomorrow: {names}</b>\n\n"
            _today = _datetime.now(REMINDER_TIMEZONE).date().isoformat()
            await asyncio.to_thread(
                _save_loop, "appointment_prebrief", COUPLE, f"{names} ({tomorrow_str}):\n{msg}", _today
            )
            for uid in ALLOWED_IDS:
                await _send_or_alert(context, uid, header + msg, "appointment_prebrief")
    except Exception:
        logger.exception("send_appointment_prebrief: synthesis failed")


def main():
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())

    if not os.getenv("RAILWAY_ENVIRONMENT"):
        print("Not running on Railway. Exiting.")
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    async def post_init(application: Application) -> None:
        commands = [
            BotCommand("start", "Help & intro"),
            BotCommand("wedding", "💒 Wedding planning"),
            BotCommand("shared", "🧠 Shared brain, tasks & reminders"),
            BotCommand("baby", "👶 Baby & pregnancy"),
            BotCommand("stocks", "📊 Stocks & crypto brief"),
            BotCommand("finances", "💼 Portfolio & money picture"),
            BotCommand("me", "👤 My personal tasks"),
            BotCommand("notifications", "🔔 Scheduled reminders — view & turn off"),
        ]
        await application.bot.set_my_commands(commands)

    app = Application.builder().token(token).post_init(post_init).build()

    # Fire missed jobs within 1 hour — survives Railway restarts mid-schedule
    if app.job_queue:
        app.job_queue.scheduler.configure(
            job_defaults={"misfire_grace_time": 3600}
        )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("wedding", cmd_wedding))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("commands", cmd_commands))
    app.add_handler(CommandHandler("bringmeuptospeed", cmd_bringmeuptospeed))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler(["notifications", "notifs", "alerts"], cmd_notifications))
    app.add_handler(CommandHandler("shared", cmd_shared_parent))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("testnotify", cmd_testnotify))
    app.add_handler(CommandHandler("stocks", cmd_stocks))
    app.add_handler(CommandHandler("finances", cmd_finances))
    app.add_handler(CommandHandler("baby", cmd_baby))
    app.add_handler(CommandHandler("babyknowledge", cmd_babyknowledge))
    app.add_handler(CommandHandler("shows", cmd_shows))
    app.add_handler(CommandHandler("groceries", cmd_groceries))
    app.add_handler(CommandHandler("goals", cmd_goals))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("build", cmd_build))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("compress", cmd_compress))

    for key in CATEGORIES:
        app.add_handler(CommandHandler(key, cmd_category_status))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VOICE | filters.Document.ALL, handle_message))

    if app.job_queue is not None:
        # ── PRE-MORNING 8:50am ───────────────────────────────────────
        # Calendar reconciliation — sync task due_dates to match calendar changes
        app.job_queue.run_daily(send_calendar_reconciliation, time=CAL_SYNC_TIME)
        # ── MORNING 9am ──────────────────────────────────────────────
        # The ONE daily driver: action-first brief + decision cards + icebox
        # offers per user (absorbed the old nightly wrap)
        app.job_queue.run_daily(send_morning_brief, time=REMINDER_TIME)
        # Baby weekly update — every Monday
        app.job_queue.run_daily(send_baby_weekly, time=BABY_WEEKLY_TIME, days=(0,))
        # Jess's daily pregnancy companion — her own interactive check-in (10am)
        app.job_queue.run_daily(send_jess_checkin, time=JESS_CHECKIN_TIME)
        # Show reminders — 7 days out, Ansen only
        app.job_queue.run_daily(send_show_reminders, time=REMINDER_TIME)
        # FYI graduation — every Sunday (surface notes nearing 30-day expiry)
        app.job_queue.run_daily(send_fyi_graduation, time=REMINDER_TIME, days=(6,))
        # Wedding brief — every Sunday morning
        app.job_queue.run_daily(send_priority_brief, time=REMINDER_TIME, days=(6,))
        # Trip milestone briefs — fires at 56/28/14/7/2 days before departure
        app.job_queue.run_daily(send_trip_milestones, time=REMINDER_TIME)
        # Appointment pre-brief — nightly check for tomorrow's medical events
        app.job_queue.run_daily(send_appointment_prebrief, time=APPOINTMENT_TIME)
        # ── EVENING 8pm ──────────────────────────────────────────────
        # Stocks & crypto brief — PAUSED at Ansen's request (Jul 2026). The
        # /stocks command still works on demand; re-enable by uncommenting.
        # app.job_queue.run_daily(send_stocks_brief, time=CRYPTO_TIME)
        # ── NIGHT 9pm ────────────────────────────────────────────────
        # Evening nuggets — optional learning only (his r/daddit, hers
        # r/BabyBumps+pregnant). The action-driven brief moved to 9am.
        app.job_queue.run_daily(send_evening_nuggets, time=EVENING_TIME)
        # Knowledge sweep — every Wednesday (extract cross-domain facts into shared brain)
        app.job_queue.run_daily(send_knowledge_sweep, time=EVENING_TIME, days=(2,))

        # Memory invariants, Monday 8:20am — before the 9am brief reads the vault
        app.job_queue.run_daily(send_self_audit, time=SELF_AUDIT_TIME, days=(0,))
        # Capability gap sweep — 1st of each month, Ansen only
        app.job_queue.run_monthly(send_capability_gap_sweep, when=dtime(hour=10, minute=0, tzinfo=REMINDER_TIMEZONE), day=1)
        # Shared brain compression — 15th of each month (merge/dedupe accumulated entries)
        app.job_queue.run_monthly(send_brain_compress, when=dtime(hour=3, minute=0, tzinfo=REMINDER_TIMEZONE), day=15)
        # ── ALWAYS ───────────────────────────────────────────────────
        # Scheduled notification check every 60 seconds
        app.job_queue.run_repeating(check_and_send_notifications, interval=60, first=10)
    else:
        logger.warning("Job queue unavailable — scheduled reminders disabled. Install python-telegram-bot[job-queue].")

    logger.info("Wedding agent starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
