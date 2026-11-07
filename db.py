"""
Database tables for the multi-store pharmacy inventory.

Set the DATABASE_URL environment variable to your Neon connection string, e.g.
    postgresql://user:password@ep-xxxx.us-east-2.aws.neon.tech/neondb?sslmode=require
If it's not set, a local SQLite file (inventory.db) is used, which is handy for testing.
"""
import os
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer, Numeric, String,
    Index, UniqueConstraint, create_engine, inspect, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///inventory.db")
# Always use the psycopg (v3) driver for Postgres, whatever the URL starts with
for prefix in ("postgres://", "postgresql://", "postgresql+psycopg2://"):
    if DATABASE_URL.startswith(prefix):
        DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len(prefix):]
        break

# Neon's pooled connection (PgBouncer) doesn't like psycopg's automatic prepared statements
connect_args = {"prepare_threshold": None} if DATABASE_URL.startswith("postgresql") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class Store(Base):
    __tablename__ = "stores"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    address = Column(String(255))
    active = Column(Boolean, nullable=False, default=True)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    login = Column(String(100), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    store_id = Column(Integer, ForeignKey("stores.id"))  # null for the owner, who sees all stores
    role = Column(String(20), nullable=False, default="store")  # "store" (one per store) or "owner"
    active = Column(Boolean, nullable=False, default=True)


class Product(Base):
    """One row per drug, shared by all stores. Loaded from the Cardinal Health CSV."""
    __tablename__ = "products"
    ndc = Column(String(11), primary_key=True)        # 11 digits, leading zeros kept
    upc = Column(String(14), index=True)              # null when Cardinal says NOT_AVAILABLE
    description = Column(String(255), nullable=False) # e.g. "BIKTARVY 50-200-25MG TB 30"
    generic_name = Column(String(255))
    size = Column(String(50))                         # e.g. "30 EA", "60X10ML"
    package_size = Column(Numeric(10, 3))             # units per package: 30 tablets, or 0.5 (mL) for syringes
    brand_generic = Column(String(50))                # "Branded Drug", "Generic Drug", ...
    last_unit_price = Column(Numeric(12, 2))          # price on the most recent invoice
    last_invoice_date = Column(Date)


class Inventory(Base):
    """Current stock: one row per drug per store."""
    __tablename__ = "inventory"
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    ndc = Column(String(11), ForeignKey("products.ndc"), nullable=False)
    quantity_on_hand = Column(Integer, nullable=False, default=0)
    min_qty = Column(Integer, nullable=False, default=0)   # low-stock alert level (0 = no alert)
    last_updated = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("store_id", "ndc"),)


class Transaction(Base):
    """Audit trail: every stock change, who made it, and why."""
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    ndc = Column(String(11), ForeignKey("products.ndc"), nullable=False)
    change = Column(Integer, nullable=False)          # +10 received, -2 dispensed
    reason = Column(String(20), nullable=False)       # received / dispensed / adjusted / transfer
    user_id = Column(Integer, ForeignKey("users.id"))
    note = Column(String(255))
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class CountSession(Base):
    """A count day. While one is open, scans add to a tally; stock only changes at finalize."""
    __tablename__ = "count_sessions"
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="open")   # open / finalized / cancelled
    started_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_by = Column(String(100))
    finalized_at = Column(DateTime(timezone=True))
    finalized_by = Column(String(100))
    set_uncounted_to_zero = Column(Boolean, nullable=False, default=False)
    # summary saved when the count is finalized
    changed_count = Column(Integer)
    value_before = Column(Numeric(14, 2))
    value_after = Column(Numeric(14, 2))
    # at most one open count per store (works on Postgres and SQLite)
    __table_args__ = (
        Index("uq_one_open_count_per_store", "store_id", unique=True,
              postgresql_where=text("status = 'open'"), sqlite_where=text("status = 'open'")),
    )


class CountEntry(Base):
    """One scan during a count. Inserted, never overwritten; removals are marked, not erased."""
    __tablename__ = "count_entries"
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("count_sessions.id"), nullable=False, index=True)
    ndc = Column(String(11), ForeignKey("products.ndc"), nullable=False)
    qty = Column(Integer, nullable=False)
    counted_by = Column(String(100), nullable=False)
    location = Column(String(100))
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    edit_note = Column(String(255))          # who edited it and what it was
    deleted_at = Column(DateTime(timezone=True))
    deleted_by = Column(String(100))


def create_tables():
    Base.metadata.create_all(engine)
    _add_missing_columns()
    _migrate_roles()


# Columns added after the first version. create_all() doesn't alter existing tables,
# so add them here if a database was created before they existed.
_NEW_COLUMNS = {
    "stores": {"active": "BOOLEAN NOT NULL DEFAULT TRUE"},
    "users": {"active": "BOOLEAN NOT NULL DEFAULT TRUE"},
    "inventory": {"min_qty": "INTEGER NOT NULL DEFAULT 0"},
}


def _add_missing_columns():
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in _NEW_COLUMNS.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _migrate_roles():
    """Old roles -> new ones. admin becomes owner; staff and manager become store accounts.
    Only the role text changes; no rows are removed."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET role = 'owner' WHERE role = 'admin'"))
        conn.execute(text("UPDATE users SET role = 'store' WHERE role IN ('staff', 'manager')"))


if __name__ == "__main__":
    create_tables()
    print(f"Tables created in {DATABASE_URL.split('@')[-1]}")
