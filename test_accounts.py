"""
End-to-end tests for store sign-up, owner/store separation and security.
Uses a throwaway local SQLite file; never touches Neon.   Run:  python test_accounts.py
"""
import io
import os
import re
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_tmp, "test.db").replace("\\", "/")
os.environ.pop("SIGNUP_CODE", None)
os.environ.pop("RENDER", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd                                    # noqa: E402
from sqlalchemy import text                            # noqa: E402

import db                                              # noqa: E402
assert db.DATABASE_URL.startswith("sqlite"), "tests must use SQLite"
import app as appmod                                   # noqa: E402

app = appmod.app
app.config["TESTING"] = False        # keep real error handling

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
    """Test client that remembers the CSRF token the way a browser form would."""
    def __init__(self, ip="1.1.1.1"):
        self.c = app.test_client()
        self.ip = ip

    def _env(self):
        return {"REMOTE_ADDR": self.ip}

    def get(self, url, **kw):
        return self.c.get(url, environ_overrides=self._env(), **kw)

    def token(self):
        with self.c.session_transaction() as s:
            if "csrf_token" not in s:
                s["csrf_token"] = "test-token"
            return s["csrf_token"]

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data["csrf_token"] = self.token()
        return self.c.post(url, data=data, environ_overrides=self._env(), **kw)

    def api(self, url, payload):
        return self.c.post(url, json=payload, headers={"X-CSRF-Token": self.token()},
                           environ_overrides=self._env())


def text_of(r):
    return r.get_data(as_text=True)


def signup(c, store, user, pw="password123", code="letmein", confirm=None):
    return c.post("/signup", {"store_name": store, "login": user, "password": pw,
                              "confirm": pw if confirm is None else confirm, "code": code})


# ---- seed two drugs (the real app loads these from the Cardinal CSV)
with db.SessionLocal() as s:
    s.add_all([db.Product(ndc="00000000001", description="TESTDRUG A 10MG", upc="111111111111"),
               db.Product(ndc="00000000002", description="TESTDRUG B 20MG", upc="222222222222")])
    s.commit()

print("Sign-up disabled")
c = Client()
r = c.get("/setup")      # no users yet -> forced to the setup page
check("fresh install goes to /setup", r.status_code == 200 and "owner account" in text_of(r))
r = c.post("/setup", {"name": "Owner", "login": "boss", "password": "ownerpass1", "confirm": "ownerpass1"})
check("owner created, lands on All stores", r.status_code == 302 and r.location.endswith("/all-stores"))
with db.SessionLocal() as s:
    check("owner role is 'owner' with no store",
          s.query(db.User).filter_by(login="boss").one().role == "owner"
          and s.query(db.User).filter_by(login="boss").one().store_id is None)
c = Client()
r = c.get("/signup")
check("page says sign-up is off", "Sign-up is turned off. Ask the owner for access." in text_of(r))
check("no form shown when off", "<form" not in text_of(r).split("</header>")[-1] or "store_name" not in text_of(r))
r = signup(c, "Hack Store", "hacker")
check("POST while disabled creates nothing", "Sign-up is turned off" in text_of(r))

os.environ["SIGNUP_CODE"] = "letmein"

print("Sign-up")
c = Client("2.2.2.2")
r = signup(c, "Main Street Pharmacy", "mainst", code="wrong")
check("wrong code refused", "sign-up code is wrong" in text_of(r))
r = signup(c, "Main Street Pharmacy", "mainst", pw="short", confirm="short")
check("short password refused", "at least 8 characters" in text_of(r))
r = signup(c, "Main Street Pharmacy", "mainst", confirm="different123")
check("password mismatch refused", "don&#39;t match" in text_of(r) or "don't match" in text_of(r))
r = signup(c, "Main Street Pharmacy", "mainst")
check("right code creates account and goes to Scan", r.status_code == 302 and r.location.endswith("/scan"))
r = c.get("/scan")
check("scan page works, green bar shows store name",
      r.status_code == 200 and "Main Street Pharmacy" in text_of(r) and 'class="storebar"' in text_of(r))
with db.SessionLocal() as s:
    u = s.query(db.User).filter_by(login="mainst").one()
    check("user is role 'store' linked to a Store row",
          u.role == "store" and s.get(db.Store, u.store_id).name == "Main Street Pharmacy")
    check("password is hashed", u.password_hash != "password123" and u.password_hash.startswith(("scrypt", "pbkdf2")))

c2 = Client("3.3.3.3")
r = signup(c2, "main street PHARMACY", "other")
check("duplicate store name refused (case-insensitive)", "already exists" in text_of(r))
r = signup(c2, "Elm Street Pharmacy", "MAINST")
check("duplicate username refused (case-insensitive)", "is taken" in text_of(r))
r = signup(c2, "Elm Street Pharmacy", "elmst")
check("second store signs up", r.status_code == 302)
r = c2.c.post("/signup", data={"store_name": "X", "login": "x", "password": "password123",
                               "confirm": "password123", "code": "letmein"},
              environ_overrides={"REMOTE_ADDR": "3.3.3.3"})
check("POST without CSRF token refused", r.status_code in (302, 400))
with db.SessionLocal() as s:
    check("CSRF-less sign-up created nothing", s.query(db.Store).filter_by(name="X").count() == 0)

print("Login")
a = Client("4.4.4.4")
r = a.post("/login", {"login": "mainst", "password": "nope"})
check("wrong password refused", "wrong" in text_of(r))
r = a.post("/login", {"login": "MainSt", "password": "password123"})
check("login works", r.status_code == 302 and r.location.endswith("/scan"))

print("Scanning and stock stay separate between stores")
b = Client("5.5.5.5")
check("second store logs in", b.post("/login", {"login": "elmst", "password": "password123"}).status_code == 302)
r = a.api("/api/adjust", {"ndc": "00000000001", "qty": 10, "action": "received"})
check("store A receives 10", r.get_json().get("on_hand") == 10)
r = b.api("/api/adjust", {"ndc": "00000000001", "qty": 3, "action": "received"})
check("store B receives 3 of the same drug", r.get_json().get("on_hand") == 3)
r = a.api("/api/adjust", {"ndc": "00000000001", "qty": 4, "action": "dispensed"})
check("store A dispenses 4 -> 6", r.get_json().get("on_hand") == 6)
r = b.api("/api/adjust", {"ndc": "00000000002", "qty": 5, "action": "dispensed"})
check("store B can't dispense more than it has", r.status_code == 400)
with db.SessionLocal() as s:
    qa = s.execute(text("select quantity_on_hand from inventory i join stores st on st.id=i.store_id "
                        "where st.name='Main Street Pharmacy'")).scalar()
    qb = s.execute(text("select quantity_on_hand from inventory i join stores st on st.id=i.store_id "
                        "where st.name='Elm Street Pharmacy'")).scalar()
    check("database has A=6, B=3", (qa, qb) == (6, 3), (qa, qb))
hist_b = text_of(b.get("/history"))
check("B's history shows only B's +3", "+3" in hist_b and "-4" not in hist_b)
rb = b.get("/api/recent").get_json()
check("B's recent list has 1 entry", len(rb) == 1)

print("Store can't reach another store")
with db.SessionLocal() as s:
    id_a = s.query(db.Store).filter_by(name="Main Street Pharmacy").one().id
    id_b = s.query(db.Store).filter_by(name="Elm Street Pharmacy").one().id
check("A: /stock?store=<B> refused", a.get(f"/stock?store={id_b}").status_code == 403)
check("A: /history?store=<B> refused", a.get(f"/history?store={id_b}").status_code == 403)
check("A: /export/stock?store=<B> refused", a.get(f"/export/stock?store={id_b}").status_code == 403)
check("A: /export/history?store=<B> refused", a.get(f"/export/history?store={id_b}").status_code == 403)
check("A: /export/stock?store=all refused", a.get("/export/stock?store=all").status_code == 403)
check("A: ?store=<own id> is fine", a.get(f"/stock?store={id_a}").status_code == 200)
r = a.post("/stock/min", {"store": id_b, "ndc": "00000000001", "min_qty": 99})
check("A: can't set B's low-stock level", r.status_code == 403)
with db.SessionLocal() as s:
    check("B's low-stock level unchanged", s.query(db.Inventory).filter_by(store_id=id_b).first().min_qty == 0)
r = a.post("/stock/min", {"ndc": "00000000001", "min_qty": 2})
with db.SessionLocal() as s:
    check("A can set its own low-stock level", s.query(db.Inventory).filter_by(store_id=id_a).first().min_qty == 2)
check("A: no All stores page", a.get("/all-stores").status_code == 403)
check("A: no Accounts page", a.get("/accounts").status_code == 403)
check("removed pages are gone",
      all(a.get(u).status_code == 404 for u in ("/stores", "/users", "/transfer")))
check("A's page has no store switcher or removed nav",
      all(w not in text_of(a.get("/stock")) for w in ("Switch store", "Transfer", ">Users<", ">Stores<")))

print("'Your name' note")
r = a.api("/api/adjust", {"ndc": "00000000002", "qty": 2, "action": "received", "note": "invoice 77", "by": "Maria"})
check("scan with name + note succeeds", r.get_json().get("ok"))
r = a.api("/api/adjust", {"ndc": "00000000002", "qty": 1, "action": "received", "by": "Maria"})
r = a.api("/api/adjust", {"ndc": "00000000002", "qty": 1, "action": "received"})
with db.SessionLocal() as s:
    notes = [t.note for t in s.query(db.Transaction).filter_by(store_id=id_a, ndc="00000000002").order_by(db.Transaction.id)]
check("note = typed note + 'By Maria'", notes[0] == "invoice 77 | By Maria", notes)
check("name only -> 'By Maria'", notes[1] == "By Maria", notes)
check("no name, no note -> empty", notes[2] is None, notes)
r = a.api("/api/adjust", {"ndc": "00000000002", "qty": 9, "action": "count", "by": "Maria"})
with db.SessionLocal() as s:
    last = s.query(db.Transaction).order_by(db.Transaction.id.desc()).first().note
check("count note keeps 'Counted ...' and adds name", last.startswith("Counted 9") and last.endswith("By Maria"), last)
r = a.api("/api/adjust", {"ndc": "00000000002", "qty": 1, "action": "received", "note": "x" * 300, "by": "Maria"})
with db.SessionLocal() as s:
    last = s.query(db.Transaction).order_by(db.Transaction.id.desc()).first().note
check("very long note still ends with 'By Maria' within 255 chars", len(last) <= 255 and last.endswith("By Maria"))
check("History page shows 'By Maria'", "By Maria" in text_of(a.get("/history")))
check("scan page has the Your name box above the barcode box",
      text_of(a.get("/scan")).index('id="by"') < text_of(a.get("/scan")).index('id="code"'))
js = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "scan.js"), encoding="utf-8").read()
check("scan.js remembers the name in localStorage inside try/catch",
      'localStorage.setItem("scanName"' in js and 'localStorage.getItem("scanName")' in js and "by: by.value" in js)

print("Drug list")
r = a.get("/drugs?all=1")
check("Show all lists every drug", "TESTDRUG A" in text_of(r) and "TESTDRUG B" in text_of(r) and "2 drugs" in text_of(r))
check("search still works", "TESTDRUG B" in text_of(a.get("/drugs?q=testdrug+b")) and "TESTDRUG A" not in text_of(a.get("/drugs?q=testdrug+b")))
check("plain page lists nothing until asked", "TESTDRUG A" not in text_of(a.get("/drugs")))

print("Store account page")
r = a.post("/account", {"action": "rename", "store_name": "elm street pharmacy"})
check("rename to another store's name refused", "already exists" in text_of(r))
r = a.post("/account", {"action": "rename", "store_name": "Main St Pharmacy"})
check("rename works", r.status_code == 302 and "Main St Pharmacy" in text_of(a.get("/scan")))
r = a.post("/account", {"current": "wrongwrong", "new": "newpassword1", "confirm": "newpassword1"})
check("password change needs current password", "current password is wrong" in text_of(r))
r = a.post("/account", {"current": "password123", "new": "newpassword1", "confirm": "newpassword1"})
check("password change works", r.status_code == 302)
check("new password logs in",
      Client("6.6.6.6").post("/login", {"login": "mainst", "password": "newpassword1"}).status_code == 302)

print("Owner")
o = Client("7.7.7.7")
check("owner logs in to All stores", o.post("/login", {"login": "boss", "password": "ownerpass1"}).location.endswith("/all-stores"))
page = text_of(o.get("/all-stores"))
check("owner sees both stores on All stores", "Main St Pharmacy" in page and "Elm Street Pharmacy" in page)
check("owner nav has no Scan link", 'href="/scan"' not in page and ">Scan<" not in page)
check("owner can't scan", o.get("/scan").status_code == 403 and
      o.api("/api/adjust", {"ndc": "00000000001", "qty": 1, "action": "received"}).status_code == 403)
check("owner sees A's stock via ?store=", "Main St Pharmacy" in text_of(o.get(f"/stock?store={id_a}")))
check("owner sees B's history via ?store=", "+3" in text_of(o.get(f"/history?store={id_b}")))
check("owner has a store dropdown on Stock", "<select name=\"store\"" in text_of(o.get("/stock")))
check("owner can't see a store that doesn't exist", o.get("/stock?store=9999").status_code == 404)


def sheets(resp):
    return pd.read_excel(io.BytesIO(resp.data), sheet_name=None)


from openpyxl import load_workbook          # noqa: E402


def book(resp):
    return load_workbook(io.BytesIO(resp.data))


r = o.get(f"/export/stock?store={id_b}")
check("old /export/stock now redirects to the styled report", r.status_code == 302 and "/export/inventory-report" in r.location)
r = o.get(f"/export/inventory-report?store={id_b}")
check("owner downloads one store's Excel", r.status_code == 200 and "spreadsheetml" in r.mimetype
      and book(r).sheetnames == ["Inventory count", "Scan log"] and book(r)["Inventory count"]["A1"].value == "Elm Street Pharmacy")
r = o.get("/export/inventory-report?store=all")
wb = book(r)
check("owner downloads all stores in one Excel (Summary first)",
      wb.sheetnames[0] == "Summary" and {"Main St Pharmacy count", "Main St Pharmacy log", "Elm Street Pharmacy count",
                                         "Elm Street Pharmacy log"} <= set(wb.sheetnames), wb.sheetnames)
ws = wb["Elm Street Pharmacy count"]
check("Excel numbers are right", [ws.cell(r_, 5).value for r_ in range(7, 20) if ws.cell(r_, 2).value] == [3])
r = a.get("/export/inventory-report")
check("store account downloads its own Excel", r.status_code == 200 and book(r)["Inventory count"]["A1"].value == "Main St Pharmacy")
check("store account's /export page works, another store is refused",
      a.get("/export").status_code == 200 and a.get(f"/export?store={id_b}").status_code == 403)
check("store account can't use the all-stores report", a.get("/export/inventory-report?store=all").status_code == 403
      and a.get("/export?store=all").status_code == 403)
r = o.get(f"/export/history?store={id_a}")
hist = list(sheets(r).values())[0]
check("history Excel includes the 'By Maria' note", hist["Note"].astype(str).str.contains("By Maria").any())

print("Accounts page and deactivation")
page = text_of(o.get("/accounts"))
check("Accounts lists every store account", "mainst" in page and "elmst" in page)
with db.SessionLocal() as s:
    uid_b = s.query(db.User).filter_by(login="elmst").one().id
r = o.post("/accounts", {"action": "reset", "id": uid_b, "password": "short"})
check("reset needs 8+ characters", "at least 8" in text_of(o.get("/accounts")))
o.post("/accounts", {"action": "reset", "id": uid_b, "password": "resetpass99"})
check("reset password works for the store",
      Client("8.8.8.8").post("/login", {"login": "elmst", "password": "resetpass99"}).status_code == 302)
check("old password no longer works",
      "wrong" in text_of(Client("8.8.8.9").post("/login", {"login": "elmst", "password": "password123"})))
check("a store account can't use the Accounts actions", a.post("/accounts", {"action": "toggle", "id": uid_b}).status_code == 403)

o.post("/accounts", {"action": "toggle", "id": uid_b})
check("deactivated store's logged-in session is cut off", b.get("/stock").status_code == 302)
check("deactivated store can't log in",
      "wrong" in text_of(Client("9.9.9.9").post("/login", {"login": "elmst", "password": "resetpass99"})))
o.get("/accounts")      # shows (and clears) the "now deactivated" message
page = text_of(o.get("/all-stores"))
check("deactivated store hidden from All stores", "Elm Street Pharmacy" not in page)
check("deactivated store hidden from owner's store dropdown", "Elm Street Pharmacy" not in text_of(o.get("/stock")))
check("all-stores Excel leaves it out", book(o.get("/export/inventory-report?store=all")).sheetnames == ["Summary", "Main St Pharmacy count", "Main St Pharmacy log"])
check("owner can't open it by id while deactivated", o.get(f"/stock?store={id_b}").status_code == 404)
with db.SessionLocal() as s:
    check("history is kept in the database", s.query(db.Transaction).filter_by(store_id=id_b).count() == 1
          and s.query(db.Store).filter_by(id=id_b).count() == 1)
check("Accounts page still lists it as deactivated", "Deactivated" in text_of(o.get("/accounts")))
o.post("/accounts", {"action": "toggle", "id": uid_b})
check("reactivated store can log in again",
      Client("9.9.9.8").post("/login", {"login": "elmst", "password": "resetpass99"}).status_code == 302)
check("reactivated store is back in reports with its history",
      "Elm Street Pharmacy" in text_of(o.get("/all-stores")) and "+3" in text_of(o.get(f"/history?store={id_b}")))

print("Rate limit")
x = Client("10.0.0.1")
for _ in range(5):
    x.post("/login", {"login": "mainst", "password": "bad-bad-bad"})
r = x.post("/login", {"login": "mainst", "password": "newpassword1"})
check("5 failed logins block that IP, even with the right password",
      r.status_code == 429 and "Too many" in text_of(r))
check("a different IP is not blocked",
      Client("10.0.0.2").post("/login", {"login": "mainst", "password": "newpassword1"}).status_code == 302)
y = Client("10.0.0.3")
for _ in range(5):
    signup(y, "Z Store", "zstore", code="nope")
r = signup(y, "Z Store", "zstore")
check("5 wrong sign-up codes block sign-up from that IP", r.status_code == 429)
with db.SessionLocal() as s:
    check("blocked sign-up created nothing", s.query(db.Store).filter_by(name="Z Store").count() == 0)
appmod._blocked[("login", "10.0.0.1")] -= 16 * 60
check("block ends after 15 minutes",
      Client("10.0.0.1").post("/login", {"login": "mainst", "password": "newpassword1"}).status_code == 302)

print("Old data / role migration")
with db.SessionLocal() as s:
    s.execute(text("UPDATE users SET role='admin' WHERE login='boss'"))
    s.execute(text("UPDATE users SET role='manager' WHERE login='mainst'"))
    s.execute(text("UPDATE users SET role='staff' WHERE login='elmst'"))
    s.commit()
before = None
with db.SessionLocal() as s:
    before = {t: s.execute(text(f"select count(*) from {t}")).scalar()
              for t in ("stores", "users", "products", "inventory", "transactions")}
db.create_tables()
with db.SessionLocal() as s:
    roles = dict(s.execute(text("select login, role from users")).all())
    after = {t: s.execute(text(f"select count(*) from {t}")).scalar() for t in before}
check("admin -> owner, staff/manager -> store", roles["boss"] == "owner" and roles["mainst"] == "store" and roles["elmst"] == "store", roles)
check("no rows lost in the 5 tables", before == after, (before, after))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
