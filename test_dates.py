"""
Tests for the dates on the Scan page card, the Stock page and the inventory report.
Uses a throwaway local SQLite file; never touches Neon.   Run:  python test_dates.py
"""
import io
import os
import re
import sys
import tempfile
from datetime import date, datetime

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_tmp, "test.db").replace("\\", "/")
os.environ["SIGNUP_CODE"] = "letmein"
os.environ["APP_TIMEZONE"] = "America/New_York"
os.environ.pop("RENDER", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openpyxl import load_workbook                     # noqa: E402
from sqlalchemy import text                            # noqa: E402

import db                                              # noqa: E402
assert db.DATABASE_URL.startswith("sqlite")
import app as appmod                                   # noqa: E402

app = appmod.app
passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok    {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} {extra}")


class Client:
    def __init__(self, ip):
        self.c, self.ip = app.test_client(), ip

    def get(self, url):
        return self.c.get(url, environ_overrides={"REMOTE_ADDR": self.ip})

    def post(self, url, data):
        with self.c.session_transaction() as s:
            s.setdefault("csrf_token", "tok")
        return self.c.post(url, data={**data, "csrf_token": "tok"}, environ_overrides={"REMOTE_ADDR": self.ip})

    def api(self, url, payload):
        with self.c.session_transaction() as s:
            s.setdefault("csrf_token", "tok")
        return self.c.post(url, json=payload, headers={"X-CSRF-Token": "tok"},
                           environ_overrides={"REMOTE_ADDR": self.ip})


today = datetime.now(appmod.LOCAL_TZ).date()


def months_back(n, days=0):
    return appmod.add_months(today, -n).fromordinal(appmod.add_months(today, -n).toordinal() + days)


FRESH, OLD, EDGE, EDGE_OLD, NODATE, NEVER = ("00000000001", "00000000002", "00000000003",
                                            "00000000004", "00000000005", "00000000006")
with db.SessionLocal() as s:
    s.add_all([
        db.Product(ndc=FRESH, upc="111111111111", description="FRESH DRUG", last_unit_price=10, last_invoice_date=months_back(1)),
        db.Product(ndc=OLD, upc="222222222222", description="OLD PRICE DRUG", last_unit_price=4, last_invoice_date=date(2025, 12, 27) if today > date(2026, 6, 27) else months_back(8)),
        db.Product(ndc=EDGE, upc="333333333333", description="EXACTLY SIX MONTHS", last_unit_price=1, last_invoice_date=months_back(6)),
        db.Product(ndc=EDGE_OLD, upc="444444444444", description="SIX MONTHS AND A DAY", last_unit_price=1, last_invoice_date=months_back(6, -1)),
        db.Product(ndc=NODATE, upc="555555555555", description="NO DATE DRUG", last_unit_price=2),
        db.Product(ndc=NEVER, upc="666666666666", description="NEVER STOCKED", last_unit_price=3, last_invoice_date=months_back(2)),
    ])
    s.commit()

o = Client("1.0.0.1")
o.post("/setup", {"name": "Owner", "login": "boss", "password": "ownerpass1", "confirm": "ownerpass1"})
a = Client("1.0.0.2")
a.post("/signup", {"store_name": "Main Street Pharmacy", "login": "main", "password": "password123",
                   "confirm": "password123", "code": "letmein"})


def lookup(c, ndc):
    return c.get(f"/api/lookup?code={ndc}").get_json()["product"]


print("Invoice date on the card (lookup API)")
p = lookup(a, FRESH)
check("recent price: date, display and not stale", p["price_date"] == months_back(1).isoformat()
      and p["price_date_display"] == f"{months_back(1):%b} {months_back(1).day}, {months_back(1).year}" and p["price_stale"] is False, p)
p = lookup(a, OLD)
check("price older than 6 months is flagged stale", p["price_stale"] is True and re.fullmatch(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", p["price_date_display"]), p)
check("exactly 6 months old is not stale", lookup(a, EDGE)["price_stale"] is False)
check("6 months and a day old is stale", lookup(a, EDGE_OLD)["price_stale"] is True)
p = lookup(a, NODATE)
check("no invoice date -> null date, not stale", p["price_date"] is None and p["price_date_display"] is None and p["price_stale"] is False)
check("specific example: Dec 27, 2025 displays as 'Dec 27, 2025'",
      appmod.price_info(db.Product(last_invoice_date=date(2025, 12, 27)))["price_date_display"] == "Dec 27, 2025")

print("Stock last updated (lookup API)")
p = lookup(a, NEVER)
check("drug never stocked at this store -> nothing to show ('Never')",
      p["stock_updated_at"] is None and p["stock_updated_display"] is None and p["stock_updated_by"] is None, p)
r = a.api("/api/adjust", {"ndc": FRESH, "qty": 5, "action": "received", "by": "Maria"})
j = r.get_json()
check("adjust returns the updated time and who", j["stock_updated_by"] == "Maria" and j["stock_updated_at"], j)
check("display looks like 'Nov 7, 12:56 PM'", re.fullmatch(r"[A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} (AM|PM)", j["stock_updated_display"]), j["stock_updated_display"])
check("ISO time carries a time zone offset", re.search(r"[+-]\d\d:\d\d$", j["stock_updated_at"]), j["stock_updated_at"])
p = lookup(a, FRESH)
check("lookup returns the same values", p["stock_updated_by"] == "Maria" and p["stock_updated_at"] == j["stock_updated_at"])
check("another drug is still 'never'", lookup(a, NEVER)["stock_updated_at"] is None)

r = a.api("/api/adjust", {"ndc": FRESH, "qty": 1, "action": "dispensed"})
check("no 'By' in the latest note -> the account name", r.get_json()["stock_updated_by"] == "Main Street Pharmacy", r.get_json())
a.api("/api/adjust", {"ndc": FRESH, "qty": 2, "action": "received", "note": "invoice 9", "by": "Mike"})
check("note with text and By -> just the name", lookup(a, FRESH)["stock_updated_by"] == "Mike")
r = a.api("/api/adjust", {"ndc": FRESH, "qty": 6, "action": "count"})
check("a count that matches still returns the fields", r.get_json().get("stock_updated_display") and r.get_json()["change"] == 0)

print("Time zone")
with db.SessionLocal() as s:
    s.execute(text("UPDATE inventory SET last_updated = '2026-11-07 17:56:00' WHERE ndc = :n"), {"n": FRESH})
    s.commit()
p = lookup(a, FRESH)
check("17:56 UTC is shown as 'Nov 7, 12:56 PM' in America/New_York", p["stock_updated_display"] == "Nov 7, 12:56 PM", p["stock_updated_display"])
check("ISO time is in APP_TIMEZONE (-05:00 in November)", p["stock_updated_at"].startswith("2026-11-07T12:56:00-05:00"), p["stock_updated_at"])
with db.SessionLocal() as s:
    s.execute(text("UPDATE inventory SET last_updated = '2026-11-07 17:05:00' WHERE ndc = :n"), {"n": FRESH})
    s.execute(text("UPDATE inventory SET last_updated = '2026-11-07 05:30:00' WHERE ndc = :n"), {"n": FRESH})
    s.commit()
check("12-hour clock: 05:30 UTC -> '12:30 AM'", lookup(a, FRESH)["stock_updated_display"] == "Nov 7, 12:30 AM")

print("Another store is separate")
b = Client("1.0.0.3")
b.post("/signup", {"store_name": "Elm Street", "login": "elm", "password": "password123",
                   "confirm": "password123", "code": "letmein"})
check("store B has never had the drug store A updated", lookup(b, FRESH)["stock_updated_at"] is None)

print("Page and Excel")
page = a.get("/scan").get_data(as_text=True)
check("scan card has the price note and Stock last updated fact",
      'id="p-price-note"' in page and "Stock last updated" in page and 'id="p-updated-by"' in page)
js = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "scan.js"), encoding="utf-8").read()
check("scan.js has the three invoice texts and 'Never'",
      '"No invoice date"' in js and "may be out of date" in js and '"Invoice "' in js and '"Never"' in js and "showStockInfo(data)" in js)
css = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "style.css"), encoding="utf-8").read()
check("facts grid wraps on phones and the stale line uses --amber",
      "auto-fit" in css and ".facts .note.stale { color: var(--amber)" in css and "overflow-wrap: anywhere" in css)
stock = a.get("/stock").get_data(as_text=True)
heads = re.findall(r"<th[^>]*>([^<]*)</th>", stock)
check("Stock page has 'Price date' right after 'Unit price'", heads.index("Price date") == heads.index("Unit price") + 1, heads)
check("Stock page shows the date as yyyy-mm-dd", months_back(1).isoformat() in stock)

r = a.get("/export/inventory-report")
ws = load_workbook(io.BytesIO(r.data))["Inventory count"]
hdr = [c.value for c in ws[6]]
check("report has 'Price date' after 'Unit price'", hdr[5:7] == ["Unit price", "Price date"], hdr)
cell = ws.cell(7, 7)
check("price date is a real date formatted yyyy-mm-dd", cell.number_format == "yyyy-mm-dd" and cell.value is not None, (cell.value, cell.number_format))
last = [r_ for r_ in range(7, ws.max_row + 1) if ws.cell(r_, 1).value == "TOTAL"][0]
check("TOTAL still sums the Value column only", str(ws.cell(last, 8).value).startswith("=SUM(H7:H") and ws.cell(last, 5).value is None)
check("old /export/stock redirects to the styled report", a.get("/export/stock").status_code == 302)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
