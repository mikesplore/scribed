from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, Numeric, String, Text, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Contract(Base):
    __tablename__ = "contracts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    client_name: Mapped[str] = mapped_column(String(255))
    project_name: Mapped[str] = mapped_column(String(255))
    gatekeeper_project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    terms_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    pdf_path: Mapped[str] = mapped_column(Text)
    pdf_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DocumentSequence(Base):
    __tablename__ = "document_sequences"
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    client_name: Mapped[str] = mapped_column(String(255))
    project_name: Mapped[str] = mapped_column(String(255))
    client_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gatekeeper_project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    gatekeeper_service_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    terms_json: Mapped[str] = mapped_column(Text, default="{}")
    amount: Mapped[float] = mapped_column(Numeric(14, 2))
    base_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    total_adjustments: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    total_payments: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    outstanding_balance: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    pdf_path: Mapped[str] = mapped_column(Text)
    pdf_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    receipt_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    receipt_number: Mapped[str | None] = mapped_column(String(50), nullable=True, unique=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class InvoiceAdjustment(Base):
    __tablename__ = "invoice_adjustments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(Integer, index=True)
    gatekeeper_adjustment_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(20))
    amount: Mapped[float] = mapped_column(Numeric(14, 2))
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (CheckConstraint("type IN ('ADDITIONAL_CHARGE', 'DISCOUNT')"), CheckConstraint("amount > 0"))


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(Integer, index=True)
    provider: Mapped[str] = mapped_column(String(30))
    provider_reference: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    paid_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    receipt_number: Mapped[str] = mapped_column(String(60), unique=True)
    receipt_path: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(40))
    document_type: Mapped[str] = mapped_column(String(20))
    document_number: Mapped[str] = mapped_column(String(50))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
