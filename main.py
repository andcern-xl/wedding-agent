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
from tools.user_memory import get_shared_summary
from tools.fyis import get_fyis
from tools.daily import complete_task

load_dotenv()

try:
    REMINDER_TIMEZONE = ZoneInfo(os.getenv("REMINDER_TZ", "Asia/Singapore"))
except Exception:
    REMINDER_TIMEZONE = ZoneInfo("UTC")
REMINDER_TIME = dtime(hour=9, minute=0, tzinfo=REMINDER_TIMEZONE)
_evening_hour = int(os.getenv("EVENING_BRIEF_HOUR", "21"))
EVENING_TIME = dtime(hour=_evening_hour, minute=0, tzinfo=REMINDER_TIMEZONE)
_proactive_hour = int(os.getenv("PROACTIVE_HOUR", "14"))
PROACTIVE_TIME = dtime(hour=_proactive_hour, minute=0, tzinfo=REMINDER_TIMEZONE)
_stocks_hour = int(os.getenv("STOCKS_BRIEF_HOUR", "9"))
STOCKS_TIME = dtime(hour=_stocks_hour, minute=0, tzinfo=REMINDER_TIMEZONE)
BABY_WEEKLY_TIME = dtime(hour=9, minute=0, tzinfo=REMINDER_TIMEZONE)  # Mondays 9am
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ALLOWED_IDS = [int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
agent = UnifiedAgent()
conversations: dict[int, list] = {}
chat_locks: dict[int, asyncio.Lock] = {}


def allowed(update: Update) -> bool:
    return not ALLOWED_IDS or update.effective_user.id in ALLOWED_IDS


async def notify_partner(context: ContextTypes.DEFAULT_TYPE, update: Update, text: str = None, photo_bytes: bytes = None, caption: str = None, analysis: str = None):
    sender_name = update.effective_user.first_name or "Partner"
    partner_ids = [uid for uid in ALLOWED_IDS if uid != update.effective_user.id]
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
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"📨 <b>{sender_name}:</b> {escape(text)}\n\n<i>{analysis}</i>" if analysis else f"📨 {sender_name}: {escape(text)}",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error(f"notify_partner failed for uid {uid}: {e}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    lines = [
        "👋 Two brains, one bot.\n",
        "/wedding — planning, tasks, reminders, categories",
        "/baby — pregnancy updates, milestones, knowledge base",
        "/stocks — newsletter digest + buy/hold/skip",
        "/me — your personal tasks\n",
        "Or just talk — drop a note, screenshot, or question.",
    ]
    await update.message.reply_text("\n".join(lines))


def _wedding_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Catch Up", callback_data="wedding_bringmeuptospeed"),
         InlineKeyboardButton("📅 Plan", callback_data="wedding_plan")],
        [InlineKeyboardButton("✅ Tasks", callback_data="wedding_tasks"),
         InlineKeyboardButton("⏰ Reminders", callback_data="wedding_reminders")],
        [InlineKeyboardButton("🧠 Shared", callback_data="wedding_shared"),
         InlineKeyboardButton("📨 FYIs", callback_data="wedding_fyis")],
        [InlineKeyboardButton("📂 Categories", callback_data="wedding_categories")],
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
    history = conversations.get(chat_id, [])

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

            result = await agent.handle_message(text=text, user_id=user_id, history=history)

            if result.get("notify_partner"):
                await notify_partner(context, update, text=text, analysis=result.get("text"))

        conversations[chat_id] = result.get("history", history)
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


async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or ""
    msg = await update.message.reply_text("Loading your tasks...")
    try:
        text, tasks = await agent.personal_brief(user_id, user_name)
        keyboard = _reminders_keyboard(tasks, user_id)
        await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.exception("cmd_me failed")
        await msg.edit_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


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


async def cmd_fyis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        fyis = get_fyis(limit=20)
        if not fyis:
            await update.message.reply_text("No FYIs yet.")
            return
        lines = ["<b>📨 Recent FYIs</b>\n"]
        for f in fyis:
            when = (f.get("created_at") or "")[:10]
            cat = f.get("category")
            cat_tag = f" [{cat}]" if cat else ""
            lines.append(f"• <i>{when}</i>{cat_tag} — {f['content']}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.exception("cmd_fyis failed")
        await update.message.reply_text(f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


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

    elif action == "shared":
        try:
            summary = get_shared_summary()
            if not summary.strip():
                await context.bot.send_message(chat_id=chat_id, text="Nothing in the shared brain yet.")
                return
            text = "<b>🧠 Shared Brain</b>\n\n" + summary
            sections = _split_sections(text)
            for section in sections:
                await context.bot.send_message(chat_id=chat_id, text=section, parse_mode="HTML")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")

    elif action == "fyis":
        try:
            fyis = get_fyis(limit=20)
            if not fyis:
                await context.bot.send_message(chat_id=chat_id, text="No FYIs yet.")
                return
            lines = ["<b>📨 Recent FYIs</b>\n"]
            for f in fyis:
                when = (f.get("created_at") or "")[:10]
                cat = f.get("category")
                cat_tag = f" [{cat}]" if cat else ""
                lines.append(f"• <i>{when}</i>{cat_tag} — {f['content']}")
            await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"[DEBUG] {type(e).__name__}: {str(e)[:300]}")


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

    else:
        await query.answer()


async def send_daily_brief(context: ContextTypes.DEFAULT_TYPE):
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
            BotCommand("baby", "👶 Baby & pregnancy"),
            BotCommand("stocks", "📊 Stocks & crypto brief"),
            BotCommand("me", "👤 My personal tasks"),
        ]
        await application.bot.set_my_commands(commands)

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("wedding", cmd_wedding))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("commands", cmd_commands))
    app.add_handler(CommandHandler("bringmeuptospeed", cmd_bringmeuptospeed))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("shared", cmd_shared))
    app.add_handler(CommandHandler("fyis", cmd_fyis))
    app.add_handler(CommandHandler("testnotify", cmd_testnotify))
    app.add_handler(CommandHandler("stocks", cmd_stocks))
    app.add_handler(CommandHandler("baby", cmd_baby))
    app.add_handler(CommandHandler("babyknowledge", cmd_babyknowledge))

    for key in CATEGORIES:
        app.add_handler(CommandHandler(key, cmd_category_status))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))

    if app.job_queue is not None:
        # Weekly wedding brief — Mondays at 9am
        app.job_queue.run_daily(send_priority_brief, time=REMINDER_TIME, days=(0,))
        # Daily task brief — every day at 9am
        app.job_queue.run_daily(send_daily_brief, time=REMINDER_TIME)
        # Evening recap — every day at EVENING_BRIEF_HOUR (default 9pm)
        app.job_queue.run_daily(send_evening_brief, time=EVENING_TIME)
        # Proactive intelligence check — daily at PROACTIVE_HOUR (default 2pm)
        app.job_queue.run_daily(send_proactive_checks, time=PROACTIVE_TIME)
        # Daily stocks & crypto brief — every day at STOCKS_BRIEF_HOUR (default 9am)
        app.job_queue.run_daily(send_stocks_brief, time=STOCKS_TIME)
        # Baby weekly update — every Monday at 9am
        app.job_queue.run_daily(send_baby_weekly, time=BABY_WEEKLY_TIME, days=(0,))
        # Check for scheduled notifications every 60 seconds
        app.job_queue.run_repeating(check_and_send_notifications, interval=60, first=10)
    else:
        logger.warning("Job queue unavailable — scheduled reminders disabled. Install python-telegram-bot[job-queue].")

    logger.info("Wedding agent starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
