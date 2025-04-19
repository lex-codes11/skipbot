# skipbot.py

import os, json, datetime, random, asyncio
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
PHRASES_FILE          = os.path.join(DATA_DIR, "skip_passphrases.json")

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
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now.hour < 1:
        now -= datetime.timedelta(days=1)
    return now.date().isoformat()

def human_date(date_iso: str) -> str:
    return datetime.date.fromisoformat(date_iso).strftime("%A, %B %-d, %Y")

def ensure_phrases_for(date_iso: str) -> list:
    os.makedirs(DATA_DIR, exist_ok=True)
    data = {}
    if os.path.exists(PHRASES_FILE):
        with open(PHRASES_FILE, "r") as f:
            data = json.load(f)
    if date_iso not in data:
        pool = [
            "Pineapples","Kinkster","Certified Freak","Hot Wife","Stag Night",
            "Velvet Vixen","Playroom Pro","Voyeur Vision","After Dark",
            "Bare Temptation","Swing Set","Sultry Eyes","Naughty List",
            "Dom Curious","Unicorn Dust","Cherry Popper","Dirty Martini",
            "Lust Lounge","Midnight Tease","Fantasy Fuel","Room 69","Wet Bar",
            "No Limits","Satin Sheets","Wild Card"
        ]
        random.shuffle(pool)
        data[date_iso] = pool
        with open(PHRASES_FILE, "w") as f:
            json.dump(data, f, indent=2)
    return data[date_iso]

def load_sales() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(SALES_FILE):
        with open(SALES_FILE, "r") as f:
            return json.load(f)
    return {}

def save_sales(all_sales: dict):
    with open(SALES_FILE, "w") as f:
        json.dump(all_sales, f, indent=2)

def record_sale(session_id: str, discord_id: int, location: str, date_iso: str) -> int:
    sales = load_sales()
    day = sales.setdefault(date_iso, {"ATL": [], "FL": []})
    if session_id not in [s["session"] for s in day[location]]:
        day[location].append({"session": session_id, "user": discord_id})
        save_sales(sales)
    return len(day[location])

def get_count(location: str) -> int:
    return len(load_sales().get(get_sale_date(), {}).get(location, []))

# ---------- DISCORD SETUP ----------
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------- TICKET SENDER ----------
async def handle_ticket(uid: int, location: str, date_iso: str, count: int):
    user = await bot.fetch_user(uid)
    human = human_date(date_iso)
    phrases = ensure_phrases_for(date_iso)
    phrase = phrases[count - 1]
    # 1) confirmation
    await user.send(
        f"✅ Payment confirmed! You’re pass **#{count}/{MAX_PER_NIGHT}** for **{location}** on **{human}**."
    )
    # 2) the “ticket”
    ticket = (
        f"🎟 **Skip The Line Pass**\n"
        f"Passphrase: **{phrase}**\n"
        f"Member: {user.display_name}\n"
        f"Valid Date: {human}"
    )
    await user.send(ticket)

# ---------- FLASK / STRIPE WEBHOOK ----------
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
        uid = int(meta.get("discord_id", 0))
        loc = meta.get("location")
        date_iso = meta.get("sale_date")
        sid = sess.get("id")
        if loc and date_iso and sid:
            count = record_sale(sid, uid, loc, date_iso)
            # schedule the DM on the bot's loop
            asyncio.run_coroutine_threadsafe(
                handle_ticket(uid, loc, date_iso, count),
                bot.loop
            )
    return "", 200

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    Thread(target=run_web, daemon=True).start()

# ---------- SLASH COMMANDS ----------
@tree.command(name="atl", description="Purchase an ATL Skip‑Line pass")
async def atl(inter: Interaction):
    date_iso = get_sale_date()
    human = human_date(date_iso)
    sold = get_count("ATL")
    left = MAX_PER_NIGHT - sold
    if left <= 0:
        return await inter.response.send_message(
            f"❌ ATL is sold out ({sold}/{MAX_PER_NIGHT}).", ephemeral=True
        )

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
        f"💳 {left} left for ATL on **{human}** — complete purchase: {sess.url}",
        ephemeral=True
    )

@tree.command(name="fl", description="Purchase an FL Skip‑Line pass")
async def fl(inter: Interaction):
    date_iso = get_sale_date()
    human = human_date(date_iso)
    sold = get_count("FL")
    left = MAX_PER_NIGHT - sold
    if left <= 0:
        return await inter.response.send_message(
            f"❌ FL is sold out ({sold}/{MAX_PER_NIGHT}).", ephemeral=True
        )

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
        f"💳 {left} left for FL on **{human}** — complete purchase: {sess.url}",
        ephemeral=True
    )

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

