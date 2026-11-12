"""
Quotation Manager - Turbo Prime Pte Ltd
---------------------------------------------------
Run:   python app.py
Open:  http://127.0.0.1:5000
"""
import hashlib
import io
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from xml.sax.saxutils import escape

import secrets
import time
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether, NextPageTemplate,
                                PageTemplate, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "quotations.db")

DO_BANNER = "Ship Spares in Transit"     # fixed bold line on the delivery order, packing list and commercial invoice

# Delivery order numbers look like DO 27-0001 (DO <year>-<running no>).
DO_PREFIX = "DO "

# Tax invoice numbers look like INV 27-0001 (INV <year>-<running no>).
INV_PREFIX = "INV "
# Purchase order numbers look like PO 2701 (PO <year><running no>).
PO_PREFIX = "PO "
JO_PREFIX = "JO "
QUOTE_PREFIX = "Q"
# Numbering of the new series: Q270001, JO 27-0001, PO 2701, DO 27-0001, INV 27-0001. The two digits after
# the prefix are the year; the series started in 2027, so documents made before then also get 27 and
# the year rolls over to 28 in 2028 (the running number restarts at 1). Numbers can always be edited by hand.
SERIES_START_YY = 27


def series_yy(year=None):
    return max(int(year or date.today().year) % 100, SERIES_START_YY)
# Currencies offered on quotations and purchase orders (EUR = Euro).
CURRENCIES = ["JPY", "SGD", "INR", "EUR", "USD"]
PO_CURRENCIES = CURRENCIES

# Standard Terms & Conditions. Pre-filled on every new quotation; editable per quotation.
# {delivery_term} and {company} depend on which company is selected (see COMPANIES below).
STANDARD_TERMS = [
    "Currency : {currency}",
    "Delivery time: {delivery}",
    "Delivery term: {delivery_term}",
    "Payment terms: 30 days from the date of invoice",
    "GST will be additional as applicable",
    "Liability is limited to our invoice value.",
    "Ordrer can not be cancelled after your confirmation/PO.",
    "Packing & delivery chargers will be additional",
    "{company}'s standard terms & conditions apply",
]

# The Terms & Conditions options offered on a quotation (Onboard job, Workshop job, Genuine spares,
# Compatible spares). They are copied into the database
# the first time the app starts and can then be edited by an administrator on the "T&C Options"
# page (the wording below is only the starting text). {currency}, {delivery}, {delivery_term} and
# {company} are filled in when an option is picked on a quotation.
TERMS_OPTIONS = [
    {"key": "onboard", "name": "Onboard job", "body": "\n".join([
        "Validity: 30 days",
        "Currency : {currency}",
        "Payment terms: 30 days from the date of invoice.",
        "TC Special tools, spares and crane to be provided by vessel.",
        "Owners/Agents to arrange for passenger launch and cargo boat (for anchorage jobs)",
        "Owners/agents to arrange shipyard entry permission, yard crane, surcharge and customs clearance if any.",
        "Collection/delivery/clearance charges for TC/rotor from boat terminal to our workshop & back will be additional",
        "If additional spares or services required then same will be informed to you for your approval.",
        "Warranty: Six months from the date of completion of job if both service and spares are ordered from us.",
        "Operation and maintenance of the TC to be done properly as per maker instructions.",
        "Additional charges will be applicable for sea trial , waiting time and OPL jobs on case to case basis",
        "Liability is limited to our invoice value.",
        "GST will be additional, if applicable",
        "{company}'s standard terms & conditions apply",
    ])},
    {"key": "workshop", "name": "Workshop job", "body": "\n".join([
        "Validity: 30 days",
        "Currency : {currency}",
        "Delivery term: {delivery_term}",
        "Payment terms: 30 days from the date of invoice.",
        "Quotation for spares required & additional services (if any) will be provided after the inspection at our workshop",
        "Collection and delivery charges will be additional",
        "Warranty: Six months from the date of completion of job if both service and spares are ordered from us.",
        "Operation and maintenance of the TC to be done properly as per maker instructions.",
        "Liability is limited to our invoice value.",
        "GST will be additional, if applicable",
        "{company}'s standard terms & conditions apply",
    ])},
    {"key": "genuine", "name": "Genuine spares", "body": "\n".join([
        "Quoted Spares are Genuine",
        "Validity: 30 days",
        "Currency : {currency}",
        "Delivery time: 3 to 4 days from the Date of Order, Subject unsold",
        "Delivery term: {delivery_term}",
        "Payment terms: 30 days from the date of invoice, subject unsold",
        "Warranty: Six months from the date of dispatch. Installation, operation",
        "and maintenance of the TC to be done properly as per maker instructions",
        "Liability is limited to our invoice value.",
        "Freight, Packing & delivery chargers will be additional",
        "GST will be additional, if applicable",
        "{company}'s standard terms & conditions apply",
    ])},
    {"key": "compatible", "name": "Compatible spares", "body": "\n".join([
        "Quoted Spares are high quality compatible spares",
        "Validity: 30 days",
        "Currency : {currency}",
        "Delivery time: 3 to 4 days from the Date of Order, Subject unsold",
        "Delivery term: {delivery_term}",
        "Payment terms: 30 days from the date of invoice, subject unsold",
        "Warranty: Six months from the date of dispatch. Installation, operation",
        "and maintenance of the TC to be done properly as per maker instructions",
        "Liability is limited to our invoice value.",
        "Freight, Packing & delivery chargers will be additional",
        "GST will be additional, if applicable",
        "{company}'s standard terms & conditions apply",
    ])},
]
# Bump this when TERMS_OPTIONS is re-issued: the database copies are replaced once, at start-up.
TERMS_VERSION = "2026-11-08b"

# The two companies the user can choose between after logging in. The chosen company decides
# the header (image in static/) on every PDF, the default currency and terms of new quotations,
# and which documents are listed. Each quotation remembers its company, and the job order,
# delivery order and tax invoice made from it use the same header.
COMPANIES = {
    "pte": {
        "key": "pte",
        "name": "Turbo Prime Pte Ltd",
        "short": "Turbo Prime Pte Ltd",
        "legal": "Turbo Prime Pte Ltd",
        "header": "header_pte.png",
        "currency": "SGD",
        "delivery_term": "Exw Singapore",
        "terms_name": "Turbo Prime Pte Ltd",
        "default_terms": "compatible",
        "bank_default": ("Turbo Prime Pte Ltd\nOCBC Bank, OCBC Centre Branch\nSingapore 049513\n"
                         "SWIFT Code: \nSGD A/C NO : "),
    },
}
# Bank details per currency, for companies that bank in several currencies. The text below is only
# the starting text: an administrator edits it on the Bank Details page. A new tax invoice starts with the set
# of its own currency (other currencies use the company's general details); it can be changed on the invoice.
def _ocbc(cur, acc):
    return ("Turbo Prime Pte Ltd\nOCBC Bank, OCBC Centre Branch\nSingapore 049513\n"
            f"SWIFT Code: OCBCSGSG\n{cur} A/C NO : {acc}")
CURRENCY_BANKS = {
    "pte": [("USD", _ocbc("USD", "601595176201")),
            ("SGD", _ocbc("SGD", "595312786001")),
            ("JPY", _ocbc("JPY", "601595176201"))],
}

DEFAULT_COMPANY = "pte"        # this application is used for Turbo Prime Pte Ltd only


def co(key):
    return COMPANIES.get(key) or COMPANIES[DEFAULT_COMPANY]


def resolve_terms(body, company=DEFAULT_COMPANY, currency=None, delivery=None):
    """Fill the placeholders of a T&C option. {delivery_term} and {company} depend on the company;
    {currency} / {delivery} are left in place when not given (the quotation form fills them in)."""
    c = co(company)
    out = body.replace("{delivery_term}", c["delivery_term"]).replace("{company}", c["terms_name"])
    if currency is not None:
        out = out.replace("{currency}", currency)
    if delivery is not None:
        out = out.replace("{delivery}", delivery)
    return out


def default_terms(currency="INR", delivery="2- 3 days", company=DEFAULT_COMPANY, body=None):
    """Terms text for a company (the Standard option unless `body` is given), fully filled in."""
    return resolve_terms(body if body is not None else "\n".join(STANDARD_TERMS), company, currency, delivery)


def terms_options(db):
    return db.execute("SELECT * FROM terms_options ORDER BY sort_order, key").fetchall()


def default_terms_for(db, company_key, currency):
    """Terms a NEW quotation of this company starts with (its default T&C option)."""
    key = co(company_key).get("default_terms", "standard")
    row = db.execute("SELECT body FROM terms_options WHERE key = ?", (key,)).fetchone()
    return default_terms(currency, "3 to 4 days from the Date of Order, Subject unsold",
                         company=company_key, body=row["body"] if row else None)


app = Flask(__name__)
# Optional: on a new server, set APP_PASSWORD (and ADMIN_USER) so the first administrator is
# created automatically and the one-time /setup page is never exposed.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")


def _secret_key():
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = os.path.join(DATA_DIR, "secret.key")      # generated once, kept with the database
    try:
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=14))

PUBLIC_ENDPOINTS = {"login", "setup", "static", "brand_logo"}
COMPANY_FREE_ENDPOINTS = {"company_select", "logout", "static"}
_FAILED_LOGINS = {}                      # "ip|user" -> [timestamps]; 5 failures = 10 min lock
_DUMMY_HASH = generate_password_hash("not-a-real-password")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,30}$")


def user_count(db):
    return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def safe_next(nxt):
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else url_for("index")


@app.before_request
def require_login():
    g.user = None
    if request.endpoint is None or request.endpoint == "static":
        return None
    if request.method == "POST":          # basic CSRF guard: form must come from this site
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin and urlparse(origin).netloc != request.host:
            abort(403)
    db = get_db()
    uid = session.get("uid")
    if uid:
        g.user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if g.user is None:                # account was deleted
            session.clear()
    if g.user:
        session["company"] = DEFAULT_COMPANY          # only one company: no choice to make
        return None
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if user_count(db) == 0:
        return redirect(url_for("setup"))
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not (g.user and g.user["is_admin"]):
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def password_problem(pw, confirm):
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if pw != confirm:
        return "The two passwords do not match."
    return None


def _throttle_key(name):
    return f"{request.remote_addr}|{name.lower()}"


def _locked(key):
    now = time.time()
    recent = [t for t in _FAILED_LOGINS.get(key, []) if now - t < 600]
    _FAILED_LOGINS[key] = recent
    return len(recent) >= 5


@app.route("/login", methods=["GET", "POST"])
def login():
    db = get_db()
    if user_count(db) == 0:
        return redirect(url_for("setup"))
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        key = _throttle_key(name)
        if _locked(key):
            flash("Too many wrong attempts. Please wait 10 minutes and try again.", "err")
        else:
            u = db.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (name,)).fetchone()
            ok = check_password_hash(u["pw_hash"] if u else _DUMMY_HASH, pw) and u is not None
            if ok:
                session.clear()
                session["uid"] = u["id"]
                session.permanent = True
                _FAILED_LOGINS.pop(key, None)
                return redirect(safe_next(request.args.get("next", "")))
            _FAILED_LOGINS.setdefault(key, []).append(time.time())
            flash("Wrong username or password.", "err")
    return render_template("login.html")


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """One-time creation of the first administrator (only while there are no users)."""
    db = get_db()
    if user_count(db) > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        err = None
        if not USERNAME_RE.match(name):
            err = "Username: 3-30 letters, digits, dot, dash or underscore."
        else:
            err = password_problem(request.form.get("password", ""), request.form.get("confirm", ""))
        if err:
            flash(err, "err")
        else:
            cur = db.execute("INSERT INTO users (username, pw_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
                             (name, generate_password_hash(request.form["password"]),
                              datetime.now().isoformat(timespec="seconds")))
            db.commit()
            session.clear()
            session["uid"] = cur.lastrowid
            session.permanent = True
            flash("Administrator account created. Welcome!", "ok")
            return redirect(url_for("index"))
    return render_template("setup.html")


def active_company():
    return co(session.get("company"))


def flow_for(quotation_id, current):
    """The document chain Quotation -> Job Order -> Delivery Order -> Tax Invoice
    for one quotation: what exists, how many, and where each step can be opened."""
    db = get_db()
    q = db.execute("SELECT id, quote_no FROM quotations WHERE id = ?", (quotation_id,)).fetchone()
    jo = db.execute("SELECT id, jo_no FROM job_orders WHERE quotation_id = ?", (quotation_id,)).fetchone()
    n_do = n_inv = 0
    if jo:
        n_do = db.execute("SELECT COUNT(*) FROM delivery_orders WHERE job_id = ?", (jo["id"],)).fetchone()[0]
        n_inv = db.execute("SELECT COUNT(*) FROM invoices i JOIN delivery_orders d ON d.id = i.delivery_id "
                           "WHERE d.job_id = ?", (jo["id"],)).fetchone()[0]

    def count_text(n):
        return f"{n} created" if n else "none yet"

    job_url = url_for("job_view", jid=jo["id"]) if jo else None
    steps = [
        {"key": "quote", "label": "Quotation", "sub": str(q["quote_no"]) if q else "",
         "url": url_for("view", qid=quotation_id), "done": True},
        {"key": "job", "label": "Job Order", "sub": jo["jo_no"] if jo else "not created yet",
         "url": job_url, "done": bool(jo)},
        {"key": "do", "label": "Delivery Order", "sub": count_text(n_do) if jo else "-",
         "url": (url_for("deliveries", q=jo["jo_no"]) if n_do else job_url + "#delivery-orders") if jo else None,
         "done": n_do > 0},
        {"key": "inv", "label": "Tax Invoice", "sub": count_text(n_inv) if jo else "-",
         "url": url_for("invoices", q=jo["jo_no"]) if jo and n_inv else None, "done": n_inv > 0},
    ]
    for st in steps:
        st["cur"] = st["key"] == current
    return steps


@app.context_processor
def inject_company():
    return {"company": active_company(), "companies": COMPANIES, "flow_for": flow_for}


@app.route("/company", methods=["GET", "POST"])
def company_select():
    """Shown after every login: choose which company's documents to work on."""
    session["company"] = DEFAULT_COMPANY
    return redirect(safe_next(request.values.get("next", "")))


@app.errorhandler(Exception)
def unexpected_error(e):
    """Instead of the bare 'Internal Server Error' page: log the full traceback (server log) and show
    the reason on screen to logged-in users, so a problem can be diagnosed from a screenshot."""
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Unhandled error on %s %s", request.method, request.path)
    try:
        get_db().rollback()
    except Exception:
        pass
    detail = f"{type(e).__name__}: {e}" if g.get("user") else ""
    try:
        return render_template("error.html", detail=detail, where=f"{request.method} {request.path}"), 500
    except Exception:       # even the normal page layout failed - fall back to plain HTML
        return (f"<h2>Something went wrong</h2><p>{escape(detail)}</p><p><a href='/'>Back to home</a></p>"), 500


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
def account():
    db = get_db()
    if request.method == "POST":
        if not check_password_hash(g.user["pw_hash"], request.form.get("current", "")):
            flash("Current password is wrong.", "err")
        else:
            err = password_problem(request.form.get("password", ""), request.form.get("confirm", ""))
            if err:
                flash(err, "err")
            else:
                db.execute("UPDATE users SET pw_hash = ? WHERE id = ?",
                           (generate_password_hash(request.form["password"]), g.user["id"]))
                db.commit()
                flash("Password changed.", "ok")
                return redirect(url_for("account"))
    return render_template("account.html")


def _admin_count(db):
    return db.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]


@app.route("/users")
@admin_required
def users():
    rows = get_db().execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()
    return render_template("users.html", rows=rows)


@app.route("/users/add", methods=["POST"])
@admin_required
def users_add():
    db = get_db()
    name = request.form.get("username", "").strip()
    err = None
    if not USERNAME_RE.match(name):
        err = "Username: 3-30 letters, digits, dot, dash or underscore."
    elif db.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (name,)).fetchone():
        err = f"User {name} already exists."
    else:
        err = password_problem(request.form.get("password", ""), request.form.get("password", ""))
    if err:
        flash(err, "err")
    else:
        db.execute("INSERT INTO users (username, pw_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
                   (name, generate_password_hash(request.form["password"]),
                    1 if request.form.get("is_admin") else 0,
                    datetime.now().isoformat(timespec="seconds")))
        db.commit()
        flash(f"User {name} added.", "ok")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/password", methods=["POST"])
@admin_required
def users_password(uid):
    db = get_db()
    err = password_problem(request.form.get("password", ""), request.form.get("password", ""))
    if not db.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
        abort(404)
    if err:
        flash(err, "err")
    else:
        db.execute("UPDATE users SET pw_hash = ? WHERE id = ?",
                   (generate_password_hash(request.form["password"]), uid))
        db.commit()
        flash("Password updated.", "ok")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/admin", methods=["POST"])
@admin_required
def users_admin(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone() or abort(404)
    if u["is_admin"] and _admin_count(db) <= 1:
        flash("There must be at least one administrator.", "err")
    else:
        db.execute("UPDATE users SET is_admin = ? WHERE id = ?", (0 if u["is_admin"] else 1, uid))
        db.commit()
        flash("Role updated.", "ok")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/delete", methods=["POST"])
@admin_required
def users_delete(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone() or abort(404)
    if uid == g.user["id"]:
        flash("You cannot delete your own account.", "err")
    elif u["is_admin"] and _admin_count(db) <= 1:
        flash("There must be at least one administrator.", "err")
    else:
        db.execute("DELETE FROM users WHERE id = ?", (uid,))
        db.commit()
        flash(f"User {u['username']} deleted.", "ok")
    return redirect(url_for("users"))


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash     TEXT NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quotations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_no        INTEGER NOT NULL UNIQUE,
    to_address      TEXT NOT NULL,
    q_date          TEXT NOT NULL,
    valid_till      TEXT NOT NULL,
    your_ref        TEXT DEFAULT '',
    tc_type         TEXT DEFAULT '',
    currency        TEXT NOT NULL DEFAULT 'INR',
    delivery_time   TEXT NOT NULL DEFAULT '2- 3 days',
    remarks         TEXT DEFAULT '',
    discount_type   TEXT NOT NULL DEFAULT 'percent',
    discount_value  TEXT NOT NULL DEFAULT '0',
    subtotal        TEXT NOT NULL,
    discount_amt    TEXT NOT NULL,
    total           TEXT NOT NULL,
    terms           TEXT NOT NULL DEFAULT '',
    company         TEXT NOT NULL DEFAULT 'pte',
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    address  TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS tc_types (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    quotation_id  INTEGER NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
    sno           INTEGER NOT NULL,
    description   TEXT NOT NULL,
    qty           TEXT NOT NULL,
    unit_price    TEXT NOT NULL,
    line_total    TEXT NOT NULL
);
"""

# Job orders keep their OWN copy of the items / remarks / discount (copied from the quotation
# when the job order is created) so they can be edited without touching the quotation.
JOB_DDL = """
CREATE TABLE IF NOT EXISTS job_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    jo_no           TEXT NOT NULL UNIQUE,
    quotation_id    INTEGER NOT NULL UNIQUE REFERENCES quotations(id) ON DELETE RESTRICT,
    jo_date         TEXT NOT NULL,
    po_number       TEXT NOT NULL DEFAULT '',
    remarks         TEXT NOT NULL DEFAULT '',
    discount_type   TEXT NOT NULL DEFAULT 'percent',
    discount_value  TEXT NOT NULL DEFAULT '0',
    subtotal        TEXT NOT NULL DEFAULT '0',
    discount_amt    TEXT NOT NULL DEFAULT '0',
    total           TEXT NOT NULL DEFAULT '0',
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL REFERENCES job_orders(id) ON DELETE CASCADE,
    sno           INTEGER NOT NULL,
    description   TEXT NOT NULL,
    qty           TEXT NOT NULL,
    unit_price    TEXT NOT NULL,
    line_total    TEXT NOT NULL
);
"""

# Delivery orders keep their OWN copy of the items (copied from the job order when created),
# so descriptions / quantities can be changed when some items are not available.
DO_DDL = """
CREATE TABLE IF NOT EXISTS delivery_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    do_no           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    job_id          INTEGER NOT NULL REFERENCES job_orders(id) ON DELETE RESTRICT,
    do_date         TEXT NOT NULL,
    po_number       TEXT NOT NULL DEFAULT '',
    shipper_name    TEXT NOT NULL DEFAULT '',
    vessel_name     TEXT NOT NULL DEFAULT '',
    remarks         TEXT NOT NULL DEFAULT '',
    discount_type   TEXT NOT NULL DEFAULT 'percent',
    discount_value  TEXT NOT NULL DEFAULT '0',
    subtotal        TEXT NOT NULL DEFAULT '0',
    discount_amt    TEXT NOT NULL DEFAULT '0',
    total           TEXT NOT NULL DEFAULT '0',
    created_at      TEXT NOT NULL,
    to_address      TEXT,
    tc_type         TEXT
);
CREATE TABLE IF NOT EXISTS delivery_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id  INTEGER NOT NULL REFERENCES delivery_orders(id) ON DELETE CASCADE,
    sno          INTEGER NOT NULL,
    description  TEXT NOT NULL,
    part_no      TEXT NOT NULL DEFAULT '',
    qty          TEXT NOT NULL,
    unit_price   TEXT NOT NULL DEFAULT '0',
    line_total   TEXT NOT NULL DEFAULT '0'
);
"""

# Tax invoices are made from a delivery order. They keep their OWN copy of the items (with part
# numbers and prices) and their own bank details, so everything can be edited on the invoice.
INV_DDL = """
CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    inv_no          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    delivery_id     INTEGER NOT NULL REFERENCES delivery_orders(id) ON DELETE RESTRICT,
    inv_date        TEXT NOT NULL,
    po_number       TEXT NOT NULL DEFAULT '',
    vessel_name     TEXT NOT NULL DEFAULT '',
    pay_terms       TEXT NOT NULL DEFAULT '',
    location        TEXT NOT NULL DEFAULT '',
    discount_type   TEXT NOT NULL DEFAULT 'percent',
    discount_value  TEXT NOT NULL DEFAULT '0',
    subtotal        TEXT NOT NULL DEFAULT '0',
    discount_amt    TEXT NOT NULL DEFAULT '0',
    gst_rate        TEXT NOT NULL DEFAULT '0',
    gst_amt         TEXT NOT NULL DEFAULT '0',
    total           TEXT NOT NULL DEFAULT '0',
    bank_details    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoice_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    sno         INTEGER NOT NULL,
    description TEXT NOT NULL,
    part_no     TEXT NOT NULL DEFAULT '',
    qty         TEXT NOT NULL,
    unit_price  TEXT NOT NULL DEFAULT '0',
    line_total  TEXT NOT NULL DEFAULT '0'
);
-- saved supplier addresses ("To" of purchase orders), editable on the Saved page
CREATE TABLE IF NOT EXISTS suppliers (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    address  TEXT NOT NULL UNIQUE
);
-- the three Terms & Conditions options offered on a quotation (edited by an administrator)
CREATE TABLE IF NOT EXISTS terms_options (
    key         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    body        TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);
-- bank details per company and currency (copied onto tax invoices)
CREATE TABLE IF NOT EXISTS company_bank_cur (
    company       TEXT NOT NULL,
    currency      TEXT NOT NULL,
    bank_details  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (company, currency)
);
-- editable default bank details, one per company (copied onto each new tax invoice)
CREATE TABLE IF NOT EXISTS company_bank (
    company       TEXT PRIMARY KEY,
    bank_details  TEXT NOT NULL DEFAULT ''
);
"""

EXPORT_DDL = """
-- commercial invoices and packing lists (stand-alone documents, like purchase orders)
CREATE TABLE IF NOT EXISTS commercial_invoices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ci_no         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    ci_date       TEXT NOT NULL,
    to_address    TEXT NOT NULL,
    shipper_name  TEXT NOT NULL DEFAULT '',
    tc_type       TEXT NOT NULL DEFAULT '',
    vessel_name   TEXT NOT NULL DEFAULT '',
    po_no         TEXT NOT NULL DEFAULT '',
    currency      TEXT NOT NULL DEFAULT 'SGD',
    total         TEXT NOT NULL DEFAULT '0',
    dimensions    TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT 'pte',
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ci_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ci_id       INTEGER NOT NULL REFERENCES commercial_invoices(id) ON DELETE CASCADE,
    sno         INTEGER NOT NULL,
    description TEXT NOT NULL,
    part_no     TEXT NOT NULL DEFAULT '',
    qty         TEXT NOT NULL,
    unit_price  TEXT NOT NULL DEFAULT '0',
    line_total  TEXT NOT NULL DEFAULT '0'
);
CREATE TABLE IF NOT EXISTS packing_lists (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pl_no         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pl_date       TEXT NOT NULL,
    to_address    TEXT NOT NULL,
    shipper_name  TEXT NOT NULL DEFAULT '',
    tc_type       TEXT NOT NULL DEFAULT '',
    vessel_name   TEXT NOT NULL DEFAULT '',
    po_no         TEXT NOT NULL DEFAULT '',
    dimensions    TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT 'pte',
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pl_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pl_id       INTEGER NOT NULL REFERENCES packing_lists(id) ON DELETE CASCADE,
    sno         INTEGER NOT NULL,
    description TEXT NOT NULL,
    part_no     TEXT NOT NULL DEFAULT '',
    qty         TEXT NOT NULL
);
"""

PO_DDL = """
-- purchase orders (we buy from a supplier for a job order)
CREATE TABLE IF NOT EXISTS purchase_orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    po_no       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    job_id      INTEGER REFERENCES job_orders(id) ON DELETE RESTRICT,   -- empty for a stand-alone PO
    po_date     TEXT NOT NULL,
    to_address  TEXT NOT NULL,
    your_ref    TEXT NOT NULL DEFAULT '',
    jo_ref      TEXT NOT NULL DEFAULT '',
    tc_type     TEXT NOT NULL DEFAULT '',
    currency    TEXT NOT NULL DEFAULT 'SGD',
    subtotal    TEXT NOT NULL DEFAULT '0',
    gst_rate    TEXT NOT NULL DEFAULT '0',
    gst_amt     TEXT NOT NULL DEFAULT '0',
    total       TEXT NOT NULL DEFAULT '0',
    created_at  TEXT NOT NULL,
    company     TEXT NOT NULL DEFAULT '',
    remarks     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS po_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id       INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    sno         INTEGER NOT NULL,
    description TEXT NOT NULL,
    part_no     TEXT NOT NULL DEFAULT '',
    qty         TEXT NOT NULL,
    unit_price  TEXT NOT NULL DEFAULT '0',
    line_total  TEXT NOT NULL DEFAULT '0'
);
"""



def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)
        # upgrade older databases: add the terms column and fill it for existing quotations
        cols = [r[1] for r in db.execute("PRAGMA table_info(quotations)")]
        if "terms" not in cols:
            db.execute("ALTER TABLE quotations ADD COLUMN terms TEXT NOT NULL DEFAULT ''")
        for qid, cur, dlv in db.execute(
                "SELECT id, currency, delivery_time FROM quotations WHERE terms = ''").fetchall():
            db.execute("UPDATE quotations SET terms = ? WHERE id = ?",
                       (default_terms(cur, dlv), qid))
        # upgrade job orders from the first version (fixed year/sequence columns, no own items)
        jo_cols = [r[1] for r in db.execute("PRAGMA table_info(job_orders)")]
        old_job_table = "jo_year" in jo_cols
        if old_job_table:
            db.execute("ALTER TABLE job_orders RENAME TO job_orders_old")
        db.executescript(JOB_DDL)
        if old_job_table:
            db.execute("""
                INSERT INTO job_orders (id, jo_no, quotation_id, jo_date, po_number, remarks,
                                        discount_type, discount_value, subtotal, discount_amt,
                                        total, created_at)
                SELECT o.id, o.jo_no, o.quotation_id, o.jo_date, o.po_number,
                       COALESCE(q.remarks, ''), q.discount_type, q.discount_value, q.subtotal,
                       q.discount_amt, q.total, o.created_at
                FROM job_orders_old o JOIN quotations q ON q.id = o.quotation_id""")
            db.execute("DROP TABLE job_orders_old")
        db.execute("""
            INSERT INTO job_items (job_id, sno, description, qty, unit_price, line_total)
            SELECT j.id, i.sno, i.description, i.qty, i.unit_price, i.line_total
            FROM job_orders j JOIN items i ON i.quotation_id = j.quotation_id
            WHERE NOT EXISTS (SELECT 1 FROM job_items x WHERE x.job_id = j.id)
            ORDER BY j.id, i.sno""")
        db.executescript(DO_DDL)
        # upgrade delivery orders made by the first version (no part no / shipper / vessel)
        for table, col in (("delivery_orders", "shipper_name"), ("delivery_orders", "vessel_name"),
                           ("delivery_items", "part_no")):
            if col not in [r[1] for r in db.execute(f"PRAGMA table_info({table})")]:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        # company column (this application is used for Turbo Prime Pte Ltd only)
        if "company" not in [r[1] for r in db.execute("PRAGMA table_info(quotations)")]:
            db.execute("ALTER TABLE quotations ADD COLUMN company TEXT NOT NULL DEFAULT 'pte'")
        # tax invoices + editable bank details
        db.executescript(INV_DDL)
        db.executescript(PO_DDL)
        db.executescript(EXPORT_DDL)
        po_cols = [r[1] for r in db.execute("PRAGMA table_info(purchase_orders)")]
        if "jo_ref" not in po_cols:
            db.execute("ALTER TABLE purchase_orders ADD COLUMN jo_ref TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE purchase_orders ADD COLUMN tc_type TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE purchase_orders SET "
                       "jo_ref = (SELECT j.jo_no FROM job_orders j WHERE j.id = purchase_orders.job_id), "
                       "tc_type = (SELECT q.tc_type FROM job_orders j JOIN quotations q ON q.id = j.quotation_id "
                       "WHERE j.id = purchase_orders.job_id)")
        # purchase orders now have a Part No per item and no GST (Total = sum of Quantity x Unit Price)
        if "part_no" not in [r[1] for r in db.execute("PRAGMA table_info(po_items)")]:
            db.execute("ALTER TABLE po_items ADD COLUMN part_no TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE purchase_orders SET total = subtotal, gst_rate = '0', gst_amt = '0'")
        # latest templates: Part Num on quotation / job order items, Vessel Name on quotations and
        # job orders, and the T&C option a quotation was made with
        for table, col, ddl in (("items", "part_no", "TEXT NOT NULL DEFAULT ''"),
                                ("job_items", "part_no", "TEXT NOT NULL DEFAULT ''"),
                                ("quotations", "vessel_name", "TEXT NOT NULL DEFAULT ''"),
                                ("quotations", "terms_type", "TEXT NOT NULL DEFAULT ''"),
                                ("job_orders", "vessel_name", "TEXT NOT NULL DEFAULT ''"),
                                ("invoices", "bank_currency", "TEXT NOT NULL DEFAULT ''"),
                                ("invoices", "to_address", "TEXT NOT NULL DEFAULT ''"),
                                # a delivery order's own editable To address / TC type (NULL = the quotation's)
                                ("delivery_orders", "to_address", "TEXT"),
                                ("delivery_orders", "tc_type", "TEXT"),
                                ("purchase_orders", "remarks", "TEXT NOT NULL DEFAULT ''")):
            if col not in [r[1] for r in db.execute(f"PRAGMA table_info({table})")]:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        # purchase orders can now stand alone (no job order) and carry their own company
        pcols = {r[1]: r for r in db.execute("PRAGMA table_info(purchase_orders)")}
        if pcols["job_id"][3] == 1:                      # job_id was NOT NULL -> rebuild the table
            db.commit()
            db.execute("CREATE TEMP TABLE po_items_keep AS SELECT * FROM po_items")
            db.execute("DROP TABLE po_items")
            db.execute("ALTER TABLE purchase_orders RENAME TO purchase_orders_old")
            db.execute("DROP INDEX IF EXISTS idx_po_job")
            db.executescript(PO_DDL)
            has_company = "company" in pcols
            db.execute("INSERT INTO purchase_orders (id, po_no, job_id, po_date, to_address, your_ref, jo_ref, "
                       "tc_type, currency, subtotal, gst_rate, gst_amt, total, created_at, company) "
                       "SELECT id, po_no, job_id, po_date, to_address, your_ref, jo_ref, tc_type, currency, "
                       "subtotal, gst_rate, gst_amt, total, created_at, " +
                       ("company" if has_company else "''") + " FROM purchase_orders_old")
            db.execute("DROP TABLE purchase_orders_old")
            db.execute("INSERT INTO po_items (id, po_id, sno, description, part_no, qty, unit_price, line_total) "
                       "SELECT id, po_id, sno, description, part_no, qty, unit_price, line_total FROM po_items_keep")
            db.execute("DROP TABLE po_items_keep")
        elif "company" not in pcols:
            db.execute("ALTER TABLE purchase_orders ADD COLUMN company TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE purchase_orders SET company = COALESCE((SELECT q.company FROM job_orders j "
                   "JOIN quotations q ON q.id = j.quotation_id WHERE j.id = purchase_orders.job_id), 'pte') "
                   "WHERE company = ''")
        # tax invoices keep their own editable "To" address (older ones copy the quotation's)
        db.execute("UPDATE invoices SET to_address = COALESCE((SELECT q.to_address FROM delivery_orders d "
                   "JOIN job_orders j ON j.id = d.job_id JOIN quotations q ON q.id = j.quotation_id "
                   "WHERE d.id = invoices.delivery_id), '') WHERE to_address = ''")
        db.execute("CREATE TABLE IF NOT EXISTS letterhead (k TEXT PRIMARY KEY, v TEXT NOT NULL DEFAULT '')")
        db.execute("CREATE TABLE IF NOT EXISTS app_meta (k TEXT PRIMARY KEY, v TEXT)")
        if not db.execute("SELECT 1 FROM app_meta WHERE k = 'suppliers_merged'").fetchone():
            for (addr,) in db.execute("SELECT address FROM suppliers ORDER BY id").fetchall():
                remember_address(db, addr)         # one saved-address list for every document
            db.execute("INSERT INTO app_meta (k, v) VALUES ('suppliers_merged', '1')")
        done = db.execute("SELECT v FROM app_meta WHERE k = 'terms_version'").fetchone()
        if not done or done[0] != TERMS_VERSION:      # (re)issue the T&C options once
            db.execute("DELETE FROM terms_options")
            for n, t in enumerate(TERMS_OPTIONS):
                db.execute("INSERT INTO terms_options (key, name, body, sort_order) VALUES (?, ?, ?, ?)",
                           (t["key"], t["name"], t["body"], n))
            db.execute("INSERT OR REPLACE INTO app_meta (k, v) VALUES ('terms_version', ?)", (TERMS_VERSION,))
        for key, c in COMPANIES.items():
            db.execute("INSERT OR IGNORE INTO company_bank (company, bank_details) VALUES (?, ?)",
                       (key, c["bank_default"]))
        for key, rows in CURRENCY_BANKS.items():
            for cur_, text in rows:
                db.execute("INSERT OR IGNORE INTO company_bank_cur (company, currency, bank_details) "
                           "VALUES (?, ?, ?)", (key, cur_, text))
        # first administrator from the environment (new server), so /setup is never exposed
        if APP_PASSWORD and not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            db.execute("INSERT INTO users (username, pw_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
                       (ADMIN_USER, generate_password_hash(APP_PASSWORD),
                        datetime.now().isoformat(timespec="seconds")))
        # forgotten password on a server: set RESET_ADMIN_PASSWORD in the host's settings, restart,
        # log in, then REMOVE the variable (while it is set, every restart resets the password again)
        reset_pw = os.environ.get("RESET_ADMIN_PASSWORD", "")
        if reset_pw:
            hit = db.execute("UPDATE users SET pw_hash = ?, is_admin = 1 WHERE username = ? COLLATE NOCASE",
                             (generate_password_hash(reset_pw), ADMIN_USER)).rowcount
            if not hit:
                db.execute("INSERT INTO users (username, pw_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
                           (ADMIN_USER, generate_password_hash(reset_pw),
                            datetime.now().isoformat(timespec="seconds")))
            app.logger.warning("RESET_ADMIN_PASSWORD is set: password of '%s' was reset. "
                               "Remove the variable now.", ADMIN_USER)


QUOTE_SERIES = re.compile(r"^" + QUOTE_PREFIX + r"(\d{2})(\d{4,})$", re.I)


def next_quote_no(db, year=None):
    """Next automatic quotation number: Q270001, Q270002 ... (Q + year + 4-digit running number).
    Continues from the highest number of that year in the series."""
    yy = f"{series_yy(year):02d}"
    top = 0
    for (no,) in db.execute("SELECT quote_no FROM quotations WHERE typeof(quote_no) = 'text' AND quote_no LIKE ?",
                            (QUOTE_PREFIX + yy + "%",)):
        m = QUOTE_SERIES.match(str(no))
        if m and m.group(1) == yy:
            top = max(top, int(m.group(2)))
    return f"{QUOTE_PREFIX}{yy}{top + 1:04d}"


QUOTE_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,29}$")


def parse_quote_no(raw):
    """A typed quotation number: letters, digits and . _ - (up to 30 characters, starting with a letter
    or digit). Digits only -> stored as a number (keeps the automatic numbering going); anything else
    is stored as text. Returns None when it is not valid."""
    raw = (raw or "").strip()
    if not QUOTE_NO_RE.match(raw):
        return None
    return int(raw) if raw.isdigit() and int(raw) < 10**9 else (None if raw.isdigit() else raw)


QUOTE_NO_ERR = "Quotation number can have letters, digits and - _ . only (up to 30 characters)."


def quote_no_taken(db, no, exclude_id=None):
    row = db.execute("SELECT id FROM quotations WHERE quote_no = ? COLLATE NOCASE AND id IS NOT ?",
                     (no, exclude_id)).fetchone()
    return bool(row)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
TWO = Decimal("0.01")


def money(value):
    return Decimal(value).quantize(TWO, rounding=ROUND_HALF_UP)


def to_decimal(text, field, errors, allow_zero=True):
    try:
        d = Decimal(str(text).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        errors.append(f"{field} must be a number.")
        return Decimal(0)
    if d < 0 or (d == 0 and not allow_zero):
        errors.append(f"{field} must be {'greater than' if not allow_zero else 'at least'} 0.")
    return d


def fmt_money(value, currency=None):
    """Plain 2-decimal format, as in the sample (20000.00)."""
    return f"{money(value):.2f}"


def fmt_qty(value):
    d = Decimal(value)
    return f"{d.normalize():f}" if d != d.to_integral() else str(int(d))


def fmt_date(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d-%m-%Y")
    except (ValueError, TypeError):
        return iso or ""


app.jinja_env.filters["money"] = fmt_money
app.jinja_env.filters["qty"] = fmt_qty
app.jinja_env.filters["dmy"] = fmt_date


def parse_lines(form, errors):
    """Item rows + discount from a submitted form (used by quotations and job orders)."""
    discount_type = form.get("discount_type", "percent")
    if discount_type not in ("percent", "amount"):
        discount_type = "percent"
    descs = form.getlist("description")
    qtys = form.getlist("qty")
    prices = form.getlist("unit_price")
    parts = form.getlist("part_no")
    if len(parts) != len(descs):          # a form without a Part Num column
        parts = [""] * len(descs)
    items = []
    for i, (desc, q, p, part) in enumerate(zip(descs, qtys, prices, parts), start=1):
        desc, part = desc.strip(), part.strip()
        if not desc and not str(q).strip() and not str(p).strip():
            continue  # fully blank row
        if not desc:
            errors.append(f"Item {i}: description is required.")
        qty = to_decimal(q, f"Item {i} quantity", errors, allow_zero=False)
        price = to_decimal(p, f"Item {i} unit price", errors)
        items.append({"description": desc, "part_no": part, "qty": qty, "unit_price": money(price),
                      "line_total": money(qty * price)})
    if not items:
        errors.append("Add at least one item.")

    subtotal = money(sum((it["line_total"] for it in items), Decimal(0)))
    disc_val = to_decimal(form.get("discount_value") or "0", "Discount", errors)
    if discount_type == "percent":
        if disc_val > 100:
            errors.append("Discount % cannot exceed 100.")
        disc_amt = money(subtotal * disc_val / 100)
    else:
        if disc_val > subtotal:
            errors.append("Discount cannot be more than the total price.")
        disc_amt = money(disc_val)
    return items, {"discount_type": discount_type, "discount_value": disc_val,
                   "subtotal": subtotal, "discount_amt": disc_amt,
                   "total": money(subtotal - disc_amt)}


def parse_form(form, qid=None):
    """Validate the quotation form and compute totals. Returns (data, items, errors)."""
    errors = []
    data = {
        "to_address": "\n".join(l.strip() for l in form.get("to_address", "").replace("\r", "").strip().split("\n")),
        "q_date": form.get("q_date", "").strip(),
        "valid_till": "",          # no longer used (the column stays for old quotations)
        "your_ref": form.get("your_ref", "").strip(),
        "tc_type": form.get("tc_type", "").strip(),
        "vessel_name": form.get("vessel_name", "").strip(),
        "terms_type": form.get("terms_type", "").strip(),
        "currency": form.get("currency", "INR"),
        "terms": "\n".join(l.rstrip() for l in form.get("terms", "").replace("\r", "").strip().split("\n")),
        "remarks": form.get("remarks", "").strip(),
    }
    if not data["to_address"]:
        errors.append("'To' address is required.")
    try:
        datetime.strptime(data["q_date"], "%Y-%m-%d")
    except ValueError:
        errors.append("Date is required.")
    if data["currency"] not in CURRENCIES:
        errors.append("Choose a valid currency.")
    if not data["terms"]:
        errors.append("Terms & Conditions cannot be empty.")
    m = re.search(r"(?im)^\s*delivery time\s*:\s*(.+)$", data["terms"])
    data["delivery_time"] = m.group(1).strip() if m else ""

    # Quotation number: automatic, unless the user typed a different one.
    raw_no = form.get("quote_no", "").strip()
    data["quote_no"] = None
    if raw_no and raw_no != form.get("suggested_quote_no", "").strip():
        parsed = parse_quote_no(raw_no)
        if parsed is None:
            errors.append(QUOTE_NO_ERR)
        else:
            data["quote_no"] = parsed
            if quote_no_taken(get_db(), data["quote_no"], qid):
                errors.append(f"Quotation number {raw_no} already exists.")

    items, money_data = parse_lines(form, errors)
    data.update(money_data)
    return data, items, errors


def save_quotation(db, data, items, qid=None):
    cols = ("to_address", "q_date", "valid_till", "your_ref", "tc_type", "vessel_name", "currency",
            "delivery_time", "remarks", "discount_type", "terms", "terms_type")
    dec = ("discount_value", "subtotal", "discount_amt", "total")
    vals = [data[c] for c in cols] + [str(data[c]) for c in dec]

    custom_no = data.get("quote_no")
    if qid is None:
        for _attempt in range(3):  # retry if two people save at the same moment
            try:
                no = custom_no or next_quote_no(db)
                cur = db.execute(
                    f"INSERT INTO quotations (quote_no, {', '.join(cols + dec)}, company, created_at) "
                    f"VALUES (?, {', '.join('?' * (len(cols) + len(dec)))}, ?, ?)",
                    [no] + vals + [data.get("company") or DEFAULT_COMPANY,
                                   datetime.now().isoformat(timespec="seconds")])
                qid = cur.lastrowid
                break
            except sqlite3.IntegrityError:
                db.rollback()
                if custom_no:
                    raise
        else:
            raise RuntimeError("Could not allocate a quotation number.")
    else:
        sets = ", ".join(f"{c} = ?" for c in cols + dec)
        db.execute(f"UPDATE quotations SET {sets} WHERE id = ?", vals + [qid])
        if custom_no:
            db.execute("UPDATE quotations SET quote_no = ? WHERE id = ?", (custom_no, qid))
        db.execute("DELETE FROM items WHERE quotation_id = ?", (qid,))

    db.executemany(
        "INSERT INTO items (quotation_id, sno, description, part_no, qty, unit_price, line_total) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(qid, n, it["description"], it["part_no"], str(it["qty"]), str(it["unit_price"]),
          str(it["line_total"])) for n, it in enumerate(items, start=1)])
    remember_masters(db, data)
    db.commit()
    return qid


def remember_address(db, address):
    """Save a 'To' address in the one saved-address list used by every document (quotation, purchase
    order, tax invoice, commercial invoice, packing list). Does nothing if it is already saved; a new
    address whose first line (name) is already used by a different address is saved as 'name - 2nd line'."""
    addr = clean_address(address)
    if not addr:
        return
    if db.execute("SELECT 1 FROM customers WHERE address = ?", (addr,)).fetchone():
        return
    lines = addr.split("\n")
    for name in [lines[0]] + ([f"{lines[0]} - {lines[1]}"] if len(lines) > 1 else []):
        if not db.execute("SELECT 1 FROM customers WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
            db.execute("INSERT INTO customers (name, address) VALUES (?, ?)", (name, addr))
            return


def remember_masters(db, data):
    """Save the To address and T/C type for future use (ignored if already saved)."""
    remember_address(db, data["to_address"])
    if data["tc_type"]:
        db.execute("INSERT OR IGNORE INTO tc_types (name) VALUES (?)", (data["tc_type"],))


def saved_addresses(db):
    return [dict(c) for c in db.execute("SELECT * FROM customers ORDER BY name COLLATE NOCASE")]


def masters(db):
    return (db.execute("SELECT * FROM customers ORDER BY name COLLATE NOCASE").fetchall(),
            db.execute("SELECT * FROM tc_types ORDER BY name COLLATE NOCASE").fetchall())


def load_quotation(db, qid):
    q = db.execute("SELECT * FROM quotations WHERE id = ?", (qid,)).fetchone()
    if q is None:
        abort(404)
    items = db.execute("SELECT * FROM items WHERE quotation_id = ? ORDER BY sno", (qid,)).fetchall()
    return q, items


# --------------------------------------------------------------------------
# PDF  (fixed template - only the values change)
# --------------------------------------------------------------------------
NAVY = colors.HexColor("#0b2a63")


class _FillToBottom(Spacer):
    """Empty space that fills whatever is left of the page above the footer block, so the
    discount / total / terms always end at the bottom of the (last) page. If the footer
    would not fit under the items, this pushes it to a fresh page."""

    def __init__(self, footer_height, min_gap):
        Spacer.__init__(self, 1, 1)
        self.footer_height, self.min_gap = footer_height, min_gap

    def wrap(self, avail_w, avail_h):
        room = avail_h - self.footer_height - 8
        if room >= self.min_gap:
            return avail_w, room
        return avail_w, avail_h + 1        # does not fit here -> move to the next page


class _GapOrFill(Spacer):
    """Space between the items and the totals / terms of a quotation. On a one-page quotation it fills
    the page so the totals and terms end at the bottom, as in the sample. As soon as the quotation runs
    to more pages there is only a small gap (the totals follow the items; the terms go on to the next
    page, right under the letterhead, when they do not fit)."""

    def __init__(self, doc, footer_height, min_gap, gap=10):
        Spacer.__init__(self, 1, 1)
        self.doc, self.footer_height, self.min_gap, self.gap = doc, footer_height, min_gap, gap

    def wrap(self, avail_w, avail_h):
        room = avail_h - self.footer_height - 8
        if self.doc.page == 1 and room >= self.min_gap:
            return avail_w, room
        return avail_w, min(self.gap, avail_h)


# --------------------------------------------------------------------------
# Letterhead (the header printed on every PDF), editable by an administrator on the Letterhead page.
# It is drawn from the saved text + the logo / certification pictures into a PNG (cached on the data
# disk), so changing the address, phone, e-mail or GST/UEN no. needs no image editing.
# --------------------------------------------------------------------------
LETTERHEAD_DEFAULTS = {
    "name": "TURBO PRIME PTE LTD",
    "reg_line": "GST No./UEN No. 202307808W",
    "address": "11 Lok Yang Way\nSingapore - 628632",
    "phone": "Phone: +65 93967400/62092432",
    "email": "E mail: sales@turboprimesg.com",
}
LETTERHEAD_DIR = os.path.join(DATA_DIR, "letterhead")
LH_IMAGES = {"logo": "letterhead_logo.png", "cert": "letterhead_cert.png"}   # default pictures (static/)


def letterhead_settings(db):
    out = dict(LETTERHEAD_DEFAULTS)
    try:
        for k, v in db.execute("SELECT k, v FROM letterhead"):
            if k in out:
                out[k] = v
    except sqlite3.Error:
        pass
    return out


def lh_image_path(kind):
    """The uploaded picture if there is one, otherwise the built-in default."""
    custom = os.path.join(LETTERHEAD_DIR, kind + ".png")
    return custom if os.path.exists(custom) else os.path.join(BASE_DIR, "static", LH_IMAGES[kind])


def find_font(bold):
    """Arial for the letterhead. Real Arial is used when it is available (arial.ttf / arialbd.ttf put in
    static/fonts, or installed on Windows / Mac); otherwise Liberation Sans, shipped in static/fonts,
    which has exactly the same letter widths as Arial, so the header looks the same on every server."""
    fonts = os.path.join(BASE_DIR, "static", "fonts")
    lib = "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"
    candidates = [
        os.path.join(fonts, "arialbd.ttf" if bold else "arial.ttf"),
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        os.path.join(fonts, lib),
        "/usr/share/fonts/truetype/liberation/" + lib,
        "/usr/share/fonts/truetype/msttcorefonts/" + ("Arial_Bold.ttf" if bold else "Arial.ttf"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise FileNotFoundError("No Arial font found - copy the static/fonts folder (LiberationSans-*.ttf) into the app.")


def render_letterhead(st, logo_path, cert_path):
    """Draw the letterhead (logo left, name + details centred, certification right) as a PNG."""
    from PIL import Image as PILImage, ImageDraw, ImageFont
    S, W = 2, 1015                                     # drawn at 2x for a sharp print
    fb = find_font(True)
    fr = find_font(False)
    cx = 556
    lines = [(st["reg_line"].strip(), True)] if st["reg_line"].strip() else []
    lines += [(l.strip(), False) for l in st["address"].replace("\r", "").split("\n") if l.strip()]
    lines += [(st[k].strip(), False) for k in ("phone", "email") if st[k].strip()]
    title = st["name"].strip()

    def font(path, size):
        return ImageFont.truetype(path, max(int(round(size * S)), 6))

    # Company name in the heavy "Arial Black" look of the original letterhead picture: bold Arial
    # thickened by an outline, widened 13 % and slightly spaced (same font on every server).
    title_img = None
    if title:
        tf = font(fb, 31.5)
        layer = PILImage.new("L", (int((tf.getlength(title) * 1.2 + 40 * S) + len(title) * 2 * S), 60 * S), 0)
        ld = ImageDraw.Draw(layer)
        x = 10 * S
        for ch in title:
            ld.text((x, 10 * S), ch, font=tf, fill=255, stroke_width=S, stroke_fill=255)
            x += tf.getlength(ch) + 1.25 * S
        layer = layer.crop(layer.getbbox())
        tw, th = int(layer.width * 1.13), layer.height
        if tw > 470 * S:                                    # a long name is scaled down to fit
            k = 470 * S / tw
            tw, th = int(tw * k), max(int(th * k), 1)
        title_img = layer.resize((tw, th), PILImage.LANCZOS)
    title_top = 7
    dsize = 16.7
    widest = max([font(fb if b else fr, dsize).getlength(t) / S for t, b in lines] or [1])
    if widest > 400:
        dsize *= 400 / widest
    step = dsize * 1.26
    top_text = title_top + title_img.height / S + 16 if title_img else 12
    text_bottom = top_text + step * len(lines) + 4
    logo = PILImage.open(logo_path).convert("RGB")
    cert = PILImage.open(cert_path).convert("RGB")

    def fit(im, bw, bh):
        k = min(bw / im.width, bh / im.height)
        return im.resize((max(int(im.width * k * S), 1), max(int(im.height * k * S), 1)), PILImage.LANCZOS)

    logo, cert = fit(logo, 190, 160), fit(cert, 210, 120)
    H = int(max(text_bottom, logo.height / S, cert.height / S, 160) + 8)
    # the title may reach over the certification box: keep the box below the title's letters
    cert_min_y = 0
    if title_img and cx + title_img.width / S / 2 > 949 - cert.width / S - 6:
        cert_min_y = title_top + title_img.height / S + 6
    cert_y = max((H * S - cert.height) // 2, int(cert_min_y * S))
    H = int(max(H, cert_y / S + cert.height / S + 8))
    canvas = PILImage.new("RGB", (W * S, H * S), "white")
    d = ImageDraw.Draw(canvas)
    if title_img:                                                                         # dark blue company name
        canvas.paste(PILImage.new("RGB", title_img.size, "#0b2a63"),
                     (cx * S - title_img.width // 2, title_top * S), title_img)
    y = top_text
    for t, b in lines:
        d.text((cx * S, y * S), t, font=font(fb if b else fr, dsize), fill="#1a3d80", anchor="ma")   # blue details
        y += step
    canvas.paste(logo, (129 * S, (H * S - logo.height) // 2))
    canvas.paste(cert, (949 * S - cert.width, cert_y))
    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()


def letterhead_file(db=None):
    """Path of the current letterhead PNG (built and cached on the data disk). Falls back to the
    original static picture if anything goes wrong, so a PDF can always be made."""
    full = os.path.join(LETTERHEAD_DIR, "full.png")          # a complete header picture uploaded by the administrator
    if os.path.exists(full):
        return full
    try:
        db = db or get_db()
        st = letterhead_settings(db)
        lp, cp = lh_image_path("logo"), lh_image_path("cert")
        sig = json.dumps([st, os.path.getmtime(lp), os.path.getsize(lp), os.path.getmtime(cp),
                          os.path.getsize(cp), find_font(True), find_font(False), "v5"], sort_keys=True)
        cache = os.path.join(LETTERHEAD_DIR, "cache")
        path = os.path.join(cache, "hdr_" + hashlib.sha1(sig.encode()).hexdigest()[:16] + ".png")
        if not os.path.exists(path):
            os.makedirs(cache, exist_ok=True)
            data = render_letterhead(st, lp, cp)
            for old in os.listdir(cache):
                if old.startswith("hdr_"):
                    try:
                        os.remove(os.path.join(cache, old))
                    except OSError:
                        pass
            with open(path, "wb") as fh:
                fh.write(data)
        return path
    except Exception:
        app.logger.exception("Could not build the letterhead - using the default picture")
        return os.path.join(BASE_DIR, "static", "header_pte.png")


def header_image(company_key, width):
    """The letterhead image (logo, name, address, GST no.) of a company, scaled to `width`."""
    path = letterhead_file()
    iw, ih = ImageReader(path).getSize()
    return Image(path, width=width, height=width * ih / iw)


_HEADER_CROPS = {}


def tight_header(company_key, content_w, left_pad):
    """Letterhead cropped to its visible content (the supplied images carry blank margins) and
    placed so that the content starts `left_pad` pt in from the left margin and is `content_w`
    pt wide - the position it has on the sample job order / purchase order."""
    from PIL import Image as PILImage
    path = letterhead_file()
    key = (path, os.path.getmtime(path))
    if key not in _HEADER_CROPS:
        im = PILImage.open(path).convert("RGB")
        px = im.load()
        w, h = im.size
        xs, ys = [], []
        for y in range(h):
            for x in range(w):
                r, g, b = px[x, y]
                if r + g + b < 700:
                    xs.append(x)
                    ys.append(y)
        box = (max(min(xs) - 1, 0), max(min(ys) - 1, 0), min(max(xs) + 2, w), min(max(ys) + 2, h))
        out = io.BytesIO()
        im.crop(box).save(out, "PNG")
        _HEADER_CROPS[key] = (out.getvalue(), box[2] - box[0], box[3] - box[1])
    data, iw, ih = _HEADER_CROPS[key]
    img = Image(io.BytesIO(data), width=content_w, height=content_w * ih / iw)
    t = Table([[img]], colWidths=[content_w + left_pad])
    t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), left_pad), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                           ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    t.hAlign = "LEFT"
    return t


# (content width, left position, top position) of the letterhead on the sample job order / purchase order
SAMPLE_HEADER = {"pte": (470, 4, 28)}


def to_and_details(to_address, right_html, right_w, base):
    """'To' + address on the left, the document details block on the right (shared by all PDFs)."""
    CONTENT_W = 175 * mm
    addr_w = max(stringWidth(l, "Helvetica", 9) for l in to_address.split("\n"))
    # mirror the address's right edge about the page centre -> equal space either side of centre
    right_x = min(CONTENT_W - addr_w, 122 * mm)   # short addresses: keep block from drifting right
    right_x = max(right_x, addr_w + 8 * mm, 70 * mm)       # never crowd the address
    right_x = min(right_x, CONTENT_W - right_w - 4 * mm)   # always fit inside the page
    info = Table([[Paragraph("To", base), ""],
                  [Paragraph(escape(to_address).replace("\n", "<br/>"), base), Paragraph(right_html, base)]],
                 colWidths=[right_x, CONTENT_W - right_x])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                              ("TOPPADDING", (0, 0), (-1, -1), 0),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return info


def rich(value):
    """User text; short lines ending with ':' (e.g. 'Job scope:') become bold dark blue."""
    out = []
    for line in str(value or "").replace("\r", "").split("\n"):
        t = line.strip()
        if t.endswith(":") and 0 < len(t) <= 40:
            out.append(f"<font color='#0b2a63'><b>{escape(line)}</b></font>")
        else:
            out.append(escape(line))
    return "<br/>".join(out)


class _PageGap(Spacer):
    """A gap that is `first` pt on page 1 (the fixed layout of the sample) and `other` pt on later pages."""

    def __init__(self, doc, first, other):
        Spacer.__init__(self, 1, 1)
        self.doc, self.first, self.other = doc, first, other

    def wrap(self, avail_w, avail_h):
        return avail_w, min(self.first if getattr(self.doc, "page", 0) <= 1 else self.other, max(avail_h, 0))


def make_doc(buf, pagesize, left_m, right_m, top_m, bottom_m, company, title, hw=None, hleft=0):
    """Document template shared by all PDFs. Page 1 starts with the letterhead (the first flowable of the
    story, returned as `hdr`); every following page gets the same letterhead drawn at the same place and its
    content starts right below it - so a document that runs to more pages always looks the same.
    The story must start with NextPageTemplate("later"). hw/hleft: cropped letterhead of that width at that
    offset (as on the quotation); without hw the letterhead is scaled to the page width."""
    PH = pagesize[1]
    doc = BaseDocTemplate(buf, pagesize=pagesize, leftMargin=left_m, rightMargin=right_m, topMargin=top_m,
                          bottomMargin=bottom_m, title=title, author=co(company)["name"])
    if hw:
        hdr = tight_header(company, hw, hleft)
        hpath = letterhead_file()
        crop = _HEADER_CROPS[(hpath, os.path.getmtime(hpath))]
        data, hdr_w, hdr_h, hx = crop[0], hw, hw * crop[2] / crop[1], left_m + 6 + hleft
    else:
        hdr = header_image(company, doc.width)
        hpath = letterhead_file()
        iw, ih = ImageReader(hpath).getSize()
        with open(hpath, "rb") as fh:
            data = fh.read()
        hdr_w, hdr_h, hx = doc.width, doc.width * ih / iw, left_m + 6
    hy = PH - top_m - 6 - hdr_h

    def draw_letterhead(canv, _doc):
        canv.drawImage(ImageReader(io.BytesIO(data)), hx, hy, width=hdr_w, height=hdr_h, mask="auto")

    doc.addPageTemplates([
        PageTemplate(id="first", frames=[Frame(left_m, bottom_m, doc.width, PH - top_m - bottom_m, id="f1")]),
        PageTemplate(id="later", onPage=draw_letterhead,
                     frames=[Frame(left_m, bottom_m, doc.width, (hy - 14 + 6) - bottom_m, id="f2")]),
    ])
    return doc, hdr, hdr_h


def _stack_height(flowables, width, avail=800):
    return sum(f.wrap(width, avail)[1] for f in flowables)


def build_job_pdf(q, items, jo):
    """Job order PDF laid out like the sample: letterhead, 'Job Order' title, To + details block at
    x=416pt, boxed '# | Item description | Quantity' table (no prices), then the editable Remarks."""
    TITLE_TOP, TABLE_TOP, REMARKS_TOP = 126, 264, 440     # pt from the top of the page (tuned to the sample)
    q = {**dict(q), "jo_no": jo["jo_no"], "jo_date": jo["jo_date"], "po_number": jo["po_number"],
         "remarks": jo["remarks"]}
    buf = io.BytesIO()
    hw, hleft, htop = SAMPLE_HEADER.get(q["company"], SAMPLE_HEADER["pte"])
    doc, hdr, _hh = make_doc(buf, A4, 30, 30, htop, 40, q["company"], f"Job Order {q['jo_no']}", hw, hleft)
    W = doc.width
    # the sample uses 11pt text and a 16pt bold title
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=11, leading=14.3, textColor=NAVY)
    bold = ParagraphStyle("bold", parent=base, fontName="Helvetica-Bold", leading=12)
    desc_st = ParagraphStyle("desc_st", parent=base, leading=14)
    title = ParagraphStyle("title", parent=bold, fontSize=16, leading=19, alignment=TA_CENTER)
    ctr = ParagraphStyle("ctr", parent=base, alignment=TA_CENTER)
    head_c = ParagraphStyle("head_c", parent=bold, alignment=TA_CENTER)
    rem = ParagraphStyle("rem", parent=base, leftIndent=17, leading=14.7)   # x 53 incl. 6pt frame padding

    def txt(v):
        return escape(str(v or "")).replace("\n", "<br/>")

    top = [NextPageTemplate("later"), hdr, Spacer(1, max(1, TITLE_TOP - (htop + _stack_height([hdr], W)))),
           Paragraph("<u>Job Order</u>", title),
           Spacer(1, 2)]
    right_block = [Paragraph(txt(f"Job Order No: {q['jo_no']}"), ParagraphStyle("b14", parent=base, fontName="Helvetica-Bold")),
                   Paragraph(txt(f"Date: {fmt_date(q['jo_date'])}"), base),
                   Paragraph(txt(f"Customer PO No: {q['po_number']}"), base),
                   Paragraph(txt(f"TC Type: {q['tc_type']}"), base),
                   Paragraph(txt(f"Vessel Name: {jo['vessel_name'] or ''}"), base)]
    info = Table([[Paragraph("To", base), ""],
                  [Paragraph(txt(q["to_address"]), base), right_block]], colWidths=[366, W - 366])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    story = top + [info]
    gap = max(14, TABLE_TOP - (doc.topMargin + _stack_height(story, W)))
    story.append(Spacer(1, gap))

    # '#' 30-80 | description 80-306 (header centred at 193) | spacer 306-335 | quantity 335-565 (centred at 450)
    cw = [50, 226, 29, W - 305]
    rows = [[Paragraph("#", bold), Paragraph("Item description", bold), "", Paragraph("Quantity", head_c)]]
    for it in items:
        rows.append([Paragraph(txt(it["sno"]), desc_st), Paragraph(rich(it["description"]), desc_st), "",
                     Paragraph(fmt_qty(it["qty"]), ctr)])
    t = Table(rows, colWidths=cw, repeatRows=1)
    style = [("BOX", (0, 0), (-1, 0), 0.8, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LEFTPADDING", (0, 0), (0, -1), 7), ("LEFTPADDING", (1, 0), (1, -1), 6),
             ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]
    style += [("SPAN", (1, r), (2, r)) for r in range(1, len(rows))]   # long descriptions use cols 2-3
    t.setStyle(TableStyle(style))
    story.append(t)
    if (q["remarks"] or "").strip():
        h = t.wrap(W, 800)[1]
        story += [Spacer(1, max(24, REMARKS_TOP - TABLE_TOP - h)),
                  Paragraph("Remarks:", rem), Paragraph(txt(q["remarks"]), rem)]
    doc.build(story)
    buf.seek(0)
    return buf


def build_pdf(q, items, jo=None, do=None):
    """Quotation PDF; the Delivery Order PDF when `do` (a delivery_orders row that also carries
    jo_no) is given; the Job Order PDF (build_job_pdf) when `jo` is given. Job and delivery
    orders print their own (editable) items and remarks."""
    if jo is not None:
        return build_job_pdf(q, items, jo)
    if do is None:
        return build_quotation_pdf(q, items)
    src = do
    if src is not None:
        q = {**dict(q), **{k: src[k] for k in ("remarks", "discount_type", "discount_value",
                                               "subtotal", "discount_amt", "total")}}
    buf = io.BytesIO()
    doc, header, _hh = make_doc(buf, A4, 15 * mm, 15 * mm, 10 * mm, 15 * mm, q["company"],
                                f"Delivery Order {do['do_no']}")

    base = ParagraphStyle("base", fontName="Helvetica", fontSize=9, leading=12, textColor=NAVY)
    bold = ParagraphStyle("bold", parent=base, fontName="Helvetica-Bold", textColor=NAVY)
    desc_st = ParagraphStyle("desc_st", parent=base, fontSize=8.4, leading=11.5)
    company = ParagraphStyle("company", parent=bold, fontSize=16, leading=20,
                             alignment=TA_CENTER, textColor=NAVY)
    center = ParagraphStyle("center", parent=base, alignment=TA_CENTER, fontSize=8, leading=10.5)
    center_b = ParagraphStyle("center_b", parent=center, fontName="Helvetica-Bold", textColor=NAVY)
    title = ParagraphStyle("title", parent=bold, fontSize=13, leading=16, alignment=TA_CENTER)
    ctr = ParagraphStyle("ctr", parent=base, alignment=TA_CENTER)
    right = ParagraphStyle("right", parent=base, alignment=TA_RIGHT)
    right_b = ParagraphStyle("right_b", parent=right, fontName="Helvetica-Bold", textColor=NAVY)
    head_c = ParagraphStyle("head_c", parent=bold, alignment=TA_CENTER)
    head_r = ParagraphStyle("head_r", parent=bold, alignment=TA_RIGHT)

    def txt(value):
        return escape(str(value or "")).replace("\n", "<br/>")

    def rich(value):
        """User text; short lines ending with ':' (e.g. 'Job scope:') become bold dark blue."""
        out = []
        for line in str(value or "").replace("\r", "").split("\n"):
            t = line.strip()
            if t.endswith(":") and 0 < len(t) <= 40:
                out.append(f"<font color='#0b2a63'><b>{escape(line)}</b></font>")
            else:
                out.append(escape(line))
        return "<br/>".join(out)

    def para(value, style=base):
        return Paragraph(txt(value), style)

    # ---- Header: the image of the company the quotation belongs to --------
    heading = "Delivery Order" if do is not None else "Job Order" if jo else "Quotation"
    story = [NextPageTemplate("later"), header, Spacer(1, 5 * mm), Paragraph(f"<u>{heading}</u>", title), Spacer(1, 3 * mm)]

    # ---- To (left) and document details (right) ------------------------
    if do is not None:
        first = f"No: {do['do_no']}"
        others = [f"Shipper Name: {do['shipper_name']}", f"Date: {fmt_date(do['do_date'])}",
                  f"TC Type: {q['tc_type']}", f"Vessel Name: {do['vessel_name']}",
                  f"Customer PO No: {do['po_number']}"]
    elif jo:
        first = f"Job Order No:{jo['jo_no']}"
        others = [f"Date: {fmt_date(jo['jo_date'])}", f"Customer PO No: {jo['po_number']}",
                  f"TC Type: {q['tc_type']}"]
    else:
        first = f"Quotation no:{q['quote_no']}"
        others = [f"Date: {fmt_date(q['q_date'])}",
                  f"Your ref: {q['your_ref']}", f"TC Type: {q['tc_type']}"]
    if do is not None:      # sample layout: plain lines, then the bold "Ship Spares in Transit"
        right_block = ("<br/>".join(txt(t) for t in [first] + others)
                       + f"<br/><b>{escape(DO_BANNER)}</b>")
    else:
        right_block = (f"<font color='#0b2a63'><b>{txt(first)}</b></font><br/>"
                       + "<br/>".join(txt(t) for t in others))
    CONTENT_W = 175 * mm
    right_w = max([stringWidth(first, "Helvetica-Bold", 9)] +
                  [stringWidth(t, "Helvetica", 9) for t in others] +
                  ([stringWidth(DO_BANNER, "Helvetica-Bold", 9)] if do is not None else []))
    info = to_and_details(q["to_address"], right_block, right_w, base)
    story += [info, Spacer(1, 8 * mm)]

    if do is not None:
        # ---- Delivery order body: no prices / discount / terms (as in the sample) ----
        do_rows = [[Paragraph("#", bold), Paragraph("Item description", bold),
                    Paragraph("Part No", head_c), Paragraph("Quantity", head_c)]]
        for it in items:
            do_rows.append([para(it["sno"]), Paragraph(rich(it["description"]), desc_st),
                            para(it["part_no"], ctr), para(fmt_qty(it["qty"]), ctr)])
        do_t = Table(do_rows, colWidths=[9 * mm, 101 * mm, 35 * mm, 30 * mm], repeatRows=1)
        do_t.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, 0), 0.8, colors.black),                 # header box only, no column lines
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        sign = Table([[Paragraph("Authorised Signature", base),
                       Paragraph("Customer's chop and signature", right)]],
                     colWidths=[CONTENT_W / 2, CONTENT_W / 2])
        sign.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        sign_h = sign.wrap(doc.width - 12, doc.height)[1]
        story += [do_t, _GapOrFill(doc, sign_h, min_gap=15 * mm), sign]
        doc.build(story)
        buf.seek(0)
        return buf


# Quotation PDF geometry (pt, from the latest sample quotation: US Letter page, 612 x 792)
Q_LEFT, Q_RIGHT = 59.5, 551.5          # left / right edge of the item table and the TOTAL box
# column widths: # | Item description | Part Num | Quantity | Unit Price | Total Price
Q_COLS = [20.2, 155.3, 92.4, 42.2, 72.0, 109.9]
Q_DETAILS_X = 378.2                    # left edge of the No / Date / Your ref ... block and of 'TOTAL(USD)'
Q_HEADER = {"pte": (490, 52, 27.5)}      # (letterhead width, left, top) on the page


def build_quotation_pdf(q, items):
    """Quotation PDF laid out like the latest sample: letterhead, title, To + details (No / Date /
    Your ref / TC Type / Vessel Name), boxed '# | Item description | Part Num | Quantity |
    Unit Price | Total Price' table, one boxed TOTAL(currency) row and the editable Terms & Conditions."""
    TITLE_TOP, TO_TOP = 147.1, 164.2                          # (tuned so the text lands where the sample's does)
    FOOT_BOTTOM = 699.5                                       # bottom of the last line of the footer block
    hw, hleft, htop = Q_HEADER.get(q["company"], Q_HEADER["pte"])
    buf = io.BytesIO()
    left_m, right_m = Q_LEFT - 6, 612 - Q_RIGHT - 6
    bottom_m = 792 - FOOT_BOTTOM - 6 - 8              # frame padding 6 pt; _FillToBottom keeps 8 pt spare
    doc = BaseDocTemplate(buf, pagesize=LETTER, leftMargin=left_m, rightMargin=right_m, topMargin=htop - 6,
                          bottomMargin=bottom_m, title=f"Quotation {q['quote_no']}",
                          author=co(q["company"])["name"])
    W = doc.width
    # Page 1 starts with the letterhead as part of the story. Every following page ("continuation")
    # gets the same letterhead drawn at the same place, and its text starts right below it.
    hdr_left = hleft - Q_LEFT + 30
    hdr_img = tight_header(q["company"], hw, hdr_left)          # (also fills the cropped-image cache)
    hpath = letterhead_file()
    crop = _HEADER_CROPS[(hpath, os.path.getmtime(hpath))]       # (png bytes, width, height)
    hdr_h = hw * crop[2] / crop[1]
    later_top = 792 - (htop + hdr_h + 14)                        # y of the top of the text area, pt

    def draw_letterhead(canv, _doc):
        canv.drawImage(ImageReader(io.BytesIO(crop[0])), Q_LEFT + hdr_left, 792 - htop - hdr_h,
                       width=hw, height=hdr_h, mask="auto")

    doc.addPageTemplates([
        PageTemplate(id="first", frames=[Frame(left_m, bottom_m, doc.width, doc.height, id="f1")]),
        PageTemplate(id="later", onPage=draw_letterhead,
                     frames=[Frame(left_m, bottom_m, doc.width, later_top - bottom_m, id="f2")]),
    ])
    cur = q["currency"]
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=10, leading=12.7, textColor=NAVY)
    bold = ParagraphStyle("bold", parent=base, fontName="Helvetica-Bold")
    desc_st = ParagraphStyle("desc_st", parent=base)
    title = ParagraphStyle("title", parent=bold, fontSize=14.7, leading=18, alignment=TA_CENTER)
    terms_st = ParagraphStyle("terms_st", parent=base, leading=13.2)
    ctr = ParagraphStyle("ctr", parent=base, alignment=TA_CENTER)
    right = ParagraphStyle("right", parent=base, alignment=TA_RIGHT)
    head_c = ParagraphStyle("head_c", parent=bold, alignment=TA_CENTER)
    head_r = ParagraphStyle("head_r", parent=bold, alignment=TA_RIGHT)
    total_r = ParagraphStyle("total_r", parent=bold, alignment=TA_RIGHT)

    def txt(v):
        return escape(str(v or "")).replace("\n", "<br/>")

    hdr = hdr_img
    story = [NextPageTemplate("later"), hdr, Spacer(1, max(1, TITLE_TOP - (htop + _stack_height([hdr], W)))),
             Paragraph("<u>Quotation</u>", title)]
    story.append(Spacer(1, max(1, TO_TOP - (htop + _stack_height(story, W)))))
    lines = [f"No : {q['quote_no']}", f"Date : {fmt_date(q['q_date'])}",
             f"Your ref: {q['your_ref'] or ''}", f"TC Type : {q['tc_type'] or ''}",
             f"Vessel Name : {q['vessel_name'] or ''}"]
    left_w = Q_DETAILS_X - Q_LEFT
    info = Table([[Paragraph("To", base), ""],
                  [Paragraph(txt(q["to_address"]), base), [Paragraph(txt(l), base) for l in lines]]],
                 colWidths=[left_w, W - left_w])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("LEFTPADDING", (0, 0), (0, -1), 1.8),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    info.hAlign = "LEFT"
    story.append(info)
    story.append(Spacer(1, 1))                  # the item table starts right under the details block

    # ---- item table (only the header row is boxed, as in the sample) ----------------
    rows = [[Paragraph("#", head_c), Paragraph("Item description", bold), Paragraph("Part Num", head_c),
             Paragraph("Quantity", head_c), Paragraph("Unit Price", head_r), Paragraph("Total Price", head_r)]]
    for it in items:
        part = it["part_no"] if "part_no" in it.keys() else ""
        rows.append([Paragraph(txt(it["sno"]), ctr), Paragraph(rich(it["description"]), desc_st),
                     Paragraph(txt(part), ctr), Paragraph(fmt_qty(it["qty"]), ctr),
                     Paragraph(fmt_money(it["unit_price"]), right), Paragraph(fmt_money(it["line_total"]), right)])
    style = [("BOX", (0, 0), (-1, 0), 1.2, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
             ("LEFTPADDING", (1, 0), (1, -1), 1.5), ("RIGHTPADDING", (4, 0), (4, -1), 1.5),
             ("RIGHTPADDING", (5, 0), (5, -1), 3),
             ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
             ("TOPPADDING", (0, 1), (-1, 1), 1.2)]
    if (q["remarks"] or "").strip():
        rows.append(["", Paragraph("<br/>" + rich("Remarks:") + "<br/>" + rich(q["remarks"]), desc_st),
                     "", "", "", ""])
        style.append(("SPAN", (1, len(rows) - 1), (5, len(rows) - 1)))
    items_t = Table(rows, colWidths=Q_COLS, repeatRows=1)
    items_t.setStyle(TableStyle(style))
    items_t.hAlign = "LEFT"
    story.append(items_t)

    # ---- (discount) and one boxed TOTAL(currency) row ----------------------------------------
    lab_w = Q_DETAILS_X - Q_LEFT
    cw = [lab_w, Q_COLS[0] + Q_COLS[1] + Q_COLS[2] + Q_COLS[3] + Q_COLS[4] - lab_w, Q_COLS[5]]
    parts = []
    disc = Decimal(q["discount_amt"])
    if disc > 0:
        label = f"{fmt_qty(q['discount_value'])}% Discount" if q["discount_type"] == "percent" else "Discount"
        sub_t = Table([["", Paragraph("Sub Total", base), Paragraph(fmt_money(q["subtotal"]), right)],
                       ["", Paragraph(label, base), Paragraph(fmt_money(disc), right)]], colWidths=cw)
        sub_t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                   ("RIGHTPADDING", (2, 0), (2, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        sub_t.hAlign = "LEFT"
        parts += [sub_t, Spacer(1, 3)]
    total_t = Table([["", Paragraph(f"TOTAL({txt(cur)})", bold), Paragraph(fmt_money(q["total"]), total_r)]],
                    colWidths=cw)
    total_t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1.2, colors.black),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                 ("RIGHTPADDING", (2, 0), (2, 0), 3),
                                 ("TOPPADDING", (0, 0), (-1, -1), 1.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6)]))
    total_t.hAlign = "LEFT"
    parts.append(total_t)

    # ---- Terms & conditions (the quotation's own, editable text) -------------------------------
    terms = (q["terms"] or default_terms(q["currency"], q["delivery_time"], q["company"])).replace("\r", "").split("\n")
    tc = [Paragraph("Terms &amp; Conditions:", ParagraphStyle("tch", parent=bold, leading=13.2, leftIndent=1.8))]
    tc += [Paragraph(escape(t), ParagraphStyle("tcl", parent=terms_st, leftIndent=1.8)) if t.strip()
           else Spacer(1, 6) for t in terms]
    tc += [Spacer(1, 13.2),
           Paragraph("Kindly revert in case of any technical or commercial clarifications required.",
                     ParagraphStyle("n1", parent=terms_st, leftIndent=1.8)),
           Paragraph("This is a computer generated document, no signature is required",
                     ParagraphStyle("n2", parent=terms_st, leftIndent=1.8))]
    foot_h = _stack_height(parts + tc, W)
    # one page: the total and the terms sit at the bottom, as in the sample; more pages: no big gaps
    story += [_GapOrFill(doc, foot_h, min_gap=10 * mm), KeepTogether(parts), KeepTogether(tc)]
    doc.build(story)
    buf.seek(0)
    return buf


def build_invoice_pdf(q, inv, items, jo_no):
    """Tax invoice PDF (layout of Tax_Invoice_sample.pdf): letterhead, details, item table with
    part numbers and prices, sub total / GST / total, bank details and 'On behalf of'."""
    buf = io.BytesIO()
    doc, header, _hh = make_doc(buf, A4, 15 * mm, 15 * mm, 10 * mm, 15 * mm, q["company"],
                                f"Tax Invoice {inv['inv_no']}")
    c = co(q["company"])
    cur = q["currency"]

    base = ParagraphStyle("base", fontName="Helvetica", fontSize=9, leading=12, textColor=NAVY)
    bold = ParagraphStyle("bold", parent=base, fontName="Helvetica-Bold")
    desc_st = ParagraphStyle("desc_st", parent=base, fontSize=8.4, leading=11.5)
    title = ParagraphStyle("title", parent=bold, fontSize=14, leading=18, alignment=TA_CENTER)
    ctr = ParagraphStyle("ctr", parent=base, alignment=TA_CENTER)
    right = ParagraphStyle("right", parent=base, alignment=TA_RIGHT)
    right_b = ParagraphStyle("right_b", parent=right, fontName="Helvetica-Bold")
    head_c = ParagraphStyle("head_c", parent=bold, alignment=TA_CENTER)
    head_r = ParagraphStyle("head_r", parent=bold, alignment=TA_RIGHT)

    def txt(value):
        return escape(str(value or "")).replace("\n", "<br/>")

    story = [NextPageTemplate("later"), header, Spacer(1, 5 * mm),
             Paragraph("<u>Tax Invoice</u>", title), Spacer(1, 3 * mm)]

    # ---- To (left) and invoice details (right) ---------------------------
    lines = [f"No : {inv['inv_no']}", f"Date : {fmt_date(inv['inv_date'])}",
             f"TC Type : {q['tc_type']}", f"Customer PO No : {inv['po_number']}",
             f"Our JO No : {jo_no}", f"Vessel Name : {inv['vessel_name']}",
             f"Terms : {inv['pay_terms']}", f"Location : {inv['location']}"]
    right_w = max(stringWidth(t, "Helvetica", 9) for t in lines)
    story += [to_and_details(inv["to_address"] or q["to_address"], "<br/>".join(txt(t) for t in lines), right_w, base),
              Spacer(1, 8 * mm)]

    # ---- Item table: # | Item description | Part Num | Quantity | Unit Price | Total Price ----
    widths = [9 * mm, 72 * mm, 28 * mm, 20 * mm, 22 * mm, 24 * mm]
    rows = [[Paragraph("#", bold), Paragraph("Item description", bold), Paragraph("Part Num", head_c),
             Paragraph("Quantity", head_c), Paragraph("Unit Price", head_r),
             Paragraph("Total Price", head_r)]]
    for it in items:
        rows.append([Paragraph(txt(it["sno"]), base), Paragraph(txt(it["description"]), desc_st),
                     Paragraph(txt(it["part_no"]), ctr), Paragraph(fmt_qty(it["qty"]), ctr),
                     Paragraph(fmt_money(it["unit_price"]), right),
                     Paragraph(fmt_money(it["line_total"]), right)])
    items_t = Table(rows, colWidths=widths, repeatRows=1)
    items_t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, 0), 0.8, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, -1), 4), ("RIGHTPADDING", (-1, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(items_t)

    # ---- Sub total / (discount) / GST / Total, right aligned ---------------
    sums = [[Paragraph("Sub Total", right), Paragraph(fmt_money(inv["subtotal"]), right)]]
    if Decimal(inv["discount_amt"]) > 0:
        label = (f"{fmt_qty(inv['discount_value'])}% Discount"
                 if inv["discount_type"] == "percent" else "Discount")
        sums.append([Paragraph(label, right), Paragraph(fmt_money(inv["discount_amt"]), right)])
    sums.append([Paragraph(f"GST {fmt_qty(inv['gst_rate'])}%", right),
                 Paragraph(fmt_money(inv["gst_amt"]), right)])
    sums.append([Paragraph(f"Total({txt(cur)})", right_b), Paragraph(fmt_money(inv["total"]), right_b)])
    summ = Table(sums, colWidths=[120 * mm, 55 * mm])
    summ.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                              ("RIGHTPADDING", (-1, 0), (-1, -1), 4)]))

    # ---- Bank details (left, editable text) and 'On behalf of' (right) -------
    bank = [Paragraph("Kindly arrange payment to our Bank account", base)]
    bank += [Paragraph(txt(l), base) if l.strip() else Spacer(1, 3 * mm)
             for l in (inv["bank_details"] or get_bank_for(get_db(), q["company"], q["currency"])).replace("\r", "").split("\n")]
    behalf = [Paragraph("On behalf of", base), Paragraph(txt(c["legal"]), base)]
    foot = Table([[bank, behalf]], colWidths=[115 * mm, 60 * mm])
    foot.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                              ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    footer_parts = [summ, Spacer(1, 12 * mm), foot]
    foot_h = sum(f.wrap(doc.width - 12, doc.height)[1] for f in footer_parts)
    # totals + bank details sit at the bottom of the (last) page, as in the sample
    story += [_GapOrFill(doc, foot_h, min_gap=10 * mm), KeepTogether(footer_parts)]

    doc.build(story)
    buf.seek(0)
    return buf


# Purchase order PDF geometry (pt, from the sample PO: US Letter page, 612 x 792)
PO_PAGE_RIGHT = 547.2            # right edge of the item table / total box
PO_TABLE_LEFT = 59.8             # left edge of the item table / total box
# column widths: # | Item description | Part No | Quantity | Unit Price | Total Price
PO_COLS = [39.6, 147.2, 83.9, 50.5, 55.0, PO_PAGE_RIGHT - PO_TABLE_LEFT - (39.6 + 147.2 + 83.9 + 50.5 + 55.0)]
PO_HEADER = {"pte": (490, 52, 27.5)}      # (letterhead width, left, top) on the PO page


def build_po_pdf(q, po, items):
    """Purchase order PDF laid out like the sample: letterhead, title, To + details (No / Your Ref /
    Our Ref No / Date / TC Type, all as typed), item table (# / Item description / Part No / Quantity /
    Unit Price / Total Price), one boxed TOTAL (currency) row and the computer-generated note."""
    TITLE_TOP, DETAILS_TOP, TABLE_TOP = 152.4, 184.2, 266.0       # (tuned so the text lands where the sample's does)
    TOTAL_TOP, NOTE_TOP = 545.75, 701.05                         # top of the total row / of the note, pt from page top
    hw, hleft, htop = PO_HEADER.get(q["company"], PO_HEADER["pte"])
    buf = io.BytesIO()
    doc, hdr, _hh = make_doc(
        buf, LETTER, 24, 612 - PO_PAGE_RIGHT - 6, htop - 6,
        792 - (701.05 + 13.3) - 6 - 8,   # frame padding 6 pt; the gap before the footer keeps 8 pt spare
        q["company"], f"Purchase Order {po['po_no']}", hw, hleft)
    W = doc.width
    TW = PO_PAGE_RIGHT - PO_TABLE_LEFT
    cur = po["currency"]
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=10, leading=13.3, textColor=NAVY)
    bold = ParagraphStyle("bold", parent=base, fontName="Helvetica-Bold")
    title = ParagraphStyle("title", parent=bold, fontSize=14.7, leading=18, alignment=TA_CENTER, leftIndent=30.6)
    ctr = ParagraphStyle("ctr", parent=base, alignment=TA_CENTER)
    right = ParagraphStyle("right", parent=base, alignment=TA_RIGHT)
    head_c = ParagraphStyle("head_c", parent=bold, alignment=TA_CENTER)
    head_r = ParagraphStyle("head_r", parent=bold, alignment=TA_RIGHT)
    total_st = ParagraphStyle("total_st", parent=bold, alignment=TA_RIGHT)

    def txt(v):
        return escape(str(v or "")).replace("\n", "<br/>")

    try:
        pdate = datetime.strptime(po["po_date"], "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        pdate = po["po_date"] or ""
    lines = [f"No : {po['po_no']}", f"Your Ref : {po['your_ref']}", f"Our Ref No : {po['jo_ref']}",
             f"Date :{pdate}", f"TC Type: {po['tc_type']}"]
    story = [NextPageTemplate("later"), hdr, Spacer(1, max(1, TITLE_TOP - (htop + _stack_height([hdr], W)))),
             Paragraph("<u>Purchase Order</u>", title)]
    story.append(Spacer(1, max(1, DETAILS_TOP - 14 - (htop + _stack_height(story, W)))))
    info = Table([[Paragraph("To", base), ""],
                  [Paragraph(txt(po["to_address"]), base), [Paragraph(txt(l), base) for l in lines]]],
                 colWidths=[434.7 - 61.7, W - (434.7 - 30)])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    info.hAlign = "LEFT"
    story.append(_Indent(info, 61.7 - 30))
    story.append(Spacer(1, max(10, TABLE_TOP - (htop + _stack_height(story, W)))))

    rows = [[Paragraph("#", head_c), Paragraph("Item description", bold), Paragraph("Part No", head_c),
             Paragraph("Quantity", head_c), Paragraph("Unit Price", bold), Paragraph("Total Price", head_r)]]
    for it in items:
        rows.append([Paragraph(txt(it["sno"]), ctr), Paragraph(txt(it["description"]), base),
                     Paragraph(txt(it["part_no"]), ctr), Paragraph(fmt_qty(it["qty"]), ctr),
                     Paragraph(fmt_money(it["unit_price"]), right), Paragraph(fmt_money(it["line_total"]), right)])
    items_t = Table(rows, colWidths=PO_COLS, repeatRows=1)
    items_t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, 0), 1.5, colors.black),                     # header box only, no column lines
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1.9), ("RIGHTPADDING", (0, 0), (-1, -1), 1.9),
        ("RIGHTPADDING", (4, 0), (4, -1), 5.2),
        ("TOPPADDING", (0, 1), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 1), (-1, -1), 1.5),
        ("TOPPADDING", (0, 1), (-1, 1), 6),      # clear gap between the headings and the first item
        ("TOPPADDING", (0, 0), (-1, 0), -1.4), ("BOTTOMPADDING", (0, 0), (-1, 0), 1.7),
    ]))
    items_t.hAlign = "RIGHT"
    story.append(items_t)

    # one boxed row: TOTAL (currency) ends at the Unit Price column edge, the amount at the right edge
    label_w = 431.0 - PO_TABLE_LEFT
    total_t = Table([[Paragraph(f"TOTAL ({txt(cur)})", total_st), Paragraph(fmt_money(po["total"]), total_st)]],
                    colWidths=[label_w, TW - label_w])
    total_t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1.5, colors.black), ("RIGHTPADDING", (0, 0), (-1, -1), 1.9),
                                 ("TOPPADDING", (0, 0), (-1, -1), -0.9), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.1)]))
    total_t.hAlign = "RIGHT"
    note = Paragraph("This computer generated and no signature is required",
                     ParagraphStyle("note", parent=base, leftIndent=101.3 - 30))
    th = total_t.wrap(W, 800)[1]
    footer_parts = []
    rate = Decimal(str(po["gst_rate"] or "0"))
    if rate:                       # GST entered: Sub Total and GST lines just above the TOTAL box
        sums_t = Table([[Paragraph("Sub Total", right), Paragraph(fmt_money(po["subtotal"]), right)],
                        [Paragraph(f"GST {fmt_qty(rate)}%", right), Paragraph(fmt_money(po["gst_amt"]), right)]],
                       colWidths=[label_w, TW - label_w])
        sums_t.setStyle(TableStyle([("RIGHTPADDING", (0, 0), (-1, -1), 1.9), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        sums_t.hAlign = "RIGHT"
        footer_parts.append(sums_t)
    footer_parts.append(total_t)
    rem_h = 0
    if (po["remarks"] or "").strip():   # remarks under the TOTAL box, aligned with the item table
        rem_t = Table([[[Paragraph("Remarks:", bold), Paragraph(txt(po["remarks"].strip()), base)]]], colWidths=[TW])
        rem_t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 1.9), ("RIGHTPADDING", (0, 0), (-1, -1), 1.9),
                                   ("TOPPADDING", (0, 0), (-1, -1), 12), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        rem_t.hAlign = "RIGHT"
        footer_parts.append(rem_t)
        rem_h = rem_t.wrap(W, 800)[1]
    footer_parts += [_PageGap(doc, max(16, NOTE_TOP - (TOTAL_TOP + th) - rem_h), 16), note]
    foot_h = _stack_height(footer_parts, W)
    story += [_GapOrFill(doc, foot_h, min_gap=10 * mm), KeepTogether(footer_parts)]
    doc.build(story)
    buf.seek(0)
    return buf


# --------------------------------------------------------------------------
# Commercial Invoice and Packing List (stand-alone documents, A4, like the two sample PDFs)
# --------------------------------------------------------------------------
EXPORT_KINDS = {
    "ci": {"key": "ci", "title": "Commercial Invoice", "plural": "Commercial Invoices", "table": "commercial_invoices",
           "items": "ci_items", "fk": "ci_id", "no": "ci_no", "date": "ci_date", "prefix": "CI ",
           "slug": "commercial-invoice", "prices": True, "file": "CommercialInvoice"},
    "pl": {"key": "pl", "title": "Packing List", "plural": "Packing Lists", "table": "packing_lists",
           "items": "pl_items", "fk": "pl_id", "no": "pl_no", "date": "pl_date", "prefix": "PL ",
           "slug": "packing-list", "prices": False, "file": "PackingList"},
}
EXP_LEFT, EXP_RIGHT = 32.0, 580.0        # left / right edge of the content on the A4 page (pt)
EXP_CI_COLS = [19, 190, 85, 70, 95, 89]  # CI:  # | Item description | Part No | Quantity | Unit Price | Total Price
EXP_PL_COLS = [19, 190, 96, 229]         # PL:  # | Item description | Part No | Quantity (boxed header cells)


def build_export_pdf(kind, row, items):
    """Commercial Invoice / Packing List PDF laid out like the two sample PDFs: letterhead, title, To +
    details (No / Shipper Name / Date / TC Type / Vessel Name / Customer PO No), item table, 'Customer PO No :' line, (the
    Total(currency) of a commercial invoice) and 'Dimensions and Weight :'. Runs onto further pages with
    the letterhead repeated, when there are many items."""
    if kind == "pl":
        return build_pl_pdf(row, items)
    K = EXPORT_KINDS[kind]
    company = row["company"] or "pte"
    no = row[K["no"]]
    buf = io.BytesIO()
    doc, header, _hh = make_doc(buf, A4, EXP_LEFT - 6, A4[0] - EXP_RIGHT - 6, 12, 40, company, f"{K['title']} {no}")
    W = doc.width - 12
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=10, leading=13, textColor=NAVY)
    bold = ParagraphStyle("bold", parent=base, fontName="Helvetica-Bold")
    title = ParagraphStyle("title", parent=bold, fontSize=14.7, leading=18, alignment=TA_CENTER)
    ctr = ParagraphStyle("ctr", parent=base, alignment=TA_CENTER)
    right = ParagraphStyle("right", parent=base, alignment=TA_RIGHT)
    head_c = ParagraphStyle("head_c", parent=bold, alignment=TA_CENTER)
    head_r = ParagraphStyle("head_r", parent=bold, alignment=TA_RIGHT)
    ind = ParagraphStyle("ind", parent=base, leftIndent=19)

    def txt(v):
        return escape(str(v or "")).replace("\n", "<br/>")

    lines = [f"No : {no}", f"Shipper Name : {row['shipper_name'] or ''}", f"Date : {fmt_date(row[K['date']])}",
             f"TC Type : {row['tc_type'] or ''}", f"Vessel Name : {row['vessel_name'] or ''}",
             f"Customer PO No : {row['po_no'] or ''}"]
    left_w = (381 if K["prices"] else 345) - EXP_LEFT
    info = Table([[Paragraph("To", bold), ""],
                  [Paragraph(txt(row["to_address"]), base), [Paragraph(txt(l), base) for l in lines]
                   + [Paragraph(f"<b>{escape(DO_BANNER)}</b>", base)]]],              # bold line under the PO no
                 colWidths=[left_w, W - left_w])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    info.hAlign = "LEFT"
    story = [NextPageTemplate("later"), header, Spacer(1, 16), Paragraph(f"<u>{K['title']}</u>", title),
             Spacer(1, 12), info, Spacer(1, 22)]

    if K["prices"]:
        cols = EXP_CI_COLS
        rows = [[Paragraph("#", head_c), Paragraph("Item description", bold), Paragraph("Part No", head_c),
                 Paragraph("Quantity", head_c), Paragraph("Unit Price", head_r), Paragraph("Total Price", head_r)]]
        for it in items:
            rows.append([Paragraph(txt(it["sno"]), ctr), Paragraph(rich(it["description"]), base),
                         Paragraph(txt(it["part_no"]), ctr), Paragraph(fmt_qty(it["qty"]), ctr),
                         Paragraph(fmt_money(it["unit_price"]), right), Paragraph(fmt_money(it["line_total"]), right)])
        style = [("BOX", (0, 0), (-1, 0), 1.2, colors.black), ("RIGHTPADDING", (4, 0), (4, -1), 6),
                 ("RIGHTPADDING", (5, 0), (5, -1), 3)]
    else:
        cols = EXP_PL_COLS
        rows = [[Paragraph("#", head_c), Paragraph("Item description", bold), Paragraph("Part No", head_c),
                 Paragraph("Quantity", head_c)]]
        for it in items:
            rows.append([Paragraph(txt(it["sno"]), ctr), Paragraph(rich(it["description"]), base),
                         Paragraph(txt(it["part_no"]), ctr), Paragraph(fmt_qty(it["qty"]), ctr)])
        style = [("BOX", (0, 0), (-1, 0), 0.8, colors.black), ("INNERGRID", (0, 0), (-1, 0), 0.8, colors.black)]
    items_t = Table(rows, colWidths=cols, repeatRows=1)
    items_t.setStyle(TableStyle(style + [
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("TOPPADDING", (0, 1), (-1, 1), 5)]))
    items_t.hAlign = "LEFT"
    story.append(items_t)

    foot = [Spacer(1, 26), Paragraph(f"<b>Customer PO No : </b>{txt(row['po_no'])}", ind)]
    if K["prices"]:
        cw = [sum(cols[:4]), cols[4], cols[5]]
        total_t = Table([["", Paragraph(f"<b>Total({txt(row['currency'])})</b>", right),
                          Paragraph(f"<b>{fmt_money(row['total'])}</b>", right)]], colWidths=cw)
        total_t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                                     ("RIGHTPADDING", (1, 0), (1, 0), 12),
                                     ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        total_t.hAlign = "LEFT"
        foot += [Spacer(1, 22), total_t]
    foot += [Spacer(1, 24), Paragraph("Dimensions and Weight :", ind)]
    for l in (row["dimensions"] or "").replace("\r", "").split("\n"):
        foot.append(Paragraph(txt(l), ind) if l.strip() else Spacer(1, 6))
    story.append(KeepTogether(foot))
    doc.build(story)
    buf.seek(0)
    return buf


PL_BOX_LEFT = 28.3                       # Packing List item table: as in the Excel sample (pt from the page edge)
PL_COLS = [21.5, 197.9, 95.9, 224.6]     # # | Item description | Part No | Quantity  (box 28.3 - 568.2 pt)
PL_INFO_X = 346.1                        # left edge of the No / Shipper Name / ... block


def build_pl_pdf(row, items):
    """Packing List laid out like the company's Excel sample: letterhead, 'Packing List' (20 pt, bold,
    underlined), To + address on the left and No / Shipper Name / Date / TC Type / Vessel Name / PO No
    on the right, the item table under a single header box (no column lines), then the bold
    'PO No :' line and 'Dimensions and Weight :'. Arial-metric text, 11 pt, 14.5 pt line pitch."""
    company = row["company"] or "pte"
    no = row["pl_no"]
    buf = io.BytesIO()
    doc, header, _hh = make_doc(buf, A4, EXP_LEFT - 6, A4[0] - EXP_RIGHT - 6, 12, 40, company, f"Packing List {no}")
    x0 = doc.leftMargin + 6                                       # where frame content starts on the page
    base = ParagraphStyle("pl_base", fontName="Helvetica", fontSize=11, leading=14.5, textColor=NAVY)
    bold = ParagraphStyle("pl_bold", parent=base, fontName="Helvetica-Bold")
    title = ParagraphStyle("pl_title", parent=bold, fontSize=20, leading=24, alignment=TA_CENTER,
                           rightIndent=2 * (x0 + (doc.width - 12) / 2 - A4[0] / 2))  # centred on the page
    ctr = ParagraphStyle("pl_ctr", parent=base, alignment=TA_CENTER)
    head_c = ParagraphStyle("pl_head_c", parent=bold, alignment=TA_CENTER)
    ind = ParagraphStyle("pl_ind", parent=base, leftIndent=PL_BOX_LEFT + PL_COLS[0] + 2.5 - x0)

    def txt(v):
        return escape(str(v or "")).replace("\n", "<br/>")

    date = fmt_date(row["pl_date"]).replace("-", "/")
    lines = [f"No : {no}", f"Shipper Name : {row['shipper_name'] or ''}", f"Date : {date}",
             f"TC Type : {row['tc_type'] or ''}", f"Vessel Name : {row['vessel_name'] or ''}",
             f"PO No : {row['po_no'] or ''}"]
    text_left = 30.8
    left_w = PL_INFO_X - text_left
    info = Table([[Paragraph(txt(row["to_address"]), base), Paragraph("<br/>".join(txt(l) for l in lines)
                                                                 + f"<br/><b>{escape(DO_BANNER)}</b>", base)]],
                 colWidths=[left_w - 8, A4[0] - PL_INFO_X - 20])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 0), ("LEFTPADDING", (1, 0), (1, 0), 8)]))
    info.hAlign = "LEFT"

    rows = [[Paragraph("#", head_c), Paragraph("Item description", bold), Paragraph("Part No", head_c),
             Paragraph("Quantity", head_c)]]
    for it in items:
        rows.append([Paragraph(txt(it["sno"]), ctr), Paragraph(rich(it["description"]), base),
                     Paragraph(txt(it["part_no"]), ctr), Paragraph(fmt_qty(it["qty"]), ctr)])
    items_t = Table(rows, colWidths=PL_COLS, repeatRows=1)
    items_t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, 0), 0.8, colors.black),                 # header box only, no column lines
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.5), ("RIGHTPADDING", (0, 0), (-1, -1), 2.5),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, 0), 0.45), ("BOTTOMPADDING", (0, 0), (-1, 0), 0.45),
        ("TOPPADDING", (0, 1), (-1, 1), 0)]))
    items_t.hAlign = "LEFT"

    foot = [Spacer(1, 29.1), Paragraph(f"PO No : {txt(row['po_no'])}", ParagraphStyle("pl_ind_b", parent=ind,
                                                                                     fontName="Helvetica-Bold")),
            Spacer(1, 29), Paragraph("Dimensions and Weight :", ind)]
    for l in (row["dimensions"] or "").replace("\r", "").split("\n"):
        foot.append(Paragraph(txt(l), ind) if l.strip() else Spacer(1, 6))
    story = [NextPageTemplate("later"), header, Spacer(1, 15), Paragraph("<u>Packing List</u>", title),
             Spacer(1, 15.7), _Indent(Paragraph("To", bold), text_left - x0), _Indent(info, text_left - x0),
             Spacer(1, 29.1), _IndentSplit(items_t, PL_BOX_LEFT - x0), KeepTogether(foot)]
    doc.build(story)
    buf.seek(0)
    return buf


class _Indent(Spacer):
    """Wraps a flowable and draws it `left` pt in from the frame's left edge."""

    def __init__(self, flowable, left):
        Spacer.__init__(self, 1, 1)
        self.f, self.left = flowable, left

    def wrap(self, aw, ah):
        w, h = self.f.wrap(aw - self.left, ah)
        self.width, self.height = aw, h
        return aw, h

    def draw(self):
        self.f.drawOn(self.canv, self.left, 0)


class _IndentSplit(_Indent):
    """_Indent that can run over a page break (for long item tables)."""

    def split(self, aw, ah):
        return [_IndentSplit(f, self.left) for f in self.f.split(aw - self.left, ah)]


# --------------------------------------------------------------------------
# Job orders
# --------------------------------------------------------------------------
def job_for_quotation(db, qid):
    return db.execute("SELECT * FROM job_orders WHERE quotation_id = ?", (qid,)).fetchone()


def load_job(db, jid):
    jo = db.execute("SELECT * FROM job_orders WHERE id = ?", (jid,)).fetchone()
    if jo is None:
        abort(404)
    q = db.execute("SELECT * FROM quotations WHERE id = ?", (jo["quotation_id"],)).fetchone()
    items = db.execute("SELECT * FROM job_items WHERE job_id = ? ORDER BY sno", (jid,)).fetchall()
    return jo, q, items


JO_PATTERN = re.compile(r"^" + re.escape(JO_PREFIX) + r"(\d{2})-(\d+)$", re.I)


def next_jo_no(db, year):
    """Automatic job order number: JO 27-0001, JO 27-0002 ... JO 28-0001 in 2028.
    Continues from the highest number used that year (even if numbers were edited)."""
    yy = f"{series_yy(year):02d}"
    top = 0
    for (no,) in db.execute("SELECT jo_no FROM job_orders WHERE jo_no LIKE ?", (JO_PREFIX + yy + "-%",)):
        m = JO_PATTERN.match(no)
        if m and m.group(1) == yy:
            top = max(top, int(m.group(2)))
    return f"{JO_PREFIX}{yy}-{top + 1:04d}"


def jo_no_taken(db, no, exclude_id=None):
    row = db.execute("SELECT id FROM job_orders WHERE jo_no = ? COLLATE NOCASE", (no,)).fetchone()
    return bool(row) and row["id"] != exclude_id


def save_job_items(db, jid, items):
    db.execute("DELETE FROM job_items WHERE job_id = ?", (jid,))
    db.executemany(
        "INSERT INTO job_items (job_id, sno, description, part_no, qty, unit_price, line_total) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(jid, n, it["description"], it.get("part_no", ""), str(it["qty"]), str(it["unit_price"]),
          str(it["line_total"])) for n, it in enumerate(items, start=1)])


def valid_iso(text):
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.route("/")
def index():
    db = get_db()
    search = request.args.get("q", "").strip()
    sql = ("SELECT q.*, j.id AS jo_id, j.jo_no FROM quotations q "
           "LEFT JOIN job_orders j ON j.quotation_id = q.id WHERE q.company = ?")
    args = [active_company()["key"]]
    if search:
        sql += (" AND (CAST(q.quote_no AS TEXT) LIKE ? OR q.to_address LIKE ? "
                "OR q.your_ref LIKE ? OR j.jo_no LIKE ?)")
        args += [f"%{search}%"] * 4
    rows = db.execute(sql + " ORDER BY q.id DESC", args).fetchall()
    return render_template("index.html", rows=rows, search=search)


def masters_ctx(company_key=None):
    db = get_db()
    customers, tcs = masters(db)
    company_key = company_key or session.get("company")
    return {"customers": [dict(c) for c in customers], "tc_types": [t["name"] for t in tcs],
            # the T&C options, with the company-specific words filled in ({currency} / {delivery} are
            # filled in by the page when an option is picked)
            "terms_opts": [{"key": t["key"], "name": t["name"], "body": resolve_terms(t["body"], company_key)}
                           for t in terms_options(db)],
            "default_terms_key": co(company_key).get("default_terms", "standard")}


def quote_items_from_form(f):
    """The item rows exactly as typed (to show the form again after an error)."""
    descs = f.getlist("description")
    parts = f.getlist("part_no")
    if len(parts) != len(descs):
        parts = [""] * len(descs)
    return [{"description": d, "part_no": pn, "qty": q, "unit_price": p} for d, pn, q, p in
            zip(descs, parts, f.getlist("qty"), f.getlist("unit_price"))]


def blank_form():
    today = date.today()
    c = active_company()
    return {"to_address": "", "q_date": today.isoformat(),
            "valid_till": (today + timedelta(days=30)).isoformat(), "your_ref": "E mail",
            "tc_type": "", "vessel_name": "", "currency": c["currency"],
            "terms": default_terms_for(get_db(), c["key"], c["currency"]),
            "terms_type": c.get("default_terms", "standard"), "remarks": "",
            "discount_type": "percent", "discount_value": "0",
            "quote_no": next_quote_no(get_db())}


@app.route("/new", methods=["GET", "POST"])
def new():
    db = get_db()
    if request.method == "POST":
        data, items, errors = parse_form(request.form)
        data["company"] = active_company()["key"]
        if not errors:
            qid = save_quotation(db, data, items)
            flash("Quotation saved.", "ok")
            return redirect(url_for("view", qid=qid))
        for e in errors:
            flash(e, "err")
        form = {k: request.form.get(k, "") for k in blank_form()}
        form["discount_value"] = request.form.get("discount_value", "0")
        item_list = quote_items_from_form(request.form)
        return render_template("form.html", q=form, items=item_list or [{}],
                               suggested_no=request.form.get("suggested_quote_no", ""),
                               editing=False, currencies=CURRENCIES, **masters_ctx())
    form = blank_form()
    return render_template("form.html", q=form, items=[{}], suggested_no=form["quote_no"],
                           editing=False, currencies=CURRENCIES, **masters_ctx())


@app.route("/duplicate/<int:qid>")
def duplicate(qid):
    db = get_db()
    q, items = load_quotation(db, qid)
    form = dict(q)
    today = date.today()
    form.update(q_date=today.isoformat(), valid_till=(today + timedelta(days=30)).isoformat(),
                quote_no=next_quote_no(db))
    return render_template("form.html", q=form, items=[dict(i) for i in items],
                           suggested_no=form["quote_no"], editing=False,
                           currencies=CURRENCIES, **masters_ctx())


@app.route("/edit/<int:qid>", methods=["GET", "POST"])
def edit(qid):
    db = get_db()
    q, items = load_quotation(db, qid)
    if job_for_quotation(db, qid):
        flash("This quotation has been accepted and converted to a job order, so it can no longer be edited.", "err")
        return redirect(url_for("view", qid=qid))
    if request.method == "POST":
        data, new_items, errors = parse_form(request.form, qid)
        if not errors:
            save_quotation(db, data, new_items, qid=qid)
            flash("Quotation updated.", "ok")
            return redirect(url_for("view", qid=qid))
        for e in errors:
            flash(e, "err")
        form = {k: request.form.get(k, "") for k in blank_form()}
        form["discount_value"] = request.form.get("discount_value", "0")
        item_list = quote_items_from_form(request.form)
        return render_template("form.html", q=form, items=item_list or [{}],
                               editing=True, qid=qid, currencies=CURRENCIES, **masters_ctx())
    return render_template("form.html", q=dict(q), items=[dict(i) for i in items],
                           editing=True, qid=qid, currencies=CURRENCIES, **masters_ctx(q["company"]))


@app.route("/quotation/<int:qid>")
def view(qid):
    db = get_db()
    q, items = load_quotation(db, qid)
    return render_template("view.html", q=q, items=items, jo=job_for_quotation(db, qid))


@app.route("/quotation/<int:qid>/number", methods=["POST"])
def quote_renumber(qid):
    db = get_db()
    load_quotation(db, qid)
    raw = request.form.get("quote_no", "").strip()
    new_no = parse_quote_no(raw)
    if new_no is None:
        flash(QUOTE_NO_ERR, "err")
    elif quote_no_taken(db, new_no, qid):
        flash(f"Quotation number {raw} already exists.", "err")
    else:
        db.execute("UPDATE quotations SET quote_no = ? WHERE id = ?", (new_no, qid))
        db.commit()
        flash("Quotation number changed.", "ok")
    return redirect(url_for("view", qid=qid))


@app.route("/pdf/<int:qid>")
def pdf(qid):
    q, items = load_quotation(get_db(), qid)
    buf = build_pdf(q, items)
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=request.args.get("download") == "1",
                     download_name=f"Quotation_{q['quote_no']}.pdf")


@app.route("/accept/<int:qid>", methods=["GET", "POST"])
def accept(qid):
    """Quotation accepted by the client -> create its job order (a copy that can be edited)."""
    db = get_db()
    q, items = load_quotation(db, qid)
    existing = job_for_quotation(db, qid)
    if existing:
        flash(f"This quotation is already job order {existing['jo_no']}.", "ok")
        return redirect(url_for("job_view", jid=existing["id"]))
    form = {"jo_date": date.today().isoformat(), "po_number": "", "jo_no": "", "suggested": ""}
    if request.method == "POST":
        form = {"jo_date": request.form.get("jo_date", "").strip(),
                "po_number": request.form.get("po_number", "").strip(),
                "jo_no": request.form.get("jo_no", "").strip(),
                "suggested": request.form.get("suggested_no", "").strip()}
        custom = bool(form["jo_no"]) and form["jo_no"] != form["suggested"]
        if not valid_iso(form["jo_date"]):
            flash("Choose the job order date.", "err")
        elif len(form["jo_no"]) > 30:
            flash("Job order number is too long.", "err")
        elif custom and jo_no_taken(db, form["jo_no"]):
            flash(f"Job order number {form['jo_no']} already exists.", "err")
        else:
            for _attempt in range(3):  # retry if two people convert at the same moment
                no = form["jo_no"] if custom else next_jo_no(db, int(form["jo_date"][:4]))
                try:
                    cur = db.execute(
                        "INSERT INTO job_orders (jo_no, quotation_id, jo_date, po_number, vessel_name, remarks, "
                        "discount_type, discount_value, subtotal, discount_amt, total, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (no, qid, form["jo_date"], form["po_number"], q["vessel_name"] or "", q["remarks"] or "",
                         q["discount_type"], q["discount_value"], q["subtotal"],
                         q["discount_amt"], q["total"],
                         datetime.now().isoformat(timespec="seconds")))
                    jid = cur.lastrowid
                    db.executemany(
                        "INSERT INTO job_items (job_id, sno, description, part_no, qty, unit_price, line_total) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(jid, i["sno"], i["description"], i["part_no"], i["qty"], i["unit_price"], i["line_total"])
                         for i in items])
                    db.commit()
                    flash(f"Quotation {q['quote_no']} accepted. Job order {no} created - "
                          "you can edit the items below if needed.", "ok")
                    return redirect(url_for("job_view", jid=jid))
                except sqlite3.IntegrityError:
                    db.rollback()
                    if job_for_quotation(db, qid):  # someone else just converted it
                        return redirect(url_for("view", qid=qid))
                    if custom:
                        flash(f"Job order number {no} already exists.", "err")
                        break
            else:
                flash("Could not allocate a job order number. Please try again.", "err")
    year = int(form["jo_date"][:4]) if valid_iso(form["jo_date"]) else date.today().year
    suggested = next_jo_no(db, year)
    if not form["suggested"]:
        form["suggested"] = suggested
    if not form["jo_no"]:
        form["jo_no"] = suggested
    return render_template("accept.html", q=q, items=items, form=form)


def propagate_job_po(db, jid, old_po, new_po):
    """The Customer PO No. is copied onto each delivery order and tax invoice when it is created.
    When it is changed on the job order, carry the change to those that still show the OLD number
    (including blank ones). A document whose Customer PO No. was edited by hand keeps its own value."""
    dos = db.execute("SELECT id, po_number FROM delivery_orders WHERE job_id = ?", (jid,)).fetchall()
    invs = db.execute("SELECT i.id, i.po_number FROM invoices i JOIN delivery_orders d ON d.id = i.delivery_id "
                      "WHERE d.job_id = ?", (jid,)).fetchall()
    n_do = db.execute("UPDATE delivery_orders SET po_number = ? WHERE job_id = ? AND po_number = ?",
                      (new_po, jid, old_po)).rowcount
    n_inv = db.execute("UPDATE invoices SET po_number = ? WHERE po_number = ? AND delivery_id IN "
                       "(SELECT id FROM delivery_orders WHERE job_id = ?)", (new_po, old_po, jid)).rowcount
    return po_change_message(old_po, new_po, n_do, len(dos) - n_do, n_inv, len(invs) - n_inv)


def po_change_message(old_po, new_po, n_do, kept_do, n_inv, kept_inv):
    shown = lambda v: f"'{v}'" if v else "(blank)"
    msg = f"Customer PO No. changed from {shown(old_po)} to {shown(new_po)}"
    parts = []
    if n_do:
        parts.append(f"{n_do} delivery order(s)")
    if n_inv:
        parts.append(f"{n_inv} tax invoice(s)")
    msg += (" and also updated on " + " and ".join(parts)) if parts else ""
    kept = kept_do + kept_inv
    if kept:
        msg += f" ({kept} document(s) with a different Customer PO No. were left as they are)"
    return msg + "."


@app.route("/jobs")
def jobs():
    db = get_db()
    search = request.args.get("q", "").strip()
    sql = ("SELECT j.*, q.quote_no, q.to_address FROM job_orders j "
           "JOIN quotations q ON q.id = j.quotation_id WHERE q.company = ?")
    args = [active_company()["key"]]
    if search:
        sql += (" AND (j.jo_no LIKE ? OR j.po_number LIKE ? OR q.to_address LIKE ? "
                "OR CAST(q.quote_no AS TEXT) LIKE ?)")
        args += [f"%{search}%"] * 4
    rows = db.execute(sql + " ORDER BY j.id DESC", args).fetchall()
    return render_template("jobs.html", rows=rows, search=search)


@app.route("/job/<int:jid>", methods=["GET", "POST"])
def job_view(jid):
    """Open / edit a job order: number, date, PO, items (description, qty, price) and remarks."""
    db = get_db()
    jo, q, items = load_job(db, jid)
    deliveries = db.execute("SELECT * FROM delivery_orders WHERE job_id = ? ORDER BY id", (jid,)).fetchall()
    if request.method == "POST":
        errors = []
        jo_no = request.form.get("jo_no", "").strip()
        jo_date = request.form.get("jo_date", "").strip()
        if not jo_no:
            errors.append("Job order number cannot be empty.")
        elif len(jo_no) > 30:
            errors.append("Job order number is too long.")
        elif jo_no_taken(db, jo_no, jid):
            errors.append(f"Job order number {jo_no} already exists.")
        if not valid_iso(jo_date):
            errors.append("Choose the job order date.")
        new_items, m = parse_lines(request.form, errors)
        # the discount is no longer shown / edited on the job order; whatever came from the quotation is kept
        # (the tax invoice still reads it)
        dval = Decimal(jo["discount_value"] or 0)
        d_amt = m["subtotal"] * dval / 100 if jo["discount_type"] == "percent" else dval
        d_amt = money(min(d_amt, m["subtotal"]))
        m.update(discount_type=jo["discount_type"], discount_value=dval, discount_amt=d_amt,
                 total=money(m["subtotal"] - d_amt))
        remarks = request.form.get("remarks", "").strip()
        po = request.form.get("po_number", "").strip()
        vessel = request.form.get("vessel_name", "").strip()
        if not errors:
            old_po = jo["po_number"]
            db.execute(
                "UPDATE job_orders SET jo_no = ?, jo_date = ?, po_number = ?, vessel_name = ?, remarks = ?, "
                "discount_type = ?, discount_value = ?, subtotal = ?, discount_amt = ?, total = ? "
                "WHERE id = ?",
                (jo_no, jo_date, po, vessel, remarks, m["discount_type"], str(m["discount_value"]),
                 str(m["subtotal"]), str(m["discount_amt"]), str(m["total"]), jid))
            save_job_items(db, jid, new_items)
            msg = "Job order saved."
            if po != old_po:
                msg += " " + propagate_job_po(db, jid, old_po, po)
            db.commit()
            flash(msg, "ok")
            return redirect(url_for("job_view", jid=jid))
        for e in errors:
            flash(e, "err")
        shown = dict(jo)
        shown.update(jo_no=jo_no, jo_date=jo_date, po_number=po, vessel_name=vessel, remarks=remarks)
        item_list = quote_items_from_form(request.form)
        return render_template("job_view.html", jo=shown, q=q, items=item_list or [{}], deliveries=deliveries)
    return render_template("job_view.html", jo=dict(jo), q=q, items=[dict(i) for i in items],
                           deliveries=deliveries)


@app.route("/job/<int:jid>/delete", methods=["POST"])
def job_delete(jid):
    db = get_db()
    jo, q, items = load_job(db, jid)
    linked = (db.execute("SELECT COUNT(*) FROM delivery_orders WHERE job_id = ?", (jid,)).fetchone()[0]
              + db.execute("SELECT COUNT(*) FROM purchase_orders WHERE job_id = ?", (jid,)).fetchone()[0])
    if linked:
        flash(f"Job order {jo['jo_no']} has delivery orders linked to it. Delete those first.", "err")
        return redirect(url_for("job_view", jid=jid))
    try:
        db.execute("DELETE FROM job_orders WHERE id = ?", (jid,))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        flash(f"Job order {jo['jo_no']} is used by other documents and cannot be deleted.", "err")
        return redirect(url_for("job_view", jid=jid))
    flash(f"Job order {jo['jo_no']} deleted.", "ok")
    return redirect(url_for("jobs"))


@app.route("/job/<int:jid>/pdf")
def job_pdf(jid):
    jo, q, items = load_job(get_db(), jid)
    buf = build_pdf(q, items, jo=jo)
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=request.args.get("download") == "1",
                     download_name=f"JobOrder_{jo['jo_no']}.pdf")


# --------------------------------------------------------------------------
# Purchase orders (created from a job order; we buy from a supplier)
# --------------------------------------------------------------------------
def clean_address(text):
    return "\n".join(l.strip() for l in (text or "").replace("\r", "").strip().split("\n")).strip()


def save_supplier(db, address):
    """Save a purchase order 'To' address for future use. The first line is its name; saving an
    address whose name already exists UPDATES that saved supplier (this is how it is edited)."""
    address = clean_address(address)
    if not address:
        return
    name = address.split("\n")[0]
    row = db.execute("SELECT id FROM suppliers WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        try:
            db.execute("UPDATE suppliers SET address = ? WHERE id = ?", (address, row["id"]))
        except sqlite3.IntegrityError:      # the very same address is saved under another entry
            pass
    else:
        db.execute("INSERT OR IGNORE INTO suppliers (name, address) VALUES (?, ?)", (name, address))


PO_PATTERN = re.compile(r"^" + re.escape(PO_PREFIX) + r"(\d{2})(\d{2,})$", re.I)


def next_po_no(db, year=None):
    """Next purchase order number: PO 2701, PO 2702 ... (PO + year + running number, at least 2 digits).
    Stand-alone purchase orders and those made from a job order share this one sequence: it continues
    from the highest number of the year, however the earlier purchase orders were created."""
    yy = f"{series_yy(year):02d}"
    top = 0
    for (no,) in db.execute("SELECT po_no FROM purchase_orders WHERE po_no LIKE ?", (PO_PREFIX + yy + "%",)):
        m = PO_PATTERN.match(no or "")
        if m and m.group(1) == yy:
            top = max(top, int(m.group(2)))
    return f"{PO_PREFIX}{yy}{top + 1:02d}"


def po_no_taken(db, no, exclude_id=None):
    row = db.execute("SELECT id FROM purchase_orders WHERE po_no = ? COLLATE NOCASE", (no,)).fetchone()
    return bool(row) and row["id"] != exclude_id


def load_po(db, pid):
    po = db.execute("SELECT * FROM purchase_orders WHERE id = ?", (pid,)).fetchone()
    if po is None:
        abort(404)
    if po["job_id"]:
        jo, q, _items = load_job(db, po["job_id"])
    else:                                   # stand-alone purchase order: no job order / quotation
        jo, q = None, {"company": po["company"] or "pte", "id": None, "tc_type": ""}
    items = db.execute("SELECT * FROM po_items WHERE po_id = ? ORDER BY sno", (pid,)).fetchall()
    return po, jo, q, items


def parse_po(form, errors):
    """PO items (description, part no, quantity, unit price) + currency.
    Total Price of an item = Quantity x Unit Price; the PO total is the sum of the item totals."""
    items = []
    for i, (desc, part, q, p) in enumerate(zip(form.getlist("description"), form.getlist("part_no"),
                                               form.getlist("qty"), form.getlist("unit_price")), start=1):
        desc, part = desc.strip(), part.strip()
        if not desc and not part and not str(q).strip() and not str(p).strip():
            continue
        if not desc:
            errors.append(f"Item {i}: description is required.")
        qty = to_decimal(q, f"Item {i} quantity", errors, allow_zero=False)
        price = to_decimal(p or "0", f"Item {i} unit price", errors)
        items.append({"description": desc, "part_no": part, "qty": qty, "unit_price": money(price),
                      "line_total": money(qty * price)})
    if not items:
        errors.append("Add at least one item.")
    cur = form.get("currency", "")
    if cur not in PO_CURRENCIES:
        errors.append("Choose the currency (" + " / ".join(PO_CURRENCIES) + ").")
    subtotal = money(sum((it["line_total"] for it in items), Decimal(0)))
    rate = to_decimal(form.get("gst_rate") or "0", "GST %", errors)
    if rate > 100:
        errors.append("GST % cannot exceed 100.")
    gst = money(subtotal * rate / 100)
    return items, {"currency": cur, "subtotal": subtotal, "gst_rate": rate, "gst_amt": gst,
                   "total": money(subtotal + gst)}


def save_po_items(db, pid, items):
    db.execute("DELETE FROM po_items WHERE po_id = ?", (pid,))
    db.executemany(
        "INSERT INTO po_items (po_id, sno, description, part_no, qty, unit_price, line_total) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(pid, n, it["description"], it["part_no"], str(it["qty"]), str(it["unit_price"]), str(it["line_total"]))
         for n, it in enumerate(items, start=1)])


def po_form_fields(f):
    """The typed-in header fields of a purchase order (nothing is filled in automatically)."""
    h = {k: f.get(k, "").strip() for k in ("po_no", "po_date", "your_ref", "jo_ref", "tc_type")}
    h["to_address"] = clean_address(f.get("to_address", ""))
    h["remarks"] = f.get("remarks", "").replace("\r", "").strip()
    return h


def po_form_errors(db, h, exclude_id=None):
    errs = []
    if not h["po_no"]:
        errs.append("Type the purchase order number.")
    elif len(h["po_no"]) > 30:
        errs.append("Purchase order number is too long.")
    elif po_no_taken(db, h["po_no"], exclude_id):
        errs.append(f"Purchase order number {h['po_no']} already exists.")
    if not valid_iso(h["po_date"]):
        errs.append("Choose the purchase order date.")
    if not h["to_address"]:
        errs.append("Type the supplier's name and address.")
    return errs


def po_items_from_form(f):
    return [{"description": a, "part_no": p, "qty": b, "unit_price": c} for a, p, b, c in
            zip(f.getlist("description"), f.getlist("part_no"), f.getlist("qty"), f.getlist("unit_price"))]


def po_page_ctx(db, jo, q):
    suppliers = db.execute("SELECT * FROM customers ORDER BY name COLLATE NOCASE").fetchall()
    tc_options = all_tc_options(db)
    return dict(jo=jo, q=q, suppliers=[dict(x) for x in suppliers], po_currencies=PO_CURRENCIES, tc_options=tc_options, suggested_no=next_po_no(db))


@app.route("/purchase/new", defaults={"jid": None}, methods=["GET", "POST"])
def po_new(jid):
    """Create a purchase order - for a job order, or on its own (no job order, jid=None) for the
    active company. Everything is typed by hand; only the supplier address can be picked from the
    saved list (and saved for future use)."""
    db = get_db()
    if jid is None:
        jo, q = None, {"company": active_company()["key"], "id": None, "tc_type": ""}
    else:
        jo, q, _jitems = load_job(db, jid)
    blank = {"id": None, "po_no": "", "po_date": "", "to_address": "", "your_ref": "", "jo_ref": "",
             "tc_type": "", "currency": "", "gst_rate": "0", "remarks": ""}
    if request.method == "POST":
        f = request.form
        h = po_form_fields(f)
        errors = po_form_errors(db, h)
        items, m = parse_po(f, errors)
        if not errors:
            try:
                cur = db.execute(
                    "INSERT INTO purchase_orders (po_no, job_id, po_date, to_address, your_ref, jo_ref, tc_type, "
                    "currency, subtotal, gst_rate, gst_amt, total, created_at, company, remarks) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (h["po_no"], jid, h["po_date"], h["to_address"], h["your_ref"], h["jo_ref"], h["tc_type"],
                     m["currency"], str(m["subtotal"]), str(m["gst_rate"]), str(m["gst_amt"]), str(m["total"]),
                     datetime.now().isoformat(timespec="seconds"), q["company"], h["remarks"]))
            except sqlite3.IntegrityError:      # someone saved the same number a moment ago
                db.rollback()
                errors.append(f"Purchase order number {h['po_no']} already exists.")
            else:
                pid = cur.lastrowid
                save_po_items(db, pid, items)
                remember_address(db, h["to_address"])
                db.commit()
                flash(f"Purchase order {h['po_no']} saved.", "ok")
                return redirect(url_for("po_view", pid=pid))
        for e in errors:
            flash(e, "err")
        blank.update(h)
        blank.update(currency=f.get("currency", ""), gst_rate=f.get("gst_rate", "0"))
        return render_template("po_view.html", po=blank, items=po_items_from_form(f) or [{}], is_new=True,
                               **po_page_ctx(db, jo, q))
    if jo is None:                      # stand-alone: the number is filled in automatically (editable)
        blank["po_no"] = next_po_no(db)
    return render_template("po_view.html", po=blank, items=[{}], is_new=True, **po_page_ctx(db, jo, q))


@app.route("/purchase-orders")
def purchase_orders():
    db = get_db()
    search = request.args.get("q", "").strip()
    sql = ("SELECT p.*, j.jo_no, j.id AS jo_id FROM purchase_orders p "
           "LEFT JOIN job_orders j ON j.id = p.job_id WHERE p.company = ?")
    args = [active_company()["key"]]
    if search:
        sql += " AND (p.po_no LIKE ? OR j.jo_no LIKE ? OR p.to_address LIKE ? OR p.your_ref LIKE ?)"
        args += [f"%{search}%"] * 4
    rows = db.execute(sql + " ORDER BY p.id DESC", args).fetchall()
    return render_template("purchase_orders.html", rows=rows, search=search)


@app.route("/purchase/<int:pid>", methods=["GET", "POST"])
def po_view(pid):
    """Open / edit a purchase order: every field (number, date, supplier, refs, items, currency)."""
    db = get_db()
    po, jo, q, items = load_po(db, pid)
    ctx = po_page_ctx(db, jo, q)
    if request.method == "POST":
        f = request.form
        h = po_form_fields(f)
        errors = po_form_errors(db, h, pid)
        new_items, m = parse_po(f, errors)
        if not errors:
            try:
                db.execute("UPDATE purchase_orders SET po_no=?, po_date=?, to_address=?, your_ref=?, jo_ref=?, "
                           "tc_type=?, currency=?, subtotal=?, gst_rate=?, gst_amt=?, total=?, remarks=? WHERE id=?",
                           (h["po_no"], h["po_date"], h["to_address"], h["your_ref"], h["jo_ref"], h["tc_type"],
                            m["currency"], str(m["subtotal"]), str(m["gst_rate"]), str(m["gst_amt"]),
                            str(m["total"]), h["remarks"], pid))
            except sqlite3.IntegrityError:
                db.rollback()
                errors.append(f"Purchase order number {h['po_no']} already exists.")
            else:
                save_po_items(db, pid, new_items)
                remember_address(db, h["to_address"])
                db.commit()
                flash("Purchase order saved.", "ok")
                return redirect(url_for("po_view", pid=pid))
        for e in errors:
            flash(e, "err")
        shown = dict(po)
        shown.update(h)
        shown.update(currency=f.get("currency", ""), gst_rate=f.get("gst_rate", "0"))
        return render_template("po_view.html", po=shown, items=po_items_from_form(f) or [{}], is_new=False, **ctx)
    return render_template("po_view.html", po=dict(po), items=[dict(i) for i in items], is_new=False, **ctx)


@app.route("/purchase/<int:pid>/pdf")
def po_pdf(pid):
    po, jo, q, items = load_po(get_db(), pid)
    buf = build_po_pdf(q, po, items)
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=request.args.get("download") == "1",
                     download_name=f"PurchaseOrder_{po['po_no']}.pdf")


@app.route("/purchase/<int:pid>/delete", methods=["POST"])
def po_delete(pid):
    db = get_db()
    po, jo, q, items = load_po(db, pid)
    db.execute("DELETE FROM purchase_orders WHERE id = ?", (pid,))
    db.commit()
    flash(f"Purchase order {po['po_no']} deleted.", "ok")
    return redirect(url_for("job_view", jid=jo["id"]) if jo else url_for("purchase_orders"))


# --------------------------------------------------------------------------
# Delivery orders (created from a job order)
# --------------------------------------------------------------------------
DO_PATTERN = re.compile(r"^" + re.escape(DO_PREFIX) + r"(\d{2})-(\d+)$", re.I)


def next_do_no(db, year):
    """Automatic delivery order number: DO 27-0001, DO 27-0002 ... DO 28-0001 in 2028."""
    yy = f"{series_yy(year):02d}"
    top = 0
    for (no,) in db.execute("SELECT do_no FROM delivery_orders WHERE do_no LIKE ?",
                            (DO_PREFIX + yy + "-%",)):
        m = DO_PATTERN.match(no)
        if m and m.group(1) == yy:
            top = max(top, int(m.group(2)))
    return f"{DO_PREFIX}{yy}-{top + 1:04d}"


def do_no_taken(db, no, exclude_id=None):
    row = db.execute("SELECT id FROM delivery_orders WHERE do_no = ? COLLATE NOCASE", (no,)).fetchone()
    return bool(row) and row["id"] != exclude_id


def load_delivery(db, did):
    d = db.execute("SELECT d.*, j.jo_no FROM delivery_orders d JOIN job_orders j ON j.id = d.job_id "
                   "WHERE d.id = ?", (did,)).fetchone()
    if d is None:
        abort(404)
    jo = db.execute("SELECT * FROM job_orders WHERE id = ?", (d["job_id"],)).fetchone()
    q = dict(db.execute("SELECT * FROM quotations WHERE id = ?", (jo["quotation_id"],)).fetchone())
    # the delivery order's own To address / TC type, once edited on the delivery order, replace the
    # quotation's everywhere the delivery order is shown (page, PDF, new tax invoices)
    for col in ("to_address", "tc_type"):
        if d[col] is not None:
            q[col] = d[col]
    items = db.execute("SELECT * FROM delivery_items WHERE delivery_id = ? ORDER BY sno", (did,)).fetchall()
    return d, jo, q, items


def parse_do_lines(form, errors):
    """Delivery order rows: description, part no, quantity (no prices on a delivery order)."""
    items = []
    for i, (desc, part, q) in enumerate(zip(form.getlist("description"), form.getlist("part_no"),
                                            form.getlist("qty")), start=1):
        desc, part = desc.strip(), part.strip()
        if not desc and not part and not str(q).strip():
            continue
        if not desc:
            errors.append(f"Item {i}: description is required.")
        qty = to_decimal(q, f"Item {i} quantity", errors, allow_zero=False)
        items.append({"description": desc, "part_no": part, "qty": qty})
    if not items:
        errors.append("Add at least one item.")
    return items


def save_delivery_items(db, did, items):
    db.execute("DELETE FROM delivery_items WHERE delivery_id = ?", (did,))
    db.executemany(
        "INSERT INTO delivery_items (delivery_id, sno, description, part_no, qty, unit_price, line_total) "
        "VALUES (?, ?, ?, ?, ?, '0', '0')",
        [(did, n, it["description"], it["part_no"], str(it["qty"]))
         for n, it in enumerate(items, start=1)])


@app.route("/job/<int:jid>/deliver", methods=["GET", "POST"])
def deliver(jid):
    """Create a delivery order from a job order (copies the job order's saved items)."""
    db = get_db()
    jo, q, items = load_job(db, jid)
    form = {"do_date": date.today().isoformat(), "po_number": jo["po_number"], "do_no": "", "suggested": "",
            "shipper_name": "", "vessel_name": jo["vessel_name"] or ""}
    if request.method == "POST":
        form = {"do_date": request.form.get("do_date", "").strip(),
                "po_number": request.form.get("po_number", "").strip(),
                "do_no": request.form.get("do_no", "").strip(),
                "suggested": request.form.get("suggested_no", "").strip(),
                "shipper_name": request.form.get("shipper_name", "").strip(),
                "vessel_name": request.form.get("vessel_name", "").strip()}
        custom = bool(form["do_no"]) and form["do_no"] != form["suggested"]
        if not valid_iso(form["do_date"]):
            flash("Choose the delivery order date.", "err")
        elif len(form["do_no"]) > 30:
            flash("Delivery order number is too long.", "err")
        elif custom and do_no_taken(db, form["do_no"]):
            flash(f"Delivery order number {form['do_no']} already exists.", "err")
        else:
            for _attempt in range(3):  # retry if two people create one at the same moment
                no = form["do_no"] if custom else next_do_no(db, int(form["do_date"][:4]))
                try:
                    cur = db.execute(
                        "INSERT INTO delivery_orders (do_no, job_id, do_date, po_number, shipper_name, "
                        "vessel_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (no, jid, form["do_date"], form["po_number"], form["shipper_name"],
                         form["vessel_name"], datetime.now().isoformat(timespec="seconds")))
                    did = cur.lastrowid
                    db.executemany(
                        "INSERT INTO delivery_items (delivery_id, sno, description, part_no, qty, unit_price, line_total) "
                        "VALUES (?, ?, ?, ?, ?, '0', '0')",
                        [(did, i["sno"], i["description"], i["part_no"], i["qty"]) for i in items])
                    db.commit()
                    flash(f"Delivery order {no} created from job order {jo['jo_no']} - "
                          "edit the items below (and add part numbers) if needed.", "ok")
                    return redirect(url_for("delivery_view", did=did))
                except sqlite3.IntegrityError:
                    db.rollback()
                    if custom:
                        flash(f"Delivery order number {no} already exists.", "err")
                        break
            else:
                flash("Could not allocate a delivery order number. Please try again.", "err")
    year = int(form["do_date"][:4]) if valid_iso(form["do_date"]) else date.today().year
    suggested = next_do_no(db, year)
    if not form["suggested"]:
        form["suggested"] = suggested
    if not form["do_no"]:
        form["do_no"] = suggested
    return render_template("do_new.html", jo=jo, q=q, items=items, form=form)


@app.route("/deliveries")
def deliveries():
    db = get_db()
    search = request.args.get("q", "").strip()
    sql = ("SELECT d.*, j.jo_no, COALESCE(d.to_address, q.to_address) AS to_address, q.currency "
           "FROM delivery_orders d "
           "JOIN job_orders j ON j.id = d.job_id JOIN quotations q ON q.id = j.quotation_id "
           "WHERE q.company = ?")
    args = [active_company()["key"]]
    if search:
        sql += (" AND (d.do_no LIKE ? OR j.jo_no LIKE ? OR d.po_number LIKE ? "
                "OR COALESCE(d.to_address, q.to_address) LIKE ? "
                "OR d.vessel_name LIKE ? OR d.shipper_name LIKE ?)")
        args += [f"%{search}%"] * 6
    rows = db.execute(sql + " ORDER BY d.id DESC", args).fetchall()
    return render_template("deliveries.html", rows=rows, search=search)


@app.route("/delivery/<int:did>", methods=["GET", "POST"])
def delivery_view(did):
    """Open / edit a delivery order: number, date, PO, items (description, qty, price), remarks, discount."""
    db = get_db()
    d, jo, q, items = load_delivery(db, did)
    if request.method == "POST":
        errors = []
        do_no = request.form.get("do_no", "").strip()
        do_date = request.form.get("do_date", "").strip()
        if not do_no:
            errors.append("Delivery order number cannot be empty.")
        elif len(do_no) > 30:
            errors.append("Delivery order number is too long.")
        elif do_no_taken(db, do_no, did):
            errors.append(f"Delivery order number {do_no} already exists.")
        if not valid_iso(do_date):
            errors.append("Choose the delivery order date.")
        to_addr = clean_address(request.form.get("to_address", ""))
        if not to_addr:
            errors.append("Enter the To address.")
        tc_type = request.form.get("tc_type", "").strip()
        new_items = parse_do_lines(request.form, errors)
        po = request.form.get("po_number", "").strip()
        shipper = request.form.get("shipper_name", "").strip()
        vessel = request.form.get("vessel_name", "").strip()
        if not errors:
            old_po = d["po_number"]
            db.execute(
                "UPDATE delivery_orders SET do_no = ?, do_date = ?, po_number = ?, shipper_name = ?, "
                "vessel_name = ?, to_address = ?, tc_type = ? WHERE id = ?",
                (do_no, do_date, po, shipper, vessel, to_addr, tc_type, did))
            remember_address(db, to_addr)
            if tc_type:
                db.execute("INSERT OR IGNORE INTO tc_types (name) VALUES (?)", (tc_type,))
            save_delivery_items(db, did, new_items)
            msg = "Delivery order saved."
            if po != old_po:
                total_inv = len(delivery_invoices(db, did))
                n_inv = db.execute("UPDATE invoices SET po_number = ? WHERE delivery_id = ? AND po_number = ?",
                                   (po, did, old_po)).rowcount
                if total_inv:
                    msg += " " + po_change_message(old_po, po, 0, 0, n_inv, total_inv - n_inv)
            db.commit()
            flash(msg, "ok")
            return redirect(url_for("delivery_view", did=did))
        for e in errors:
            flash(e, "err")
        shown = dict(d)
        shown.update(do_no=do_no, do_date=do_date, po_number=po, shipper_name=shipper, vessel_name=vessel)
        q.update(to_address=request.form.get("to_address", ""), tc_type=tc_type)
        item_list = [{"description": x, "part_no": pn, "qty": a} for x, pn, a in
                     zip(request.form.getlist("description"), request.form.getlist("part_no"),
                         request.form.getlist("qty"))]
        return render_template("do_view.html", dlv=shown, jo=jo, q=q, items=item_list or [{}],
                               invoices=delivery_invoices(db, did), **do_pickers(db))
    return render_template("do_view.html", dlv=dict(d), jo=jo, q=q, items=[dict(i) for i in items],
                           invoices=delivery_invoices(db, did), **do_pickers(db))


def do_pickers(db):
    """Saved addresses and TC types offered on the delivery order page."""
    return {"customers": saved_addresses(db),
            "tc_types": [t["name"] for t in db.execute("SELECT name FROM tc_types ORDER BY name COLLATE NOCASE")]}


@app.route("/delivery/<int:did>/pdf")
def delivery_pdf(did):
    d, jo, q, items = load_delivery(get_db(), did)
    buf = build_pdf(q, items, do=d)
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=request.args.get("download") == "1",
                     download_name=f"DeliveryOrder_{d['do_no']}.pdf")


@app.route("/delivery/<int:did>/delete", methods=["POST"])
def delivery_delete(did):
    db = get_db()
    d, jo, q, items = load_delivery(db, did)
    if delivery_invoices(db, did):
        flash("This delivery order has a tax invoice, so it cannot be deleted. Delete the invoice first.", "err")
        return redirect(url_for("delivery_view", did=did))
    db.execute("DELETE FROM delivery_orders WHERE id = ?", (did,))
    db.commit()
    flash(f"Delivery order {d['do_no']} deleted.", "ok")
    return redirect(url_for("job_view", jid=jo["id"]))


# --------------------------------------------------------------------------
# Tax invoices (created from a delivery order)
# --------------------------------------------------------------------------
INV_PATTERN = re.compile(r"^" + re.escape(INV_PREFIX) + r"(\d{2})-(\d+)$", re.I)


def next_inv_no(db, year):
    """Automatic tax invoice number: INV 27-0001, INV 27-0002 ... INV 28-0001 in 2028."""
    yy = f"{series_yy(year):02d}"
    top = 0
    for (no,) in db.execute("SELECT inv_no FROM invoices WHERE inv_no LIKE ?", (INV_PREFIX + yy + "-%",)):
        m = INV_PATTERN.match(no)
        if m and m.group(1) == yy:
            top = max(top, int(m.group(2)))
    return f"{INV_PREFIX}{yy}-{top + 1:04d}"


def inv_no_taken(db, no, exclude_id=None):
    row = db.execute("SELECT id FROM invoices WHERE inv_no = ? COLLATE NOCASE", (no,)).fetchone()
    return bool(row) and row["id"] != exclude_id


def delivery_invoices(db, did):
    return db.execute("SELECT * FROM invoices WHERE delivery_id = ? ORDER BY id", (did,)).fetchall()


def load_invoice(db, iid):
    inv = db.execute("SELECT * FROM invoices WHERE id = ?", (iid,)).fetchone()
    if inv is None:
        abort(404)
    d, jo, q, _items = load_delivery(db, inv["delivery_id"])
    items = db.execute("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY sno", (iid,)).fetchall()
    return inv, d, jo, q, items


def get_bank_cur(db, company_key, currency):
    """Bank details of a company for one currency, or '' when none are set up for it."""
    row = db.execute("SELECT bank_details FROM company_bank_cur WHERE company = ? AND currency = ?",
                     (company_key, currency)).fetchone()
    return (row["bank_details"] if row else "").strip()


def get_bank_for(db, company_key, currency):
    """Bank details for a tax invoice: the currency's own set when there is one, else the company's."""
    return get_bank_cur(db, company_key, currency) or get_bank(db, company_key)


def bank_choices(db, company_key):
    """The bank detail sets a tax invoice of this company can use: [(currency, text), ...] ([] when the
    company has only its general details)."""
    return [(c, get_bank_cur(db, company_key, c)) for c, _ in CURRENCY_BANKS.get(company_key, [])
            if get_bank_cur(db, company_key, c)]


def default_bank_currency(db, company_key, currency):
    """A new tax invoice starts with the set of its own currency when there is one ('' = general)."""
    return currency if currency in [c for c, _ in bank_choices(db, company_key)] else ""


def get_bank(db, company_key):
    row = db.execute("SELECT bank_details FROM company_bank WHERE company = ?", (company_key,)).fetchone()
    return row["bank_details"] if row else co(company_key)["bank_default"]


def parse_invoice_lines(form, errors):
    """Invoice rows (description, part no, qty, unit price) + discount + GST -> totals."""
    items = []
    rows = zip(form.getlist("description"), form.getlist("part_no"), form.getlist("qty"),
               form.getlist("unit_price"))
    for i, (desc, part, q, p) in enumerate(rows, start=1):
        desc, part = desc.strip(), part.strip()
        if not desc and not part and not str(q).strip() and not str(p).strip():
            continue
        if not desc:
            errors.append(f"Item {i}: description is required.")
        qty = to_decimal(q, f"Item {i} quantity", errors, allow_zero=False)
        price = to_decimal(p, f"Item {i} unit price", errors)
        items.append({"description": desc, "part_no": part, "qty": qty,
                      "unit_price": money(price), "line_total": money(qty * price)})
    if not items:
        errors.append("Add at least one item.")
    subtotal = money(sum((it["line_total"] for it in items), Decimal(0)))

    dtype = form.get("discount_type", "percent")
    if dtype not in ("percent", "amount"):
        dtype = "percent"
    dval = to_decimal(form.get("discount_value") or "0", "Discount", errors)
    if dtype == "percent":
        if dval > 100:
            errors.append("Discount % cannot exceed 100.")
        disc = money(subtotal * dval / 100)
    else:
        if dval > subtotal:
            errors.append("Discount cannot be more than the sub total.")
        disc = money(dval)
    rate = to_decimal(form.get("gst_rate") or "0", "GST %", errors)
    if rate > 100:
        errors.append("GST % cannot exceed 100.")
    taxable = money(subtotal - disc)
    gst = money(taxable * rate / 100)
    return items, {"discount_type": dtype, "discount_value": dval, "subtotal": subtotal,
                   "discount_amt": disc, "gst_rate": rate, "gst_amt": gst, "total": money(taxable + gst)}


def save_invoice_items(db, iid, items):
    db.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (iid,))
    db.executemany(
        "INSERT INTO invoice_items (invoice_id, sno, description, part_no, qty, unit_price, line_total) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(iid, n, it["description"], it["part_no"], str(it["qty"]), str(it["unit_price"]),
          str(it["line_total"])) for n, it in enumerate(items, start=1)])


@app.route("/delivery/<int:did>/invoice", methods=["GET", "POST"])
def invoice_new(did):
    """Create a tax invoice from a delivery order. Items, part numbers and quantities come from
    the delivery order; unit prices are taken from the job order's items."""
    db = get_db()
    d, jo, q, items = load_delivery(db, did)
    pay = re.search(r"(?im)^\s*payment terms\s*:\s*(.+)$", q["terms"] or "")
    form = {"inv_date": date.today().isoformat(), "po_number": d["po_number"], "inv_no": "",
            "suggested": "", "vessel_name": d["vessel_name"], "pay_terms": pay.group(1).strip() if pay else "",
            "location": "", "gst_rate": "0"}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in
                ("inv_date", "po_number", "inv_no", "vessel_name", "pay_terms", "location", "gst_rate")}
        form["suggested"] = request.form.get("suggested_no", "").strip()
        custom = bool(form["inv_no"]) and form["inv_no"] != form["suggested"]
        errs = []
        rate = to_decimal(form["gst_rate"] or "0", "GST %", errs)
        if rate > 100:
            errs.append("GST % cannot exceed 100.")
        if not valid_iso(form["inv_date"]):
            errs.append("Choose the invoice date.")
        if len(form["inv_no"]) > 30:
            errs.append("Invoice number is too long.")
        elif custom and inv_no_taken(db, form["inv_no"]):
            errs.append(f"Invoice number {form['inv_no']} already exists.")
        for e in errs:
            flash(e, "err")
        if not errs:
            # unit prices from the job order, matched on the item description
            job_items = db.execute("SELECT * FROM job_items WHERE job_id = ? ORDER BY sno", (jo["id"],)).fetchall()
            pool = {}
            for ji in job_items:
                pool.setdefault(ji["description"].strip(), []).append(ji["unit_price"])
            rows, missing = [], 0
            for it in items:
                prices = pool.get(it["description"].strip())
                price = money(prices.pop(0)) if prices else None
                if price is None:
                    missing += 1
                    price = Decimal(0)
                qty = Decimal(it["qty"])
                rows.append({"description": it["description"], "part_no": it["part_no"], "qty": qty,
                             "unit_price": price, "line_total": money(qty * price)})
            subtotal = money(sum((r["line_total"] for r in rows), Decimal(0)))
            dtype, dval = "percent", Decimal(0)
            if jo["discount_type"] == "percent":
                dval = Decimal(jo["discount_value"] or 0)
            disc = money(subtotal * dval / 100)
            taxable = money(subtotal - disc)
            gst = money(taxable * rate / 100)
            for _attempt in range(3):  # retry if two people create one at the same moment
                no = form["inv_no"] if custom else next_inv_no(db, int(form["inv_date"][:4]))
                try:
                    cur = db.execute(
                        "INSERT INTO invoices (inv_no, delivery_id, inv_date, po_number, vessel_name, pay_terms, "
                        "location, discount_type, discount_value, subtotal, discount_amt, gst_rate, gst_amt, "
                        "total, bank_details, bank_currency, created_at, to_address) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (no, did, form["inv_date"], form["po_number"], form["vessel_name"], form["pay_terms"],
                         form["location"], dtype, str(dval), str(subtotal), str(disc), str(rate), str(gst),
                         str(money(taxable + gst)), get_bank_for(db, q["company"], q["currency"]),
                         default_bank_currency(db, q["company"], q["currency"]),
                         datetime.now().isoformat(timespec="seconds"), q["to_address"]))
                    iid = cur.lastrowid
                    save_invoice_items(db, iid, rows)
                    db.commit()
                    msg = f"Tax invoice {no} created from delivery order {d['do_no']}."
                    if missing:
                        msg += (f" {missing} item(s) were not found on the job order (description changed) - "
                                "please enter their unit prices.")
                    if jo["discount_type"] != "percent" and Decimal(jo["discount_amt"] or 0) > 0:
                        msg += " The job order has a fixed discount - add it on the invoice if it applies."
                    flash(msg, "ok")
                    return redirect(url_for("invoice_view", iid=iid))
                except sqlite3.IntegrityError:
                    db.rollback()
                    if custom:
                        flash(f"Invoice number {no} already exists.", "err")
                        break
            else:
                flash("Could not allocate an invoice number. Please try again.", "err")
    year = int(form["inv_date"][:4]) if valid_iso(form["inv_date"]) else date.today().year
    suggested = next_inv_no(db, year)
    if not form["suggested"]:
        form["suggested"] = suggested
    if not form["inv_no"]:
        form["inv_no"] = suggested
    return render_template("inv_new.html", d=d, jo=jo, q=q, items=items, form=form)


@app.route("/invoices")
def invoices():
    db = get_db()
    search = request.args.get("q", "").strip()
    sql = ("SELECT i.*, d.do_no, d.id AS do_id, j.jo_no, COALESCE(NULLIF(i.to_address, ''), q.to_address) AS to_address, "
           "q.currency "
           "FROM invoices i JOIN delivery_orders d ON d.id = i.delivery_id "
           "JOIN job_orders j ON j.id = d.job_id JOIN quotations q ON q.id = j.quotation_id "
           "WHERE q.company = ?")
    args = [active_company()["key"]]
    if search:
        sql += (" AND (i.inv_no LIKE ? OR d.do_no LIKE ? OR j.jo_no LIKE ? OR i.po_number LIKE ? "
                "OR q.to_address LIKE ? OR i.vessel_name LIKE ?)")
        args += [f"%{search}%"] * 6
    rows = db.execute(sql + " ORDER BY i.id DESC", args).fetchall()
    return render_template("invoices.html", rows=rows, search=search)


@app.route("/invoice/<int:iid>", methods=["GET", "POST"])
def invoice_view(iid):
    """Open / edit a tax invoice: number, date, PO, vessel, terms, location, items, GST and which
    currency's bank details to print (Turbo Prime Pte Ltd: USD / SGD / JPY). The bank details
    themselves are not shown or edited here - only an administrator can change them (/bank)."""
    db = get_db()
    inv, d, jo, q, items = load_invoice(db, iid)
    choices = bank_choices(db, q["company"])
    bank_opts = [c for c, _ in choices]
    if request.method == "POST":
        errors = []
        f = request.form
        no, dt = f.get("inv_no", "").strip(), f.get("inv_date", "").strip()
        if not no:
            errors.append("Invoice number cannot be empty.")
        elif len(no) > 30:
            errors.append("Invoice number is too long.")
        elif inv_no_taken(db, no, iid):
            errors.append(f"Invoice number {no} already exists.")
        if not valid_iso(dt):
            errors.append("Choose the invoice date.")
        to_addr = clean_address(f.get("to_address", ""))
        if not to_addr:
            errors.append("Type the name and address the invoice is addressed to.")
        new_items, m = parse_invoice_lines(f, errors)
        if not errors:
            db.execute("UPDATE invoices SET to_address = ? WHERE id = ?", (to_addr, iid))
            remember_address(db, to_addr)
            db.execute(
                "UPDATE invoices SET inv_no=?, inv_date=?, po_number=?, vessel_name=?, pay_terms=?, location=?, "
                "discount_type=?, discount_value=?, subtotal=?, discount_amt=?, gst_rate=?, gst_amt=?, total=? "
                "WHERE id=?",
                (no, dt, f.get("po_number", "").strip(), f.get("vessel_name", "").strip(),
                 f.get("pay_terms", "").strip(), f.get("location", "").strip(), m["discount_type"],
                 str(m["discount_value"]), str(m["subtotal"]), str(m["discount_amt"]), str(m["gst_rate"]),
                 str(m["gst_amt"]), str(m["total"]), iid))
            save_invoice_items(db, iid, new_items)
            pick = f.get("bank_currency", None)
            if bank_opts and pick is not None and pick != (inv["bank_currency"] or ""):
                if pick in bank_opts:       # a different set was chosen: print that one
                    db.execute("UPDATE invoices SET bank_currency = ?, bank_details = ? WHERE id = ?",
                               (pick, get_bank_cur(db, q["company"], pick), iid))
                elif pick == "":            # the company's general details
                    db.execute("UPDATE invoices SET bank_currency = '', bank_details = ? WHERE id = ?",
                               (get_bank(db, q["company"]), iid))
            db.commit()
            flash("Tax invoice saved.", "ok")
            return redirect(url_for("invoice_view", iid=iid))
        for e in errors:
            flash(e, "err")
        shown = dict(inv)
        shown.update(to_address=f.get("to_address", ""), inv_no=no, inv_date=dt, po_number=f.get("po_number", ""), vessel_name=f.get("vessel_name", ""),
                     pay_terms=f.get("pay_terms", ""), location=f.get("location", ""),
                     discount_type=m["discount_type"], discount_value=f.get("discount_value", "0"),
                     gst_rate=f.get("gst_rate", "0"))
        item_list = [{"description": a, "part_no": b, "qty": c, "unit_price": e} for a, b, c, e in
                     zip(f.getlist("description"), f.getlist("part_no"), f.getlist("qty"), f.getlist("unit_price"))]
        shown["bank_currency"] = f.get("bank_currency", inv["bank_currency"])
        return render_template("inv_view.html", inv=shown, d=d, jo=jo, q=q, items=item_list or [{}],
                               bank_opts=bank_opts, customers=saved_addresses(db))
    shown = dict(inv)
    shown["to_address"] = shown["to_address"] or q["to_address"]
    if not shown["bank_currency"] and bank_opts:     # older invoice: it was made with its currency's set
        shown["bank_currency"] = default_bank_currency(db, q["company"], q["currency"])
    return render_template("inv_view.html", inv=shown, d=d, jo=jo, q=q, items=[dict(i) for i in items],
                           bank_opts=bank_opts, customers=saved_addresses(db))


@app.route("/invoice/<int:iid>/pdf")
def invoice_pdf(iid):
    inv, d, jo, q, items = load_invoice(get_db(), iid)
    buf = build_invoice_pdf(q, inv, items, jo["jo_no"])
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=request.args.get("download") == "1",
                     download_name=f"TaxInvoice_{inv['inv_no']}.pdf")


@app.route("/invoice/<int:iid>/delete", methods=["POST"])
def invoice_delete(iid):
    db = get_db()
    inv, d, jo, q, items = load_invoice(db, iid)
    db.execute("DELETE FROM invoices WHERE id = ?", (iid,))
    db.commit()
    flash(f"Tax invoice {inv['inv_no']} deleted.", "ok")
    return redirect(url_for("delivery_view", did=d["id"]))


@app.route("/letterhead", methods=["GET", "POST"])
@admin_required
def letterhead():
    """Administrator: edit the header printed on every PDF - name, GST/UEN, address, phone, e-mail -
    and (optionally) replace the logo or the certification picture."""
    from PIL import Image as PILImage
    db = get_db()
    if request.method == "POST":
        f = request.form
        errors = []
        if f.get("reset_all"):
            db.execute("DELETE FROM letterhead")
            for kind in list(LH_IMAGES) + ["full"]:
                try:
                    os.remove(os.path.join(LETTERHEAD_DIR, kind + ".png"))
                except OSError:
                    pass
            db.commit()
            flash("The letterhead was reset to the original.", "ok")
            return redirect(url_for("letterhead"))
        vals = {k: f.get(k, "").strip() for k in LETTERHEAD_DEFAULTS}
        vals["address"] = clean_address(f.get("address", ""))
        if not vals["name"]:
            errors.append("Type the company name.")
        for k, label, n in (("name", "Company name", 60), ("reg_line", "GST / UEN line", 80),
                            ("phone", "Phone line", 80), ("email", "E-mail line", 80)):
            if len(vals[k]) > n:
                errors.append(f"{label} is too long (at most {n} characters).")
        if len(vals["address"]) > 300 or vals["address"].count("\n") > 5:
            errors.append("The address is too long (at most 6 lines).")
        new_imgs = {}
        for kind in list(LH_IMAGES) + ["full"]:
            up = request.files.get(kind)
            if up and up.filename:
                raw = up.read()
                try:
                    if len(raw) > 5 * 1024 * 1024:
                        raise ValueError("too large")
                    im = PILImage.open(io.BytesIO(raw))
                    im.load()
                    if im.mode in ("RGBA", "LA", "P"):
                        im = im.convert("RGBA")
                        bg = PILImage.new("RGB", im.size, "white")
                        bg.paste(im, mask=im.split()[-1])
                        im = bg
                    new_imgs[kind] = im.convert("RGB")
                except Exception:
                    errors.append(f"The {dict(logo='logo', cert='certification', full='header')[kind]} picture could not be read "
                                  "(use a PNG or JPG under 5 MB).")
        if errors:
            for e in errors:
                flash(e, "err")
            return render_template("letterhead.html", st=vals, ver="x", unsaved=True)
        for k, v in vals.items():
            db.execute("INSERT INTO letterhead (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                       (k, v))
        db.commit()
        os.makedirs(LETTERHEAD_DIR, exist_ok=True)
        for kind, im in new_imgs.items():
            im.save(os.path.join(LETTERHEAD_DIR, kind + ".png"), "PNG")
        for kind in list(LH_IMAGES) + ["full"]:
            if f.get("default_" + kind) and kind not in new_imgs:
                try:
                    os.remove(os.path.join(LETTERHEAD_DIR, kind + ".png"))
                except OSError:
                    pass
        flash("Letterhead saved - it is used on all new PDFs.", "ok")
        return redirect(url_for("letterhead"))
    return render_template("letterhead.html", st=letterhead_settings(db),
                           ver=os.path.basename(letterhead_file(db)), unsaved=False)


@app.route("/brand/logo.png")
def brand_logo():
    """The company logo shown in the page header: the letterhead logo uploaded on the Letterhead
    page if there is one, otherwise the built-in default."""
    resp = send_file(lh_image_path("logo"), mimetype="image/png")
    resp.headers["Cache-Control"] = "max-age=300"
    return resp


@app.route("/letterhead/preview.png")
def letterhead_preview():
    resp = send_file(letterhead_file(), mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/bank", methods=["GET", "POST"])
@admin_required
def bank():
    """Edit the bank details (administrators only). A company with per-currency details (Turbo Prime
    Pte Ltd: USD / SGD / JPY) has one set per currency - chosen on the tax invoice; every company also has a general set used for the other currencies."""
    db = get_db()
    if request.method == "POST":
        key = request.form.get("company", "")
        cur_ = request.form.get("currency", "")
        if key not in COMPANIES:
            abort(400)
        if cur_ and cur_ not in [c for c, _ in CURRENCY_BANKS.get(key, [])]:
            abort(400)
        text = "\n".join(l.rstrip() for l in request.form.get("bank_details", "").replace("\r", "").strip().split("\n"))
        what = f"{COMPANIES[key]['name']}" + (f" ({cur_})" if cur_ else "")
        if cur_:
            db.execute("INSERT INTO company_bank_cur (company, currency, bank_details) VALUES (?, ?, ?) "
                       "ON CONFLICT(company, currency) DO UPDATE SET bank_details = excluded.bank_details",
                       (key, cur_, text))
        else:
            db.execute("INSERT INTO company_bank (company, bank_details) VALUES (?, ?) "
                       "ON CONFLICT(company) DO UPDATE SET bank_details = excluded.bank_details", (key, text))
        msg = f"Bank details for {what} saved. New quotations and tax invoices use them; " if True else ""
        if request.form.get("apply_existing"):
            sql = ("UPDATE invoices SET bank_details = ? WHERE delivery_id IN (SELECT d.id FROM "
                   "delivery_orders d JOIN job_orders j ON j.id = d.job_id JOIN quotations q "
                   "ON q.id = j.quotation_id WHERE q.company = ?")
            args = [text, key]
            if cur_:   # invoices that use this currency's set (older ones: made in this currency)
                sql += " AND (invoices.bank_currency = ? OR (invoices.bank_currency = '' AND q.currency = ?))"
                args += [cur_, cur_]
            elif key in CURRENCY_BANKS:   # general details: only invoices not using a currency set
                sql += (" AND invoices.bank_currency = '' AND q.currency NOT IN (%s)"
                        % ",".join("?" * len(CURRENCY_BANKS[key])))
                args += [c for c, _ in CURRENCY_BANKS[key]]
            n = db.execute(sql + ")", args).rowcount
            msg += f"{n} existing tax invoice(s) were updated too."
        else:
            msg += "existing tax invoices keep their own copy."
        db.commit()
        flash(msg, "ok")
        return redirect(url_for("bank") + "#" + key + (cur_ or ""))
    return render_template("bank.html", banks={k: get_bank(db, k) for k in COMPANIES},
                           cur_banks={k: [(c, get_bank_cur(db, k, c)) for c, _ in rows]
                                      for k, rows in CURRENCY_BANKS.items()})


@app.route("/terms", methods=["GET", "POST"])
@admin_required
def terms_page():
    """Edit the three Terms & Conditions options offered on a quotation (name and wording)."""
    db = get_db()
    if request.method == "POST":
        key = request.form.get("key", "")
        name = request.form.get("name", "").strip()
        body = "\n".join(l.rstrip() for l in request.form.get("body", "").replace("\r", "").strip().split("\n"))
        if not db.execute("SELECT 1 FROM terms_options WHERE key = ?", (key,)).fetchone():
            abort(400)
        if not name or not body:
            flash("Give the option a name and its terms.", "err")
        else:
            db.execute("UPDATE terms_options SET name = ?, body = ? WHERE key = ?", (name, body, key))
            db.commit()
            flash(f"T&C option '{name}' saved. It is used on new quotations; existing quotations keep their own terms.", "ok")
        return redirect(url_for("terms_page") + "#" + key)
    return render_template("terms.html", options=terms_options(db))


@app.route("/saved", methods=["GET", "POST"])
def saved():
    """Manage saved customer addresses and T/C types."""
    db = get_db()
    if request.method == "POST":
        kind = request.form.get("kind")
        if kind == "customer":
            addr = clean_address(request.form.get("address", ""))
            if addr:
                rid = request.form.get("rid", "").strip()
                try:
                    if rid.isdigit():
                        db.execute("UPDATE customers SET name = ?, address = ? WHERE id = ?",
                                   (addr.split("\n")[0], addr, int(rid)))
                    else:
                        remember_address(db, addr)
                except sqlite3.IntegrityError:
                    db.rollback()
                    flash("A saved address with this name or exactly this text already exists.", "err")
                    return redirect(url_for("saved"))
        elif kind == "tc" and request.form.get("name", "").strip():
            db.execute("INSERT OR IGNORE INTO tc_types (name) VALUES (?)",
                       (request.form["name"].strip(),))
        db.commit()
        flash("Saved.", "ok")
        return redirect(url_for("saved"))
    customers, tcs = masters(db)
    return render_template("saved.html", customers=customers, tcs=tcs)


@app.route("/saved/delete/<kind>/<int:rid>", methods=["POST"])
def saved_delete(kind, rid):
    table = {"customer": "customers", "tc": "tc_types", "supplier": "suppliers"}.get(kind)
    if table is None:
        abort(404)
    db = get_db()
    db.execute(f"DELETE FROM {table} WHERE id = ?", (rid,))
    db.commit()
    flash("Removed from the saved list (existing documents are not affected).", "ok")
    return redirect(url_for("saved"))


@app.route("/delete/<int:qid>", methods=["POST"])
def delete(qid):
    db = get_db()
    if job_for_quotation(db, qid):
        flash("This quotation has a job order. Delete the job order first (from the Job Orders list), then delete the quotation.", "err")
        return redirect(url_for("view", qid=qid))
    db.execute("DELETE FROM quotations WHERE id = ?", (qid,))
    db.commit()
    flash("Quotation deleted.", "ok")
    return redirect(url_for("index"))


init_db()

# --------------------------------------------------------------------------
# Commercial Invoices and Packing Lists (stand-alone, like purchase orders)
# --------------------------------------------------------------------------
def next_export_no(db, kind, year=None):
    """CI 27-0001, CI 27-0002 ... / PL 27-0001 ... (same numbering style as delivery orders)."""
    K = EXPORT_KINDS[kind]
    yy = f"{series_yy(year):02d}"
    pat = re.compile(r"^" + re.escape(K["prefix"]) + r"(\d{2})-(\d+)$", re.I)
    top = 0
    for (no,) in db.execute(f"SELECT {K['no']} FROM {K['table']} WHERE {K['no']} LIKE ?", (K["prefix"] + yy + "-%",)):
        m = pat.match(no or "")
        if m and m.group(1) == yy:
            top = max(top, int(m.group(2)))
    return f"{K['prefix']}{yy}-{top + 1:04d}"


def all_tc_options(db):
    """Every TC Type used before (saved list + earlier quotations, purchase orders, commercial invoices, packing
    lists), most recently used first - offered as a choice on the stand-alone documents."""
    seen, out = set(), []
    queries = ["SELECT tc_type FROM commercial_invoices", "SELECT tc_type FROM packing_lists",
               "SELECT tc_type FROM purchase_orders", "SELECT tc_type FROM quotations"]
    vals = []
    for qry in queries:
        vals += [r[0] for r in db.execute(qry + " WHERE tc_type != '' ORDER BY id DESC")]
    vals += [r[0] for r in db.execute("SELECT name FROM tc_types ORDER BY name COLLATE NOCASE")]
    for v in vals:
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


def make_export_routes(kind):
    K = EXPORT_KINDS[kind]
    table, itable, fk = K["table"], K["items"], K["fk"]

    def load(db, xid):
        row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (xid,)).fetchone()
        if row is None:
            abort(404)
        return row, db.execute(f"SELECT * FROM {itable} WHERE {fk} = ? ORDER BY sno", (xid,)).fetchall()

    def no_taken(db, no, exclude=None):
        r = db.execute(f"SELECT id FROM {table} WHERE {K['no']} = ? COLLATE NOCASE", (no,)).fetchone()
        return bool(r) and r["id"] != exclude

    def list_view():
        db = get_db()
        search = request.args.get("q", "").strip()
        sql = f"SELECT * FROM {table} WHERE company = ?"
        args = [active_company()["key"]]
        if search:
            sql += f" AND ({K['no']} LIKE ? OR to_address LIKE ? OR po_no LIKE ? OR vessel_name LIKE ?)"
            args += [f"%{search}%"] * 4
        rows = db.execute(sql + " ORDER BY id DESC", args).fetchall()
        return render_template("export_list.html", k=K, rows=rows, search=search)

    def edit_view(xid=None):
        db = get_db()
        is_new = xid is None
        company = active_company()["key"]
        if is_new:
            row = {"id": None, K["no"]: "", K["date"]: date.today().isoformat(), "to_address": "",
                   "shipper_name": co(company)["name"], "tc_type": "", "vessel_name": "", "po_no": "",
                   "currency": "", "dimensions": ""}
            items = [{}]
        else:
            r, its = load(db, xid)
            row, items, company = dict(r), [dict(i) for i in its], r["company"] or company
        if request.method == "POST":
            f = request.form
            h = {k: f.get(k, "").strip() for k in ("no", "date", "shipper_name", "tc_type", "vessel_name", "po_no")}
            h["to_address"] = clean_address(f.get("to_address", ""))
            h["dimensions"] = "\n".join(l.rstrip() for l in f.get("dimensions", "").replace("\r", "").split("\n")).strip()
            errors = []
            if not h["no"]:
                errors.append(f"Type the {K['title'].lower()} number.")
            elif len(h["no"]) > 30:
                errors.append(f"{K['title']} number is too long.")
            elif no_taken(db, h["no"], xid):
                errors.append(f"{K['title']} number {h['no']} already exists.")
            if not valid_iso(h["date"]):
                errors.append(f"Choose the {K['title'].lower()} date.")
            if not h["to_address"]:
                errors.append("Type the customer's name and address.")
            if K["prices"]:
                new_items, m = parse_po(f, errors)
                extra = {"currency": m["currency"], "total": str(m["total"])}
            else:
                new_items, extra = parse_do_lines(f, errors), {}
            if not errors:
                cols = {K["no"]: h["no"], K["date"]: h["date"], "to_address": h["to_address"],
                        "shipper_name": h["shipper_name"], "tc_type": h["tc_type"], "vessel_name": h["vessel_name"],
                        "po_no": h["po_no"], "dimensions": h["dimensions"], **extra}
                try:
                    if is_new:
                        cols.update(company=company, created_at=datetime.now().isoformat(timespec="seconds"))
                        cur = db.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                         list(cols.values()))
                        xid = cur.lastrowid
                    else:
                        db.execute(f"UPDATE {table} SET " + ", ".join(f"{c} = ?" for c in cols) + " WHERE id = ?",
                                   list(cols.values()) + [xid])
                except sqlite3.IntegrityError:
                    db.rollback()
                    errors.append(f"{K['title']} number {h['no']} already exists.")
                else:
                    db.execute(f"DELETE FROM {itable} WHERE {fk} = ?", (xid,))
                    for n, it in enumerate(new_items, start=1):
                        if K["prices"]:
                            db.execute(f"INSERT INTO {itable} ({fk}, sno, description, part_no, qty, unit_price, line_total) "
                                       "VALUES (?,?,?,?,?,?,?)", (xid, n, it["description"], it["part_no"], str(it["qty"]),
                                                                    str(it["unit_price"]), str(it["line_total"])))
                        else:
                            db.execute(f"INSERT INTO {itable} ({fk}, sno, description, part_no, qty) VALUES (?,?,?,?,?)",
                                       (xid, n, it["description"], it["part_no"], str(it["qty"])))
                    remember_address(db, h["to_address"])
                    if h["tc_type"]:
                        db.execute("INSERT OR IGNORE INTO tc_types (name) VALUES (?)", (h["tc_type"],))
                    db.commit()
                    flash(f"{K['title']} {h['no']} saved.", "ok")
                    return redirect(url_for(f"{kind}_view", xid=xid))
            for e in errors:
                flash(e, "err")
            row.update({K["no"]: h["no"], K["date"]: h["date"], "to_address": h["to_address"],
                        "shipper_name": h["shipper_name"], "tc_type": h["tc_type"], "vessel_name": h["vessel_name"],
                        "po_no": h["po_no"], "dimensions": h["dimensions"], "currency": f.get("currency", "")})
            items = po_items_from_form(f) or [{}]
        elif is_new:
            row[K["no"]] = next_export_no(db, kind)
        customers = [dict(c) for c in db.execute("SELECT * FROM customers ORDER BY name COLLATE NOCASE")]
        return render_template("export_view.html", k=K, row=row, items=items, is_new=is_new, customers=customers,
                               tc_options=all_tc_options(db), currencies=PO_CURRENCIES)

    def pdf_view(xid):
        row, items = load(get_db(), xid)
        buf = build_export_pdf(kind, row, items)
        return send_file(buf, mimetype="application/pdf", as_attachment=request.args.get("download") == "1",
                         download_name=f"{K['file']}_{row[K['no']]}.pdf")

    def delete_view(xid):
        db = get_db()
        row, _ = load(db, xid)
        db.execute(f"DELETE FROM {table} WHERE id = ?", (xid,))
        db.commit()
        flash(f"{K['title']} {row[K['no']]} deleted.", "ok")
        return redirect(url_for(f"{kind}_list"))

    slug = K["slug"]
    app.add_url_rule(f"/{slug}s", f"{kind}_list", list_view)
    app.add_url_rule(f"/{slug}/new", f"{kind}_new", edit_view, defaults={"xid": None}, methods=["GET", "POST"])
    app.add_url_rule(f"/{slug}/<int:xid>", f"{kind}_view", edit_view, methods=["GET", "POST"])
    app.add_url_rule(f"/{slug}/<int:xid>/pdf", f"{kind}_pdf", pdf_view)
    app.add_url_rule(f"/{slug}/<int:xid>/delete", f"{kind}_delete", delete_view, methods=["POST"])


for _kind in EXPORT_KINDS:
    make_export_routes(_kind)



if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)), debug=False)
