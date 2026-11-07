"""
Tests for the Download page and the styled inventory report.
Uses a throwaway local SQLite file; never touches Neon.   Run:  python test_export.py
Also writes sample_inventory_report.xlsx next to this file so you can open it.
"""
import io
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_tmp, "test.db").replace("\\", "/")
os.environ["SIGNUP_CODE"] = "letmein"
os.environ["APP_TIMEZONE"] = "America/New_York"
os.environ.pop("RENDER", None)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from openpyxl import load_workbook                     # noqa: E402
from sqlalchemy import text                            # noqa: E402

import db                                              # noqa: E402
assert db.DATABASE_URL.startswith("sqlite")
import app as appmod                                   # noqa: E402

app = appmod.app
passed = failed = 0
_n = [0]


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
        _n[0] += 1
        self.ip = f"10.9.0.{_n[0]}"
        self.c = app.test_client()

    def get(self, url):
        return self.c.get(url, environ_overrides={"REMOTE_ADDR": self.ip})

    def post(self, url, data):
        with self.c.session_transaction() as s:
            s["csrf_token"] = "tok"
        return self.c.post(url, data={**data, "csrf_token": "tok"}, environ_overrides={"REMOTE_ADDR": self.ip})

    def api(self, url, payload):
        with self.c.session_transaction() as s:
            s["csrf_token"] = "tok"
        return self.c.post(url, json=payload, headers={"X-CSRF-Token": "tok"}, environ_overrides={"REMOTE_ADDR": self.ip})


def signup(name, user):
    c = Client()
    r = c.post("/signup", {"store_name": name, "login": user, "password": "password123",
                           "confirm": "password123", "code": "letmein"})
    assert r.status_code == 302
    with db.SessionLocal() as s:
        return c, s.query(db.Store).filter_by(name=name).one().id


def book(resp):
    assert resp.status_code == 200, resp.status_code
    return load_workbook(io.BytesIO(resp.data))


now = datetime.now(timezone.utc)
local_day = lambda dt: appmod.to_local(dt).date()          # noqa: E731

# ---- drugs (11-digit NDCs, several with leading zeros)
DRUGS = [
    ("00074055402", "ZYRTEC 10MG TB 30", "CETIRIZINE", "30 EA", 12.50, "2026-01-05"),
    ("00093714201", "ATORVASTATIN 20MG TB 90", "ATORVASTATIN", "90 EA", 8.25, "2025-11-20"),
    ("00378018005", "BIKTARVY 50-200-25MG TB 30", "BICTEGRAVIR", "30 EA", 3500.00, "2026-02-14"),
    ("00054327099", "COUNTED TO ZERO 5MG", None, "100 EA", 4.00, None),
    ("00065401001", "SOLD OUT 1MG", None, "60 EA", 6.00, None),
    ("00002771001", "OLD STOCK DRUG", None, "10 EA", 1.00, None),
]
with db.SessionLocal() as s:
    for ndc, desc, gen, size, price, inv in DRUGS:
        s.add(db.Product(ndc=ndc, upc=ndc[:-1] + "0", description=desc, generic_name=gen, size=size,
                         last_unit_price=price,
                         last_invoice_date=datetime.strptime(inv, "%Y-%m-%d").date() if inv else None))
    s.commit()
ZY, AT, BI, ZERO, SOLD, OLDD = (d[0] for d in DRUGS)

owner = Client()
owner.post("/setup", {"name": "Owner", "login": "boss", "password": "ownerpass1", "confirm": "ownerpass1"})
a, A = signup("Main Street Pharmacy", "main")
b, B = signup("Elm Street", "elm")
c, C = signup("Long/Name: Pharmacy [West] Branch Number Forty-Two", "west")


def adj(client, ndc, qty, action, by=None, note=None):
    r = client.api("/api/adjust", {"ndc": ndc, "qty": qty, "action": action, "by": by, "note": note})
    assert r.status_code == 200, r.get_data(as_text=True)


adj(a, OLDD, 7, "received", "Zed")                 # long ago: last year
adj(a, ZY, 10, "received", "Maria", "invoice 55")
adj(a, AT, 5, "received", "Mike")
adj(a, ZY, 2, "dispensed", "maria")                 # same person, different letter case
adj(a, BI, 3, "received", "Mike")
adj(a, ZERO, 3, "received", "Mike")
adj(a, ZERO, 0, "count", "Mike")                    # counted down to zero in the period -> still listed
adj(a, SOLD, 4, "received", "Maria")
adj(a, SOLD, 4, "dispensed", "Maria")               # sold out, never counted -> not listed
adj(a, ZY, 1, "dispensed")                          # no name -> falls back to the account name
adj(b, ZY, 6, "received", "Bea")

with db.SessionLocal() as s:
    ids = [r[0] for r in s.execute(text("select id from transactions where store_id=:a order by id"), {"a": A})]
    stamps = [now - timedelta(days=400)] + [now - timedelta(days=10 - i) for i in range(len(ids) - 1)]
    # latest transaction is 3 days ago... make that explicit
    stamps[-1] = now - timedelta(days=3)
    stamps[-2] = now - timedelta(days=5)
    for i, ts in zip(ids, stamps):
        s.execute(text("update transactions set created_at=:t where id=:i"), {"t": ts.replace(tzinfo=None), "i": i})
    s.commit()
LATEST = stamps[-1]
OLD = stamps[0]

print("Download page")
page = a.get("/export")
body = page.get_data(as_text=True)
check("/export exists for a store account", page.status_code == 200 and "Download inventory report" in body)
check("'Download' is in the nav", 'href="/export"' in body)
check("period choices: This year (default), Last year, Last 30 days, Custom",
      all(t in body for t in ("This year", "Last year", "Last 30 days", "Custom")) and 'value="this_year" selected' in body)
check("store account has no store dropdown", 'name="store" type' not in body and "All stores" not in body)
ob = owner.get("/export").get_data(as_text=True)
check("owner's dropdown has All stores and every active store",
      "All stores" in ob and "Main Street Pharmacy" in ob and "Elm Street" in ob and "Branch Number" in ob)
check("Stock and History download buttons go to /export",
      'href="/export?store=' in a.get("/stock").get_data(as_text=True) and 'href="/export?store=' in a.get("/history").get_data(as_text=True))
check("All stores page download goes to /export", 'href="/export?store=all"' in owner.get("/all-stores").get_data(as_text=True))
check("old /export/stock sends you to the styled report", a.get("/export/stock").status_code == 302)

print("Access")
check("store can't download another store's report", a.get(f"/export/inventory-report?store={B}").status_code == 403)
check("store can't open another store's download page", a.get(f"/export?store={B}").status_code == 403)
check("store can't download the all-stores report", a.get("/export/inventory-report?store=all").status_code == 403)
check("store can download its own", a.get(f"/export/inventory-report?store={A}").status_code == 200)
check("login required", Client().get("/export").status_code == 302)

print("Sheet 1: Inventory count")
resp = a.get("/export/inventory-report?period=last_30")
wb = book(resp)
check("sheets are 'Inventory count' then 'Scan log'", wb.sheetnames == ["Inventory count", "Scan log"], wb.sheetnames)
ws = wb["Inventory count"]
check("A1 is the store name, 18pt bold, merged across the table",
      ws["A1"].value == "Main Street Pharmacy" and ws["A1"].font.bold and ws["A1"].font.size == 18
      and any(str(m) == "A1:H1" for m in ws.merged_cells.ranges))
check("A2 'as of' = date of the latest transaction in the period",
      ws["A2"].value == f"Inventory count as of {appmod.fmt_day(local_day(LATEST))}", ws["A2"].value)
check("A3 names are unique (case-insensitive) and sorted", ws["A3"].value == "Counted by: Maria, Mike", ws["A3"].value)
hdr = [x.value for x in ws[6]]
check("table header on row 6", hdr == ["NDC", "Drug", "Generic name", "Size", "Packages", "Unit price", "Price date", "Value"], hdr)
rows = []
r = 7
while ws.cell(r, 1).value != "TOTAL":
    rows.append([ws.cell(r, k).value for k in range(1, 9)])
    r += 1
total_row = r
drugs = [x[1] for x in rows]
check("drugs sorted by name", drugs == sorted(drugs), drugs)
check("drugs with stock are listed; counted-to-zero is listed; sold-out never-counted is not",
      "ZYRTEC 10MG TB 30" in drugs and "COUNTED TO ZERO 5MG" in drugs and "SOLD OUT 1MG" not in drugs, drugs)
check("old-stock drug with stock is still listed even though its transaction is out of the period", "OLD STOCK DRUG" in drugs)
check("NDC is text with leading zeros (12345-6789-01 style)",
      rows[0][0] == "00054-3270-99" or all(re.fullmatch(r"\d{5}-\d{4}-\d{2}", x[0]) for x in rows))
zy = next(x for x in rows if x[1].startswith("ZYRTEC"))
check("NDC cell keeps its leading zeros as text", zy[0] == "00074-0554-02" and isinstance(zy[0], str)
      and ws.cell(7, 1).number_format == "@")
check("packages, unit price, price date, value are right", zy[4] == 7 and zy[5] == 12.5 and zy[7] == 87.5
      and zy[6].strftime("%Y-%m-%d") == "2026-01-05")
check("number formats: packages 0, money, date yyyy-mm-dd",
      ws.cell(7, 5).number_format == "0" and ws.cell(7, 6).number_format == '"$"#,##0.00'
      and ws.cell(7, 7).number_format == "yyyy-mm-dd" and ws.cell(7, 8).number_format == '"$"#,##0.00')
value_sum = round(sum(x[7] for x in rows), 2)
check("A4 is bold and equals the TOTAL row's value",
      ws["A4"].value == f"Total inventory value: ${value_sum:,.2f}" and ws["A4"].font.bold, (ws["A4"].value, value_sum))
check("TOTAL row is bold and uses a SUM formula over the Value column",
      ws.cell(total_row, 8).value == f"=SUM(H7:H{total_row - 1})" and ws.cell(total_row, 1).font.bold and ws.cell(total_row, 8).font.bold)
check("TOTAL row has no package total", ws.cell(total_row, 5).value is None)
check("two blank rows, then the sign-off line",
      ws.cell(total_row + 1, 1).value is None and ws.cell(total_row + 2, 1).value is None
      and ws.cell(total_row + 3, 1).value == "Counted by: ____________   Verified by: ____________   Date: ________")

print("Styling and print setup")
h = ws["B6"]
check("header: fill #0E5B4A, white bold, wrapped", h.fill.fgColor.rgb.endswith("0E5B4A") and h.font.bold
      and h.font.color.rgb.endswith("FFFFFF") and h.alignment.wrap_text)
check("frozen below the header row", ws.freeze_panes == "A7")
check("alternate row banding #F2F6F4", ws["A8"].fill.fgColor.rgb.endswith("F2F6F4") and ws["A7"].fill.fill_type is None)
check("thin borders #D4DDD9", ws["A7"].border.left.style == "thin" and ws["A7"].border.left.color.rgb.endswith("D4DDD9"))
check("drug column ~45 wide and wrapped", 30 <= ws.column_dimensions["B"].width <= 47 and ws["B7"].alignment.wrap_text)
check("landscape, 1 page wide, header repeats",
      ws.page_setup.orientation == "landscape" and ws.page_setup.fitToWidth == 1 and ws.page_setup.fitToHeight == 0
      and ws.sheet_properties.pageSetUpPr.fitToPage and ws.print_title_rows in ("6:6", "$6:$6"))
check("footer: 'Page &P of &N' left, 'Printed <date>' right",
      ws.oddFooter.left.text == "Page &P of &N" and ws.oddFooter.right.text.startswith("Printed 20"))
check("0.5 inch margins", all(abs(getattr(ws.page_margins, k) - 0.5) < 1e-9 for k in ("left", "right", "top", "bottom")))
cd = resp.headers["Content-Disposition"]
check("file name inventory-<store>-<as-of date>.xlsx",
      f"inventory-main-street-pharmacy-{local_day(LATEST).isoformat()}.xlsx" in cd, cd)

print("Sheet 2: Scan log")
sl = wb["Scan log"]
check("heading: store name, 'Scan log, <from> to <to>', 'Counted by'",
      sl["A1"].value == "Main Street Pharmacy" and re.fullmatch(r"Scan log, [A-Z][a-z]{2} \d+, \d{4} to [A-Z][a-z]{2} \d+, \d{4}", sl["A2"].value)
      and sl["A3"].value == "Counted by: Maria, Mike", (sl["A2"].value, sl["A3"].value))
check("columns", [x.value for x in sl[5]] == ["Date and time", "NDC", "Drug", "Change", "Type", "Scanned by", "Note"])
log = [[x.value for x in row] for row in sl.iter_rows(min_row=6)]
log = [x for x in log if x[0] is not None]
stamps_col = [x[0] for x in log]
check("newest first", stamps_col == sorted(stamps_col, reverse=True))
check("period only: the 400-day-old transaction is not in last-30", all(x[2] != "OLD STOCK DRUG" for x in log))
inv55 = next(x for x in log if x[6] == "invoice 55")
check("note has the 'By <name>' part removed; 'Scanned by' is the parsed name", inv55[5] == "Maria" and "By" not in inv55[6])
check("types: Received / Dispensed / Count", {x[4] for x in log} == {"Received", "Dispensed", "Count"})
check("no-name scan falls back to the account name", any(x[5] == "Main Street Pharmacy" for x in log))
chg = next(i for i, row in enumerate(sl.iter_rows(min_row=6), 6) if row[3].value and row[3].value > 0)
neg = next(i for i, row in enumerate(sl.iter_rows(min_row=6), 6) if row[3].value and row[3].value < 0)
check("positive changes green, negative red", sl.cell(chg, 4).font.color.rgb.endswith("0E5B4A") and sl.cell(neg, 4).font.color.rgb.endswith("B42318"))
check("date/time formatted yyyy-mm-dd hh:mm", sl.cell(6, 1).number_format == "yyyy-mm-dd hh:mm")
check("scan log is landscape with repeating header", sl.page_setup.orientation == "landscape" and sl.print_title_rows in ("5:5", "$5:$5"))

print("Period filter")
wb2 = book(a.get("/export/inventory-report"))                     # default = this year
log2 = [[x.value for x in row] for row in wb2["Scan log"].iter_rows(min_row=6) if row[0].value]
check("default period is this year: the 400-day-old transaction is excluded", all(x[2] != "OLD STOCK DRUG" for x in log2))
check("this year's A3 leaves out the old name 'Zed'", "Zed" not in wb2["Inventory count"]["A3"].value)
if OLD.year == datetime.now().year - 1:
    wb3 = book(a.get("/export/inventory-report?period=last_year"))
    check("Last year includes the older transaction, and its names", wb3["Inventory count"]["A3"].value == "Counted by: Zed"
          and any(row[2].value == "OLD STOCK DRUG" for row in wb3["Scan log"].iter_rows(min_row=6)))
    check("Last year's as-of is that older transaction's date",
          wb3["Inventory count"]["A2"].value == f"Inventory count as of {appmod.fmt_day(local_day(OLD))}")
d = local_day(OLD).isoformat()
wb4 = book(a.get(f"/export/inventory-report?period=custom&from={d}&to={d}"))
check("Custom from/to", wb4["Inventory count"]["A3"].value == "Counted by: Zed")
wb5 = book(a.get("/export/inventory-report?period=custom&from=2001-01-01&to=2001-01-31"))
today_txt = appmod.fmt_day(datetime.now(appmod.LOCAL_TZ).date())
check("no transactions: 'as of <today>, no scans in this period'",
      wb5["Inventory count"]["A2"].value == f"Inventory count as of {today_txt}, no scans in this period", wb5["Inventory count"]["A2"].value)
check("no names: 'Counted by: not recorded'", wb5["Inventory count"]["A3"].value == "Counted by: not recorded")

print("All stores (owner)")
resp = owner.get("/export/inventory-report?store=all&period=last_30")
wb = book(resp)
names = wb.sheetnames
check("Summary is the first sheet", names[0] == "Summary", names)
check("each store has '<Store> count' and '<Store> log'",
      {"Main Street Pharmacy count", "Main Street Pharmacy log", "Elm Street count", "Elm Street log"} <= set(names), names)
check("sheet names are max 31 characters with no invalid characters",
      all(len(n) <= 31 and not re.search(r"[\\/?*\[\]:]", n) for n in names), names)
check("long store name still gets a count and a log sheet", sum(1 for n in names if n.startswith("Long Name")) == 2, names)
sm = wb["Summary"]
check("Summary columns", [x.value for x in sm[3]] == ["Store", "Inventory count as of", "Counted by", "Total inventory value"])
store_rows = {sm.cell(r, 1).value: [sm.cell(r, k).value for k in range(1, 5)] for r in range(4, sm.max_row)}
main = store_rows["Main Street Pharmacy"]
check("Summary row for a store: as-of, names, value",
      main[1] == appmod.fmt_day(local_day(LATEST)) and main[2] == "Maria, Mike" and main[3] > 0, main)
check("Summary grand total is a value-only SUM",
      sm.cell(sm.max_row, 1).value == "TOTAL" and sm.cell(sm.max_row, 4).value == f"=SUM(D4:D{sm.max_row - 1})"
      and sm.cell(sm.max_row, 2).value is None and sm.cell(sm.max_row, 3).value is None)
check("a store with no scans says so on the Summary",
      "no scans in this period" in store_rows["Long/Name: Pharmacy [West] Branch Number Forty-Two"][1])
check("Summary header styled", sm["A3"].fill.fgColor.rgb.endswith("0E5B4A"))
check("all-stores file name", f"inventory-all-stores-{datetime.now(appmod.LOCAL_TZ).date().isoformat()}.xlsx" in resp.headers["Content-Disposition"])
elm = wb["Elm Street count"]
check("another store's count sheet has only its own stock", elm["A1"].value == "Elm Street" and elm["B7"].value == "ZYRTEC 10MG TB 30" and elm["E7"].value == 6)
with db.SessionLocal() as s:
    uid = s.query(db.User).filter_by(login="west").one().id
owner.post("/accounts", {"action": "toggle", "id": uid})
check("deactivated store is gone from the all-stores file",
      not any(n.startswith("Long Name") for n in book(owner.get("/export/inventory-report?store=all")).sheetnames))

# ---- sample for you to open
with open(os.path.join(HERE, "sample_inventory_report.xlsx"), "wb") as f:
    f.write(a.get("/export/inventory-report?period=last_30").data)
print("  saved sample_inventory_report.xlsx")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
