"""
Customer Flow Handlers - talent selection, ordering, payment
"""

import asyncio
import time
import base64
import logging
from io import BytesIO

from pyrogram import filters, enums
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from bot_manager import bot
from session_manager import start_session
from payment import create_invoice, check_invoice
from handlers.admin import is_admin, admin_state
from rich_message import render_template, send_template, duration_display, apply_duration_label, strip_price_duration_rows
from currency import get_myr_rate
import database as db

logger = logging.getLogger(__name__)

# Active payment polling tasks
polling_tasks = {}
invoice_messages = {}  # invoice_id -> list of message ids (QR photo + invoice)

# Pesan UI terakhir per chat (welcome, foto talent, detail) — dihapus saat navigasi
# supaya chat customer selalu bersih. Disimpan di MongoDB agar tahan restart/redeploy
# (sebelumnya in-memory: setelah redeploy, foto talent lama tidak ikut terhapus).


async def track_ui(chat_id, *msg_ids):
    """Catat message id UI untuk dibersihkan pada navigasi berikutnya."""
    await db.track_ui_messages(chat_id, [m for m in msg_ids if m])


async def _delete_ids(chat_id, ids):
    """Hapus batch; kalau gagal, coba satu-satu supaya satu id rusak tidak menyisakan yang lain."""
    try:
        await bot.delete_messages(chat_id, ids)
    except Exception:
        for mid in ids:
            try:
                await bot.delete_messages(chat_id, mid)
            except Exception as e:
                logger.warning(f"clean_ui: gagal hapus msg {mid} di chat {chat_id}: {e}")


async def clean_ui(chat_id):
    """Hapus semua pesan UI lama yang tercatat di chat ini."""
    ids = await db.pop_ui_messages(chat_id)
    if ids:
        await _delete_ids(chat_id, ids)


async def clean_ui_except(chat_id, keep_msg_id):
    """Hapus pesan UI lama kecuali satu (mis. pesan yang mau di-edit sebagai fallback)."""
    ids = [m for m in await db.pop_ui_messages(chat_id) if m != keep_msg_id]
    if ids:
        await _delete_ids(chat_id, ids)
    await db.set_ui_messages(chat_id, [keep_msg_id])


async def send_welcome_menu(client, chat_id):
    """Kirim menu awal (daftar talent) + lacak untuk dibersihkan saat navigasi.

    Returns message_id atau None kalau tidak ada talent.
    """
    talents = await db.get_talents()
    if not talents:
        return None
    btns = []
    for t in talents:
        tid = t["id"]
        in_session = await db.get_session_by_talent(tid)
        cooldown_at = await db.get_cooldown(tid)
        is_off = t.get("offline", False)
        if is_off or in_session or (cooldown_at and time.time() < cooldown_at):
            btns.append(InlineKeyboardButton(
                f"{t['name']} — FULL",
                callback_data=f"full_{tid}"
            ))
        else:
            btns.append(InlineKeyboardButton(
                f"{t['name']}",
                callback_data=f"talent_{tid}"
            ))
    # Susun max 2 tombol per baris supaya rapi saat talent banyak
    buttons = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    welcome_id = await send_template(
        client, chat_id,
        await db.get_template("welcome"),
        markup=InlineKeyboardMarkup(buttons),
    )
    await track_ui(chat_id, welcome_id)
    return welcome_id


def register_customer_handlers():
    """Register all customer flow handlers on the bot."""

    @bot.on_message(filters.command("start") & filters.private)
    async def cmd_start(client, message: Message):
        user_id = message.from_user.id
        admin_state.pop(user_id, None)

        # Bersihkan sisa pesan UI dari navigasi sebelumnya
        await clean_ui(message.chat.id)

        settings = await db.get_settings()
        if not settings.get("admin_ids"):
            await db.add_admin(user_id)
            await db.log_activity("first_admin_registered", category="admin", user_id=user_id)

        await db.log_activity("bot_start", category="user", user_id=user_id, details={"name": message.from_user.first_name})

        # Check if user has active session
        existing_session = await db.get_session_by_user(user_id)
        if existing_session and not await is_admin(user_id):
            remaining = max(0, existing_session["end_time"] - time.time())
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            talent = await db.get_talent(existing_session.get("talent_id", ""))
            talent_name = talent["name"] if talent else "Video"
            await message.reply_text(
                f"**Active Session**\n\n"
                f"Talent: **{talent_name}**\n"
                f"Remaining: **{mins}m {secs}s**\n\n"
                f"The session will end automatically. Please wait until it finishes.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Refresh", callback_data="refresh_session")]
                ])
            )
            return

        if await is_admin(user_id):
            await message.reply_text(
                "**Admin Panel**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Talent", callback_data="adm_talents"),
                     InlineKeyboardButton("Status", callback_data="adm_status")],
                    [InlineKeyboardButton("Transaksi", callback_data="adm_txn"),
                     InlineKeyboardButton("Setting", callback_data="adm_setting")],
                ])
            )
        else:
            welcome_id = await send_welcome_menu(client, message.chat.id)
            if not welcome_id:
                await message.reply_text("No talents available. Please try again later.")

    @bot.on_callback_query(filters.regex("^refresh_session$"))
    async def cb_refresh_session(client, callback: CallbackQuery):
        user_id = callback.from_user.id
        existing_session = await db.get_session_by_user(user_id)
        if not existing_session:
            await callback.answer("Session has ended!", show_alert=True)
            await callback.message.edit_text("Your session has ended.\n\nSend /start for a new session.")
            return
        remaining = max(0, existing_session["end_time"] - time.time())
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        talent = await db.get_talent(existing_session.get("talent_id", ""))
        talent_name = talent["name"] if talent else "Video"
        await callback.message.edit_text(
            f"**Active Session**\n\n"
            f"Talent: **{talent_name}**\n"
            f"Remaining: **{mins}m {secs}s**\n\n"
            f"The session will end automatically. Please wait until it finishes.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Refresh", callback_data="refresh_session")]
            ])
        )
        await callback.answer()

    @bot.on_callback_query(filters.regex("^talent_"))
    async def cb_talent(client, callback: CallbackQuery):
        talent_id = callback.data.replace("talent_", "")
        talent = await db.get_talent(talent_id)
        if not talent:
            return await callback.answer("", show_alert=True)

        # Hapus pesan sebelumnya (daftar talent) + sisa UI lama
        try:
            await callback.message.delete()
        except Exception:
            pass
        await clean_ui(callback.message.chat.id)

        myr_rate = await get_myr_rate()
        myr_price = talent["price"] / myr_rate
        price_str = f"{talent['price']:,} IDR / {myr_price:.2f} MYR"

        # Paket durasi (opsional). Kalau ada, tampilkan pilihan per paket (2 tombol/baris);
        # kalau tidak, pakai tombol Order tunggal seperti biasa.
        packages = talent.get("packages") or []
        if packages:
            vids = talent.get("videos") or []
            pkg_btns = []
            for i, p in enumerate(packages):
                lbl = (p.get("label") or "").strip()
                if not lbl:
                    # Fallback label: judul video terikat, atau durasi
                    vi = p.get("video_index")
                    if vi is not None and 0 <= vi < len(vids) and isinstance(vids[vi], dict):
                        lbl = (vids[vi].get("title") or "").strip()
                    if not lbl:
                        lbl = f"{p.get('duration', 0)} min"
                pkg_price = int(p.get('price', 0))
                pkg_myr = pkg_price / myr_rate
                pkg_btns.append(InlineKeyboardButton(
                    f"{lbl} — Rp {pkg_price:,} / {pkg_myr:.2f} MYR",
                    callback_data=f"pord_{talent_id}_{i}"
                ))
            buttons = [pkg_btns[j:j + 2] for j in range(0, len(pkg_btns), 2)]
            buttons.append([InlineKeyboardButton("Back", callback_data="back_menu")])
        else:
            buttons = [
                [InlineKeyboardButton("Order", callback_data=f"order_{talent_id}")],
                [InlineKeyboardButton("Back", callback_data="back_menu")],
            ]

        # Kirim foto dulu (tanpa caption), lalu rich message template di bawahnya
        # (caption foto tidak mendukung rich/tabel — pola sama seperti QR + invoice)
        photo_msg = None
        try:
            photo_msg = await client.send_photo(
                callback.message.chat.id,
                photo=talent["photo"],
            )
        except Exception:
            pass

        tpl = await db.get_template("talent_detail")
        # duration_label (kalau ada) tampil menggantikan angka durasi asli
        tpl = apply_duration_label(tpl, talent)
        # Talent dengan paket: sembunyikan baris Price/Duration tunggal —
        # harga & durasi sudah tampil di tombol paket, biar tidak dobel/beda.
        if packages:
            tpl = strip_price_duration_rows(tpl)
        detail_id = await send_template(
            client, callback.message.chat.id, tpl,
            markup=InlineKeyboardMarkup(buttons),
            talent_name=talent["name"],
            desc=talent.get("desc", ""),
            price=price_str,
            duration=duration_display(talent),
        )
        # Lacak foto + detail supaya terhapus saat Back/Order/navigasi lain
        await track_ui(callback.message.chat.id, photo_msg.id if photo_msg else None, detail_id)
        await callback.answer()

    @bot.on_callback_query(filters.regex("^full_"))
    async def cb_full(client, callback: CallbackQuery):
        talent_id = callback.data.replace("full_", "")
        talent = await db.get_talent(talent_id)
        if not talent:
            return await callback.answer("", show_alert=True)

        is_sub = await db.is_subscribed(talent_id, callback.from_user.id)
        notif_text = "Disable Notifications" if is_sub else "Enable Notifications"

        tpl = await db.get_template("talent_full")

        # Rich message tidak bisa edit pesan lama — kirim baru + hapus lama
        full_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(notif_text, callback_data=f"sub_{talent_id}")],
            [InlineKeyboardButton("Back", callback_data="back_menu")],
        ])
        await clean_ui_except(callback.message.chat.id, callback.message.id)
        msg_id = await send_template(
            client, callback.message.chat.id, tpl,
            markup=full_markup,
            talent_name=talent["name"],
        )
        if msg_id:
            await track_ui(callback.message.chat.id, msg_id)
            try:
                await callback.message.delete()
            except Exception:
                pass
        else:
            await callback.message.edit_text(
                render_template(tpl, talent_name=talent["name"]),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=full_markup
            )

    @bot.on_callback_query(filters.regex("^sub_"))
    async def cb_sub(client, callback: CallbackQuery):
        talent_id = callback.data.replace("sub_", "")
        uid = callback.from_user.id
        if await db.is_subscribed(talent_id, uid):
            await db.remove_subscriber(talent_id, uid)
            await db.log_activity("unsubscribe_talent", category="user", user_id=uid, details={"talent_id": talent_id})
            await callback.answer("Notifications disabled.", show_alert=True)
            notif_text = "Enable Notifications"
        else:
            await db.add_subscriber(talent_id, uid)
            await db.log_activity("subscribe_talent", category="user", user_id=uid, details={"talent_id": talent_id})
            await callback.answer("You'll be notified when available!", show_alert=True)
            notif_text = "Disable Notifications"
        try:
            await callback.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(notif_text, callback_data=f"sub_{talent_id}")],
                    [InlineKeyboardButton("Back", callback_data="back_menu")],
                ])
            )
        except Exception:
            pass

    @bot.on_callback_query(filters.regex("^back_menu$"))
    async def cb_back_menu(client, callback: CallbackQuery):
        # Hapus pesan detail talent + semua sisa UI (foto, dll) — chat tetap bersih
        try:
            await callback.message.delete()
        except Exception:
            pass
        await clean_ui(callback.message.chat.id)

        await send_welcome_menu(client, callback.message.chat.id)
        await callback.answer()

    @bot.on_callback_query(filters.regex("^order_"))
    async def cb_order(client, callback: CallbackQuery):
        talent_id = callback.data.replace("order_", "")
        talent = await db.get_talent(talent_id)
        if not talent:
            return await callback.answer("", show_alert=True)
        await _start_order(client, callback, talent)

    @bot.on_callback_query(filters.regex("^pord_"))
    async def cb_pkg_order(client, callback: CallbackQuery):
        # pord_{talent_id}_{index} — talent_id boleh mengandung underscore, index di akhir
        raw = callback.data.replace("pord_", "")
        cut = raw.rfind("_")
        talent_id = raw[:cut]
        try:
            idx = int(raw[cut + 1:])
        except (ValueError, IndexError):
            return await callback.answer("", show_alert=True)
        talent = await db.get_talent(talent_id)
        if not talent:
            return await callback.answer("", show_alert=True)
        packages = talent.get("packages") or []
        if idx < 0 or idx >= len(packages):
            return await callback.answer("Paket tidak tersedia.", show_alert=True)
        pkg = packages[idx]
        # Salin talent lalu override harga/durasi sesuai paket —
        # seluruh rantai downstream (invoice, sesi, timer) tetap tak berubah.
        eff = dict(talent)
        eff["price"] = pkg.get("price", talent.get("price", 0))
        eff["duration"] = pkg.get("duration", talent.get("duration", 0))
        lbl = (pkg.get("label") or "").strip()
        if lbl:
            eff["duration_label"] = lbl
        # Kalau paket terikat video tertentu, paksa video itu (bukan rotation)
        vidx = pkg.get("video_index")
        if vidx is not None:
            eff["_force_video_index"] = vidx
        await _start_order(client, callback, eff)

    @bot.on_callback_query(filters.regex("^chk_"))
    async def cb_chk(client, cb: CallbackQuery):
        inv = await check_invoice(cb.data.replace("chk_", ""))
        if not inv:
            return await cb.answer("", show_alert=True)
        msgs = {
            "PAID": "Paid!",
            "PENDING": "Not paid yet",
            "EXPIRED": "Expired"
        }
        await cb.answer(msgs.get(inv.get("status"), inv.get("status")), show_alert=True)

    @bot.on_callback_query(filters.regex("^cnl_"))
    async def cb_cnl(client, cb: CallbackQuery):
        iid = cb.data.replace("cnl_", "")
        if iid in polling_tasks:
            polling_tasks[iid].cancel()
            del polling_tasks[iid]
        # Hapus semua pesan invoice (foto QR + rich message)
        msg_ids = invoice_messages.pop(iid, None)
        if msg_ids:
            try:
                await bot.delete_messages(cb.message.chat.id, msg_ids)
            except Exception:
                pass
        try:
            await cb.message.delete()
        except Exception:
            pass
        # Tandai transaksi batal + catat aktivitas
        try:
            await db.update_transaction(iid, status="CANCELLED")
            await db.log_activity("payment_cancelled_by_user", category="payment", user_id=cb.from_user.id, details={"invoice_id": iid})
        except Exception:
            pass
        # Balik otomatis ke menu pilih talent (seperti /start)
        await clean_ui(cb.message.chat.id)
        await send_welcome_menu(client, cb.message.chat.id)
        await cb.answer("Order cancelled")


async def _start_order(client, callback: CallbackQuery, talent: dict):
    """Buat invoice + mulai polling pembayaran.

    `talent` bisa versi dasar (harga/durasi tunggal) atau salinan dengan harga/durasi
    paket yang sudah di-override — logika di bawah tidak peduli asalnya.
    """
    talent_id = talent.get("id")

    if talent.get("offline"):
        return await callback.answer("Not available.", show_alert=True)

    user = callback.from_user

    # Limit 1 session per user
    existing = await db.get_session_by_user(user.id)
    if existing:
        return await callback.answer("You still have an active session!", show_alert=True)

    # Cooldown check
    cd = await db.get_cooldown(talent_id)
    if cd and time.time() < cd:
        return await callback.answer("Currently serving another customer.", show_alert=True)

    myr_rate = await get_myr_rate()
    price = talent["price"]
    duration = talent["duration"]
    merchant_ref = f"S-{user.id}-{talent_id}-{int(time.time())}"

    # Hapus pesan detail talent + foto (semua UI lama) — chat bersih saat invoice muncul
    try:
        await callback.message.delete()
    except Exception:
        pass
    await clean_ui(callback.message.chat.id)

    inv_msg = await client.send_message(callback.message.chat.id, "**Creating invoice...**")

    # Deskripsi invoice ikut pakai label durasi (kalau ada) supaya angka asli tidak bocor
    desc_dur = (talent.get("duration_label") or "").strip() or f"{duration}m"
    invoice = await create_invoice(
        amount=price,
        merchant_ref=merchant_ref,
        description=f"{talent['name']} {desc_dur}",
        customer_name=user.first_name or "User",
        expired_time=3600
    )
    if not invoice:
        return await client.send_message(callback.message.chat.id, "Failed to create invoice. Please try again.")

    invoice_id = invoice["invoice_id"]
    total = invoice["total_amount"]
    # Tampilkan nominal dalam dua mata uang: IDR dan MYR
    nominal_str = f"{total:,} IDR / {total / myr_rate:.2f} MYR"
    qr_base64 = invoice.get("payment_info", {}).get("qr_image", "")

    # Hapus pesan "Membuat invoice..."
    try:
        await inv_msg.delete()
    except Exception:
        pass

    qr_photo_msg = None
    if qr_base64 and "base64" in qr_base64:
        img_data = qr_base64.split(",", 1)[1]
        qr_file = BytesIO(base64.b64decode(img_data))
        qr_file.name = "qris.png"
        # Kirim foto QR dulu (tanpa caption)
        qr_photo_msg = await client.send_photo(
            callback.message.chat.id,
            photo=qr_file,
        )

    # Kirim rich message payment template + tombol (fallback otomatis)
    tpl = await db.get_template("payment")
    tpl = apply_duration_label(tpl, talent)
    qr_msg_id = await send_template(
        client, callback.message.chat.id, tpl,
        markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Check Status", callback_data=f"chk_{invoice_id}"),
             InlineKeyboardButton("Cancel", callback_data=f"cnl_{invoice_id}")]
        ]),
        invoice_id=invoice_id, talent_name=talent["name"], duration=duration_display(talent), nominal=nominal_str,
    )

    # Simpan kedua message id untuk dihapus nanti
    qr_msg_ids = []
    if qr_photo_msg:
        qr_msg_ids.append(qr_photo_msg.id)
    if qr_msg_id:
        qr_msg_ids.append(qr_msg_id)
    invoice_messages[invoice_id] = qr_msg_ids

    await db.add_transaction({
        "invoice_id": invoice_id,
        "merchant_ref": merchant_ref,
        "user_id": user.id,
        "user_name": user.first_name,
        "amount": price,
        "total_amount": total,
        "talent": talent["name"],
        "status": "PENDING",
        "created_at": time.time()
    })
    await db.log_activity("order_created", category="payment", user_id=user.id, details={
        "invoice_id": invoice_id,
        "talent": talent["name"],
        "amount": price,
        "total_amount": total,
    })

    task = asyncio.create_task(
        poll_payment(user.id, invoice_id, callback.message.chat.id, talent, qr_msg_ids)
    )
    polling_tasks[invoice_id] = task


async def poll_payment(user_id: int, invoice_id: str, chat_id: int, talent: dict, qr_msg_ids: list = None):
    """Poll payment status until paid/expired."""
    try:
        for _ in range(1200):
            await asyncio.sleep(3)
            inv = await check_invoice(invoice_id)
            if not inv:
                continue
            status = inv.get("status")
            if status == "PAID":
                await db.update_transaction(invoice_id, status="PAID")
                await db.log_activity("payment_paid", category="payment", user_id=user_id, details={
                    "invoice_id": invoice_id,
                    "talent": talent.get("name", ""),
                })
                # Delete QR photo + invoice message
                if qr_msg_ids:
                    try:
                        await bot.delete_messages(chat_id, qr_msg_ids)
                    except Exception:
                        pass

                # Step 1: Minta bukti pembayaran (rich message + fallback)
                msg1_id = await send_template(
                    bot, chat_id,
                    await db.get_template("paid"),
                    markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Skip", callback_data=f"skip_bukti_{invoice_id}")]
                    ]),
                )

                # Tunggu foto bukti max 60 detik, atau tombol "Lewati" diklik
                # Simpan state supaya handler foto bisa detect
                from handlers.admin import admin_state
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
                        await bot.delete_messages(chat_id, msg1_id)
                    except Exception:
                        pass

                await _proceed_after_bukti(user_id, invoice_id, chat_id, talent)
                return
            elif status in ["EXPIRED", "CANCELLED"]:
                await db.update_transaction(invoice_id, status=status)
                await db.log_activity(f"payment_{status.lower()}", category="payment", user_id=user_id, details={
                    "invoice_id": invoice_id,
                    "talent": talent.get("name", ""),
                })
                if qr_msg_ids:
                    try:
                        await bot.delete_messages(chat_id, qr_msg_ids)
                    except Exception:
                        pass
                await bot.send_message(chat_id, f"Invoice {status.lower()}. Choose a talent to order again.")
                # Langsung tampilkan menu pilih talent lagi
                try:
                    await clean_ui(chat_id)
                    await send_welcome_menu(bot, chat_id)
                except Exception:
                    pass
                return
    except asyncio.CancelledError:
        pass
    finally:
        polling_tasks.pop(invoice_id, None)
        invoice_messages.pop(invoice_id, None)


async def _proceed_after_bukti(user_id, invoice_id, chat_id, talent):
    """Lanjut proses setelah bukti diterima atau di-skip."""
    tpl = await db.get_template("connecting")
    msg2_id = await send_template(bot, chat_id, tpl, talent_name=talent["name"])
    await asyncio.sleep(6)

    if msg2_id:
        try:
            await bot.delete_messages(chat_id, msg2_id)
        except Exception:
            pass

    await start_session(user_id, invoice_id, chat_id, talent)


def register_bukti_handlers():
    """Register handlers for bukti pembayaran (foto) and skip button."""

    @bot.on_message(filters.photo & filters.private)
    async def handle_bukti_photo(client, message: Message):
        """Customer kirim foto bukti → forward ke admin → lanjut proses."""
        uid = message.from_user.id
        from handlers.admin import admin_state

        key = f"bukti_{uid}"
        if key not in admin_state:
            return

        state = admin_state[key]
        del admin_state[key]

        # Forward bukti ke semua admin
        admin_ids = await db.get_admin_ids()
        for aid in admin_ids:
            try:
                await message.forward(aid)
                await bot.send_message(
                    aid,
                    f"Bukti pembayaran dari user `{uid}`\n"
                    f"Talent: {state['talent']['name']}"
                )
            except Exception:
                pass

        # Hapus pesan minta bukti
        try:
            await bot.delete_messages(state["chat_id"], state["msg1_id"])
        except Exception:
            pass

        # Reply ke customer
        await message.reply_text("Payment proof received. Processing...")
        await db.log_activity("payment_proof_received", category="payment", user_id=uid, details={
            "invoice_id": state["invoice_id"],
            "talent": state["talent"]["name"],
        })
        await asyncio.sleep(2)

        # Lanjut
        await _proceed_after_bukti(uid, state["invoice_id"], state["chat_id"], state["talent"])

    @bot.on_callback_query(filters.regex("^skip_bukti_"))
    async def cb_skip_bukti(client, callback: CallbackQuery):
        """Customer skip kirim bukti."""
        uid = callback.from_user.id
        from handlers.admin import admin_state

        key = f"bukti_{uid}"
        if key not in admin_state:
            await callback.answer()
            return

        state = admin_state[key]
        del admin_state[key]

        try:
            await callback.message.delete()
        except Exception:
            pass

        await _proceed_after_bukti(uid, state["invoice_id"], state["chat_id"], state["talent"])
        await callback.answer()

