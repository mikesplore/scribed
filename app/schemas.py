from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from typing import Annotated
import re

DocumentNumber = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,50}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=255)]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=8000)]
MAX_MONEY = Decimal("999999999999.99")


class RequestModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    @field_validator("amount", check_fields=False)
    @classmethod
    def finite_money(cls, value):
        if value is not None and (not value.is_finite() or value.as_tuple().exponent < -2):
            raise ValueError("amounts must be finite and have at most two decimal places")
        return value

    @field_validator("client_email", check_fields=False)
    @classmethod
    def valid_email(cls, value):
        if value is not None and (len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value)):
            raise ValueError("client_email must be a valid email address")
        return value


class APIClientRegistration(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    address: Annotated[str, StringConstraints(max_length=1000)] | None = None
    contact_email: Annotated[str, StringConstraints(max_length=254)] | None = None
    phone: Annotated[str, StringConstraints(max_length=40)] | None = None

    @field_validator("contact_email")
    @classmethod
    def valid_contact_email(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("contact_email must be a valid email address")
        return value


class ContractRequest(RequestModel):
    client_name: ShortText
    client_email: str | None = Field(default=None, max_length=254)
    project_name: ShortText
    scope: LongText
    deliverables: list[Annotated[str, StringConstraints(min_length=1, max_length=1000)]] = Field(min_length=1, max_length=50)
    timeline: LongText
    amount: Decimal = Field(gt=0, le=MAX_MONEY)
    currency: str = Field(default="KES", min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    payment_schedule: LongText
    deposit_percent: int | None = Field(default=None, ge=0, le=100)
    payment_due_days: int = Field(default=7, ge=0, le=90)
    late_payment_days: int = Field(default=7, ge=1, le=90)
    revision_rounds: int = Field(default=3, ge=0, le=20)
    effective_date: date = Field(default_factory=date.today)
    contract_number: DocumentNumber = Field(default="DRAFT-CONTRACT")


class InvoiceRequest(RequestModel):
    client_name: ShortText
    client_email: str | None = Field(default=None, max_length=254)
    project_name: ShortText
    description: LongText
    amount: Decimal = Field(gt=0, le=MAX_MONEY)
    currency: str = Field(default="KES", min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    issue_date: date = Field(default_factory=date.today)
    due_date: date | None = None
    invoice_number: DocumentNumber = Field(default="DRAFT-INVOICE")
    payment_instructions: Annotated[str, StringConstraints(max_length=2000)] = "Log in to your portal using your email address to pay."
    items: list[dict] | None = Field(default=None, max_length=100)
    paid_amount: Decimal = Field(default=Decimal("0.00"), ge=0, le=MAX_MONEY)
    balance_due: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY)
    payment_status: Annotated[str, StringConstraints(max_length=24)] | None = None

    @model_validator(mode="after")
    def validate_balance_due(self):
        expected = max(self.amount - self.paid_amount, Decimal("0.00"))
        if self.balance_due is not None and self.balance_due != expected:
            raise ValueError("balance_due must equal amount less paid_amount, floored at zero")
        if self.payment_status not in {None, "unpaid", "partially_paid", "paid"}:
            raise ValueError("payment_status must be unpaid, partially_paid, or paid")
        if self.items is not None and sum((item["amount"] for item in self.items), Decimal("0.00")) != self.amount:
            raise ValueError("invoice item totals must add up to the invoice amount")
        return self

    @field_validator("items")
    @classmethod
    def validate_items(cls, value: list[dict] | None) -> list[dict] | None:
        if value is None:
            return None
        for item in value:
            if not isinstance(item.get("name"), str) or not item["name"].strip() or len(item["name"]) > 255:
                raise ValueError("each invoice item needs a name of at most 255 characters")
            raw_amount = item.get("amount")
            try:
                amount = Decimal(str(raw_amount))
            except Exception as exc:
                raise ValueError("each invoice item needs a numeric amount") from exc
            if not amount.is_finite() or amount == 0 or abs(amount) > MAX_MONEY or amount.as_tuple().exponent < -2:
                raise ValueError("invoice item amounts must be non-zero, finite, and have at most two decimal places")
            try:
                quantity = Decimal(str(item.get("quantity", 1)))
                unit_price = Decimal(str(item.get("unit_price", amount / quantity)))
            except Exception as exc:
                raise ValueError("invoice item quantity and unit price must be numeric") from exc
            if not quantity.is_finite() or quantity <= 0 or quantity > MAX_MONEY or quantity.as_tuple().exponent < -3:
                raise ValueError("invoice item quantity must be positive with at most three decimal places")
            if not unit_price.is_finite() or abs(unit_price) > MAX_MONEY or unit_price.as_tuple().exponent < -2:
                raise ValueError("invoice item unit price must be finite and have at most two decimal places")
            if (quantity * unit_price).quantize(Decimal("0.01")) != amount:
                raise ValueError("invoice item total must equal quantity multiplied by unit price")
            item["amount"] = amount
            item["quantity"] = quantity
            item["unit_price"] = unit_price
        return value


    @field_validator("description")
    @classmethod
    def reject_placeholder_description(cls, value: str) -> str:
        if value.strip().lower() in {"sdfsdfsdfs", "test", "placeholder", "todo"}:
            raise ValueError("description must contain a real invoice description")
        return value


class ReceiptRequest(RequestModel):
    """Receipt content asserted by the caller; Scribed does not verify payment."""

    client_name: ShortText
    client_email: str | None = Field(default=None, max_length=254)
    project_name: ShortText
    description: LongText
    amount: Decimal = Field(gt=0, le=MAX_MONEY)
    currency: str = Field(default="KES", min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    received_date: date = Field(default_factory=date.today)
    payment_method: Annotated[str, StringConstraints(max_length=120)] | None = None
    payment_reference: Annotated[str, StringConstraints(max_length=255)] | None = None

    @field_validator("description")
    @classmethod
    def reject_placeholder_description(cls, value: str) -> str:
        if value.strip().lower() in {"sdfsdfsdfs", "test", "placeholder", "todo"}:
            raise ValueError("description must contain a real receipt description")
        return value


