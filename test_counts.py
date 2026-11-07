"""
Tests for count day. Uses a throwaway local SQLite file; never touches Neon.
Run:  python test_counts.py
"""
import io
import os
import random
import re
import sys
import tempfile
import threading
from datetime import datetime, timezone

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_tmp, "test.db").replace("\\", "/")
os.environ["SIGNUP_CODE"] = "letmein"
os.environ.pop("RENDER", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openpyxl import load_workbook                     # noqa: E402
from sqlalchemy import func, select, text              # noqa: E402
from sqlalchemy.exc import IntegrityError              # noqa: E402

import counts                                          # noqa: E402
import db                                              # noqa: E402
assert db.DATABASE_URL.startswith("sqlite"), "tests must use SQLite"
import app as appmod                                   # noqa: E402

app = appmod.app
passed = failed = 0
_ip = [0]


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok    {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} {extra}")


class Client:
    def __init__(self):
        _ip[0] += 1
        self.ip = f"10.1.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
        self.c = app.test_client()

    def _env(self):
        return {"REMOTE_ADDR": self.ip}

    def get(self, url):
        return self.c.get(url, environ_overrides=self._env())

    def token(self):
        with self.c.session_transaction() as s:
            s.setdefault("csrf_token", "tok")
            return s["csrf_token"]

    def post(self, url, data=None):
        data = dict(data or {})
        data["csrf_token"] = self.token()
        return self.c.post(url, data=data, environ_overrides=self._env())

    def api(self, url, payload):
        return self.c.post(url, json=payload, headers={"X-CSRF-Token": self.token()},
                           environ_overrides=self._env())

    def login(self, user, pw="password123"):
        return self.post("/login", {"login": user, "password": pw})


def body(r):
    return r.get_data(as_text=True)


def make_store(name, user):
    c = Client()
    r = c.post("/signup", {"store_name": name, "login": user, "password": "password123",
                           "confirm": "password123", "code": "letmein"})
    assert r.status_code == 302, body(r)
    with db.SessionLocal() as s:
        return c, s.query(db.Store).filter_by(name=name).one().id


def stock_of(store_id, ndc):
    with db.SessionLocal() as s:
        row = s.query(db.Inventory).filter_by(store_id=store_id, ndc=ndc).first()
        return row.quantity_on_hand if row else 0


def entry(c, ndc, qty, by, loc=""):
    return c.api("/api/count/entry", {"ndc": ndc, "qty": qty, "by": by, "location": loc})


def count_start(c, store_id, name):
    return c.post("/count/start", {"store": store_id, "name": name})


def open_id(store_id):
    with db.SessionLocal() as s:
        sess = counts.open_session(s, store_id)
        return sess.id if sess else None


def receive(c, ndc, qty):
    return c.api("/api/adjust", {"ndc": ndc, "qty": qty, "action": "received"})


D1, D2, D3 = "00000000001", "00000000002", "00000000003"
with db.SessionLocal() as s:
    s.add_all([db.Product(ndc=D1, description="DRUG ONE 10MG", upc="111111111111", last_unit_price=10),
               db.Product(ndc=D2, description="DRUG TWO 20MG", upc="222222222222", last_unit_price=2.5),
               db.Product(ndc=D3, description="DRUG THREE 30MG", upc="333333333333", last_unit_price=1)])
    s.commit()

owner = Client()
owner.post("/setup", {"name": "Owner", "login": "boss", "password": "ownerpass1", "confirm": "ownerpass1"})

print("Tables and the one-open-count rule")
with db.SessionLocal() as s:
    names = {r[0] for r in s.execute(text("select name from sqlite_master where type='table'"))}
check("count_sessions and count_entries tables exist alongside the old five",
      {"count_sessions", "count_entries", "stores", "users", "products", "inventory", "transactions"} <= names)

a1, A = make_store("Store A", "storea")
a2 = Client()
a2.login("storea")
b1, B = make_store("Store B", "storeb")

print("Start a count; two people tally the same drug")
receive(a1, D1, 20)
receive(a1, D2, 4)
check("count page exists for the store", a1.get("/count").status_code == 200 and "Start count" in body(a1.get("/count")))
check("Count day is in the nav", 'href="/count"' in body(a1.get("/count")))
r = count_start(a1, A, "")
check("starting needs a name", open_id(A) is None)
r = count_start(a1, A, "Priyanka")
check("count starts", open_id(A) is not None)
page = body(a1.get("/scan"))
check("scan page shows the amber banner", "Count in progress, started" in page and "by Priyanka" in page and "Scans add to the count." in page)
check("scan page shows only the Count mode button", 'data-mode="counting"' in page and 'data-mode="received"' not in page)
check("scan page has Location next to Your name and an Undo button", 'id="loc"' in page and "Undo my last count" in page)
r = entry(a1, D1, 5, "Priyanka", "Shelf 3")
check("first entry: counted 5, on hand shown", r.get_json()["counted"] == 5 and r.get_json()["on_hand"] == 20)
r = entry(a2, D1, 3, "Mike", "Back room")
j = r.get_json()
check("second person's entry ADDS: tally 8", j["counted"] == 8, j)
check("tally lists both people", {p["name"]: p["qty"] for p in j["people"]} == {"Priyanka": 5, "Mike": 3})
check("stock unchanged while counting", stock_of(A, D1) == 20)
r = a1.get(f"/api/lookup?code={D1}")
check("lookup shows the running tally", r.get_json()["count"]["counted"] == 8)
check("entry needs a name", entry(a1, D1, 1, "").status_code == 400)
check("entry rejects negative qty", entry(a1, D1, -1, "Priyanka").status_code == 400)
check("entry for unknown drug refused", entry(a1, "99999999999", 1, "Priyanka").status_code == 404)

print("Receive / dispense blocked during a count")
for action in ("received", "dispensed", "count"):
    r = a1.api("/api/adjust", {"ndc": D1, "qty": 1, "action": action})
    check(f"API blocks '{action}'", r.status_code == 409 and r.get_json()["error"] == "Finish or cancel the count before receiving or dispensing.")
check("stock still 20", stock_of(A, D1) == 20)
check("another store is not blocked", receive(b1, D1, 2).status_code == 200)

print("Undo only removes your own last entry")
entry(a1, D1, 2, "Priyanka", "Shelf 4")                # Priyanka 5+2, Mike 3 -> 10
r = a1.api("/api/count/undo", {"by": "Priyanka"})
j = r.get_json()
check("Priyanka's undo removes her 2 (not Mike's 3)", j["counted"] == 8 and {p["name"]: p["qty"] for p in j["people"]} == {"Priyanka": 5, "Mike": 3}, j)
r = a2.api("/api/count/undo", {"by": "mike"})
check("Mike's undo (any letter case) removes his 3", r.get_json()["counted"] == 5)
r = a2.api("/api/count/undo", {"by": "Mike"})
check("Mike has nothing left to undo", r.status_code == 404)
check("Priyanka's 5 is still there", counts.tally(db.SessionLocal(), open_id(A), D1)[0] == 5)
check("undo needs a name", a1.api("/api/count/undo", {"by": ""}).status_code == 400)
entry(a2, D1, 3, "Mike", "Back room")                   # back to 8

print("Only one open count per store, and stores are separate")
r = count_start(a2, A, "Mike")
check("second start for the same store refused", "already in progress" in body(a2.get("/count")))
with db.SessionLocal() as s:
    check("still exactly one open count for A", s.query(db.CountSession).filter_by(store_id=A, status="open").count() == 1)
    try:
        s.add(db.CountSession(store_id=A, status="open"))
        s.commit()
        dup = False
    except IntegrityError:
        s.rollback()
        dup = True
check("database itself refuses a second open count (partial unique index)", dup)
with db.SessionLocal() as s:
    try:
        counts.start_count(s, A, "X")
        raised = False
    except counts.CountError:
        raised = True
check("start_count() raises when one is open", raised)
count_start(b1, B, "Bea")
check("store B has its own separate count", open_id(B) is not None and open_id(B) != open_id(A))
entry(b1, D1, 7, "Bea")
sess_b = open_id(B)
with db.SessionLocal() as s:
    ent_b = s.query(db.CountEntry).filter_by(session_id=sess_b).first().id
check("A's tally unaffected by B's entry", counts.tally(db.SessionLocal(), open_id(A), D1)[0] == 8)
check("A can't open B's count page", a1.get(f"/count?store={B}").status_code == 403)
check("A can't finalize B's count", a1.post(f"/count/{sess_b}/finalize", {"store": B, "name": "Priyanka"}).status_code == 403)
check("A can't finalize B's count even naming its own store", a1.post(f"/count/{sess_b}/finalize", {"store": A, "name": "Priyanka"}).status_code == 403)
check("A can't cancel B's count", a1.post(f"/count/{sess_b}/cancel", {"store": A, "name": "P"}).status_code == 403)
check("A can't edit B's entry", a1.post(f"/count/entry/{ent_b}/edit", {"store": A, "name": "P", "qty": 99}).status_code == 403)
check("A can't delete B's entry", a1.post(f"/count/entry/{ent_b}/delete", {"store": A, "name": "P"}).status_code == 403)
check("B's count is still open and untouched", open_id(B) == sess_b and counts.tally(db.SessionLocal(), sess_b, D1)[0] == 7)
check("A's page never mentions B's count", "Store B" not in body(a1.get("/count")))

print("Review page")
entry(a1, D3, 4, "Priyanka", "Shelf 1")
page = body(a1.get("/count"))
check("review shows counted drug with 8 vs on hand 20", "DRUG ONE" in page and "<strong>8</strong>" in page)
check("difference -12 shown as red tag", 'tag out">-12<' in page)
check("people and locations listed", "Priyanka, Mike" in page and "Shelf 3, Back room" in page)
check("each entry is listed with time, person, location, qty", "Shelf 3" in page and "Back room" in page)
check("'In stock but not counted' shows DRUG TWO (4 on hand)", "In stock but not counted" in page and "DRUG TWO" in page)
check("totals: drugs counted and value at counted quantities", "<strong>2</strong>drugs counted" in page and "$84.00" in page)
check("checkbox 'set uncounted to 0' is ticked by default", 'id="set-zero" checked' in page)
check("search filters the table", "DRUG THREE" not in body(a1.get("/count?q=one")) and "DRUG ONE" in body(a1.get("/count?q=one")))
with db.SessionLocal() as s:
    mike_entry = s.query(db.CountEntry).filter_by(session_id=open_id(A), counted_by="Mike", deleted_at=None).first().id
r = a2.post(f"/count/entry/{mike_entry}/edit", {"store": A, "name": "Mike", "qty": 4, "location": "Back wall"})
check("entry can be edited (needs a name)", counts.tally(db.SessionLocal(), open_id(A), D1)[0] == 9)
page = body(a1.get("/count"))
check("edit is logged on the entry", "Edited by Mike: qty 3 to 4" in page)
a2.post(f"/count/entry/{mike_entry}/edit", {"store": A, "name": "", "qty": 50})
check("edit without a name is refused", counts.tally(db.SessionLocal(), open_id(A), D1)[0] == 9)
a2.post(f"/count/entry/{mike_entry}/delete", {"store": A, "name": "Mike"})
check("entry can be deleted; total drops", counts.tally(db.SessionLocal(), open_id(A), D1)[0] == 5)
with db.SessionLocal() as s:
    e = s.get(db.CountEntry, mike_entry)
check("delete is logged, not erased", e is not None and e.deleted_by == "Mike" and "Deleted by Mike" in e.edit_note)
check("deleting an already-deleted entry is refused", "already removed" in (a2.post(f"/count/entry/{mike_entry}/delete", {"store": A, "name": "Mike"}) and body(a2.get("/count"))))
entry(a2, D1, 3, "Mike", "Back room")                   # 8 again

print("Owner")
check("owner sees the count page for any store", owner.get(f"/count?store={A}").status_code == 200 and "Count in progress" in body(owner.get(f"/count?store={A}")))
page = body(owner.get("/all-stores"))
check("All stores shows A in progress, B in progress", page.count("In progress") == 2)

print("Cancel leaves stock unchanged")
r = b1.post(f"/count/{sess_b}/cancel", {"store": B, "name": "Bea"})
with db.SessionLocal() as s:
    cs = s.get(db.CountSession, sess_b)
    check("B's count is cancelled and its entries kept",
          cs.status == "cancelled" and s.query(db.CountEntry).filter_by(session_id=sess_b).count() == 1)
check("B's stock unchanged by the cancelled count", stock_of(B, D1) == 2)
check("receive/dispense allowed again after cancel", receive(b1, D1, 1).get_json()["on_hand"] == 3)
b1.post(f"/count/{sess_b}/cancel", {"store": B, "name": "Bea"})
check("cancelling twice is harmless", stock_of(B, D1) == 3)

print("Finalize")
sid_a = open_id(A)
tx_before = None
with db.SessionLocal() as s:
    tx_before = s.query(db.Transaction).filter_by(store_id=A).count()


# all-or-nothing: make the 2nd stock change fail and check nothing is half-applied
real = counts.adjust_stock
calls = {"n": 0}


def flaky(*a, **kw):
    calls["n"] += 1
    if calls["n"] == 2:
        raise RuntimeError("boom")
    return real(*a, **kw)


counts.adjust_stock = flaky
try:
    try:
        a1.post(f"/count/{sid_a}/finalize", {"store": A, "name": "Priyanka", "set_zero": "1"})
    except Exception:
        pass
finally:
    counts.adjust_stock = real
with db.SessionLocal() as s:
    check("a failure part-way leaves everything unchanged and the count open",
          s.get(db.CountSession, sid_a).status == "open" and s.query(db.Transaction).filter_by(store_id=A).count() == tx_before
          and stock_of(A, D1) == 20 and stock_of(A, D2) == 4 and stock_of(A, D3) == 0)

r = a1.post(f"/count/{sid_a}/finalize", {"store": A, "name": "Priyanka", "set_zero": "1"})
check("finalize redirects to the summary", r.status_code == 302 and "done=" in r.location)
check("counted drug set to the tally: 8", stock_of(A, D1) == 8)
check("new counted drug (not in stock before) set to 4", stock_of(A, D3) == 4)
check("uncounted in-stock drug set to 0 (box ticked)", stock_of(A, D2) == 0)
today = appmod.fmt_date(datetime.now(timezone.utc))
with db.SessionLocal() as s:
    notes = {t.ndc: (t.note, t.change, t.reason) for t in s.query(db.Transaction).filter_by(store_id=A).filter(
        db.Transaction.note.like("Count day%"))}
check("note format for counted drug", notes[D1][0] == f"Count day {today}: counted 8 (was 20) | By Priyanka, Mike", notes.get(D1))
check("note format for uncounted drug", notes[D2][0] == f"Count day {today}: not counted (was 4)", notes.get(D2))
check("count changes are 'adjusted' transactions", all(v[2] == "adjusted" for v in notes.values()) and notes[D1][1] == -12)
summary = body(a1.get(r.location))
check("summary page: drugs changed, value before/after, report button",
      "Count finalized" in summary and "<strong>3</strong>drugs changed" in summary
      and "$210.00" in summary and "$84.00" in summary and "Download inventory report" in summary,
      re.findall(r"\$[\d,.]+", summary))
with db.SessionLocal() as s:
    n_tx = s.query(db.Transaction).filter_by(store_id=A).count()
r = a1.post(f"/count/{sid_a}/finalize", {"store": A, "name": "Priyanka", "set_zero": "1"})
with db.SessionLocal() as s:
    check("finalizing twice changes nothing the second time",
          s.query(db.Transaction).filter_by(store_id=A).count() == n_tx and stock_of(A, D1) == 8)
check("API accepts receive again after finalize", receive(a1, D2, 6).get_json()["on_hand"] == 6)
check("entries API says no count is open after finalize", entry(a1, D1, 1, "Priyanka").status_code == 409)
check("All stores shows A last finalized", f"last finalized {today}" in body(owner.get("/all-stores")))

print("Without the checkbox, uncounted drugs are left alone")
count_start(a1, A, "Priyanka")
entry(a1, D1, 9, "Priyanka")
sid2 = open_id(A)
a1.post(f"/count/{sid2}/finalize", {"store": A, "name": "Priyanka"})       # no set_zero
check("counted drug updated to 9", stock_of(A, D1) == 9)
check("uncounted drugs unchanged (D2=6, D3=4)", stock_of(A, D2) == 6 and stock_of(A, D3) == 4)
with db.SessionLocal() as s:
    check("session remembers the checkbox", s.get(db.CountSession, sid2).set_uncounted_to_zero is False)
count_start(a1, A, "Priyanka")
entry(a1, D1, 9, "Priyanka")
sid3 = open_id(A)
a1.post(f"/count/{sid3}/finalize", {"store": A, "name": "Priyanka"})
with db.SessionLocal() as s:
    check("a count that matches stock changes nothing for that drug (no no-op transactions)",
          s.query(db.Transaction).filter(db.Transaction.store_id == A, db.Transaction.note.like("Count day%was 9%")).count() == 0
          and s.get(db.CountSession, sid3).changed_count == 0)

print("Owner can start and finalize for any store")
r = count_start(owner, B, "Boss")
check("owner starts a count for store B", open_id(B) is not None)
entry(b1, D2, 5, "Bea")
r = owner.post(f"/count/{open_id(B)}/finalize", {"store": B, "name": "Boss", "set_zero": "1"})
check("owner finalizes it", stock_of(B, D2) == 5 and stock_of(B, D1) == 0)

print("Inventory report")
count_start(a1, A, "Priyanka")
entry(a1, D1, 4, "Zoe")
entry(a1, D2, 2, "Zoe", "Shelf 9")
sid4 = open_id(A)
a1.post(f"/count/{sid4}/finalize", {"store": A, "name": "Zoe", "set_zero": "1"})


def wb_of(resp):
    assert resp.status_code == 200, resp.status_code
    return load_workbook(io.BytesIO(resp.data))


_resp = a1.get("/export/inventory-report")
wb = wb_of(_resp)
check("sheets: Inventory count, Scan log, Count entries", wb.sheetnames == ["Inventory count", "Scan log", "Count entries"], wb.sheetnames)
ws = wb["Inventory count"]
check("row 2 uses the finalized count's date", ws["A2"].value == f"Inventory count as of {today}", ws["A2"].value)
check("row 3 'Counted by' lists the unique names, sorted", ws["A3"].value == "Counted by: Mike, Priyanka, Zoe", ws["A3"].value)
vals = []
r_ = 7
while ws.cell(r_, 1).value != "TOTAL":
    vals.append(ws.cell(r_, 8).value)
    r_ += 1
check("row 4 is 'Total inventory value: $...' in bold", ws["A4"].value.startswith("Total inventory value: $") and ws["A4"].font.bold)
check("row 4 value matches the TOTAL row's value", ws["A4"].value == f"Total inventory value: ${sum(vals):,.2f}", (ws["A4"].value, vals))
check("TOTAL row is a SUM formula over the Value column", str(ws.cell(r_, 8).value).startswith("=SUM(H7:H"))
with db.SessionLocal() as s:
    _last = s.scalar(select(func.max(db.Transaction.created_at)).where(db.Transaction.store_id == A))
check("'as of' date equals the date of the store's latest transaction", ws["A2"].value == "Inventory count as of " + appmod.fmt_date(_last), (ws["A2"].value, _last))
check("TOTAL row has no package total", ws.cell(r_, 5).value is None)
check("expected value: 4x$10 + 2x$2.50 = $45.00", abs(sum(vals) - 45.0) < 0.001, vals)
ce = wb["Count entries"]
rows = [[c.value for c in row] for row in ce.iter_rows(min_row=4, values_only=False)]
check("Count entries sheet lists every entry (time, NDC, drug, qty, by, location)",
      [r[3] for r in rows[1:]] == [4, 2] and rows[0][:6] == ["Time", "NDC", "Drug", "Qty", "Counted by", "Location"]
      and rows[2][5] == "Shelf 9", rows)
check("Scan log heading keeps the period", wb["Scan log"]["A2"].value.startswith("Scan log, ") and " to " in wb["Scan log"]["A2"].value)

# a store whose latest transaction is the 'as of' date, and a period with no scans
ws2 = wb_of(b1.get("/export/inventory-report"))["Inventory count"]
check("a finalized count by the owner is used for store B", ws2["A2"].value == f"Inventory count as of {today}")
ws3 = wb_of(a1.get("/export/inventory-report?from=2001-01-01&to=2001-01-31"))["Inventory count"]
check("no scans in period -> '<today>, no scans in this period'", ws3["A2"].value == f"Inventory count as of {today}, no scans in this period", ws3["A2"].value)
check("and no 'Count entries' sheet for a period without a count",
      "Count entries" not in wb_of(a1.get("/export/inventory-report?from=2001-01-01&to=2001-01-31")).sheetnames)

# no finalized count: date = latest transaction date in the period; names from "By <name>" tags
c_, C = make_store("Store C", "storec")
c_.api("/api/adjust", {"ndc": D1, "qty": 3, "action": "received", "by": "Kim"})
with db.SessionLocal() as s:
    s.execute(text("UPDATE transactions SET created_at = '2026-03-05 15:00:00' WHERE store_id = :c"), {"c": C})
    s.commit()
ws4 = wb_of(c_.get("/export/inventory-report?from=2026-03-01&to=2026-03-31"))["Inventory count"]
check("no count: 'as of' = latest transaction date in the period", ws4["A2"].value == "Inventory count as of Mar 5, 2026", ws4["A2"].value)
check("no count: 'Counted by' from By tags", ws4["A3"].value == "Counted by: Kim", ws4["A3"].value)

wb = wb_of(owner.get("/export/inventory-report?store=all"))
check("all-stores report starts with a Summary sheet", wb.sheetnames[0] == "Summary" and "Store A count" in wb.sheetnames and "Store A log" in wb.sheetnames, wb.sheetnames)
sm = wb["Summary"]
check("Summary columns", [c.value for c in sm[3]][:4] == ["Store", "Inventory count as of", "Counted by", "Total inventory value"])
check("Summary has a grand total of value only",
      [c.value for c in sm[sm.max_row]][0] == "TOTAL" and str(sm.cell(sm.max_row, 4).value).startswith("=SUM(D4:D")
      and sm.cell(sm.max_row, 2).value is None and sm.cell(sm.max_row, 3).value is None)
check("a store account can't download the all-stores report", a1.get("/export/inventory-report?store=all").status_code == 403)
check("a store account can't download another store's report", a1.get(f"/export/inventory-report?store={B}").status_code == 403)

print("Concurrent counting: 6 stores x 3 people")
stores = []
for i in range(6):
    first, sid = make_store(f"Busy {i}", f"busy{i}")
    clients = [first] + [Client() for _ in range(2)]
    for cl in clients[1:]:
        assert cl.login(f"busy{i}").status_code == 302
    count_start(first, sid, "Lead")
    stores.append((sid, clients, [f"P{i}a", f"P{i}b", f"P{i}c"]))

expected = {}           # (store, ndc) -> total
lock = threading.Lock()
errors = []
SCANS = 22              # per person -> 6 x 3 x 22 = 396 entries
barrier = threading.Barrier(18)


def worker(sid, client, person, seed):
    rnd = random.Random(seed)
    barrier.wait()
    for _ in range(SCANS):
        ndc, qty = rnd.choice([D1, D2, D3]), rnd.randint(0, 9)
        for attempt in range(5):
            r = entry(client, ndc, qty, person, f"Shelf {rnd.randint(1, 5)}")
            if r.status_code == 200:
                break
        else:
            errors.append((person, r.status_code, r.get_data(as_text=True)[:100]))
            continue
        with lock:
            expected[(sid, ndc)] = expected.get((sid, ndc), 0) + qty


threads = [threading.Thread(target=worker, args=(sid, cl, name, hash((sid, name)) & 0xFFFF))
           for sid, clients, people in stores for cl, name in zip(clients, people)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("no scan failed", not errors, errors[:3])
with db.SessionLocal() as s:
    n = s.scalar(select(func.count(db.CountEntry.id)).join(db.CountSession, db.CountSession.id == db.CountEntry.session_id)
                 .where(db.CountSession.store_id.in_([sid for sid, _, _ in stores])))
check(f"no entries lost: {18 * SCANS} scans -> {18 * SCANS} entries", n == 18 * SCANS == len(threads) * SCANS, n)
ok = True
for sid, _, _ in stores:
    sess = open_id(sid)
    with db.SessionLocal() as s:
        for ndc in (D1, D2, D3):
            got = counts.tally(s, sess, ndc)[0]
            if got != expected.get((sid, ndc), 0):
                ok = False
                print("      mismatch", sid, ndc, got, expected.get((sid, ndc), 0))
check("every store/drug total is exact", ok)
for sid, clients, _ in stores:
    clients[0].post(f"/count/{open_id(sid)}/finalize", {"store": sid, "name": "Lead", "set_zero": "1"})
check("finalizing all six sets stock to the exact totals",
      all(stock_of(sid, ndc) == expected.get((sid, ndc), 0) for sid, _, _ in stores for ndc in (D1, D2, D3)))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
