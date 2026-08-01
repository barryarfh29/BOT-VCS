"""
Configuration - All values from environment variables.
No hardcoded secrets. Copy .env.example to .env for local development.
"""

import os
import sys

# ============================================================
# REQUIRED — Bot will not start without these
# ============================================================

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# ============================================================
# PAYMENT GATEWAY
# ============================================================

PAYMENT_API_URL = os.environ.get("PAYMENT_API_URL", "https://botpayment.site/api/v1")
PAYMENT_API_KEY = os.environ.get("PAYMENT_API_KEY", "")
PAYMENT_SECRET = os.environ.get("PAYMENT_SECRET", "")

# ============================================================
# MONGODB
# ============================================================

MONGO_URI = os.environ.get("MONGO_URI", "")

# ============================================================
# BASE URL — Sistem "tanam domain/IP"
# Frontend dan API pakai ini sebagai base.
# Ganti VPS? Cukup update env BASE_URL ke IP/domain baru.
# Contoh: http://123.456.78.90:8080 atau https://admin.domain.com
# ============================================================

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")

# Allowed origins untuk CORS (comma-separated).
# Default: BASE_URL saja. Tambah kalau frontend di domain/port lain.
# Contoh: "http://localhost:3000,https://admin.domain.com"
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "")

# ============================================================
# API WEB ADMIN — secret untuk endpoint sensitif
# ============================================================

API_SECRET = os.environ.get("API_SECRET", "")

# ============================================================
# PATHS & MISC
# ============================================================

USERBOT_SESSION = os.environ.get("USERBOT_SESSION", "live_stream_bot")
VIDEO_FOLDER = os.environ.get("VIDEO_FOLDER", "videos")
LOG_FILE = os.environ.get("LOG_FILE", "bot.log")

# API Server port
API_PORT = int(os.environ.get("API_PORT", "8080"))

# Rate limiting: max requests per IP per window
RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "60"))
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))  # seconds

# Telegram Log Channels (disimpan di MongoDB via web admin, bukan env)
# Fallback ke env kalau belum diset di web
LOG_CHANNEL_START = int(os.environ.get("LOG_CHANNEL_START", "0"))
LOG_CHANNEL_PAYMENT = int(os.environ.get("LOG_CHANNEL_PAYMENT", "0"))

# ============================================================
# STARTUP VALIDATION
# ============================================================

def validate_config():
    """Check required env vars are set. Call at startup."""
    missing = []
    if not API_ID:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not MONGO_URI:
        missing.append("MONGO_URI")

    if missing:
        print("=" * 55)
        print("❌ MISSING REQUIRED ENVIRONMENT VARIABLES:")
        for m in missing:
            print(f"   - {m}")
        print("")
        print("Copy .env.example → .env and fill in your values.")
        print("=" * 55)
        sys.exit(1)
