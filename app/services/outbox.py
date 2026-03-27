import asyncio
import logging

from faststream.rabbit import RabbitBroker, RabbitExchange
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.outbox import OutboxEvent

logger = logging.getLogger(__name__)

PAYMENTS_EXCHANGE = "payments"
POLL_INTERVAL = 2  # секунды между опросами таблицы outbox


async def outbox_publisher(broker: RabbitBroker, exchange: RabbitExchange) -> None:
    """
    Фоновый loop, который читает неопубликованные события из outbox
    и публикует их в RabbitMQ. Запускается один раз при старте API.

    Отдельный процесс публикации нужен чтобы гарантировать доставку:
    если приложение упадёт сразу после записи в БД — событие не потеряется,
    потому что оно уже в outbox и будет опубликовано после перезапуска.
    """
    logger.info("Outbox publisher started")

    while True:
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.published == False)  # noqa: E712
                    .order_by(OutboxEvent.created_at)
                    .limit(50)
                )
                events = result.scalars().all()

                for event in events:
                    try:
                        # Публикуем в exchange с routing_key = имя очереди,
                        # чтобы сообщение прошло через нашу топологию (и DLQ в случае чего)
                        await broker.publish(
                            event.payload,
                            exchange=exchange,
                            routing_key="payments.new",
                        )
                        event.published = True
                        logger.info("Published outbox event %s for payment %s", event.id, event.payment_id)
                    except Exception:
                        logger.exception("Failed to publish outbox event %s, will retry", event.id)

                if events:
                    await session.commit()

        except Exception:
            logger.exception("Outbox publisher iteration failed")

        await asyncio.sleep(POLL_INTERVAL)
