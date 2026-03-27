import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange

from app.api.v1.payments import router as payments_router
from app.core.config import settings
from app.services.outbox import outbox_publisher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

broker = RabbitBroker(settings.RABBITMQ_URL)

# API только публикует — объявляем exchange, очередь декларирует воркер
payments_exchange = RabbitExchange("payments", type=ExchangeType.DIRECT, durable=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await broker.connect()
    await broker.declare_exchange(payments_exchange)

    # Запускаем outbox publisher как фоновую задачу
    publisher_task = asyncio.create_task(outbox_publisher(broker, payments_exchange))
    logger.info("API started")

    yield

    publisher_task.cancel()
    try:
        await publisher_task
    except asyncio.CancelledError:
        pass

    await broker.close()
    logger.info("API stopped")


app = FastAPI(
    title="Payment Processing Service",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(payments_router)
