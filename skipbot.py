# skipbot.py

import os
import json
import datetime
import random
import asyncio
from threading import Thread
from zoneinfo import ZoneInfo

import stripe
import discord
from discord import app_commands, Interaction, File, Embed, Color
from discord.ext import commands
from flask import Flask, request, abort

# ---------- CONFIG ----------
DATA_DIR              = "data"
SALES_FILE            = os.path.join(DATA_DIR, "skip_sales.json")
PHRASES_FILE          = os.path.join(DATA_DIR, "skip_passphrases.json")
LOGO_PATH             = "logo-icon-text.png"  # put your logo here

DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
STRIPE_API_KEY        = os.getenv("STRIPE_API_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
PRICE_ID_ATL          = os.getenv("PRICE_ID_ATL")
PRICE_ID_FL           = os.getenv("PRICE_ID_FL")
SUCCESS_URL           = os.getenv("SUCCESS_URL")
CANCEL_URL            = os.getenv("CANCEL_URL")

MAX_PER_NIGHT         = 25

stripe.api_key = STRIPE_API_KEY

# ---------- HELPERS & STORAGE ----------
def get_sale_date() -> str:
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now.hour < 1:
        now -= datetime.timedelta(days=1)
    return now.date().isoformat()

def human_date(date_iso: str) -> str:
    dt = datetime.date.fromisoformat(date_iso)
    return dt.strftime("%A, %B %-d, %Y")

def load_json(path: str) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)

def save_json(path: str, data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def ensure_phrases_for(date_iso: str) -> list[str]:
    data = load_json(PHRASES_FILE)
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
        save_json(PHRASES_FILE, data)
    return data[date_iso]

def load_sales() -> dict:
    return load_json(SALES_FILE)

def save_sales(data: dict):
    save_json(SALES_FILE, data)

def record_sale(session_id: str, discord_id: int, location: str, date_iso: str) -> int:
    all_sales = load_sales()
    day = all_sales.setdefault(date_iso, {"ATL": [], "FL": []})
    if session_id not in [s["session"] for s in day[location]]:
        day[location].append({"session": session_id, "user": discord_id})
        save_sales(all_sales)
    return len(day[location])

def get_count(location: str) -> int:
    return len(load_sales().get(get_sale_date(), {}).get(location, []))

# ---------- TICKET EMBED & DM ----------
async def dm_ticket(user: discord.User, phrase: str, date_iso: str):
    embed = Embed(
        title="🎟 Skip The Line Pass",
        color=Color.pink()
    )
    embed.set_thumbnail(url="attachment://logo-icon-text.png")
    embed.add_field(name="Passphrase",   value=phrase,                 inline=False)
    embed.add_field(name="Member",       value=user.display_name,     inline=True)
    embed.add_field(name="Valid Date",   value=human_date(date_iso),  inline=True)

    file = File(LOGO_PATH, filename="logo-icon-text.png")
    await user.send(embed=embed, file=file)

# ---------- FLASK WEBHOOK ----------
app = Flask(__name__)

@app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload, sig = request.get_data(), request.headers.get("Stripe-Signature", "")
    try:
        ev = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return abort(400)

    if ev["type"] == "checkout.session.completed":
        sess     = ev["data"]["object"]
        meta     = sess.get("metadata", {})
        uid      = int(meta.get("discord_id", 0))
        loc      = meta.get("location")
        date_iso = meta.get("sale_date")
        sid      = sess.get("id")

        if loc and date_iso and sid:
            count   = record_sale(sid, uid, loc, date_iso)
            phrases = ensure_phrases_for(date_iso)
            phrase  = phrases[count - 1]
            user    = bot.get_user(uid)
            if user:
                loop = bot.loop
                # confirmation DM
                asyncio.run_coroutine_threadsafe(
                    user.send(
                        f"✅ Payment confirmed! You’re pass **#{count}/{MAX_PER_NIGHT}** "
                        f"for **{loc}** on **{human_date(date_iso)}**."
                    ),
                    loop
                )
                # ticket DM
                asyncio.run_coroutine_threadsafe(
                    dm_ticket(user, phrase, date_iso),
                    loop
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
@tree.command(name="atl", description="Purchase an ATL Skip‑Line pass")
async def atl(inter: Interaction):
    sold = get_count("ATL")
    left = MAX_PER_NIGHT - sold
    if left <= 0:
        return await inter.response.send_message(
            f"❌ ATL is sold out ({sold}/{MAX_PER_NIGHT}).", ephemeral=True
        )

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
        f"💳 **{left}** left for ATL tonight – complete purchase: {sess.url}",
        ephemeral=True
    )

@tree.command(name="fl", description="Purchase an FL Skip‑Line pass")
async def fl(inter: Interaction):
    sold = get_count("FL")
    left = MAX_PER_NIGHT - sold
    if left <= 0:
        return await inter.response.send_message(
            f"❌ FL is sold out ({sold}/{MAX_PER_NIGHT}).", ephemeral=True
        )

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
        f"💳 **{left}** left for FL tonight – complete purchase: {sess.url}",
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
