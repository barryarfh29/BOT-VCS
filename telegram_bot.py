"""
Telegram Bot API wrapper via HTTP — drop-in replacement for Pyrogram bot client.
Semua handler tetap panggil bot.send_message(), bot.delete_messages(), dll
tapi transport-nya via HTTP bukan MTProto.

Pyrofork tetap dipakai HANYA untuk userbot + pytgcalls.
"""

import asyncio
import logging
import json
import os
from io import BytesIO
from typing import Optional, Union, List

import aiohttp

from config import BOT_TOKEN

logger = logging.getLogger(__name__)

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
    return _session


async def _api(method: str, **kwargs) -> dict:
    """Call Telegram Bot API."""
    session = await _get_session()
    # Remove None values
    params = {k: v for k, v in kwargs.items() if v is not None}
    try:
        async with session.post(f"{API_URL}/{method}", json=params) as resp:
            data = await resp.json()
            if not data.get("ok"):
                logger.warning(f"Bot API {method} failed: {data.get('description', '')}")
            return data
    except Exception as e:
        logger.error(f"Bot API {method} error: {e}")
        return {"ok": False}


async def _api_form(method: str, data: dict, files: dict) -> dict:
    """Call API with multipart form data (file upload)."""
    session = await _get_session()
    form = aiohttp.FormData()
    for k, v in data.items():
        if v is not None:
            if isinstance(v, (dict, list)):
                form.add_field(k, json.dumps(v))
            else:
                form.add_field(k, str(v))
    for k, (filename, file_obj, content_type) in files.items():
        form.add_field(k, file_obj, filename=filename, content_type=content_type)
    try:
        async with session.post(f"{API_URL}/{method}", data=form) as resp:
            data = await resp.json()
            if not data.get("ok"):
                logger.warning(f"Bot API form {method} failed: {data.get('description', '')}")
            return data
    except Exception as e:
        logger.error(f"Bot API form {method} error: {e}")
        return {"ok": False}


# ============================================================
# Message-like object (agar handler bisa akses .id, .chat.id, dll)
# ============================================================

class ChatObj:
    def __init__(self, data: dict):
        self.id = data.get("id")
        self.type = data.get("type", "private")
        self.first_name = data.get("first_name", "")
        self.last_name = data.get("last_name")
        self.username = data.get("username")


class UserObj:
    def __init__(self, data: dict):
        self.id = data.get("id")
        self.is_bot = data.get("is_bot", False)
        self.first_name = data.get("first_name", "")
        self.last_name = data.get("last_name")
        self.username = data.get("username")
        self.language_code = data.get("language_code")


class PhotoObj:
    def __init__(self, data: dict):
        # Telegram sends array of PhotoSize, use largest
        if isinstance(data, list) and data:
            largest = data[-1]
            self.file_id = largest.get("file_id", "")
        else:
            self.file_id = ""


class VideoObj:
    def __init__(self, data: dict):
        self.file_id = data.get("file_id", "")
        self.file_name = data.get("file_name")
        self.file_size = data.get("file_size", 0)
        self.duration = data.get("duration")


class DocumentObj:
    def __init__(self, data: dict):
        self.file_id = data.get("file_id", "")
        self.file_name = data.get("file_name")
        self.file_size = data.get("file_size", 0)


class MessageObj:
    """Wrapper for Telegram message — compatible with handler code."""

    def __init__(self, data: dict, bot_ref):
        self._bot = bot_ref
        self._data = data
        self.id = data.get("message_id")
        self.message_id = data.get("message_id")
        self.date = data.get("date")
        self.text = data.get("text")
        self.chat = ChatObj(data.get("chat", {}))
        self.from_user = UserObj(data.get("from", {})) if data.get("from") else None

        # Media
        self.photo = PhotoObj(data["photo"]) if "photo" in data else None
        self.video = VideoObj(data["video"]) if "video" in data else None
        self.document = DocumentObj(data["document"]) if "document" in data else None

    async def reply_text(self, text: str, reply_markup=None, **kwargs):
        """Reply to this message."""
        return await self._bot.send_message(
            self.chat.id, text, reply_markup=reply_markup,
            reply_to_message_id=self.id, **kwargs
        )

    async def edit_text(self, text: str, reply_markup=None, parse_mode: str = "Markdown", **kwargs):
        """Edit this message text."""
        markup = self._bot._convert_markup(reply_markup)
        r = await _api("editMessageText",
                       chat_id=self.chat.id, message_id=self.id,
                       text=text, parse_mode=parse_mode, reply_markup=markup)
        return r.get("ok", False)

    async def edit_reply_markup(self, reply_markup=None):
        """Edit this message reply markup."""
        markup = self._bot._convert_markup(reply_markup)
        await _api("editMessageReplyMarkup",
                   chat_id=self.chat.id, message_id=self.id, reply_markup=markup)

    async def delete(self):
        """Delete this message."""
        await self._bot.delete_messages(self.chat.id, self.id)

    async def forward(self, chat_id: int):
        """Forward this message to another chat."""
        await _api("forwardMessage",
                   chat_id=chat_id, from_chat_id=self.chat.id, message_id=self.id)


class CallbackQueryObj:
    """Wrapper for Telegram callback_query."""

    def __init__(self, data: dict, bot_ref):
        self._bot = bot_ref
        self._data = data
        self.id = data.get("id")
        self.data = data.get("data", "")
        self.from_user = UserObj(data.get("from", {}))
        self.message = MessageObj(data.get("message", {}), bot_ref) if data.get("message") else None

    async def answer(self, text: str = None, show_alert: bool = False):
        await _api("answerCallbackQuery",
                   callback_query_id=self.id,
                   text=text, show_alert=show_alert)


# ============================================================
# BotClient — drop-in object yang handler pakai (bot.send_message, dll)
# ============================================================

class BotClient:
    """Pyrogram-compatible bot client yang pakai HTTP Bot API."""

    def __init__(self):
        self._me = None
        self._handlers_message = []   # [(filter_fn, handler_fn)]
        self._handlers_callback = []  # [(pattern_str, handler_fn)]
        self._started = False

    async def start(self):
        """Cuma get_me — tidak polling."""
        me = await _api("getMe")
        if me.get("ok"):
            self._me = me.get("result", {})
        self._started = True

    async def get_me(self):
        """Return UserObj of this bot."""
        if not self._me:
            r = await _api("getMe")
            self._me = r.get("result", {})
        return UserObj(self._me)

    # --- Message sending ---

    async def send_message(self, chat_id: int, text: str, parse_mode: str = "Markdown",
                           reply_markup=None, disable_web_page_preview: bool = True,
                           reply_to_message_id: int = None):
        """Send text message. Returns MessageObj."""
        markup = self._convert_markup(reply_markup)
        # Normalize parse_mode (Pyrogram uses lowercase, Bot API accepts both)
        pm = parse_mode
        if pm and pm.lower() == "html":
            pm = "HTML"
        elif pm and pm.lower() == "markdown":
            pm = "Markdown"
        r = await _api("sendMessage",
                       chat_id=chat_id, text=text, parse_mode=pm,
                       reply_markup=markup,
                       disable_web_page_preview=disable_web_page_preview,
                       reply_to_message_id=reply_to_message_id)
        if r.get("ok"):
            return MessageObj(r["result"], self)
        return None

    async def send_photo(self, chat_id: int, photo, caption: str = None,
                         parse_mode: str = "Markdown", reply_markup=None):
        """Send photo (file_id string, BytesIO, or local file path)."""
        markup = self._convert_markup(reply_markup)
        if isinstance(photo, str) and not os.path.isfile(photo):
            # file_id
            r = await _api("sendPhoto", chat_id=chat_id, photo=photo,
                           caption=caption, parse_mode=parse_mode,
                           reply_markup=markup)
        elif isinstance(photo, str) and os.path.isfile(photo):
            # Local file path
            with open(photo, "rb") as f:
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                    data["parse_mode"] = parse_mode
                if markup:
                    data["reply_markup"] = markup
                files = {"photo": (os.path.basename(photo), f, "image/jpeg")}
                r = await _api_form("sendPhoto", data, files)
        else:
            # BytesIO
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = parse_mode
            if markup:
                data["reply_markup"] = markup
            files = {"photo": ("photo.jpg", photo, "image/jpeg")}
            r = await _api_form("sendPhoto", data, files)
        if r.get("ok"):
            return MessageObj(r["result"], self)
        return None

    async def send_video(self, chat_id: int, video, caption: str = None,
                         parse_mode: str = "Markdown", reply_markup=None):
        """Send video (file_id string or local file path)."""
        markup = self._convert_markup(reply_markup)
        if isinstance(video, str) and not os.path.isfile(video):
            # file_id
            r = await _api("sendVideo", chat_id=chat_id, video=video,
                           caption=caption, parse_mode=parse_mode,
                           reply_markup=markup)
        elif isinstance(video, str) and os.path.isfile(video):
            # Local file
            with open(video, "rb") as f:
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                    data["parse_mode"] = parse_mode
                if markup:
                    data["reply_markup"] = markup
                files = {"video": (os.path.basename(video), f, "video/mp4")}
                r = await _api_form("sendVideo", data, files)
        else:
            r = {"ok": False}
        if r.get("ok"):
            return MessageObj(r["result"], self)
        return None

    async def send_document(self, chat_id: int, document: str, caption: str = None,
                            parse_mode: str = "Markdown"):
        r = await _api("sendDocument", chat_id=chat_id, document=document,
                       caption=caption, parse_mode=parse_mode)
        if r.get("ok"):
            return MessageObj(r["result"], self)
        return None

    # --- Edit / Delete ---

    async def delete_messages(self, chat_id: int, message_ids):
        """Delete messages. message_ids can be int or list."""
        if isinstance(message_ids, int):
            message_ids = [message_ids]
        if not message_ids:
            return
        # Try batch delete first
        r = await _api("deleteMessages", chat_id=chat_id, message_ids=message_ids)
        if not r.get("ok"):
            # Fallback: one by one
            for mid in message_ids:
                await _api("deleteMessage", chat_id=chat_id, message_id=mid)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str,
                                parse_mode: str = "Markdown", reply_markup=None):
        markup = self._convert_markup(reply_markup)
        return await _api("editMessageText",
                          chat_id=chat_id, message_id=message_id,
                          text=text, parse_mode=parse_mode, reply_markup=markup)

    async def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup=None):
        markup = self._convert_markup(reply_markup)
        return await _api("editMessageReplyMarkup",
                          chat_id=chat_id, message_id=message_id, reply_markup=markup)

    # --- File download ---

    async def download_media(self, file_id: str, file_name: str = None):
        """Download file from Telegram. Returns path or None."""
        r = await _api("getFile", file_id=file_id)
        if not r.get("ok"):
            return None
        file_path = r["result"].get("file_path")
        if not file_path:
            return None
        session = await _get_session()
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        dest = file_name or file_path.split("/")[-1]
        try:
            async with session.get(url) as resp:
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        f.write(chunk)
            return dest
        except Exception as e:
            logger.error(f"Download failed: {e}")
            return None

    # --- Handler registration (Pyrogram-compatible decorators) ---

    def on_message(self, filters=None):
        """Decorator to register message handler."""
        def decorator(func):
            self._handlers_message.append((filters, func))
            return func
        return decorator

    def on_callback_query(self, filters=None):
        """Decorator to register callback query handler."""
        def decorator(func):
            self._handlers_callback.append((filters, func))
            return func
        return decorator

    # --- Dispatch (called by polling.py) ---

    async def dispatch_update(self, update: dict):
        """Route update to registered handlers."""
        if "message" in update:
            msg = MessageObj(update["message"], self)
            for filt, handler in self._handlers_message:
                if filt is None or self._match_filter(filt, msg, update["message"]):
                    try:
                        await handler(self, msg)
                    except Exception as e:
                        logger.error(f"Handler error: {e}", exc_info=True)
                    return

        elif "callback_query" in update:
            cb = CallbackQueryObj(update["callback_query"], self)
            for filt, handler in self._handlers_callback:
                if filt is None or self._match_cb_filter(filt, cb):
                    try:
                        await handler(self, cb)
                    except Exception as e:
                        logger.error(f"Callback handler error: {e}", exc_info=True)
                    return

    def _match_filter(self, filt, msg: 'MessageObj', raw: dict) -> bool:
        """Match Pyrogram-style filter against message."""
        try:
            return self._eval_filter(filt, msg, raw)
        except Exception:
            return False

    def _eval_filter(self, filt, msg: 'MessageObj', raw: dict) -> bool:
        """Evaluate Pyrogram filter tree recursively."""
        ftype = type(filt).__name__

        # AndFilter (filters.X & filters.Y)
        if ftype == "AndFilter" or (hasattr(filt, 'base') and hasattr(filt, 'other')):
            return self._eval_filter(filt.base, msg, raw) and self._eval_filter(filt.other, msg, raw)

        # OrFilter (filters.X | filters.Y)
        if ftype == "OrFilter" or (hasattr(filt, 'base') and hasattr(filt, 'other') and 'or' in ftype.lower()):
            return self._eval_filter(filt.base, msg, raw) or self._eval_filter(filt.other, msg, raw)

        # InvertFilter (~filters.X)
        if ftype == "InvertFilter" or (hasattr(filt, 'base') and not hasattr(filt, 'other')):
            return not self._eval_filter(filt.base, msg, raw)

        # Command filter
        if hasattr(filt, 'commands'):
            text = raw.get("text", "")
            if not text or not text.startswith("/"):
                return False
            cmd = text.split()[0].lstrip("/").split("@")[0].lower()
            return cmd in [c.lower() for c in filt.commands]

        # Private chat filter
        fname = str(getattr(filt, 'name', '') or '').lower()
        if fname == 'private' or 'private' in fname:
            return raw.get("chat", {}).get("type") == "private"

        # Photo filter
        if fname == 'photo' or 'photo' in fname:
            return "photo" in raw

        # Video filter
        if fname == 'video' or 'video' in fname:
            return "video" in raw

        # Document filter
        if fname == 'document' or 'document' in fname:
            return "document" in raw

        # Text filter
        if fname == 'text' or 'text' in fname:
            return bool(raw.get("text"))

        # AllFilter (matches everything)
        if fname == 'all' or ftype == 'AllFilter':
            return True

        # Fallback: assume match
        return True

    def _match_cb_filter(self, filt, cb: 'CallbackQueryObj') -> bool:
        """Match callback filter (Pyrogram regex pattern)."""
        import re
        # Pyrogram filters.regex creates object with .pattern attribute
        if hasattr(filt, 'pattern'):
            pattern = filt.pattern
            if isinstance(pattern, str):
                return bool(re.match(pattern, cb.data))
            elif hasattr(pattern, 'match'):
                return bool(pattern.match(cb.data))
            else:
                return bool(re.match(str(pattern), cb.data))
        # Check if it's a combined filter (AndFilter wrapping regex)
        if hasattr(filt, 'base'):
            return self._match_cb_filter(filt.base, cb)
        if hasattr(filt, 'other'):
            return self._match_cb_filter(filt.other, cb)
        # No pattern found — DON'T match (safe default)
        return False

    # --- Utility ---

    @staticmethod
    def _convert_markup(markup):
        """Convert Pyrogram InlineKeyboardMarkup to dict for Bot API."""
        if markup is None:
            return None
        if isinstance(markup, dict):
            return markup
        # Pyrogram InlineKeyboardMarkup object
        if hasattr(markup, 'inline_keyboard'):
            rows = []
            for row in markup.inline_keyboard:
                btns = []
                for b in row:
                    btn = {"text": b.text}
                    if getattr(b, "callback_data", None):
                        btn["callback_data"] = b.callback_data
                    if getattr(b, "url", None):
                        btn["url"] = b.url
                    btns.append(btn)
                rows.append(btns)
            return {"inline_keyboard": rows}
        return markup
