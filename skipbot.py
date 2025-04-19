# skipbot.py

import os, json
import datetime
import random
import asyncio
from io import BytesIO
from threading import Thread
from zoneinfo import ZoneInfo

import stripe
import discord
from discord import app_commands, Interaction, File
from discord.ext import commands
from flask import Flask, request, abort
from PIL import Image, ImageDraw, ImageFont

# ---------- CONFIG ----------
DATA_DIR              = "data"
SALES_FILE            = os.path.join(DATA_DIR, "skip_sales.json")
PHRASES_FILE          = os.path.join(DATA_DIR, "skip_passphrases.json")
LOGO_PATH             = "logo-icon-text.png"  # place your logo here

DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
STRIPE_API_KEY        = os.getenv("STRIPE_API_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
PRICE_ID_ATL          = os.getenv("PRICE_ID_ATL")
PRICE_ID_FL           = os.getenv("PRICE_ID_FL")
SUCCESS_URL           = os.getenv("SUCCESS_URL")
CANCEL_URL            = os.getenv("CANCEL_URL")

MAX_PER_NIGHT         = 25

stripe.api_key = STRIPE_API_KEY

# ---------- HELPERS & PERSISTENCE ----------
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

def ensure_phrases_for(date_iso: str):
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
    day       = all_sales.setdefault(date_iso, {"ATL": [], "FL": []})
    # avoid dupes
    if session_id not in [s["session"] for s in day[location]]:
        day[location].append({"session": session_id, "user": discord_id})
        save_sales(all_sales)
    return len(day[location])

def get_count(location: str) -> int:
    return len(load_sales().get(get_sale_date(), {}).get(location, []))

# ---------- TICKET GENERATOR ----------
async def send_ticket(user: discord.User, phrase: str, date_iso: str, member_id: int):
    # build an 800×400 white canvas
    img = Image.new("RGB", (800,400), "white")
    draw = ImageDraw.Draw(img)

    # logo
    if os.path.exists(LOGO_PATH):
        logo = Image.open(LOGO_PATH).convert("RGBA")
        logo.thumbnail((200,100), Image.ANTIALIAS)
        img.paste(logo, (20,20), logo)

    # fonts
    title_fnt = ImageFont.load_default()
    body_fnt  = ImageFont.load_default()

    # title
    draw.text((250,30), "Skip The Line Pass", font=title_fnt, fill="black")

    # phrase
    draw.text((50,150), f"Passphrase: {phrase}", font=body_fnt, fill="black")

    # name & member ID
    draw.text((50,200), f"Member: {user.display_name}", font=body_fnt, fill="black")
    draw.text((50,230), f"ID: {member_id}", font=body_fnt, fill="black")

    # valid date
    draw.text((50,280), f"Valid: {human_date(date_iso)}", font=body_fnt, fill="black")

    # save to bytes
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    await user.send(file=File(buf, filename="skip_pass.png"))

# ---------- FLASK WEBHOOK ----------
app = Flask(__name__)

@app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload, sig = request.get_data(), request.headers.get("Stripe-Signature","")
    try:
        ev = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return abort(400)

    if ev["type"] == "checkout.session.completed":
        sess    = ev["data"]["object"]
        meta    = sess.get("metadata", {})
        uid     = int(meta.get("discord_id",0))
        loc     = meta.get("location")
        date_iso= meta.get("sale_date")
        sid     = sess.get("id")

        if loc and date_iso and sid:
            cnt      = record_sale(sid, uid, loc, date_iso)
            phrases  = ensure_phrases_for(date_iso)
            phrase   = phrases[cnt-1]  # nth sale → nth phrase
            user     = bot.get_user(uid)
            if user:
                loop = bot.loop
                # send simple text DM
                asyncio.run_coroutine_threadsafe(
                    user.send(
                        f"✅ Payment confirmed! You’re pass **#{cnt}/{MAX_PER_NIGHT}** "
                        f"for **{loc}** on **{human_date(date_iso)}**."
                    ),
                    loop
                )
                # send the fancy ticket image
                asyncio.run_coroutine_threadsafe(
                    send_ticket(user, phrase, date_iso, uid),
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
    sold = get_count("ATL"); left = MAX_PER_NIGHT - sold
    if left <= 0:
        return await inter.response.send_message(
            f"❌ ATL sold out ({sold}/{MAX_PER_NIGHT}).", ephemeral=True
        )
    date_iso = get_sale_date()
    sess = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": PRICE_ID_ATL, "quantity":1}],
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
        f"💳 {left} left for ATL tonight—complete purchase: {sess.url}",
        ephemeral=True
    )

@tree.command(name="fl", description="Purchase an FL Skip‑Line pass")
async def fl(inter: Interaction):
    sold = get_count("FL"); left = MAX_PER_NIGHT - sold
    if left <= 0:
        return await inter.response.send_message(
            f"❌ FL sold out ({sold}/{MAX_PER_NIGHT}).", ephemeral=True
        )
    date_iso = get_sale_date()
    sess = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": PRICE_ID_FL, "quantity":1}],
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
        f"💳 {left} left for FL tonight—complete purchase: {sess.url}",
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
