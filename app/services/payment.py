import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox import OutboxEvent
from app.models.payment import Payment
from app.schemas.payment import PaymentCreate


async def create_payment(session: AsyncSession, data: PaymentCreate, idempotency_key: str) -> Payment:
    # Сначала проверяем — вдруг такой ключ уже есть
    existing = await session.scalar(select(Payment).where(Payment.idempotency_key == idempotency_key))
    if existing:
        return existing

    payment = Payment(
        amount=data.amount,
        currency=data.currency,
        description=data.description,
        metadata_=data.metadata,
        status="pending",
        idempotency_key=idempotency_key,
        webhook_url=data.webhook_url,
    )
    session.add(payment)

    # Flush чтобы получить payment.id до создания outbox записи
    await session.flush()

    # Outbox event создаётся в той же транзакции
    outbox_event = OutboxEvent(
        payment_id=payment.id,
        event_type="payment.created",
        payload={
            "payment_id": str(payment.id),
            "amount": str(payment.amount),
            "currency": payment.currency,
        },
        published=False,
    )
    session.add(outbox_event)

    try:
        await session.commit()
    except IntegrityError:
        # Гонка: два одновременных запроса с одним ключом — второй проиграл на unique constraint
        # Откатываемся и возвращаем то что уже есть
        await session.rollback()
        existing = await session.scalar(select(Payment).where(Payment.idempotency_key == idempotency_key))
        return existing

    await session.refresh(payment)
    return payment


async def get_payment(session: AsyncSession, payment_id: uuid.UUID) -> Payment | None:
    return await session.get(Payment, payment_id)
