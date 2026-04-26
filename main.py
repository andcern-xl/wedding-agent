import logging
import os
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from dotenv import load_dotenv
from agent import WeddingAgent
from categories import CATEGORIES, detect_category
from tools.log import drop

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ALLOWED_IDS = [int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
agent = WeddingAgent()
conversations: dict[int, list] = {}


def allowed(update: Update) -> bool:
    return not ALLOWED_IDS or update.effective_user.id in ALLOWED_IDS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    lines = [
        "💒 Wedding Agent\n",
        "Just talk — drop notes, screenshots, quotes, anything. I'll figure out what it's about and keep track.\n",
        "CATEGORY SHORTCUTS",
    ]
    for key, cat in CATEGORIES.items():
        lines.append(f"{cat['emoji']} /{key}")
    lines.append("\n/bringmeuptospeed — full planning overview")
    await update.message.reply_text("\n".join(lines))


async def cmd_bringmeuptospeed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("Pulling everything together...")
    summary = await agent.bring_me_up_to_speed()
    await msg.edit_text(summary)


async def cmd_category_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    command = update.message.text[1:].split()[0].lower()
    if command not in CATEGORIES:
        return
    msg = await update.message.reply_text("Checking...")
    status = await agent.category_status(command)
    await msg.edit_text(status)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    history = conversations.get(chat_id, [])

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        if update.message.photo:
            photo = update.message.photo[-1]
            photo_file = await photo.get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            caption = update.message.caption or ""

            result = await agent.handle_image(
                image_bytes=bytes(photo_bytes),
                caption=caption,
                history=history,
            )
            log_content = f"[screenshot] {caption + ' — ' if caption else ''}{result['text']}"
            drop(result.get("detected_category"), "image", log_content, user_id)

        else:
            text = update.message.text or ""
            if text.startswith("/"):
                return

            drop(detect_category(text), "text", text, user_id)
            result = await agent.handle_message(text=text, history=history)

        conversations[chat_id] = result.get("history", history)
        await update.message.reply_text(result["text"])

    except Exception as e:
        logger.error(f"Error handling message: {e}")
        await update.message.reply_text("Something went wrong, try again.")


def main():
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("bringmeuptospeed", cmd_bringmeuptospeed))

    for key in CATEGORIES:
        app.add_handler(CommandHandler(key, cmd_category_status))

    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))

    logger.info("Wedding agent starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
