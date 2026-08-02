"""
PTB Handlers — bridge between python-telegram-bot updates and business logic.
Semua logic bisnis (admin, customer, payment, session) tetap di files lama.
File ini hanya routing dan adaptasi format.
"""

import asyncio
import time
import re
import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

import database as db
from config import LOG_CHANNEL_START, LOG_CHANNEL_PAYMENT

logger = logging.getLogger(__name__)

# Admin state (shared)
admin_state = {}

# Payment message tracking: invoice_id -> {chat_id, msg_ids: [qr_msg_id, invoice_msg_id]}
_payment_msgs = {}

# Telegram supported HTML tags
_TG_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
                    "code", "pre", "a", "tg-spoiler", "blockquote", "span"}


def _strip_unsupported_html(html: str) -> str:
    """Strip HTML tags not supported by Telegram, keep text content and allowed tags."""
    import re
    def _replace_tag(m):
        tag = m.group(1).lower().split()[0].strip("/")
        if tag in _TG_ALLOWED_TAGS:
            return m.group(0)
        # Replace block-level tags with newline, inline with nothing
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
            return "\n"
        return ""
    result = re.sub(r'<(/?\w[^>]*)>', _replace_tag, html)
    # Clean up multiple newlines
    result = re.sub(r'\n{3,}', '\n\n', result).strip()
    return result


# ============================================================
# UI CLEANUP — hapus pesan lama supaya chat selalu bersih
# ============================================================

async def _clean_ui(chat_id, context):
    """Hapus semua pesan UI lama yang tercatat di chat ini."""
    ids = await db.pop_ui_messages(chat_id)
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


async def _track_ui(chat_id, *msg_ids):
    """Catat message id UI untuk dihapus pada navigasi berikutnya."""
    valid = [m for m in msg_ids if m]
    if valid:
        await db.track_ui_messages(chat_id, valid)


async def _get_bot(context):
    """Get PTB bot instance."""
    return context.bot


async def is_admin(user_id: int) -> bool:
    admins = await db.get_admin_ids()
    return user_id in admins


async def format_price(price_idr: int, user_id: int) -> str:
    """Format harga berdasarkan bahasa user. IDR → MYR kalau Malaysia."""
    lang = await db.get_user_lang(user_id)
    if lang == "my":
        from currency import get_myr_rate
        rate = await get_myr_rate()
        myr = price_idr / rate
        return f"RM {myr:.2f}"
    return f"Rp {price_idr:,}"


async def _send_talent_menu(chat_id: int, user_id: int, context):
    """Kirim menu talent (loading + welcome). Bisa dipanggil dari message atau callback."""
    from rich_message import render_template, send_template
    from bot_manager import bot as bot_wrapper

    talents = await db.get_talents()
    count = len(talents)
    tpl1_raw = (await db.get_template("loading_1")).replace("{count}", str(count))
    tpl1_html = render_template(tpl1_raw, count=str(count))
    loading = await context.bot.send_message(chat_id=chat_id, text=tpl1_html or "🔍 Loading...", parse_mode=ParseMode.HTML)
    await asyncio.sleep(1)

    if not talents:
        await loading.edit_text("No talents available.")
        return

    btns = []
    for t in talents:
        tid = t["id"]
        in_session = await db.get_session_by_talent(tid)
        cooldown_at = await db.get_cooldown(tid)
        is_off = t.get("offline", False)
        if is_off or in_session or (cooldown_at and time.time() < cooldown_at):
            btns.append(InlineKeyboardButton(f"{t['name']} — FULL", callback_data=f"full_{tid}"))
        else:
            btns.append(InlineKeyboardButton(f"{t['name']}", callback_data=f"talent_{tid}"))
    buttons = [btns[i:i+2] for i in range(0, len(btns), 2)]

    try:
        await loading.delete()
    except Exception:
        pass

    template = await db.get_template("welcome")
    clean = re.sub(r'<[^>]+>', '', template).strip() if template else ""
    markup_rows = [[{"text": b.text, "callback_data": b.callback_data} for b in row] for row in buttons]

    # Tambah tombol CS kalau ada setting
    settings = await db.get_settings()
    cs_username = settings.get("cs_username", "")
    if cs_username:
        cs_username = cs_username.lstrip("@")
        markup_rows.append([{"text": "📞 Customer Service", "url": f"https://t.me/{cs_username}"}])

    markup_dict = {"inline_keyboard": markup_rows}

    if clean:
        welcome_id = await send_template(bot_wrapper, chat_id, template, markup=markup_dict)
        await _track_ui(chat_id, welcome_id)
    else:
        msg = await bot_wrapper.send_message(chat_id, "ㅤ", reply_markup=markup_dict)
        if msg:
            await _track_ui(chat_id, getattr(msg, 'message_id', None) or getattr(msg, 'id', None))


async def _log_to_channel(channel_key: str, text: str, reply_markup=None, context=None):
    """Log ke Telegram channel."""
    try:
        settings = await db.get_settings()
        channel_id = settings.get(channel_key, 0)
        if not channel_id or not context:
            return
        kwargs = {"chat_id": int(channel_id), "text": text, "parse_mode": ParseMode.MARKDOWN}
        if reply_markup:
            kwargs["reply_markup"] = reply_markup
        await context.bot.send_message(**kwargs)
    except Exception:
        pass


# ============================================================
# /start COMMAND
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id

    admin_state.pop(user_id, None)

    # Clean previous UI messages
    await _clean_ui(chat_id, context)

    # Cek bahasa — kalau belum pilih DAN bukan admin, tampilkan pilihan
    if not await is_admin(user_id):
        lang = await db.get_user_lang(user_id)
        if not lang:
            await update.message.reply_text(
                "🌐 **Select Language / Pilih Bahasa**",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🇮🇩 Indonesia", callback_data="lang_id"),
                     InlineKeyboardButton("🇲🇾 Malaysia", callback_data="lang_my")]
                ])
            )
            return

    settings = await db.get_settings()
    if not settings.get("admin_ids"):
        await db.add_admin(user_id)

    await db.log_activity("bot_start", category="user", user_id=user_id,
                          details={"name": user.first_name})

    # Log to channel
    await _log_to_channel("log_channel_start",
        f"👤 **User Start**\nID: `{user_id}`\nName: {user.first_name or '-'}\nUsername: @{user.username or '-'}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Chat", url=f"tg://user?id={user_id}")]]),
        context=context,
    )

    # Check active session
    existing = await db.get_session_by_user(user_id)
    if existing and not await is_admin(user_id):
        remaining = max(0, existing["end_time"] - time.time())
        mins = int(remaining // 60)
        await update.message.reply_text(
            f"**Active Session**\nRemaining: **{mins}m**\nWait until it finishes.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Refresh", callback_data="refresh_session")]])
        )
        return

    if await is_admin(user_id):
        await update.message.reply_text(
            "**Admin Panel**",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Talent", callback_data="adm_talents"),
                 InlineKeyboardButton("Status", callback_data="adm_status")],
                [InlineKeyboardButton("Transaksi", callback_data="adm_txn"),
                 InlineKeyboardButton("Setting", callback_data="adm_setting")],
            ])
        )
    else:
        # Loading animation (support HTML formatting from template)
        from rich_message import render_template
        talents = await db.get_talents()
        count = len(talents)
        tpl1_raw = (await db.get_template("loading_1")).replace("{count}", str(count))
        tpl1_html = render_template(tpl1_raw, count=str(count))
        loading = await update.message.reply_text(tpl1_html or "🔍 Loading...", parse_mode=ParseMode.HTML)
        await asyncio.sleep(1)

        if not talents:
            await loading.edit_text("No talents available.")
            return

        # Build talent buttons
        btns = []
        for t in talents:
            tid = t["id"]
            in_session = await db.get_session_by_talent(tid)
            cooldown_at = await db.get_cooldown(tid)
            is_off = t.get("offline", False)
            if is_off or in_session or (cooldown_at and time.time() < cooldown_at):
                btns.append(InlineKeyboardButton(f"{t['name']} — FULL", callback_data=f"full_{tid}"))
            else:
                btns.append(InlineKeyboardButton(f"{t['name']}", callback_data=f"talent_{tid}"))
        buttons = [btns[i:i+2] for i in range(0, len(btns), 2)]

        # Delete loading message
        try:
            await loading.delete()
        except Exception:
            pass

        # Send welcome via rich message (send_template)
        from rich_message import send_template
        from bot_manager import bot

        template = await db.get_template("welcome")
        clean = re.sub(r'<[^>]+>', '', template).strip() if template else ""

        # Build markup as dict
        markup_rows = []
        for row in buttons:
            markup_rows.append([{"text": b.text, "callback_data": b.callback_data} for b in row])

        # Tambah tombol CS
        settings = await db.get_settings()
        cs_username = settings.get("cs_username", "")
        if cs_username:
            cs_username = cs_username.lstrip("@")
            markup_rows.append([{"text": "📞 Customer Service", "url": f"https://t.me/{cs_username}"}])

        markup_dict = {"inline_keyboard": markup_rows}

        if clean:
            welcome_id = await send_template(bot, chat_id, template, markup=markup_dict)
            await _track_ui(chat_id, welcome_id)
        else:
            msg = await bot.send_message(chat_id, "ㅤ", reply_markup=markup_dict)
            if msg:
                await _track_ui(chat_id, getattr(msg, 'message_id', None) or getattr(msg, 'id', None))


# ============================================================
# CALLBACK QUERY HANDLER (routes all callbacks)
# ============================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route all callback queries."""
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    await query.answer()

    # Language selection
    if data in ("lang_id", "lang_my"):
        lang = "id" if data == "lang_id" else "my"
        await db.set_user_lang(user_id, lang)
        try:
            await query.message.delete()
        except Exception:
            pass

        # Log ke channel
        lang_label = "🇮🇩 Indonesia" if lang == "id" else "🇲🇾 Malaysia"
        await _log_to_channel("log_channel_start",
            f"👤 **New User Start**\nID: `{user_id}`\nName: {query.from_user.first_name or '-'}\nUsername: @{query.from_user.username or '-'}\nLanguage: {lang_label}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Chat", url=f"tg://user?id={user_id}")]]),
            context=context,
        )
        await db.log_activity("bot_start", category="user", user_id=user_id,
                              details={"name": query.from_user.first_name, "lang": lang})

        # Langsung tampilkan menu talent
        chat_id = query.message.chat_id
        await _send_talent_menu(chat_id, user_id, context)
        return

    # Admin callbacks — check permission
    if data.startswith("adm_"):
        if not await is_admin(user_id):
            return
        await _handle_admin_callback(update, context, data)
        return

    # Customer callbacks
    if data.startswith("talent_"):
        await _handle_talent_detail(update, context, data)
    elif data.startswith("full_"):
        await _handle_talent_full(update, context, data)
    elif data.startswith("order_"):
        await _handle_order(update, context, data)
    elif data.startswith("pord_"):
        await _handle_pkg_order(update, context, data)
    elif data == "back_menu":
        await _handle_back_menu(update, context)
    elif data.startswith("chk_"):
        await _handle_check_payment(update, context, data)
    elif data.startswith("cnl_"):
        await _handle_cancel_payment(update, context, data)
    elif data.startswith("sub_"):
        await _handle_subscribe(update, context, data)
    elif data.startswith("skip_bukti_"):
        await _handle_skip_bukti(update, context, data)
    elif data == "promo_skip":
        await _handle_promo_skip(update, context)
    elif data == "refresh_session":
        await _handle_refresh_session(update, context)


# ============================================================
# ADMIN CALLBACK HANDLERS (simplified — key actions only)
# ============================================================

async def _handle_admin_callback(update, context, data):
    """Handle admin panel callbacks."""
    query = update.callback_query
    user_id = query.from_user.id

    if data == "adm_talents":
        talents = await db.get_talents()
        buttons = []
        for t in talents:
            buttons.append([InlineKeyboardButton(
                f"{t['name']} — Rp {t['price']:,}/{t['duration']}m",
                callback_data=f"adm_tedit_{t['id']}"
            )])
        buttons.append([InlineKeyboardButton("+ Tambah Talent", callback_data="adm_tadd")])
        buttons.append([InlineKeyboardButton("Kembali", callback_data="adm_menu")])
        await query.message.edit_text("**Manage Talent**", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "adm_menu":
        await query.message.edit_text("**Admin Panel**", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Talent", callback_data="adm_talents"),
                 InlineKeyboardButton("Status", callback_data="adm_status")],
                [InlineKeyboardButton("Transaksi", callback_data="adm_txn"),
                 InlineKeyboardButton("Setting", callback_data="adm_setting")],
            ]))

    elif data == "adm_status":
        sessions = await db.get_active_sessions()
        if not sessions:
            text = "**Tidak ada sesi aktif.**"
        else:
            lines = [f"  • User `{s['user_id']}` — {int(max(0, s['end_time']-time.time())//60)}m" for s in sessions]
            text = f"**Sesi Aktif** ({len(sessions)})\n\n" + "\n".join(lines)
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data="adm_menu")]]))

    elif data == "adm_txn":
        txns = await db.get_transactions(10)
        if not txns:
            text = "**Belum ada transaksi.**"
        else:
            lines = [f"  • Rp {t['amount']:,} — {t.get('status','?')} — {t.get('user_name','?')}" for t in txns]
            text = "**Transaksi:**\n\n" + "\n".join(lines)
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data="adm_menu")]]))

    elif data == "adm_setting":
        settings = await db.get_settings()
        text = (f"**Setting**\n\nHarga: Rp {settings.get('price',50000):,}\n"
                f"Durasi: {settings.get('duration',30)}m\nAdmin: {settings.get('admin_ids',[])}")
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Harga", callback_data="adm_s_price"),
                 InlineKeyboardButton("Durasi", callback_data="adm_s_duration")],
                [InlineKeyboardButton("+ Admin", callback_data="adm_s_addadmin")],
                [InlineKeyboardButton("Kembali", callback_data="adm_menu")],
            ]))

    elif data.startswith("adm_tedit_"):
        talent_id = data.replace("adm_tedit_", "")
        talent = await db.get_talent(talent_id)
        if not talent:
            return
        videos = talent.get("videos", [])
        pkgs = talent.get("packages") or []
        if pkgs:
            pkg_lines = []
            for i, p in enumerate(pkgs):
                lbl = (p.get('label') or '').strip() or f"{p.get('duration',0)}m"
                pkg_lines.append(f"  {i+1}. {lbl} — Rp {int(p.get('price',0)):,}")
            pkg_text = "\n".join(pkg_lines)
        else:
            pkg_text = "  (belum ada)"
        videos_text = "\n".join([f"  {i+1}. {v.get('filename',v) if isinstance(v,dict) else v}" for i, v in enumerate(videos)]) if videos else "  Belum ada"
        text = (f"<b>{talent['name']}</b>\n\n"
                f"Status: {'OFFLINE' if talent.get('offline') else 'ONLINE'}\n"
                f"Harga: Rp {talent['price']:,} | Durasi: {talent['duration']}m | CD: {talent.get('cooldown',0)}m\n\n"
                f"<b>Paket ({len(pkgs)}):</b>\n{pkg_text}\n\n"
                f"<b>Video ({len(videos)}):</b>\n{videos_text}")
        toggle = "Set ONLINE" if talent.get("offline") else "Set OFFLINE"
        buttons = [
            [InlineKeyboardButton(toggle, callback_data=f"adm_toggle_{talent_id}")],
            [InlineKeyboardButton("Set Akun", callback_data=f"adm_tset_{talent_id}_session"),
             InlineKeyboardButton("+ Video", callback_data=f"adm_tset_{talent_id}_video")],
            [InlineKeyboardButton("Lihat Video", callback_data=f"adm_vplay_{talent_id}"),
             InlineKeyboardButton("Hapus Video", callback_data=f"adm_vdel_{talent_id}")],
            [InlineKeyboardButton("Nama", callback_data=f"adm_tset_{talent_id}_name"),
             InlineKeyboardButton("Desc", callback_data=f"adm_tset_{talent_id}_desc")],
            [InlineKeyboardButton("Harga", callback_data=f"adm_tset_{talent_id}_price"),
             InlineKeyboardButton("Durasi", callback_data=f"adm_tset_{talent_id}_duration")],
            [InlineKeyboardButton("Label Durasi", callback_data=f"adm_tset_{talent_id}_durationlabel"),
             InlineKeyboardButton("Paket Durasi", callback_data=f"adm_pkg_{talent_id}")],
            [InlineKeyboardButton("Foto", callback_data=f"adm_tset_{talent_id}_photo"),
             InlineKeyboardButton("Cooldown", callback_data=f"adm_tset_{talent_id}_cooldown")],
            [InlineKeyboardButton("Hapus Talent", callback_data=f"adm_tdel_{talent_id}")],
            [InlineKeyboardButton("Kembali", callback_data="adm_talents")],
        ]
        await query.message.edit_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm_toggle_"):
        talent_id = data.replace("adm_toggle_", "")
        talent = await db.get_talent(talent_id)
        if talent:
            new_val = not talent.get("offline", False)
            await db.update_talent(talent_id, offline=new_val)
            if not new_val:
                await db.remove_cooldown(talent_id)
        # Refresh
        await _handle_admin_callback(update, context, f"adm_tedit_{talent_id}")

    elif data.startswith("adm_tset_"):
        raw = data.replace("adm_tset_", "")
        last_us = raw.rfind("_")
        talent_id = raw[:last_us]
        field = raw[last_us + 1:]
        if field == "video":
            admin_state[user_id] = {"action": "edit_video", "talent_id": talent_id}
            await query.message.edit_text("**Kirim/forward video:**",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data=f"adm_tedit_{talent_id}")]]))
        elif field == "photo":
            admin_state[user_id] = {"action": "edit_photo", "talent_id": talent_id}
            await query.message.edit_text("**Kirim foto baru:**",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data=f"adm_tedit_{talent_id}")]]))
        else:
            admin_state[user_id] = {"action": "edit_field", "talent_id": talent_id, "field": field}
            await query.message.edit_text(f"**Kirim {field} baru:**",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data=f"adm_tedit_{talent_id}")]]))

    elif data.startswith("adm_tdel_"):
        talent_id = data.replace("adm_tdel_", "")
        await db.delete_talent(talent_id)
        await query.message.edit_text("**Dihapus.**", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Kembali", callback_data="adm_talents")]]))

    elif data == "adm_tadd":
        admin_state[user_id] = {"action": "add_talent", "step": "photo"}
        await query.message.edit_text("**Tambah Talent**\n\nKirim foto:", parse_mode=ParseMode.MARKDOWN)

    elif data.startswith("adm_vplay_"):
        talent_id = data.replace("adm_vplay_", "")
        talent = await db.get_talent(talent_id)
        if not talent:
            return
        videos = talent.get("videos", [])
        if not videos:
            await query.answer("Belum ada video.", show_alert=True)
            return
        for v in videos:
            if isinstance(v, dict) and v.get("file_id"):
                try:
                    await context.bot.send_video(chat_id=query.message.chat_id, video=v["file_id"],
                                                 caption=f"`{v.get('filename','video')}`", parse_mode=ParseMode.MARKDOWN)
                except Exception:
                    try:
                        await context.bot.send_document(chat_id=query.message.chat_id, document=v["file_id"],
                                                       caption=f"`{v.get('filename','video')}`", parse_mode=ParseMode.MARKDOWN)
                    except Exception:
                        pass

    elif data.startswith("adm_vdel_"):
        talent_id = data.replace("adm_vdel_", "")
        talent = await db.get_talent(talent_id)
        if not talent:
            return
        videos = talent.get("videos", [])
        if not videos:
            await query.answer("Tidak ada video.", show_alert=True)
            return
        buttons = []
        for i, v in enumerate(videos):
            fname = v.get('filename', f'video_{i}') if isinstance(v, dict) else str(v)
            buttons.append([InlineKeyboardButton(fname, callback_data=f"adm_vrem_{talent_id}_{i}")])
        buttons.append([InlineKeyboardButton("Kembali", callback_data=f"adm_tedit_{talent_id}")])
        await query.message.edit_text("**Pilih video untuk dihapus:**", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm_vrem_"):
        raw = data.replace("adm_vrem_", "")
        last_us = raw.rfind("_")
        talent_id = raw[:last_us]
        idx = int(raw[last_us + 1:])
        await db.remove_video_from_talent(talent_id, idx)
        await _handle_admin_callback(update, context, f"adm_tedit_{talent_id}")

    elif data.startswith("adm_pkg_"):
        talent_id = data.replace("adm_pkg_", "")
        talent = await db.get_talent(talent_id)
        if not talent:
            return
        pkgs = talent.get("packages") or []
        if pkgs:
            lines = []
            for i, p in enumerate(pkgs):
                lbl = (p.get('label') or '').strip() or f"{p.get('duration',0)}m"
                lines.append(f"  {i+1}. {lbl} — Rp {int(p.get('price',0)):,}")
            body = "\n".join(lines)
        else:
            body = "(belum ada)"
        buttons = [[InlineKeyboardButton("+ Tambah Paket", callback_data=f"adm_pkgadd_{talent_id}")]]
        buttons.append([InlineKeyboardButton("Kembali", callback_data=f"adm_tedit_{talent_id}")])
        await query.message.edit_text(f"**Paket Durasi**\n\n{body}", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm_pkgadd_"):
        talent_id = data.replace("adm_pkgadd_", "")
        admin_state[user_id] = {"action": "add_package", "talent_id": talent_id}
        await query.message.edit_text(
            "**Tambah Paket**\n\nKirim: `durasi harga [label]`\nContoh: `5 50000 Exclusive`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data=f"adm_pkg_{talent_id}")]]))

    # Catch-all — route to settings sub-handler or show not available
    elif data.startswith("adm_s_"):
        await _handle_admin_settings_callback(update, context, data)
    else:
        await query.answer("Fitur ini belum tersedia.", show_alert=True)


# Additional admin settings handlers
async def _handle_admin_settings_callback(update, context, data):
    """Called from _handle_admin_callback for adm_s_ prefix."""
    query = update.callback_query
    user_id = query.from_user.id

    if data == "adm_s_price":
        admin_state[user_id] = {"action": "set_price"}
        s = await db.get_settings()
        await query.message.edit_text(f"**Kirim harga baru:**\n\nSaat ini: Rp {s.get('price',50000):,}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data="adm_setting")]]))

    elif data == "adm_s_duration":
        admin_state[user_id] = {"action": "set_duration"}
        s = await db.get_settings()
        await query.message.edit_text(f"**Kirim durasi baru (menit):**\n\nSaat ini: {s.get('duration',30)}m",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data="adm_setting")]]))

    elif data == "adm_s_addadmin":
        admin_state[user_id] = {"action": "add_admin"}
        await query.message.edit_text("**Kirim user ID:**", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data="adm_setting")]]))


# ============================================================
# CUSTOMER CALLBACKS
# ============================================================

async def _handle_talent_detail(update, context, data):
    """Show talent detail when user clicks talent button."""
    query = update.callback_query
    talent_id = data.replace("talent_", "")
    talent = await db.get_talent(talent_id)
    if not talent:
        return

    from rich_message import send_template, duration_display, apply_duration_label, strip_price_duration_rows
    from bot_manager import bot

    price_str = await format_price(int(talent['price']), query.from_user.id)

    packages = talent.get("packages") or []
    if packages:
        btns = []
        for i, p in enumerate(packages):
            lbl = (p.get("label") or "").strip() or f"{p.get('duration',0)} min"
            pkg_price = int(p.get('price', 0))
            pkg_price_str = await format_price(pkg_price, query.from_user.id)
            btns.append(InlineKeyboardButton(f"{lbl} — {pkg_price_str}",
                                             callback_data=f"pord_{talent_id}_{i}"))
        buttons = [btns[j:j+2] for j in range(0, len(btns), 2)]
    else:
        buttons = [[InlineKeyboardButton("Order", callback_data=f"order_{talent_id}")]]
    buttons.append([InlineKeyboardButton("Back", callback_data="back_menu")])

    # Delete old message + clean UI
    chat_id = query.message.chat_id
    try:
        await query.message.delete()
    except Exception:
        pass
    await _clean_ui(chat_id, context)

    # Send photo
    photo_msg = None
    if talent.get("photo"):
        try:
            photo_msg = await context.bot.send_photo(chat_id=chat_id, photo=talent["photo"])
        except Exception:
            pass

    # Send rich template
    tpl = await db.get_template("talent_detail")
    tpl = apply_duration_label(tpl, talent)
    if packages:
        tpl = strip_price_duration_rows(tpl)

    # Build markup as dict (compatible with both rich message and fallback)
    markup_rows = []
    for row in buttons:
        markup_rows.append([{"text": b.text, "callback_data": b.callback_data} for b in row])
    markup_dict = {"inline_keyboard": markup_rows}

    detail_id = await send_template(
        bot, chat_id, tpl,
        markup=markup_dict,
        talent_name=talent["name"],
        desc=talent.get("desc", ""),
        price=price_str,
        duration=duration_display(talent),
    )

    # Track for cleanup
    await _track_ui(chat_id, photo_msg.message_id if photo_msg else None, detail_id)


async def _handle_talent_full(update, context, data):
    query = update.callback_query
    talent_id = data.replace("full_", "")
    talent = await db.get_talent(talent_id)
    if not talent:
        return

    from rich_message import send_template
    from bot_manager import bot as bot_wrapper

    chat_id = query.message.chat_id
    is_sub = await db.is_subscribed(talent_id, query.from_user.id)
    notif_text = "Disable Notifications" if is_sub else "Enable Notifications"

    # Clean + delete old message
    try:
        await query.message.delete()
    except Exception:
        pass
    await _clean_ui(chat_id, context)

    tpl = await db.get_template("talent_full")
    markup_dict = {"inline_keyboard": [
        [{"text": notif_text, "callback_data": f"sub_{talent_id}"}],
        [{"text": "Back", "callback_data": "back_menu"}],
    ]}
    msg_id = await send_template(bot_wrapper, chat_id, tpl, markup=markup_dict, talent_name=talent["name"])
    await _track_ui(chat_id, msg_id)


async def _handle_subscribe(update, context, data):
    query = update.callback_query
    talent_id = data.replace("sub_", "")
    uid = query.from_user.id
    if await db.is_subscribed(talent_id, uid):
        await db.remove_subscriber(talent_id, uid)
    else:
        await db.add_subscriber(talent_id, uid)


async def _handle_back_menu(update, context):
    """Back to talent selection."""
    query = update.callback_query
    chat_id = query.message.chat_id

    # Delete current + clean all UI
    try:
        await query.message.delete()
    except Exception:
        pass
    await _clean_ui(chat_id, context)
    talents = await db.get_talents()
    btns = []
    for t in talents:
        tid = t["id"]
        in_session = await db.get_session_by_talent(tid)
        cooldown_at = await db.get_cooldown(tid)
        is_off = t.get("offline", False)
        if is_off or in_session or (cooldown_at and time.time() < cooldown_at):
            btns.append(InlineKeyboardButton(f"{t['name']} — FULL", callback_data=f"full_{tid}"))
        else:
            btns.append(InlineKeyboardButton(f"{t['name']}", callback_data=f"talent_{tid}"))
    buttons = [btns[i:i+2] for i in range(0, len(btns), 2)]
    template = await db.get_template("welcome")
    clean = re.sub(r'<[^>]+>', '', template).strip() if template else ""

    from rich_message import send_template
    from bot_manager import bot as bot_wrapper

    markup_rows = []
    for row in buttons:
        markup_rows.append([{"text": b.text, "callback_data": b.callback_data} for b in row])
    markup_dict = {"inline_keyboard": markup_rows}

    if clean:
        welcome_id = await send_template(bot_wrapper, chat_id, template, markup=markup_dict)
        await _track_ui(chat_id, welcome_id)
    else:
        msg = await bot_wrapper.send_message(chat_id, "ㅤ", reply_markup=markup_dict)
        if msg:
            await _track_ui(chat_id, getattr(msg, 'message_id', None))


async def _handle_order(update, context, data):
    """Start order process."""
    query = update.callback_query
    talent_id = data.replace("order_", "")
    talent = await db.get_talent(talent_id)
    if not talent:
        return
    await _start_order(update, context, talent)


async def _handle_pkg_order(update, context, data):
    """Package order."""
    raw = data.replace("pord_", "")
    cut = raw.rfind("_")
    talent_id = raw[:cut]
    try:
        idx = int(raw[cut+1:])
    except (ValueError, IndexError):
        return
    talent = await db.get_talent(talent_id)
    if not talent:
        return
    packages = talent.get("packages") or []
    if idx < 0 or idx >= len(packages):
        return
    pkg = packages[idx]
    eff = dict(talent)
    eff["price"] = pkg.get("price", talent.get("price", 0))
    eff["duration"] = pkg.get("duration", talent.get("duration", 0))
    if pkg.get("video_index") is not None:
        eff["_force_video_index"] = pkg["video_index"]
    await _start_order(update, context, eff)


async def _start_order(update, context, talent):
    """Start order — show promo code prompt first, then create invoice."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    talent_id = talent.get("id")

    if talent.get("offline"):
        return

    existing = await db.get_session_by_user(user.id)
    if existing:
        return

    # Clean previous UI (foto talent, detail, dll)
    await _clean_ui(chat_id, context)

    # Simpan state order — tunggu promo input atau skip
    promo_tpl = await db.get_template("promo_prompt")
    promo_text = _strip_unsupported_html(promo_tpl) if promo_tpl else "🎟 <b>Punya promo code?</b>\nKirim kode sekarang atau klik Skip."
    promo_msg = await context.bot.send_message(
        chat_id,
        promo_text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Skip ▶️", callback_data="promo_skip")]
        ])
    )

    admin_state[user.id] = {
        "action": "promo_input",
        "talent": talent,
        "chat_id": chat_id,
        "promo_msg_id": promo_msg.message_id,
    }


async def _proceed_order(context, user_id: int, chat_id: int, talent: dict, promo: dict = None):
    """Create invoice and start payment polling (after promo step)."""
    from payment import create_invoice, check_invoice
    from currency import get_myr_rate
    from session_manager import start_session

    talent_id = talent.get("id")
    price = talent["price"]
    duration = talent["duration"]

    # Apply promo discount
    original_price = price
    if promo:
        if promo["discount_type"] == "percent":
            price = int(price * (100 - promo["discount_value"]) / 100)
        else:
            price = max(0, int(price - promo["discount_value"]))

    myr_rate = await get_myr_rate()
    merchant_ref = f"S-{user_id}-{talent_id}-{int(time.time())}"

    inv_msg = await context.bot.send_message(chat_id, "**Creating invoice...**", parse_mode=ParseMode.MARKDOWN)

    invoice = await create_invoice(
        amount=price, merchant_ref=merchant_ref,
        description=f"{talent['name']} {duration}m",
        customer_name="User", expired_time=3600
    )
    if not invoice:
        await inv_msg.edit_text("Failed to create invoice.")
        return

    invoice_id = invoice["invoice_id"]
    total = invoice["total_amount"]
    nominal = await format_price(total, user_id)

    import base64
    from io import BytesIO
    qr_base64 = invoice.get("payment_info", {}).get("qr_image", "")

    await inv_msg.delete()

    qr_msg = None
    if qr_base64 and "base64" in qr_base64:
        img_data = qr_base64.split(",", 1)[1]
        qr_file = BytesIO(base64.b64decode(img_data))
        qr_file.name = "qris.png"
        qr_msg = await context.bot.send_photo(chat_id, photo=qr_file)

    # Send rich payment template
    from rich_message import send_template, duration_display, apply_duration_label
    from bot_manager import bot as bot_wrapper

    tpl = await db.get_template("payment")
    tpl = apply_duration_label(tpl, talent)
    markup_dict = {"inline_keyboard": [[
        {"text": "Check", "callback_data": f"chk_{invoice_id}"},
        {"text": "Cancel", "callback_data": f"cnl_{invoice_id}"}
    ]]}

    # Tambahkan info diskon di nominal kalau pakai promo
    if promo and original_price != price:
        original_nominal = await format_price(original_price, user_id)
        nominal = f"~{original_nominal}~ → {nominal} (🎟 {promo['code']})"

    pay_msg_id = await send_template(
        bot_wrapper, chat_id, tpl,
        markup=markup_dict,
        invoice_id=invoice_id, talent_name=talent["name"],
        duration=duration_display(talent) if hasattr(talent, 'get') else str(talent.get("duration", "")),
        nominal=nominal,
    )

    await db.add_transaction({
        "invoice_id": invoice_id, "merchant_ref": merchant_ref,
        "user_id": user_id, "user_name": "User",
        "amount": price, "original_amount": original_price,
        "total_amount": total,
        "talent": talent["name"], "status": "PENDING", "created_at": time.time(),
        "promo_code": promo["code"] if promo else None,
    })

    # Increment promo usage
    if promo:
        await db.use_promo(promo["code"])

    # Poll payment in background
    _payment_msgs[invoice_id] = {"chat_id": chat_id, "msg_ids": [qr_msg.message_id if qr_msg else None, pay_msg_id]}
    asyncio.create_task(_poll_payment(context, user_id, invoice_id, chat_id, talent,
                                     [qr_msg.message_id if qr_msg else None, pay_msg_id]))


async def _poll_payment(context, user_id, invoice_id, chat_id, talent, msg_ids):
    """Poll payment status."""
    from payment import check_invoice
    from session_manager import start_session

    try:
        for _ in range(1200):
            await asyncio.sleep(3)
            inv = await check_invoice(invoice_id)
            if not inv:
                continue
            status = inv.get("status")
            if status == "PAID":
                await db.update_transaction(invoice_id, status="PAID")
                # Log payment
                await _log_to_channel("log_channel_payment",
                    f"💰 **Pembayaran Berhasil**\nInvoice: `{invoice_id}`\nUser: `{user_id}`\n"
                    f"Talent: {talent.get('name','-')}\nAmount: Rp {talent.get('price',0):,}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Chat", url=f"tg://user?id={user_id}")]]),
                    context=context)
                # Delete QR + invoice msg
                for mid in msg_ids:
                    if mid:
                        try:
                            await context.bot.delete_message(chat_id, mid)
                        except Exception:
                            pass

                # Step 1: Minta bukti pembayaran (template "paid" + Skip button)
                from rich_message import send_template
                from bot_manager import bot as bot_wrapper
                tpl_paid = await db.get_template("paid")
                paid_markup = {"inline_keyboard": [[{"text": "Skip", "callback_data": f"skip_bukti_{invoice_id}"}]]}
                msg1_id = await send_template(bot_wrapper, chat_id, tpl_paid, markup=paid_markup)

                # Simpan state untuk handler foto bukti
                admin_state[f"bukti_{user_id}"] = {
                    "invoice_id": invoice_id,
                    "talent": talent,
                    "chat_id": chat_id,
                    "msg1_id": msg1_id,
                }

                # Tunggu 60 detik — kalau belum kirim bukti, lanjut otomatis
                await asyncio.sleep(60)

                # Cek apakah sudah diproses (oleh handler foto atau tombol skip)
                if f"bukti_{user_id}" not in admin_state:
                    return  # Sudah diproses

                # Belum kirim bukti, lanjut otomatis
                del admin_state[f"bukti_{user_id}"]
                if msg1_id:
                    try:
                        await context.bot.delete_message(chat_id, msg1_id)
                    except Exception:
                        pass

                # Step 2: Connecting + start session
                tpl_conn = await db.get_template("connecting")
                conn_id = await send_template(bot_wrapper, chat_id, tpl_conn, talent_name=talent["name"])
                await asyncio.sleep(3)
                if conn_id:
                    try:
                        await context.bot.delete_message(chat_id, conn_id)
                    except Exception:
                        pass
                await start_session(user_id, invoice_id, chat_id, talent)
                return
            elif status in ["EXPIRED", "CANCELLED"]:
                await db.update_transaction(invoice_id, status=status)
                for mid in msg_ids:
                    if mid:
                        try:
                            await context.bot.delete_message(chat_id, mid)
                        except Exception:
                            pass
                await context.bot.send_message(chat_id, f"Invoice {status.lower()}.")
                return
    except asyncio.CancelledError:
        pass


async def _handle_check_payment(update, context, data):
    query = update.callback_query
    from payment import check_invoice
    iid = data.replace("chk_", "")
    inv = await check_invoice(iid)
    if inv:
        await query.answer(inv.get("status", "?"), show_alert=True)


async def _handle_cancel_payment(update, context, data):
    query = update.callback_query
    iid = data.replace("cnl_", "")
    chat_id = query.message.chat_id
    await db.update_transaction(iid, status="CANCELLED")

    # Hapus semua pesan invoice (QR + invoice text)
    info = _payment_msgs.pop(iid, None)
    if info:
        for mid in info.get("msg_ids", []):
            if mid:
                try:
                    await context.bot.delete_message(chat_id, mid)
                except Exception:
                    pass
    # Hapus pesan yang punya tombol cancel juga
    try:
        await query.message.delete()
    except Exception:
        pass

    # Kembali ke menu talent
    await query.answer("Order cancelled")
    from rich_message import send_template
    from bot_manager import bot as bot_wrapper
    template = await db.get_template("welcome")
    clean = re.sub(r'<[^>]+>', '', template).strip() if template else ""
    talents = await db.get_talents()
    btns = []
    for t in talents:
        tid = t["id"]
        btns.append(InlineKeyboardButton(f"{t['name']}", callback_data=f"talent_{tid}"))
    buttons = [btns[i:i+2] for i in range(0, len(btns), 2)]
    markup_dict = {"inline_keyboard": [[{"text": b.text, "callback_data": b.callback_data} for b in row] for row in buttons]}
    if clean:
        await send_template(bot_wrapper, chat_id, template, markup=markup_dict)
    else:
        await bot_wrapper.send_message(chat_id, "ㅤ", reply_markup=markup_dict)


async def _handle_refresh_session(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    existing = await db.get_session_by_user(user_id)
    if not existing:
        await query.message.edit_text("Session ended. /start for new.")
        return
    remaining = max(0, existing["end_time"] - time.time())
    await query.message.edit_text(f"**Active Session**\nRemaining: **{int(remaining//60)}m**",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Refresh", callback_data="refresh_session")]]))


async def _handle_skip_bukti(update, context, data):
    query = update.callback_query
    uid = query.from_user.id

    key = f"bukti_{uid}"
    if key not in admin_state:
        await query.answer()
        return

    state = admin_state[key]
    del admin_state[key]

    try:
        await query.message.delete()
    except Exception:
        pass

    # Proceed to connecting + session
    from rich_message import send_template
    from bot_manager import bot as bot_wrapper
    from session_manager import start_session

    tpl_conn = await db.get_template("connecting")
    conn_id = await send_template(bot_wrapper, state["chat_id"], tpl_conn, talent_name=state["talent"]["name"])
    await asyncio.sleep(3)
    if conn_id:
        try:
            await context.bot.delete_message(state["chat_id"], conn_id)
        except Exception:
            pass
    await start_session(uid, state["invoice_id"], state["chat_id"], state["talent"])
    await query.answer()


async def _handle_promo_skip(update, context):
    """User skips promo code — proceed to invoice without discount."""
    query = update.callback_query
    uid = query.from_user.id

    if uid not in admin_state or admin_state[uid].get("action") != "promo_input":
        await query.answer()
        return

    state = admin_state.pop(uid)

    # Hapus pesan promo prompt
    try:
        await query.message.delete()
    except Exception:
        pass

    await _proceed_order(context, uid, state["chat_id"], state["talent"], promo=None)
    await query.answer()


# ============================================================
# PHOTO HANDLER
# ============================================================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages (admin edit photo or bukti pembayaran)."""
    user_id = update.effective_user.id
    message = update.message

    # Cek bukti pembayaran dulu
    bukti_key = f"bukti_{user_id}"
    if bukti_key in admin_state:
        state = admin_state[bukti_key]
        del admin_state[bukti_key]

        # Forward bukti ke channel testimoni (log_channel_payment)
        settings = await db.get_settings()
        payment_channel = settings.get("log_channel_payment", 0)
        if payment_channel:
            try:
                await context.bot.forward_message(chat_id=int(payment_channel), from_chat_id=message.chat_id, message_id=message.message_id)
            except Exception:
                pass

        # Notify admin DM
        admin_ids = await db.get_admin_ids()
        for aid in admin_ids:
            try:
                await context.bot.send_message(chat_id=aid,
                    text=f"💰 **Bukti Pembayaran**\nUser: `{user_id}`\nTalent: {state['talent']['name']}",
                    parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass

        # Hapus pesan minta bukti
        if state.get("msg1_id"):
            try:
                await context.bot.delete_message(state["chat_id"], state["msg1_id"])
            except Exception:
                pass

        processing_msg = await message.reply_text("Payment proof received. Processing...")
        await asyncio.sleep(2)

        # Hapus pesan "Processing..."
        try:
            await processing_msg.delete()
        except Exception:
            pass

        # Proceed to session
        from rich_message import send_template
        from bot_manager import bot as bot_wrapper
        from session_manager import start_session

        tpl_conn = await db.get_template("connecting")
        conn_id = await send_template(bot_wrapper, state["chat_id"], tpl_conn, talent_name=state["talent"]["name"])
        await asyncio.sleep(3)
        if conn_id:
            try:
                await context.bot.delete_message(state["chat_id"], conn_id)
            except Exception:
                pass
        await start_session(user_id, state["invoice_id"], state["chat_id"], state["talent"])
        return

    # Admin photo actions
    if user_id not in admin_state:
        return

    state = admin_state[user_id]

    # Broadcast photo
    if state.get("action") == "broadcast_content":
        del admin_state[user_id]
        photo_id = message.photo[-1].file_id
        caption = message.caption or ""
        asyncio.create_task(_execute_broadcast(context, user_id, message.chat_id,
                                              photo_id=photo_id, caption=caption))
        return

    if state.get("action") == "add_talent" and state.get("step") == "photo":
        state["photo"] = message.photo[-1].file_id
        state["step"] = "name"
        await message.reply_text("Foto OK\n\nKirim **nama:**", parse_mode=ParseMode.MARKDOWN)

    elif state.get("action") == "edit_photo":
        tid = state["talent_id"]
        await db.update_talent(tid, photo=message.photo[-1].file_id)
        del admin_state[user_id]
        await message.reply_text("Foto diubah!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Kembali", callback_data=f"adm_tedit_{tid}")]]))


# ============================================================
# VIDEO / DOCUMENT HANDLER
# ============================================================

async def handle_video_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle video/document upload from admin."""
    user_id = update.effective_user.id
    message = update.message

    if user_id not in admin_state:
        return

    state = admin_state[user_id]
    if state.get("action") not in ["edit_video", "add_talent", "broadcast_content"]:
        return

    # Get file info
    if message.video:
        file_id = message.video.file_id
        filename = message.video.file_name or f"video_{int(time.time())}.mp4"
        length_seconds = message.video.duration
    elif message.document:
        file_id = message.document.file_id
        filename = message.document.file_name or f"video_{int(time.time())}.mp4"
        length_seconds = None
    else:
        return

    # Broadcast video
    if state["action"] == "broadcast_content":
        del admin_state[user_id]
        caption = message.caption or ""
        asyncio.create_task(_execute_broadcast(context, user_id, message.chat_id,
                                              video_id=file_id, caption=caption))
        return

    if state["action"] == "edit_video":
        tid = state["talent_id"]
        # Track jumlah video yang sudah ditambahkan
        count = state.get("_video_count", 0) + 1
        state["_video_count"] = count
        await db.add_video_to_talent(tid, {
            "file_id": file_id, "filename": filename,
            "title": "", "length_seconds": length_seconds,
        })
        # Jangan hapus state — tunggu video lain atau "Selesai"
        await message.reply_text(
            f"✅ Video #{count} ditambahkan: `{filename}`\n\nKirim video lagi atau klik **Selesai**.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai", callback_data=f"adm_tedit_{tid}")]]))

    elif state["action"] == "add_talent" and state.get("step") == "video":
        await db.add_talent({
            "id": f"t_{int(time.time())}",
            "name": state["name"], "photo": state["photo"],
            "desc": state["desc"], "price": state["price"],
            "duration": state["duration"],
            "videos": [{"file_id": file_id, "filename": filename, "title": "", "length_seconds": length_seconds}],
            "video_index": 0,
        })
        del admin_state[user_id]
        await message.reply_text(f"Talent **{state['name']}** ditambahkan!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Kembali", callback_data="adm_talents")]]))


# ============================================================
# TEXT HANDLER
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for admin actions (set price, name, etc) and promo code input."""
    user_id = update.effective_user.id
    message = update.message
    text = message.text.strip()

    if user_id not in admin_state:
        return

    state = admin_state[user_id]
    action = state.get("action")

    # Promo code input (customer or admin)
    if action == "promo_input":
        code = text.upper()
        promo = await db.get_promo(code)

        if not promo or not promo.get("active", True):
            await message.reply_text(
                "❌ Code tidak valid. Kirim lagi atau klik Skip.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Skip ▶️", callback_data="promo_skip")]])
            )
            return

        # Check max uses
        if promo.get("max_uses", 0) > 0 and promo.get("used_count", 0) >= promo["max_uses"]:
            await message.reply_text(
                "❌ Code sudah habis masa pakai. Kirim lagi atau klik Skip.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Skip ▶️", callback_data="promo_skip")]])
            )
            return

        # Check talent restriction
        talent_ids = promo.get("talent_ids", [])
        talent_id = state["talent"].get("id")
        if talent_ids and talent_id not in talent_ids:
            await message.reply_text(
                "❌ Code ini tidak berlaku untuk talent ini. Kirim lagi atau klik Skip.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Skip ▶️", callback_data="promo_skip")]])
            )
            return

        # Valid promo — proceed with discount
        chat_id = state["chat_id"]
        talent = state["talent"]
        promo_msg_id = state.get("promo_msg_id")
        del admin_state[user_id]

        # Hapus pesan promo prompt
        if promo_msg_id:
            try:
                await context.bot.delete_message(chat_id, promo_msg_id)
            except Exception:
                pass

        # Hapus pesan user (kode yang dikirim)
        try:
            await message.delete()
        except Exception:
            pass

        # Show confirmation dengan harga sesuai negara
        if promo["discount_type"] == "percent":
            disc_text = f"{int(promo['discount_value'])}%"
        else:
            disc_text = await format_price(int(promo['discount_value']), user_id)

        applied_tpl = await db.get_template("promo_applied")
        applied_raw = (applied_tpl or "✅ Promo <b>{code}</b> applied! Diskon: {discount}").replace("{code}", code).replace("{discount}", disc_text)
        applied_text = _strip_unsupported_html(applied_raw)

        confirm_msg = await context.bot.send_message(
            chat_id,
            applied_text,
            parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(1.5)

        # Hapus pesan konfirmasi juga
        try:
            await confirm_msg.delete()
        except Exception:
            pass

        await _proceed_order(context, user_id, chat_id, talent, promo=promo)
        return

    if not await is_admin(user_id):
        admin_state.pop(user_id, None)
        return

    state = admin_state[user_id]
    action = state.get("action")

    # Add talent flow
    if action == "add_talent":
        step = state.get("step")
        if step == "name":
            state["name"] = text
            state["step"] = "desc"
            await message.reply_text("Kirim **deskripsi:**", parse_mode=ParseMode.MARKDOWN)
        elif step == "desc":
            state["desc"] = text
            state["step"] = "price"
            await message.reply_text("Kirim **harga:**", parse_mode=ParseMode.MARKDOWN)
        elif step == "price":
            if not text.isdigit():
                return await message.reply_text("Angka:")
            state["price"] = int(text)
            state["step"] = "duration"
            await message.reply_text("Kirim **durasi (menit):**", parse_mode=ParseMode.MARKDOWN)
        elif step == "duration":
            try:
                dur = float(text.replace(",", "."))
                if dur <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                return await message.reply_text("Angka:")
            state["duration"] = int(dur) if dur == int(dur) else dur
            state["step"] = "video"
            await message.reply_text("Kirim/forward **video:**", parse_mode=ParseMode.MARKDOWN)
        return

    # Edit field
    if action == "edit_field":
        tid = state["talent_id"]
        field = state["field"]
        if field in ["price", "duration"]:
            try:
                val = int(text) if field == "price" else float(text.replace(",", "."))
            except (ValueError, TypeError):
                return await message.reply_text("Angka:")
            await db.update_talent(tid, **{field: val})
        else:
            await db.update_talent(tid, **{field: text})
        del admin_state[user_id]
        await message.reply_text(f"`{field}` → **{text}**", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Kembali", callback_data=f"adm_tedit_{tid}")]]))
        return

    # Set price/duration globally
    if action == "set_price":
        if not text.isdigit():
            return await message.reply_text("Angka:")
        await db.update_settings(price=int(text))
        del admin_state[user_id]
        await message.reply_text(f"Harga: Rp {int(text):,}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Kembali", callback_data="adm_setting")]]))
    elif action == "set_duration":
        if not text.isdigit():
            return await message.reply_text("Angka:")
        await db.update_settings(duration=int(text))
        del admin_state[user_id]
        await message.reply_text(f"Durasi: {text}m",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Kembali", callback_data="adm_setting")]]))
    elif action == "add_admin":
        if not text.isdigit():
            return await message.reply_text("User ID angka:")
        await db.add_admin(int(text))
        del admin_state[user_id]
        await message.reply_text(f"Admin ditambahkan: `{text}`", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Kembali", callback_data="adm_setting")]]))

    # Broadcast — admin mengirim konten broadcast
    elif action == "broadcast_content":
        # Text-only broadcast
        del admin_state[user_id]
        asyncio.create_task(_execute_broadcast(context, user_id, message.chat_id, text=text))


# ============================================================
# BROADCAST FEATURE
# ============================================================

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /broadcast command (admin only).
    
    Usage:
    - /broadcast (reply ke pesan) → langsung broadcast pesan yang di-reply
    - /broadcast (tanpa reply) → masuk mode input, kirim konten berikutnya
    """
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        return

    message = update.message
    chat_id = message.chat_id

    # Kalau reply ke pesan → langsung broadcast pesan itu via copy_message
    if message.reply_to_message:
        reply_msg = message.reply_to_message
        asyncio.create_task(_execute_broadcast_copy(
            context, user_id, chat_id, reply_msg.chat_id, reply_msg.message_id
        ))
        return

    # Tanpa reply → masuk mode input
    admin_state[user_id] = {"action": "broadcast_content"}
    await message.reply_text(
        "📢 **Broadcast**\n\n"
        "Kirim pesan yang ingin di-broadcast ke semua user.\n\n"
        "Support: teks, foto + caption, video + caption.\n"
        "Atau reply `/broadcast` ke pesan yang ingin di-forward.\n\n"
        "Kirim pesan sekarang atau /start untuk batal.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="adm_menu")]])
    )


async def _execute_broadcast_copy(context, admin_id: int, admin_chat_id: int,
                                  from_chat_id: int, message_id: int):
    """Broadcast via copy_message — support semua jenis pesan (teks, foto, video, sticker, dll)."""
    user_ids = await db.get_all_user_ids()
    total = len(user_ids)

    if total == 0:
        await context.bot.send_message(admin_chat_id, "❌ Tidak ada user terdaftar.")
        return

    progress_msg = await context.bot.send_message(
        admin_chat_id,
        f"📢 Broadcasting ke {total} user...\n⏳ 0/{total}",
    )

    success = 0
    failed = 0
    blocked = 0

    for i, uid in enumerate(user_ids):
        if uid == admin_id:
            success += 1
            continue

        try:
            await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=from_chat_id,
                message_id=message_id,
            )
            success += 1
        except Exception as e:
            err_str = str(e).lower()
            if "blocked" in err_str or "deactivated" in err_str or "not found" in err_str:
                blocked += 1
            else:
                failed += 1

        await asyncio.sleep(0.05)

        if (i + 1) % 50 == 0:
            try:
                await progress_msg.edit_text(
                    f"📢 Broadcasting...\n⏳ {i+1}/{total}\n✅ {success} | ❌ {failed} | 🚫 {blocked}"
                )
            except Exception:
                pass

    report = (
        f"📢 **Broadcast Selesai**\n\n"
        f"👥 Total: {total}\n"
        f"✅ Terkirim: {success}\n"
        f"🚫 Blocked/deactivated: {blocked}\n"
        f"❌ Gagal: {failed}"
    )
    try:
        await progress_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await context.bot.send_message(admin_chat_id, report, parse_mode=ParseMode.MARKDOWN)

    await db.save_broadcast({
        "admin_id": admin_id,
        "type": "copy",
        "content": "(replied message)",
        "total": total,
        "success": success,
        "blocked": blocked,
        "failed": failed,
        "created_at": time.time(),
    })
    await db.log_activity("broadcast_sent", category="admin", user_id=admin_id,
                          details={"total": total, "success": success, "blocked": blocked, "failed": failed, "method": "reply"})


async def _execute_broadcast(context, admin_id: int, admin_chat_id: int, text: str = None,
                             photo_id: str = None, video_id: str = None, caption: str = None):
    """Execute broadcast to all users with delay and progress report."""
    user_ids = await db.get_all_user_ids()
    total = len(user_ids)

    if total == 0:
        await context.bot.send_message(admin_chat_id, "❌ Tidak ada user terdaftar.")
        return

    progress_msg = await context.bot.send_message(
        admin_chat_id,
        f"📢 Broadcasting ke {total} user...\n⏳ 0/{total}",
        parse_mode=ParseMode.MARKDOWN
    )

    success = 0
    failed = 0
    blocked = 0

    for i, uid in enumerate(user_ids):
        # Skip admin sendiri
        if uid == admin_id:
            success += 1
            continue

        try:
            if photo_id:
                await context.bot.send_photo(chat_id=uid, photo=photo_id, caption=caption,
                                             parse_mode=ParseMode.HTML)
            elif video_id:
                await context.bot.send_video(chat_id=uid, video=video_id, caption=caption,
                                             parse_mode=ParseMode.HTML)
            elif text:
                await context.bot.send_message(chat_id=uid, text=text, parse_mode=ParseMode.HTML)
            success += 1
        except Exception as e:
            err_str = str(e).lower()
            if "blocked" in err_str or "deactivated" in err_str or "not found" in err_str:
                blocked += 1
            else:
                failed += 1

        # Rate limit: 0.05s delay per message
        await asyncio.sleep(0.05)

        # Update progress every 50 users
        if (i + 1) % 50 == 0:
            try:
                await progress_msg.edit_text(
                    f"📢 Broadcasting...\n⏳ {i+1}/{total}\n✅ {success} | ❌ {failed} | 🚫 {blocked}"
                )
            except Exception:
                pass

    # Final report
    report = (
        f"📢 **Broadcast Selesai**\n\n"
        f"👥 Total: {total}\n"
        f"✅ Terkirim: {success}\n"
        f"🚫 Blocked/deactivated: {blocked}\n"
        f"❌ Gagal: {failed}"
    )
    try:
        await progress_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await context.bot.send_message(admin_chat_id, report, parse_mode=ParseMode.MARKDOWN)

    # Save to DB
    await db.save_broadcast({
        "admin_id": admin_id,
        "type": "photo" if photo_id else ("video" if video_id else "text"),
        "content": caption or text or "",
        "total": total,
        "success": success,
        "blocked": blocked,
        "failed": failed,
        "created_at": time.time(),
    })
    await db.log_activity("broadcast_sent", category="admin", user_id=admin_id,
                          details={"total": total, "success": success, "blocked": blocked, "failed": failed})
