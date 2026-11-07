"""
Stock changes: the scan -> add/remove quantity step.
Every change updates the store's inventory row AND writes an audit transaction.
"""
from sqlalchemy import select

from db import Inventory, Transaction

VALID_REASONS = {"received", "dispensed", "adjusted", "transfer"}


def adjust_stock(session, store_id, ndc, change, reason, user_id=None, note=None, commit=True):
    """Add (change > 0) or remove (change < 0) stock. Returns the new quantity."""
    if reason not in VALID_REASONS:
        raise ValueError(f"reason must be one of {sorted(VALID_REASONS)}")
    if change == 0:
        raise ValueError("change can't be 0")

    row = session.scalars(
        select(Inventory)
        .where(Inventory.store_id == store_id, Inventory.ndc == ndc)
        .with_for_update()                    # stops two scans at once from clashing (Postgres)
    ).first()
    if row is None:
        row = Inventory(store_id=store_id, ndc=ndc, quantity_on_hand=0)
        session.add(row)

    new_qty = row.quantity_on_hand + change
    if new_qty < 0:
        raise ValueError(f"Not enough stock: have {row.quantity_on_hand}, tried to remove {-change}")

    row.quantity_on_hand = new_qty
    session.add(Transaction(store_id=store_id, ndc=ndc, change=change,
                            reason=reason, user_id=user_id, note=note))
    if commit:
        session.commit()
    return new_qty


def transfer_stock(session, from_store_id, to_store_id, ndc, qty, user_id=None, note=None):
    """Move stock between stores as one action (logged on both sides)."""
    note = note or f"store {from_store_id} -> store {to_store_id}"
    try:
        adjust_stock(session, from_store_id, ndc, -qty, "transfer", user_id, note, commit=False)
        adjust_stock(session, to_store_id, ndc, qty, "transfer", user_id, note, commit=False)
        session.commit()
    except Exception:
        session.rollback()
        raise
