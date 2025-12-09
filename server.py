from flask import Flask, render_template, jsonify, request
import stripe
import os

app = Flask(__name__)

# Stripe keys (set in WSGI for PythonAnywhere Free)
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")

@app.route("/")
def index():
    return render_template("index.html", pk=PUBLISHABLE_KEY)

# Create a customer + SetupIntent to SAVE CARD FOR LATER
@app.route("/create-setup-intent", methods=["POST"])
def create_setup_intent():
    customer = stripe.Customer.create()

    setup_intent = stripe.SetupIntent.create(
        customer=customer.id,
        payment_method_types=["card"],
    )

    return jsonify({
        "clientSecret": setup_intent.client_secret,
        "customerId": customer.id
    })

@app.route("/success")
def success():
    return render_template("success.html")

# OPTIONAL: Charge later endpoint (example)
@app.route("/charge-later", methods=["POST"])
def charge_later():
    data = request.json
    stripe.PaymentIntent.create(
        amount=5000,  # $50.00 later charge example
        currency="usd",
        customer=data["customerId"],
        payment_method=data["paymentMethodId"],
        off_session=True,
        confirm=True,
    )
    return jsonify({"status": "charged"})
