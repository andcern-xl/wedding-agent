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
from tools.notifications import get_pending_notifications, mark_notification_sent
from tools.user_memory import get_shared_summary, append_shared_summary
from tools.fyis import get_fyis, get_fyis_expiring, keep_fyi, promote_fyi, archive_fyi, ack_fyi, get_fyis_unacked
from tools.conversation import load_history, save_history
from tools.daily import complete_task
from tools.shows import get_upcoming_shows, get_shows_in_n_days, get_show_by_id, mark_calendar_added as mark_show_calendar_added, delete_show as _delete_show_by_id

ANSEN_ID = 63756531

load_dotenv()

try:
    REMINDER_TIMEZONE = ZoneInfo(os.getenv("REMINDER_TZ", "Asia/Singapore"))
except Exception:
    REMINDER_TIMEZONE = ZoneInfo("UTC")
REMINDER_TIME   = dtime(hour=9,  minute=0, tzinfo=REMINDER_TIMEZONE)   # 9am  — tasks, FYIs, baby, shows
_evening_hour   = int(os.getenv("EVENING_BRIEF_HOUR", "21"))
EVENING_TIME    = dtime(hour=_evening_hour, minute=0, tzinfo=REMINDER_TIMEZONE)  # 9pm — recap, knowledge sweep
_proactive_hour = int(os.getenv("PROACTIVE_HOUR", "14"))
PROACTIVE_TIME  = dtime(hour=_proactive_hour, minute=0, tzinfo=REMINDER_TIMEZONE)  # 2pm — proactive intelligence
CRYPTO_TIME     = dtime(hour=20, minute=0, tzinfo=REMINDER_TIMEZONE)               # 8pm — stocks & crypto
BABY_WEEKLY_TIME = dtime(hour=9, minute=0, tzinfo=REMINDER_TIMEZONE)
APPOINTMENT_TIME = dtime(hour=21, minute=0, tzinfo=REMINDER_TIMEZONE)  # 9pm — appointment pre-brief for tomorrow

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
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Got it", callback_data=f"fyi_ack:{sender_id}:{uid}"),
                        InlineKeyboardButton("📌 Save to my FYIs", callback_data=f"fyi_save:{sender_id}:{uid}"),
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
        "/shared — tasks, reminders, FYIs, shared brain",
        "/baby — pregnancy updates, milestones, knowledge base",
        "/stocks — newsletter digest + buy/hold/skip",
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
         InlineKeyboardButton("📨 FYIs", callback_data="shared_fyis")],
        [InlineKeyboardButton("✅ Tasks", callback_data="shared_tasks"),
         InlineKeyboardButton("⏰ Reminders", callback_data="shared_reminders")],
        [InlineKeyboardButton("💰 Budget", callback_data="shared_budget"),
         InlineKeyboardButton("✈️ Travel", callback_data="shared_travel")],
    ])


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

def _reminders_keyboard(tasks: list[dict], user_id: int) -> InlineKeyboardMarkup | None:
    mine = [t for t in tasks if _is_task(t) and _can_complete(t, user_id)]
    rows = []
    for t in mine[:12]:
        raw = (t.get("task") or "").strip()
        if raw.upper().startswith("TASK:"):
            raw = raw[5:].strip()
        label = raw[:35] + "…" if len(raw) > 35 else raw
        rows.append([InlineKeyboardButton(f"✅ {label}", callback_data=f"done:{t['id']}")])
    return InlineKeyboardMarkup(rows) if rows else None


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
        "💒 /wedding — catch up, plan, tasks, reminders, shared, FYIs, categories",
        "👶 /baby — weekly brief, knowledge base, milestones",
        "📊 /stocks — newsletter digest + buy/hold/skip\n",
        "<b>Shortcuts</b>",
        "/bringmeuptospeed — full wedding overview",
        "/plan /tasks /reminders /shared /fyis",
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


async def _process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int):
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    # Load from Supabase if this is the first message after a restart
    if chat_id not in conversations:
        conversations[chat_id] = load_history(chat_id)
    history = conversations[chat_id]

    try:
        if update.message.photo:
            photo = update.message.photo[-1]
            photo_file = await photo.get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            caption = update.message.caption or ""

            result = await agent.handle_image(image_bytes=bytes(photo_bytes), caption=caption, user_id=user_id, history=history)
            if result.get("notify_partner"):
                await notify_partner(context, update, photo_bytes=bytes(photo_bytes), caption=caption, analysis=result.get("text"))

        else:
            text = update.message.text or ""
            if text.startswith("/"):
                return

            # Prepend reply context so the agent knows what the user is referring to
            reply = update.message.reply_to_message
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
        await update.message.reply_text(result["text"], parse_mode="HTML")

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

    except Exception as e:
        logger.exception(f"Error handling message: {e}")
        err_type = type(e).__name__
        err_msg = str(e)[:300]
        await update.message.reply_text(f"[DEBUG] {err_type}: {err_msg}")


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
    if not ALLOWED_IDS:
        return
    try:
        brief = await agent.stocks_brief()
        sections = _split_sections(brief)
        for uid in ALLOWED_IDS:
            await context.bot.send_message(
                chat_id=uid,
                text="📊 <b>Daily Stocks & Crypto Brief</b>",
                parse_mode="HTML",
            )
            for section in sections:
                try:
                    await context.bot.send_message(chat_id=uid, text=section, parse_mode="HTML")
                except Exception:
                    import re as _re
                    await context.bot.send_message(chat_id=uid, text=_re.sub(r"<[^>]+>", "", section))
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
        brief = await agent.baby_brief()
        sections = _split_sections(brief)
        for uid in ALLOWED_IDS:
            for section in sections:
                try:
                    await context.bot.send_message(chat_id=uid, text=section, parse_mode="HTML")
                except Exception:
                    import re as _re
                    await context.bot.send_message(chat_id=uid, text=_re.sub(r"<[^>]+>", "", section))
    except Exception:
        logger.exception("Error sending baby weekly brief")


async def send_priority_brief(context: ContextTypes.DEFAULT_TYPE):
    if not ALLOWED_IDS:
        return
    try:
        brief = await agent.priority_brief()
        sections = _split_sections(brief)
        for uid in ALLOWED_IDS:
            await context.bot.send_message(
                chat_id=uid,
                text="<b>Weekly Planning Check-in</b>",
                parse_mode="HTML",
            )
            for section in sections:
                await context.bot.send_message(chat_id=uid, text=section, parse_mode="HTML")
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


async def cmd_shared(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        summary = get_shared_summary()
        if not summary.strip():
            await update.message.reply_text("Nothing in the shared brain yet. Confirmed decisions will appear here automatically.")
            return
        text = "<b>🧠 Shared Brain</b>\n\n" + summary
        sections = _split_sections(text)
        await update.message.reply_text(sections[0], parse_mode="HTML")
        for section in sections[1:]:
            await update.message.reply_text(section, parse_mode="HTML")
    except Exception as e:
        logger.exception("cmd_shared failed")
        await update.message.reply_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


_CAT_EMOJI = {
    "health": "🏥", "finance": "💰", "personal": "🙋", "home": "🏠",
    "social": "🎉", "travel": "✈️", "food": "🍽️", "baby": "👶",
    "wedding": "💒", "work": "💼",
}


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
            blocks.append(f"• <i>{when}</i> — {f['content']}")
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
    user_id = update.effective_user.id
    try:
        fyis = get_fyis_unacked(user_id, limit=30)
        if not fyis:
            await update.message.reply_text("You're all caught up — no unread FYIs. 🎉")
            return
        text, keyboard = _format_fyis_with_buttons(fyis)
        sections = _split_sections(text)
        await update.message.reply_text(sections[0], parse_mode="HTML", reply_markup=keyboard)
        for section in sections[1:]:
            await update.message.reply_text(section, parse_mode="HTML")
    except Exception as e:
        logger.exception("cmd_fyis failed")
        await update.message.reply_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


async def _handle_shared_callback(query, context, action: str, user_id: int):
    chat_id = query.message.chat_id

    if action == "brain":
        msg = await context.bot.send_message(chat_id=chat_id, text="Synthesising your knowledge base...")
        try:
            text = await agent.brain_synthesis()
            sections = _split_sections(text)
            await msg.edit_text(sections[0], parse_mode="HTML")
            for section in sections[1:]:
                await context.bot.send_message(chat_id=chat_id, text=section, parse_mode="HTML")
        except Exception as e:
            await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif action == "fyis":
        try:
            fyis = get_fyis_unacked(user_id, limit=30)
            if not fyis:
                await context.bot.send_message(chat_id=chat_id, text="You're all caught up — no unread FYIs. 🎉")
                return
            text, keyboard = _format_fyis_with_buttons(fyis)
            sections = _split_sections(text)
            await context.bot.send_message(chat_id=chat_id, text=sections[0], parse_mode="HTML", reply_markup=keyboard)
            for section in sections[1:]:
                await context.bot.send_message(chat_id=chat_id, text=section, parse_mode="HTML")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

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
            count = len(trips)
            lines = [f"✈️ <b>Upcoming Trips</b>  ·  {count}\n"]
            rows = []
            for t in trips:
                dest = t["destination"]
                status = t.get("status") or "planning"
                icon = STATUS_ICON.get(status, "🗓")
                start = t.get("start_date") or ""
                if start:
                    try:
                        start = _dt.strptime(start, "%Y-%m-%d").strftime("%-d %b '%y")
                    except Exception:
                        pass
                date_preview = start or "dates TBC"
                lines.append(f"{icon} <b>{escape(dest)}</b>  ·  {date_preview}")
                label = f"{icon} {dest[:24]}  {date_preview} →"
                rows.append([InlineKeyboardButton(label, callback_data=f"trip_expand:{t['id']}")])
            text = "\n".join(lines)
            keyboard = InlineKeyboardMarkup(rows)
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

    elif data.startswith("shared_"):
        await query.answer()
        await _handle_shared_callback(query, context, data[7:], user_id)

    elif data.startswith("fyi_"):
        await query.answer()
        await _handle_fyi_callback(query, context, data[4:])

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
            from datetime import datetime as _dt2
            t = get_trip_by_id(trip_id)
            if not t:
                await context.bot.send_message(chat_id=query.message.chat_id, text="Trip not found.")
                return
            def _fmt_d(d):
                try:
                    return _dt2.strptime(d, "%Y-%m-%d").strftime("%-d %b %Y")
                except Exception:
                    return d
            STATUS_ICON = {"planning": "🗓", "booked": "✅", "completed": "🏁", "cancelled": "❌"}
            dest = t["destination"]
            status = t.get("status") or "planning"
            icon = STATUS_ICON.get(status, "🗓")
            start_str = _fmt_d(t["start_date"]) if t.get("start_date") else "TBC"
            end_str = _fmt_d(t["end_date"]) if t.get("end_date") else "TBC"
            date_str = f"{start_str} – {end_str}" if t.get("start_date") and t.get("end_date") else start_str
            lines = [f"✈️ <b>{escape(dest)}</b>  {icon} {status.title()}", f"\n📅 {date_str}"]
            va = t.get("visa_ansen")
            vj = t.get("visa_jess")
            if va or vj:
                lines.append(f"🛂 Ansen: {va or '—'}  |  Jess: {vj or '—'}")
            notes = t.get("notes") or ""
            if notes:
                lines.append(f"\n📝 <i>{escape(notes[:600])}</i>")
            text = "\n".join(lines)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Delete trip", callback_data=f"trip_del:{trip_id}")]
            ])
            await context.bot.send_message(
                chat_id=query.message.chat_id, text=text, parse_mode="HTML", reply_markup=keyboard
            )
        except Exception as e:
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"[DEBUG] {type(e).__name__}: {str(e)[:200]}")

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
    """Unified morning brief — narrative prose per user, merges tasks + FYIs + calendar + context."""
    if not ALLOWED_IDS:
        return
    USER_NAMES = {63756531: "Ansen", 6927468999: "Jess"}
    for uid in ALLOWED_IDS:
        name = USER_NAMES.get(uid, "")
        try:
            text = await agent.morning_brief(uid, name)
            await context.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
        except Exception:
            logger.exception(f"send_morning_brief failed for uid {uid}")


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


async def check_and_send_notifications(context: ContextTypes.DEFAULT_TYPE):
    try:
        pending = get_pending_notifications()
    except Exception:
        logger.exception("Error fetching pending notifications")
        return
    for notif in pending:
        try:
            await context.bot.send_message(chat_id=notif["user_id"], text=notif["message"])
            mark_notification_sent(notif["id"])
        except Exception:
            logger.exception(f"Failed to send notification {notif['id']} to {notif['user_id']}")


async def send_evening_brief(context: ContextTypes.DEFAULT_TYPE):
    if not ALLOWED_IDS:
        return
    try:
        user_names: dict[int, str] = {}
        for uid in ALLOWED_IDS:
            try:
                chat = await context.bot.get_chat(uid)
                user_names[uid] = chat.first_name or str(uid)
            except Exception:
                user_names[uid] = str(uid)

        brief = await agent.evening_brief(ALLOWED_IDS, user_names)
        sections = _split_sections(brief)
        for uid in ALLOWED_IDS:
            await context.bot.send_message(chat_id=uid, text="<b>Evening Recap</b>", parse_mode="HTML")
            for section in sections:
                await context.bot.send_message(chat_id=uid, text=section, parse_mode="HTML")
    except Exception:
        logger.exception("Error sending evening brief")


async def send_fyi_graduation(context: ContextTypes.DEFAULT_TYPE):
    """Sunday check-in: surface FYIs nearing expiry for keep/promote/archive."""
    if not ALLOWED_IDS:
        return
    try:
        expiring = get_fyis_expiring(days_threshold=21, limit=10)
        if not expiring:
            return
        for f in expiring:
            when = (f.get("created_at") or "")[:10]
            cat = f.get("category") or "misc"
            fyi_id = f["id"]
            text = (
                f"🗂 <b>FYI check-in</b>\n\n"
                f"<i>{when} · {cat}</i>\n\n"
                f"{f['content']}\n\n"
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
            content = promote_fyi(fyi_id)
            if content:
                try:
                    append_shared_summary(content)
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

            try:
                from tools.fyis import log_fyi as _log_fyi
                _log_fyi(receiver_id, content)
            except Exception:
                pass

            await context.bot.send_message(
                chat_id=chat_id,
                text="📌 Saved to your FYIs.",
            )
            await context.bot.send_message(
                chat_id=sender_id,
                text=f"📌 <b>{receiver_name}</b> saved your FYI to their list.",
                parse_mode="HTML",
            )


async def send_knowledge_sweep(context: ContextTypes.DEFAULT_TYPE):
    """Weekly sweep across all knowledge silos — extracts facts into shared brain."""
    if not ALLOWED_IDS:
        return
    try:
        facts = await agent.knowledge_sweep()
        if not facts:
            return
        lines = ["🧠 <b>Knowledge update</b>\n", "<i>I've added these to our shared brain from this week's conversations:</i>\n"]
        for f in facts:
            lines.append(f"• {f}")
        text = "\n".join(lines)
        for uid in ALLOWED_IDS:
            await context.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
    except Exception:
        logger.exception("send_knowledge_sweep failed")


async def send_proactive_checks(context: ContextTypes.DEFAULT_TYPE):
    if not ALLOWED_IDS:
        return
    USER_NAMES = {63756531: "Ansen", 6927468999: "Jess"}
    for uid in ALLOWED_IDS:
        name = USER_NAMES.get(uid, str(uid))
        try:
            msg = await agent.proactive_check(uid, name)
            if msg:
                await context.bot.send_message(chat_id=uid, text=msg, parse_mode="HTML")
        except Exception:
            logger.exception(f"proactive_check failed for {uid}")


async def send_trip_milestones(context: ContextTypes.DEFAULT_TYPE):
    """Daily check — fire pre-trip intelligence briefs at milestone days before departure."""
    from tools.trips import get_upcoming_trips
    from datetime import date as _date

    if not ALLOWED_IDS:
        return

    today = _date.today()
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
                    await context.bot.send_message(chat_id=uid, text=msg, parse_mode="HTML")
        except Exception:
            logger.exception(f"trip_milestone_brief failed for {trip.get('destination')}")


async def send_appointment_prebrief(context: ContextTypes.DEFAULT_TYPE):
    """Nightly check — synthesise pre-brief for tomorrow's medical/health appointments."""
    from tools.gcal import get_events as _get_events
    from datetime import date as _date, timedelta as _td

    if not ALLOWED_IDS:
        return

    tomorrow_str = (_date.today() + _td(days=1)).isoformat()
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
        msg = await agent.appointment_pre_brief(medical_events)
        if msg:
            names = ", ".join(e.get("title", "appointment") for e in medical_events)
            header = f"📅 <b>Tomorrow: {names}</b>\n\n"
            for uid in ALLOWED_IDS:
                await context.bot.send_message(
                    chat_id=uid, text=header + msg, parse_mode="HTML"
                )
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
            BotCommand("shared", "🧠 Shared tasks, FYIs & brain"),
            BotCommand("baby", "👶 Baby & pregnancy"),
            BotCommand("stocks", "📊 Stocks & crypto brief"),
            BotCommand("me", "👤 My personal tasks"),
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
    app.add_handler(CommandHandler("shared", cmd_shared_parent))
    app.add_handler(CommandHandler("fyis", cmd_fyis))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("testnotify", cmd_testnotify))
    app.add_handler(CommandHandler("stocks", cmd_stocks))
    app.add_handler(CommandHandler("baby", cmd_baby))
    app.add_handler(CommandHandler("babyknowledge", cmd_babyknowledge))
    app.add_handler(CommandHandler("shows", cmd_shows))

    for key in CATEGORIES:
        app.add_handler(CommandHandler(key, cmd_category_status))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))

    if app.job_queue is not None:
        # ── MORNING 9am ──────────────────────────────────────────────
        # Unified narrative morning brief per user (tasks + FYIs + calendar synthesized)
        app.job_queue.run_daily(send_morning_brief, time=REMINDER_TIME)
        # Baby weekly update — every Monday
        app.job_queue.run_daily(send_baby_weekly, time=BABY_WEEKLY_TIME, days=(0,))
        # Show reminders — 7 days out, Ansen only
        app.job_queue.run_daily(send_show_reminders, time=REMINDER_TIME)
        # FYI graduation — every Sunday (surface notes nearing 30-day expiry)
        app.job_queue.run_daily(send_fyi_graduation, time=REMINDER_TIME, days=(6,))
        # Wedding brief — every Sunday morning
        app.job_queue.run_daily(send_priority_brief, time=REMINDER_TIME, days=(6,))
        # ── MIDDAY 2pm ───────────────────────────────────────────────
        # Proactive intelligence check (event-centric, sorted by proximity)
        app.job_queue.run_daily(send_proactive_checks, time=PROACTIVE_TIME)
        # Trip milestone briefs — fires at 56/28/14/7/2 days before departure
        app.job_queue.run_daily(send_trip_milestones, time=REMINDER_TIME)
        # Appointment pre-brief — nightly check for tomorrow's medical events
        app.job_queue.run_daily(send_appointment_prebrief, time=APPOINTMENT_TIME)
        # ── EVENING 8pm ──────────────────────────────────────────────
        # Stocks & crypto brief
        app.job_queue.run_daily(send_stocks_brief, time=CRYPTO_TIME)
        # ── NIGHT 9pm ────────────────────────────────────────────────
        # Evening recap — every day
        app.job_queue.run_daily(send_evening_brief, time=EVENING_TIME)
        # Knowledge sweep — every Wednesday (extract cross-domain facts into shared brain)
        app.job_queue.run_daily(send_knowledge_sweep, time=EVENING_TIME, days=(2,))
        # ── ALWAYS ───────────────────────────────────────────────────
        # Scheduled notification check every 60 seconds
        app.job_queue.run_repeating(check_and_send_notifications, interval=60, first=10)
    else:
        logger.warning("Job queue unavailable — scheduled reminders disabled. Install python-telegram-bot[job-queue].")

    logger.info("Wedding agent starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
