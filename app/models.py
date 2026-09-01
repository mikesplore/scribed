from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Contract(Base):
    __tablename__ = "contracts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    client_name: Mapped[str] = mapped_column(String(255))
    project_name: Mapped[str] = mapped_column(String(255))
    terms_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    pdf_path: Mapped[str] = mapped_column(Text)


class DocumentSequence(Base):
    __tablename__ = "document_sequences"
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    client_name: Mapped[str] = mapped_column(String(255))
    amount: Mapped[float] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    pdf_path: Mapped[str] = mapped_column(Text)
