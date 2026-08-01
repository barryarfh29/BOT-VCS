"""
Generate Session String untuk akun talent.
Jalankan: python gen_session.py
Login dengan nomor HP akun talent, lalu copy session string yang muncul.

Pastikan env API_ID dan API_HASH sudah di-set, atau buat file .env.
"""

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")

if not API_ID or not API_HASH:
    print("❌ Set API_ID dan API_HASH di environment variables atau file .env")
    sys.exit(1)

from pyrogram import Client

with Client("talent_session", api_id=API_ID, api_hash=API_HASH) as app:
    session_string = app.export_session_string()
    print("\n" + "=" * 55)
    print("✅ SESSION STRING (copy semua teks di bawah):")
    print("=" * 55)
    print(session_string)
    print("=" * 55)
    print("\nPaste session string ini ke bot saat set akun talent.")
