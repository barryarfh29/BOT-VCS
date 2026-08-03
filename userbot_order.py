"""
Userbot Order Handler — handle VCS orders via CS userbot (text-based, no buttons).
Customer chat ke userbot CS, ketik keyword/nama talent → proses order otomatis.
"""

import asyncio
import time
import re
import logging
from difflib import SequenceMatcher

from pyrogram import Client, filters
from pyrogram.types import Message

import database as db

logger = logging.getLogger(__name__)

# State per user: {user_id: {"step": ..., "talent": ..., "pkg_index": ..., ...}}
_ub_state = {}

# Default triggers (editable via DB settings.userbot_triggers)
DEFAULT_TRIGGERS = [
    "menu", "/menu", "katalog", "produk", "daftar", "list",
    "harga", "price", "berapa", "brp", "brpa",
    "halo", "hai", "hi", "hello", "hey",
    "mau order", "mau join", "nak join", "nak tengok",
    "order", "book", "beli", "buy",
    "paket", "pakej", "package",
]


def _fuzzy_match_talent(name: str, talents: list) -> dict | None:
    """Fuzzy match talent name. Returns best match or None."""
    name_lower = name.lower().strip()
    best = None
    best_score = 0

    for t in talents:
        t_name = t["name"].lower()
        # Exact match
        if name_lower == t_name:
            return t
        # Contains match
        if name_lower in t_name or t_name in name_lower:
            score = 0.9
            if score > best_score:
                best_score = score
                best = t
                continue
        # Fuzzy
        score = SequenceMatcher(None, name_lower, t_name).ratio()
        if score > best_score:
            best_score = score
            best = t

    return best if best_score >= 0.5 else None


async def _get_triggers() -> list:
    """Get trigger keywords from DB or default."""
    settings = await db.get_settings()
    custom = settings.get("userbot_triggers", [])
    if custom:
        return [t.strip().lower() for t in custom if t.strip()]
    return DEFAULT_TRIGGERS


async def _build_menu_text(user_id: int = None) -> str:
    """Build talent menu text with prices."""
    from ptb_handlers import format_price

    talents = await db.get_talents()
    online = [t for t in talents if not t.get("offline")]

    if not online:
        return "❌ Tidak ada talent yang tersedia saat ini."

    lines = ["Halo kak, selamat datang 👋\n"]
    lines.append("📝 DAFTAR TALENT:\n")

    for i, t in enumerate(online, 1):
        packages = t.get("packages") or []
        if packages:
            # Show cheapest and most expensive
            prices_sorted = sorted(packages, key=lambda p: p.get("price", 0))
            cheapest = prices_sorted[0]
            price_str = await format_price(int(cheapest["price"]), user_id) if user_id else f"Rp {cheapest['price']:,}"
            lbl = (cheapest.get("label") or "").strip() or f"{cheapest.get('duration', 0)}m"
            lines.append(f"  {i}. {t['name']} — mulai {price_str} ({lbl})")
        else:
            price_str = await format_price(int(t["price"]), user_id) if user_id else f"Rp {t['price']:,}"
            lines.append(f"  {i}. {t['name']} — {price_str} / {t['duration']}m")

    lines.append("\n✏️ Ketik nama talent untuk order.")
    lines.append("Contoh: Sharifah")

    return "\n".join(lines)


async def _build_package_text(talent: dict, user_id: int = None) -> str:
    """Build package selection text for a talent."""
    from ptb_handlers import format_price

    packages = talent.get("packages") or []
    name = talent["name"]

    if not packages:
        price_str = await format_price(int(talent["price"]), user_id) if user_id else f"Rp {talent['price']:,}"
        return (
            f"✅ {name}\n"
            f"Harga: {price_str}\n"
            f"Durasi: {talent['duration']} menit\n\n"
            f"Ketik 'ok' untuk lanjut bayar atau 'batal' untuk cancel."
        )

    lines = [f"✅ {name}\n", "Pilih paket:\n"]
    for i, p in enumerate(packages, 1):
        lbl = (p.get("label") or "").strip() or f"{p.get('duration', 0)} menit"
        price_str = await format_price(int(p["price"]), user_id) if user_id else f"Rp {p['price']:,}"
        lines.append(f"  {i}. {lbl} — {price_str}")

    lines.append(f"\nKetik nomor paket (1-{len(packages)}) atau 'batal'.")
    return "\n".join(lines)


async def _send_qr_and_poll(client: Client, user_id: int, chat_id: int, talent: dict, price: int, duration):
    """Create invoice, send QR, poll payment, start session."""
    from payment import create_invoice, check_invoice
    from session_manager import start_session
    from ptb_handlers import format_price

    talent_id = talent.get("id")
    talent_name = talent["name"]
    merchant_ref = f"UB-{user_id}-{talent_id}-{int(time.time())}"

    nominal = await format_price(price, user_id)

    await client.send_message(chat_id, f"⏳ Membuat invoice...")

    invoice = await create_invoice(
        amount=price, merchant_ref=merchant_ref,
        description=f"{talent_name} {duration}m",
        customer_name="Customer", expired_time=3600
    )

    if not invoice:
        await client.send_message(chat_id, "❌ Gagal membuat invoice. Coba lagi nanti.")
        _ub_state.pop(user_id, None)
        return

    invoice_id = invoice["invoice_id"]
    total = invoice["total_amount"]
    nominal = await format_price(total, user_id)

    # Send QR
    import base64
    from io import BytesIO
    qr_base64 = invoice.get("payment_info", {}).get("qr_image", "")

    qr_msg = None
    if qr_base64 and "base64" in qr_base64:
        img_data = qr_base64.split(",", 1)[1]
        qr_file = BytesIO(base64.b64decode(img_data))
        qr_file.name = "qris.png"
        qr_msg = await client.send_photo(chat_id, photo=qr_file)

    await client.send_message(
        chat_id,
        f"📱 **Invoice**\n\n"
        f"Talent: {talent_name}\n"
        f"Durasi: {duration} menit\n"
        f"Total: **{nominal}**\n\n"
        f"Scan QR di atas untuk bayar.\n"
        f"Pembayaran otomatis terdeteksi.\n\n"
        f"Ketik 'batal' untuk cancel."
    )

    # Save transaction
    await db.add_transaction({
        "invoice_id": invoice_id, "merchant_ref": merchant_ref,
        "user_id": user_id, "user_name": "UB_Customer",
        "amount": price, "total_amount": total,
        "talent": talent_name, "status": "PENDING",
        "created_at": time.time(), "via": "userbot",
    })

    # Update state
    _ub_state[user_id] = {
        "step": "waiting_payment",
        "invoice_id": invoice_id,
        "talent": talent,
        "price": price,
        "duration": duration,
        "chat_id": chat_id,
    }

    # Poll payment
    try:
        for _ in range(1200):  # 1 hour max
            await asyncio.sleep(3)

            # Check if cancelled
            state = _ub_state.get(user_id)
            if not state or state.get("step") != "waiting_payment":
                return

            inv = await check_invoice(invoice_id)
            if not inv:
                continue

            status = inv.get("status")
            if status == "PAID":
                await db.update_transaction(invoice_id, status="PAID")
                _ub_state.pop(user_id, None)

                # Delete QR
                if qr_msg:
                    try:
                        await qr_msg.delete()
                    except Exception:
                        pass

                await client.send_message(chat_id, "✅ Pembayaran dikonfirmasi!\n⏳ Menghubungkan ke talent...")
                await asyncio.sleep(2)

                # Start session — use bot wrapper for session (channel creation uses talent bot)
                await start_session(user_id, invoice_id, chat_id, talent)
                return

            elif status in ["EXPIRED", "CANCELLED"]:
                await db.update_transaction(invoice_id, status=status)
                _ub_state.pop(user_id, None)
                await client.send_message(chat_id, f"❌ Invoice {status.lower()}.")
                return
    except asyncio.CancelledError:
        pass


def register_userbot_handlers(client: Client):
    """Register message handlers on the CS userbot client."""

    @client.on_message(filters.private & filters.incoming & ~filters.me)
    async def on_private_message(c: Client, message: Message):
        user_id = message.from_user.id if message.from_user else 0
        if not user_id:
            return

        # Check if feature is enabled
        settings = await db.get_settings()
        if not settings.get("userbot_order_enabled", True):
            return

        chat_id = message.chat.id
        text = (message.text or message.caption or "").strip()
        is_sticker = bool(message.sticker)

        # Check if user is admin — skip auto-reply for admins
        admin_ids = await db.get_admin_ids()
        if user_id in admin_ids:
            return

        state = _ub_state.get(user_id)

        # --- State: waiting_payment ---
        if state and state.get("step") == "waiting_payment":
            if text.lower() in ("batal", "cancel", "stop"):
                _ub_state.pop(user_id, None)
                await message.reply("❌ Order dibatalkan.")
            # Ignore other messages during payment wait
            return

        # --- State: confirm_order (single price, no packages) ---
        if state and state.get("step") == "confirm_order":
            if text.lower() in ("ok", "yes", "ya", "lanjut", "bayar"):
                talent = state["talent"]
                price = talent["price"]
                duration = talent["duration"]
                asyncio.create_task(_send_qr_and_poll(c, user_id, chat_id, talent, price, duration))
                return
            elif text.lower() in ("batal", "cancel", "tidak", "no"):
                _ub_state.pop(user_id, None)
                await message.reply("❌ Order dibatalkan.")
                return

        # --- State: pick_package ---
        if state and state.get("step") == "pick_package":
            talent = state["talent"]
            packages = talent.get("packages") or []

            if text.lower() in ("batal", "cancel"):
                _ub_state.pop(user_id, None)
                await message.reply("❌ Order dibatalkan.")
                return

            # Try parse package number
            try:
                idx = int(text) - 1
                if 0 <= idx < len(packages):
                    pkg = packages[idx]
                    price = pkg.get("price", talent["price"])
                    duration = pkg.get("duration", talent["duration"])
                    # Build effective talent with package overrides
                    eff = dict(talent)
                    eff["price"] = price
                    eff["duration"] = duration
                    if pkg.get("video_index") is not None:
                        eff["_force_video_index"] = pkg["video_index"]
                    asyncio.create_task(_send_qr_and_poll(c, user_id, chat_id, eff, price, duration))
                    return
                else:
                    await message.reply(f"❌ Pilih 1-{len(packages)}")
                    return
            except ValueError:
                await message.reply(f"Ketik nomor paket (1-{len(packages)}) atau 'batal'.")
                return

        # --- No state: check triggers or talent name ---

        # Check sticker trigger
        if is_sticker:
            menu_text = await _build_menu_text(user_id)
            await message.reply(menu_text)
            return

        if not text:
            return

        # Check keyword triggers
        triggers = await _get_triggers()
        text_lower = text.lower()

        is_trigger = any(t in text_lower for t in triggers)

        if is_trigger:
            menu_text = await _build_menu_text(user_id)
            await message.reply(menu_text)
            return

        # Try match talent name
        talents = await db.get_talents()
        online = [t for t in talents if not t.get("offline")]

        if not online:
            return  # Don't reply if no talent available

        matched = _fuzzy_match_talent(text, online)

        if matched:
            # Check if talent is in session
            in_session = await db.get_session_by_talent(matched["id"])
            cooldown_at = await db.get_cooldown(matched["id"])
            if in_session or (cooldown_at and time.time() < cooldown_at):
                await message.reply(f"❌ {matched['name']} sedang tidak tersedia. Coba lagi nanti.")
                return

            # Show packages or confirm
            packages = matched.get("packages") or []
            if packages:
                _ub_state[user_id] = {"step": "pick_package", "talent": matched}
                pkg_text = await _build_package_text(matched, user_id)
                await message.reply(pkg_text)
            else:
                _ub_state[user_id] = {"step": "confirm_order", "talent": matched}
                confirm_text = await _build_package_text(matched, user_id)
                await message.reply(confirm_text)
            return

        # No match — don't reply (avoid noise for unrelated messages)
