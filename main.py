import asyncio
import io
import logging
import os
from datetime import time as dtime
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from dotenv import load_dotenv
from agent import UnifiedAgent
from categories import CATEGORIES
from tools.log import drop

load_dotenv()

try:
    REMINDER_TIMEZONE = ZoneInfo(os.getenv("REMINDER_TZ", "Asia/Singapore"))
except Exception:
    REMINDER_TIMEZONE = ZoneInfo("UTC")
REMINDER_TIME = dtime(hour=9, minute=0, tzinfo=REMINDER_TIMEZONE)
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
    await update.message.reply_text("\n".join(lines))


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


async def cmd_bringmeuptospeed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("Pulling everything together...")
    summary = await agent.bring_me_up_to_speed()
    sections = _split_sections(summary)
    await msg.edit_text(sections[0], parse_mode="HTML")
    for section in sections[1:]:
        await update.message.reply_text(section, parse_mode="HTML")


async def cmd_category_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    command = update.message.text[1:].split()[0].lower()
    if command not in CATEGORIES:
        return
    msg = await update.message.reply_text("Checking...")
    status = await agent.category_status(command)
    sections = _split_sections(status)
    await msg.edit_text(sections[0], parse_mode="HTML")
    for section in sections[1:]:
        await update.message.reply_text(section, parse_mode="HTML")


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

            await notify_partner(context, update, photo_bytes=bytes(photo_bytes), caption=caption)
            result = await agent.handle_image(image_bytes=bytes(photo_bytes), caption=caption, user_id=user_id, history=history)
            try:
                log_content = f"[screenshot] {caption + ' — ' if caption else ''}{result['text']}"
                drop(result.get("detected_category"), "image", log_content, user_id)
            except Exception:
                logger.exception("Failed to log image drop")

        else:
            text = update.message.text or ""
            if text.startswith("/"):
                return

            result = await agent.handle_message(text=text, user_id=user_id, history=history)

            if result.get("notify_partner"):
                await notify_partner(context, update, text=text)

        conversations[chat_id] = result.get("history", history)
        await update.message.reply_text(result["text"], parse_mode="HTML")

    except Exception as e:
        logger.exception(f"Error handling message: {e}")
        await update.message.reply_text("Something went wrong, try again.")


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
    brief = await agent.priority_brief()
    sections = _split_sections(brief)
    await msg.edit_text(sections[0], parse_mode="HTML")
    for section in sections[1:]:
        await update.message.reply_text(section, parse_mode="HTML")


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("Checking your tasks...")
    user_names: dict[int, str] = {}
    for uid in ALLOWED_IDS:
        try:
            chat = await context.bot.get_chat(uid)
            user_names[uid] = chat.first_name or str(uid)
        except Exception:
            user_names[uid] = str(uid)
    brief = await agent.combined_daily_brief(ALLOWED_IDS, user_names)
    sections = _split_sections(brief)
    await msg.edit_text(sections[0], parse_mode="HTML")
    for section in sections[1:]:
        await update.message.reply_text(section, parse_mode="HTML")


async def send_daily_brief(context: ContextTypes.DEFAULT_TYPE):
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

        brief = await agent.combined_daily_brief(ALLOWED_IDS, user_names)
        sections = _split_sections(brief)
        for uid in ALLOWED_IDS:
            await context.bot.send_message(chat_id=uid, text="<b>Daily Brief</b>", parse_mode="HTML")
            for section in sections:
                await context.bot.send_message(chat_id=uid, text=section, parse_mode="HTML")
    except Exception:
        logger.exception("Error sending combined daily brief")


def main():
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())

    if not os.getenv("RAILWAY_ENVIRONMENT"):
        print("Not running on Railway. Exiting.")
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("bringmeuptospeed", cmd_bringmeuptospeed))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("testnotify", cmd_testnotify))

    for key in CATEGORIES:
        app.add_handler(CommandHandler(key, cmd_category_status))

    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))

    if app.job_queue is not None:
        # Weekly wedding brief — Mondays at 9am
        app.job_queue.run_daily(send_priority_brief, time=REMINDER_TIME, days=(0,))
        # Daily task brief — every day at 9am
        app.job_queue.run_daily(send_daily_brief, time=REMINDER_TIME)
    else:
        logger.warning("Job queue unavailable — scheduled reminders disabled. Install python-telegram-bot[job-queue].")

    logger.info("Wedding agent starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
