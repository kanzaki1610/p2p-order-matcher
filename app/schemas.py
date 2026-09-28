from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OrderCreate(BaseModel):
    order_code: str = Field(min_length=3, max_length=100)
    side: Literal["SELL"] = "SELL"
    fiat_amount: Decimal = Field(gt=0, decimal_places=0)
    crypto_amount: Decimal | None = Field(default=None, gt=0)
    counterparty_name: str = Field(min_length=2, max_length=255)
    expected_bank: Literal["MB", "VIB", "VPBANK", "ACB"] | None = None
    payment_note: str | None = Field(default=None, max_length=255)
    expires_at: datetime | None = None


class OrderOut(OrderCreate):
    id: int
    status: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class BankTransactionIn(BaseModel):
    transaction_id: str = Field(min_length=3, max_length=150)
    amount: Decimal = Field(gt=0, decimal_places=0)
    direction: Literal["CREDIT"] = "CREDIT"
    sender_name: str | None = Field(default=None, max_length=255)
    sender_account: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at phải kèm múi giờ, ví dụ +07:00")
        return value


class MatchResult(BaseModel):
    bank_transaction_id: int
    decision: str
    order_code: str | None = None
    score: int = 0
    reasons: list[str] = Field(default_factory=list)


class SePayWebhookIn(BaseModel):
    """Payload giao dịch do SePay gửi; cho phép thêm trường để tương thích."""

    id: int | str
    gateway: str
    transactionDate: str
    accountNumber: str | None = None
    code: str | None = None
    content: str | None = None
    transferType: str
    transferAmount: Decimal = Field(gt=0)
    accumulated: Decimal | None = None
    subAccount: str | None = None
    referenceCode: str | None = None
    description: str | None = None

    model_config = ConfigDict(extra="allow")


class SePayWebhookOut(BaseModel):
    success: bool = True
    message: str
    decision: str | None = None
    order_code: str | None = None
    score: int | None = None
