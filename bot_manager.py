"""
Bot Manager - Multi-userbot management
Bot utama: python-telegram-bot (PTB) — reliable polling.
Userbot + pytgcalls: pyrofork.
"""

import os
import logging
from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality, VideoQuality
from telegram import Bot
from telegram.constants import ParseMode as TGParseMode

from config import API_ID, API_HASH, BOT_TOKEN, USERBOT_SESSION

logger = logging.getLogger(__name__)


class BotWrapper:
    """Wrapper around PTB Bot that provides interface compatible with session_manager."""

    def __init__(self, token):
        self._bot = Bot(token=token)

    @property
    def bot(self):
        return self._bot

    async def send_message(self, chat_id, text, parse_mode="Markdown", reply_markup=None, **kwargs):
        pm = TGParseMode.MARKDOWN if parse_mode and "markdown" in str(parse_mode).lower() else TGParseMode.HTML if parse_mode and "html" in str(parse_mode).lower() else None
        # Convert Pyrogram markup to PTB markup if needed
        rm = self._convert_markup(reply_markup)
        try:
            msg = await self._bot.send_message(chat_id=chat_id, text=text, parse_mode=pm, reply_markup=rm, **kwargs)
            return msg
        except Exception as e:
            logger.error(f"send_message error: {e}")
            return None

    @staticmethod
    def _convert_markup(markup):
        """Convert any markup format to PTB InlineKeyboardMarkup."""
        if markup is None:
            return None
        # Already PTB InlineKeyboardMarkup
        if hasattr(markup, 'inline_keyboard') and hasattr(markup, 'to_json'):
            return markup
        # Dict format {"inline_keyboard": [[{"text":..., "callback_data":...}]]}
        if isinstance(markup, dict) and "inline_keyboard" in markup:
            from telegram import InlineKeyboardMarkup as PTBMarkup, InlineKeyboardButton as PTBButton
            rows = []
            for row in markup["inline_keyboard"]:
                btns = []
                for b in row:
                    if b.get("callback_data"):
                        btns.append(PTBButton(text=b["text"], callback_data=b["callback_data"]))
                    elif b.get("url"):
                        btns.append(PTBButton(text=b["text"], url=b["url"]))
                    else:
                        btns.append(PTBButton(text=b["text"], callback_data="noop"))
                rows.append(btns)
            return PTBMarkup(rows)
        # Pyrogram InlineKeyboardMarkup → PTB InlineKeyboardMarkup
        if hasattr(markup, 'inline_keyboard'):
            from telegram import InlineKeyboardMarkup as PTBMarkup, InlineKeyboardButton as PTBButton
            rows = []
            for row in markup.inline_keyboard:
                btns = []
                for b in row:
                    if getattr(b, "callback_data", None):
                        btns.append(PTBButton(text=b.text, callback_data=b.callback_data))
                    elif getattr(b, "url", None):
                        btns.append(PTBButton(text=b.text, url=b.url))
                    else:
                        btns.append(PTBButton(text=b.text, callback_data="noop"))
                rows.append(btns)
            return PTBMarkup(rows)
        return markup

    async def delete_messages(self, chat_id, message_ids):
        if isinstance(message_ids, int):
            message_ids = [message_ids]
        for mid in message_ids:
            try:
                await self._bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass

    async def download_media(self, file_id, file_name=None):
        """Download file from Telegram."""
        try:
            file = await self._bot.get_file(file_id)
            dest = file_name or f"/tmp/{file.file_id}"
            await file.download_to_drive(custom_path=dest)
            return dest
        except Exception as e:
            logger.error(f"download_media error: {e}")
            return None

    async def send_photo(self, chat_id, photo, caption=None, parse_mode="Markdown", reply_markup=None):
        pm = TGParseMode.MARKDOWN if parse_mode and "markdown" in parse_mode.lower() else None
        try:
            return await self._bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, parse_mode=pm, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"send_photo error: {e}")
            return None

    async def send_video(self, chat_id, video, caption=None, parse_mode="Markdown", reply_markup=None):
        pm = TGParseMode.MARKDOWN if parse_mode and "markdown" in parse_mode.lower() else None
        try:
            return await self._bot.send_video(chat_id=chat_id, video=video, caption=caption, parse_mode=pm, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"send_video error: {e}")
            return None


# Main bot instance — wrapper compatible with session_manager
bot = BotWrapper(BOT_TOKEN)

# Pyrogram bot client — hanya untuk download file via MTProto (support file besar)
# Bot API HTTP limit 20MB, tapi Pyrogram MTProto support sampai 2GB
_pyro_bot = None


async def get_pyro_bot():
    """Get Pyrogram bot client for file downloads."""
    global _pyro_bot
    if _pyro_bot is None:
        _pyro_bot = Client("download_bot", bot_token=BOT_TOKEN, api_id=API_ID,
                           api_hash=API_HASH, no_updates=True, workdir="/tmp")
        await _pyro_bot.start()
    return _pyro_bot

# Default userbot - pyrofork + pytgcalls
userbot = None
call = None

# Per-talent bots: {talent_id: {"client": Client, "call": PyTgCalls, "ready": bool}}
talent_bots = {}

# Userbot ready flag
userbot_ready = False


async def start_default_userbot():
    """Start default userbot from session string in MongoDB or env."""
    global userbot, call, userbot_ready

    import database as db
    settings = await db.get_settings()
    session_string = settings.get("userbot_session_string", "")

    if not session_string:
        session_string = os.environ.get("USERBOT_SESSION_STRING", "")

    if not session_string:
        logger.warning("No userbot session available.")
        print("⚠️  Userbot belum login.")
        return False

    try:
        userbot = Client("default_userbot", api_id=API_ID, api_hash=API_HASH,
                         session_string=session_string)
        await userbot.start()
        call = PyTgCalls(userbot)
        await call.start()
        me = await userbot.get_me()
        userbot_ready = True

        # Register userbot order handlers (CS auto-reply)
        from userbot_order import register_userbot_handlers
        register_userbot_handlers(userbot)

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
        c = Client(f"talent_{talent_id}", api_id=API_ID, api_hash=API_HASH,
                   session_string=session_string)
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
