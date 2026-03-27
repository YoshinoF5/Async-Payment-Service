import asyncio
import logging

import aio_pika
from faststream import FastStream
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange, RabbitQueue
from faststream.rabbit.annotations import RabbitMessage

from app.core.config import settings
from app.worker.consumer import close_http_client, process_payment

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

MAX_RETRIES = 3
# Задержки между попытками: 5s, 10s, 20s
RETRY_DELAYS_MS = [5_000, 10_000, 20_000]

payments_exchange = RabbitExchange("payments", type=ExchangeType.DIRECT, durable=True)

# Retry exchange: хендлер сам публикует сюда с нужным routing_key
retry_exchange = RabbitExchange("payments.retry", type=ExchangeType.DIRECT, durable=True)

dlx = RabbitExchange("payments.dead", type=ExchangeType.FANOUT, durable=True)
dlq = RabbitQueue("payments.dlq", durable=True)

# При финальном nack из основной очереди → DLQ
payments_queue = RabbitQueue(
    "payments.new",
    durable=True,
    arguments={"x-dead-letter-exchange": "payments.dead"},
)

broker = RabbitBroker(settings.RABBITMQ_URL)
app = FastStream(broker)


@broker.subscriber(payments_queue)
async def handle_payment(payload: dict, message: RabbitMessage) -> None:
    payment_id = payload.get("payment_id")
    if not payment_id:
        logger.error("Received message without payment_id: %s", payload)
        return

    retry_count = int(message.headers.get("x-retry-count", 0)) if message.headers else 0

    try:
        await process_payment(payment_id)
    except Exception as exc:
        if retry_count >= MAX_RETRIES:
            logger.error(
                "Payment %s failed after %d retries, sending to DLQ: %s",
                payment_id, retry_count, exc,
            )
            raise  # FastStream nack → x-dead-letter-exchange → DLQ

        next_retry = retry_count + 1
        delay_ms = RETRY_DELAYS_MS[retry_count]
        logger.warning(
            "Payment %s processing failed (attempt %d/%d), retry in %dms: %s",
            payment_id, retry_count + 1, MAX_RETRIES, delay_ms, exc,
        )
        # Публикуем в retry exchange с кастомным счётчиком в хедерах.
        # Retry-очередь с TTL вернёт сообщение обратно в payments exchange после задержки.
        await broker.publish(
            payload,
            exchange=retry_exchange,
            routing_key=f"retry.{next_retry}",
            headers={"x-retry-count": next_retry},
        )
        await message.ack()  # ack оригинальное — мы сами переложили в retry


@app.on_startup
async def setup_topology() -> None:
    """
    Декларируем всю топологию через aio-pika до старта брокера FastStream.
    @app.on_startup запускается до broker.start(), поэтому используем
    отдельное соединение aio-pika вместо broker.declare_*.
    """
    connection = await aio_pika.connect_robust(settings.RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()

        payments_ex = await channel.declare_exchange("payments", aio_pika.ExchangeType.DIRECT, durable=True)
        retry_ex = await channel.declare_exchange("payments.retry", aio_pika.ExchangeType.DIRECT, durable=True)
        dead_ex = await channel.declare_exchange("payments.dead", aio_pika.ExchangeType.FANOUT, durable=True)

        pq = await channel.declare_queue(
            "payments.new",
            durable=True,
            arguments={"x-dead-letter-exchange": "payments.dead"},
        )
        await pq.bind(payments_ex, routing_key="payments.new")

        for i, delay_ms in enumerate(RETRY_DELAYS_MS, start=1):
            rq = await channel.declare_queue(
                f"payments.retry.{i}",
                durable=True,
                arguments={
                    "x-message-ttl": delay_ms,
                    "x-dead-letter-exchange": "payments",
                    "x-dead-letter-routing-key": "payments.new",
                },
            )
            await rq.bind(retry_ex, routing_key=f"retry.{i}")

        dlq_q = await channel.declare_queue("payments.dlq", durable=True)
        await dlq_q.bind(dead_ex)

    logger.info(
        "Worker topology configured (%d retries, delays: %s ms)",
        MAX_RETRIES, RETRY_DELAYS_MS,
    )


@app.on_shutdown
async def cleanup() -> None:
    await close_http_client()


if __name__ == "__main__":
    asyncio.run(app.run())
