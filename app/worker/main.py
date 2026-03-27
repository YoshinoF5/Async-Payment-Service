import asyncio
import logging

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
dlq = RabbitQueue("payments.dlq", durable=True, bind_to=dlx)

# При финальном nack из основной очереди → DLQ
payments_queue = RabbitQueue(
    "payments.new",
    durable=True,
    bind_to=payments_exchange,
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
    Декларируем всю топологию при старте воркера.
    Порядок важен: exchanges перед очередями, которые на них ссылаются.
    """
    await broker.declare_exchange(payments_exchange)
    await broker.declare_exchange(retry_exchange)
    await broker.declare_exchange(dlx)

    await broker.declare_queue(payments_queue)

    # Retry-очереди с разными TTL — сообщение ждёт, потом возвращается в payments.new
    for i, delay_ms in enumerate(RETRY_DELAYS_MS, start=1):
        retry_queue = RabbitQueue(
            f"payments.retry.{i}",
            durable=True,
            bind_to=retry_exchange,
            routing_key=f"retry.{i}",
            arguments={
                "x-message-ttl": delay_ms,
                "x-dead-letter-exchange": "payments",
                "x-dead-letter-routing-key": "payments.new",
            },
        )
        await broker.declare_queue(retry_queue)

    await broker.declare_queue(dlq)

    logger.info(
        "Worker started, retry topology configured (%d retries, delays: %s ms)",
        MAX_RETRIES, RETRY_DELAYS_MS,
    )


@app.on_shutdown
async def cleanup() -> None:
    await close_http_client()


if __name__ == "__main__":
    asyncio.run(app.run())
