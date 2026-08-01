"""
Payment Gateway Integration with retry + timeout
"""

import asyncio
import aiohttp
import logging
from config import PAYMENT_API_URL, PAYMENT_API_KEY

logger = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=30)


async def _request(method, url, headers, json=None, retries=3):
    """HTTP request with retry on timeout."""
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                if method == "GET":
                    async with session.get(url, headers=headers) as resp:
                        return await resp.json(), resp.status
                else:
                    async with session.post(url, json=json, headers=headers) as resp:
                        return await resp.json(), resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"Payment request attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(3)
    return None, 0


async def create_invoice(amount: int, merchant_ref: str, description: str = "", customer_name: str = "", expired_time: int = 3600):
    """Buat invoice QRIS baru."""
    url = f"{PAYMENT_API_URL}/invoice"
    headers = {"Content-Type": "application/json", "X-Api-Key": PAYMENT_API_KEY}
    body = {
        "amount": amount,
        "merchant_ref": merchant_ref,
        "description": description,
        "customer_name": customer_name,
        "expired_time": expired_time,
    }

    data, status = await _request("POST", url, headers, json=body)
    if data and status == 201 and data.get("success"):
        return data["data"]
    return None


async def check_invoice(invoice_id: str):
    """Cek status invoice."""
    url = f"{PAYMENT_API_URL}/invoice/{invoice_id}"
    headers = {"X-Api-Key": PAYMENT_API_KEY}

    data, status = await _request("GET", url, headers)
    if data and status == 200 and data.get("success"):
        return data["data"]
    return None


async def cancel_invoice(invoice_id: str):
    """Cancel invoice."""
    url = f"{PAYMENT_API_URL}/invoice/{invoice_id}/cancel"
    headers = {"X-Api-Key": PAYMENT_API_KEY}

    data, status = await _request("POST", url, headers)
    return data.get("success", False) if data else False
