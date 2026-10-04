from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from typing import Callable, Optional

import click
from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = BASE_DIR / "instance" / "bizmate.db"
PRODUCT_UPLOADS = BASE_DIR / "static" / "uploads" / "products"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
UNLIMITED_PRODUCT_LIMIT = 100000
DEFAULT_PLANS = (
    {
        "name": "Free",
        "tier_label": "STARTER",
        "description": "A free starting point for solo sellers and new shops.",
        "price_paise": 0,
        "product_limit": 25,
        "is_featured": 0,
        "features": "Up to 25 active products\nSeller product and inventory management\nOrder and expense tracking\nMarketplace listings after owner approval",
    },
    {
        "name": "Business",
        "tier_label": "GROWING STORE",
        "description": "More room for growing retail shops and active merchants.",
        "price_paise": 29900,
        "product_limit": UNLIMITED_PRODUCT_LIMIT,
        "is_featured": 1,
        "features": "Unlimited active products\nInventory, orders, and expense tracking\nCustomer and sales reports\nSeller business insights",
    },
    {
        "name": "Pro",
        "tier_label": "SCALE & ENTERPRISE",
        "description": "The same full seller workspace at the highest catalogue tier.",
        "price_paise": 69900,
        "product_limit": UNLIMITED_PRODUCT_LIMIT,
        "is_featured": 0,
        "features": "Everything available in Business\nUnlimited active products\nSeller reports and business insights\nManual UPI plan payment with owner verification",
    },
)

app = Flask(__name__)
secret = os.environ.get("BIZMATE_SECRET_KEY")
IS_PRODUCTION = os.environ.get("BIZMATE_ENV", "development").lower() == "production"
if IS_PRODUCTION and not secret:
    raise RuntimeError("Set BIZMATE_SECRET_KEY before running BizMate in production.")
app.config.update(
    SECRET_KEY=secret or secrets.token_hex(32),
    DATABASE=os.environ.get("BIZMATE_DATABASE", str(DEFAULT_DATABASE)),
    MAX_CONTENT_LENGTH=6 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("BIZMATE_HTTPS", "1" if IS_PRODUCTION else "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL,
 email TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('owner','seller')),
 status TEXT NOT NULL CHECK(status IN ('pending','active','rejected','suspended')),
 created_at TEXT NOT NULL,
 last_login TEXT
);
CREATE TABLE IF NOT EXISTS plans (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL UNIQUE COLLATE NOCASE,
 description TEXT NOT NULL DEFAULT '',
 price_paise INTEGER NOT NULL CHECK(price_paise >= 0),
 billing_interval TEXT NOT NULL CHECK(billing_interval IN ('monthly','yearly')),
 product_limit INTEGER NOT NULL CHECK(product_limit > 0),
 tier_label TEXT NOT NULL DEFAULT '',
 features TEXT NOT NULL DEFAULT '',
 is_featured INTEGER NOT NULL DEFAULT 0 CHECK(is_featured IN (0,1)),
 is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seller_profiles (
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 business_name TEXT NOT NULL,
 registration_number TEXT NOT NULL DEFAULT '',
 description TEXT NOT NULL DEFAULT '',
 requested_plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL,
 plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL,
 payment_plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL,
 payment_reference TEXT NOT NULL DEFAULT '',
 payment_status TEXT NOT NULL DEFAULT 'not_required',
 payment_verified_at TEXT,
 approved_at TEXT
);
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 seller_id INTEGER NOT NULL REFERENCES users(id),
 name TEXT NOT NULL,
 category TEXT NOT NULL,
 description TEXT NOT NULL,
 price_paise INTEGER NOT NULL CHECK(price_paise > 0),
 inventory INTEGER NOT NULL DEFAULT 0 CHECK(inventory >= 0),
 low_stock_at INTEGER NOT NULL DEFAULT 5 CHECK(low_stock_at >= 0),
 image_path TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','archived')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cart_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 cart_key TEXT NOT NULL,
 product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 quantity INTEGER NOT NULL CHECK(quantity > 0),
 created_at TEXT NOT NULL,
 UNIQUE(cart_key, product_id)
);
CREATE TABLE IF NOT EXISTS orders (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 order_number TEXT NOT NULL UNIQUE,
 seller_id INTEGER NOT NULL REFERENCES users(id),
 customer_name TEXT NOT NULL,
 customer_email TEXT NOT NULL,
 shipping_address TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','processing','shipped','delivered','cancelled')),
 payment_method TEXT NOT NULL DEFAULT 'cod',
 payment_status TEXT NOT NULL DEFAULT 'due' CHECK(payment_status IN ('due','paid')),
 subtotal_paise INTEGER NOT NULL,
 shipping_paise INTEGER NOT NULL,
 total_paise INTEGER NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
 product_id INTEGER NOT NULL REFERENCES products(id),
 product_name TEXT NOT NULL,
 unit_price_paise INTEGER NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity > 0)
);
CREATE TABLE IF NOT EXISTS expenses (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 seller_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 title TEXT NOT NULL,
 category TEXT NOT NULL,
 amount_paise INTEGER NOT NULL CHECK(amount_paise > 0),
 expense_date TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
 attempt_key TEXT PRIMARY KEY,
 failures INTEGER NOT NULL,
 locked_until INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 action TEXT NOT NULL,
 entity_type TEXT NOT NULL,
 entity_id INTEGER,
 details TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_marketplace ON products(status, category);
CREATE INDEX IF NOT EXISTS idx_products_seller ON products(seller_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_seller_date ON orders(seller_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cart_key ON cart_items(cart_key);
CREATE INDEX IF NOT EXISTS idx_expenses_seller_date ON expenses(seller_id, expense_date);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        path = Path(app.config["DATABASE"])
        path.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(str(path), timeout=20)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA busy_timeout=20000")
    return g.db


def init_db() -> None:
    db = get_db()
    db_path = str(Path(app.config["DATABASE"]).resolve())
    if app.extensions.get("bizmate_initialized_db") == db_path:
        return
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='products'").fetchone()
    if exists:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(products)")}
        if "seller_id" not in columns:
            raise RuntimeError("The existing database has an incompatible schema. Back it up and apply an explicit migration before reusing it.")
    db.executescript(SCHEMA)
    migrations = {
        "plans": {
            "tier_label": "TEXT NOT NULL DEFAULT ''",
            "features": "TEXT NOT NULL DEFAULT ''",
            "is_featured": "INTEGER NOT NULL DEFAULT 0",
        },
        "seller_profiles": {
            "payment_plan_id": "INTEGER REFERENCES plans(id) ON DELETE SET NULL",
            "payment_reference": "TEXT NOT NULL DEFAULT ''",
            "payment_status": "TEXT NOT NULL DEFAULT 'not_required'",
            "payment_verified_at": "TEXT",
        },
    }
    for table, columns in migrations.items():
        present = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column not in present:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    starter = db.execute("SELECT id FROM plans WHERE name='Starter' AND price_paise=0 AND product_limit=25 AND features=?",
        (DEFAULT_PLANS[0]["features"],)).fetchone()
    free = db.execute("SELECT id FROM plans WHERE name='Free'").fetchone()
    if starter and not free:
        db.execute("UPDATE plans SET name='Free',tier_label='STARTER' WHERE id=?", (starter["id"],))
    for plan in DEFAULT_PLANS:
        db.execute(
            """INSERT OR IGNORE INTO plans
            (name,description,price_paise,billing_interval,product_limit,tier_label,features,is_featured,is_active,created_at)
            VALUES(?,?,?,?,?,?,?,?,1,?)""",
            (plan["name"], plan["description"], plan["price_paise"], "monthly", plan["product_limit"],
             plan["tier_label"], plan["features"], plan["is_featured"], utc_now()),
        )
    db.execute("PRAGMA journal_mode=WAL")
    db.commit()
    app.extensions["bizmate_initialized_db"] = db_path


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def money(paise: int) -> str:
    rupees, cents = divmod(int(paise or 0), 100)
    return f"₹{rupees:,}" if cents == 0 else f"₹{rupees:,}.{cents:02d}"


def money_to_paise(value: str, allow_zero: bool = False) -> Optional[int]:
    try:
        amount = Decimal(value.strip()).quantize(Decimal("0.01"))
    except (AttributeError, InvalidOperation, ValueError):
        return None
    if amount < 0 or (amount == 0 and not allow_zero) or amount > Decimal("999999999"):
        return None
    return int(amount * 100)


def valid_email(email: str) -> bool:
    return bool(email and len(email) <= 254 and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email))


def valid_password(password: str) -> bool:
    return 12 <= len(password) <= 1024 and any(c.isalpha() for c in password) and any(c.isdigit() for c in password)


def valid_utr(reference: str) -> bool:
    return bool(reference and re.fullmatch(r"[A-Za-z0-9/-]{8,32}", reference))


def clean_plan_features(value: str) -> Optional[str]:
    if len(value or "") > 1200:
        return None
    lines = [line.strip() for line in (value or "").splitlines() if line.strip()]
    if len(lines) > 8 or any(len(line) > 140 for line in lines):
        return None
    return "\n".join(lines)


def hash_password(password: str) -> str:
    # Explicit PBKDF2 keeps password creation compatible with supported Python/OpenSSL builds.
    return generate_password_hash(password, method="pbkdf2:sha256:600000")


def safe_next_url(target: Optional[str]) -> bool:
    return bool(target and target.startswith("/") and not target.startswith("//") and "\\" not in target)


def log_event(action: str, entity_type: str, entity_id: Optional[int], details: str = "") -> None:
    user = getattr(g, "user", None)
    get_db().execute(
        "INSERT INTO audit_events (actor_user_id,action,entity_type,entity_id,details,created_at) VALUES (?,?,?,?,?,?)",
        (user["id"] if user else None, action, entity_type, entity_id, details[:500], utc_now()),
    )


def seller_id() -> int:
    if not getattr(g, "user", None) or g.user["role"] != "seller":
        abort(403)
    return int(g.user["id"])


def owner_required(view: Callable):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(g, "user", None):
            return redirect(url_for("login", next=request.path))
        if g.user["role"] != "owner" or g.user["status"] != "active":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def seller_required(view: Callable):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(g, "user", None):
            return redirect(url_for("login", next=request.path))
        if g.user["role"] != "seller":
            abort(403)
        if g.user["status"] != "active":
            return redirect(url_for("seller_application"))
        return view(*args, **kwargs)
    return wrapped


def sniff_image(payload: bytes):
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp"
    return None


def save_uploaded_image(upload):
    if upload is None or not upload.filename:
        return None, "Choose a product image."
    payload = upload.stream.read(MAX_IMAGE_BYTES + 1)
    if not payload:
        return None, "The selected image is empty."
    if len(payload) > MAX_IMAGE_BYTES:
        return None, "Images must be 5 MB or smaller."
    extension = sniff_image(payload)
    if not extension:
        return None, "Upload a valid JPEG, PNG, or WebP image. SVG and other file types are not accepted."
    PRODUCT_UPLOADS.mkdir(parents=True, exist_ok=True)
    filename = uuid.uuid4().hex + extension
    (PRODUCT_UPLOADS / filename).write_bytes(payload)
    return f"uploads/products/{filename}", None


def cart_rows():
    key = session.get("cart_key")
    if not key:
        return []
    return get_db().execute(
        """SELECT c.product_id,c.quantity,p.name,p.price_paise,p.image_path,p.inventory,p.seller_id,s.business_name
        FROM cart_items c JOIN products p ON p.id=c.product_id
        JOIN users u ON u.id=p.seller_id AND u.role='seller' AND u.status='active'
        JOIN seller_profiles s ON s.user_id=p.seller_id
        WHERE c.cart_key=? AND p.status='published' ORDER BY c.created_at DESC""",
        (key,),
    ).fetchall()


def cart_summary():
    items = cart_rows()
    count = sum(row["quantity"] for row in items)
    subtotal = sum(row["price_paise"] * row["quantity"] for row in items)
    sellers = {}
    for item in items:
        sellers[item["seller_id"]] = sellers.get(item["seller_id"], 0) + item["price_paise"] * item["quantity"]
    shipping = sum(0 if amount >= 100000 else 4900 for amount in sellers.values())
    return items, count, subtotal, shipping


@app.before_request
def prepare_request():
    init_db()
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    if "cart_key" not in session:
        session["cart_key"] = secrets.token_urlsafe(24)
    g.user = None
    if session.get("user_id"):
        g.user = get_db().execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if g.user is None or g.user["status"] in {"rejected", "suspended"}:
            session.clear()
            g.user = None
            session["csrf_token"] = secrets.token_urlsafe(32)
            session["cart_key"] = secrets.token_urlsafe(24)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        token = request.headers.get("X-CSRFToken") or request.form.get("_csrf_token", "")
        if not token or not secrets.compare_digest(token, session.get("csrf_token", "")):
            abort(400, description="This form expired. Reload the page and try again.")


@app.context_processor
def template_globals():
    _items, count, _subtotal, _shipping = cart_summary()
    profile = None
    if getattr(g, "user", None) and g.user["role"] == "seller":
        profile = get_db().execute("SELECT business_name FROM seller_profiles WHERE user_id=?", (g.user["id"],)).fetchone()
    return {"cart_count": count, "money": money, "year": datetime.now(timezone.utc).year,
            "today_iso": utc_day(), "csrf_token": session.get("csrf_token", ""),
            "current_user": getattr(g, "user", None),
            "seller_business": profile["business_name"] if profile else ""}


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'")
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.cli.command("create-owner")
def create_owner_command():
    """Create the first platform-owner account interactively."""
    init_db()
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM users WHERE role='owner'").fetchone()[0]:
        raise click.ClickException("A platform owner already exists.")
    name = click.prompt("Owner name").strip()
    if not name or len(name) > 100:
        raise click.ClickException("Owner name is required and must be 100 characters or fewer.")
    email = click.prompt("Owner email").strip().lower()
    if not valid_email(email):
        raise click.ClickException("Enter a valid email address.")
    password = click.prompt("Password (12+ characters, include a number)", hide_input=True, confirmation_prompt=True)
    if not valid_password(password):
        raise click.ClickException("Password must have at least 12 characters and include letters and numbers.")
    db.execute("INSERT INTO users(name,email,password_hash,role,status,created_at) VALUES(?,?,?,'owner','active',?)", (name, email, hash_password(password), utc_now()))
    db.commit()
    click.echo("Owner account created. Sign in at /login.")


@app.cli.command("promote-owner")
@click.option("--email", required=True, help="Email of the existing account to convert.")
def promote_owner_command(email):
    """Convert an existing seller account to the first platform owner."""
    init_db()
    db = get_db()
    account = db.execute("SELECT id,email,name,role FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if not account:
        raise click.ClickException("No account uses that email.")
    if account["role"] != "seller":
        raise click.ClickException("Only an existing seller account can be converted by this command.")
    if db.execute("SELECT COUNT(*) FROM users WHERE role='owner'").fetchone()[0]:
        raise click.ClickException("A platform owner already exists; refusing to change owner access.")
    click.confirm(
        f"Convert {account['email']} to the platform owner? "
        "Its seller workspace access will stop; seller records will be preserved.",
        abort=True,
    )
    password = click.prompt("New owner password (12+ characters, include a number)", hide_input=True, confirmation_prompt=True)
    if not valid_password(password):
        raise click.ClickException("Password must have at least 12 characters and include letters and numbers.")
    db.execute("UPDATE users SET role='owner',status='active',password_hash=?,last_login=NULL WHERE id=?",
        (hash_password(password), account["id"]))
    log_event("seller.converted_to_owner", "user", account["id"], "Owner access granted; seller records retained")
    db.commit()
    click.echo("Account converted. Sign in at /login with the owner email and new password.")


@app.route("/")
def home():
    db = get_db()
    products = db.execute("""SELECT p.*,s.business_name FROM products p
        JOIN users u ON u.id=p.seller_id AND u.role='seller' AND u.status='active'
        JOIN seller_profiles s ON s.user_id=p.seller_id
        WHERE p.status='published' AND p.inventory>0 ORDER BY p.created_at DESC LIMIT 4""").fetchall()
    categories = db.execute("""SELECT DISTINCT p.category FROM products p
        JOIN users u ON u.id=p.seller_id WHERE u.role='seller' AND p.status='published' AND p.inventory>0
        AND u.status='active' ORDER BY p.category""").fetchall()
    return render_template("home.html", products=products, categories=categories)


@app.route("/marketplace")
def marketplace():
    db = get_db()
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "All")
    sql = """SELECT p.*,s.business_name FROM products p
        JOIN users u ON u.id=p.seller_id AND u.role='seller' AND u.status='active'
        JOIN seller_profiles s ON s.user_id=p.seller_id
        WHERE p.status='published' AND p.inventory>0"""
    params = []
    if category != "All":
        sql += " AND p.category=?"
        params.append(category)
    if query:
        sql += " AND (p.name LIKE ? OR p.category LIKE ? OR p.description LIKE ? OR s.business_name LIKE ?)"
        term = f"%{query}%"
        params.extend([term, term, term, term])
    products = db.execute(sql + " ORDER BY p.created_at DESC", params).fetchall()
    categories = ["All"] + [row["category"] for row in db.execute(
        """SELECT DISTINCT p.category FROM products p JOIN users u ON u.id=p.seller_id
        WHERE p.status='published' AND p.inventory>0 AND u.role='seller' AND u.status='active' ORDER BY p.category""").fetchall()]
    return render_template("marketplace.html", products=products, categories=categories, active_category=category, query=query)


@app.route("/product/<int:product_id>")
def product_detail(product_id):
    db = get_db()
    product = db.execute("""SELECT p.*,s.business_name FROM products p
        JOIN users u ON u.id=p.seller_id AND u.role='seller' AND u.status='active'
        JOIN seller_profiles s ON s.user_id=p.seller_id
        WHERE p.id=? AND p.status='published'""", (product_id,)).fetchone()
    if not product:
        abort(404)
    related = db.execute("""SELECT p.*,s.business_name FROM products p
        JOIN users u ON u.id=p.seller_id AND u.role='seller' AND u.status='active'
        JOIN seller_profiles s ON s.user_id=p.seller_id
        WHERE p.category=? AND p.id!=? AND p.status='published' AND p.inventory>0 LIMIT 3""",
        (product["category"], product_id)).fetchall()
    return render_template("product.html", product=product, related=related)


@app.post("/cart/add/<int:product_id>")
def add_to_cart(product_id):
    db = get_db()
    product = db.execute("""SELECT p.* FROM products p JOIN users u ON u.id=p.seller_id
        WHERE p.id=? AND p.status='published' AND p.inventory>0 AND u.role='seller' AND u.status='active'""", (product_id,)).fetchone()
    if not product:
        abort(404)
    existing = db.execute("SELECT quantity FROM cart_items WHERE cart_key=? AND product_id=?", (session["cart_key"], product_id)).fetchone()
    current_qty = existing["quantity"] if existing else 0
    wants_json = request.accept_mimetypes.best == "application/json" or request.is_json
    if current_qty >= product["inventory"]:
        result = {"ok": False, "cart_count": sum(row["quantity"] for row in cart_rows()), "message": "The available stock is already in your cart."}
        if wants_json:
            return jsonify(result), 409
        flash(result["message"], "error")
        return redirect(request.referrer if safe_next_url(request.referrer) else url_for("cart"))
    db.execute("""INSERT INTO cart_items(cart_key,product_id,quantity,created_at) VALUES(?,?,1,?)
        ON CONFLICT(cart_key,product_id) DO UPDATE SET quantity=quantity+1""", (session["cart_key"], product_id, utc_now()))
    db.commit()
    _items, count, _subtotal, _shipping = cart_summary()
    if wants_json:
        return jsonify({"ok": True, "cart_count": count, "message": "Added to your cart."})
    flash("Added to your cart.", "success")
    return redirect(request.referrer if safe_next_url(request.referrer) else url_for("cart"))


@app.route("/cart")
def cart():
    items, _count, subtotal, shipping = cart_summary()
    return render_template("cart.html", items=items, subtotal=subtotal, shipping=shipping, total=subtotal + shipping)


@app.post("/cart/update/<int:product_id>")
def update_cart(product_id):
    try:
        quantity = max(0, min(50, int(request.form.get("quantity", "1"))))
    except (TypeError, ValueError):
        quantity = 1
    db = get_db()
    product = db.execute("SELECT inventory FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        abort(404)
    quantity = min(quantity, product["inventory"])
    if quantity == 0:
        db.execute("DELETE FROM cart_items WHERE cart_key=? AND product_id=?", (session["cart_key"], product_id))
    else:
        db.execute("UPDATE cart_items SET quantity=? WHERE cart_key=? AND product_id=?", (quantity, session["cart_key"], product_id))
    db.commit()
    return redirect(url_for("cart"))


@app.post("/cart/remove/<int:product_id>")
def remove_from_cart(product_id):
    db = get_db()
    db.execute("DELETE FROM cart_items WHERE cart_key=? AND product_id=?", (session["cart_key"], product_id))
    db.commit()
    return redirect(url_for("cart"))


@app.post("/checkout")
def checkout():
    items, _count, _subtotal, _shipping = cart_summary()
    if not items:
        flash("Your cart is empty.", "error")
        return redirect(url_for("marketplace"))
    name = request.form.get("customer_name", "").strip()
    email = request.form.get("customer_email", "").strip().lower()
    address = request.form.get("shipping_address", "").strip()
    if not name or len(name) > 120 or not valid_email(email) or len(address) < 8 or len(address) > 500:
        flash("Enter your name, a valid email, and a complete delivery address.", "error")
        return redirect(url_for("cart"))
    grouped = {}
    for item in items:
        group = grouped.setdefault(item["seller_id"], {"items": [], "subtotal": 0, "business_name": item["business_name"]})
        group["items"].append(item)
        group["subtotal"] += item["price_paise"] * item["quantity"]
    db = get_db()
    confirmations = []
    try:
        db.execute("BEGIN IMMEDIATE")
        for item in items:
            stock = db.execute("SELECT inventory,status FROM products WHERE id=?", (item["product_id"],)).fetchone()
            if not stock or stock["status"] != "published" or stock["inventory"] < item["quantity"]:
                db.rollback()
                flash("Stock changed while you were shopping. Review your cart and try again.", "error")
                return redirect(url_for("cart"))
        for shop_id, group in grouped.items():
            delivery = 0 if group["subtotal"] >= 100000 else 4900
            number = "BM-" + secrets.token_hex(10).upper()
            cursor = db.execute("""INSERT INTO orders(order_number,seller_id,customer_name,customer_email,shipping_address,status,payment_method,payment_status,subtotal_paise,shipping_paise,total_paise,created_at)
                VALUES(?,?,?,?,?,'pending','cod','due',?,?,?,?)""",
                (number, shop_id, name, email, address, group["subtotal"], delivery, group["subtotal"] + delivery, utc_now()))
            for item in group["items"]:
                db.execute("INSERT INTO order_items(order_id,product_id,product_name,unit_price_paise,quantity) VALUES(?,?,?,?,?)",
                    (cursor.lastrowid, item["product_id"], item["name"], item["price_paise"], item["quantity"]))
                db.execute("UPDATE products SET inventory=inventory-?,updated_at=? WHERE id=?", (item["quantity"], utc_now(), item["product_id"]))
            confirmations.append({"order_number": number, "business_name": group["business_name"], "total": group["subtotal"] + delivery})
        db.execute("DELETE FROM cart_items WHERE cart_key=?", (session["cart_key"],))
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise
    return render_template("checkout_success.html", orders=confirmations, customer=name, customer_email=email)


@app.route("/track/<order_number>", methods=["GET", "POST"])
def track_order(order_number):
    email = request.form.get("email", "").strip().lower() if request.method == "POST" else ""
    db = get_db()
    order = db.execute("SELECT o.*,s.business_name FROM orders o JOIN seller_profiles s ON s.user_id=o.seller_id WHERE o.order_number=? AND o.customer_email=?", (order_number, email)).fetchone() if email and valid_email(email) else None
    if not order:
        return render_template("track_order.html", order=None, order_number=order_number, attempted=request.method == "POST"), 200
    items = db.execute("SELECT * FROM order_items WHERE order_id=?", (order["id"],)).fetchall()
    return render_template("track_order.html", order=order, items=items, order_number=order_number, attempted=True)


@app.route("/dashboard")
def dashboard():
    if not getattr(g, "user", None):
        return redirect(url_for("login"))
    if g.user["role"] == "owner":
        return redirect(url_for("owner_dashboard"))
    return redirect(url_for("seller_dashboard" if g.user["status"] == "active" else "seller_application"))


@app.route("/seller/application", methods=["GET", "POST"])
def seller_application():
    if not getattr(g, "user", None) or g.user["role"] != "seller":
        return redirect(url_for("login"))
    db = get_db()
    if request.method == "POST":
        if g.user["status"] != "pending":
            abort(403)
        profile = db.execute("""SELECT s.requested_plan_id,p.price_paise FROM seller_profiles s
            LEFT JOIN plans p ON p.id=s.requested_plan_id WHERE s.user_id=?""", (g.user["id"],)).fetchone()
        reference = request.form.get("payment_reference", "").strip().replace(" ", "")
        if not profile or not profile["requested_plan_id"] or not profile["price_paise"]:
            flash("Your selected plan does not require a payment reference.", "error")
        elif not valid_utr(reference):
            flash("Enter the UTR/reference shown by your UPI payment (8–32 letters or numbers).", "error")
        else:
            db.execute("""UPDATE seller_profiles SET payment_reference=?,payment_status='submitted',
                payment_plan_id=requested_plan_id,payment_verified_at=NULL WHERE user_id=?""",
                (reference, g.user["id"]))
            log_event("plan_payment.resubmitted", "seller", g.user["id"], f"plan={profile['requested_plan_id']}")
            db.commit()
            flash("Payment reference resubmitted. BizMate will verify it before plan approval.", "success")
        return redirect(url_for("seller_application"))
    profile = db.execute("""SELECT s.*,p.name AS requested_plan_name,p.price_paise AS requested_plan_price
        FROM seller_profiles s LEFT JOIN plans p ON p.id=s.requested_plan_id WHERE s.user_id=?""", (g.user["id"],)).fetchone()
    return render_template("seller_application.html", profile=profile, status=g.user["status"])


@app.route("/seller/dashboard")
@seller_required
def seller_dashboard():
    db = get_db()
    sid = seller_id()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    stats = db.execute("""SELECT
       COALESCE((SELECT SUM(total_paise) FROM orders WHERE seller_id=? AND payment_status='paid' AND status!='cancelled' AND substr(created_at,1,7)=?),0) AS revenue,
       COALESCE((SELECT SUM(amount_paise) FROM expenses WHERE seller_id=? AND substr(expense_date,1,7)=?),0) AS expenses,
       COALESCE((SELECT SUM(price_paise*inventory) FROM products WHERE seller_id=? AND status!='archived'),0) AS stock_value,
       (SELECT COUNT(*) FROM orders WHERE seller_id=? AND status IN ('pending','processing','shipped')) AS pending_orders,
       (SELECT COUNT(*) FROM products WHERE seller_id=? AND status!='archived') AS product_count""",
       (sid, month, sid, month, sid, sid, sid)).fetchone()
    low_stock = db.execute("SELECT * FROM products WHERE seller_id=? AND status!='archived' AND inventory<=low_stock_at ORDER BY inventory LIMIT 5", (sid,)).fetchall()
    recent_orders = db.execute("SELECT * FROM orders WHERE seller_id=? ORDER BY created_at DESC LIMIT 5", (sid,)).fetchall()
    top_products = db.execute("""SELECT p.id,p.name,p.image_path,SUM(oi.quantity) AS units_sold FROM order_items oi
        JOIN products p ON p.id=oi.product_id JOIN orders o ON o.id=oi.order_id
        WHERE p.seller_id=? AND o.status!='cancelled' GROUP BY p.id ORDER BY units_sold DESC LIMIT 4""", (sid,)).fetchall()
    first_day = (datetime.now(timezone.utc).date() - timedelta(days=6)).isoformat()
    sales_rows = db.execute("SELECT substr(created_at,1,10) AS day,SUM(total_paise) AS amount FROM orders WHERE seller_id=? AND payment_status='paid' AND status!='cancelled' AND created_at>=? GROUP BY day", (sid, first_day)).fetchall()
    by_day = {row["day"]: row["amount"] for row in sales_rows}
    sales_chart = []
    for offset in range(6, -1, -1):
        current_day = datetime.now(timezone.utc).date() - timedelta(days=offset)
        sales_chart.append({"day": current_day.strftime("%a"), "date": current_day.isoformat(), "value": by_day.get(current_day.isoformat(), 0)})
    scale = max([row["value"] for row in sales_chart] + [1])
    coords = [f"{index * 100 / 6:.2f},{92 - row['value'] / scale * 78:.2f}" for index, row in enumerate(sales_chart)]
    return render_template("dashboard.html", stats=stats, low_stock=low_stock, recent_orders=recent_orders,
        top_products=top_products, sales_chart=sales_chart, chart_path="M" + " L".join(coords), today=utc_day(),
        monthly_profit=stats["revenue"] - stats["expenses"])


@app.route("/seller/products")
@seller_required
def seller_products():
    sid = seller_id()
    db = get_db()
    query = request.args.get("q", "").strip()
    if query:
        term = f"%{query}%"
        products = db.execute("SELECT * FROM products WHERE seller_id=? AND (name LIKE ? OR category LIKE ? OR status LIKE ?) ORDER BY created_at DESC", (sid, term, term, term)).fetchall()
    else:
        products = db.execute("SELECT * FROM products WHERE seller_id=? ORDER BY created_at DESC", (sid,)).fetchall()
    categories = sorted({row["category"] for row in products})
    plan = db.execute("SELECT p.* FROM seller_profiles s JOIN plans p ON p.id=s.plan_id WHERE s.user_id=?", (sid,)).fetchone()
    return render_template("seller_products.html", products=products, categories=categories, plan=plan, query=query,
        active_count=db.execute("SELECT COUNT(*) FROM products WHERE seller_id=? AND status!='archived'", (sid,)).fetchone()[0],
        low_stock_count=db.execute("SELECT COUNT(*) FROM products WHERE seller_id=? AND status!='archived' AND inventory<=low_stock_at", (sid,)).fetchone()[0],
        stock_value=db.execute("SELECT COALESCE(SUM(price_paise*inventory),0) FROM products WHERE seller_id=? AND status!='archived'", (sid,)).fetchone()[0])


@app.post("/seller/products/<int:product_id>/stock")
@seller_required
def update_stock(product_id):
    sid = seller_id()
    try:
        inventory = int(request.form.get("inventory", "-1"))
    except ValueError:
        inventory = -1
    if inventory < 0 or inventory > 1000000:
        flash("Stock must be between 0 and 1,000,000 units.", "error")
        return redirect(url_for("seller_products"))
    db = get_db()
    result = db.execute("UPDATE products SET inventory=?,updated_at=? WHERE id=? AND seller_id=?", (inventory, utc_now(), product_id, sid))
    if not result.rowcount:
        abort(404)
    db.commit()
    log_event("product.stock_updated", "product", product_id, str(inventory))
    db.commit()
    flash("Inventory updated.", "success")
    return redirect(request.referrer if safe_next_url(request.referrer) else url_for("seller_products"))


@app.route("/seller/products/new", methods=["GET", "POST"])
@seller_required
def new_product():
    sid = seller_id()
    db = get_db()
    plan = db.execute("SELECT p.* FROM seller_profiles s JOIN plans p ON p.id=s.plan_id WHERE s.user_id=? AND p.is_active=1", (sid,)).fetchone()
    used = db.execute("SELECT COUNT(*) FROM products WHERE seller_id=? AND status!='archived'", (sid,)).fetchone()[0]
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        description = request.form.get("description", "").strip()
        price = money_to_paise(request.form.get("price", ""))
        try:
            inventory = int(request.form.get("inventory", "0"))
        except ValueError:
            inventory = -1
        if not plan:
            flash("An active plan is required. Contact the platform owner.", "error")
        elif plan["product_limit"] < UNLIMITED_PRODUCT_LIMIT and used >= plan["product_limit"]:
            flash("Your plan’s product limit has been reached.", "error")
        elif not name or len(name) > 120 or not category or len(category) > 60 or not description or len(description) > 2000 or price is None or inventory < 0 or inventory > 1000000:
            flash("Enter a name, category, description, valid price, and stock quantity.", "error")
        else:
            image_path, image_error = save_uploaded_image(request.files.get("image"))
            if image_error:
                flash(image_error, "error")
            else:
                now = utc_now()
                cursor = db.execute("""INSERT INTO products(seller_id,name,category,description,price_paise,inventory,image_path,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,'draft',?,?)""", (sid, name, category, description, price, inventory, image_path, now, now))
                product_id = cursor.lastrowid
                db.commit()
                log_event("product.created", "product", product_id, name)
                db.commit()
                flash("Product saved as a draft. Publish it when it is ready.", "success")
                return redirect(url_for("seller_products"))
    return render_template("new_product.html", plan=plan, used_count=used)


@app.route("/seller/products/<int:product_id>/edit", methods=["GET", "POST"])
@seller_required
def edit_product(product_id):
    sid = seller_id()
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id=? AND seller_id=?", (product_id, sid)).fetchone()
    if not product:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        description = request.form.get("description", "").strip()
        price = money_to_paise(request.form.get("price", ""))
        try:
            inventory = int(request.form.get("inventory", "0"))
        except ValueError:
            inventory = -1
        if not name or len(name) > 120 or not category or len(category) > 60 or not description or len(description) > 2000 or price is None or inventory < 0 or inventory > 1000000:
            flash("Enter a name, category, description, valid price, and stock quantity.", "error")
        else:
            image_path = product["image_path"]
            upload = request.files.get("image")
            if upload and upload.filename:
                image_path, image_error = save_uploaded_image(upload)
                if image_error:
                    flash(image_error, "error")
                    return render_template("edit_product.html", product=product)
            db.execute("UPDATE products SET name=?,category=?,description=?,price_paise=?,inventory=?,image_path=?,updated_at=? WHERE id=? AND seller_id=?",
                (name, category, description, price, inventory, image_path, utc_now(), product_id, sid))
            db.commit()
            log_event("product.updated", "product", product_id, name)
            db.commit()
            flash("Product details saved.", "success")
            return redirect(url_for("seller_products"))
    return render_template("edit_product.html", product=product)


@app.post("/seller/products/<int:product_id>/status")
@seller_required
def update_product_status(product_id):
    sid = seller_id()
    status = request.form.get("status", "")
    if status not in {"draft", "published", "archived"}:
        abort(400)
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id=? AND seller_id=?", (product_id, sid)).fetchone()
    if not product:
        abort(404)
    if status == "published" or (status == "draft" and product["status"] == "archived"):
        profile = db.execute("SELECT plan_id FROM seller_profiles WHERE user_id=?", (sid,)).fetchone()
        plan = db.execute("SELECT product_limit,is_active FROM plans WHERE id=?", (profile["plan_id"],)).fetchone() if profile and profile["plan_id"] else None
        count = db.execute("SELECT COUNT(*) FROM products WHERE seller_id=? AND status!='archived'", (sid,)).fetchone()[0]
        if not plan or not plan["is_active"]:
            flash("An active plan is required before listing products.", "error")
            return redirect(url_for("seller_products"))
        if product["status"] == "archived" and plan["product_limit"] < UNLIMITED_PRODUCT_LIMIT and count >= plan["product_limit"]:
            flash("Your plan’s active catalogue product limit has been reached.", "error")
            return redirect(url_for("seller_products"))
        if status == "published" and product["inventory"] < 1:
            flash("Add stock before publishing this product.", "error")
            return redirect(url_for("seller_products"))
    db.execute("UPDATE products SET status=?,updated_at=? WHERE id=? AND seller_id=?", (status, utc_now(), product_id, sid))
    db.commit()
    log_event(f"product.{status}", "product", product_id, product["name"])
    db.commit()
    flash(f"Product is now {status}.", "success")
    return redirect(url_for("seller_products"))


@app.route("/seller/orders")
@seller_required
def seller_orders():
    sid = seller_id()
    status = request.args.get("status", "all")
    if status not in {"all", "pending", "processing", "shipped", "delivered", "cancelled"}:
        status = "all"
    if status == "all":
        orders = get_db().execute("SELECT * FROM orders WHERE seller_id=? ORDER BY created_at DESC", (sid,)).fetchall()
    else:
        orders = get_db().execute("SELECT * FROM orders WHERE seller_id=? AND status=? ORDER BY created_at DESC", (sid, status)).fetchall()
    return render_template("orders.html", orders=orders, active_status=status)


@app.post("/seller/orders/<int:order_id>/update")
@seller_required
def update_order(order_id):
    sid = seller_id()
    status = request.form.get("status", "")
    payment_status = request.form.get("payment_status", "")
    if status not in {"pending", "processing", "shipped", "delivered", "cancelled"} or payment_status not in {"due", "paid"}:
        abort(400)
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    order = db.execute("SELECT * FROM orders WHERE id=? AND seller_id=?", (order_id, sid)).fetchone()
    if not order:
        db.rollback()
        abort(404)
    transitions = {
        "pending": {"pending", "processing", "cancelled"},
        "processing": {"processing", "shipped", "cancelled"},
        "shipped": {"shipped", "delivered"},
        "delivered": {"delivered"},
        "cancelled": {"cancelled", "processing"},
    }
    if status not in transitions[order["status"]]:
        db.rollback()
        flash("That order status change is not allowed. Follow the fulfilment sequence.", "error")
        return redirect(url_for("seller_orders"))
    if payment_status == "paid" and status != "delivered":
        db.rollback()
        flash("Cash-on-delivery orders can be marked paid after delivery is recorded.", "error")
        return redirect(url_for("seller_orders"))
    if status == "cancelled" and payment_status != "due":
        db.rollback()
        flash("A cancelled order cannot be marked paid.", "error")
        return redirect(url_for("seller_orders"))
    items = db.execute("SELECT product_id,quantity FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    if order["status"] != "cancelled" and status == "cancelled":
        for item in items:
            db.execute("UPDATE products SET inventory=inventory+?,updated_at=? WHERE id=? AND seller_id=?",
                (item["quantity"], utc_now(), item["product_id"], sid))
    elif order["status"] == "cancelled" and status != "cancelled":
        for item in items:
            product = db.execute("SELECT inventory FROM products WHERE id=? AND seller_id=?", (item["product_id"], sid)).fetchone()
            if not product or product["inventory"] < item["quantity"]:
                db.rollback()
                flash("There is not enough stock to reopen this cancelled order.", "error")
                return redirect(url_for("seller_orders"))
        for item in items:
            db.execute("UPDATE products SET inventory=inventory-?,updated_at=? WHERE id=? AND seller_id=?",
                (item["quantity"], utc_now(), item["product_id"], sid))
    db.execute("UPDATE orders SET status=?,payment_status=? WHERE id=? AND seller_id=?", (status, payment_status, order_id, sid))
    db.commit()
    log_event("order.updated", "order", order_id, f"{status}/{payment_status}")
    db.commit()
    flash("Order updated.", "success")
    return redirect(request.referrer if safe_next_url(request.referrer) else url_for("seller_orders"))


@app.route("/owner/dashboard")
@owner_required
def owner_dashboard():
    db = get_db()
    stats = db.execute("""SELECT
        SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
        SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,
        SUM(CASE WHEN status='suspended' THEN 1 ELSE 0 END) AS suspended
        FROM users WHERE role='seller'""").fetchone()
    commerce = db.execute("SELECT COUNT(*) AS order_count,COALESCE(SUM(total_paise),0) AS volume FROM orders WHERE status!='cancelled'").fetchone()
    applications = db.execute("""SELECT u.id,u.name,u.email,u.created_at,s.business_name,s.registration_number,
        s.requested_plan_id,s.payment_reference,s.payment_status,s.payment_plan_id,
        p.name AS requested_plan,p.price_paise AS requested_plan_price FROM users u JOIN seller_profiles s ON s.user_id=u.id
        LEFT JOIN plans p ON p.id=s.requested_plan_id WHERE u.role='seller' AND u.status='pending'
        ORDER BY u.created_at LIMIT 8""").fetchall()
    events = db.execute("SELECT a.*,u.name AS actor_name FROM audit_events a LEFT JOIN users u ON u.id=a.actor_user_id ORDER BY a.created_at DESC LIMIT 8").fetchall()
    active_plans = db.execute("SELECT * FROM plans WHERE is_active=1 ORDER BY price_paise,name").fetchall()
    return render_template("owner_dashboard.html", stats=stats, commerce=commerce, applications=applications, events=events, active_plans=active_plans)


@app.route("/owner/sellers")
@owner_required
def owner_sellers():
    status = request.args.get("status", "all")
    query = request.args.get("q", "").strip()
    if status not in {"all", "pending", "active", "rejected", "suspended"}:
        status = "all"
    db = get_db()
    plans = db.execute("SELECT * FROM plans WHERE is_active=1 ORDER BY price_paise").fetchall()
    base_sql = """SELECT u.id,u.name,u.email,u.status,u.created_at,s.business_name,s.registration_number,
        s.approved_at,s.requested_plan_id,s.payment_plan_id,s.payment_reference,s.payment_status,s.payment_verified_at,
        rp.name AS requested_plan,rp.price_paise AS requested_plan_price,ap.name AS plan_name
        FROM users u JOIN seller_profiles s ON s.user_id=u.id
        LEFT JOIN plans rp ON rp.id=s.requested_plan_id LEFT JOIN plans ap ON ap.id=s.plan_id
        WHERE u.role='seller'"""
    filters, params = [], []
    if status != "all":
        filters.append("u.status=?")
        params.append(status)
    if query:
        term = f"%{query}%"
        filters.append("(u.name LIKE ? OR u.email LIKE ? OR s.business_name LIKE ? OR s.registration_number LIKE ?)")
        params.extend([term, term, term, term])
    sql = base_sql + (" AND " + " AND ".join(filters) if filters else "")
    ordering = " ORDER BY CASE u.status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END,u.created_at DESC" if status == "all" else " ORDER BY u.created_at DESC"
    sellers = db.execute(sql + ordering, params).fetchall()
    return render_template("owner_sellers.html", sellers=sellers, plans=plans, active_plans=plans, active_status=status, query=query)


@app.post("/owner/sellers/<int:account_id>/payment")
@owner_required
def review_seller_payment(account_id):
    action = request.form.get("action", "")
    if action not in {"verify", "reject"}:
        abort(400)
    db = get_db()
    profile = db.execute("""SELECT s.*,u.status AS seller_status,p.price_paise AS requested_price
        FROM seller_profiles s JOIN users u ON u.id=s.user_id
        LEFT JOIN plans p ON p.id=s.requested_plan_id
        WHERE s.user_id=? AND u.role='seller'""", (account_id,)).fetchone()
    if not profile:
        abort(404)
    if (profile["seller_status"] != "pending" or profile["requested_price"] is None
            or profile["requested_price"] <= 0 or profile["payment_status"] != "submitted"
            or profile["payment_plan_id"] != profile["requested_plan_id"]
            or not valid_utr(profile["payment_reference"])):
        flash("There is no submitted UTR for the currently requested paid plan to review.", "error")
        return redirect(url_for("owner_sellers", status="pending"))
    if action == "verify":
        db.execute("UPDATE seller_profiles SET payment_status='verified',payment_verified_at=? WHERE user_id=?",
            (utc_now(), account_id))
        log_event("plan_payment.verified", "seller", account_id, f"plan={profile['requested_plan_id']}")
        message = "UTR marked verified. You can now approve the matching paid plan."
    else:
        db.execute("UPDATE seller_profiles SET payment_status='rejected',payment_verified_at=NULL WHERE user_id=?",
            (account_id,))
        log_event("plan_payment.rejected", "seller", account_id, f"plan={profile['requested_plan_id']}")
        message = "UTR rejected. The seller can submit the correct reference from their application page."
    db.commit()
    flash(message, "success")
    return redirect(request.referrer if safe_next_url(request.referrer) else url_for("owner_sellers", status="pending"))


@app.post("/owner/sellers/<int:account_id>/review")
@owner_required
def review_seller(account_id):
    action = request.form.get("action", "")
    db = get_db()
    seller = db.execute("SELECT id FROM users WHERE id=? AND role='seller'", (account_id,)).fetchone()
    if not seller:
        abort(404)
    if action == "approve":
        try:
            plan_id = int(request.form.get("plan_id", "0"))
        except ValueError:
            plan_id = 0
        plan = db.execute("SELECT id,price_paise FROM plans WHERE id=? AND is_active=1", (plan_id,)).fetchone()
        if not plan:
            flash("Select an active plan before approving this seller.", "error")
            return redirect(url_for("owner_sellers", status="pending"))
        profile = db.execute("SELECT requested_plan_id,payment_plan_id,payment_status FROM seller_profiles WHERE user_id=?",
            (account_id,)).fetchone()
        if plan["price_paise"] > 0 and (
                profile["requested_plan_id"] != plan_id
                or profile["payment_plan_id"] != plan_id
                or profile["payment_status"] != "verified"):
            flash("Verify the submitted UTR for this exact paid plan before approving it.", "error")
            return redirect(url_for("owner_sellers", status="pending"))
        db.execute("UPDATE users SET status='active' WHERE id=?", (account_id,))
        db.execute("UPDATE seller_profiles SET plan_id=?,approved_at=? WHERE user_id=?", (plan_id, utc_now(), account_id))
        log_event("seller.approved", "seller", account_id, f"plan={plan_id}")
        message = "Seller approved and plan assigned."
    elif action in {"reject", "suspend", "reactivate"}:
        target = {"reject": "rejected", "suspend": "suspended", "reactivate": "active"}[action]
        if target == "active":
            profile = db.execute("SELECT plan_id FROM seller_profiles WHERE user_id=?", (account_id,)).fetchone()
            plan = db.execute("SELECT is_active FROM plans WHERE id=?", (profile["plan_id"],)).fetchone() if profile and profile["plan_id"] else None
            if not plan or not plan["is_active"]:
                flash("Assign an active plan before reactivating this seller.", "error")
                return redirect(url_for("owner_sellers"))
        db.execute("UPDATE users SET status=? WHERE id=?", (target, account_id))
        log_event(f"seller.{target}", "seller", account_id)
        message = f"Seller account {target}."
    else:
        abort(400)
    db.commit()
    flash(message, "success")
    return redirect(request.referrer if safe_next_url(request.referrer) else url_for("owner_sellers"))


@app.route("/owner/plans", methods=["GET", "POST"])
@owner_required
def owner_plans():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        tier_label = request.form.get("tier_label", "").strip()
        description = request.form.get("description", "").strip()
        features = clean_plan_features(request.form.get("features", ""))
        featured = 1 if request.form.get("is_featured") == "1" else 0
        interval = request.form.get("billing_interval", "monthly")
        price = money_to_paise(request.form.get("price", "0"), allow_zero=True)
        try:
            limit = int(request.form.get("product_limit", "0"))
        except ValueError:
            limit = 0
        if not name or len(name) > 60 or len(tier_label) > 80 or not description or len(description) > 500 or features is None or interval not in {"monthly", "yearly"} or price is None or limit < 1 or limit > UNLIMITED_PRODUCT_LIMIT:
            flash("Enter a name, description, price, billing cycle, and product limit.", "error")
        else:
            try:
                if featured:
                    db.execute("UPDATE plans SET is_featured=0")
                cursor = db.execute("""INSERT INTO plans
                    (name,description,price_paise,billing_interval,product_limit,tier_label,features,is_featured,is_active,created_at)
                    VALUES(?,?,?,?,?,?,?,?,1,?)""",
                    (name, description, price, interval, limit, tier_label, features, featured, utc_now()))
                plan_id = cursor.lastrowid
                db.commit()
                log_event("plan.created", "plan", plan_id, name)
                db.commit()
                flash("Plan created.", "success")
            except sqlite3.IntegrityError:
                db.rollback()
                flash("A plan with that name already exists.", "error")
        return redirect(url_for("owner_plans"))
    plans = db.execute("""SELECT p.*,(SELECT COUNT(*) FROM seller_profiles s JOIN users u ON u.id=s.user_id
        WHERE s.plan_id=p.id AND u.status='active') AS seller_count FROM plans p
        ORDER BY p.is_featured DESC,p.price_paise,p.created_at""").fetchall()
    return render_template("owner_plans.html", plans=plans)


@app.post("/owner/plans/<int:plan_id>/update")
@owner_required
def update_plan(plan_id):
    name = request.form.get("name", "").strip()
    tier_label = request.form.get("tier_label", "").strip()
    description = request.form.get("description", "").strip()
    features = clean_plan_features(request.form.get("features", ""))
    featured = 1 if request.form.get("is_featured") == "1" else 0
    interval = request.form.get("billing_interval", "monthly")
    price = money_to_paise(request.form.get("price", "0"), allow_zero=True)
    try:
        limit = int(request.form.get("product_limit", "0"))
    except ValueError:
        limit = 0
    if not name or len(name) > 60 or len(tier_label) > 80 or not description or len(description) > 500 or features is None or interval not in {"monthly", "yearly"} or price is None or limit < 1 or limit > UNLIMITED_PRODUCT_LIMIT:
        flash("Enter valid plan details.", "error")
        return redirect(url_for("owner_plans"))
    db = get_db()
    try:
        if featured:
            db.execute("UPDATE plans SET is_featured=0 WHERE id!=?", (plan_id,))
        db.execute("""UPDATE plans SET name=?,tier_label=?,description=?,features=?,is_featured=?,
            price_paise=?,billing_interval=?,product_limit=? WHERE id=?""",
            (name, tier_label, description, features, featured, price, interval, limit, plan_id))
        db.commit()
        log_event("plan.updated", "plan", plan_id, name)
        db.commit()
        flash("Plan updated.", "success")
    except sqlite3.IntegrityError:
        db.rollback()
        flash("A plan with that name already exists.", "error")
    return redirect(url_for("owner_plans"))


@app.post("/owner/plans/<int:plan_id>/toggle")
@owner_required
def toggle_plan(plan_id):
    db = get_db()
    plan = db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not plan:
        abort(404)
    active = 0 if plan["is_active"] else 1
    db.execute("UPDATE plans SET is_active=? WHERE id=?", (active, plan_id))
    db.commit()
    log_event("plan.activated" if active else "plan.deactivated", "plan", plan_id, plan["name"])
    db.commit()
    flash("Plan availability updated.", "success")
    return redirect(url_for("owner_plans"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if getattr(g, "user", None):
        return redirect(url_for("dashboard"))
    db = get_db()
    plans = db.execute("SELECT * FROM plans WHERE is_active=1 ORDER BY is_featured DESC,price_paise").fetchall()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        business_name = request.form.get("business_name", "").strip()
        registration_number = request.form.get("registration_number", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirmation", "")
        try:
            requested_plan = int(request.form.get("requested_plan_id", "0"))
        except ValueError:
            requested_plan = 0
        selected_plan = db.execute("SELECT * FROM plans WHERE id=? AND is_active=1", (requested_plan,)).fetchone() if requested_plan else None
        payment_reference = request.form.get("payment_reference", "").strip().replace(" ", "")
        if not name or len(name) > 100 or not business_name or len(business_name) > 120 or not valid_email(email):
            flash("Enter your name, business name, and a valid email.", "error")
        elif not valid_password(password):
            flash("Use at least 12 characters, including letters and numbers.", "error")
        elif password != confirm:
            flash("The passwords do not match.", "error")
        elif not selected_plan:
            flash("Choose an available seller plan.", "error")
        elif selected_plan["price_paise"] > 0 and not valid_utr(payment_reference):
            flash("Pay with the BizMate UPI QR and enter the 8–32 character UTR/reference to request a paid plan.", "error")
        else:
            try:
                cursor = db.execute("INSERT INTO users(name,email,password_hash,role,status,created_at) VALUES(?,?,?,'seller','pending',?)",
                    (name, email, hash_password(password), utc_now()))
                paid = selected_plan["price_paise"] > 0
                db.execute("""INSERT INTO seller_profiles
                    (user_id,business_name,registration_number,requested_plan_id,payment_plan_id,payment_reference,payment_status)
                    VALUES(?,?,?,?,?,?,?)""",
                    (cursor.lastrowid, business_name, registration_number[:80], requested_plan,
                     requested_plan if paid else None, payment_reference if paid else "",
                     "submitted" if paid else "not_required"))
                db.commit()
                return render_template("registration_submitted.html", business_name=business_name,
                    requested_plan=selected_plan["name"], payment_required=paid)
            except sqlite3.IntegrityError:
                db.rollback()
                flash("An account already uses that email.", "error")
    featured = next((plan for plan in plans if plan["is_featured"]), None)
    selected_plan_id = request.form.get("requested_plan_id", type=int) if request.method == "POST" else None
    if not selected_plan_id and featured:
        selected_plan_id = featured["id"]
    qr_available = (BASE_DIR / "static" / "images" / "qr.jpeg").is_file()
    return render_template("register.html", plans=plans, selected_plan_id=selected_plan_id, qr_available=qr_available)


@app.route("/login", methods=["GET", "POST"])
def login():
    if getattr(g, "user", None):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        attempt_key = hashlib.sha256(f"{request.remote_addr or 'unknown'}|{email}".encode()).hexdigest()
        db = get_db()
        attempt = db.execute("SELECT failures,locked_until FROM login_attempts WHERE attempt_key=?", (attempt_key,)).fetchone()
        now = int(time.time())
        if attempt and attempt["locked_until"] > now:
            flash("Sign-in is temporarily locked. Try again later.", "error")
            return render_template("login.html"), 429
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone() if valid_email(email) else None
        if not user or len(password) > 1024 or not check_password_hash(user["password_hash"], password):
            failures = attempt["failures"] + 1 if attempt and attempt["locked_until"] <= now else 1
            locked_until = now + 900 if failures >= 5 else 0
            db.execute("INSERT INTO login_attempts(attempt_key,failures,locked_until) VALUES(?,?,?) ON CONFLICT(attempt_key) DO UPDATE SET failures=excluded.failures,locked_until=excluded.locked_until",
                (attempt_key, failures, locked_until))
            db.commit()
            flash("Email or password is incorrect.", "error")
            return render_template("login.html"), 401
        if user["status"] in {"rejected", "suspended"}:
            flash("This account cannot currently sign in. Contact the platform owner.", "error")
            return render_template("login.html"), 403
        db.execute("DELETE FROM login_attempts WHERE attempt_key=?", (attempt_key,))
        db.execute("UPDATE users SET last_login=? WHERE id=?", (utc_now(), user["id"]))
        db.commit()
        session.clear()
        session.permanent = True
        session["csrf_token"] = secrets.token_urlsafe(32)
        session["cart_key"] = secrets.token_urlsafe(24)
        session["user_id"] = user["id"]
        if user["role"] == "seller" and user["status"] == "pending":
            return redirect(url_for("seller_application"))
        next_path = request.args.get("next")
        if safe_next_url(next_path):
            return redirect(next_path)
        return redirect(url_for("owner_dashboard" if user["role"] == "owner" else "seller_dashboard"))
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", code=400, message=getattr(error, "description", "The request could not be processed.")), 400


@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403, message="You do not have access to this workspace."), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404, message="We could not find that page."), 404


@app.errorhandler(413)
def upload_too_large(_error):
    return render_template("error.html", code=413, message="The upload is too large. Product images must be 5 MB or smaller."), 413


@app.route("/seller/expenses", methods=["GET", "POST"])
@seller_required
def expenses():
    sid = seller_id()
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "Other").strip()
        expense_date = request.form.get("expense_date", utc_day())
        amount = money_to_paise(request.form.get("amount", ""))
        try:
            date.fromisoformat(expense_date)
            date_valid = True
        except ValueError:
            date_valid = False
        if not title or len(title) > 120 or not category or len(category) > 60 or amount is None or not date_valid:
            flash("Enter a description, category, date, and valid amount.", "error")
        else:
            cursor = db.execute("INSERT INTO expenses(seller_id,title,category,amount_paise,expense_date,created_at) VALUES(?,?,?,?,?,?)",
                (sid, title, category, amount, expense_date, utc_now()))
            expense_id = cursor.lastrowid
            db.commit()
            log_event("expense.created", "expense", expense_id, title)
            db.commit()
            flash("Expense recorded.", "success")
        return redirect(url_for("expenses"))
    rows = db.execute("SELECT * FROM expenses WHERE seller_id=? ORDER BY expense_date DESC,id DESC", (sid,)).fetchall()
    return render_template("expenses.html", expenses=rows, total=sum(row["amount_paise"] for row in rows))


@app.post("/seller/expenses/<int:expense_id>/delete")
@seller_required
def delete_expense(expense_id):
    sid = seller_id()
    db = get_db()
    db.execute("DELETE FROM expenses WHERE id=? AND seller_id=?", (expense_id, sid))
    db.commit()
    flash("Expense removed.", "success")
    return redirect(url_for("expenses"))


@app.route("/seller/customers")
@seller_required
def customers():
    rows = get_db().execute("""SELECT customer_name,customer_email,COUNT(*) AS order_count,
        SUM(total_paise) AS total_spend_paise,MAX(created_at) AS last_order
        FROM orders WHERE seller_id=? GROUP BY customer_email ORDER BY last_order DESC""",
        (seller_id(),)).fetchall()
    return render_template("customers.html", customers=rows)


@app.route("/seller/reports")
@seller_required
def reports():
    sid = seller_id()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    db = get_db()
    stats = db.execute("""SELECT
        COALESCE((SELECT SUM(total_paise) FROM orders WHERE seller_id=? AND payment_status='paid' AND status!='cancelled' AND substr(created_at,1,7)=?),0) AS revenue_paise,
        COALESCE((SELECT SUM(amount_paise) FROM expenses WHERE seller_id=? AND substr(expense_date,1,7)=?),0) AS expenses_paise,
        (SELECT COUNT(*) FROM orders WHERE seller_id=?) AS order_count,
        (SELECT COUNT(*) FROM products WHERE seller_id=? AND status!='archived') AS product_count""",
        (sid, month, sid, month, sid, sid)).fetchone()
    products = db.execute("""SELECT p.name,p.image_path,COALESCE(SUM(CASE WHEN o.id IS NOT NULL THEN oi.quantity ELSE 0 END),0) AS units_sold
        FROM products p LEFT JOIN order_items oi ON oi.product_id=p.id
        LEFT JOIN orders o ON o.id=oi.order_id AND o.status!='cancelled'
        WHERE p.seller_id=? AND p.status!='archived' GROUP BY p.id
        ORDER BY units_sold DESC LIMIT 8""", (sid,)).fetchall()
    return render_template("reports.html", stats=stats, products=products)


@app.route("/seller/assistant")
@seller_required
def assistant_page():
    return render_template("assistant.html")


@app.post("/seller/assistant")
@seller_required
def assistant_reply():
    sid = seller_id()
    payload = request.get_json(silent=True) or request.form
    message = (payload.get("message") or "").strip().lower()
    db = get_db()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    revenue = db.execute("SELECT COALESCE(SUM(total_paise),0) FROM orders WHERE seller_id=? AND payment_status='paid' AND status!='cancelled' AND substr(created_at,1,7)=?", (sid, month)).fetchone()[0]
    costs = db.execute("SELECT COALESCE(SUM(amount_paise),0) FROM expenses WHERE seller_id=? AND substr(expense_date,1,7)=?", (sid, month)).fetchone()[0]
    low = db.execute("SELECT name,inventory FROM products WHERE seller_id=? AND status!='archived' AND inventory<=low_stock_at ORDER BY inventory LIMIT 5", (sid,)).fetchall()
    pending = db.execute("SELECT COUNT(*) FROM orders WHERE seller_id=? AND status IN ('pending','processing','shipped')", (sid,)).fetchone()[0]
    if any(word in message for word in ("profit", "revenue", "sales", "month")):
        answer = f"This month, paid sales are {money(revenue)}, expenses are {money(costs)}, and sales less expenses are {money(revenue - costs)}."
    elif any(word in message for word in ("stock", "inventory", "restock")):
        answer = "No products are below their low-stock threshold." if not low else "Low stock: " + ", ".join(f"{row['name']} ({row['inventory']} left)" for row in low) + "."
    elif any(word in message for word in ("order", "pending", "ship")):
        answer = f"There are {pending} orders that are not yet delivered or cancelled."
    else:
        answer = "Ask about paid sales, expenses, low-stock products, or orders waiting for action."
    return jsonify({"answer": answer})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
