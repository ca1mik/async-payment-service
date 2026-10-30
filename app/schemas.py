import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, JsonValue, field_validator


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    amount: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]
    currency: Literal["RUB", "USD", "EUR"]
    description: str = Field(min_length=1, max_length=1000)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    webhook_url: AnyHttpUrl = Field(max_length=2048)

    @field_validator("webhook_url")
    @classmethod
    def no_url_credentials(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username or value.password or value.fragment:
            raise ValueError("Webhook URL must not contain credentials or a fragment")
        return value

    def fingerprint(self) -> str:
        data = self.model_dump(mode="json")
        data["amount"] = format(self.amount, ".2f")
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()


class PaymentAccepted(BaseModel):
    payment_id: UUID
    status: Literal["pending", "succeeded", "failed"]
    created_at: datetime


class PaymentDetail(PaymentAccepted):
    amount: Decimal
    currency: str
    description: str
    metadata: dict[str, JsonValue]
    idempotency_key: str
    webhook_url: str
    processed_at: datetime | None
    webhook_delivered_at: datetime | None
    attempts_completed: int
    last_error: str | None


class PaymentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_id: UUID
    attempt: Annotated[int, Field(strict=True, ge=1, le=3)]
