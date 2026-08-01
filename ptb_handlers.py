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
        text = (f"**{talent['name']}**\n\n"
                f"Status: {'OFFLINE' if talent.get('offline') else 'ONLINE'}\n"
                f"Harga: Rp {talent['price']:,} | Durasi: {talent['duration']}m | CD: {talent.get('cooldown',0)}m\n\n"
                f"**Paket ({len(pkgs)}):**\n{pkg_text}\n\n"
                f"**Video ({len(videos)}):**\n{videos_text}")
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
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
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

    from currency import get_myr_rate
    from rich_message import send_template, duration_display, apply_duration_label, strip_price_duration_rows
    from bot_manager import bot

    myr_rate = await get_myr_rate()
    price_str = f"{talent['price']:,} IDR / {talent['price']/myr_rate:.2f} MYR"

    packages = talent.get("packages") or []
    if packages:
        btns = []
        for i, p in enumerate(packages):
            lbl = (p.get("label") or "").strip() or f"{p.get('duration',0)} min"
            pkg_price = int(p.get('price', 0))
            btns.append(InlineKeyboardButton(f"{lbl} — Rp {pkg_price:,}",
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
    """Create invoice and start payment polling."""
    from payment import create_invoice, check_invoice
    from currency import get_myr_rate
    from session_manager import start_session

    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    talent_id = talent.get("id")

    if talent.get("offline"):
        return

    existing = await db.get_session_by_user(user.id)
    if existing:
        return

    myr_rate = await get_myr_rate()
    price = talent["price"]
    duration = talent["duration"]
    merchant_ref = f"S-{user.id}-{talent_id}-{int(time.time())}"

    # Clean previous UI (foto talent, detail, dll)
    await _clean_ui(chat_id, context)

    inv_msg = await context.bot.send_message(chat_id, "**Creating invoice...**", parse_mode=ParseMode.MARKDOWN)

    invoice = await create_invoice(
        amount=price, merchant_ref=merchant_ref,
        description=f"{talent['name']} {duration}m",
        customer_name=user.first_name or "User", expired_time=3600
    )
    if not invoice:
        await inv_msg.edit_text("Failed to create invoice.")
        return

    invoice_id = invoice["invoice_id"]
    total = invoice["total_amount"]
    nominal = f"{total:,} IDR / {total/myr_rate:.2f} MYR"

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
    pay_msg_id = await send_template(
        bot_wrapper, chat_id, tpl,
        markup=markup_dict,
        invoice_id=invoice_id, talent_name=talent["name"],
        duration=duration_display(talent) if hasattr(talent, 'get') else str(talent.get("duration", "")),
        nominal=nominal,
    )

    await db.add_transaction({
        "invoice_id": invoice_id, "merchant_ref": merchant_ref,
        "user_id": user.id, "user_name": user.first_name,
        "amount": price, "total_amount": total,
        "talent": talent["name"], "status": "PENDING", "created_at": time.time()
    })

    # Poll payment in background
    asyncio.create_task(_poll_payment(context, user.id, invoice_id, chat_id, talent,
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
                # Proceed to session
                from rich_message import send_template
                from bot_manager import bot as bot_wrapper
                tpl_connecting = await db.get_template("connecting")
                connecting_id = await send_template(bot_wrapper, chat_id, tpl_connecting, talent_name=talent["name"])
                await asyncio.sleep(3)
                if connecting_id:
                    try:
                        await context.bot.delete_message(chat_id, connecting_id)
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
    await db.update_transaction(iid, status="CANCELLED")
    try:
        await query.message.delete()
    except Exception:
        pass


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
    try:
        await query.message.delete()
    except Exception:
        pass


# ============================================================
# PHOTO HANDLER
# ============================================================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages (admin edit photo or bukti pembayaran)."""
    user_id = update.effective_user.id
    message = update.message

    if user_id not in admin_state:
        return

    state = admin_state[user_id]

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
    if state.get("action") not in ["edit_video", "add_talent"]:
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

    if state["action"] == "edit_video":
        tid = state["talent_id"]
        await db.add_video_to_talent(tid, {
            "file_id": file_id, "filename": filename,
            "title": "", "length_seconds": length_seconds,
        })
        del admin_state[user_id]
        await message.reply_text(f"Video ditambahkan: `{filename}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Kembali", callback_data=f"adm_tedit_{tid}")]]))

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
    """Handle text input for admin actions (set price, name, etc)."""
    user_id = update.effective_user.id
    message = update.message
    text = message.text.strip()

    if user_id not in admin_state:
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
