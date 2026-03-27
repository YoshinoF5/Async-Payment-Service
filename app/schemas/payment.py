import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, field_validator


class PaymentCreate(BaseModel):
    amount: Decimal
    currency: str
    description: str | None = None
    metadata: dict[str, Any] | None = None
    webhook_url: str | None = None

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        allowed = {"RUB", "USD", "EUR"}
        if v not in allowed:
            raise ValueError(f"currency must be one of {allowed}")
        return v

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("amount must be positive")
        return v


class PaymentCreateResponse(BaseModel):
    payment_id: uuid.UUID
    status: str
    created_at: datetime


class PaymentDetail(BaseModel):
    id: uuid.UUID
    amount: Decimal
    currency: str
    description: str | None
    metadata: dict[str, Any] | None
    status: str
    idempotency_key: str
    webhook_url: str | None
    created_at: datetime
    processed_at: datetime | None

    model_config = {"from_attributes": True}
