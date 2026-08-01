"""
Bot Manager - Multi-userbot management
Handles default userbot + per-talent userbots with pytgcalls
"""

import os
import logging
from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality, VideoQuality

from config import API_ID, API_HASH, BOT_TOKEN, USERBOT_SESSION

logger = logging.getLogger(__name__)

# Main bot instance
bot = Client("payment_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# Default userbot - akan di-start setelah session tersedia
userbot = None
call = None

# Per-talent bots: {talent_id: {"client": Client, "call": PyTgCalls, "ready": bool}}
talent_bots = {}

# Userbot ready flag
userbot_ready = False


async def start_default_userbot():
    """Start default userbot from session string in MongoDB or env."""
    global userbot, call, userbot_ready

    # Coba ambil session string dari MongoDB
    import database as db
    settings = await db.get_settings()
    session_string = settings.get("userbot_session_string", "")

    # Fallback ke env
    if not session_string:
        session_string = os.environ.get("USERBOT_SESSION_STRING", "")

    if not session_string:
        logger.warning("No userbot session available. Login via bot required.")
        print("⚠️  Userbot belum login. Admin perlu login via bot (Setting → 📱 Login Userbot)")
        return False

    try:
        userbot = Client("default_userbot", api_id=API_ID, api_hash=API_HASH, session_string=session_string)
        await userbot.start()
        call = PyTgCalls(userbot)
        await call.start()
        me = await userbot.get_me()
        userbot_ready = True
        logger.info(f"Default userbot: {me.first_name} ({me.id})")
        print(f"✅ Default userbot: {me.first_name} ({me.id})")
        return True
    except Exception as e:
        logger.error(f"Default userbot failed: {e}")
        print(f"⚠️  Default userbot: {e}")
        return False


async def start_talent_bot(talent_id: str, session_string: str):
    """Start a talent-specific userbot with pytgcalls."""
    try:
        c = Client(
            f"talent_{talent_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string
        )
        await c.start()
        tc = PyTgCalls(c)
        await tc.start()
        talent_bots[talent_id] = {"client": c, "call": tc, "ready": True}
        me = await c.get_me()
        logger.info(f"Talent bot started: {me.first_name} ({me.id})")
        print(f"  ✅ Talent bot: {me.first_name} ({me.id})")
        return True
    except Exception as e:
        logger.error(f"Talent bot {talent_id} failed: {e}")
        print(f"  ❌ Talent bot {talent_id}: {e}")
        return False


async def get_talent_bot(talent_id: str):
    """Get the appropriate userbot+call pair for a talent."""
    if talent_id and talent_id in talent_bots and talent_bots[talent_id]["ready"]:
        return talent_bots[talent_id]["client"], talent_bots[talent_id]["call"]
    return userbot, call


async def stop_all_bots():
    """Gracefully stop all bots."""
    for tid, info in talent_bots.items():
        try:
            await info["client"].stop()
        except Exception:
            pass
    try:
        await userbot.stop()
    except Exception:
        pass
    try:
        await bot.stop()
    except Exception:
        pass
