"""
Load the Cardinal Health CSV export into the products table.

Usage:
    python load_products.py cardinal_export.csv

Export settings in Cardinal: Format = CSV, Unformatted, All results.
Safe to re-run with a newer export: existing drugs are updated, new ones added.
"""
import sys

import pandas as pd
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db import Product, SessionLocal, create_tables, engine

# Cardinal column name (after removing the "Cardinal Health Billing " prefix) -> our column
COLUMN_MAP = {
    "NDC": "ndc",
    "UPC": "upc",
    "Product Description": "description",
    "Size": "size",
    "Package Size": "package_size",
    "Brand/Generic": "brand_generic",
    "Generic Name": "generic_name",
    "Invoice Date": "last_invoice_date",
    "Unit price (Current Invoice)": "last_unit_price",
}


def digits_or_none(value):
    """Keep only digits; return None for blanks and NOT_AVAILABLE."""
    if value is None or pd.isna(value):
        return None
    d = "".join(ch for ch in str(value) if ch.isdigit())
    return d or None


def read_cardinal_csv(path):
    # dtype=str keeps leading zeros on NDCs (00074055402 stays 00074055402)
    df = pd.read_csv(path, dtype=str, skipinitialspace=True)
    df.columns = [c.strip().replace("Cardinal Health Billing ", "") for c in df.columns]

    missing = [c for c in COLUMN_MAP if c not in df.columns]
    if missing:
        sys.exit(f"Missing columns in CSV: {missing}\nFound: {list(df.columns)}")

    df = df[list(COLUMN_MAP)].rename(columns=COLUMN_MAP)

    total = len(df)
    df["ndc"] = df["ndc"].map(digits_or_none)
    df["upc"] = df["upc"].map(digits_or_none)
    # Cardinal drops the leading zero on some UPC-A codes (11 digits) -> restore it,
    # so it matches what the scanner sends. 13-digit EAN codes are kept as-is.
    df["upc"] = df["upc"].map(lambda u: u.zfill(12) if isinstance(u, str) and len(u) == 11 else u)

    for col in ("description", "size", "brand_generic", "generic_name"):
        df[col] = df[col].fillna("").str.strip()

    # Rebates / pass-through charges ("NET GEN SRC RBT", "SERVICE BILLING") aren't stock.
    service = df["ndc"].isna() | (df["generic_name"].str.upper() == "SERVICE BILLING")
    df = df[~service].copy()
    df["ndc"] = df["ndc"].str.zfill(11)

    df["package_size"] = pd.to_numeric(df["package_size"], errors="coerce")
    df["last_unit_price"] = pd.to_numeric(df["last_unit_price"], errors="coerce")
    df["last_invoice_date"] = pd.to_datetime(df["last_invoice_date"], errors="coerce").dt.date

    # Each row is an invoice line, so the same drug appears many times.
    # Keep one row per NDC: the latest invoice date. $0 lines (free goods / credits)
    # are only used when a drug has no priced invoice at all. On a same-day tie,
    # the higher price wins (the paid line, not the $0 one).
    df["_priced"] = df["last_unit_price"].fillna(0) > 0
    df = df.sort_values(["ndc", "_priced", "last_invoice_date", "last_unit_price"],
                        na_position="first")
    before = len(df)
    df = df.drop_duplicates("ndc", keep="last").drop(columns="_priced")

    print(f"Read {total} rows: skipped {service.sum()} rebate/service rows, "
          f"{before - len(df)} duplicate invoice lines -> {len(df)} unique drugs")
    return df.sort_values("description")


def upsert_products(df):
    records = df.astype(object).where(df.notna(), None).to_dict("records")
    insert = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert

    with SessionLocal() as session:
        for start in range(0, len(records), 1000):   # batches keep Neon round-trips low
            batch = records[start:start + 1000]
            stmt = insert(Product).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["ndc"],
                set_={c: stmt.excluded[c] for c in COLUMN_MAP.values() if c != "ndc"},
            )
            session.execute(stmt)
        session.commit()
    print(f"Saved {len(records)} products")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python load_products.py <cardinal_export.csv>")
    create_tables()
    upsert_products(read_cardinal_csv(sys.argv[1]))
