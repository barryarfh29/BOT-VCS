"""
Main Entry Point - Video Call Berbayar Bot
Multi-userbot + MongoDB + Video Rotation + Loop

Bot utama: HTTP Bot API polling (reliable di Docker/EasyPanel)
Userbot: pyrofork + pytgcalls (untuk streaming)
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

from config import VIDEO_FOLDER, LOG_FILE, API_PORT, validate_config
from bot_manager import bot, start_default_userbot, start_talent_bot
from session_manager import session_timer, end_session, get_video_list
from handlers import register_all_handlers
from api_server import start_api_server
from polling import start_polling
import database as db

# ============================================================
# VALIDATE CONFIG BEFORE ANYTHING
# ============================================================

validate_config()

# ============================================================
# LOGGING SETUP
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
# Suppress noisy loggers
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("pytgcalls").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ============================================================
# MAIN
# ============================================================

async def main():
    print("=" * 55)
    print("🎬 Video Streaming Berbayar (MongoDB + Multi-Bot)")
    print("=" * 55)
    os.makedirs(VIDEO_FOLDER, exist_ok=True)

    # Register all handlers pada bot object
    register_all_handlers()

    # Start bot (HTTP API — hanya getMe, tidak polling via MTProto)
    await bot.start()

    # Flush pending updates + hapus webhook (antisipasi conflict)
    from telegram_bot import _api
    await _api("deleteWebhook", drop_pending_updates=True)

    me_bot = await bot.get_me()
    print(f"✅ Bot: @{me_bot.username}")
    logger.info(f"Bot started: @{me_bot.username}")

    # Start default userbot (pyrofork + pytgcalls)
    await start_default_userbot()

    # Load talent-specific bots from DB
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
    await db.log_activity("bot_started", category="system", details={"bot_username": me_bot.username})

    # Restore active sessions (survived restart)
    sessions = await db.get_active_sessions()
    for s in sessions:
        remaining = s["end_time"] - time.time()
        if remaining > 0:
            sc = s.copy()
            sc["started_at"] = time.time()
            sc["end_time"] = time.time() + remaining
            asyncio.create_task(session_timer(sc))
            logger.info(f"Restored session: user={s['user_id']}, {int(remaining//60)}m left")
        else:
            asyncio.create_task(end_session(s))
            logger.info(f"Ending expired session: user={s['user_id']}")

    # Start API server
    await start_api_server(API_PORT)

    # Start HTTP long-polling (reliable, tidak pakai MTProto)
    asyncio.create_task(start_polling(bot))

    # Keep running
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        print(f"FATAL ERROR: {e}")
        traceback.print_exc()
