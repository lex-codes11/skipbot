# skipbot.py

import os
import json
import datetime
import random
import stripe
from threading import Thread
from zoneinfo import ZoneInfo

import discord
from discord import app_commands, ui, Interaction
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

DAILY_PHRASES = [
    "Pineapples","Kinkster","Certified Freak","Hot Wife","Stag Night",
    "Velvet Vixen","Playroom Pro","Voyeur Vision","After Dark",
    "Bare Temptation","Swing Set","Sultry Eyes","Naughty List",
    "Dom Curious","Unicorn Dust","Cherry Popper","Dirty Martini",
    "Lust Lounge","Midnight Tease","Fantasy Fuel","Room 69","Wet Bar",
    "No Limits","Satin Sheets","Wild Card"
]

stripe.api_key = STRIPE_API_KEY

# ---------- HELPERS ----------
def get_sale_date() -> datetime.date:
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    return (now - datetime.timedelta(days=1)).date() if now.hour < 1 else now.date()

def iso_date(dt: datetime.date) -> str:
    return dt.isoformat()

def human_date(dt: datetime.date) -> str:
    return dt.strftime("%b %-d, %Y")

def load_json(path):
    with open(path, "r") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ---------- PERSISTENCE SETUP ----------
os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.exists(SALES_FILE):
    save_json(SALES_FILE, {})
if not os.path.exists(PHRASES_FILE):
    save_json(PHRASES_FILE, {})

def ensure_phrases_for(date_iso: str):
    data = load_json(PHRASES_FILE)
    if date_iso not in data:
        pool = DAILY_PHRASES.copy()
        random.shuffle(pool)
        data[date_iso] = pool
        save_json(PHRASES_FILE, data)
    return data[date_iso]

def load_sales():
    return load_json(SALES_FILE)

def save_sales(all_sales):
    save_json(SALES_FILE, all_sales)

def record_sale(session_id: str, discord_id: int, location: str, sale_date_iso: str):
    all_sales = load_sales()
    day = all_sales.setdefault(sale_date_iso, {"ATL": [], "FL": []})
    if session_id not in [s["session"] for s in day[location]]:
        day[location].append({"session": session_id, "user": discord_id})
        save_sales(all_sales)
    return len(day[location])

# ---------- FLASK & STRIPE WEBHOOK ----------
app = Flask(__name__)

@app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload, sig = request.get_data(), request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return abort(400)
    if event["type"] == "checkout.session.completed":
        sess = event["data"]["object"]
        meta = sess.get("metadata", {})
        uid, loc, sdate = int(meta.get("discord_id", 0)), meta.get("location"), meta.get("sale_date")
        sid = sess.get("id")
        if loc and sdate and sid:
            cnt = record_sale(sid, uid, loc, sdate)
            user = bot.get_user(uid)
            if user:
                discord.utils.asyncio.create_task(
                    user.send(f"✅ You’re pass **#{cnt}/25** for {loc} on {human_date(get_sale_date())}.")
                )
    return "", 200

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    Thread(target=run_web, daemon=True).start()

# ---------- DISCORD SETUP ----------
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------- URL BUTTON VIEW ----------
class URLView(ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(ui.Button(label="🔗 Complete Purchase", style=discord.ButtonStyle.link, url=url))

# ---------- BUY PASS COMMAND ----------
@tree.command(name="buy_pass", description="Purchase a Skip‑Line Pass")
@app_commands.describe(location="ATL or FL")
@app_commands.choices(location=[
    app_commands.Choice(name="ATL", value="ATL"),
    app_commands.Choice(name="FL", value="FL")
])
async def buy_pass(interaction: Interaction, location: str):
    # 1) defer immediately (gives you 15 min to follow up)
    await interaction.response.defer(ephemeral=True)

    # 2) build your Stripe session
    iso = iso_date(get_sale_date())
    ensure_phrases_for(iso)
    price = PRICE_ID_ATL if location == "ATL" else PRICE_ID_FL

    sess = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": price, "quantity": 1}],
        mode="payment",
        success_url=SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=CANCEL_URL,
        metadata={
            "discord_id": str(interaction.user.id),
            "location":   location,
            "sale_date":  iso
        }
    )

    # 3) follow up with your URL button
    view = URLView(sess.url)
    await interaction.followup.send(
        f"💳 Purchase your **{location}** pass for **{human_date(get_sale_date())}**:",
        view=view,
        ephemeral=True
    )

# ---------- STARTUP & SYNC ----------
@bot.event
async def on_ready():
    keep_alive()
    await tree.sync()
    print(f"✅ SkipBot running as {bot.user}")

# ---------- RUN ----------
if not DISCORD_TOKEN:
    raise ValueError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
