"""
Rich Message sender - uses Telegram Bot API sendRichMessage (API 10.1+)
Bypasses Pyrogram, calls raw HTTP endpoint directly.
Supports: headings, paragraphs, tables, lists, dividers, code, bold, italic
"""

import socket
import time

import aiohttp
import logging
from config import BOT_TOKEN

logger = logging.getLogger(__name__)

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Timeout cepat: kalau rich message tidak respond dalam 3 detik, langsung fallback
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=3, connect=2)

# Circuit breaker: setelah gagal, skip rich message selama periode ini
RICH_MESSAGE_ENABLED = True
FAIL_COOLDOWN = 10  # detik — cooldown pendek, coba lagi cepat
_last_fail_ts = 0.0

MAX_ATTEMPTS = 1  # Satu kali coba, langsung fallback kalau gagal


async def _post_rich(payload: dict, label: str) -> dict:
    """POST ke sendRichMessage: timeout cepat, 1 attempt, fallback instant."""
    global _last_fail_ts

    if not RICH_MESSAGE_ENABLED:
        return {}

    # Baru saja gagal? Langsung fallback
    if time.time() - _last_fail_ts < FAIL_COOLDOWN:
        return {}

    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            # family=AF_INET: beberapa VPS punya IPv6 rusak -> connect timeout
            connector = aiohttp.TCPConnector(family=socket.AF_INET)
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, connector=connector) as session:
                async with session.post(f"{API_URL}/sendRichMessage", json=payload) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        return data.get("result", {})
                    # Error dari API (bukan network) -> tidak perlu retry/cooldown
                    logger.error(f"{label} failed: {data}")
                    return {}
        except Exception as e:
            last_err = e
            logger.warning(f"{label} attempt {attempt}/{MAX_ATTEMPTS} error: {e}")

    # Semua attempt gagal (network) -> aktifkan cooldown, biarkan caller fallback
    _last_fail_ts = time.time()
    logger.error(f"{label} error: {last_err} (rich message paused {FAIL_COOLDOWN}s)")
    return {}


async def send_rich_message(chat_id: int, markdown: str, reply_markup=None) -> dict:
    """Send a rich message using markdown format."""
    payload = {
        "chat_id": chat_id,
        "rich_message": {
            "markdown": markdown
        }
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    return await _post_rich(payload, "sendRichMessage")


async def send_rich_html(chat_id: int, html: str, reply_markup=None) -> dict:
    """Send a rich message using HTML format."""
    payload = {
        "chat_id": chat_id,
        "rich_message": {
            "html": html
        }
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    return await _post_rich(payload, "sendRichMessage HTML")


def template_to_rich_markdown(html_template: str, **kwargs) -> str:
    """Convert HTML template from web editor to Rich Markdown format."""
    import re
    text = html_template
    # Replace variables
    for key, val in kwargs.items():
        text = text.replace(f"{{{key}}}", str(val))

    # First: handle tables (before heading conversion to avoid ## inside cells)
    def convert_table(match):
        table_html = match.group(0)
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)
        md_rows = []
        for i, row in enumerate(rows):
            cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)
            clean_cells = []
            for c in cells:
                # Preserve bold/italic, strip other tags
                c = re.sub(r'<(b|strong)>(.*?)</\1>', r'**\2**', c)
                c = re.sub(r'<(i|em)>(.*?)</\1>', r'*\2*', c)
                c = re.sub(r'<[^>]+>', '', c).strip()
                clean_cells.append(c)
            md_rows.append('| ' + ' | '.join(clean_cells) + ' |')
            if i == 0:
                md_rows.append('| ' + ' | '.join(['---'] * len(clean_cells)) + ' |')
        # Baris kosong setelah tabel supaya tabel berurutan tidak menempel jadi satu
        return '\n'.join(md_rows) + '\n\n'

    text = re.sub(r'<table[^>]*>.*?</table>', convert_table, text, flags=re.DOTALL)

    # Lists (BEFORE paragraph conversion — <li><p>x</p></li> harus tetap dapat bullet/nomor)
    def convert_ul(match):
        items = re.findall(r'<li[^>]*>(.*?)</li>', match.group(1), re.DOTALL)
        return '\n'.join('- ' + re.sub(r'</?p[^>]*>', '', it).strip() for it in items) + '\n\n'

    def convert_ol(match):
        items = re.findall(r'<li[^>]*>(.*?)</li>', match.group(1), re.DOTALL)
        return '\n'.join(f'{i+1}. ' + re.sub(r'</?p[^>]*>', '', it).strip() for i, it in enumerate(items)) + '\n\n'

    text = re.sub(r'<ul[^>]*>(.*?)</ul>', convert_ul, text, flags=re.DOTALL)
    text = re.sub(r'<ol[^>]*>(.*?)</ol>', convert_ol, text, flags=re.DOTALL)

    # Convert headings (only outside tables now)
    text = re.sub(r'<h1[^>]*>(.*?)</h1>', r'# \1\n\n', text)
    text = re.sub(r'<h2[^>]*>(.*?)</h2>', r'## \1\n\n', text)
    text = re.sub(r'<h3[^>]*>(.*?)</h3>', r'### \1\n\n', text)
    text = re.sub(r'<h4[^>]*>(.*?)</h4>', r'#### \1\n\n', text)

    # Paragraphs and line breaks
    text = re.sub(r'<p[^>]*>(.*?)</p>', r'\1\n\n', text, flags=re.DOTALL)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<hr\s*/?>', '---\n\n', text)

    # Inline formatting
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text)
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', text)
    text = re.sub(r'<i>(.*?)</i>', r'*\1*', text)
    text = re.sub(r'<em>(.*?)</em>', r'*\1*', text)
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text)

    # Remove remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    # Clean whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def duration_display(talent: dict) -> str:
    """Teks durasi untuk customer: pakai duration_label kalau diisi, fallback angka asli."""
    label = (talent.get("duration_label") or "").strip()
    return label or str(talent.get("duration", ""))


def apply_duration_label(tpl: str, talent: dict) -> str:
    """Kalau talent punya duration_label, buang satuan bawaan template ('minutes'/'menit')
    di sebelah {duration} supaya label bebas tidak dobel satuan."""
    if not (talent.get("duration_label") or "").strip():
        return tpl
    for unit in ("{duration} minutes", "{duration} minute", "{duration} menit"):
        tpl = tpl.replace(unit, "{duration}")
    return tpl


def strip_price_duration_rows(tpl: str) -> str:
    """Buang baris tabel/paragraf yang memuat {price} atau {duration}.

    Dipakai di profil talent yang punya paket: harga & durasi sudah tampil di
    tombol paket, jadi baris tunggal di tabel disembunyikan supaya tidak dobel.
    """
    import re
    for var in ("price", "duration"):
        # Baris tabel yang memuat placeholder (ikut label di kolom sebelahnya)
        tpl = re.sub(r"<tr>(?:(?!</tr>).)*\{%s\}(?:(?!</tr>).)*</tr>" % var, "", tpl, flags=re.DOTALL)
        # Fallback: paragraf biasa yang memuat placeholder
        tpl = re.sub(r"<p>(?:(?!</p>).)*\{%s\}(?:(?!</p>).)*</p>" % var, "", tpl, flags=re.DOTALL)
    # Bersihkan tabel yang jadi kosong setelah semua barisnya dibuang
    tpl = re.sub(r"<table>\s*(?:<tbody>\s*</tbody>\s*)?</table>", "", tpl)
    return tpl


def render_template(html: str, **kwargs) -> str:
    """Convert HTML template to Telegram-friendly text with HTML parse mode (fallback)."""
    import re
    text = html
    # Replace variables
    for key, val in kwargs.items():
        text = text.replace(f"{{{key}}}", str(val))
    # Convert HTML to Telegram-supported format
    # TipTap menghasilkan <strong>/<em> — konversi ke <b>/<i> yang didukung Telegram
    text = re.sub(r'<strong>(.*?)</strong>', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'<em>(.*?)</em>', r'<i>\1</i>', text, flags=re.DOTALL)
    text = re.sub(r'<h[1-6][^>]*>(.*?)</h[1-6]>', r'<b>\1</b>\n', text)
    # Blok rich baru → padanan terdekat di pesan biasa Telegram
    # Fold: <details><summary>Judul</summary>isi</details> → blockquote expandable (native Telegram)
    def _details(m):
        inner = m.group(1)
        sm = re.search(r'<summary[^>]*>(.*?)</summary>', inner, re.DOTALL)
        summary = f"<b>{sm.group(1).strip()}</b>\n" if sm else ""
        body = re.sub(r'<summary[^>]*>.*?</summary>', '', inner, flags=re.DOTALL)
        return f'<blockquote expandable>{summary}{body}</blockquote>\n'
    text = re.sub(r'<details[^>]*>(.*?)</details>', _details, text, flags=re.DOTALL)
    # Pull quote (aside) → blockquote biasa
    text = re.sub(r'<aside[^>]*>(.*?)</aside>', r'<blockquote>\1</blockquote>\n', text, flags=re.DOTALL)
    # Footer → teks miring
    text = re.sub(r'<footer[^>]*>(.*?)</footer>', r'<i>\1</i>\n', text, flags=re.DOTALL)
    # Math (LaTeX) → monospace
    text = re.sub(r'<tg-math-block[^>]*>(.*?)</tg-math-block>', r'<pre>\1</pre>\n', text, flags=re.DOTALL)
    text = re.sub(r'<p[^>]*>(.*?)</p>', r'\1\n', text)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<hr\s*/?>', '─────────────────────\n', text)
    # Lists: kasih bullet/nomor sebelum tag di-strip
    text = re.sub(r'<ul[^>]*>(.*?)</ul>', lambda m: re.sub(r'<li[^>]*>\s*(.*?)\s*</li>', r'• \1\n', m.group(1), flags=re.DOTALL), text, flags=re.DOTALL)
    text = re.sub(r'<ol[^>]*>(.*?)</ol>', lambda m: '\n'.join(f'{i+1}. {it.strip()}' for i, it in enumerate(re.findall(r'<li[^>]*>(.*?)</li>', m.group(1), re.DOTALL))) + '\n', text, flags=re.DOTALL)
    # Tables: convert rows to lines
    text = re.sub(r'<table[^>]*>', '', text)
    text = re.sub(r'</table>', '', text)
    text = re.sub(r'<tr[^>]*>', '', text)
    text = re.sub(r'</tr>', '\n', text)
    text = re.sub(r'<td[^>]*>(.*?)</td>', r'\1  ', text)
    text = re.sub(r'<th[^>]*>(.*?)</th>', r'<b>\1</b>  ', text)
    # Keep <b>, <i>, <code>, <a>, <pre>, <blockquote> (Telegram supports these)
    # Remove all other tags
    text = re.sub(r'<(?!/?(?:b|i|code|a|pre|u|s|blockquote)\b)[^>]+>', '', text)
    # Clean up extra whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def markup_to_dict(markup) -> dict:
    """Convert pyrogram InlineKeyboardMarkup to raw dict for sendRichMessage payload."""
    if markup is None:
        return None
    if isinstance(markup, dict):
        return markup
    rows = []
    for row in markup.inline_keyboard:
        btns = []
        for b in row:
            btn = {"text": b.text}
            if getattr(b, "callback_data", None):
                btn["callback_data"] = b.callback_data
            elif getattr(b, "url", None):
                btn["url"] = b.url
            btns.append(btn)
        rows.append(btns)
    return {"inline_keyboard": rows}


def dict_to_markup(markup):
    """Convert raw dict keyboard to pyrogram InlineKeyboardMarkup (untuk fallback)."""
    if markup is None or not isinstance(markup, dict):
        return markup
    from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    rows = []
    for row in markup.get("inline_keyboard", []):
        rows.append([
            InlineKeyboardButton(b["text"], callback_data=b.get("callback_data"), url=b.get("url"))
            for b in row
        ])
    return InlineKeyboardMarkup(rows)


def template_to_rich_html(html_template: str, **kwargs) -> str:
    """Siapkan HTML template untuk dikirim langsung via sendRichMessage (field html).

    Bot API 10.1 mendukung tag: h1-h6, p, ul/ol/li, table, hr, blockquote, aside,
    footer, details/summary, pre/code, tg-math-block, b/strong, i/em, u, s, a.
    Jadi HTML dari editor TipTap bisa dikirim apa adanya — cukup ganti variabel.
    """
    import re
    text = html_template
    for key, val in kwargs.items():
        text = text.replace(f"{{{key}}}", str(val))
    # Fold (details) selalu default TERTUTUP: editor TipTap ikut menyimpan atribut
    # "open" kalau blok sedang terbuka saat di-save — buang supaya customer harus tap dulu
    text = re.sub(r'<details\b[^>]*>', '<details>', text)
    return text


async def send_template(client, chat_id: int, html_template: str, markup=None,
                        append_text: str = "", disable_preview: bool = True, **variables):
    """Kirim template: coba rich message dulu (tabel/heading tampil), fallback ke pesan biasa.

    Returns message_id (int) atau None kalau dua-duanya gagal.
    """
    from pyrogram import enums

    # Kirim HTML langsung: semua blok (quote, footer, fold, code, math, tabel) tampil native
    html = template_to_rich_html(html_template, **variables)
    if append_text:
        import html as _html
        html += "<p>" + _html.escape(append_text).replace("\n", "<br>") + "</p>"

    result = await send_rich_html(chat_id, html, reply_markup=markup_to_dict(markup))
    if result and result.get("message_id"):
        return result["message_id"]

    # Fallback: pesan biasa (tabel diratakan jadi baris)
    text = render_template(html_template, **variables)
    if append_text:
        text += f"\n\n{append_text}"
    try:
        msg = await client.send_message(
            chat_id, text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=dict_to_markup(markup),
            disable_web_page_preview=disable_preview,
        )
        return msg.id
    except Exception as e:
        logger.error(f"send_template fallback failed: {e}")
        return None
