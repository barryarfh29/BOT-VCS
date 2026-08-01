"""
API Server - REST endpoints for Web Admin Dashboard
Matches frontend expected format exactly
"""

import os
import time
import uuid
import asyncio
import logging
from collections import defaultdict
from aiohttp import web

from config import (
    API_SECRET, VIDEO_FOLDER, API_ID, API_HASH,
    BASE_URL, ALLOWED_ORIGINS, RATE_LIMIT_MAX, RATE_LIMIT_WINDOW, API_PORT,
)
import database as db

logger = logging.getLogger(__name__)

# Login web (OTP/2FA) yang sedang berjalan: {login_id: {client, phone, phone_code_hash, target, talent_id, ts}}
LOGIN_SESSIONS = {}
LOGIN_TTL = 600  # 10 menit

# ============================================================
# RATE LIMITING (per IP, sliding window)
# ============================================================

_rate_store: dict = defaultdict(list)  # ip -> [timestamps]


def _is_rate_limited(ip: str) -> bool:
    """Check if IP has exceeded rate limit. Cleans old entries."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    # Clean old entries
    _rate_store[ip] = [t for t in _rate_store[ip] if t > window_start]
    if len(_rate_store[ip]) >= RATE_LIMIT_MAX:
        return True
    _rate_store[ip].append(now)
    return False


def _get_allowed_origins() -> set:
    """Build set of allowed origins from config."""
    origins = set()
    # Always allow BASE_URL
    if BASE_URL:
        # Strip trailing slash and path
        from urllib.parse import urlparse
        parsed = urlparse(BASE_URL)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        origins.add(origin)
    # Additional origins from env
    if ALLOWED_ORIGINS:
        for o in ALLOWED_ORIGINS.split(","):
            o = o.strip().rstrip("/")
            if o:
                origins.add(o)
    # Always allow localhost variants for development
    origins.add("http://localhost:3000")
    origins.add("http://localhost:5173")
    origins.add("http://127.0.0.1:3000")
    origins.add("http://127.0.0.1:5173")
    return origins

TEMPLATE_DESCRIPTIONS = {
    "welcome": "Halaman pilih talent",
    "payment": "Invoice QRIS",
    "paid": "Pembayaran diterima",
    "connecting": "Menghubungi talent",
    "session_ready": "Sesi siap",
    "session_end": "Sesi berakhir",
    "talent_full": "Talent full",
    "talent_detail": "Detail talent (deskripsi via admin bot)",
    "channel_greeting": "Sapaan di channel private (dikirim talent saat customer join)",
    "join_warning": "Peringatan kalau customer belum naik ke video chat (5 menit)",
}


async def verify_admin(token: str) -> bool:
    if not token:
        return False
    token = token.replace("Bearer ", "")
    # Secret web admin (env API_SECRET)
    if API_SECRET and token == API_SECRET:
        return True
    # Kompatibilitas lama: token = user_id admin
    try:
        user_id = int(token)
        admin_ids = await db.get_admin_ids()
        return user_id in admin_ids
    except (ValueError, TypeError):
        return False


def _request_token(request) -> str:
    """Ambil token dari header Authorization ATAU query ?token= (untuk <img>/<video> tag)."""
    token = request.headers.get("Authorization", "")
    if not token:
        token = request.query.get("token", "")
    return token


async def ping(request):
    """Tes koneksi + auth. Balikan jenis auth yang dipakai supaya web bisa menampilkan status."""
    token = _request_token(request)
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)
    raw = token.replace("Bearer ", "")
    auth_mode = "secret" if (API_SECRET and raw == API_SECRET) else "admin_id"
    return web.json_response({"ok": True, "auth": auth_mode, "secret_configured": bool(API_SECRET)})


def _talent_detail_json(t: dict) -> dict:
    """Bentuk JSON detail talent untuk web (tanpa membocorkan session_string)."""
    videos = []
    for i, v in enumerate(t.get("videos", [])):
        if isinstance(v, dict):
            videos.append({
                "index": i,
                "filename": v.get("filename", f"video_{i}"),
                "title": v.get("title", ""),
                "length_seconds": v.get("length_seconds"),
                "clip_seconds": v.get("clip_seconds"),
            })
        else:
            videos.append({"index": i, "filename": str(v), "title": "", "length_seconds": None, "clip_seconds": None})
    return {
        "id": t.get("id"),
        "name": t.get("name"),
        "status": "offline" if t.get("offline") else "online",
        "price": t.get("price", 0),
        "duration": t.get("duration", 0),
        "duration_label": t.get("duration_label", ""),
        "packages": t.get("packages", []),
        "desc": t.get("desc", ""),
        "cooldown": t.get("cooldown", 0),
        "has_photo": bool(t.get("photo")),
        "has_session": bool(t.get("session_string")),
        "videos": videos,
    }


def _parse_packages(raw):
    """Validasi list paket durasi. Return (list, None) kalau valid, atau (None, pesan_error).

    Tiap paket: {duration>0 (menit, boleh desimal), price>0 (int), label (teks opsional)}.
    """
    if not isinstance(raw, list):
        return None, "Invalid packages"
    result = []
    for p in raw:
        if not isinstance(p, dict):
            return None, "Invalid package"
        dur = _parse_number(p.get("duration"))
        if dur is None:
            return None, "Invalid package duration"
        try:
            price = int(p.get("price"))
        except (ValueError, TypeError):
            return None, "Invalid package price"
        if price <= 0:
            return None, "Invalid package price"
        result.append({
            "duration": dur,
            "price": price,
            "label": str(p.get("label", "")).strip(),
            "video_index": _parse_video_index(p.get("video_index")),
        })
    return result, None


def _parse_video_index(value):
    """Index video untuk paket: int >= 0, atau None (= rotation)."""
    if value is None or value == "":
        return None
    try:
        idx = int(value)
    except (ValueError, TypeError):
        return None
    return idx if idx >= 0 else None


def _parse_number(value):
    """Angka positif (menit boleh desimal); int kalau bulat. None kalau invalid."""
    try:
        val = float(value)
    except (ValueError, TypeError):
        return None
    if val <= 0:
        return None
    return int(val) if val == int(val) else val


async def get_templates(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    result = []
    for key, desc in TEMPLATE_DESCRIPTIONS.items():
        content = await db.get_template(key)
        result.append({"key": key, "content": content, "description": desc})
    return web.json_response(result)


async def get_template(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    key = request.match_info["key"]
    if key not in TEMPLATE_DESCRIPTIONS:
        return web.json_response({"error": "Not found"}, status=404)

    content = await db.get_template(key)
    return web.json_response({"key": key, "content": content, "description": TEMPLATE_DESCRIPTIONS[key]})


async def update_template(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    key = request.match_info["key"]
    if key not in TEMPLATE_DESCRIPTIONS:
        return web.json_response({"error": "Not found"}, status=404)

    body = await request.json()
    content = body.get("content", "")
    await db.set_template(key, content)
    await db.log_activity("template_updated", category="admin", details={"key": key, "via": "web"})

    return web.json_response({"key": key, "content": content, "description": TEMPLATE_DESCRIPTIONS[key]})


async def get_talent_detail(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    talent = await db.get_talent(request.match_info["id"])
    if not talent:
        return web.json_response({"error": "Not found"}, status=404)
    return web.json_response(_talent_detail_json(talent))


async def create_talent(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return web.json_response({"error": "Name required"}, status=400)
    try:
        price = int(body.get("price", 0))
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid price"}, status=400)
    duration = _parse_number(body.get("duration", 0))
    if duration is None:
        return web.json_response({"error": "Invalid duration"}, status=400)

    talent = {
        "id": f"t_{int(time.time())}",
        "name": name,
        "photo": None,
        "desc": str(body.get("desc", "")),
        "price": price,
        "duration": duration,
        "duration_label": str(body.get("duration_label", "")).strip(),
        "packages": [],
        "videos": [],
        "video_index": 0,
    }
    await db.add_talent(talent)
    await db.log_activity("talent_added", category="admin", details={"name": name, "via": "web"})
    return web.json_response(_talent_detail_json(talent))


async def delete_talent(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    talent_id = request.match_info["id"]
    talent = await db.get_talent(talent_id)
    if not talent:
        return web.json_response({"error": "Not found"}, status=404)
    await db.delete_talent(talent_id)
    await db.log_activity("talent_deleted", category="admin", details={"talent_id": talent_id, "via": "web"})
    return web.json_response({"ok": True})


async def get_talents(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    talents = await db.get_talents()
    result = []
    for t in talents:
        result.append({
            "id": t.get("id"),
            "name": t.get("name"),
            "status": "offline" if t.get("offline") else "online",
            "price": t.get("price", 0),
            "duration": t.get("duration", 0),
            "desc": t.get("desc", ""),
        })
    return web.json_response(result)


async def update_talent(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    talent_id = request.match_info["id"]
    talent = await db.get_talent(talent_id)
    if not talent:
        return web.json_response({"error": "Not found"}, status=404)

    body = await request.json()
    updates = {}
    if "desc" in body:
        updates["desc"] = str(body["desc"])
    if "name" in body:
        name = str(body["name"]).strip()
        if not name:
            return web.json_response({"error": "Invalid name"}, status=400)
        updates["name"] = name
    if "price" in body:
        try:
            updates["price"] = int(body["price"])
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid price"}, status=400)
    if "cooldown" in body:
        cd = _parse_number(body["cooldown"]) if body["cooldown"] not in (0, "0") else 0
        if cd is None:
            return web.json_response({"error": "Invalid cooldown"}, status=400)
        updates["cooldown"] = cd
    if "offline" in body:
        updates["offline"] = bool(body["offline"])
    if "duration" in body:
        try:
            dur = float(body["duration"])
            if dur <= 0:
                raise ValueError
            # Simpan int kalau bulat (2.0 -> 2) supaya tampil rapi di template
            updates["duration"] = int(dur) if dur == int(dur) else dur
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid duration"}, status=400)
    if "duration_label" in body:
        updates["duration_label"] = str(body["duration_label"]).strip()
    if "packages" in body:
        pkgs, err = _parse_packages(body["packages"])
        if err:
            return web.json_response({"error": err}, status=400)
        updates["packages"] = pkgs

    if updates:
        await db.update_talent(talent_id, **updates)
        # Set online kembali -> hapus cooldown + kabari subscriber (samakan perilaku adm_toggle)
        if "offline" in updates and not updates["offline"] and talent.get("offline"):
            from session_manager import notify_subscribers_now
            await db.remove_cooldown(talent_id)
            asyncio.create_task(notify_subscribers_now(talent_id))
        await db.log_activity("talent_edited", category="admin", details={
            "talent_id": talent_id,
            "field": ",".join(updates.keys()),
            "via": "web",
        })

    talent = await db.get_talent(talent_id)
    return web.json_response(_talent_detail_json(talent))


async def get_settings(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    settings = await db.get_settings()
    return web.json_response({
        "bot_name": "StreamingBot",
        "price": settings.get("price", 50000),
        "duration": settings.get("duration", 30),
        "myr_rate": settings.get("myr_rate", 3500),
        "admin_ids": settings.get("admin_ids", []),
        "log_channel_start": settings.get("log_channel_start", 0),
        "log_channel_payment": settings.get("log_channel_payment", 0),
    })


async def update_settings(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    body = await request.json()
    updates = {}
    if "myr_rate" in body:
        try:
            rate = float(body["myr_rate"])
            if rate <= 0:
                raise ValueError
            updates["myr_rate"] = rate
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid myr_rate"}, status=400)
    if "price" in body:
        try:
            updates["price"] = int(body["price"])
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid price"}, status=400)
    if "duration" in body:
        try:
            dur = float(body["duration"])
            if dur <= 0:
                raise ValueError
            # Simpan int kalau bulat (2.0 -> 2) supaya tampil rapi di template
            updates["duration"] = int(dur) if dur == int(dur) else dur
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid duration"}, status=400)
    if "log_channel_start" in body:
        try:
            updates["log_channel_start"] = int(body["log_channel_start"]) if body["log_channel_start"] else 0
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid log_channel_start"}, status=400)
    if "log_channel_payment" in body:
        try:
            updates["log_channel_payment"] = int(body["log_channel_payment"]) if body["log_channel_payment"] else 0
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid log_channel_payment"}, status=400)

    if updates:
        await db.update_settings(**updates)
        await db.log_activity("settings_updated", category="admin", details={"fields": list(updates.keys()), "via": "web"})

    settings = await db.get_settings()
    return web.json_response({
        "bot_name": "StreamingBot",
        "price": settings.get("price", 50000),
        "duration": settings.get("duration", 30),
        "myr_rate": settings.get("myr_rate", 3500),
        "admin_ids": settings.get("admin_ids", []),
        "log_channel_start": settings.get("log_channel_start", 0),
        "log_channel_payment": settings.get("log_channel_payment", 0),
    })


async def get_transactions(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    txns = await db.get_transactions(50)
    result = []
    for t in txns:
        result.append({
            "id": t.get("invoice_id"),
            "user_id": t.get("user_id"),
            "talent_name": t.get("talent", ""),
            "amount": t.get("amount", 0),
            "status": t.get("status", ""),
            "created_at": t.get("created_at", 0),
        })
    return web.json_response(result)


async def get_activities(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        limit = min(int(request.query.get("limit", 50)), 200)
    except (ValueError, TypeError):
        limit = 50
    category = request.query.get("category") or None

    acts = await db.get_activities(limit, category)
    result = []
    for a in acts:
        result.append({
            "action": a.get("action", ""),
            "category": a.get("category", ""),
            "user_id": a.get("user_id"),
            "details": a.get("details", {}),
            "created_at": a.get("created_at", 0),
        })
    return web.json_response(result)


async def _read_upload(request, field_name="file"):
    """Baca satu file dari multipart body. Return (filename, local_path) atau (None, None)."""
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            return None, None
        if part.name == field_name or part.filename:
            filename = os.path.basename(part.filename or f"upload_{int(time.time())}")
            os.makedirs(VIDEO_FOLDER, exist_ok=True)
            local_path = os.path.join(VIDEO_FOLDER, f"web_{int(time.time())}_{filename}")
            with open(local_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            return filename, local_path


async def _file_id_via_bot(local_path: str, kind: str):
    """Kirim media ke DM admin pertama via bot untuk dapat file_id, lalu hapus pesannya.
    (Pola sama dengan handlers/input.py — Telegram butuh pesan nyata untuk menghasilkan file_id.)
    """
    from bot_manager import bot
    admin_ids = await db.get_admin_ids()
    if not admin_ids:
        raise RuntimeError("No admin configured (butuh minimal 1 admin untuk upload)")
    target = admin_ids[0]
    if kind == "photo":
        sent = await bot.send_photo(target, photo=local_path)
        file_id = sent.photo.file_id
    else:
        sent = await bot.send_video(target, video=local_path)
        file_id = sent.video.file_id if sent.video else sent.document.file_id
    try:
        await sent.delete()
    except Exception:
        pass
    return file_id


async def upload_talent_photo(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    talent_id = request.match_info["id"]
    talent = await db.get_talent(talent_id)
    if not talent:
        return web.json_response({"error": "Not found"}, status=404)

    filename, local_path = await _read_upload(request)
    if not local_path:
        return web.json_response({"error": "No file"}, status=400)
    try:
        file_id = await _file_id_via_bot(local_path, "photo")
        await db.update_talent(talent_id, photo=file_id)
        await db.log_activity("talent_photo_updated", category="admin", details={"talent_id": talent_id, "via": "web"})
        return web.json_response({"ok": True})
    except Exception as e:
        logger.error(f"Photo upload failed: {e}")
        return web.json_response({"error": str(e)}, status=500)
    finally:
        try:
            os.remove(local_path)
        except Exception:
            pass


async def upload_talent_video(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    talent_id = request.match_info["id"]
    talent = await db.get_talent(talent_id)
    if not talent:
        return web.json_response({"error": "Not found"}, status=404)

    filename, raw_path = await _read_upload(request)
    if not raw_path:
        return web.json_response({"error": "No file"}, status=400)

    compressed_path = os.path.join(VIDEO_FOLDER, f"opt_{filename}")
    try:
        from media_utils import probe_duration_seconds, probe_video_codec
        # Mode cepat: kalau video sudah H.264 (mayoritas file dari HP/kamera),
        # upload apa adanya tanpa re-encode ffmpeg yang berat di CPU.
        if probe_video_codec(raw_path) == "h264":
            compressed_path = raw_path
        else:
            # Kompres dengan FFmpeg (1080p, 2.5Mbps, audio 128k)
            import subprocess
            cmd = [
                "ffmpeg", "-y", "-i", raw_path,
                "-c:v", "libx264", "-preset", "veryfast",
                "-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "6000k",
                "-vf", "scale=-2:1080",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                compressed_path
            ]
            proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=600)
            if proc.returncode != 0 or not os.path.isfile(compressed_path):
                compressed_path = raw_path  # fallback: pakai file asli

        # Deteksi panjang video (detik) untuk info admin
        length_seconds = probe_duration_seconds(compressed_path)

        file_id = await _file_id_via_bot(compressed_path, "video")
        await db.add_video_to_talent(talent_id, {
            "file_id": file_id, "filename": filename,
            "title": "", "length_seconds": length_seconds,
        })
        await db.log_activity("talent_video_added", category="admin", details={"talent_id": talent_id, "filename": filename, "via": "web"})
        talent = await db.get_talent(talent_id)
        return web.json_response(_talent_detail_json(talent))
    except Exception as e:
        logger.error(f"Video upload failed: {e}")
        return web.json_response({"error": str(e)}, status=500)
    finally:
        for p in {raw_path, compressed_path}:
            try:
                os.remove(p)
            except Exception:
                pass


async def update_video_clip(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    talent_id = request.match_info["id"]
    try:
        index = int(request.match_info["index"])
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid index"}, status=400)

    body = await request.json()
    fields = {}
    if "clip_seconds" in body:
        clip = body.get("clip_seconds")
        if clip is not None:
            clip = _parse_number(clip)
            if clip is None:
                return web.json_response({"error": "Invalid clip_seconds"}, status=400)
        fields["clip_seconds"] = clip
    if "title" in body:
        fields["title"] = str(body.get("title", "")).strip()

    if not fields:
        return web.json_response({"error": "No fields"}, status=400)

    ok = await db.update_video_fields(talent_id, index, fields)
    if not ok:
        return web.json_response({"error": "Not found"}, status=404)
    await db.log_activity("talent_video_updated", category="admin", details={"talent_id": talent_id, "index": index, "fields": list(fields.keys()), "via": "web"})
    talent = await db.get_talent(talent_id)
    return web.json_response(_talent_detail_json(talent))


async def delete_video(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    talent_id = request.match_info["id"]
    try:
        index = int(request.match_info["index"])
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid index"}, status=400)

    await db.remove_video_from_talent(talent_id, index)
    await db.log_activity("talent_video_deleted", category="admin", details={"talent_id": talent_id, "index": index, "via": "web"})
    talent = await db.get_talent(talent_id)
    if not talent:
        return web.json_response({"error": "Not found"}, status=404)
    return web.json_response(_talent_detail_json(talent))


async def _serve_media_from_file_id(file_id: str, cache_name: str):
    """Download media Telegram (sekali, lalu cache lokal) dan balikan path-nya."""
    from bot_manager import bot
    os.makedirs(VIDEO_FOLDER, exist_ok=True)
    local_path = os.path.join(VIDEO_FOLDER, cache_name)
    if not os.path.isfile(local_path):
        await bot.download_media(file_id, file_name=local_path)
    return local_path if os.path.isfile(local_path) else None


async def get_talent_photo(request):
    """Serve foto talent (untuk <img> di web). Auth via header atau ?token=."""
    if not await verify_admin(_request_token(request)):
        return web.json_response({"error": "Unauthorized"}, status=401)

    talent = await db.get_talent(request.match_info["id"])
    if not talent or not talent.get("photo"):
        return web.json_response({"error": "No photo"}, status=404)

    import hashlib
    file_id = talent["photo"]
    cache_name = f"cache_photo_{hashlib.md5(file_id.encode()).hexdigest()[:16]}.jpg"
    try:
        path = await _serve_media_from_file_id(file_id, cache_name)
    except Exception as e:
        logger.error(f"Photo serve failed: {e}")
        path = None
    if not path:
        return web.json_response({"error": "Photo unavailable"}, status=404)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


async def get_talent_video_file(request):
    """Serve/stream file video talent (untuk preview <video> di web). Support range request."""
    if not await verify_admin(_request_token(request)):
        return web.json_response({"error": "Unauthorized"}, status=401)

    talent = await db.get_talent(request.match_info["id"])
    if not talent:
        return web.json_response({"error": "Not found"}, status=404)
    try:
        index = int(request.match_info["index"])
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid index"}, status=400)

    videos = talent.get("videos", [])
    if not (0 <= index < len(videos)):
        return web.json_response({"error": "Not found"}, status=404)

    v = videos[index]
    try:
        if isinstance(v, dict) and v.get("file_id"):
            filename = v.get("filename", f"video_{index}.mp4")
            path = await _serve_media_from_file_id(v["file_id"], filename)
        else:
            # Video lama berupa filename lokal
            filename = v if isinstance(v, str) else v.get("filename", "")
            path = os.path.join(VIDEO_FOLDER, filename)
            path = path if os.path.isfile(path) else None
    except Exception as e:
        logger.error(f"Video serve failed: {e}")
        path = None

    if not path:
        return web.json_response({"error": "Video unavailable"}, status=404)
    # FileResponse aiohttp otomatis support Range (seek di <video>)
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def get_admins(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)
    return web.json_response({"admin_ids": await db.get_admin_ids()})


async def add_admin(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    body = await request.json()
    try:
        user_id = int(body.get("user_id"))
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid user_id"}, status=400)
    await db.add_admin(user_id)
    await db.log_activity("admin_added", category="admin", details={"new_admin_id": user_id, "via": "web"})
    return web.json_response({"admin_ids": await db.get_admin_ids()})


async def remove_admin(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        user_id = int(request.match_info["user_id"])
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid user_id"}, status=400)
    await db.remove_admin(user_id)
    await db.log_activity("admin_removed", category="admin", details={"removed_admin_id": user_id, "via": "web"})
    return web.json_response({"admin_ids": await db.get_admin_ids()})


async def _cleanup_login_sessions():
    """Buang login session yang kedaluwarsa (>TTL)."""
    now = time.time()
    for lid in list(LOGIN_SESSIONS.keys()):
        if now - LOGIN_SESSIONS[lid]["ts"] > LOGIN_TTL:
            sess = LOGIN_SESSIONS.pop(lid)
            try:
                await sess["client"].disconnect()
            except Exception:
                pass


async def login_send_code(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    await _cleanup_login_sessions()
    body = await request.json()
    target = body.get("target")  # 'userbot' | 'talent'
    talent_id = body.get("talent_id")
    phone = str(body.get("phone", "")).strip()
    if target not in ("userbot", "talent") or not phone:
        return web.json_response({"error": "Invalid target/phone"}, status=400)
    if target == "talent" and not talent_id:
        return web.json_response({"error": "talent_id required"}, status=400)

    try:
        from pyrogram import Client as PyroClient
        name = f"web_login_{talent_id}" if target == "talent" else "web_login_userbot"
        client = PyroClient(name, api_id=API_ID, api_hash=API_HASH, in_memory=True)
        await client.connect()
        sent_code = await client.send_code(phone)
    except Exception as e:
        logger.error(f"login send_code failed: {e}")
        return web.json_response({"error": str(e)}, status=500)

    login_id = uuid.uuid4().hex
    LOGIN_SESSIONS[login_id] = {
        "client": client,
        "phone": phone,
        "phone_code_hash": sent_code.phone_code_hash,
        "target": target,
        "talent_id": talent_id,
        "ts": time.time(),
    }
    return web.json_response({"login_id": login_id, "needs": "otp"})


async def _finish_login(login_id: str, sess: dict):
    """Export session string, simpan, dan start bot. Return info user login."""
    client = sess["client"]
    session_string = await client.export_session_string()
    me = await client.get_me()
    await client.disconnect()
    LOGIN_SESSIONS.pop(login_id, None)

    if sess["target"] == "talent":
        from bot_manager import start_talent_bot
        await db.update_talent(sess["talent_id"], session_string=session_string)
        await db.log_activity("talent_bot_login", category="admin", details={"talent_id": sess["talent_id"], "via": "web"})
        started = await start_talent_bot(sess["talent_id"], session_string)
    else:
        from bot_manager import start_default_userbot
        await db.update_settings(userbot_session_string=session_string)
        await db.log_activity("userbot_login", category="admin", details={"via": "web"})
        started = await start_default_userbot()

    return {"ok": True, "started": bool(started), "name": me.first_name, "user_id": me.id}


def _is_2fa_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "two" in msg or "password" in msg or "2fa" in msg


async def login_verify_otp(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    body = await request.json()
    login_id = body.get("login_id")
    code = str(body.get("code", "")).strip().replace(" ", "").replace("-", "")
    sess = LOGIN_SESSIONS.get(login_id)
    if not sess:
        return web.json_response({"error": "Login session expired"}, status=404)

    try:
        await sess["client"].sign_in(sess["phone"], sess["phone_code_hash"], code)
        result = await _finish_login(login_id, sess)
        return web.json_response(result)
    except Exception as e:
        if _is_2fa_error(e):
            return web.json_response({"needs": "2fa", "login_id": login_id})
        LOGIN_SESSIONS.pop(login_id, None)
        try:
            await sess["client"].disconnect()
        except Exception:
            pass
        return web.json_response({"error": str(e)}, status=400)


async def login_verify_2fa(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    body = await request.json()
    login_id = body.get("login_id")
    password = str(body.get("password", ""))
    sess = LOGIN_SESSIONS.get(login_id)
    if not sess:
        return web.json_response({"error": "Login session expired"}, status=404)

    try:
        await sess["client"].check_password(password)
        result = await _finish_login(login_id, sess)
        return web.json_response(result)
    except Exception as e:
        LOGIN_SESSIONS.pop(login_id, None)
        try:
            await sess["client"].disconnect()
        except Exception:
            pass
        return web.json_response({"error": str(e)}, status=400)


async def get_userbot_status(request):
    token = request.headers.get("Authorization", "")
    if not await verify_admin(token):
        return web.json_response({"error": "Unauthorized"}, status=401)

    import bot_manager
    info = {"ready": bool(bot_manager.userbot_ready)}
    if bot_manager.userbot_ready:
        try:
            me = await bot_manager.userbot.get_me()
            info["name"] = me.first_name
            info["user_id"] = me.id
        except Exception:
            pass
    return web.json_response(info)


@web.middleware
async def cors_middleware(request, handler):
    """CORS + Rate Limiting middleware."""
    # Rate limiting (skip for OPTIONS preflight)
    if request.method != "OPTIONS":
        ip = request.remote or "unknown"
        if _is_rate_limited(ip):
            response = web.json_response(
                {"error": "Rate limit exceeded. Try again later."},
                status=429
            )
            response.headers["Retry-After"] = str(RATE_LIMIT_WINDOW)
            return response

    # Determine allowed origin
    allowed_origins = _get_allowed_origins()
    request_origin = request.headers.get("Origin", "")

    if request.method == "OPTIONS":
        response = web.Response()
    else:
        try:
            response = await handler(request)
        except web.HTTPException as e:
            response = web.json_response({"error": str(e)}, status=e.status)

    # Set CORS headers — allow only configured origins (or * for dev if no origins set)
    if request_origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = request_origin
    elif not allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = "*"
    else:
        # Fallback: pick first allowed origin (browser will block mismatched)
        response.headers["Access-Control-Allow-Origin"] = next(iter(allowed_origins))

    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


async def get_frontend_config(request):
    """Public endpoint — frontend ambil base URL dan info koneksi dari sini.
    Tidak butuh auth. Frontend panggil ini saat load untuk tahu ke mana connect API.
    """
    return web.json_response({
        "api_url": BASE_URL,
        "version": "1.0.0",
    })


async def start_api_server(port=None):
    if port is None:
        port = API_PORT

    app = web.Application(middlewares=[cors_middleware], client_max_size=1024 * 1024 * 1024)

    # Public (no auth)
    app.router.add_get("/api/config", get_frontend_config)

    # Protected endpoints
    app.router.add_get("/api/ping", ping)
    app.router.add_get("/api/templates", get_templates)
    app.router.add_get("/api/templates/{key}", get_template)
    app.router.add_put("/api/templates/{key}", update_template)
    app.router.add_get("/api/talents", get_talents)
    app.router.add_post("/api/talents", create_talent)
    app.router.add_get("/api/talents/{id}", get_talent_detail)
    app.router.add_put("/api/talents/{id}", update_talent)
    app.router.add_delete("/api/talents/{id}", delete_talent)
    app.router.add_post("/api/talents/{id}/photo", upload_talent_photo)
    app.router.add_get("/api/talents/{id}/photo", get_talent_photo)
    app.router.add_post("/api/talents/{id}/videos", upload_talent_video)
    app.router.add_get("/api/talents/{id}/videos/{index}/file", get_talent_video_file)
    app.router.add_put("/api/talents/{id}/videos/{index}", update_video_clip)
    app.router.add_delete("/api/talents/{id}/videos/{index}", delete_video)
    app.router.add_get("/api/admins", get_admins)
    app.router.add_post("/api/admins", add_admin)
    app.router.add_delete("/api/admins/{user_id}", remove_admin)
    app.router.add_post("/api/login/send-code", login_send_code)
    app.router.add_post("/api/login/verify-otp", login_verify_otp)
    app.router.add_post("/api/login/verify-2fa", login_verify_2fa)
    app.router.add_get("/api/userbot/status", get_userbot_status)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_put("/api/settings", update_settings)
    app.router.add_get("/api/transactions", get_transactions)
    app.router.add_get("/api/activities", get_activities)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"API server running on port {port}")
    print(f"🌐 API server: {BASE_URL}")
    print(f"   Local: http://0.0.0.0:{port}")
