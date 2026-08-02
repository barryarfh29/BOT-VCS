"""
MongoDB Database Handler
Collections: settings, talents, sessions, transactions, subscribers, cooldowns, activities
"""

import time
import logging

import motor.motor_asyncio
from config import MONGO_URI

logger = logging.getLogger(__name__)

client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = client["live_stream"]

# Collections
settings_col = db["settings"]
talents_col = db["talents"]
sessions_col = db["sessions"]
transactions_col = db["transactions"]
subscribers_col = db["subscribers"]
cooldowns_col = db["cooldowns"]
activities_col = db["activities"]


# ============================================================
# ACTIVITY LOG (auto backup semua kegiatan)
# ============================================================

async def log_activity(action: str, category: str = "general", user_id: int = None, details: dict = None):
    """Backup satu kegiatan ke MongoDB. Tidak pernah raise agar alur bot aman."""
    try:
        await activities_col.insert_one({
            "action": action,
            "category": category,
            "user_id": user_id,
            "details": details or {},
            "created_at": time.time(),
        })
    except Exception as e:
        logger.warning(f"log_activity failed ({action}): {e}")


async def get_activities(limit: int = 50, category: str = None):
    """Ambil riwayat kegiatan terbaru, opsional filter per kategori."""
    query = {"category": category} if category else {}
    cursor = activities_col.find(query).sort("created_at", -1).limit(limit)
    return await cursor.to_list(limit)


# ============================================================
# SETTINGS
# ============================================================

async def get_settings():
    doc = await settings_col.find_one({"_id": "main"})
    if not doc:
        doc = {"_id": "main", "price": 50000, "duration": 30, "admin_ids": [], "myr_rate": 3500}
        await settings_col.insert_one(doc)
    return doc


async def update_settings(**kwargs):
    await settings_col.update_one({"_id": "main"}, {"$set": kwargs}, upsert=True)


async def get_admin_ids():
    doc = await get_settings()
    return doc.get("admin_ids", [])


async def add_admin(user_id: int):
    await settings_col.update_one({"_id": "main"}, {"$addToSet": {"admin_ids": user_id}}, upsert=True)


async def remove_admin(user_id: int):
    await settings_col.update_one({"_id": "main"}, {"$pull": {"admin_ids": user_id}})


# ============================================================
# TALENTS
# ============================================================

async def get_talents():
    return await talents_col.find().to_list(100)


async def get_talent(talent_id: str):
    return await talents_col.find_one({"id": talent_id})


async def add_talent(talent: dict):
    await talents_col.insert_one(talent)


async def update_talent(talent_id: str, **kwargs):
    await talents_col.update_one({"id": talent_id}, {"$set": kwargs})


async def delete_talent(talent_id: str):
    await talents_col.delete_one({"id": talent_id})


async def add_video_to_talent(talent_id: str, video_data):
    """Add a video to talent's videos list. video_data can be dict {file_id, filename} or string."""
    await talents_col.update_one({"id": talent_id}, {"$push": {"videos": video_data}})


async def remove_video_from_talent(talent_id: str, index: int):
    """Remove a video from talent's videos list by index."""
    talent = await get_talent(talent_id)
    if not talent:
        return
    videos = talent.get("videos", [])
    if 0 <= index < len(videos):
        videos.pop(index)
        await update_talent(talent_id, videos=videos)


async def update_video_clip(talent_id: str, index: int, clip_seconds):
    """Set/hapus durasi potongan (detik) untuk satu video. None = pakai durasi penuh."""
    return await update_video_fields(talent_id, index, {"clip_seconds": clip_seconds})


async def update_video_fields(talent_id: str, index: int, fields: dict):
    """Update sebagian field satu video (mis. clip_seconds, title, length_seconds)."""
    talent = await get_talent(talent_id)
    if not talent:
        return False
    videos = talent.get("videos", [])
    if not (0 <= index < len(videos)):
        return False
    v = videos[index]
    # Backward compat: video lama berupa string filename -> ubah jadi dict
    if isinstance(v, str):
        v = {"file_id": None, "filename": v}
    v.update(fields)
    videos[index] = v
    await update_talent(talent_id, videos=videos)
    return True


async def get_next_video(talent_id: str):
    """Get next video in rotation. Returns dict {file_id, filename} or string (backward compat)."""
    talent = await get_talent(talent_id)
    if not talent:
        return None

    videos = talent.get("videos", [])
    if not videos:
        return None

    if not videos:
        return None

    video_index = talent.get("video_index", 0)
    selected = videos[video_index % len(videos)]

    # Increment index
    new_index = (video_index + 1) % len(videos)
    await update_talent(talent_id, video_index=new_index)

    return selected


# ============================================================
# SESSIONS
# ============================================================

async def get_active_sessions():
    return await sessions_col.find().to_list(100)


async def get_session_by_user(user_id: int):
    return await sessions_col.find_one({"user_id": user_id})


async def get_session_by_talent(talent_id: str):
    return await sessions_col.find_one({"talent_id": talent_id})


async def add_session(session: dict):
    await sessions_col.insert_one(session)


async def remove_session(invoice_id: str):
    await sessions_col.delete_one({"invoice_id": invoice_id})


# ============================================================
# TRANSACTIONS
# ============================================================

async def add_transaction(txn: dict):
    await transactions_col.insert_one(txn)


async def update_transaction(invoice_id: str, **kwargs):
    await transactions_col.update_one({"invoice_id": invoice_id}, {"$set": kwargs})


async def get_transactions(limit=10):
    cursor = transactions_col.find().sort("created_at", -1).limit(limit)
    return await cursor.to_list(limit)


# ============================================================
# COOLDOWNS
# ============================================================

async def set_cooldown(talent_id: str, available_at: float):
    await cooldowns_col.update_one(
        {"talent_id": talent_id},
        {"$set": {"talent_id": talent_id, "available_at": available_at}},
        upsert=True
    )


async def get_cooldown(talent_id: str):
    doc = await cooldowns_col.find_one({"talent_id": talent_id})
    return doc.get("available_at", 0) if doc else 0


async def remove_cooldown(talent_id: str):
    await cooldowns_col.delete_one({"talent_id": talent_id})


# ============================================================
# SUBSCRIBERS
# ============================================================

async def get_subscribers(talent_id: str):
    doc = await subscribers_col.find_one({"talent_id": talent_id})
    return doc.get("users", []) if doc else []


async def add_subscriber(talent_id: str, user_id: int):
    await subscribers_col.update_one(
        {"talent_id": talent_id},
        {"$addToSet": {"users": user_id}},
        upsert=True
    )


async def remove_subscriber(talent_id: str, user_id: int):
    await subscribers_col.update_one(
        {"talent_id": talent_id},
        {"$pull": {"users": user_id}}
    )


async def is_subscribed(talent_id: str, user_id: int) -> bool:
    doc = await subscribers_col.find_one({"talent_id": talent_id, "users": user_id})
    return doc is not None


# ============================================================
# UI MESSAGES (pesan UI per chat — persisten agar tahan restart/redeploy)
# ============================================================

ui_messages_col = db["ui_messages"]
user_prefs_col = db["user_prefs"]


async def get_user_lang(user_id: int) -> str:
    """Get user language preference. Returns 'id' or 'my'. None = belum pilih."""
    doc = await user_prefs_col.find_one({"user_id": user_id})
    return doc.get("lang") if doc else None


async def set_user_lang(user_id: int, lang: str):
    """Set user language preference ('id' or 'my')."""
    await user_prefs_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "lang": lang}},
        upsert=True
    )


async def track_ui_messages(chat_id: int, msg_ids: list):
    """Catat message id UI (welcome, foto talent, detail) untuk dihapus saat navigasi."""
    if msg_ids:
        await ui_messages_col.update_one(
            {"chat_id": chat_id},
            {"$push": {"ids": {"$each": msg_ids}}},
            upsert=True
        )


async def pop_ui_messages(chat_id: int) -> list:
    """Ambil + hapus semua message id UI tercatat untuk chat ini."""
    doc = await ui_messages_col.find_one_and_delete({"chat_id": chat_id})
    return doc.get("ids", []) if doc else []


async def set_ui_messages(chat_id: int, msg_ids: list):
    """Timpa daftar message id UI untuk chat ini."""
    await ui_messages_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"ids": msg_ids}},
        upsert=True
    )


# ============================================================
# MESSAGE TEMPLATES
# ============================================================

templates_col = db["templates"]

DEFAULT_TEMPLATES = {
    "welcome": "<h4>CHOOSE A TALENT</h4><p>Select a talent below to start your private session.</p><ul><li><p>Private one-on-one live session</p></li><li><p>Easy payment via QRIS - TNG / Maybank / Boost supported</p></li><li><p>Timer starts only after you join</p></li></ul>",
    "loading_1": "🔍 Checking available talents...",
    "loading_2": "🔍 Checking available talents...\n✅ {count} talents found",
    "loading_3": "🔍 Checking available talents...\n✅ {count} talents found\n⏳ Loading menu...",
    "payment": "<h2>QRIS Invoice</h2><h4>Order Details</h4><table><tbody><tr><td><p><strong>Invoice ID</strong></p></td><td><p>{invoice_id}</p></td></tr><tr><td><p><strong>Talent</strong></p></td><td><p>{talent_name}</p></td></tr><tr><td><p><strong>Duration</strong></p></td><td><p>{duration} minutes</p></td></tr></tbody></table><h4>Payment</h4><table><tbody><tr><td><p><strong>Total</strong></p></td><td><p><strong>{nominal}</strong></p></td></tr><tr><td><p><strong>Method</strong></p></td><td><p>QRIS / Cross-border QR</p></td></tr></tbody></table><p>Scan the QR code above to pay. Payment is detected automatically.</p><p>Malaysian e-wallets (TNG, Maybank, Boost) can scan the same code.</p>",
    "paid": "<h4>PAYMENT RECEIVED</h4><p>Your payment has been confirmed.</p><p>Please send a <b>screenshot of your payment proof</b> for verification, or tap <b>Skip</b> to continue.</p>",
    "connecting": "<h4>CONNECTING TO TALENT</h4><p>Contacting <b>{talent_name}</b> to serve you...</p><p>Please wait a moment.</p>",
    "session_ready": "<h4>SESSION READY</h4><p><b>Talent:</b> {talent_name}</p><p><b>Duration:</b> {duration} minutes</p><p><b>{talent_name}</b> is ready for you. The timer starts when you join the voice chat.</p>",
    "session_end": "<h4>SESSION ENDED</h4><p>Thank you for using our service!</p><p>Send /start whenever you want a new session.</p>",
    "talent_full": "<h4>{talent_name} IS BUSY</h4><p>Currently serving another customer.</p><p>Tap <b>Enable Notifications</b> to get notified when this talent is available again.</p>",
    "talent_detail": "<h4>{talent_name}</h4><p>{desc}</p><table><tbody><tr><td><p><strong>Price</strong></p></td><td><p><strong>{price}</strong></p></td></tr><tr><td><p><strong>Duration</strong></p></td><td><p>{duration} minutes</p></td></tr></tbody></table><p>Tap <b>Order</b> to continue.</p>",
    "channel_greeting": "<h4>WELCOME</h4><p><b>{talent_name}</b> is here for you.</p><p>Silahkan naik ke live stream di atas, saya siap melayani anda.</p><p>Your <b>{duration} minute</b> private session starts when you join the voice chat.</p>",
    "join_warning": "<h4>⏰ ARE YOU THERE?</h4><p><b>{talent_name}</b> is live and waiting for you.</p><p>Please join the video chat within <b>{remaining} minutes</b>, or your session will be marked as completed.</p>",
}


async def get_template(key: str) -> str:
    """Get message template by key."""
    doc = await templates_col.find_one({"key": key})
    if doc:
        return doc.get("content", DEFAULT_TEMPLATES.get(key, ""))
    return DEFAULT_TEMPLATES.get(key, "")


async def set_template(key: str, content: str):
    """Set/update message template."""
    await templates_col.update_one(
        {"key": key},
        {"$set": {"key": key, "content": content}},
        upsert=True
    )


async def get_all_templates() -> dict:
    """Get all templates."""
    result = {}
    for key in DEFAULT_TEMPLATES:
        result[key] = await get_template(key)
    return result
