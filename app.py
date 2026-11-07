"""
Pharmacy inventory web app (Flask).

Run locally:   python app.py        then open http://127.0.0.1:5000
On Render:     gunicorn app:app     (see render.yaml)

Environment variables:
    DATABASE_URL   Neon connection string (falls back to local SQLite)
    SECRET_KEY     random string that signs login cookies (required in production)
    SIGNUP_CODE    code a new store must enter to sign up (sign-up is off if not set)
"""
import calendar
import io
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from functools import wraps

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.page import PageMargins
from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request,
    send_file, session, url_for,
)
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import counts
from barcode import find_product
from db import CountEntry, CountSession, Inventory, Product, SessionLocal, Store, Transaction, User, create_tables
from inventory import adjust_stock

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")),   # HTTPS-only cookies on Render
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

if os.getenv("RENDER"):          # behind Render's proxy: use the real client IP for rate limiting
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

create_tables()

LOCAL_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "America/New_York"))


def local(dt, fmt="%Y-%m-%d %H:%M"):
    """Show a stored (UTC) time in the pharmacy's time zone."""
    if dt is None:
        return ""
    if dt.tzinfo is None:            # SQLite returns times without a zone; they're UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).strftime(fmt)


app.jinja_env.filters["local"] = local


def to_local(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def fmt_date(dt):
    """Local date like 'Nov 7, 2026' (works on Windows too, unlike %-d)."""
    d = to_local(dt)
    return f"{d:%b} {d.day}, {d.year}"


app.jinja_env.filters["ldate"] = fmt_date
PUBLIC_ENDPOINTS = {"login", "signup", "setup", "static", "health"}


# ---------------------------------------------------------------- request setup

@app.before_request
def load_user():
    g.db = SessionLocal()
    g.user = None
    if request.endpoint in ("static", "health"):
        return
    no_users = g.db.scalar(select(func.count(User.id))) == 0
    if no_users and request.endpoint != "setup":
        return redirect(url_for("setup"))
    uid = session.get("user_id")
    if uid:
        user = g.db.get(User, uid)
        store = g.db.get(Store, user.store_id) if user and user.store_id else None
        store_ok = user is not None and (user.role != "store" or (store is not None and store.active))
        if user and user.active and store_ok:
            g.user = user
        else:
            session.clear()
    if g.user is None and request.endpoint not in PUBLIC_ENDPOINTS:
        if request.path.startswith("/api/"):
            return jsonify(error="Your session ended. Log in again."), 401
        return redirect(url_for("login", next=request.path))
    if request.method == "POST":
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        if not sent or sent != session.get("csrf_token"):
            if request.path.startswith("/api/"):
                return jsonify(error="Page expired. Reload and try again."), 400
            flash("That form expired. Try again.", "error")
            return redirect(request.path)


@app.teardown_request
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        if exc:
            db.rollback()
        db.close()


@app.context_processor
def template_globals():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    ctx = {"csrf_token": session["csrf_token"], "user": g.get("user")}
    if g.get("user"):
        sid = working_store_id()
        ctx["working_store"] = g.db.get(Store, sid) if sid else None
    return ctx


def role_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if g.user.role not in roles:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def active_stores():
    return g.db.scalars(select(Store).where(Store.active).order_by(Store.name)).all()


def working_store_id():
    """The store this account scans for. Store accounts have exactly one; the owner has none."""
    return g.user.store_id if g.user.role == "store" else None


def viewable_store_id(requested):
    """Store for a report page. The owner may pick any active store; a store account only ever
    gets its own, and asking for any other store (?store=...) is refused."""
    if g.user.role != "owner":
        if requested and str(requested) != str(g.user.store_id):
            abort(403)
        return g.user.store_id
    if requested:
        try:
            sid = int(requested)
        except ValueError:
            abort(404)
        store = g.db.get(Store, sid)
        if store is None or not store.active:
            abort(404)
        return sid
    return g.db.scalar(select(Store.id).where(Store.active).order_by(Store.name))


# ---------------------------------------------------------------- rate limiting
# In memory, per worker process: after 5 failures from one IP in 15 minutes, block for 15 minutes.
MAX_FAILS, WINDOW = 5, 15 * 60
_fails = {}          # (kind, ip) -> list of failure times
_blocked = {}        # (kind, ip) -> time the block ends


def _rl_key(kind):
    return (kind, request.remote_addr or "?")


def rate_limited(kind):
    key, now = _rl_key(kind), time.time()
    until = _blocked.get(key)
    if until and until > now:
        return True
    _blocked.pop(key, None)
    return False


def record_failure(kind):
    key, now = _rl_key(kind), time.time()
    hits = [t for t in _fails.get(key, []) if now - t < WINDOW] + [now]
    _fails[key] = hits
    if len(hits) >= MAX_FAILS:
        _blocked[key] = now + WINDOW
        _fails.pop(key, None)


def clear_failures(kind):
    _fails.pop(_rl_key(kind), None)


def too_many_message():
    flash("Too many failed attempts. Try again in 15 minutes.", "error")


def store_name_taken(name, except_id=None):
    stmt = select(Store).where(func.lower(Store.name) == name.lower())
    if except_id:
        stmt = stmt.where(Store.id != except_id)
    return g.db.scalars(stmt).first() is not None


def clean_digits(value):
    return re.sub(r"\D", "", value or "")


def fmt_ndc(ndc):
    return f"{ndc[:5]}-{ndc[5:9]}-{ndc[9:]}" if ndc and len(ndc) == 11 else (ndc or "")


app.jinja_env.filters["ndc"] = fmt_ndc


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def price_info(p):
    """When the price came from, and whether that invoice is more than 6 months old."""
    d = p.last_invoice_date
    if not d:
        return {"price_date": None, "price_date_display": None, "price_stale": False}
    today = datetime.now(LOCAL_TZ).date()
    return {"price_date": d.isoformat(), "price_date_display": f"{d:%b} {d.day}, {d.year}",
            "price_stale": d < add_months(today, -6)}


def stock_info(store_id, ndc):
    """When this store's stock of the drug last changed, and who did it ("By <name>" from the
    latest transaction's note, else the name on the account)."""
    row = stock_row(store_id, ndc)
    if row is None or row.last_updated is None:
        return {"stock_updated_at": None, "stock_updated_display": None, "stock_updated_by": None}
    latest = g.db.execute(
        select(Transaction.note, User.name).outerjoin(User, User.id == Transaction.user_id)
        .where(Transaction.store_id == store_id, Transaction.ndc == ndc)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(1)).first()
    by = None
    if latest:
        m = re.search(r"(?:^|\| )By (.+)$", latest[0] or "")
        by = m.group(1).strip() if m else latest[1]
    dt = to_local(row.last_updated)
    return {"stock_updated_at": dt.isoformat(),
            "stock_updated_display": f"{dt:%b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}",
            "stock_updated_by": by or g.user.name}


def product_json(p, qty=None, min_qty=None, store_id=None):
    extra = price_info(p)
    if store_id:
        extra.update(stock_info(store_id, p.ndc))
    return {**extra,
        "ndc": p.ndc, "ndc_display": fmt_ndc(p.ndc), "upc": p.upc,
        "description": p.description, "generic_name": p.generic_name,
        "size": p.size,
        "package_size": float(p.package_size) if p.package_size is not None else None,
        "brand_generic": p.brand_generic,
        "price": float(p.last_unit_price) if p.last_unit_price is not None else None,
        "price_date": p.last_invoice_date.isoformat() if p.last_invoice_date else None,
        "on_hand": qty, "min_qty": min_qty,
    }


def stock_row(store_id, ndc):
    return g.db.scalars(select(Inventory).where(
        Inventory.store_id == store_id, Inventory.ndc == ndc)).first()


# ---------------------------------------------------------------- setup & login

@app.route("/health")
def health():
    return "ok"


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if g.db.scalar(select(func.count(User.id))) > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        login_name = request.form.get("login", "").strip().lower()
        pw = request.form.get("password", "")
        if not name or not login_name:
            flash("Enter your name and a username.", "error")
        elif len(pw) < 8:
            flash("Use a password of at least 8 characters.", "error")
        elif pw != request.form.get("confirm", ""):
            flash("The two passwords don't match.", "error")
        else:
            user = User(name=name, login=login_name, role="owner",
                        password_hash=generate_password_hash(pw))
            g.db.add(user)
            g.db.commit()
            start_session(user)
            flash("Owner account created. Stores sign up for their own accounts.", "ok")
            return redirect(url_for("all_stores"))
    return render_template("setup.html")


def start_session(user):
    session.clear()
    session["user_id"] = user.id
    session.permanent = True


def home_url(user):
    return url_for("all_stores") if user.role == "owner" else url_for("scan")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    code = os.getenv("SIGNUP_CODE", "")
    if not code:
        return render_template("signup.html", disabled=True)
    if request.method == "POST":
        if rate_limited("signup"):
            too_many_message()
            return render_template("signup.html", disabled=False), 429
        store_name = " ".join(request.form.get("store_name", "").split())
        login_name = request.form.get("login", "").strip().lower()
        pw = request.form.get("password", "")
        if not secrets.compare_digest(request.form.get("code", "").encode(), code.encode()):
            record_failure("signup")
            flash("That sign-up code is wrong.", "error")
        elif not store_name or not login_name:
            flash("Enter your store name and a username.", "error")
        elif len(store_name) > 100 or len(login_name) > 100:
            flash("The store name and username must be 100 characters or fewer.", "error")
        elif len(pw) < 8:
            flash("Use a password of at least 8 characters.", "error")
        elif pw != request.form.get("confirm", ""):
            flash("The two passwords don't match.", "error")
        elif store_name_taken(store_name):
            flash(f"A store called {store_name} already exists.", "error")
        elif g.db.scalars(select(User).where(func.lower(User.login) == login_name)).first():
            flash(f"The username {login_name} is taken.", "error")
        else:
            try:
                store = Store(name=store_name, active=True)
                g.db.add(store)
                g.db.flush()
                user = User(name=store_name, login=login_name, role="store", store_id=store.id,
                            password_hash=generate_password_hash(pw), active=True)
                g.db.add(user)
                g.db.commit()
            except IntegrityError:        # someone grabbed the same name a moment ago
                g.db.rollback()
                flash("That store name or username was just taken. Try another.", "error")
                return render_template("signup.html", disabled=False)
            clear_failures("signup")
            start_session(user)
            flash(f"Welcome, {store_name}! Your account is ready.", "ok")
            return redirect(url_for("scan"))
    return render_template("signup.html", disabled=False)


@app.route("/login", methods=["GET", "POST"])
def login():
    signup_on = bool(os.getenv("SIGNUP_CODE"))
    if request.method == "POST":
        if rate_limited("login"):
            too_many_message()
            return render_template("login.html", signup_on=signup_on), 429
        login_name = request.form.get("login", "").strip().lower()
        user = g.db.scalars(select(User).where(func.lower(User.login) == login_name)).first()
        if user and user.active and check_password_hash(user.password_hash, request.form.get("password", "")):
            store = g.db.get(Store, user.store_id) if user.store_id else None
            if user.role != "store" or (store and store.active):
                clear_failures("login")
                start_session(user)
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else home_url(user))
        record_failure("login")
        flash("Username or password is wrong.", "error")
    return render_template("login.html", signup_on=signup_on)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
def account():
    store = g.db.get(Store, g.user.store_id) if g.user.store_id else None
    if request.method == "POST":
        if request.form.get("action") == "rename" and store:
            name = " ".join(request.form.get("store_name", "").split())
            if not name or len(name) > 100:
                flash("Enter a store name (100 characters or fewer).", "error")
            elif store_name_taken(name, except_id=store.id):
                flash(f"A store called {name} already exists.", "error")
            else:
                store.name = name
                g.user.name = name
                g.db.commit()
                flash("Store renamed.", "ok")
                return redirect(url_for("account"))
        elif not check_password_hash(g.user.password_hash, request.form.get("current", "")):
            flash("Your current password is wrong.", "error")
        elif len(request.form.get("new", "")) < 8:
            flash("Use a new password of at least 8 characters.", "error")
        elif request.form.get("new") != request.form.get("confirm"):
            flash("The two new passwords don't match.", "error")
        else:
            g.user.password_hash = generate_password_hash(request.form["new"])
            g.db.commit()
            flash("Password changed.", "ok")
            return redirect(url_for("account"))
    return render_template("account.html", store=store)


# ---------------------------------------------------------------- scanning

@app.route("/")
def home():
    return redirect(home_url(g.user))


@app.route("/scan")
@role_required("store")
def scan():
    sid = working_store_id()
    return render_template("scan.html", store_id=sid, count_session=counts.open_session(g.db, sid))


@app.route("/api/lookup")
@role_required("store")
def api_lookup():
    sid = working_store_id()
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify(error="Scan or type a barcode."), 400
    product, extra = find_product(g.db, code)
    if product is None:
        return jsonify(found=False, code=clean_digits(code) or code, extra=extra)
    row = stock_row(sid, product.ndc) if sid else None
    out = dict(found=True, extra=extra,
               product=product_json(product, row.quantity_on_hand if row else 0,
                                    row.min_qty if row else 0, store_id=sid))
    sess = counts.open_session(g.db, sid)
    if sess:
        total, people = counts.tally(g.db, sess.id, product.ndc)
        out["count"] = {"counted": total, "people": [{"name": n, "qty": q} for n, q in people]}
    return jsonify(out)


@app.route("/api/adjust", methods=["POST"])
@role_required("store")
def api_adjust():
    sid = working_store_id()
    if not sid:
        return jsonify(error="No store selected."), 400
    if counts.open_session(g.db, sid):       # enforced here, not just in the page
        return jsonify(error=counts.BLOCK_MESSAGE), 409
    data = request.get_json(silent=True) or {}
    ndc, action = data.get("ndc", ""), data.get("action")
    try:
        qty = int(data.get("qty"))
    except (TypeError, ValueError):
        return jsonify(error="Enter a whole number for quantity."), 400
    if g.db.get(Product, ndc) is None:
        return jsonify(error="Unknown drug."), 404

    note = (data.get("note") or "").strip()
    who = " ".join(str(data.get("by") or "").split())[:40]
    row = stock_row(sid, ndc)
    current = row.quantity_on_hand if row else 0
    if action == "received":
        if qty <= 0:
            return jsonify(error="Quantity must be more than 0."), 400
        change, reason = qty, "received"
    elif action == "dispensed":
        if qty <= 0:
            return jsonify(error="Quantity must be more than 0."), 400
        change, reason = -qty, "dispensed"
    elif action == "count":
        if qty < 0:
            return jsonify(error="A count can't be negative."), 400
        change, reason = qty - current, "adjusted"
        note = note or f"Counted {qty} (was {current})"
        if change == 0:
            return jsonify(ok=True, on_hand=current, change=0, message=f"Count matches: {current} on hand.",
                           **stock_info(sid, ndc))
    else:
        return jsonify(error="Choose receive, dispense or count."), 400

    if who:      # record who did it, e.g. "invoice 123 | By Maria"
        tag = f"By {who}"
        note = f"{note[:255 - len(tag) - 3]} | {tag}" if note else tag
    note = note[:255] or None if note else None
    try:
        new_qty = adjust_stock(g.db, sid, ndc, change, reason, g.user.id, note)
    except ValueError as e:
        g.db.rollback()
        return jsonify(error=str(e)), 400
    except IntegrityError:          # two scans created the same stock row at once; retry once
        g.db.rollback()
        new_qty = adjust_stock(g.db, sid, ndc, change, reason, g.user.id, note)
    return jsonify(ok=True, on_hand=new_qty, change=change, **stock_info(sid, ndc))


def _count_reply(sess, ndc, **extra):
    total, people = counts.tally(g.db, sess.id, ndc)
    row = stock_row(sess.store_id, ndc)
    return jsonify(ok=True, counted=total, people=[{"name": n, "qty": q} for n, q in people],
                   on_hand=row.quantity_on_hand if row else 0, **extra)


@app.route("/api/count/entry", methods=["POST"])
@role_required("store")
def api_count_entry():
    sid = working_store_id()
    sess = counts.open_session(g.db, sid)
    if sess is None:
        return jsonify(error="No count is open. Reload the page."), 409
    data = request.get_json(silent=True) or {}
    by = counts.clean_name(data.get("by"))
    if not by:
        return jsonify(error="Enter your name before counting."), 400
    ndc = data.get("ndc", "")
    if g.db.get(Product, ndc) is None:
        return jsonify(error="Unknown drug."), 404
    try:
        qty = int(data.get("qty"))
    except (TypeError, ValueError):
        return jsonify(error="Enter a whole number for quantity."), 400
    try:
        entry = counts.add_entry(g.db, sess.id, ndc, qty, by, data.get("location"))
    except counts.CountError as e:
        return jsonify(error=str(e)), e.status
    return _count_reply(sess, ndc, entry_id=entry.id)


@app.route("/api/count/undo", methods=["POST"])
@role_required("store")
def api_count_undo():
    sid = working_store_id()
    sess = counts.open_session(g.db, sid)
    if sess is None:
        return jsonify(error="No count is open. Reload the page."), 409
    by = counts.clean_name((request.get_json(silent=True) or {}).get("by"))
    try:
        entry = counts.undo_last(g.db, sess.id, by)
    except counts.CountError as e:
        return jsonify(error=str(e)), e.status
    if entry is None:
        return jsonify(error=f"{by} has no count to undo."), 404
    drug = g.db.get(Product, entry.ndc)
    return _count_reply(sess, entry.ndc, message=f"Removed {entry.qty} × {drug.description} counted by {entry.counted_by}.")


@app.route("/api/products", methods=["POST"])
def api_add_product():
    data = request.get_json(silent=True) or {}
    ndc = clean_digits(data.get("ndc"))
    upc = clean_digits(data.get("upc")) or None
    desc = (data.get("description") or "").strip().upper()
    if len(ndc) != 11:
        return jsonify(error="NDC must be 11 digits, e.g. 00074-0554-02. Add a leading zero to the short segment if the label shows 10."), 400
    if not desc:
        return jsonify(error="Enter the drug name and strength."), 400
    if g.db.get(Product, ndc):
        return jsonify(error=f"NDC {fmt_ndc(ndc)} is already in the drug list."), 400
    try:
        pkg = float(data["package_size"]) if data.get("package_size") not in (None, "") else None
    except ValueError:
        return jsonify(error="Package size must be a number."), 400
    p = Product(ndc=ndc, upc=upc, description=desc,
                generic_name=(data.get("generic_name") or "").strip().upper() or None,
                size=(data.get("size") or "").strip().upper() or None,
                package_size=pkg, brand_generic=data.get("brand_generic") or None)
    g.db.add(p)
    g.db.commit()
    return jsonify(ok=True, product=product_json(p, 0, 0, store_id=working_store_id()))


@app.route("/api/recent")
@role_required("store")
def api_recent():
    sid = working_store_id()
    rows = g.db.execute(
        select(Transaction, Product.description, User.name)
        .join(Product, Product.ndc == Transaction.ndc)
        .outerjoin(User, User.id == Transaction.user_id)
        .where(Transaction.store_id == sid)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(12)
    ).all() if sid else []
    return jsonify([{
        "description": d, "change": t.change, "reason": t.reason, "user": n or "",
        "at": (t.created_at if t.created_at.tzinfo else t.created_at.replace(tzinfo=timezone.utc)).isoformat(),
    } for t, d, n in rows])


# ---------------------------------------------------------------- reports

def stock_query(store_id, q, show):
    stmt = (select(Inventory, Product).join(Product, Product.ndc == Inventory.ndc)
            .where(Inventory.store_id == store_id))
    if q:
        like = f"%{q.upper()}%"
        digits = clean_digits(q)
        conds = [Product.description.ilike(like), Product.generic_name.ilike(like)]
        if digits:
            conds += [Product.ndc.like(f"%{digits}%"), Product.upc.like(f"%{digits}%")]
        stmt = stmt.where(or_(*conds))
    if show == "in":
        stmt = stmt.where(Inventory.quantity_on_hand > 0)
    elif show == "low":
        stmt = stmt.where(Inventory.min_qty > 0, Inventory.quantity_on_hand <= Inventory.min_qty)
    elif show == "out":
        stmt = stmt.where(Inventory.quantity_on_hand == 0)
    return g.db.execute(stmt.order_by(Product.description)).all()


@app.route("/stock")
def stock():
    sid = viewable_store_id(request.args.get("store"))
    q, show = request.args.get("q", "").strip(), request.args.get("show", "all")
    rows = stock_query(sid, q, show) if sid else []
    total_value = sum(inv.quantity_on_hand * float(p.last_unit_price or 0) for inv, p in rows)
    low_count = g.db.scalar(select(func.count(Inventory.id)).where(
        Inventory.store_id == sid, Inventory.min_qty > 0,
        Inventory.quantity_on_hand <= Inventory.min_qty)) if sid else 0
    stores_list = active_stores() if g.user.role == "owner" else []
    return render_template("stock.html", rows=rows, q=q, show=show, store=g.db.get(Store, sid) if sid else None,
                           stores_list=stores_list, total_value=total_value, low_count=low_count,
                           can_edit_min=True)


@app.route("/stock/min", methods=["POST"])
def set_min():
    sid = viewable_store_id(request.form.get("store"))
    row = stock_row(sid, request.form.get("ndc", ""))
    if row is None:
        abort(404)
    try:
        row.min_qty = max(0, int(request.form.get("min_qty", 0)))
    except ValueError:
        flash("Low-stock level must be a whole number.", "error")
        return redirect(request.referrer or url_for("stock"))
    g.db.commit()
    return redirect(request.referrer or url_for("stock", store=sid))


@app.route("/all-stores")
@role_required("owner")
def all_stores():
    q = request.args.get("q", "").strip()
    stores_list = active_stores()
    stmt = (select(Product, Inventory.store_id, Inventory.quantity_on_hand)
            .join(Inventory, Inventory.ndc == Product.ndc))
    if q:
        like, digits = f"%{q.upper()}%", clean_digits(q)
        conds = [Product.description.ilike(like), Product.generic_name.ilike(like)]
        if digits:
            conds.append(Product.ndc.like(f"%{digits}%"))
        stmt = stmt.where(or_(*conds))
    table = {}
    for p, sid, qty in g.db.execute(stmt.order_by(Product.description)).all():
        entry = table.setdefault(p.ndc, {"product": p, "qty": {}, "total": 0})
        entry["qty"][sid] = qty
        entry["total"] += qty
    return render_template("all_stores.html", stores_list=stores_list, table=list(table.values()), q=q,
                           count_status=counts.status_by_store(g.db))


@app.route("/history")
def history():
    sid = viewable_store_id(request.args.get("store"))
    reason = request.args.get("reason", "")
    rows = history_rows(sid, reason, 300) if sid else []
    stores_list = active_stores() if g.user.role == "owner" else []
    return render_template("history.html", rows=rows, reason=reason, stores_list=stores_list,
                           store=g.db.get(Store, sid) if sid else None)


def history_rows(store_id, reason="", limit=None):
    stmt = (select(Transaction, Product.description, User.name)
            .join(Product, Product.ndc == Transaction.ndc)
            .outerjoin(User, User.id == Transaction.user_id)
            .where(Transaction.store_id == store_id))
    if reason:
        stmt = stmt.where(Transaction.reason == reason)
    stmt = stmt.order_by(Transaction.created_at.desc(), Transaction.id.desc())
    if limit:
        stmt = stmt.limit(limit)
    return g.db.execute(stmt).all()


@app.route("/drugs")
def drugs():
    q = request.args.get("q", "").strip()
    show_all = request.args.get("all") == "1"
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 100
    rows, matches = [], 0
    if q or show_all:
        stmt = (select(Product, Inventory.quantity_on_hand)
                .outerjoin(Inventory, (Inventory.ndc == Product.ndc) & (Inventory.store_id == working_store_id())))
        if q:
            like, digits = f"%{q.upper()}%", clean_digits(q)
            conds = [Product.description.ilike(like), Product.generic_name.ilike(like)]
            if len(digits) >= 4:
                conds += [Product.ndc.like(f"%{digits}%"), Product.upc.like(f"%{digits}%")]
            stmt = stmt.where(or_(*conds))
        matches = g.db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
        rows = g.db.execute(stmt.order_by(Product.description, Product.ndc)
                            .limit(per_page).offset((page - 1) * per_page)).all()
    total = g.db.scalar(select(func.count(Product.ndc)))
    return render_template("drugs.html", rows=rows, q=q, total=total, show_all=show_all, page=page,
                           per_page=per_page, matches=matches)


# ---------------------------------------------------------------- Excel exports

def xlsx_response(sheets, filename):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for name, df in sheets.items():
            df.to_excel(xl, sheet_name=name[:31], index=False)
            ws = xl.sheets[name[:31]]
            for i, col in enumerate(df.columns, 1):
                width = max([len(str(col))] + [len(str(v)) for v in df[col].head(500)]) + 2
                ws.column_dimensions[ws.cell(1, i).column_letter].width = min(width, 50)
            ws.freeze_panes = "A2"
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/export/stock")
def export_stock():
    """The old plain table is gone: this now sends you to the styled inventory report."""
    store = request.args.get("store")
    if not (store == "all" and g.user.role == "owner"):
        viewable_store_id(store)              # another store -> 403
    return redirect(url_for("export_inventory_report", store=store))


@app.route("/export/history")
def export_history():
    sid = viewable_store_id(request.args.get("store"))
    store = g.db.get(Store, sid) if sid else None
    if not store:
        abort(404)
    df = pd.DataFrame([{
        "Date": local(t.created_at), "NDC": fmt_ndc(t.ndc), "Drug": d,
        "Change": t.change, "Reason": t.reason, "By": n or "", "Note": t.note or "",
    } for t, d, n in history_rows(sid, request.args.get("reason", ""))],
        columns=["Date", "NDC", "Drug", "Change", "Reason", "By", "Note"])
    today = pd.Timestamp.now(tz=LOCAL_TZ).strftime("%Y-%m-%d")
    return xlsx_response({store.name: df}, f"history-{slug(store.name)}-{today}.xlsx")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "store"


# ---------------------------------------------------------------- owner: store accounts

@app.route("/accounts", methods=["GET", "POST"])
@role_required("owner")
def accounts():
    if request.method == "POST":
        action = request.form.get("action")
        try:
            uid = int(request.form.get("id", 0))
        except ValueError:
            abort(400)
        u = g.db.get(User, uid)
        if u is None or u.role != "store":
            abort(404)
        store = g.db.get(Store, u.store_id)
        if action == "toggle":
            new_state = not (u.active and store.active)
            # Deactivating hides the store from reports; its history is kept either way.
            g.db.execute(update(Store).where(Store.id == u.store_id).values(active=new_state))
            g.db.execute(update(User).where(User.store_id == u.store_id, User.role == "store")
                         .values(active=new_state))
            g.db.commit()
            flash(f"{store.name} is now {'active' if new_state else 'deactivated'}.", "ok")
        elif action == "reset":
            pw = request.form.get("password", "")
            if len(pw) < 8:
                flash("Use a password of at least 8 characters.", "error")
            else:
                u.password_hash = generate_password_hash(pw)
                g.db.commit()
                flash(f"Password reset for {u.login}.", "ok")
        else:
            abort(400)
        return redirect(url_for("accounts"))
    rows = g.db.execute(
        select(User, Store).join(Store, Store.id == User.store_id)
        .where(User.role == "store").order_by(Store.active.desc(), Store.name)).all()
    return render_template("accounts.html", rows=rows)


# ---------------------------------------------------------------- count day

def count_store():
    """The store a count page acts on: a store account's own, or the owner's pick."""
    sid = viewable_store_id(request.values.get("store"))
    if not sid:
        abort(404)
    return sid


def count_back(sid, **kw):
    return redirect(url_for("count_day", store=sid, **kw))


def typed_name():
    name = counts.clean_name(request.form.get("name"))
    if not name:
        flash("Enter your name first (the Your name box at the top).", "error")
    return name


@app.route("/count")
def count_day():
    sid = viewable_store_id(request.args.get("store"))
    store = g.db.get(Store, sid) if sid else None
    ctx = dict(store=store, stores_list=active_stores() if g.user.role == "owner" else [],
               sess=None, rev=None, q=request.args.get("q", "").strip(), done=None, past=[])
    if store:
        sess = counts.open_session(g.db, sid)
        ctx["sess"] = sess
        if sess:
            ctx["rev"] = counts.review(g.db, sess, ctx["q"])
        done = request.args.get("done", type=int)
        if done:
            d = g.db.get(CountSession, done)
            if d and d.store_id == sid and d.status == "finalized":
                ctx["done"] = d
        ctx["past"] = g.db.scalars(select(CountSession).where(
            CountSession.store_id == sid, CountSession.status != "open")
            .order_by(CountSession.id.desc()).limit(5)).all()
    return render_template("count.html", **ctx)


@app.route("/count/start", methods=["POST"])
def count_start():
    sid, name = count_store(), typed_name()
    if name:
        try:
            counts.start_count(g.db, sid, name)
            flash("Count started. Every scan now adds to the count.", "ok")
        except counts.CountError as e:
            flash(str(e), "error")
    return count_back(sid)


def _own_session(session_id):
    sid = count_store()
    sess = g.db.get(CountSession, session_id)
    if sess is None or sess.store_id != sid:
        abort(403)
    return sid, sess


@app.route("/count/<int:session_id>/cancel", methods=["POST"])
def count_cancel(session_id):
    sid, sess = _own_session(session_id)
    name = typed_name()
    if name:
        if counts.cancel_count(g.db, sess.id, name):
            flash("Count cancelled. Stock was not changed.", "ok")
        else:
            flash("That count is no longer open.", "error")
    return count_back(sid)


@app.route("/count/<int:session_id>/finalize", methods=["POST"])
def count_finalize(session_id):
    sid, sess = _own_session(session_id)
    name = typed_name()
    if not name:
        return count_back(sid)
    res = counts.finalize_count(g.db, sess.id, name, request.form.get("set_zero") == "1",
                                g.user.id, fmt_date(datetime.now(timezone.utc)))
    if res is None:
        flash("That count was already finished. Nothing was changed.", "error")
        return count_back(sid)
    flash("Count finalized. Stock has been updated.", "ok")
    return count_back(sid, done=sess.id)


def _own_entry(entry_id):
    sid = count_store()
    entry = g.db.get(CountEntry, entry_id)
    sess = g.db.get(CountSession, entry.session_id) if entry else None
    if sess is None or sess.store_id != sid:
        abort(403)
    return sid, entry


@app.route("/count/entry/<int:entry_id>/edit", methods=["POST"])
def count_entry_edit(entry_id):
    sid, entry = _own_entry(entry_id)
    name = typed_name()
    if name:
        try:
            counts.edit_entry(g.db, entry, int(request.form.get("qty", "")), request.form.get("location"), name)
            flash("Entry updated.", "ok")
        except ValueError:
            flash("Enter a whole number for quantity.", "error")
        except counts.CountError as e:
            flash(str(e), "error")
    return count_back(sid)


@app.route("/count/entry/<int:entry_id>/delete", methods=["POST"])
def count_entry_delete(entry_id):
    sid, entry = _own_entry(entry_id)
    name = typed_name()
    if name:
        try:
            counts.delete_entry(g.db, entry, name)
            flash("Entry deleted.", "ok")
        except counts.CountError as e:
            flash(str(e), "error")
    return count_back(sid)


# ---------------------------------------------------------------- inventory report (Excel)

PERIODS = [("this_year", "This year"), ("last_year", "Last year"),
           ("last_30", "Last 30 days"), ("custom", "Custom dates")]


def report_period():
    """(period key, first day, last day) in APP_TIMEZONE. Default: this year."""
    today = datetime.now(LOCAL_TZ).date()

    def parse(v, default):
        try:
            return date.fromisoformat(v)
        except (TypeError, ValueError):
            return default
    period = request.args.get("period")
    if period not in dict(PERIODS):
        period = "custom" if (request.args.get("from") or request.args.get("to")) else "this_year"
    if period == "last_year":
        start, end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    elif period == "last_30":
        start, end = today - timedelta(days=29), today
    elif period == "custom":
        start = parse(request.args.get("from"), date(today.year, 1, 1))
        end = parse(request.args.get("to"), today)
    else:
        start, end = date(today.year, 1, 1), today
    return (period, end, start) if end < start else (period, start, end)


def period_bounds(start, end):
    def midnight(d):
        return datetime(d.year, d.month, d.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    return midnight(start), midnight(end + timedelta(days=1))


def fmt_day(d):
    return f"{d:%b} {d.day}, {d.year}"


BY_RE = re.compile(r"(?:^|\s*\| )By (.+)$")


def split_note(note):
    """'invoice 9 | By Maria, Mike' -> ('invoice 9', ['Maria', 'Mike'])."""
    note = note or ""
    m = BY_RE.search(note)
    if not m:
        return note.strip(), []
    return note[:m.start()].strip(), [n.strip() for n in m.group(1).split(", ") if n.strip()]


def unique_sorted(names):
    seen = {}
    for n in names:
        if n and n.strip():
            seen.setdefault(n.strip().lower(), n.strip())
    return sorted(seen.values(), key=str.lower)


def report_info(store, start, end):
    """As-of date, who counted, the drugs counted in the period, and the finalized count (if any)."""
    lo, hi = period_bounds(start, end)
    in_period = (Transaction.store_id == store.id, Transaction.created_at >= lo, Transaction.created_at < hi)
    txs = g.db.execute(select(Transaction.created_at, Transaction.note, Transaction.reason, Transaction.ndc)
                       .where(*in_period)).all()
    sess = counts.last_finalized(g.db, store.id, lo, hi)
    sess_entries = counts.entries_for(g.db, sess.id) if sess else []
    stamps = [to_local(t[0]) for t in txs] + ([to_local(sess.finalized_at)] if sess else [])
    names = [n for t in txs for n in split_note(t[1])[1]] + [e.counted_by for e in sess_entries]
    counted = {t[3] for t in txs if t[2] == "adjusted"} | {e.ndc for e in sess_entries}
    today = datetime.now(LOCAL_TZ).date()
    if stamps:
        as_of_date = max(stamps).date()
        as_of = fmt_day(as_of_date)
    else:
        as_of_date = today
        as_of = f"{fmt_day(today)}, no scans in this period"
    return {"sess": sess, "as_of_date": as_of_date, "as_of": as_of, "line": f"Inventory count as of {as_of}",
            "names": ", ".join(unique_sorted(names)) or "not recorded", "counted": counted}


# ---- styling helpers
THIN = Side(style="thin", color="D4DDD9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEAD_FILL = PatternFill("solid", fgColor="0E5B4A")
BAND_FILL = PatternFill("solid", fgColor="F2F6F4")
BOLD = Font(bold=True)
MONEY = '"$"#,##0.00'


def style_header(ws, row, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row, c)
        cell.fill, cell.border = HEAD_FILL, BORDER
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")


def style_body(ws, row, ncols, index):
    for c in range(1, ncols + 1):
        cell = ws.cell(row, c)
        cell.border = BORDER
        if index % 2 == 1:
            cell.fill = BAND_FILL
        if cell.alignment is None or not cell.alignment.wrap_text:
            cell.alignment = Alignment(vertical="top")


def fit_widths(ws, first_row, ncols, caps, mins=None):
    """Column width from the longest value (capped); long text wraps."""
    for c in range(1, ncols + 1):
        longest = max((len(str(ws.cell(r, c).value)) for r in range(first_row, ws.max_row + 1)
                       if ws.cell(r, c).value is not None and not str(ws.cell(r, c).value).startswith("=")), default=8)
        ws.column_dimensions[get_column_letter(c)].width = max((mins or {}).get(c, 8), min(longest + 2, caps[c - 1]))


def setup_print(ws, title_row):
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_title_rows = f"{title_row}:{title_row}"
    ws.page_margins = PageMargins(left=0.5, right=0.5, top=0.5, bottom=0.5, header=0.3, footer=0.3)
    ws.oddFooter.left.text = "Page &P of &N"
    ws.oddFooter.right.text = "Printed " + datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def heading(ws, row, text, ncols, font=None):
    ws.cell(row, 1, text).font = font or BOLD
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncols)
    ws.cell(row, 1).alignment = Alignment(vertical="center", wrap_text=False)


def inventory_rows(store, info):
    """Drugs with stock, plus drugs counted in the period even if now 0, by drug name."""
    return [(inv, p) for inv, p in stock_query(store.id, "", "all")
            if inv.quantity_on_hand > 0 or p.ndc in info["counted"]]


def write_inventory_sheet(ws, store, info):
    """A1 store, A2 as-of, A3 counted by, A4 total value, table from row 6, TOTAL (value only)."""
    N = 8
    rows = inventory_rows(store, info)
    values = [round(inv.quantity_on_hand * float(p.last_unit_price or 0), 2) for inv, p in rows]
    total = round(sum(values), 2)
    heading(ws, 1, store.name, N, Font(bold=True, size=18))
    ws.row_dimensions[1].height = 30
    heading(ws, 2, info["line"], N)
    heading(ws, 3, f"Counted by: {info['names']}", N, Font())
    heading(ws, 4, f"Total inventory value: ${total:,.2f}", N)
    for c, h in enumerate(["NDC", "Drug", "Generic name", "Size", "Packages", "Unit price", "Price date", "Value"], 1):
        ws.cell(6, c, h)
    style_header(ws, 6, N)
    ws.row_dimensions[6].height = 30
    for i, ((inv, p), v) in enumerate(zip(rows, values)):
        r = 7 + i
        ws.cell(r, 1, fmt_ndc(p.ndc)).number_format = "@"           # text, keeps leading zeros
        ws.cell(r, 2, p.description).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(r, 3, p.generic_name or "").alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(r, 4, p.size or "")
        ws.cell(r, 5, inv.quantity_on_hand).number_format = "0"
        ws.cell(r, 6, float(p.last_unit_price or 0)).number_format = MONEY
        ws.cell(r, 7, p.last_invoice_date).number_format = "yyyy-mm-dd"
        ws.cell(r, 8, v).number_format = MONEY
        style_body(ws, r, N, i)
    tr = 7 + len(rows)
    ws.cell(tr, 1, "TOTAL")
    ws.cell(tr, 8, f"=SUM(H7:H{tr - 1})" if rows else 0).number_format = MONEY     # value only
    for c in range(1, N + 1):
        ws.cell(tr, c).font = BOLD
        ws.cell(tr, c).border = BORDER
    heading(ws, tr + 3, "Counted by: ____________   Verified by: ____________   Date: ________", N, Font())
    fit_widths(ws, 6, N, [16, 45, 30, 12, 11, 12, 12, 14], mins={2: 45, 3: 24})
    ws.freeze_panes = "A7"
    setup_print(ws, 6)
    return total


def write_table_sheet(ws, headings, headers, data, caps, formats=None, change_col=None, wrap=()):
    """Heading lines, then a styled table. formats: {col: number_format}; change_col: green/red numbers."""
    N = len(headers)
    for i, h in enumerate(headings, 1):
        heading(ws, i, h, N, Font(bold=True, size=18) if i == 1 else (BOLD if i == 2 else Font()))
    ws.row_dimensions[1].height = 30
    hr = len(headings) + 2
    for c, h in enumerate(headers, 1):
        ws.cell(hr, c, h)
    style_header(ws, hr, N)
    ws.row_dimensions[hr].height = 30
    for i, row in enumerate(data):
        r = hr + 1 + i
        for c, v in enumerate(row, 1):
            cell = ws.cell(r, c, v)
            if formats and c in formats:
                cell.number_format = formats[c]
            if c in wrap:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        style_body(ws, r, N, i)
        if change_col and isinstance(row[change_col - 1], (int, float)) and row[change_col - 1] != 0:
            ws.cell(r, change_col).font = Font(bold=True, color="0E5B4A" if row[change_col - 1] > 0 else "B42318")
    fit_widths(ws, hr, N, caps, mins={3: 45} if headers[2] == "Drug" else None)
    ws.freeze_panes = ws.cell(hr + 1, 1)
    setup_print(ws, hr)


def sheet_name(wb, name, suffix=""):
    """Max 31 characters, no characters Excel forbids, unique within the workbook."""
    base = re.sub(r"[\\/?*\[\]:]", " ", name).strip() or "Store"
    out, n = (base[:31 - len(suffix)].rstrip() + suffix), 2
    while out in wb.sheetnames:
        tag = f" {n}"
        out = base[:31 - len(suffix) - len(tag)].rstrip() + tag + suffix
        n += 1
    return out


TYPE_NAMES = {"received": "Received", "dispensed": "Dispensed", "adjusted": "Count", "transfer": "Transfer"}


def write_scan_log(ws, store, info, start, end):
    lo, hi = period_bounds(start, end)
    tx = g.db.execute(
        select(Transaction, Product.description, User.name)
        .join(Product, Product.ndc == Transaction.ndc).outerjoin(User, User.id == Transaction.user_id)
        .where(Transaction.store_id == store.id, Transaction.created_at >= lo, Transaction.created_at < hi)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())).all()
    data = []
    for t, d, account in tx:
        note, names = split_note(t.note)
        data.append([to_local(t.created_at).replace(tzinfo=None), fmt_ndc(t.ndc), d, t.change,
                     TYPE_NAMES.get(t.reason, t.reason), ", ".join(names) or account or "", note])
    write_table_sheet(
        ws, [store.name, f"Scan log, {fmt_day(start)} to {fmt_day(end)}", f"Counted by: {info['names']}"],
        ["Date and time", "NDC", "Drug", "Change", "Type", "Scanned by", "Note"], data,
        caps=[17, 16, 45, 9, 11, 22, 50], formats={1: "yyyy-mm-dd hh:mm", 2: "@", 4: "+0;-0;0"},
        change_col=4, wrap=(3, 7))


@app.route("/export")
def export_page():
    period, start, end = report_period()
    requested = request.args.get("store")
    if g.user.role == "owner":
        stores_list = active_stores()
        selected = "all" if requested in (None, "", "all") else str(viewable_store_id(requested))
    else:
        stores_list = []
        viewable_store_id(requested)                # another store (or "all") -> 403
        selected = str(g.user.store_id)
    return render_template("export.html", periods=PERIODS, period=period, start=start.isoformat(),
                           end=end.isoformat(), stores_list=stores_list, selected=selected)


@app.route("/export/inventory-report")
def export_inventory_report():
    period, start, end = report_period()
    today = datetime.now(LOCAL_TZ).date()
    wb = Workbook()
    wb.remove(wb.active)
    if request.args.get("store") == "all" and g.user.role == "owner":
        summary = wb.create_sheet("Summary")
        stores = active_stores()
        heading(summary, 1, "Inventory count, all stores", 4, Font(bold=True, size=18))
        summary.row_dimensions[1].height = 30
        heading(summary, 2, f"Period: {fmt_day(start)} to {fmt_day(end)}", 4, Font())
        for c, h in enumerate(["Store", "Inventory count as of", "Counted by", "Total inventory value"], 1):
            summary.cell(3, c, h)
        style_header(summary, 3, 4)
        for i, st in enumerate(stores):
            info = report_info(st, start, end)
            total = write_inventory_sheet(wb.create_sheet(sheet_name(wb, st.name, " count")), st, info)
            write_scan_log(wb.create_sheet(sheet_name(wb, st.name, " log")), st, info, start, end)
            r = 4 + i
            summary.cell(r, 1, st.name)
            summary.cell(r, 2, info["as_of"])
            summary.cell(r, 3, info["names"]).alignment = Alignment(wrap_text=True, vertical="top")
            summary.cell(r, 4, total).number_format = MONEY
            style_body(summary, r, 4, i)
        tr = 4 + len(stores)
        summary.cell(tr, 1, "TOTAL")
        summary.cell(tr, 4, f"=SUM(D4:D{tr - 1})" if stores else 0).number_format = MONEY      # value only
        for c in range(1, 5):
            summary.cell(tr, c).font, summary.cell(tr, c).border = BOLD, BORDER
        fit_widths(summary, 3, 4, [34, 40, 40, 22])
        summary.freeze_panes = "A4"
        setup_print(summary, 3)
        wb.move_sheet("Summary", offset=-(len(wb.sheetnames) - 1))
        filename = f"inventory-all-stores-{today.isoformat()}.xlsx"
    else:
        sid = viewable_store_id(request.args.get("store"))
        store = g.db.get(Store, sid) if sid else None
        if not store:
            abort(404)
        info = report_info(store, start, end)
        write_inventory_sheet(wb.create_sheet("Inventory count"), store, info)
        write_scan_log(wb.create_sheet("Scan log"), store, info, start, end)
        if info["sess"]:
            entries = g.db.execute(
                select(CountEntry, Product.description).join(Product, Product.ndc == CountEntry.ndc)
                .where(CountEntry.session_id == info["sess"].id, CountEntry.deleted_at.is_(None))
                .order_by(CountEntry.created_at, CountEntry.id)).all()
            write_table_sheet(
                wb.create_sheet("Count entries"),
                [store.name, f"Count entries, count finalized {fmt_date(info['sess'].finalized_at)}"],
                ["Time", "NDC", "Drug", "Qty", "Counted by", "Location", "Note"],
                [[to_local(e.created_at).replace(tzinfo=None), fmt_ndc(e.ndc), d, e.qty, e.counted_by,
                  e.location or "", e.edit_note or ""] for e, d in entries],
                caps=[17, 16, 45, 8, 20, 20, 40], formats={1: "yyyy-mm-dd hh:mm", 2: "@"}, wrap=(3,))
        filename = f"inventory-{slug(store.name)}-{info['as_of_date'].isoformat()}.xlsx"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.errorhandler(403)
def forbidden(e):
    return render_template("message.html", title="Not available",
                           text="Your account doesn't have access to this page."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("message.html", title="Not found",
                           text="That page or record doesn't exist."), 404


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=int(os.getenv("PORT", 5000)))
