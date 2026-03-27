import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone

import httpx

from app.core.db import AsyncSessionLocal
from app.models.payment import Payment

logger = logging.getLogger(__name__)

# httpx клиент переиспользуется между вызовами (не пересоздаём на каждый webhook)
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=10.0)
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()


async def send_webhook(webhook_url: str, payload: dict) -> None:
    client = get_http_client()
    # Задержки перед каждой следующей попыткой (первая — без задержки)
    retry_delays = [1, 2, 4]

    for attempt in range(1, len(retry_delays) + 2):
        try:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
            logger.info("Webhook delivered to %s (attempt %d)", webhook_url, attempt)
            return
        except Exception as exc:
            logger.warning("Webhook attempt %d/%d failed for %s: %s", attempt, len(retry_delays) + 1, webhook_url, exc)
            if attempt <= len(retry_delays):
                await asyncio.sleep(retry_delays[attempt - 1])

    logger.error("Webhook delivery failed after all attempts: %s", webhook_url)


async def process_payment(payment_id: str) -> None:
    logger.info("Processing payment %s", payment_id)

    async with AsyncSessionLocal() as session:
        payment = await session.get(Payment, uuid.UUID(payment_id))
        if not payment:
            logger.error("Payment %s not found in DB", payment_id)
            return

        # Защита от повторной обработки при дублях из очереди (at-least-once delivery)
        if payment.status != "pending":
            logger.warning("Payment %s already processed (status=%s), skipping", payment_id, payment.status)
            return

        # Эмуляция обработки во внешнем шлюзе
        await asyncio.sleep(random.uniform(2, 5))
        new_status = "succeeded" if random.random() < 0.9 else "failed"

        payment.status = new_status
        payment.processed_at = datetime.now(timezone.utc)
        await session.commit()

        logger.info("Payment %s -> %s", payment_id, new_status)

        if payment.webhook_url:
            webhook_payload = {
                "payment_id": payment_id,
                "status": new_status,
                "amount": str(payment.amount),
                "currency": payment.currency,
                "processed_at": payment.processed_at.isoformat(),
            }
            await send_webhook(payment.webhook_url, webhook_payload)
