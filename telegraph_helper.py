"""
Telegraph Helper - Create rich message pages via Telegra.ph API
Uses aiohttp directly (no extra dependency needed)
"""

import aiohttp
import logging

logger = logging.getLogger(__name__)

TELEGRAPH_API = "https://api.telegra.ph"
_access_token = None


async def _get_token():
    """Get or create Telegraph account token."""
    global _access_token
    if _access_token:
        return _access_token

    import database as db
    settings = await db.get_settings()
    token = settings.get("telegraph_token")

    if token:
        _access_token = token
        return token

    # Create new account
    async with aiohttp.ClientSession() as session:
        resp = await session.post(f"{TELEGRAPH_API}/createAccount", json={
            "short_name": "StreamBot",
            "author_name": "Stream Service"
        })
        data = await resp.json()
        if data.get("ok"):
            token = data["result"]["access_token"]
            _access_token = token
            await db.update_settings(telegraph_token=token)
            return token
    return None


async def create_rich_page(title: str, html_content: str) -> str:
    """Create a Telegraph page and return the URL."""
    token = await _get_token()
    if not token:
        return ""

    async with aiohttp.ClientSession() as session:
        resp = await session.post(f"{TELEGRAPH_API}/createPage", json={
            "access_token": token,
            "title": title,
            "content": [{"tag": "p", "children": ["Loading..."]}],
            "author_name": "Stream Service",
            "return_content": False,
        })
        data = await resp.json()

        if not data.get("ok"):
            logger.error(f"Telegraph createPage failed: {data}")
            return ""

        path = data["result"]["path"]

        # Edit page with actual HTML content
        # Convert HTML to Telegraph node format
        nodes = html_to_nodes(html_content)

        resp2 = await session.post(f"{TELEGRAPH_API}/editPage", json={
            "access_token": token,
            "path": path,
            "title": title,
            "content": nodes,
            "author_name": "Stream Service",
            "return_content": False,
        })
        data2 = await resp2.json()

        if data2.get("ok"):
            return data2["result"]["url"]
        else:
            return f"https://telegra.ph/{path}"


def html_to_nodes(html: str) -> list:
    """Convert simple HTML to Telegraph node format."""
    # Simple parser - handle basic tags
    import re
    nodes = []
    # Split by tags
    parts = re.split(r'(<[^>]+>)', html)

    current_tag = None
    current_children = []

    for part in parts:
        if not part:
            continue
        if part.startswith('<') and not part.startswith('</'):
            # Opening tag
            tag_match = re.match(r'<(\w+)([^>]*)>', part)
            if tag_match:
                if current_tag and current_children:
                    nodes.append({"tag": current_tag, "children": current_children})
                    current_children = []
                current_tag = tag_match.group(1)
        elif part.startswith('</'):
            # Closing tag
            if current_children:
                nodes.append({"tag": current_tag or "p", "children": current_children})
            elif current_tag:
                nodes.append({"tag": current_tag, "children": [" "]})
            current_tag = None
            current_children = []
        else:
            # Text content
            if part.strip():
                if current_tag:
                    current_children.append(part)
                else:
                    nodes.append({"tag": "p", "children": [part]})

    if current_children:
        nodes.append({"tag": current_tag or "p", "children": current_children})

    if not nodes:
        nodes = [{"tag": "p", "children": [html]}]

    return nodes


async def create_payment_page(invoice_id, talent_name, duration, nominal) -> str:
    """Create rich payment page."""
    html = f"""
<h4>PEMBAYARAN QRIS</h4>
<table>
<tr><td><b>Order ID</b></td><td>{invoice_id}</td></tr>
<tr><td><b>Talent</b></td><td>{talent_name}</td></tr>
<tr><td><b>Durasi</b></td><td>{duration} menit</td></tr>
<tr><td><b>Nominal</b></td><td>Rp {nominal:,}</td></tr>
<tr><td><b>Status</b></td><td>Menunggu Pembayaran</td></tr>
</table>
<p>Scan kode QR untuk pembayaran. Otomatis terdeteksi.</p>
"""
    return await create_rich_page(f"Payment - {talent_name}", html)


async def create_session_page(talent_name, duration, invite_link) -> str:
    """Create rich session ready page."""
    html = f"""
<h4>SESI SIAP</h4>
<table>
<tr><td><b>Talent</b></td><td>{talent_name}</td></tr>
<tr><td><b>Durasi</b></td><td>{duration} menit</td></tr>
<tr><td><b>Status</b></td><td>Siap Melayani</td></tr>
</table>
<p><b>{talent_name}</b> sudah siap melayani kamu.</p>
<p>Timer dimulai saat kamu masuk voice chat.</p>
"""
    return await create_rich_page(f"Session - {talent_name}", html)
