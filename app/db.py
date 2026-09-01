import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./scribed.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "sqlite":
        additions = {"contracts": [("accepted_at", "DATETIME")],
                     "invoices": [("client_email", "VARCHAR(255)"), ("paid_at", "DATETIME")]}
        with engine.begin() as connection:
            for table, columns in additions.items():
                existing = {column["name"] for column in inspect(engine).get_columns(table)}
                for name, definition in columns:
                    if name not in existing:
                        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def storage_path(kind: str, number: str) -> Path:
    path = Path(os.getenv("STORAGE_DIR", "storage")) / kind
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{number}.pdf"


def next_number(db, kind: str, prefix: str) -> str:
    from .models import DocumentSequence
    sequence = db.get(DocumentSequence, kind)
    if sequence is None:
        sequence = DocumentSequence(kind=kind, value=0)
        db.add(sequence)
        db.flush()
    sequence.value += 1
    db.flush()
    return f"{prefix}-{sequence.value:04d}"
