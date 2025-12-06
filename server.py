# server.py
import os
import sqlite3
import uuid
import stripe
from flask import Flask, jsonify, request, send_from_directory, abort
from dotenv import load_dotenv
import requests

load_dotenv()

# ---- CONFIG ----
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change_this_admin_token")
RECAPTCHA_SECRET = os.getenv("RECAPTCHA_SECRET")  # optional for anti-fraud
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")  # optional (recommended)

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

# initialize DB
init_db()

# ---- Helpers ----
def create_or_get_customer(email=None):
    # reuse if email exists
    if email:
        rec = find_customer_by_email(email)
        if rec:
            return rec["stripe_customer_id"], rec["internal_id"]
    internal_id = str(uuid.uuid4())
    customer = stripe.Customer.create(email=email, metadata={"internal_id": internal_id})
    save_customer_record(internal_id, email, customer.id)
    return customer.id, internal_id

def verify_recaptcha(token):
    """Optional: verify Google reCAPTCHA v2/v3 token server-side"""
    if not RECAPTCHA_SECRET:
        return True  # not configured
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

# create a checkout session (Stripe Checkout) — saves card for future off-session use
@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    data = request.json or {}
    amount = int(data.get("amount", 0))  # in cents (if you want dynamic price)
    email = data.get("email")
    recaptcha = data.get("recaptcha_token")

    # optional reCAPTCHA check
    if RECAPTCHA_SECRET and not verify_recaptcha(recaptcha):
        return jsonify({"error": "reCAPTCHA verification failed"}), 400

    # ensure we have a Stripe customer (will be created by Checkout if not provided)
    customer_id, internal_id = (None, None)
    if email:
        # try to reuse or create a Stripe customer and prefill the checkout
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

    # store mapping if email provided
    if email:
        # if Stripe created a customer automatically, retrieve it after payment via webhook
        pass

    return jsonify({"url": session.url, "id": session.id, "clientSecret": None})

# webhook - handle events (very important)
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

    # When checkout session completes, save customer info / payment method
    if typ == "checkout.session.completed":
        session = obj
        # fetch PaymentIntent to record details
        if session.get("payment_intent"):
            pi = stripe.PaymentIntent.retrieve(session["payment_intent"])
            record_payment(pi)
            # Save customer to DB if present
            if session.get("customer_email") or pi.get("customer"):
                try:
                    # if customer exists in PI or session, persist mapping
                    cust = None
                    if pi.get("customer"):
                        cust = stripe.Customer.retrieve(pi.get("customer"))
                    else:
                        # optional: search by email
                        pass
                    if cust:
                        save_customer_record(cust.metadata.get("internal_id", str(uuid.uuid4())), cust.email, cust.id)
                except Exception:
                    pass

    # handle other events as needed
    return jsonify({"received": True})

# success page (static file)
@app.route("/success.html")
def success_page():
    return send_from_directory("static", "success.html")

@app.route("/cancel.html")
def cancel_page():
    return send_from_directory("static", "cancel.html")

# ADMIN: list customers (protected)
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

# ADMIN: charge a saved customer (off-session)
@app.route("/admin/charge", methods=["POST"])
def admin_charge_customer():
    require_admin()
    payload = request.json or {}
    customer_id = payload.get("customerId")
    amount = int(payload.get("amount", 0))
    currency = payload.get("currency", "usd")
    if not customer_id or amount <= 0:
        return jsonify({"error": "customerId and positive amount required"}), 400

    # pick first card payment method
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

# ADMIN: create subscription for an existing customer (use saved card)
@app.route("/admin/create-subscription", methods=["POST"])
def admin_create_subscription():
    require_admin()
    data = request.json or {}
    customer_id = data.get("customerId")
    price_id = data.get("priceId")  # a Stripe Price (recurring) id you created in dashboard
    if not customer_id or not price_id:
        return jsonify({"error": "customerId and priceId required"}), 400

    # Attach default payment method if present
    pm_list = stripe.PaymentMethod.list(customer=customer_id, type="card", limit=1)
    if not pm_list.data:
        return jsonify({"error": "No saved card payment method"}), 400
    payment_method_id = pm_list.data[0].id

    try:
        # create subscription using saved payment method
        sub = stripe.Subscription.create(
            customer=customer_id,
            items=[{"price": price_id}],
            default_payment_method=payment_method_id,
            expand=["latest_invoice.payment_intent"]
        )
        return jsonify({"subscription": sub})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    # in production use gunicorn
    app.run(port=int(os.getenv("PORT", 4242)), debug=True)
