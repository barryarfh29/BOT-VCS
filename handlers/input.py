"""
Input Handlers - text, photo, video input processing for admin actions
"""

import os
import time
import logging
import re

from pyrogram import filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config import VIDEO_FOLDER
from bot_manager import bot, talent_bots, start_talent_bot
from session_manager import get_video_list
from handlers.admin import admin_state, is_admin
import database as db

logger = logging.getLogger(__name__)


def parse_duration(text):
    """Parse durasi menjadi MENIT (boleh desimal). Return angka >0 atau None.

    Format yang diterima:
      - '1.5' / '1,5' / '2'  -> menit (desimal)
      - '58s' / '90s'        -> detik (dikonversi ke menit)
    Kembalikan int kalau bulat (2.0 -> 2) supaya tampil rapi di template.
    """
    s = str(text).strip().lower().replace(",", ".")
    is_seconds = False
    if s.endswith("s"):
        is_seconds = True
        s = s[:-1].strip()
    try:
        val = float(s)
    except (ValueError, TypeError):
        return None
    if val <= 0:
        return None
    if is_seconds:
        val = val / 60  # detik -> menit
    return int(val) if val == int(val) else val


def register_input_handlers():
    """Register text/photo/video input handlers on the bot."""

    @bot.on_message(filters.photo & filters.private)
    async def handle_photo(client, message: Message):
        uid = message.from_user.id
        if uid not in admin_state:
            return
        state = admin_state[uid]

        if state.get("action") == "add_talent" and state.get("step") == "photo":
            state["photo"] = message.photo.file_id
            state["step"] = "name"
            await message.reply_text("Foto OK\n\nKirim **nama:**")
        elif state.get("action") == "edit_photo":
            tid = state["talent_id"]
            await db.update_talent(tid, photo=message.photo.file_id)
            del admin_state[uid]
            await message.reply_text(
                "Foto diubah!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("", callback_data=f"adm_tedit_{tid}")]
                ])
            )

    @bot.on_message(filters.private)
    async def handle_video(client, message: Message):
        # Manual check: hanya handle kalau ada video atau document
        if not message.video and not message.document:
            return
        uid = message.from_user.id
        if uid not in admin_state:
            return
        state = admin_state[uid]
        if state.get("action") not in ["edit_video", "add_talent"]:
            return

        # Ambil file info
        if message.video:
            filename = message.video.file_name or f"video_{int(time.time())}.mp4"
            size_mb = (message.video.file_size or 0) / (1024 * 1024)
        elif message.document:
            filename = message.document.file_name or f"video_{int(time.time())}.mp4"
            size_mb = (message.document.file_size or 0) / (1024 * 1024)
        else:
            return

        # Mode cepat: video yang dikirim admin sudah ada di Telegram (punya file_id),
        # jadi pakai langsung — tanpa download, kompres ffmpeg, atau upload ulang (instan).
        media = message.video or message.document
        file_id = media.file_id
        length_seconds = message.video.duration if message.video else None

        status_msg = await message.reply_text("Menyimpan video...")
        try:
            if state["action"] == "edit_video":
                tid = state["talent_id"]
                await db.add_video_to_talent(tid, {
                    "file_id": file_id, "filename": filename,
                    "title": "", "length_seconds": length_seconds,
                })
                await db.log_activity("talent_video_added", category="admin", user_id=uid, details={"talent_id": tid, "filename": filename})
                del admin_state[uid]
                await status_msg.edit_text(
                    f"Video ditambahkan: `{filename}`\n"
                    f"Siap dipakai.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Kembali", callback_data=f"adm_tedit_{tid}")]
                    ])
                )
            elif state["action"] == "add_talent" and state.get("step") == "video":
                await db.add_talent({
                    "id": f"t_{int(time.time())}",
                    "name": state["name"],
                    "photo": state["photo"],
                    "desc": state["desc"],
                    "price": state["price"],
                    "duration": state["duration"],
                    "videos": [{"file_id": file_id, "filename": filename, "title": "", "length_seconds": length_seconds}],
                    "video_index": 0,
                })
                await db.log_activity("talent_added", category="admin", user_id=uid, details={"name": state["name"]})
                del admin_state[uid]
                await status_msg.edit_text(
                    f"Talent **{state['name']}** ditambahkan!",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Kembali", callback_data="adm_talents")]
                    ])
                )
        except Exception as e:
            logger.error(f"Video process error: {e}")
            await status_msg.edit_text(f"Error: `{e}`")

    @bot.on_message(filters.text & filters.private & ~filters.command(["start"]))
    async def handle_text(client, message: Message):
        uid = message.from_user.id
        if uid not in admin_state:
            return
        if not await is_admin(uid):
            admin_state.pop(uid, None)
            return

        state = admin_state[uid]
        text = message.text.strip()
        action = state.get("action")

        # Add talent flow
        if action == "add_talent":
            step = state.get("step")
            if step == "name":
                state["name"] = text
                state["step"] = "desc"
                await message.reply_text(f"{text}\n\n📝 Kirim **deskripsi:**")
            elif step == "desc":
                state["desc"] = text
                state["step"] = "price"
                await message.reply_text("\n\nKirim **harga:**")
            elif step == "price":
                if not text.isdigit():
                    return await message.reply_text("Angka:")
                state["price"] = int(text)
                state["step"] = "duration"
                await message.reply_text(f"Rp {int(text):,}\n\nKirim **durasi:**\n• menit: `1.5` (= 90 detik)\n• detik: `58s`")
            elif step == "duration":
                dur = parse_duration(text)
                if dur is None:
                    return await message.reply_text("Format: `1.5` (menit) atau `58s` (detik):")
                state["duration"] = dur
                state["step"] = "video"
                await message.reply_text(f"{dur}m\n\nKirim/forward **video:**")
            elif step == "video":
                # Allow selecting existing video by number or name
                videos = get_video_list()
                if text.isdigit() and 0 <= int(text) - 1 < len(videos):
                    text = videos[int(text) - 1]
                await db.add_talent({
                    "id": f"t_{int(time.time())}",
                    "name": state["name"],
                    "photo": state["photo"],
                    "desc": state["desc"],
                    "price": state["price"],
                    "duration": state["duration"],
                    "videos": [text],
                    "video_index": 0,
                })
                await db.log_activity("talent_added", category="admin", user_id=uid, details={"name": state["name"]})
                del admin_state[uid]
                await message.reply_text(
                    f"**{state['name']}** ditambahkan!",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("", callback_data="adm_talents")]
                    ])
                )
            return

        # Edit talent field
        if action == "edit_field":
            tid = state["talent_id"]
            field = state["field"]
            if field in ["duration", "cooldown"]:
                dur = parse_duration(text)
                if dur is None:
                    return await message.reply_text("Format: `1.5` (menit) atau `58s` (detik):")
                await db.update_talent(tid, **{field: dur})
                text = str(dur)
            elif field == "durationlabel":
                # Teks bebas; kirim '-' atau kosong untuk menghapus label (pakai angka asli)
                label = "" if text.strip() in ("-", "") else text.strip()
                await db.update_talent(tid, duration_label=label)
                text = label or "(pakai angka durasi)"
            elif field == "price":
                if not text.isdigit():
                    return await message.reply_text("Angka:")
                await db.update_talent(tid, **{field: int(text)})
            else:
                await db.update_talent(tid, **{field: text})
            await db.log_activity("talent_edited", category="admin", user_id=uid, details={"talent_id": tid, "field": field})
            del admin_state[uid]
            await message.reply_text(
                f"`{field}` → **{text}**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("", callback_data=f"adm_tedit_{tid}")]
                ])
            )
            return

        # Tambah paket durasi: input "durasi harga [vN] [label]"
        # vN (opsional) mengikat paket ke video ke-N (v1 = video pertama).
        if action == "add_package":
            tid = state["talent_id"]
            parts = text.split()
            if len(parts) < 2:
                return await message.reply_text("Format: `durasi harga [vN] [label]`\nContoh: `5 50000 v1 Exclusive`")
            dur = parse_duration(parts[0])
            if dur is None:
                return await message.reply_text("Durasi tidak valid. Contoh: `5`, `1.5`, atau `58s`.")
            if not parts[1].isdigit():
                return await message.reply_text("Harga harus angka. Contoh: `50000`.")
            price = int(parts[1])
            if price <= 0:
                return await message.reply_text("Harga harus lebih dari 0.")
            talent = await db.get_talent(tid)
            videos = (talent.get("videos") if talent else None) or []
            # Token opsional vN untuk mengikat video
            rest = parts[2:]
            video_index = None
            if rest and re.fullmatch(r"v\d+", rest[0], re.IGNORECASE):
                n = int(rest[0][1:]) - 1
                if 0 <= n < len(videos):
                    video_index = n
                    rest = rest[1:]
                else:
                    return await message.reply_text(f"Nomor video tidak valid. Talent ini punya {len(videos)} video (v1..v{len(videos)}).")
            label = " ".join(rest).strip()
            pkgs = list((talent.get("packages") if talent else None) or [])
            pkgs.append({"duration": dur, "price": price, "label": label, "video_index": video_index})
            await db.update_talent(tid, packages=pkgs)
            await db.log_activity("talent_package_added", category="admin", user_id=uid, details={"talent_id": tid, "duration": dur, "price": price, "video_index": video_index})
            del admin_state[uid]
            shown = label or f"{dur}m"
            vtxt = f" • video #{video_index + 1}" if video_index is not None else " • rotation"
            await message.reply_text(
                f"Paket ditambahkan: **{shown} — Rp {price:,}**{vtxt}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Kelola Paket", callback_data=f"adm_pkg_{tid}")]
                ])
            )
            return

        # Login flow: phone → OTP → password (2FA)
        if action == "login_phone":
            phone = text.strip()
            tid = state["talent_id"]
            admin_state[uid] = {"action": "login_otp", "talent_id": tid, "phone": phone}

            msg = await message.reply_text("**Mengirim kode OTP...**")
            try:
                from pyrogram import Client as PyroClient
                from config import API_ID, API_HASH
                temp_client = PyroClient(
                    f"talent_{tid}",
                    api_id=API_ID,
                    api_hash=API_HASH,
                    in_memory=True,
                )
                await temp_client.connect()
                sent_code = await temp_client.send_code(phone)
                admin_state[uid]["temp_client"] = temp_client
                admin_state[uid]["phone_code_hash"] = sent_code.phone_code_hash
                await msg.edit_text(
                    "**Kode OTP sudah dikirim!**\n\n"
                    "Cek Telegram akun talent, lalu kirim kode di sini.\n\n"
                    "Format: `12345` (angka saja)"
                )
            except Exception as e:
                del admin_state[uid]
                await msg.edit_text(
                    f"**Gagal kirim OTP:** `{e}`",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("", callback_data=f"adm_tedit_{tid}")]
                    ])
                )
            return

        if action == "login_otp":
            code = text.strip().replace(" ", "").replace("-", "")
            tid = state["talent_id"]
            phone = state["phone"]
            temp_client = state.get("temp_client")
            phone_code_hash = state.get("phone_code_hash")

            if not temp_client:
                del admin_state[uid]
                return await message.reply_text("Session expired. Coba lagi.")

            try:
                await temp_client.sign_in(phone, phone_code_hash, code)
                # Login berhasil — export session
                session_string = await temp_client.export_session_string()
                await temp_client.disconnect()

                # Simpan dan start bot
                await db.update_talent(tid, session_string=session_string)
                await db.log_activity("talent_bot_login", category="admin", user_id=uid, details={"talent_id": tid})
                del admin_state[uid]

                ok = await start_talent_bot(tid, session_string)
                if ok:
                    me = await talent_bots[tid]["client"].get_me()
                    await message.reply_text(
                        f"**Login berhasil!**\n\n{me.first_name} (`{me.id}`)",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("", callback_data=f"adm_tedit_{tid}")]
                        ])
                    )
                else:
                    await message.reply_text("Login OK tapi bot gagal start. Restart bot untuk fix.")

            except Exception as e:
                error_msg = str(e).lower()
                if "two" in error_msg or "password" in error_msg or "2fa" in error_msg:
                    # 2FA aktif — minta password
                    admin_state[uid]["action"] = "login_2fa"
                    await message.reply_text(
                        "**Akun ini punya 2FA.**\n\n"
                        "Kirim **password** 2FA:"
                    )
                else:
                    del admin_state[uid]
                    try:
                        await temp_client.disconnect()
                    except:
                        pass
                    await message.reply_text(
                        f"**Gagal login:** `{e}`",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("", callback_data=f"adm_tedit_{tid}")]
                        ])
                    )
            return

        if action == "login_2fa":
            password = text.strip()
            tid = state["talent_id"]
            temp_client = state.get("temp_client")

            if not temp_client:
                del admin_state[uid]
                return await message.reply_text("Session expired. Coba lagi.")

            try:
                await temp_client.check_password(password)
                session_string = await temp_client.export_session_string()
                await temp_client.disconnect()

                await db.update_talent(tid, session_string=session_string)
                await db.log_activity("talent_bot_login", category="admin", user_id=uid, details={"talent_id": tid, "via": "2fa"})
                del admin_state[uid]

                ok = await start_talent_bot(tid, session_string)
                if ok:
                    me = await talent_bots[tid]["client"].get_me()
                    await message.reply_text(
                        f"**Login berhasil!**\n\n{me.first_name} (`{me.id}`)",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("", callback_data=f"adm_tedit_{tid}")]
                        ])
                    )
                else:
                    await message.reply_text("Login OK tapi bot gagal start. Restart untuk fix.")
            except Exception as e:
                del admin_state[uid]
                try:
                    await temp_client.disconnect()
                except:
                    pass
                await message.reply_text(
                    f"**Password salah:** `{e}`",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("", callback_data=f"adm_tedit_{tid}")]
                    ])
                )
            return

        # Login default userbot via bot
        if action == "login_userbot_phone":
            phone = text.strip()
            admin_state[uid] = {"action": "login_userbot_otp", "phone": phone}
            msg = await message.reply_text("**Mengirim OTP...**")
            try:
                from pyrogram import Client as PyroClient
                from config import API_ID, API_HASH
                temp_client = PyroClient("default_userbot_temp", api_id=API_ID, api_hash=API_HASH, in_memory=True)
                await temp_client.connect()
                sent_code = await temp_client.send_code(phone)
                admin_state[uid]["temp_client"] = temp_client
                admin_state[uid]["phone_code_hash"] = sent_code.phone_code_hash
                await msg.edit_text("**OTP dikirim!**\n\nKirim kode OTP (angka saja):")
            except Exception as e:
                del admin_state[uid]
                await msg.edit_text(f"Gagal: `{e}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("", callback_data="adm_setting")]]))
            return

        if action == "login_userbot_otp":
            code = text.strip().replace(" ", "").replace("-", "")
            phone = state["phone"]
            temp_client = state.get("temp_client")
            phone_code_hash = state.get("phone_code_hash")
            if not temp_client:
                del admin_state[uid]
                return await message.reply_text("Expired. Coba lagi.")
            try:
                await temp_client.sign_in(phone, phone_code_hash, code)
                session_string = await temp_client.export_session_string()
                await temp_client.disconnect()
                # Simpan ke MongoDB
                await db.update_settings(userbot_session_string=session_string)
                await db.log_activity("userbot_login", category="admin", user_id=uid)
                del admin_state[uid]
                # Start userbot
                from bot_manager import start_default_userbot
                ok = await start_default_userbot()
                if ok:
                    await message.reply_text("**Userbot berhasil login & aktif!**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("", callback_data="adm_setting")]]))
                else:
                    await message.reply_text("Login OK, restart bot untuk activate.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("", callback_data="adm_setting")]]))
            except Exception as e:
                error_msg = str(e).lower()
                if "two" in error_msg or "password" in error_msg or "2fa" in error_msg:
                    admin_state[uid]["action"] = "login_userbot_2fa"
                    await message.reply_text("**2FA aktif.** Kirim password:")
                else:
                    del admin_state[uid]
                    try: await temp_client.disconnect()
                    except: pass
                    await message.reply_text(f"`{e}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("", callback_data="adm_setting")]]))
            return

        if action == "login_userbot_2fa":
            password = text.strip()
            temp_client = state.get("temp_client")
            if not temp_client:
                del admin_state[uid]
                return await message.reply_text("Expired.")
            try:
                await temp_client.check_password(password)
                session_string = await temp_client.export_session_string()
                await temp_client.disconnect()
                await db.update_settings(userbot_session_string=session_string)
                await db.log_activity("userbot_login", category="admin", user_id=uid, details={"via": "2fa"})
                del admin_state[uid]
                from bot_manager import start_default_userbot
                ok = await start_default_userbot()
                if ok:
                    await message.reply_text("**Userbot login & aktif!**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("", callback_data="adm_setting")]]))
                else:
                    await message.reply_text("Login OK, restart untuk activate.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("", callback_data="adm_setting")]]))
            except Exception as e:
                del admin_state[uid]
                try: await temp_client.disconnect()
                except: pass
                await message.reply_text(f"Password salah: `{e}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("", callback_data="adm_setting")]]))
            return

        # Edit template
        if action == "edit_template":
            key = state.get("template_key")
            del admin_state[uid]
            if text.lower() == "reset":
                from database import DEFAULT_TEMPLATES
                content = DEFAULT_TEMPLATES.get(key, "")
                await db.set_template(key, content)
                await db.log_activity("template_reset", category="admin", user_id=uid, details={"key": key})
                await message.reply_text(
                    "Template di-reset ke default.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Kembali", callback_data="adm_s_templates")]])
                )
            else:
                await db.set_template(key, text)
                await db.log_activity("template_updated", category="admin", user_id=uid, details={"key": key})
                await message.reply_text(
                    f"Template **{key}** diupdate.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Kembali", callback_data="adm_s_templates")]])
                )
            return

        # Global settings
        if action == "set_price":
            if not text.isdigit():
                return await message.reply_text("Angka:")
            await db.update_settings(price=int(text))
            await db.log_activity("settings_price_changed", category="admin", user_id=uid, details={"price": int(text)})
            del admin_state[uid]
            await message.reply_text(
                f"Harga: Rp {int(text):,}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("", callback_data="adm_setting")]
                ])
            )
        elif action == "set_duration":
            if not text.isdigit():
                return await message.reply_text("Angka:")
            await db.update_settings(duration=int(text))
            await db.log_activity("settings_duration_changed", category="admin", user_id=uid, details={"duration": int(text)})
            del admin_state[uid]
            await message.reply_text(
                f"Durasi: {text}m",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("", callback_data="adm_setting")]
                ])
            )
        elif action == "add_admin":
            if not text.isdigit():
                return await message.reply_text("User ID angka:")
            await db.add_admin(int(text))
            await db.log_activity("admin_added", category="admin", user_id=uid, details={"new_admin_id": int(text)})
            del admin_state[uid]
            await message.reply_text(
                f"Admin: `{text}`",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("", callback_data="adm_setting")]
                ])
            )
