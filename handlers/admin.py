"""
Admin Panel Handlers - All admin callback queries
"""

import asyncio
import time
import logging

from pyrogram import filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from bot_manager import bot, talent_bots
from session_manager import get_video_list, notify_subscribers_now
import database as db

logger = logging.getLogger(__name__)

# Admin state dict (shared across handlers)
admin_state = {}


async def is_admin(user_id: int) -> bool:
    admins = await db.get_admin_ids()
    return user_id in admins


def register_admin_handlers():
    """Register all admin callback handlers on the bot."""

    @bot.on_callback_query(filters.regex("^adm_menu$"))
    async def adm_menu(client, callback: CallbackQuery):
        await callback.message.edit_text(
            "**Admin Panel**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Talent", callback_data="adm_talents"),
                 InlineKeyboardButton("Status", callback_data="adm_status")],
                [InlineKeyboardButton("Transaksi", callback_data="adm_txn"),
                 InlineKeyboardButton("Setting", callback_data="adm_setting")],
            ])
        )

    @bot.on_callback_query(filters.regex("^adm_talents$"))
    async def adm_talents(client, callback: CallbackQuery):
        if not await is_admin(callback.from_user.id):
            return
        talents = await db.get_talents()
        buttons = []
        for t in talents:
            buttons.append([InlineKeyboardButton(
                f"{t['name']} — Rp {t['price']:,}/{t['duration']}m",
                callback_data=f"adm_tedit_{t['id']}"
            )])
        buttons.append([InlineKeyboardButton("+ Tambah Talent", callback_data="adm_tadd")])
        buttons.append([InlineKeyboardButton("Kembali", callback_data="adm_menu")])
        await callback.message.edit_text("**Manage Talent**", reply_markup=InlineKeyboardMarkup(buttons))

    @bot.on_callback_query(filters.regex("^adm_tedit_"))
    async def adm_tedit(client, callback: CallbackQuery):
        if not await is_admin(callback.from_user.id):
            return
        # Clear state kalau ada (batal edit)
        admin_state.pop(callback.from_user.id, None)
        talent_id = callback.data.replace("adm_tedit_", "")
        talent = await db.get_talent(talent_id)
        if not talent:
            return await callback.answer("Not found")

        is_off = talent.get("offline", False)
        has_bot = talent_id in talent_bots and talent_bots[talent_id]["ready"]
        toggle_text = "Set ONLINE" if is_off else "Set OFFLINE"
        akun_st = "Aktif" if has_bot else "Belum"

        # Show videos list
        videos = talent.get("videos", [])
        if not videos and talent.get("video"):
            videos = [talent["video"]]
        videos_text = "\n".join([f"  {i+1}. `{v.get('filename', v) if isinstance(v, dict) else v}`" for i, v in enumerate(videos)]) if videos else "  Belum ada"

        # Ringkasan paket durasi (kalau ada)
        pkgs = talent.get("packages") or []
        if pkgs:
            pkg_lines = []
            for i, p in enumerate(pkgs):
                lbl = (p.get("label") or "").strip() or f"{p.get('duration', 0)}m"
                pkg_lines.append(f"  {i+1}. {lbl} — Rp {int(p.get('price', 0)):,}")
            pkg_text = "\n".join(pkg_lines)
        else:
            pkg_text = "  (belum ada — pakai harga/durasi tunggal)"

        text = (
            f"**{talent['name']}**\n\n"
            f"Status: {'OFFLINE' if is_off else 'ONLINE'} | Akun: {akun_st}\n"
            f"Harga: Rp {talent['price']:,} | Durasi: {talent['duration']}m | CD: {talent.get('cooldown', 0)}m\n\n"
            f"**Paket ({len(pkgs)}):**\n{pkg_text}\n\n"
            f"**Video ({len(videos)}):**\n{videos_text}"
        )
        buttons = [
            [InlineKeyboardButton(toggle_text, callback_data=f"adm_toggle_{talent_id}")],
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
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    @bot.on_callback_query(filters.regex("^adm_toggle_"))
    async def adm_toggle(client, callback: CallbackQuery):
        if not await is_admin(callback.from_user.id):
            return
        talent_id = callback.data.replace("adm_toggle_", "")
        talent = await db.get_talent(talent_id)
        if not talent:
            return
        new_val = not talent.get("offline", False)
        await db.update_talent(talent_id, offline=new_val)
        await db.log_activity("talent_toggle_offline", category="admin", user_id=callback.from_user.id, details={"talent_id": talent_id, "offline": new_val})
        if not new_val:
            await db.remove_cooldown(talent_id)
            asyncio.create_task(notify_subscribers_now(talent_id))
        await callback.answer(f"{'OFFLINE' if new_val else 'ONLINE'}", show_alert=True)
        callback.data = f"adm_tedit_{talent_id}"
        await adm_tedit(client, callback)

    @bot.on_callback_query(filters.regex("^adm_tset_"))
    async def adm_tset(client, callback: CallbackQuery):
        if not await is_admin(callback.from_user.id):
            return
        raw = callback.data.replace("adm_tset_", "")
        last_us = raw.rfind("_")
        talent_id = raw[:last_us]
        field = raw[last_us + 1:]

        if field == "photo":
            admin_state[callback.from_user.id] = {"action": "edit_photo", "talent_id": talent_id}
            await callback.message.edit_text(
                "**Kirim foto baru:**",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data=f"adm_tedit_{talent_id}")]])
            )
        elif field == "video":
            admin_state[callback.from_user.id] = {"action": "edit_video", "talent_id": talent_id}
            await callback.message.edit_text(
                "**Kirim/forward video:**\n\nVideo akan ditambahkan ke list rotation.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data=f"adm_tedit_{talent_id}")]])
            )
        elif field == "session":
            admin_state[callback.from_user.id] = {"action": "login_phone", "talent_id": talent_id}
            await callback.message.edit_text(
                "**Login Akun Talent**\n\n"
                "Kirim nomor HP akun yang akan dipakai:\n"
                "Contoh: `+6281234567890`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data=f"adm_tedit_{talent_id}")]])
            )
        else:
            labels = {
                "name": "nama",
                "desc": "deskripsi",
                "price": "harga (angka)",
                "duration": "durasi menit",
                "durationlabel": "label durasi (teks bebas, kosongkan=pakai angka)",
                "cooldown": "cooldown menit"
            }
            # Tampilkan nilai saat ini
            talent = await db.get_talent(talent_id)
            db_key = "duration_label" if field == "durationlabel" else field
            current = talent.get(db_key, "-") if talent else "-"
            if field == "durationlabel" and not str(current).strip():
                current = "(kosong — pakai angka durasi)"
            admin_state[callback.from_user.id] = {"action": "edit_field", "talent_id": talent_id, "field": field}
            await callback.message.edit_text(
                f"**Kirim {labels.get(field, field)} baru:**\n\nSaat ini: `{current}`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data=f"adm_tedit_{talent_id}")]])
            )

    @bot.on_callback_query(filters.regex("^adm_pkg_"))
    async def adm_pkg(client, callback: CallbackQuery):
        """Kelola paket durasi talent: lihat daftar, tambah, hapus."""
        if not await is_admin(callback.from_user.id):
            return
        admin_state.pop(callback.from_user.id, None)
        talent_id = callback.data.replace("adm_pkg_", "")
        talent = await db.get_talent(talent_id)
        if not talent:
            return await callback.answer("Not found")

        pkgs = talent.get("packages") or []
        vids = talent.get("videos") or []
        buttons = []
        if pkgs:
            lines = []
            for i, p in enumerate(pkgs):
                lbl = (p.get("label") or "").strip() or f"{p.get('duration', 0)}m"
                vi = p.get("video_index")
                if vi is not None and 0 <= vi < len(vids):
                    v = vids[vi]
                    vname = (v.get("title") or v.get("filename") if isinstance(v, dict) else str(v)) or f"video #{vi+1}"
                    vtxt = f" • 🎬 {vname}"
                else:
                    vtxt = " • rotation"
                lines.append(f"  {i+1}. {lbl} — Rp {int(p.get('price', 0)):,} ({p.get('duration', 0)}m){vtxt}")
                buttons.append([InlineKeyboardButton(
                    f"❌ Hapus #{i+1} ({lbl})", callback_data=f"adm_pkgdel_{talent_id}_{i}"
                )])
            body = "\n".join(lines)
        else:
            body = "(belum ada paket — customer pakai harga/durasi tunggal)"

        buttons.append([InlineKeyboardButton("+ Tambah Paket", callback_data=f"adm_pkgadd_{talent_id}")])
        if pkgs:
            buttons.append([InlineKeyboardButton("Hapus Semua Paket", callback_data=f"adm_pkgclr_{talent_id}")])
        buttons.append([InlineKeyboardButton("Kembali", callback_data=f"adm_tedit_{talent_id}")])

        await callback.message.edit_text(
            f"**Paket Durasi — {talent['name']}**\n\n{body}\n\n"
            f"Tiap paket punya durasi + harga sendiri. Kalau kosong, dipakai harga/durasi tunggal.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    @bot.on_callback_query(filters.regex("^adm_pkgadd_"))
    async def adm_pkgadd(client, callback: CallbackQuery):
        if not await is_admin(callback.from_user.id):
            return
        talent_id = callback.data.replace("adm_pkgadd_", "")
        talent = await db.get_talent(talent_id)
        vids = (talent.get("videos") if talent else None) or []
        vid_lines = []
        for i, v in enumerate(vids):
            vname = (v.get("title") or v.get("filename")) if isinstance(v, dict) else str(v)
            vid_lines.append(f"  v{i+1} = {vname}")
        vid_help = ("\n\nVideo tersedia:\n" + "\n".join(vid_lines)) if vid_lines else ""
        admin_state[callback.from_user.id] = {"action": "add_package", "talent_id": talent_id}
        await callback.message.edit_text(
            "**Tambah Paket**\n\n"
            "Kirim: `durasi harga [vN] [label]`\n"
            "Contoh:\n"
            "• `5 50000 v1 Exclusive` (pakai video #1)\n"
            "• `10 90000` (video rotation, tanpa label)\n\n"
            "Durasi: menit (boleh desimal / `58s` detik). `vN` opsional = ikat video."
            + vid_help,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data=f"adm_pkg_{talent_id}")]])
        )

    @bot.on_callback_query(filters.regex("^adm_pkgdel_"))
    async def adm_pkgdel(client, callback: CallbackQuery):
        if not await is_admin(callback.from_user.id):
            return
        raw = callback.data.replace("adm_pkgdel_", "")
        cut = raw.rfind("_")
        talent_id = raw[:cut]
        try:
            idx = int(raw[cut + 1:])
        except ValueError:
            return await callback.answer("", show_alert=True)
        talent = await db.get_talent(talent_id)
        if not talent:
            return await callback.answer("Not found")
        pkgs = list(talent.get("packages") or [])
        if 0 <= idx < len(pkgs):
            pkgs.pop(idx)
            await db.update_talent(talent_id, packages=pkgs)
            await db.log_activity("talent_package_deleted", category="admin", user_id=callback.from_user.id, details={"talent_id": talent_id})
        callback.data = f"adm_pkg_{talent_id}"
        await adm_pkg(client, callback)

    @bot.on_callback_query(filters.regex("^adm_pkgclr_"))
    async def adm_pkgclr(client, callback: CallbackQuery):
        if not await is_admin(callback.from_user.id):
            return
        talent_id = callback.data.replace("adm_pkgclr_", "")
        await db.update_talent(talent_id, packages=[])
        await db.log_activity("talent_packages_cleared", category="admin", user_id=callback.from_user.id, details={"talent_id": talent_id})
        callback.data = f"adm_pkg_{talent_id}"
        await adm_pkg(client, callback)

    @bot.on_callback_query(filters.regex("^adm_vplay_"))
    async def adm_vplay(client, callback: CallbackQuery):
        """Kirim video ke admin untuk preview."""
        if not await is_admin(callback.from_user.id):
            return
        talent_id = callback.data.replace("adm_vplay_", "")
        talent = await db.get_talent(talent_id)
        if not talent:
            return await callback.answer("Not found")

        videos = talent.get("videos", [])
        if not videos and talent.get("video"):
            videos = [talent["video"]]

        if not videos:
            return await callback.answer("Belum ada video.", show_alert=True)

        await callback.answer("Mengirim video...")
        for v in videos:
            if isinstance(v, dict) and v.get("file_id"):
                try:
                    await client.send_video(
                        callback.message.chat.id,
                        video=v["file_id"],
                        caption=f"`{v.get('filename', 'video')}`"
                    )
                except Exception:
                    await client.send_document(
                        callback.message.chat.id,
                        document=v["file_id"],
                        caption=f"`{v.get('filename', 'video')}`"
                    )
            elif isinstance(v, str):
                await client.send_message(
                    callback.message.chat.id,
                    f"`{v}` (format lama, upload ulang)"
                )

    @bot.on_callback_query(filters.regex("^adm_vdel_"))
    async def adm_vdel(client, callback: CallbackQuery):
        """Show video list with delete buttons."""
        if not await is_admin(callback.from_user.id):
            return
        talent_id = callback.data.replace("adm_vdel_", "")
        talent = await db.get_talent(talent_id)
        if not talent:
            return await callback.answer("Not found")

        videos = talent.get("videos", [])
        if not videos and talent.get("video"):
            videos = [talent["video"]]

        if not videos:
            return await callback.answer("Tidak ada video.", show_alert=True)

        buttons = []
        for i, v in enumerate(videos):
            buttons.append([InlineKeyboardButton(
                f"{v.get('filename', v) if isinstance(v, dict) else v}",
                callback_data=f"adm_vrem_{talent_id}_{i}"
            )])
        buttons.append([InlineKeyboardButton("Kembali", callback_data=f"adm_tedit_{talent_id}")])
        await callback.message.edit_text(
            "**Pilih video untuk dihapus:**",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    @bot.on_callback_query(filters.regex("^adm_vrem_"))
    async def adm_vrem(client, callback: CallbackQuery):
        """Remove a specific video from talent's list."""
        if not await is_admin(callback.from_user.id):
            return
        raw = callback.data.replace("adm_vrem_", "")
        last_us = raw.rfind("_")
        talent_id = raw[:last_us]
        idx = int(raw[last_us + 1:])

        await db.remove_video_from_talent(talent_id, idx)
        await db.log_activity("talent_video_removed", category="admin", user_id=callback.from_user.id, details={"talent_id": talent_id, "index": idx})
        await callback.answer("Video dihapus", show_alert=True)

        callback.data = f"adm_tedit_{talent_id}"
        await adm_tedit(client, callback)

    @bot.on_callback_query(filters.regex("^adm_tdel_"))
    async def adm_tdel(client, callback: CallbackQuery):
        if not await is_admin(callback.from_user.id):
            return
        talent_id = callback.data.replace("adm_tdel_", "")
        await db.delete_talent(talent_id)
        await db.log_activity("talent_deleted", category="admin", user_id=callback.from_user.id, details={"talent_id": talent_id})
        await callback.message.edit_text(
            "**Dihapus.**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("", callback_data="adm_talents")]])
        )

    @bot.on_callback_query(filters.regex("^adm_tadd$"))
    async def adm_tadd(client, callback: CallbackQuery):
        if not await is_admin(callback.from_user.id):
            return
        admin_state[callback.from_user.id] = {"action": "add_talent", "step": "photo"}
        await callback.message.edit_text("**Tambah Talent**\n\nKirim foto:")

    @bot.on_callback_query(filters.regex("^adm_videos$"))
    async def adm_videos(client, callback: CallbackQuery):
        if not await is_admin(callback.from_user.id):
            return
        videos = get_video_list()
        if videos:
            text = f"**Video** ({len(videos)})\n\n" + "\n".join(
                [f"  {i+1}. `{v}`" for i, v in enumerate(videos)]
            )
        else:
            text = "Kosong"
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data="adm_menu")]])
        )

    @bot.on_callback_query(filters.regex("^adm_status$"))
    async def adm_status(client, callback: CallbackQuery):
        if not await is_admin(callback.from_user.id):
            return
        sessions = await db.get_active_sessions()
        if not sessions:
            text = "**Tidak ada sesi aktif.**"
        else:
            lines = [
                f"  • User `{s['user_id']}` — {int(max(0, s['end_time'] - time.time()) // 60)}m left"
                for s in sessions
            ]
            text = f"**Sesi Aktif** ({len(sessions)})\n\n" + "\n".join(lines)
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data="adm_menu")]])
        )

    @bot.on_callback_query(filters.regex("^adm_txn$"))
    async def adm_txn(client, callback: CallbackQuery):
        if not await is_admin(callback.from_user.id):
            return
        txns = await db.get_transactions(10)
        if not txns:
            text = "**Belum ada transaksi.**"
        else:
            lines = [
                f"  • Rp {t['amount']:,} — {t.get('status', '?')} — {t.get('user_name', '?')}"
                for t in txns
            ]
            text = f"**Transaksi:**\n\n" + "\n".join(lines)
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data="adm_menu")]])
        )

    @bot.on_callback_query(filters.regex("^adm_setting$"))
    async def adm_setting(client, callback: CallbackQuery):
        if not await is_admin(callback.from_user.id):
            return
        admin_state.pop(callback.from_user.id, None)
        settings = await db.get_settings()

        # Cek userbot aktif
        from bot_manager import userbot, userbot_ready
        if userbot_ready and userbot:
            try:
                me = await userbot.get_me()
                ub_info = f"{me.first_name} (`{me.id}`)"
            except Exception:
                ub_info = "Connected tapi error"
        else:
            ub_info = "Belum login"

        text = (
            f"**Setting**\n\n"
            f"📱 Userbot: {ub_info}\n"
            f"Harga: Rp {settings.get('price', 50000):,}\n"
            f"Durasi: {settings.get('duration', 30)}m\n"
            f"Admin: {settings.get('admin_ids', [])}"
        )
        buttons = [
            [InlineKeyboardButton("Harga", callback_data="adm_s_price"),
             InlineKeyboardButton("Durasi", callback_data="adm_s_duration")],
            [InlineKeyboardButton("Login Userbot", callback_data="adm_s_login"),
             InlineKeyboardButton("Rich Message", callback_data="adm_s_templates")],
            [InlineKeyboardButton("+ Admin", callback_data="adm_s_addadmin")],
            [InlineKeyboardButton("Kembali", callback_data="adm_menu")],
        ]
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    @bot.on_callback_query(filters.regex("^adm_s_price$"))
    async def adm_s_price(client, cb: CallbackQuery):
        settings = await db.get_settings()
        admin_state[cb.from_user.id] = {"action": "set_price"}
        await cb.message.edit_text(
            f"**Kirim harga baru:**\n\nSaat ini: Rp {settings.get('price', 50000):,}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data="adm_setting")]])
        )

    @bot.on_callback_query(filters.regex("^adm_s_duration$"))
    async def adm_s_duration(client, cb: CallbackQuery):
        settings = await db.get_settings()
        admin_state[cb.from_user.id] = {"action": "set_duration"}
        await cb.message.edit_text(
            f"**Kirim durasi baru (menit):**\n\nSaat ini: {settings.get('duration', 30)}m",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data="adm_setting")]])
        )

    @bot.on_callback_query(filters.regex("^adm_s_addadmin$"))
    async def adm_s_addadmin(client, cb: CallbackQuery):
        admin_state[cb.from_user.id] = {"action": "add_admin"}
        await cb.message.edit_text(
            "**Kirim user ID:**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data="adm_setting")]])
        )

    @bot.on_callback_query(filters.regex("^adm_s_login$"))
    async def adm_s_login(client, cb: CallbackQuery):
        if not await is_admin(cb.from_user.id):
            return
        admin_state[cb.from_user.id] = {"action": "login_userbot_phone"}
        await cb.message.edit_text(
            "**Login Default Userbot**\n\n"
            "Kirim nomor HP akun yang akan dijadikan userbot utama:\n"
            "Contoh: `+6281234567890`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data="adm_setting")]])
        )

    @bot.on_callback_query(filters.regex("^adm_s_templates$"))
    async def adm_s_templates(client, cb: CallbackQuery):
        """Show list of editable message templates."""
        if not await is_admin(cb.from_user.id):
            return

        template_labels = {
            "welcome": "Halaman Pilih Talent",
            "payment": "Invoice Pembayaran",
            "paid": "Pembayaran Diterima",
            "connecting": "Menghubungi Talent",
            "session_ready": "Sesi Siap",
            "session_end": "Sesi Berakhir",
            "talent_full": "Talent Full",
        }

        buttons = []
        for key, label in template_labels.items():
            buttons.append([InlineKeyboardButton(label, callback_data=f"adm_tpl_{key}")])
        buttons.append([InlineKeyboardButton("Kembali", callback_data="adm_setting")])

        await cb.message.edit_text(
            "**Rich Message Templates**\n\n"
            "Pilih template yang mau diedit.\n"
            "Kirim HTML content untuk update.\n\n"
            "Format: heading, tabel, bold, paragraph",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    @bot.on_callback_query(filters.regex("^adm_tpl_"))
    async def adm_tpl(client, cb: CallbackQuery):
        """Show current template and ask for new content."""
        if not await is_admin(cb.from_user.id):
            return

        key = cb.data.replace("adm_tpl_", "")
        current = await db.get_template(key)

        template_labels = {
            "welcome": "Halaman Pilih Talent",
            "payment": "Invoice Pembayaran",
            "paid": "Pembayaran Diterima",
            "connecting": "Menghubungi Talent",
            "session_ready": "Sesi Siap",
            "session_end": "Sesi Berakhir",
            "talent_full": "Talent Full",
        }

        # Show current template + variables available
        variables = {
            "payment": "{invoice_id}, {talent_name}, {duration}, {nominal}",
            "connecting": "{talent_name}",
            "session_ready": "{talent_name}, {duration}",
            "talent_full": "{talent_name}",
            "paid": "-",
            "session_end": "-",
            "welcome": "-",
        }

        admin_state[cb.from_user.id] = {"action": "edit_template", "template_key": key}

        await cb.message.edit_text(
            f"**Edit: {template_labels.get(key, key)}**\n\n"
            f"Variabel: `{variables.get(key, '-')}`\n\n"
            f"Template saat ini:\n`{current[:500]}`\n\n"
            f"Kirim HTML baru atau ketik 'reset' untuk default.\n\n"
            f"Contoh HTML:\n"
            f"`<h4>JUDUL</h4><table><tr><td><b>Label</b></td><td>Value</td></tr></table><p>Text biasa</p>`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data="adm_s_templates")]])
        )
