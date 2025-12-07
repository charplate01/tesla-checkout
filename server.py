# server.py
import os
import sqlite3
import uuid
import stripe
from flask import Flask, jsonify, request, send_from_directory, abort
import requests

# Optional local .env for development
from dotenv import load_dotenv
load_dotenv()

# ---- CONFIG ----
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change_this_admin_token")
RECAPTCHA_SECRET = os.getenv("RECAPTCHA_SECRET")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

if not STRIPE_SECRET_KEY or not STRIPE_PUBLISHABLE_KEY:
    raise Exception("Set STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY in environment")

stripe.api_key = STRIPE_SECRET_KEY

app = Flask(__name__, static_folder="static", static_url_path="")

DB_PATH = "customers.db"

# ---- DB helpers ----
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        internal_id TEXT UNIQUE,
        email TEXT,
        stripe_customer_id TEXT UNIQUE,
        created_at TEXT
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stripe_pid TEXT,
        customer_id TEXT,
        amount INTEGER,
        currency TEXT,
        status TEXT,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

def save_customer_record(internal_id, email, stripe_customer_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO customers (internal_id, email, stripe_customer_id, created_at) VALUES (?, ?, ?, datetime('now'))",
        (internal_id, email, stripe_customer_id)
    )
    conn.commit()
    conn.close()

def find_customer_by_email(email):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT internal_id, email, stripe_customer_id FROM customers WHERE email = ?", (email,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"internal_id": row[0], "email": row[1], "stripe_customer_id": row[2]}
    return None

def record_payment(pi):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO payments (stripe_pid, customer_id, amount, currency, status, created_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (pi.get("id"), pi.get("customer"), pi.get("amount"), pi.get("currency"), pi.get("status"))
    )
    conn.commit()
    conn.close()

# Initialize DB
init_db()

# ---- Helpers ----
def create_or_get_customer(email=None):
    if email:
        rec = find_customer_by_email(email)
        if rec:
            return rec["stripe_customer_id"], rec["internal_id"]
    internal_id = str(uuid.uuid4())
    customer = stripe.Customer.create(email=email, metadata={"internal_id": internal_id})
    save_customer_record(internal_id, email, customer.id)
    return customer.id, internal_id

def verify_recaptcha(token):
    if not RECAPTCHA_SECRET:
        return True
    resp = requests.post(
        "https://www.google.com/recaptcha/api/siteverify",
        data={"secret": RECAPTCHA_SECRET, "response": token},
        timeout=10
    )
    data = resp.json()
    return data.get("success", False)

# ---- Routes ----
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    data = request.json or {}
    amount = int(data.get("amount", 0))
    email = data.get("email")
    recaptcha = data.get("recaptcha_token")

    if RECAPTCHA_SECRET and not verify_recaptcha(recaptcha):
        return jsonify({"error": "reCAPTCHA verification failed"}), 400

    customer_id, internal_id = (None, None)
    if email:
        customer_id, internal_id = create_or_get_customer(email)

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        customer_email=email if not customer_id else None,
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Tesla Event Access"},
                "unit_amount": int(amount) if amount > 0 else 0,
            },
            "quantity": 1,
        }],
        payment_intent_data={"setup_future_usage": "off_session"},
        success_url=request.host_url + "success.html?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=request.host_url + "cancel.html",
    )

    return jsonify({"url": session.url, "id": session.id, "clientSecret": None})

@app.route("/webhook", methods=["POST"])
def webhook_received():
    payload = request.data
    sig = request.headers.get("Stripe-Signature")
    event = None
    try:
        if WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
        else:
            event = request.get_json()
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    typ = event["type"]
    obj = event["data"]["object"]

    if typ == "checkout.session.completed":
        session = obj
        if session.get("payment_intent"):
            pi = stripe.PaymentIntent.retrieve(session["payment_intent"])
            record_payment(pi)
            if session.get("customer_email") or pi.get("customer"):
                try:
                    cust = None
                    if pi.get("customer"):
                        cust = stripe.Customer.retrieve(pi.get("customer"))
                    if cust:
                        save_customer_record(cust.metadata.get("internal_id", str(uuid.uuid4())), cust.email, cust.id)
                except Exception:
                    pass

    return jsonify({"received": True})

@app.route("/success.html")
def success_page():
    return send_from_directory("static", "success.html")

@app.route("/cancel.html")
def cancel_page():
    return send_from_directory("static", "cancel.html")

# ADMIN helpers
def require_admin():
    token = request.headers.get("x-admin-token") or request.args.get("admin_token")
    if not token or token != ADMIN_TOKEN:
        abort(401)

@app.route("/admin/customers", methods=["GET"])
def admin_list_customers():
    require_admin()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT internal_id, email, stripe_customer_id, created_at FROM customers ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    customers = [{"internal_id": r[0], "email": r[1], "stripe_customer_id": r[2], "created_at": r[3]} for r in rows]
    return jsonify({"customers": customers})

@app.route("/admin/charge", methods=["POST"])
def admin_charge_customer():
    require_admin()
    payload = request.json or {}
    customer_id = payload.get("customerId")
    amount = int(payload.get("amount", 0))
    currency = payload.get("currency", "usd")
    if not customer_id or amount <= 0:
        return jsonify({"error": "customerId and positive amount required"}), 400

    pm_list = stripe.PaymentMethod.list(customer=customer_id, type="card", limit=1)
    if not pm_list.data:
        return jsonify({"error": "No saved payment method"}), 400
    payment_method_id = pm_list.data[0].id

    try:
        pi = stripe.PaymentIntent.create(
            amount=amount,
            currency=currency,
            customer=customer_id,
            payment_method=payment_method_id,
            off_session=True,
            confirm=True,
            description="Admin charge"
        )
        record_payment(pi)
        return jsonify({"paymentIntent": pi})
    except stripe.error.CardError as e:
        return jsonify({"error": "card_error", "message": e.user_message}), 402
    except Exception as e:
        return jsonify({"error": "error", "message": str(e)}), 500

@app.route("/admin/create-subscription", methods=["POST"])
def admin_create_subscription():
    require_admin()
    data = request.json or {}
    customer_id = data.get("customerId")
    price_id = data.get("priceId")
    if not customer_id or not price_id:
        return jsonify({"error": "customerId and priceId required"}), 400

    pm_list = stripe.PaymentMethod.list(customer=customer_id, type="card", limit=1)
    if not pm_list.data:
        return jsonify({"error": "No saved card payment method"}), 400
    payment_method_id = pm_list.data[0].id

    try:
        sub = stripe.Subscription.create(
            customer=customer_id,
            items=[{"price": price_id}],
            default_payment_method=payment_method_id,
            expand=["latest_invoice.payment_intent"]
        )
        return jsonify({"subscription": sub})
    except Exception as e:
        return jsonify({"error": str(e)}, 500)

# ---- RUN ----
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
