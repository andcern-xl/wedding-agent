import asyncio
import io
import logging
import os
from datetime import time as dtime
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
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ALLOWED_IDS = [int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
agent = UnifiedAgent()
conversations: dict[int, list] = {}
chat_locks: dict[int, asyncio.Lock] = {}


def allowed(update: Update) -> bool:
    return not ALLOWED_IDS or update.effective_user.id in ALLOWED_IDS


async def notify_partner(context: ContextTypes.DEFAULT_TYPE, update: Update, text: str = None, photo_bytes: bytes = None, caption: str = None):
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
            elif text:
                await context.bot.send_message(chat_id=uid, text=f"📨 {sender_name}: {text}")
        except Exception as e:
            logger.error(f"notify_partner failed for uid {uid}: {e}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    lines = [
        "👋 Two brains, one bot.\n",
        "💒 WEDDING BRAIN",
        "Drop notes, screenshots, quotes — anything wedding related. I'll sort it, track it, and keep you both across it.\n",
        "Wedding category shortcuts:",
    ]
    for key, cat in CATEGORIES.items():
        lines.append(f"  {cat['emoji']} /{key}")
    lines.append("\n/bringmeuptospeed — full wedding overview")
    lines.append("/plan — wedding priorities this week\n")
    lines.append("🗓 DAILY BRAIN")
    lines.append("Reminders and tasks for everyday life — personal or shared.")
    lines.append("  • \"remind me to call the dentist Friday\" → private")
    lines.append("  • \"remind us to confirm the caterer Monday\" → shared")
    lines.append("  • \"add a category for Mochi 🐶\" → custom category\n")
    lines.append("/tasks — your daily brief")
    lines.append("/reminders — to-dos for both of you")
    lines.append("/fyis — recent shared FYIs")
    lines.append("/shared — shared brain (confirmed decisions)")
    lines.append("/commands — full command list")
    await update.message.reply_text("\n".join(lines))


_JUNK_PREFIXES = ("fyi", "• fyi", "ansen deposited", "jess deposited", "ansen paid", "jess paid")

def _is_task(t: dict) -> bool:
    raw = (t.get("task") or "").strip().lower()
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
        "<b>Info</b>",
        "/start — intro",
        "/commands — this list\n",
        "<b>Wedding</b>",
        "/bringmeuptospeed — full wedding overview",
        "/plan — priorities this week",
    ]
    for key, cat in CATEGORIES.items():
        lines.append(f"{cat['emoji']} /{key} — {cat['name'].lower()} status")
    lines += [
        "\n<b>Daily</b>",
        "/tasks — daily brief for both of you",
        "/reminders — to-do list split by person",
        "/fyis — recent FYIs from both of you",
        "/shared — what's in the shared brain\n",
        "<b>Debug</b>",
        "/testnotify — check partner notifications are working",
    ]
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
                await notify_partner(context, update, photo_bytes=bytes(photo_bytes), caption=caption)

        else:
            text = update.message.text or ""
            if text.startswith("/"):
                return

            result = await agent.handle_message(text=text, user_id=user_id, history=history)

            if result.get("notify_partner"):
                await notify_partner(context, update, text=text)

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


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    user_id = query.from_user.id
    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        await query.answer("Not authorised.")
        return

    action, _, payload = query.data.partition(":")
    if action == "done":
        try:
            success = complete_task(payload, user_id)
        except Exception:
            await query.answer("Couldn't mark done — try again.")
            return
        if not success:
            await query.answer("That's not your task to mark done.")
            return
        # Instant feedback: answer the toast and drop the button immediately — no LLM call
        await query.answer("✅ Done!")
        try:
            current = query.message.reply_markup
            if current:
                new_rows = [
                    row for row in current.inline_keyboard
                    if not any(btn.callback_data == query.data for btn in row)
                ]
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup(new_rows) if new_rows else None
                )
        except Exception:
            logger.exception("handle_callback button removal failed")
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
            BotCommand("start", "Intro and help"),
            BotCommand("commands", "Full command list"),
            BotCommand("bringmeuptospeed", "Full wedding overview"),
            BotCommand("plan", "Wedding priorities this week"),
            BotCommand("tasks", "Daily brief for both"),
            BotCommand("reminders", "To-dos split by person"),
            BotCommand("fyis", "Recent shared FYIs"),
            BotCommand("shared", "Shared brain — confirmed decisions"),
        ]
        for key, cat in CATEGORIES.items():
            commands.append(BotCommand(key, f"{cat['emoji']} {cat['name']} status"))
        await application.bot.set_my_commands(commands)

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("commands", cmd_commands))
    app.add_handler(CommandHandler("bringmeuptospeed", cmd_bringmeuptospeed))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("shared", cmd_shared))
    app.add_handler(CommandHandler("fyis", cmd_fyis))
    app.add_handler(CommandHandler("testnotify", cmd_testnotify))

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
        # Check for scheduled notifications every 60 seconds
        app.job_queue.run_repeating(check_and_send_notifications, interval=60, first=10)
    else:
        logger.warning("Job queue unavailable — scheduled reminders disabled. Install python-telegram-bot[job-queue].")

    logger.info("Wedding agent starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
