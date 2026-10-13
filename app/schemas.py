from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from typing import Annotated
from urllib.parse import urlsplit
import re

DocumentNumber = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,50}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=255)]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=8000)]
MAX_MONEY = Decimal("999999999999.99")


class RequestModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    @field_validator("amount", "original_amount", "amount_paid", check_fields=False)
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
    gatekeeper_project_id: str | None = Field(default=None, max_length=64)


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
    payment_instructions: Annotated[str, StringConstraints(max_length=2000)] = "Payment details will be shared by email."
    gatekeeper_project_id: str | None = Field(default=None, max_length=64)
    gatekeeper_service_id: str | None = Field(default=None, max_length=64)
    original_amount: Decimal | None = Field(default=None, gt=0, le=MAX_MONEY)
    amount_paid: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY)
    payments: list[dict] = Field(default_factory=list, max_length=100)
    payment_portal_url: str | None = Field(default=None, max_length=2048)
    items: list[dict] | None = Field(default=None, max_length=100)

    @field_validator("payment_portal_url")
    @classmethod
    def secure_payment_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("payment_portal_url must be an HTTPS URL without embedded credentials")
        return value

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
            if not amount.is_finite() or amount <= 0 or amount > MAX_MONEY or amount.as_tuple().exponent < -2:
                raise ValueError("invoice item amounts must be positive, finite, and have at most two decimal places")
        return value

    @field_validator("description")
    @classmethod
    def reject_placeholder_description(cls, value: str) -> str:
        if value.strip().lower() in {"sdfsdfsdfs", "test", "placeholder", "todo"}:
            raise ValueError("description must contain a real invoice description")
        return value


class LedgerAdjustment(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    type: str = Field(pattern="^(ADDITIONAL_CHARGE|DISCOUNT)$")
    amount: Decimal = Field(gt=0, le=MAX_MONEY)
    reason: str = Field(min_length=1)
    actor: str = Field(min_length=1)

    @field_validator("amount")
    @classmethod
    def finite_adjustment(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value > MAX_MONEY or value.as_tuple().exponent < -2:
            raise ValueError("adjustment amount must be finite and have at most two decimal places")
        return value


class LedgerSyncRequest(BaseModel):
    project_id: str = Field(min_length=1)
    # Older Gatekeeper outbox events predate versioned ledger snapshots.
    ledger_version: str = Field(default="legacy-sync", pattern=r"^(?:legacy-sync|v?\d+(?:\.\d+)*)$", max_length=128)
    base_amount: Decimal = Field(ge=0, le=MAX_MONEY)
    additional_charges: Decimal = Field(default=Decimal("0"), ge=0, le=MAX_MONEY)
    discounts: Decimal = Field(default=Decimal("0"), ge=0, le=MAX_MONEY)
    # Legacy Gatekeeper payloads supplied one net adjustment total.
    total_adjustments: Decimal | None = Field(default=None)
    successful_payments: Decimal = Field(default=Decimal("0"), ge=0, le=MAX_MONEY)
    # Legacy Gatekeeper payloads supplied this field name.
    total_payments: Decimal | None = Field(default=None, ge=0)
    outstanding_balance: Decimal = Field(ge=0, le=MAX_MONEY)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    adjustments: list[LedgerAdjustment] = Field(default_factory=list)

    @field_validator("base_amount", "additional_charges", "discounts", "total_adjustments",
                     "successful_payments", "total_payments", "outstanding_balance")
    @classmethod
    def finite_ledger_money(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value.copy_abs() > MAX_MONEY or value.as_tuple().exponent < -2):
            raise ValueError("ledger amounts must be finite and have at most two decimal places")
        return value

    @property
    def net_adjustments(self) -> Decimal:
        if self.total_adjustments is not None:
            return self.total_adjustments
        return self.additional_charges - self.discounts

    @property
    def net_payments(self) -> Decimal:
        if self.total_payments is not None:
            return self.total_payments
        return self.successful_payments


class ServiceLedgerSyncRequest(BaseModel):
    service_id: str = Field(min_length=1, max_length=64)
    adjustments: list[LedgerAdjustment] = Field(default_factory=list)
