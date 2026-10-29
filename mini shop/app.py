"""Small, server-rendered storefront backed by SQLite."""
import os
import hmac
import secrets
import sqlite3
from decimal import Decimal
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash
from paytm_gateway import config as paytm_config, fetch_payment_status, initiate_transaction, verify_callback

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
app = Flask(__name__)
app.config.update(SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32), SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.environ.get("PAYTM_ENV", "test").lower() == "production")
DB_PATH = Path(os.environ.get("DATABASE_PATH", ROOT / "store.db"))


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
      price REAL NOT NULL CHECK(price>=0), image TEXT NOT NULL DEFAULT '', stock INTEGER NOT NULL DEFAULT 0 CHECK(stock>=0),
      category TEXT NOT NULL DEFAULT 'General', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS cart_items(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE, quantity INTEGER NOT NULL CHECK(quantity>0),
      PRIMARY KEY(user_id, product_id));
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
      status TEXT NOT NULL DEFAULT 'pending', total REAL NOT NULL, payment_status TEXT NOT NULL DEFAULT 'unpaid',
      payment_order_id TEXT UNIQUE, paytm_txn_token TEXT, payment_txn_id TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
      product_id INTEGER REFERENCES products(id) ON DELETE SET NULL, product_name TEXT NOT NULL, price REAL NOT NULL, quantity INTEGER NOT NULL);
    """)
    # Migrate existing local databases without dropping orders or customer data.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
    for name, declaration in (
        ("payment_status", "TEXT NOT NULL DEFAULT 'unpaid'"),
        ("payment_order_id", "TEXT"),
        ("paytm_txn_token", "TEXT"),
        ("payment_txn_id", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {declaration}")
    if not conn.execute("SELECT 1 FROM products LIMIT 1").fetchone():
        conn.executemany("INSERT INTO products(name,description,price,image,stock,category) VALUES(?,?,?,?,?,?)", [
          ("Everyday Carry Backpack", "Weather-ready canvas, padded laptop sleeve, and thoughtful pockets for daily journeys.", 64.00, "🎒", 18, "Accessories"),
          ("Studio Wireless Headphones", "Immersive sound, soft memory-foam cushions, and up to 30 hours of listening.", 129.00, "🎧", 12, "Tech"),
          ("Ceramic Pour-over Set", "A small-batch stoneware dripper and matching mug, made for slow mornings.", 48.00, "☕", 25, "Home"),
          ("Field Notes Journal", "Lay-flat recycled paper notebook with a durable linen cover.", 18.00, "📓", 40, "Stationery"),
          ("Arc Desk Lamp", "A sculptural, dimmable warm-light desk lamp in brushed aluminum.", 86.00, "💡", 9, "Home"),
          ("Weekend Bottle", "Double-wall stainless steel keeps drinks cold for 24 hours.", 32.00, "🧴", 30, "Accessories")])
    conn.commit()
    conn.close()


@app.before_request
def load_user():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    if request.method == "POST" and request.path != "/payment/callback" and not hmac.compare_digest(request.form.get("csrf_token", ""), session["csrf_token"]):
        abort(400)
    g.user = db().execute("SELECT * FROM users WHERE id=?", (session.get("user_id"),)).fetchone() if session.get("user_id") else None


@app.context_processor
def nav_data():
    count = 0
    if getattr(g, "user", None):
        row = db().execute("SELECT COALESCE(SUM(quantity),0) n FROM cart_items WHERE user_id=?", (g.user["id"],)).fetchone()
        count = row["n"]
    return {"cart_count": count, "csrf_token": session.get("csrf_token", "")}


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not g.user:
            flash("Please sign in to continue.", "info")
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not g.user or not g.user["is_admin"]:
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


@app.get("/")
def home():
    category = request.args.get("category", "")
    sql, params = "SELECT * FROM products WHERE stock>0", []
    if category:
        sql += " AND category=?"; params.append(category)
    products = db().execute(sql + " ORDER BY id DESC", params).fetchall()
    categories = db().execute("SELECT DISTINCT category FROM products ORDER BY category").fetchall()
    return render_template("home.html", products=products, categories=categories, selected=category)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name, email, password = request.form.get("name", "").strip(), request.form.get("email", "").strip().lower(), request.form.get("password", "")
        if not name or len(name) > 80 or "@" not in email or len(password) < 8:
            flash("Enter your name, a valid email, and a password of at least 8 characters.", "error")
        else:
            try:
                cur = db().execute("INSERT INTO users(name,email,password_hash) VALUES(?,?,?)", (name,email,generate_password_hash(password)))
                db().commit(); session.clear(); session["user_id"] = cur.lastrowid
                flash("Welcome to Common Goods.", "success"); return redirect(url_for("home"))
            except sqlite3.IntegrityError:
                flash("That email is already registered.", "error")
    return render_template("auth.html", mode="signup")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email, password = request.form.get("email", "").strip().lower(), request.form.get("password", "")
        user = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear(); session["user_id"] = user["id"]
            return redirect(request.args.get("next") or url_for("home"))
        flash("Email or password was not recognized.", "error")
    return render_template("auth.html", mode="login")


@app.post("/logout")
def logout():
    session.clear(); return redirect(url_for("home"))


@app.post("/cart/add/<int:product_id>")
@login_required
def cart_add(product_id):
    product = db().execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product or product["stock"] < 1: abort(404)
    try: quantity = max(1, min(10, int(request.form.get("quantity", 1))))
    except ValueError: quantity = 1
    db().execute("INSERT INTO cart_items(user_id,product_id,quantity) VALUES(?,?,?) ON CONFLICT(user_id,product_id) DO UPDATE SET quantity=MIN(quantity+excluded.quantity,10)", (g.user["id"],product_id,quantity))
    db().commit(); flash(f"{product['name']} added to your bag.", "success"); return redirect(request.referrer or url_for("home"))


@app.get("/cart")
@login_required
def cart():
    items = db().execute("SELECT c.quantity,p.* FROM cart_items c JOIN products p ON p.id=c.product_id WHERE c.user_id=?", (g.user["id"],)).fetchall()
    return render_template("cart.html", items=items, total=sum(i["price"]*i["quantity"] for i in items))


@app.post("/cart/update/<int:product_id>")
@login_required
def cart_update(product_id):
    try: quantity = int(request.form.get("quantity", 1))
    except ValueError: quantity = 1
    if quantity <= 0: db().execute("DELETE FROM cart_items WHERE user_id=? AND product_id=?", (g.user["id"],product_id))
    else: db().execute("UPDATE cart_items SET quantity=MIN(?,10) WHERE user_id=? AND product_id=?", (quantity,g.user["id"],product_id))
    db().commit(); return redirect(url_for("cart"))


@app.post("/checkout")
@login_required
def checkout():
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        items = conn.execute("SELECT c.quantity,p.* FROM cart_items c JOIN products p ON p.id=c.product_id WHERE c.user_id=?", (g.user["id"],)).fetchall()
        if not items: flash("Your bag is empty.", "info"); return redirect(url_for("cart"))
        if any(i["stock"] < i["quantity"] for i in items):
            conn.rollback(); flash("Some items no longer have enough stock. Please update your bag.", "error"); return redirect(url_for("cart"))
        total = sum(i["price"]*i["quantity"] for i in items)
        order_id = conn.execute("INSERT INTO orders(user_id,total,payment_status) VALUES(?,?,'initiating')", (g.user["id"],total)).lastrowid
        paytm_order_id = f"CG{order_id}-{secrets.token_hex(6)}"
        conn.execute("UPDATE orders SET payment_order_id=? WHERE id=?", (paytm_order_id, order_id))
        for item in items:
            conn.execute("INSERT INTO order_items(order_id,product_id,product_name,price,quantity) VALUES(?,?,?,?,?)", (order_id,item["id"],item["name"],item["price"],item["quantity"]))
            conn.execute("UPDATE products SET stock=stock-? WHERE id=?", (item["quantity"],item["id"]))
        conn.commit()
    except Exception:
        conn.rollback(); raise

    try:
        settings = paytm_config()
        public_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        if not public_url.startswith("https://") or "[" in public_url or "]" in public_url:
            raise RuntimeError("Set PUBLIC_BASE_URL to your public HTTPS tunnel URL (for example, ngrok) so Paytm can reach the callback.")
        amount = f"{Decimal(str(total)).quantize(Decimal('0.01')):.2f}"
        txn_token, _ = initiate_transaction(paytm_order_id, amount, g.user["id"], f"{public_url}{url_for('payment_callback')}")
        conn.execute("UPDATE orders SET paytm_txn_token=? WHERE id=?", (txn_token, order_id))
        conn.execute("DELETE FROM cart_items WHERE user_id=?", (g.user["id"],))
        conn.commit()
        return redirect(url_for("pay_order", order_id=order_id))
    except Exception as exc:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE products SET stock=stock+(SELECT oi.quantity FROM order_items oi WHERE oi.order_id=? AND oi.product_id=products.id) WHERE id IN (SELECT product_id FROM order_items WHERE order_id=? AND product_id IS NOT NULL)", (order_id, order_id))
        conn.execute("UPDATE orders SET payment_status='failed',status='cancelled' WHERE id=? AND payment_status!='paid'", (order_id,))
        conn.commit()
        app.logger.exception("Paytm transaction initiation failed")
        flash(f"Couldn't start Paytm checkout: {exc}", "error")
        return redirect(url_for("cart"))


@app.get("/pay/<int:order_id>")
@login_required
def pay_order(order_id):
    order = db().execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, g.user["id"])).fetchone()
    if not order or not order["paytm_txn_token"]:
        abort(404)
    settings = paytm_config()
    if order["payment_status"] in {"paid", "failed"}:
        return redirect(url_for("payment_return", order_id=order_id))
    return render_template("paytm_checkout.html", order=order, mid=settings["mid"], host=settings["host"])


@app.post("/payment/callback")
def payment_callback():
    fields = request.form.to_dict(flat=True)
    if not fields and request.is_json:
        fields = request.get_json(silent=True) or {}
    try:
        settings = paytm_config()
    except RuntimeError:
        abort(503)
    if not verify_callback(fields, settings["key"]):
        abort(400, "Invalid Paytm callback signature.")
    payment_order_id = fields.get("ORDERID", "")
    if fields.get("MID") != settings["mid"]:
        abort(400, "Paytm callback merchant did not match.")
    order = db().execute("SELECT * FROM orders WHERE payment_order_id=?", (payment_order_id,)).fetchone()
    if not order:
        abort(404)
    expected_amount = f"{Decimal(str(order['total'])).quantize(Decimal('0.01')):.2f}"
    try:
        callback_amount_matches = Decimal(fields.get("TXNAMOUNT", "")) == Decimal(expected_amount)
    except Exception:
        callback_amount_matches = False
    if not callback_amount_matches:
        abort(400, "Paytm callback amount did not match the order.")

    # A valid callback checksum authenticates the post; the Status API is final authority.
    final_status = fetch_payment_status(payment_order_id, settings)
    try:
        final_amount_matches = Decimal(str(final_status.get("txnAmount", ""))) == Decimal(expected_amount)
    except Exception:
        final_amount_matches = False
    if (final_status.get("orderId") != payment_order_id or final_status.get("mid") != settings["mid"]
            or not final_amount_matches):
        abort(400, "Paytm status response did not match the order.")
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    result_status = final_status.get("resultInfo", {}).get("resultStatus")
    if result_status == "TXN_SUCCESS":
        conn.execute("UPDATE orders SET payment_status='paid',payment_txn_id=?,status='processing' WHERE id=? AND payment_status NOT IN ('paid','failed')",
                     (final_status.get("txnId"), order["id"]))
    elif result_status == "TXN_FAILURE":
        if order["payment_status"] not in {"paid", "failed"}:
            conn.execute("UPDATE products SET stock=stock+(SELECT oi.quantity FROM order_items oi WHERE oi.order_id=? AND oi.product_id=products.id) WHERE id IN (SELECT product_id FROM order_items WHERE order_id=? AND product_id IS NOT NULL)", (order["id"], order["id"]))
            conn.execute("UPDATE orders SET payment_status='failed',status='cancelled' WHERE id=?", (order["id"],))
    else:
        conn.execute("UPDATE orders SET payment_status='pending' WHERE id=? AND payment_status NOT IN ('paid','failed')", (order["id"],))
    conn.commit()
    return redirect(url_for("payment_return", order_id=order["id"]))


@app.get("/payment/return/<int:order_id>")
@login_required
def payment_return(order_id):
    order = db().execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, g.user["id"])).fetchone()
    if not order:
        abort(404)
    return render_template("payment_result.html", order=order)


@app.get("/orders")
@login_required
def orders():
    rows = db().execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (g.user["id"],)).fetchall()
    details = {o["id"]: db().execute("SELECT * FROM order_items WHERE order_id=?", (o["id"],)).fetchall() for o in rows}
    return render_template("orders.html", orders=rows, details=details)


@app.get("/admin")
@admin_required
def admin():
    conn = db()
    return render_template("admin.html", products=conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall(),
        orders=conn.execute("SELECT o.*,u.name,u.email FROM orders o JOIN users u ON u.id=o.user_id ORDER BY o.id DESC").fetchall(),
        users=conn.execute("SELECT id,name,email,is_admin,created_at FROM users ORDER BY id DESC").fetchall())


@app.post("/admin/products")
@admin_required
def product_create():
    try:
        name=request.form["name"].strip(); price=float(request.form["price"]); stock=int(request.form["stock"])
        if not name or price<0 or stock<0: raise ValueError
        db().execute("INSERT INTO products(name,description,price,image,stock,category) VALUES(?,?,?,?,?,?)", (name,request.form.get("description", "").strip(),price,request.form.get("image", "📦").strip() or "📦",stock,request.form.get("category", "General").strip() or "General")); db().commit()
        flash("Product added.", "success")
    except (ValueError,KeyError): flash("Check the product details and try again.", "error")
    return redirect(url_for("admin")+"#products")


@app.post("/admin/products/<int:product_id>/update")
@admin_required
def product_update(product_id):
    try:
        name=request.form["name"].strip(); price=float(request.form["price"]); stock=int(request.form["stock"])
        if not name or price<0 or stock<0: raise ValueError
        db().execute("UPDATE products SET name=?,description=?,price=?,image=?,stock=?,category=? WHERE id=?", (name,request.form.get("description", "").strip(),price,request.form.get("image", "📦").strip(),stock,request.form.get("category", "General").strip(),product_id)); db().commit(); flash("Product updated.","success")
    except (ValueError,KeyError): flash("Check the product details and try again.","error")
    return redirect(url_for("admin")+"#products")


@app.post("/admin/products/<int:product_id>/delete")
@admin_required
def product_delete(product_id):
    db().execute("DELETE FROM products WHERE id=?", (product_id,)); db().commit(); flash("Product removed.", "success"); return redirect(url_for("admin")+"#products")


@app.post("/admin/orders/<int:order_id>/status")
@admin_required
def order_status(order_id):
    status=request.form.get("status")
    if status not in {"pending","processing","shipped","delivered","cancelled"}: abort(400)
    order = db().execute("SELECT payment_status FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        abort(404)
    if status in {"processing", "shipped", "delivered"} and order["payment_status"] != "paid":
        flash("An order cannot be prepared or shipped until Paytm confirms payment.", "error")
        return redirect(url_for("admin")+"#orders")
    if status == "cancelled" and order["payment_status"] != "paid":
        flash("A pending payment cannot be cancelled from the admin page.", "error")
        return redirect(url_for("admin")+"#orders")
    db().execute("UPDATE orders SET status=? WHERE id=?", (status,order_id)); db().commit(); flash("Order status updated.","success"); return redirect(url_for("admin")+"#orders")


@app.errorhandler(403)
def forbidden(_error): return render_template("error.html", code=403, message="You do not have access to this page."), 403


@app.errorhandler(404)
def not_found(_error): return render_template("error.html", code=404, message="We couldn't find that page."), 404


init_db()
if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", "5000")))
