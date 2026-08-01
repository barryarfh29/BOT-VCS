"""
Session Manager - start_session, end_session, timers, video loop
Handles channel creation, video playback with loop, and session cleanup
"""

import os
import asyncio
import time
import logging

from pyrogram import enums
from pytgcalls.types import MediaStream, VideoQuality

from config import VIDEO_FOLDER
from bot_manager import bot, get_talent_bot, talent_bots
from rich_message import send_template, render_template
import database as db

logger = logging.getLogger(__name__)

# Active loop tasks: {slot_chat_id: asyncio.Task}
loop_tasks = {}


def _video_stream(video_path: str) -> MediaStream:
    """Stream video TANPA audio — talent selalu tampil mute di live stream."""
    return MediaStream(
        video_path,
        video_parameters=VideoQuality.FHD_1080p,
        audio_flags=MediaStream.Flags.IGNORE,
    )


def get_video_list():
    """Get list of video filenames in VIDEO_FOLDER."""
    import glob
    if not os.path.isdir(VIDEO_FOLDER):
        os.makedirs(VIDEO_FOLDER, exist_ok=True)
        return []
    extensions = ["*.mp4", "*.mkv", "*.avi", "*.webm", "*.mov"]
    videos = []
    for ext in extensions:
        videos.extend(glob.glob(os.path.join(VIDEO_FOLDER, ext)))
    return [os.path.basename(v) for v in videos]


async def start_session(user_id: int, invoice_id: str, chat_id: int, talent: dict):
    """Create channel, send invite. Timer starts when user joins."""
    duration = float(talent.get("duration", 0) or 0)
    talent_name = talent["name"]
    talent_id = talent.get("id")

    # Get next video in rotation
    # Kalau paket mengikat video tertentu (_force_video_index), pakai video itu.
    force_idx = talent.get("_force_video_index")
    if force_idx is not None:
        vids = talent.get("videos") or []
        video_data = vids[force_idx] if 0 <= force_idx < len(vids) else await db.get_next_video(talent_id)
    else:
        video_data = await db.get_next_video(talent_id)

    t_userbot, t_call = await get_talent_bot(talent_id)

    # Selalu create channel BARU
    try:
        channel = await t_userbot.create_channel(
            title=f"{talent_name} - Private",
            description="Private streaming session"
        )
        slot_chat_id = channel.id
        logger.info(f"Channel created: {slot_chat_id}")
    except Exception as e:
        logger.error(f"Channel creation failed: {e}")
        return await bot.send_message(chat_id, f"Gagal: `{e}`")

    # Enable content protection
    try:
        await t_userbot.set_chat_protected_content(slot_chat_id, enabled=True)
    except Exception:
        pass

    # Session BELUM mulai timer — timer start saat user join
    session = {
        "user_id": user_id,
        "invoice_id": invoice_id,
        "chat_id": chat_id,
        "slot_chat_id": slot_chat_id,
        "duration": duration,
        "end_time": 0,  # belum di-set, nanti saat join
        "started_at": 0,  # belum di-set
        "talent_id": talent_id,
        "video_data": video_data,
        "status": "waiting_join",  # waiting_join → active → ended
    }
    await db.add_session(session)

    # Create invite link
    try:
        invite = await t_userbot.create_chat_invite_link(slot_chat_id, member_limit=1)
        invite_link = invite.invite_link
    except Exception as e:
        logger.error(f"Invite link failed: {e}")
        await bot.send_message(chat_id, f"Gagal invite: `{e}`")
        await db.remove_session(invoice_id)
        try:
            await t_userbot.delete_channel(slot_chat_id)
        except Exception:
            pass
        return

    # Langsung kirim link (delay sudah di-handle di customer.py)
    # Rich message (tabel/heading tampil) + fallback otomatis ke pesan biasa
    tpl = await db.get_template("session_ready")
    from rich_message import duration_display, apply_duration_label
    tpl = apply_duration_label(tpl, talent)
    success_msg_id = await send_template(
        bot, chat_id, tpl,
        append_text=f"Join here:\n{invite_link}",
        talent_name=talent_name, duration=duration_display(talent),
    )

    # Simpan message id untuk dihapus nanti
    session["success_msg_id"] = success_msg_id
    await db.remove_session(invoice_id)
    await db.add_session(session)

    # Notify admins
    asyncio.create_task(notify_admins_paid(user_id, talent_name, talent["price"], duration))

    # Monitor join — polling member count
    asyncio.create_task(wait_for_join(session))

    await db.log_activity("session_created", category="session", user_id=user_id, details={
        "invoice_id": invoice_id,
        "talent_id": talent_id,
        "talent": talent_name,
        "duration": duration,
        "channel_id": slot_chat_id,
    })

    logger.info(f"Session waiting join: user={user_id}, talent={talent_id}, channel={slot_chat_id}")


async def notify_admins_paid(user_id: int, talent_name: str, amount: int, duration: int):
    """Notify all admins when a transaction is paid."""
    admin_ids = await db.get_admin_ids()
    text = (
        f"**Transaksi Baru!**\n\n"
        f"User: `{user_id}`\n"
        f"Talent: {talent_name}\n"
        f"Rp {amount:,}\n"
        f"{duration} menit"
    )
    for aid in admin_ids:
        try:
            await bot.send_message(aid, text)
        except Exception:
            pass


async def wait_for_join(session: dict):
    """Wait until user joins voice chat (not just channel). Then start timer + video."""
    slot_chat_id = session["slot_chat_id"]
    talent_id = session.get("talent_id")
    user_id = session["user_id"]
    t_userbot, t_call = await get_talent_bot(talent_id)

    # Phase 1: Wait user join channel (max 10 menit)
    channel_joined = False
    for _ in range(200):
        await asyncio.sleep(3)
        s = await db.get_session_by_user(user_id)
        if not s:
            return
        try:
            chat = await t_userbot.get_chat(slot_chat_id)
            members = chat.members_count or 0
            if members >= 2:
                channel_joined = True
                break
        except Exception:
            pass

    if not channel_joined:
        logger.warning(f"User {user_id} didn't join channel within 10 min")
        await end_session(session)
        return

    # Phase 2: Buka video chat STANDBY (tanpa video) + siapkan file video.
    # Video baru diputar setelah customer benar-benar naik ke video chat —
    # jaga-jaga kalau customer cuma join channel dulu, waktu belum berjalan.
    video_data = session.get("video_data")
    video_path = None
    clip_seconds = None
    if video_data:
        if isinstance(video_data, dict):
            clip_seconds = video_data.get("clip_seconds")
            filename = video_data.get("filename", f"video_{slot_chat_id}.mp4")
            video_path = os.path.join(VIDEO_FOLDER, filename)
            if video_data.get("file_id") and not os.path.isfile(video_path):
                try:
                    # Download via Pyrogram MTProto bot (support file besar, punya file reference)
                    from bot_manager import get_pyro_bot
                    pyro = await get_pyro_bot()
                    await pyro.download_media(video_data["file_id"], file_name=video_path)
                except Exception as e:
                    logger.error(f"Download video failed: {e}")
        elif isinstance(video_data, str):
            video_path = os.path.join(VIDEO_FOLDER, video_data)
        if video_path and not os.path.isfile(video_path):
            video_path = None

    stream_opened = False
    try:
        await t_call.play(slot_chat_id)  # join VC tanpa stream (standby)
        stream_opened = True
        logger.info(f"VC opened (standby) in {slot_chat_id}")
    except Exception as e:
        logger.error(f"Open VC failed: {e}")

    # Sapa customer SETELAH talent membuka video chat (standby, belum putar video)
    # Userbot tidak bisa kirim rich message Bot API — render ke HTML biasa Telegram
    if stream_opened:
        try:
            talent = await db.get_talent(talent_id)
            tpl = await db.get_template("channel_greeting")
            from rich_message import duration_display, apply_duration_label
            if talent:
                tpl = apply_duration_label(tpl, talent)
            text = render_template(
                tpl,
                talent_name=talent["name"] if talent else "",
                duration=duration_display(talent) if talent else str(session.get("duration", "")),
            )
            if text.strip():
                await t_userbot.send_message(slot_chat_id, text, parse_mode=enums.ParseMode.HTML)
        except Exception as e:
            logger.warning(f"channel_greeting failed: {e}")

    # Phase 3: Wait user naik ke video chat (max 10 menit setelah join channel)
    vc_joined = False
    warn_msg_id = None
    for i in range(200):
        await asyncio.sleep(3)
        s = await db.get_session_by_user(user_id)
        if not s:
            return
        # 5 menit belum naik juga → kirim peringatan sekali (template join_warning, editable di web)
        if i == 99 and warn_msg_id is None:
            try:
                talent = await db.get_talent(talent_id)
                tpl = await db.get_template("join_warning")
                warn_msg_id = await send_template(
                    bot, session["chat_id"], tpl,
                    talent_name=talent["name"] if talent else "",
                    remaining="5",
                )
            except Exception as e:
                logger.warning(f"join_warning failed: {e}")
        try:
            participants = await t_call.get_participants(slot_chat_id)
            # Cek apakah ada participant selain userbot
            for p in participants:
                if hasattr(p, 'chat') and p.chat and p.chat.id != (await t_userbot.get_me()).id:
                    vc_joined = True
                    break
                elif hasattr(p, 'user_id') and p.user_id != (await t_userbot.get_me()).id:
                    vc_joined = True
                    break
            if vc_joined:
                break
        except Exception:
            # get_participants mungkin error kalau belum ada VC
            pass

    # Peringatan sudah tidak relevan (user naik / sesi berakhir) — hapus
    if warn_msg_id:
        try:
            await bot.delete_messages(session["chat_id"], warn_msg_id)
        except Exception:
            pass

    if not vc_joined:
        logger.warning(f"User {user_id} didn't join VC within 10 min")
        await db.log_activity("session_join_timeout", category="session", user_id=user_id, details={
            "invoice_id": session.get("invoice_id"),
            "talent_id": session.get("talent_id"),
        })
        await end_session(session)
        return

    # User sudah naik ke video chat! Putar video + timer mulai SEKARANG
    if video_path:
        try:
            await t_call.play(slot_chat_id, _video_stream(video_path))
            logger.info(f"Video playing in {slot_chat_id}")
        except Exception as e:
            logger.error(f"Play failed: {e}")

    duration = session["duration"]
    session["started_at"] = time.time()
    session["end_time"] = time.time() + (duration * 60)
    session["status"] = "active"

    await db.remove_session(session["invoice_id"])
    await db.add_session(session)

    await db.log_activity("session_started", category="session", user_id=user_id, details={
        "invoice_id": session["invoice_id"],
        "talent_id": session.get("talent_id"),
        "duration": duration,
    })

    logger.info(f"User {user_id} joined VC. Timer started: {duration}m")

    # Start timer
    asyncio.create_task(session_timer(session))

    # Start video loop
    if video_path:
        task = asyncio.create_task(video_loop(slot_chat_id, video_path, talent_id, session, clip_seconds))
        loop_tasks[slot_chat_id] = task


async def session_timer(session: dict):
    """Wait until session ends, then cleanup."""
    remaining = session["end_time"] - time.time()
    if remaining > 0:
        await asyncio.sleep(remaining)
    await end_session(session)


async def end_session(session: dict):
    """End session: stop video, delete channel, set cooldown."""
    talent_id = session.get("talent_id")
    slot_chat_id = session["slot_chat_id"]
    chat_id = session["chat_id"]

    t_userbot, t_call = await get_talent_bot(talent_id)

    # Cancel loop task if any
    if slot_chat_id in loop_tasks:
        loop_tasks[slot_chat_id].cancel()
        del loop_tasks[slot_chat_id]

    # Leave call
    try:
        await t_call.leave_call(slot_chat_id)
    except Exception:
        pass

    # Delete channel
    try:
        await t_userbot.delete_channel(slot_chat_id)
    except Exception:
        pass

    # Remove session from DB
    await db.remove_session(session.get("invoice_id"))

    # Set cooldown if talent has one
    if talent_id:
        talent = await db.get_talent(talent_id)
        cd = float(talent.get("cooldown", 0) or 0) if talent else 0
        if cd > 0:
            await db.set_cooldown(talent_id, time.time() + cd * 60)

    # Hapus pesan "Pembayaran berhasil + link"
    success_msg_id = session.get("success_msg_id")
    if success_msg_id:
        try:
            await bot.delete_messages(chat_id, success_msg_id)
        except Exception:
            pass

    # Notify user (rich message + fallback otomatis)
    end_msg_id = None
    try:
        tpl = await db.get_template("session_end")
        end_msg_id = await send_template(bot, chat_id, tpl)
    except Exception:
        pass

    # 5 menit setelah ended → otomatis kembali ke menu awal (/start)
    asyncio.create_task(back_to_menu_later(chat_id, end_msg_id))

    # Notify subscribers after cooldown
    if talent_id:
        asyncio.create_task(notify_after_cooldown(talent_id))

    await db.log_activity("session_ended", category="session", user_id=session.get("user_id"), details={
        "invoice_id": session.get("invoice_id"),
        "talent_id": talent_id,
        "channel_id": slot_chat_id,
    })

    logger.info(f"Session ended: talent={talent_id}, channel={slot_chat_id}")


async def back_to_menu_later(chat_id: int, end_msg_id: int = None, delay: int = 300):
    """Setelah delay, hapus pesan session ended + tampilkan menu awal (daftar talent)."""
    await asyncio.sleep(delay)

    # Kalau user sudah punya sesi aktif baru, jangan ganggu
    # (lazy import: customer.py meng-import session_manager di level modul)
    from handlers.customer import clean_ui, send_welcome_menu

    if end_msg_id:
        try:
            await bot.delete_messages(chat_id, end_msg_id)
        except Exception:
            pass
    # Bersihkan sisa UI (mis. menu dari /start manual) supaya tidak dobel
    await clean_ui(chat_id)
    try:
        await send_welcome_menu(bot, chat_id)
    except Exception as e:
        logger.warning(f"back_to_menu_later failed: {e}")


async def delayed_play(session: dict):
    """Start video playback (called after user joins)."""
    await asyncio.sleep(3)  # Sedikit delay supaya user settle

    # Check session still active
    s = await db.get_session_by_user(session["user_id"])
    if not s:
        return

    video_data = session.get("video_data")
    if not video_data:
        return

    talent_id = session.get("talent_id")
    slot_chat_id = session["slot_chat_id"]
    t_userbot, t_call = await get_talent_bot(talent_id)

    # Download video dari file_id ke local (temp)
    video_path = None
    clip_seconds = None
    if isinstance(video_data, dict):
        clip_seconds = video_data.get("clip_seconds")
        filename = video_data.get("filename", f"video_{slot_chat_id}.mp4")
        video_path = os.path.join(VIDEO_FOLDER, filename)
        if video_data.get("file_id") and not os.path.isfile(video_path):
            try:
                from bot_manager import get_pyro_bot
                pyro = await get_pyro_bot()
                await pyro.download_media(video_data["file_id"], file_name=video_path)
            except Exception as e:
                logger.error(f"Download video failed: {e}")
                return
    elif isinstance(video_data, str):
        # Backward compat: langsung filename
        video_path = os.path.join(VIDEO_FOLDER, video_data)

    if not video_path or not os.path.isfile(video_path):
        logger.warning(f"Video not found: {video_path}")
        return

    # Play video
    try:
        await t_call.play(slot_chat_id, _video_stream(video_path))
        logger.info(f"Playing video in {slot_chat_id}")
    except Exception as e:
        logger.error(f"Play failed: {e}")
        return

    # Start video loop task
    task = asyncio.create_task(video_loop(slot_chat_id, video_path, talent_id, session, clip_seconds))
    loop_tasks[slot_chat_id] = task


async def video_loop(slot_chat_id: int, video_path: str, talent_id: str, session: dict, clip_seconds=None):
    """Loop video until session ends. Re-play when video finishes.

    clip_seconds: kalau di-set, video di-replay tiap N detik (trim Opsi A —
    hanya N detik pertama yang terlihat, lalu diulang dari awal).
    """
    try:
        if clip_seconds and clip_seconds > 0:
            # Interval replay dari setting per-video, tanpa perlu ffprobe
            video_duration = float(clip_seconds)
            replay_buffer = 0  # potong tepat di clip_seconds
        else:
            # Get video duration estimate (approximate, re-play every cycle)
            # pytgcalls doesn't have a clean stream_ended callback in all versions
            # So we use a polling approach: check if still in session, replay periodically
            import subprocess
            try:
                result = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                    capture_output=True, text=True, timeout=10
                )
                video_duration = float(result.stdout.strip())
            except Exception:
                # Default fallback: replay every 10 minutes
                video_duration = 600
            replay_buffer = 2

        while True:
            # Wait for video to finish (with small buffer)
            await asyncio.sleep(video_duration + replay_buffer)

            # Check if session still active
            s = await db.get_session_by_user(session["user_id"])
            if not s:
                break

            # Re-play video
            t_userbot, t_call = await get_talent_bot(talent_id)
            try:
                await t_call.play(slot_chat_id, _video_stream(video_path))
                logger.info(f"Video loop replay: {video_path} in {slot_chat_id}")
            except Exception as e:
                logger.error(f"Video loop replay failed: {e}")
                break

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Video loop error: {e}")


async def notify_after_cooldown(talent_id: str):
    """Wait for cooldown then notify subscribers."""
    talent = await db.get_talent(talent_id)
    if not talent:
        return
    cd = float(talent.get("cooldown", 0) or 0)
    if cd > 0:
        await asyncio.sleep(cd * 60)
    await db.remove_cooldown(talent_id)
    await notify_subscribers_now(talent_id)


async def notify_subscribers_now(talent_id: str):
    """Notify all subscribers that talent is available."""
    talent = await db.get_talent(talent_id)
    if not talent:
        return
    subs = await db.get_subscribers(talent_id)
    for uid in subs:
        try:
            await bot.send_message(uid, f"**{talent['name']}** is now available!\n\nSend /start to order.")
        except Exception:
            pass

