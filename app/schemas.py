from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class ContractRequest(BaseModel):
    client_name: str = Field(min_length=1)
    client_email: str | None = None
    project_name: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    deliverables: list[str] = Field(min_length=1)
    timeline: str = Field(min_length=1)
    amount: Decimal = Field(gt=0)
    currency: str = Field(default="KES", min_length=3, max_length=3)
    payment_schedule: str = Field(min_length=1)
    effective_date: date = Field(default_factory=date.today)
    contract_number: str = Field(default="DRAFT-CONTRACT")


class InvoiceRequest(BaseModel):
    client_name: str = Field(min_length=1)
    client_email: str | None = None
    project_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    amount: Decimal = Field(gt=0)
    currency: str = Field(default="KES", min_length=3, max_length=3)
    issue_date: date = Field(default_factory=date.today)
    due_date: date | None = None
    invoice_number: str = Field(default="DRAFT-INVOICE")
    payment_instructions: str = "Payment details will be shared by email."
