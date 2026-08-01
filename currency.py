"""
Currency helper - live IDR/MYR exchange rate with cache + fallback.
Rate source: open.er-api.com (free, no API key).
"""

import time
import logging

import aiohttp

import database as db

logger = logging.getLogger(__name__)

RATE_API_URL = "https://open.er-api.com/v6/latest/MYR"
CACHE_TTL = 3 * 3600  # refresh setiap 3 jam
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)

_cache = {"rate": None, "ts": 0.0}


async def get_myr_rate() -> float:
    """Return IDR per 1 MYR. Live rate (cached), fallback to settings.myr_rate."""
    # Pakai cache kalau masih fresh
    if _cache["rate"] and time.time() - _cache["ts"] < CACHE_TTL:
        return _cache["rate"]

    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.get(RATE_API_URL) as resp:
                data = await resp.json()
                rate = float(data["rates"]["IDR"])
                if rate <= 0:
                    raise ValueError(f"invalid rate: {rate}")
                _cache["rate"] = rate
                _cache["ts"] = time.time()
                # Simpan sebagai fallback kalau API down nanti
                await db.update_settings(myr_rate=rate)
                logger.info(f"MYR rate updated: 1 MYR = Rp {rate:,.2f}")
                return rate
    except Exception as e:
        logger.warning(f"Fetch MYR rate failed, using fallback: {e}")

    # Fallback: cache lama > settings > default
    if _cache["rate"]:
        return _cache["rate"]
    settings = await db.get_settings()
    return float(settings.get("myr_rate", 3500))
