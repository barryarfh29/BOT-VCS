"""
HTTP Long-Polling — reliable getUpdates via aiohttp.
"""

import asyncio
import logging
import aiohttp

from config import BOT_TOKEN

logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
POLL_TIMEOUT = 30


async def start_polling(bot_client):
    """Long-polling loop. Dispatch updates ke bot_client.dispatch_update()."""
    offset = 0
    retry_delay = 1
    logger.info("HTTP polling started — waiting for messages...")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                url = f"{TELEGRAM_API}/getUpdates?offset={offset}&timeout={POLL_TIMEOUT}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=POLL_TIMEOUT + 10)) as resp:
                    data = await resp.json()

                if not data.get("ok"):
                    err = data.get("description", "unknown")
                    code = data.get("error_code", 0)
                    logger.warning(f"getUpdates error ({code}): {err}")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 30)
                    continue

                retry_delay = 1
                updates = data.get("result", [])

                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        await bot_client.dispatch_update(update)
                    except Exception as e:
                        logger.error(f"Dispatch error: {e}", exc_info=True)

            except asyncio.CancelledError:
                logger.info("Polling stopped")
                break
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)
