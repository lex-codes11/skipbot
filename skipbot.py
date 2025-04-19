# skipbot.py

import os, json, datetime
from threading import Thread
from zoneinfo import ZoneInfo

import stripe
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from flask import Flask, request, abort

# ---------- CONFIG ----------
DATA_DIR              = "data"
SALES_FILE            = os.path.join(DATA_DIR, "skip_sales.json")

DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
STRIPE_API_KEY        = os.getenv("STRIPE_API_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
PRICE_ID_ATL          = os.getenv("PRICE_ID_ATL")
PRICE_ID_FL           = os.getenv("PRICE_ID_FL")
SUCCESS_URL           = os.getenv("SUCCESS_URL")
CANCEL_URL            = os.getenv("CANCEL_URL")

MAX_PER_NIGHT         = 25

stripe.api_key = STRIPE_API_KEY

# ---------- HELPERS ----------
def get_sale_date() -> str:
    """Return ISO date, attributing early‐morning sales to the prior night."""
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now.hour < 1:
        now -= datetime.timedelta(days=1)
    return now.date().isoformat()

def load_sales() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(SALES_FILE):
        return {}
    with open(SALES_FILE, "r") as f:
        return json.load(f)

def save_sales(data: dict):
    with open(SALES_FILE, "w") as f:
        json.dump(data, f, indent=2)

def record_sale(session_id: str, discord_id: int, location: str, date_iso: str) -> int:
    all_sales = load_sales()
    day = all_sales.setdefault(date_iso, {"ATL": [], "FL": []})
    # avoid double‐counting the same session
    if session_id not in [s["session"] for s in day[location]]:
        day[location].append({"session": session_id, "user": discord_id})
        save_sales(all_sales)
    return len(day[location])

def get_count(location: str) -> int:
    return len(load_sales().get(get_sale_date(), {}).get(location, []))

# ---------- FLASK WEBHOOK ----------
app = Flask(__name__)

@app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload, sig = request.get_data(), request.headers.get("Stripe-Signature", "")
    try:
        evt = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return abort(400)
    if evt["type"] == "checkout.session.completed":
        sess = evt["data"]["object"]
        meta = sess.get("metadata", {})
        uid   = int(meta.get("discord_id", 0))
        loc   = meta.get("location")
        date  = meta.get("sale_date")
        sid   = sess.get("id")
        if loc and date and sid:
            cnt = record_sale(sid, uid, loc, date)
            user = bot.get_user(uid)
            if user:
                discord.utils.asyncio.create_task(
                    user.send(
                        f"✅ Payment confirmed! You’re pass **#{cnt}/{MAX_PER_NIGHT}** "
                        f"for **{loc}** on **{datetime.datetime.fromisoformat(date).strftime('%b %-d, %Y')}**."
                    )
                )
    return "", 200

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    Thread(target=run_web, daemon=True).start()

# ---------- DISCORD BOT SETUP ----------
intents = discord.Intents.default()
intents.members = True

bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------- SLASH COMMANDS ----------
@app_commands.command(name="atl", description="Purchase an ATL Skip‑Line pass")
async def atl(inter: Interaction):
    sold = get_count("ATL")
    left = MAX_PER_NIGHT - sold
    if left <= 0:
        await inter.response.send_message(
            f"❌ ATL is sold out for tonight ({sold}/{MAX_PER_NIGHT}).", ephemeral=True
        )
        return

    # build Stripe Checkout
    date_iso = get_sale_date()
    sess = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": PRICE_ID_ATL, "quantity": 1}],
        mode="payment",
        success_url=SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=CANCEL_URL,
        metadata={
            "discord_id": str(inter.user.id),
            "location":   "ATL",
            "sale_date":  date_iso
        }
    )

    await inter.response.send_message(
        f"💳 **{left}** tickets left for ATL tonight. Complete purchase: {sess.url}",
        ephemeral=True
    )

@app_commands.command(name="fl", description="Purchase an FL Skip‑Line pass")
async def fl(inter: Interaction):
    sold = get_count("FL")
    left = MAX_PER_NIGHT - sold
    if left <= 0:
        await inter.response.send_message(
            f"❌ FL is sold out for tonight ({sold}/{MAX_PER_NIGHT}).", ephemeral=True
        )
        return

    date_iso = get_sale_date()
    sess = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": PRICE_ID_FL, "quantity": 1}],
        mode="payment",
        success_url=SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=CANCEL_URL,
        metadata={
            "discord_id": str(inter.user.id),
            "location":   "FL",
            "sale_date":  date_iso
        }
    )

    await inter.response.send_message(
        f"💳 **{left}** tickets left for FL tonight. Complete purchase: {sess.url}",
        ephemeral=True
    )

tree.add_command(atl)
tree.add_command(fl)

# ---------- STARTUP & SYNC ----------
@bot.event
async def on_ready():
    keep_alive()
    await tree.sync()
    print(f"✅ SkipBot online as {bot.user}")

# ---------- RUN ----------
if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
