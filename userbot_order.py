"""
Userbot Order Handler — handle VCS orders via CS userbot (text-based, no buttons).
Customer chat ke userbot CS, ketik keyword/nama talent → proses order otomatis.

Cleanup Rules:
- Self command (/menu, /order, angka, batal) dari operator → HAPUS command-nya saja
- Pesan biasa operator (chat/tanya) → JANGAN HAPUS
- Pesan customer → JANGAN PERNAH HAPUS
- QR + invoice dari bot → HAPUS saat bayar selesai atau batal
- Loading "Membuat invoice..." → HAPUS
- Menu / pilih paket / konfirmasi (pesan bot) → HAPUS saat step berikutnya
- Chat bebas kedua pihak → JANGAN HAPUS
"""

import asyncio
import time
import re
import logging
from difflib import SequenceMatcher

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram import enums

import database as db

logger = logging.getLogger(__name__)

# State per user: {user_id: {"step": ..., "talent": ..., "pkg_index": ..., ...}}
_ub_state = {}

# Track BOT-SENT message IDs per user for cleanup: {user_id: [msg_id, ...]}
_ub_bot_msgs = {}

# Default triggers (editable via DB settings.userbot_triggers)
DEFAULT_TRIGGERS = [
    "menu", "/menu", "katalog", "produk", "daftar", "list",
    "harga", "price", "berapa", "brp", "brpa",
    "mau order", "mau join", "nak join", "nak tengok",
    "order", "book", "beli", "buy",
    "paket", "pakej", "package",
]

CONFIRM_TIMEOUT = 120  # seconds


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

async def _clean_bot_msgs(client: Client, chat_id: int, user_id: int):
    """Hapus HANYA pesan bot yang sudah di-track. TIDAK hapus pesan customer/operator."""
    ids = _ub_bot_msgs.pop(user_id, [])
    for mid in ids:
        try:
            await client.delete_messages(chat_id, mid)
        except Exception:
            pass


async def _track_bot_msg(user_id: int, *msg_ids):
    """Track message IDs (bot-sent only) untuk dihapus di step berikutnya."""
    if user_id not in _ub_bot_msgs:
        _ub_bot_msgs[user_id] = []
    for mid in msg_ids:
        if mid:
            _ub_bot_msgs[user_id].append(mid)


async def _delete_self_cmd(message: Message):
    """Hapus command operator/self saja (bukan chat biasa)."""
    try:
        await message.delete()
    except Exception:
        pass


def _fuzzy_match_talent(name: str, talents: list) -> dict | None:
    """Fuzzy match talent name. Returns best match or None."""
    name_lower = name.lower().strip()
    best = None
    best_score = 0

    for t in talents:
        t_name = t["name"].lower()
        if name_lower == t_name:
            return t
        if name_lower in t_name or t_name in name_lower:
            score = 0.9
            if score > best_score:
                best_score = score
                best = t
                continue
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


# ============================================================
# TEXT BUILDERS (all from DB templates)
# ============================================================

async def _build_menu_text(user_id: int = None, first_name: str = "") -> str:
    """Build talent menu text with prices, using templates from DB."""
    from ptb_handlers import format_price

    talents = await db.get_talents()
    online = [t for t in talents if not t.get("offline")]

    if not online:
        tpl = await db.get_template("ub_unavailable")
        return (tpl or "❌ Tidak ada talent yang tersedia saat ini.").replace("{talent_name}", "")

    item_tpl = await db.get_template("ub_talent_item")
    if not item_tpl:
        item_tpl = "{num}. {name} — {price} ({label})"

    talent_lines = []
    for i, t in enumerate(online, 1):
        packages = t.get("packages") or []
        if packages:
            prices_sorted = sorted(packages, key=lambda p: p.get("price", 0))
            cheapest = prices_sorted[0]
            price_str = await format_price(int(cheapest["price"]), user_id) if user_id else f"Rp {cheapest['price']:,}"
            lbl = (cheapest.get("label") or "").strip() or f"{cheapest.get('duration', 0)}m"
        else:
            price_str = await format_price(int(t["price"]), user_id) if user_id else f"Rp {t['price']:,}"
            lbl = f"{t.get('duration', 0)}m"

        line = (item_tpl
                .replace("{num}", str(i))
                .replace("{name}", t["name"])
                .replace("{price}", price_str)
                .replace("{label}", lbl))
        talent_lines.append(line)

    talent_list = "\n".join(talent_lines)

    tpl = await db.get_template("ub_menu")
    if tpl and "{talent_list}" in tpl:
        mention = f"[{first_name or 'kak'}](tg://user?id={user_id})" if user_id else (first_name or "kak")
        result = (tpl
                  .replace("{talent_list}", talent_list)
                  .replace("{first_name}", first_name or "kak")
                  .replace("{user_id}", str(user_id or ""))
                  .replace("{mention}", mention))
        return result

    return f"Halo kak, selamat datang 👋\n\n📝 DAFTAR TALENT:\n\n{talent_list}\n\n✏️ Ketik nama talent untuk order.\nContoh: Sharifah"


async def _build_package_text(talent: dict, user_id: int = None) -> str:
    """Build package selection or confirm text for a talent."""
    from ptb_handlers import format_price

    packages = talent.get("packages") or []
    name = talent["name"]

    if not packages:
        price_str = await format_price(int(talent["price"]), user_id) if user_id else f"Rp {talent['price']:,}"
        tpl = await db.get_template("ub_confirm")
        if tpl:
            return (tpl
                    .replace("{talent_name}", name)
                    .replace("{price}", price_str)
                    .replace("{duration}", str(talent.get("duration", 0))))
        return (
            f"✅ {name}\n"
            f"Harga: {price_str}\n"
            f"Durasi: {talent.get('duration', 0)} menit\n\n"
            f"Ketik 'ok' untuk lanjut bayar atau 'batal' untuk cancel."
        )

    pkg_item_tpl = await db.get_template("ub_pkg_item")
    if not pkg_item_tpl:
        pkg_item_tpl = "{num}. {label} — {price}"

    pkg_lines = []
    for i, p in enumerate(packages, 1):
        lbl = (p.get("label") or "").strip() or f"{p.get('duration', 0)} menit"
        price_str = await format_price(int(p["price"]), user_id) if user_id else f"Rp {p['price']:,}"
        line = (pkg_item_tpl
                .replace("{num}", str(i))
                .replace("{label}", lbl)
                .replace("{price}", price_str))
        pkg_lines.append(line)

    package_list = "\n".join(pkg_lines)
    package_count = str(len(packages))

    tpl = await db.get_template("ub_package")
    if tpl:
        return (tpl
                .replace("{talent_name}", name)
                .replace("{package_list}", package_list)
                .replace("{package_count}", package_count))

    return f"✅ {name}\n\nPilih paket:\n{package_list}\n\nKetik nomor paket (1-{package_count}) atau 'batal'."


# ============================================================
# PAYMENT FLOW
# ============================================================

async def _send_qr_and_poll(client: Client, user_id: int, chat_id: int,
                            talent: dict, price: int, duration):
    """Create invoice, send QR, poll payment, start session on success."""
    from payment import create_invoice, check_invoice
    from session_manager import start_session
    from ptb_handlers import format_price

    talent_id = talent.get("id")
    talent_name = talent["name"]
    merchant_ref = f"UB-{user_id}-{talent_id}-{int(time.time())}"

    # Loading message (will be deleted)
    loading_msg = await client.send_message(chat_id, "⏳ Membuat invoice...")

    invoice = await create_invoice(
        amount=price, merchant_ref=merchant_ref,
        description=f"{talent_name} {duration}m",
        customer_name="Customer", expired_time=3600
    )

    if not invoice:
        try:
            await loading_msg.delete()
        except Exception:
            pass
        err_msg = await client.send_message(chat_id, "❌ Gagal membuat invoice. Coba lagi nanti.")
        await _track_bot_msg(user_id, err_msg.id)
        _ub_state.pop(user_id, None)
        return

    invoice_id = invoice["invoice_id"]
    total = invoice["total_amount"]
    nominal = await format_price(total, user_id)

    # Delete loading
    try:
        await loading_msg.delete()
    except Exception:
        pass

    # Send QR image
    import base64
    from io import BytesIO
    qr_base64 = invoice.get("payment_info", {}).get("qr_image", "")

    qr_msg = None
    if qr_base64 and "base64" in qr_base64:
        img_data = qr_base64.split(",", 1)[1]
        qr_file = BytesIO(base64.b64decode(img_data))
        qr_file.name = "qris.png"
        qr_msg = await client.send_photo(chat_id, photo=qr_file)

    # Invoice text message
    inv_tpl = await db.get_template("ub_invoice")
    if inv_tpl:
        inv_text = (inv_tpl
                    .replace("{talent_name}", talent_name)
                    .replace("{duration}", str(duration))
                    .replace("{nominal}", nominal))
    else:
        inv_text = (
            f"📱 **Invoice**\n\n"
            f"Talent: {talent_name}\n"
            f"Durasi: {duration} menit\n"
            f"Total: **{nominal}**\n\n"
            f"Scan QR di atas untuk bayar.\n"
            f"Pembayaran otomatis terdeteksi.\n\n"
            f"Ketik 'batal' untuk cancel."
        )

    inv_msg = await client.send_message(chat_id, inv_text, parse_mode=enums.ParseMode.MARKDOWN)

    # Track payment messages for cleanup (QR + invoice only)
    payment_msg_ids = [m.id for m in [qr_msg, inv_msg] if m]

    # Save transaction to DB
    await db.add_transaction({
        "invoice_id": invoice_id, "merchant_ref": merchant_ref,
        "user_id": user_id, "user_name": "UB_Customer",
        "amount": price, "total_amount": total,
        "talent": talent_name, "status": "PENDING",
        "created_at": time.time(), "via": "userbot",
    })

    # Update state to waiting_payment
    _ub_state[user_id] = {
        "step": "waiting_payment",
        "invoice_id": invoice_id,
        "talent": talent,
        "price": price,
        "duration": duration,
        "chat_id": chat_id,
        "payment_msg_ids": payment_msg_ids,
    }

    # Poll payment status
    try:
        for _ in range(1200):  # ~1 hour max
            await asyncio.sleep(3)

            state = _ub_state.get(user_id)
            if not state or state.get("step") != "waiting_payment":
                return  # Cancelled or state changed

            inv = await check_invoice(invoice_id)
            if not inv:
                continue

            status = inv.get("status")

            if status == "PAID":
                await db.update_transaction(invoice_id, status="PAID")
                _ub_state.pop(user_id, None)

                # Delete QR + invoice messages
                for mid in payment_msg_ids:
                    try:
                        await client.delete_messages(chat_id, mid)
                    except Exception:
                        pass

                # Send paid confirmation (briefly, then delete)
                paid_tpl = await db.get_template("ub_paid")
                paid_text = paid_tpl or "✅ Pembayaran dikonfirmasi!\n⏳ Menghubungkan ke talent..."
                confirm_msg = await client.send_message(
                    chat_id, paid_text, parse_mode=enums.ParseMode.MARKDOWN)
                await asyncio.sleep(2)
                try:
                    await confirm_msg.delete()
                except Exception:
                    pass

                # Start streaming session
                await start_session(user_id, invoice_id, chat_id, talent)
                return

            elif status in ("EXPIRED", "CANCELLED"):
                await db.update_transaction(invoice_id, status=status)
                _ub_state.pop(user_id, None)

                # Delete QR + invoice
                for mid in payment_msg_ids:
                    try:
                        await client.delete_messages(chat_id, mid)
                    except Exception:
                        pass

                expired_tpl = await db.get_template("ub_expired")
                expired_text = (expired_tpl or "❌ Invoice {status}.").replace("{status}", status.lower())
                exp_msg = await client.send_message(chat_id, expired_text, parse_mode=enums.ParseMode.MARKDOWN)
                await _track_bot_msg(user_id, exp_msg.id)
                return

    except asyncio.CancelledError:
        pass


# ============================================================
# FLOW HELPERS
# ============================================================

async def _show_menu(client: Client, chat_id: int, user_id: int, first_name: str = ""):
    """Clean old bot messages and show fresh menu."""
    await _clean_bot_msgs(client, chat_id, user_id)
    menu_text = await _build_menu_text(user_id, first_name)
    reply = await client.send_message(chat_id, menu_text, parse_mode=enums.ParseMode.MARKDOWN)
    await _track_bot_msg(user_id, reply.id)


async def _show_packages(client: Client, chat_id: int, user_id: int, talent: dict):
    """Show package selection for talent."""
    await _clean_bot_msgs(client, chat_id, user_id)
    packages = talent.get("packages") or []
    if packages:
        _ub_state[user_id] = {"step": "pick_package", "talent": talent}
        pkg_text = await _build_package_text(talent, user_id)
    else:
        _ub_state[user_id] = {"step": "confirm_order", "talent": talent}
        pkg_text = await _build_package_text(talent, user_id)
    reply = await client.send_message(chat_id, pkg_text, parse_mode=enums.ParseMode.MARKDOWN)
    await _track_bot_msg(user_id, reply.id)


async def _cancel_and_show_menu(client: Client, chat_id: int, user_id: int, first_name: str = ""):
    """Cancel current state and return to menu."""
    state = _ub_state.pop(user_id, None)
    # Delete payment QR/invoice if in waiting_payment
    if state and state.get("step") == "waiting_payment":
        for mid in state.get("payment_msg_ids", []):
            try:
                await client.delete_messages(chat_id, mid)
            except Exception:
                pass
    else:
        await _clean_bot_msgs(client, chat_id, user_id)

    # Send cancelled template
    cancelled_tpl = await db.get_template("ub_cancelled")
    if cancelled_tpl:
        cancel_msg = await client.send_message(chat_id, cancelled_tpl, parse_mode=enums.ParseMode.MARKDOWN)
        await _track_bot_msg(user_id, cancel_msg.id)

    await _show_menu(client, chat_id, user_id, first_name)


async def _start_payment(client: Client, chat_id: int, user_id: int, talent: dict):
    """Initiate payment flow for a resolved talent+package."""
    await _clean_bot_msgs(client, chat_id, user_id)
    asyncio.create_task(
        _send_qr_and_poll(client, user_id, chat_id, talent, talent["price"], talent.get("duration", 0))
    )


async def _get_first_name(client: Client, chat_id: int) -> str:
    """Get peer's first_name for template variables."""
    try:
        peer = await client.get_users(chat_id)
        return peer.first_name or "kak"
    except Exception:
        return "kak"


async def _check_talent_available(talent: dict, user_id: int) -> bool:
    """Check if talent is in session or on cooldown for this user."""
    talent_id = talent.get("id")
    in_session = await db.get_session_by_talent(talent_id)
    if in_session:
        return False
    cooldown_at = await db.get_cooldown(talent_id, user_id=user_id)
    talent_cd_setting = talent.get("cooldown", 0)
    if cooldown_at and time.time() < cooldown_at and talent_cd_setting > 0:
        return False
    return True


# ============================================================
# SELF-ORDER HANDLERS (operator commands in customer chat)
# ============================================================

async def _handle_self_menu(client: Client, message: Message, chat_id: int):
    """Handle /menu from self (operator)."""
    await _delete_self_cmd(message)
    first_name = await _get_first_name(client, chat_id)
    await _show_menu(client, chat_id, chat_id, first_name)


async def _handle_self_order(client: Client, message: Message, chat_id: int, talent_name: str):
    """Handle /order <name> from self (operator)."""
    await _delete_self_cmd(message)
    await _clean_bot_msgs(client, chat_id, chat_id)

    talents = await db.get_talents()
    online = [t for t in talents if not t.get("offline")]
    text_clean = re.sub(r'[^\w\s\-.]', '', talent_name).strip()
    matched = _fuzzy_match_talent(text_clean, online) if online else None

    if matched:
        await _show_packages(client, chat_id, chat_id, matched)
    else:
        err = await client.send_message(chat_id, f"❌ Talent '{talent_name}' tidak ditemukan.")
        await _track_bot_msg(chat_id, err.id)


async def _handle_self_batal(client: Client, message: Message, chat_id: int):
    """Handle batal from self (operator)."""
    await _delete_self_cmd(message)
    first_name = await _get_first_name(client, chat_id)
    await _cancel_and_show_menu(client, chat_id, chat_id, first_name)


async def _handle_self_state(client: Client, message: Message, chat_id: int, text: str):
    """Handle state-based input from self (angka, ok, batal)."""
    state = _ub_state.get(chat_id)
    if not state:
        return False

    text_lower = text.lower()

    # Batal in any state
    if text_lower in ("batal", "cancel", "stop"):
        await _delete_self_cmd(message)
        first_name = await _get_first_name(client, chat_id)
        await _cancel_and_show_menu(client, chat_id, chat_id, first_name)
        return True

    step = state.get("step")

    if step == "waiting_payment":
        # Only batal is handled above; let other messages pass through
        return False

    if step == "pick_package":
        talent = state["talent"]
        packages = talent.get("packages") or []
        try:
            idx = int(text) - 1
            if 0 <= idx < len(packages):
                await _delete_self_cmd(message)
                pkg = packages[idx]
                eff = dict(talent)
                eff["price"] = pkg.get("price", talent["price"])
                eff["duration"] = pkg.get("duration", talent.get("duration", 0))
                if pkg.get("video_index") is not None:
                    eff["_force_video_index"] = pkg["video_index"]
                await _start_payment(client, chat_id, chat_id, eff)
                return True
        except (ValueError, TypeError):
            pass
        return False

    if step == "confirm_order":
        if text_lower in ("ok", "yes", "ya", "lanjut", "bayar"):
            await _delete_self_cmd(message)
            talent = state["talent"]
            await _start_payment(client, chat_id, chat_id, talent)
            return True
        return False

    return False


# ============================================================
# CUSTOMER HANDLERS
# ============================================================

async def _handle_customer_cancel(client: Client, message: Message, user_id: int, chat_id: int):
    """Customer says batal/cancel."""
    first_name = message.from_user.first_name if message.from_user else ""
    await _cancel_and_show_menu(client, chat_id, user_id, first_name)


async def _handle_customer_waiting_payment(client: Client, message: Message,
                                           user_id: int, chat_id: int, text: str):
    """Handle customer message while waiting for payment."""
    if text.lower() in ("batal", "cancel", "stop"):
        await _handle_customer_cancel(client, message, user_id, chat_id)
        return True
    # Ignore other messages during payment — don't reply, don't delete
    return True


async def _handle_customer_confirm(client: Client, message: Message,
                                   user_id: int, chat_id: int, text: str):
    """Handle customer message during confirm_order state."""
    state = _ub_state.get(user_id)
    if not state:
        return False

    text_lower = text.lower()

    if text_lower in ("ok", "yes", "ya", "iya", "lanjut", "bayar"):
        talent = state["talent"]
        await _start_payment(client, chat_id, user_id, talent)
        return True

    if text_lower in ("batal", "cancel", "tidak", "no"):
        first_name = message.from_user.first_name if message.from_user else ""
        await _cancel_and_show_menu(client, chat_id, user_id, first_name)
        return True

    return False


async def _handle_customer_pick_package(client: Client, message: Message,
                                        user_id: int, chat_id: int, text: str):
    """Handle customer message during pick_package state."""
    state = _ub_state.get(user_id)
    if not state:
        return False

    text_lower = text.lower()
    talent = state["talent"]
    packages = talent.get("packages") or []

    if text_lower in ("batal", "cancel"):
        first_name = message.from_user.first_name if message.from_user else ""
        await _cancel_and_show_menu(client, chat_id, user_id, first_name)
        return True

    # Try parse package number
    try:
        idx = int(text) - 1
        if 0 <= idx < len(packages):
            pkg = packages[idx]
            eff = dict(talent)
            eff["price"] = pkg.get("price", talent["price"])
            eff["duration"] = pkg.get("duration", talent.get("duration", 0))
            if pkg.get("video_index") is not None:
                eff["_force_video_index"] = pkg["video_index"]
            await _start_payment(client, chat_id, user_id, eff)
            return True
        else:
            reply = await client.send_message(chat_id, f"❌ Pilih 1-{len(packages)}")
            await _track_bot_msg(user_id, reply.id)
            return True
    except ValueError:
        reply = await client.send_message(
            chat_id, f"Ketik nomor paket (1-{len(packages)}) atau 'batal'.")
        await _track_bot_msg(user_id, reply.id)
        return True


async def _handle_customer_no_state(client: Client, message: Message,
                                    user_id: int, chat_id: int, text: str, is_sticker: bool):
    """Handle customer message when no active order state."""
    first_name = message.from_user.first_name if message.from_user else ""

    # Sticker triggers menu
    if is_sticker:
        await _show_menu(client, chat_id, user_id, first_name)
        return

    if not text:
        return

    text_clean = re.sub(r'[^\w\s\-.]', '', text).strip()
    text_lower = text.lower()

    # Try match talent by name or number FIRST
    talents = await db.get_talents()
    online = [t for t in talents if not t.get("offline")]
    matched = None

    if online:
        # Number-based selection
        try:
            num = int(text_clean)
            if 1 <= num <= len(online):
                matched = online[num - 1]
        except ValueError:
            pass

        # Fuzzy match by name
        if not matched and text_clean:
            matched = _fuzzy_match_talent(text_clean, online)

    if matched:
        # Check availability (per-user cooldown)
        available = await _check_talent_available(matched, user_id)
        if not available:
            unavail_tpl = await db.get_template("ub_unavailable")
            unavail_text = (unavail_tpl or "❌ {talent_name} sedang tidak tersedia. Coba lagi nanti.").replace(
                "{talent_name}", matched["name"])
            reply = await client.send_message(chat_id, unavail_text, parse_mode=enums.ParseMode.MARKDOWN)
            await _track_bot_msg(user_id, reply.id)
            return

        # Show packages/confirm
        await _show_packages(client, chat_id, user_id, matched)
        return

    # No talent match — check keyword triggers
    triggers = await _get_triggers()
    is_trigger = any(t in text_lower for t in triggers)

    if is_trigger:
        await _show_menu(client, chat_id, user_id, first_name)
        return

    # No match — don't reply (silent, chat bebas)


# ============================================================
# MAIN HANDLER REGISTRATION
# ============================================================

def register_userbot_handlers(client: Client):
    """Register message handlers on the CS userbot client."""

    @client.on_message(filters.private & ~filters.channel)
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

        # Detect self (operator/userbot's own account)
        me = await c.get_me()
        is_self = (user_id == me.id)

        # ============================================================
        # SELF MESSAGES (operator helping customer)
        # ============================================================
        if is_self:
            text_lower = text.lower()

            # /menu
            if text_lower == "/menu":
                await _handle_self_menu(c, message, chat_id)
                return

            # /order <name>
            if text_lower.startswith("/order "):
                talent_name = text[7:].strip()
                if talent_name:
                    await _handle_self_order(c, message, chat_id, talent_name)
                return

            # /batal
            if text_lower in ("/batal", "batal", "cancel"):
                state = _ub_state.get(chat_id)
                if state:
                    await _handle_self_batal(c, message, chat_id)
                    return
                # No state, just delete the command
                await _delete_self_cmd(message)
                return

            # State-based self input (angka, ok)
            state = _ub_state.get(chat_id)
            if state:
                handled = await _handle_self_state(c, message, chat_id, text)
                if handled:
                    return

            # Self-message not a command and not state-related:
            # DO NOT delete, DO NOT respond — it's free chat from operator
            return

        # ============================================================
        # ADMIN MESSAGES
        # ============================================================
        admin_ids = await db.get_admin_ids()
        if user_id in admin_ids:
            text_lower = text.lower()

            # Admin commands: /menu, /order to help customer
            if text_lower == "/menu":
                await _delete_self_cmd(message)
                first_name = await _get_first_name(c, chat_id)
                await _show_menu(c, chat_id, chat_id, first_name)
                return

            if text_lower.startswith("/order "):
                talent_name = text[7:].strip()
                if talent_name:
                    await _delete_self_cmd(message)
                    await _handle_self_order(c, message, chat_id, talent_name)
                return

            if text_lower in ("/batal", "batal"):
                state = _ub_state.get(chat_id)
                if state:
                    await _delete_self_cmd(message)
                    first_name = await _get_first_name(c, chat_id)
                    await _cancel_and_show_menu(c, chat_id, chat_id, first_name)
                return

            # Regular admin message → SKIP auto-reply (free chat)
            return

        # ============================================================
        # CUSTOMER MESSAGES
        # ============================================================
        state = _ub_state.get(user_id)

        # --- State: waiting_payment ---
        if state and state.get("step") == "waiting_payment":
            await _handle_customer_waiting_payment(c, message, user_id, chat_id, text)
            return

        # --- State: confirm_order ---
        if state and state.get("step") == "confirm_order":
            handled = await _handle_customer_confirm(c, message, user_id, chat_id, text)
            if handled:
                return

        # --- State: pick_package ---
        if state and state.get("step") == "pick_package":
            handled = await _handle_customer_pick_package(c, message, user_id, chat_id, text)
            if handled:
                return

        # --- No state: trigger/talent detection ---
        await _handle_customer_no_state(c, message, user_id, chat_id, text, is_sticker)
