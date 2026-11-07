"""Quick end-to-end check: scan barcodes, add/remove stock, transfer between stores."""
from db import SessionLocal, Store
from barcode import find_product
from inventory import adjust_stock, transfer_stock

scans = [
    "359676800309",                       # Symtuza bottle UPC
    "361958250115",                       # Biktarvy bottle UPC
    "300740554024",                       # Humira (Cardinal has no UPC, matched via NDC)
    "00361958250115",                     # Biktarvy as GTIN-14
    "0100361958250115172712311034ABC9",   # Biktarvy 2D DataMatrix with expiry + lot
    "999999999999",                       # unknown product
]

with SessionLocal() as s:
    if not s.query(Store).count():
        s.add_all([Store(name="Store 1"), Store(name="Store 2")]); s.commit()

    for raw in scans:
        p, extra = find_product(s, raw)
        if p:
            print(f"{raw:<36} -> {p.ndc} | {p.description} | {p.size} | pkg {p.package_size} {extra or ''}")
        else:
            print(f"{raw:<36} -> NOT FOUND (show 'add product' form)")

    p, _ = find_product(s, "361958250115")
    print("\nStore 1 receives 5:", adjust_stock(s, 1, p.ndc, +5, "received"))
    print("Store 1 dispenses 2:", adjust_stock(s, 1, p.ndc, -2, "dispensed"))
    transfer_stock(s, 1, 2, p.ndc, 1)
    print("After transfer of 1 to Store 2 ->",
          s.execute(__import__("sqlalchemy").text(
              "select store_id, quantity_on_hand from inventory order by store_id")).all())
    try:
        adjust_stock(s, 2, p.ndc, -5, "dispensed")
    except ValueError as e:
        s.rollback(); print("Blocked:", e)
