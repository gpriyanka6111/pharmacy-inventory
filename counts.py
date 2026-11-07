"""
Count day: several people count the same store at once.

While a count is open, every scan INSERTS a count entry (a tally). Stock does not change until the
count is finalized, so two people counting the same drug in different places add up instead of
overwriting each other.
"""
from collections import OrderedDict

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from db import CountEntry, CountSession, Inventory, Product, utcnow
from inventory import adjust_stock

BLOCK_MESSAGE = "Finish or cancel the count before receiving or dispensing."


class CountError(Exception):
    """A problem the person can fix; the message is safe to show."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def clean_name(value):
    return " ".join(str(value or "").split())[:40]


def open_session(db, store_id):
    if not store_id:
        return None
    return db.scalars(select(CountSession).where(
        CountSession.store_id == store_id, CountSession.status == "open")).first()


def start_count(db, store_id, started_by):
    if open_session(db, store_id):
        raise CountError("A count is already in progress for this store.", 409)
    sess = CountSession(store_id=store_id, status="open", started_by=started_by)
    db.add(sess)
    try:
        db.commit()
    except IntegrityError:           # two people pressed Start at the same moment
        db.rollback()
        raise CountError("A count is already in progress for this store.", 409)
    return sess


def _locked_open(db, session_id):
    """The session, only if still open. A shared row lock (Postgres) keeps finalize from
    running while an entry is being added; entries themselves never block each other."""
    sess = db.scalars(select(CountSession).where(CountSession.id == session_id)
                      .with_for_update(read=True)).first()
    if sess is None or sess.status != "open":
        db.rollback()
        raise CountError("No count is open. Reload the page.", 409)
    return sess


def add_entry(db, session_id, ndc, qty, counted_by, location=None):
    if not counted_by:
        raise CountError("Enter your name before counting.")
    if qty < 0:
        raise CountError("A count can't be negative.")
    _locked_open(db, session_id)
    entry = CountEntry(session_id=session_id, ndc=ndc, qty=qty, counted_by=counted_by,
                       location=(location or "").strip()[:100] or None)
    db.add(entry)
    db.commit()
    return entry


def entries_for(db, session_id, ndc=None, include_deleted=False):
    stmt = select(CountEntry).where(CountEntry.session_id == session_id)
    if ndc:
        stmt = stmt.where(CountEntry.ndc == ndc)
    if not include_deleted:
        stmt = stmt.where(CountEntry.deleted_at.is_(None))
    return db.scalars(stmt.order_by(CountEntry.created_at, CountEntry.id)).all()


def tally(db, session_id, ndc):
    """(total, [(name, qty), ...]) for one drug. Names are matched ignoring upper/lower case."""
    people = OrderedDict()
    for e in entries_for(db, session_id, ndc):
        key = e.counted_by.lower()
        name, qty = people.get(key, (e.counted_by, 0))
        people[key] = (name, qty + e.qty)
    return sum(q for _, q in people.values()), list(people.values())


def undo_last(db, session_id, counted_by):
    """Remove the most recent entry by this person (and nobody else's)."""
    if not counted_by:
        raise CountError("Enter your name to undo your last count.")
    _locked_open(db, session_id)
    entry = db.scalars(select(CountEntry).where(
        CountEntry.session_id == session_id, CountEntry.deleted_at.is_(None),
        func.lower(CountEntry.counted_by) == counted_by.lower())
        .order_by(CountEntry.created_at.desc(), CountEntry.id.desc())).first()
    if entry is None:
        db.rollback()
        return None
    entry.deleted_at, entry.deleted_by = utcnow(), counted_by
    entry.edit_note = _append(entry.edit_note, f"Undone by {counted_by}")
    db.commit()
    return entry


def _append(old, text):
    return ((old + "; " if old else "") + text)[-255:]


def _live(db, entry):
    _locked_open(db, entry.session_id)
    db.refresh(entry)
    if entry.deleted_at is not None:
        db.rollback()
        raise CountError("That entry was already removed.", 409)


def edit_entry(db, entry, qty, location, edited_by):
    _live(db, entry)
    if qty < 0:
        raise CountError("A count can't be negative.")
    changes = []
    if qty != entry.qty:
        changes.append(f"qty {entry.qty} to {qty}")
        entry.qty = qty
    location = (location or "").strip()[:100] or None
    if location != entry.location:
        changes.append(f"location {entry.location or 'none'} to {location or 'none'}")
        entry.location = location
    if changes:
        entry.edit_note = _append(entry.edit_note, f"Edited by {edited_by}: " + ", ".join(changes))
    db.commit()


def delete_entry(db, entry, deleted_by):
    _live(db, entry)
    entry.deleted_at, entry.deleted_by = utcnow(), deleted_by
    entry.edit_note = _append(entry.edit_note, f"Deleted by {deleted_by}")
    db.commit()


def cancel_count(db, session_id, cancelled_by):
    """Mark cancelled. Stock is not touched and the entries are kept."""
    res = db.execute(update(CountSession).where(
        CountSession.id == session_id, CountSession.status == "open")
        .values(status="cancelled", finalized_at=utcnow(), finalized_by=cancelled_by))
    db.commit()
    return res.rowcount == 1


def store_value(db, store_id):
    v = db.scalar(select(func.coalesce(func.sum(Inventory.quantity_on_hand * Product.last_unit_price), 0))
                  .join(Product, Product.ndc == Inventory.ndc).where(Inventory.store_id == store_id))
    return round(float(v or 0), 2)


def totals_by_drug(db, session_id):
    """OrderedDict ndc -> {total, names (in order, no repeats)} using entries that aren't removed."""
    out = OrderedDict()
    for e in entries_for(db, session_id):
        d = out.setdefault(e.ndc, {"total": 0, "names": OrderedDict()})
        d["total"] += e.qty
        d["names"].setdefault(e.counted_by.lower(), e.counted_by)
    return out


def review(db, sess, q=""):
    """Everything the review page needs."""
    inv = {r.ndc: r.quantity_on_hand for r in db.scalars(
        select(Inventory).where(Inventory.store_id == sess.store_id))}
    entries = db.execute(select(CountEntry, Product).join(Product, Product.ndc == CountEntry.ndc)
                         .where(CountEntry.session_id == sess.id, CountEntry.deleted_at.is_(None))
                         .order_by(CountEntry.created_at, CountEntry.id)).all()
    drugs = OrderedDict()
    for e, p in entries:
        d = drugs.setdefault(p.ndc, {"product": p, "total": 0, "names": OrderedDict(),
                                     "locations": OrderedDict(), "entries": []})
        d["total"] += e.qty
        d["names"].setdefault(e.counted_by.lower(), e.counted_by)
        if e.location:
            d["locations"].setdefault(e.location.lower(), e.location)
        d["entries"].append(e)
    for ndc, d in drugs.items():
        d["on_hand"] = inv.get(ndc, 0)
        d["diff"] = d["total"] - d["on_hand"]
        d["names"], d["locations"] = list(d["names"].values()), list(d["locations"].values())
    value = sum(d["total"] * float(d["product"].last_unit_price or 0) for d in drugs.values())
    will_change = sum(1 for d in drugs.values() if d["diff"] != 0)

    uncounted = []
    for inv_row, p in db.execute(select(Inventory, Product).join(Product, Product.ndc == Inventory.ndc)
                                 .where(Inventory.store_id == sess.store_id, Inventory.quantity_on_hand > 0)
                                 .order_by(Product.description)).all():
        if p.ndc not in drugs:
            uncounted.append((p, inv_row.quantity_on_hand))

    uncounted_total = len(uncounted)
    counted_rows = sorted(drugs.values(), key=lambda d: d["product"].description)
    if q:
        up, digits = q.upper(), "".join(c for c in q if c.isdigit())

        def hit(p):
            return (up in (p.description or "").upper() or up in (p.generic_name or "").upper()
                    or (digits and digits in p.ndc))
        counted_rows = [d for d in counted_rows if hit(d["product"])]
        uncounted = [(p, n) for p, n in uncounted if hit(p)]
    return {"rows": counted_rows, "uncounted": uncounted, "drugs_counted": len(drugs),
            "value": round(value, 2), "will_change": will_change,
            "uncounted_total": uncounted_total}


def finalize_count(db, session_id, finalized_by, set_uncounted_to_zero, user_id, date_label):
    """Apply the count in ONE transaction (all or nothing). Returns a summary dict, or None if the
    count was not open (so finalizing twice does nothing the second time)."""
    claimed = db.execute(update(CountSession).where(
        CountSession.id == session_id, CountSession.status == "open")
        .values(status="finalized", finalized_at=utcnow(), finalized_by=finalized_by,
                set_uncounted_to_zero=bool(set_uncounted_to_zero)))
    if claimed.rowcount != 1:
        db.rollback()
        return None
    try:
        sess = db.get(CountSession, session_id)
        db.refresh(sess)
        sid = sess.store_id
        before = store_value(db, sid)
        inv = {r.ndc: r.quantity_on_hand for r in db.scalars(
            select(Inventory).where(Inventory.store_id == sid))}
        counted = totals_by_drug(db, session_id)
        changed = 0
        for ndc, d in counted.items():
            was = inv.get(ndc, 0)
            if d["total"] == was:
                continue
            names = ", ".join(d["names"].values())
            prefix = f"Count day {date_label}: counted {d['total']} (was {was}) | By "
            note = prefix + names[:max(0, 255 - len(prefix))]
            adjust_stock(db, sid, ndc, d["total"] - was, "adjusted", user_id, note, commit=False)
            changed += 1
        if set_uncounted_to_zero:
            for ndc, was in inv.items():
                if was > 0 and ndc not in counted:
                    adjust_stock(db, sid, ndc, -was, "adjusted", user_id,
                                 f"Count day {date_label}: not counted (was {was})", commit=False)
                    changed += 1
        sess.changed_count = changed
        sess.value_before = before
        sess.value_after = store_value(db, sid)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"changed": changed, "before": before, "after": sess.value_after, "session_id": session_id}


def last_finalized(db, store_id, start=None, end=None):
    stmt = select(CountSession).where(CountSession.store_id == store_id, CountSession.status == "finalized")
    if start is not None:
        stmt = stmt.where(CountSession.finalized_at >= start)
    if end is not None:
        stmt = stmt.where(CountSession.finalized_at < end)
    return db.scalars(stmt.order_by(CountSession.finalized_at.desc(), CountSession.id.desc())).first()


def status_by_store(db):
    """store_id -> ('open', session) / ('finalized', session) / (None, None) for the owner's overview."""
    out = {}
    for sess in db.scalars(select(CountSession).where(CountSession.status == "open")):
        out[sess.store_id] = ("open", sess)
    for sess in db.scalars(select(CountSession).where(CountSession.status == "finalized")
                           .order_by(CountSession.finalized_at, CountSession.id)):
        if sess.store_id not in out or out[sess.store_id][0] != "open":
            out[sess.store_id] = ("finalized", sess)
    return out
