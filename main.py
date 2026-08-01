"""
Main Entry Point - Video Call Berbayar Bot
Bot utama: python-telegram-bot (reliable polling)
Userbot: pyrofork + pytgcalls (streaming)
"""

import os
import asyncio
import time
import logging

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from config import VIDEO_FOLDER, LOG_FILE, API_PORT, BOT_TOKEN, validate_config

validate_config()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("pytgcalls").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.ERROR)
logging.getLogger("telegram.ext.Updater").setLevel(logging.CRITICAL)

logger = logging.getLogger(__name__)


async def main():
    from telegram.ext import (
        ApplicationBuilder, CommandHandler, CallbackQueryHandler,
        MessageHandler, filters
    )
    from ptb_handlers import (
        cmd_start, handle_callback, handle_photo,
        handle_video_document, handle_text
    )
    from bot_manager import start_default_userbot, start_talent_bot
    from session_manager import session_timer, end_session, get_video_list
    from api_server import start_api_server
    import database as db

    print("=" * 55)
    print("🎬 Video Streaming Berbayar (PTB + Multi-Bot)")
    print("=" * 55)
    os.makedirs(VIDEO_FOLDER, exist_ok=True)

    # Build PTB Application
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_photo))
    app.add_handler(MessageHandler(
        (filters.VIDEO | filters.Document.ALL) & filters.ChatType.PRIVATE,
        handle_video_document
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_text
    ))

    # Initialize + start polling (tanpa run_polling yang block)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    me = await app.bot.get_me()
    print(f"✅ Bot: @{me.username}")
    logger.info(f"Bot started: @{me.username}")

    # Start userbot
    await start_default_userbot()

    # Load talent bots
    talents = await db.get_talents()
    talent_sessions = [t for t in talents if t.get("session_string")]
    if talent_sessions:
        print(f"📱 Loading {len(talent_sessions)} talent bots...")
        for t in talent_sessions:
            await start_talent_bot(t["id"], t["session_string"])

    print(f"📁 Video: {len(get_video_list())} file")
    print("-" * 55)
    print("Ready! /start di bot.")
    print("-" * 55)
    logger.info("Bot fully started and ready.")
    await db.log_activity("bot_started", category="system")

    # Restore sessions
    sessions = await db.get_active_sessions()
    for s in sessions:
        remaining = s["end_time"] - time.time()
        if remaining > 0:
            asyncio.create_task(session_timer(s))
        else:
            asyncio.create_task(end_session(s))

    # Start API server
    await start_api_server(API_PORT)

    # Keep running forever
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        print(f"FATAL: {e}")
        traceback.print_exc()
