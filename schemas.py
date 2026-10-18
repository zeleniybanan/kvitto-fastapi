"""Input validation and API response schemas."""

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Method(str, Enum):
    card = "card"
    sbp = "sbp"
    installment = "installment"


class Status(str, Enum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    refunded = "refunded"


class TariffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    price: int


class PaymentCreate(BaseModel):
    tariff_id: str
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    method: Method
    installment_months: Literal[3, 6, 12] | None = None
    promo_code: str | None = None

    @field_validator("promo_code")
    @classmethod
    def validate_promo(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.upper() != "KVITTO10":
            raise ValueError("unknown promo code")
        return "KVITTO10"

    @model_validator(mode="after")
    def validate_installment(self):
        if self.method == Method.installment and self.installment_months is None:
            raise ValueError("installment_months must be 3, 6 or 12")
        if self.method != Method.installment and self.installment_months is not None:
            raise ValueError("installment_months is only allowed for installment")
        return self


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: Status
    tariff_id: str
    amount: int
    discount: int
    method: Method
    installment_months: int | None
    schedule: list[int] | None
    email: str
    created_at: datetime


class BankWebhook(BaseModel):
    payment_id: str
    status: Status


class BankWebhookResult(BaseModel):
    result: Literal["ok"] = "ok"
