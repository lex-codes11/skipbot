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
SKIP_CHANNEL_ID       = int(os.getenv("SKIP_CHANNEL_ID", "0"))

DAILY_PHRASES = [
    "Pineapples","Kinkster","Certified Freak","Hot Wife","Stag Night",
    "Velvet Vixen","Playroom Pro","Voyeur Vision","After Dark",
    "Bare Temptation","Swing Set","Sultry Eyes","Naughty List",
    "Dom Curious","Unicorn Dust","Cherry Popper","Dirty Martini",
    "Lust Lounge","Midnight Tease","Fantasy Fuel","Room 69","Wet Bar",
    "No Limits","Satin Sheets","Wild Card"
]

# ---------- STRIPE SETUP ----------
stripe.api_key = STRIPE_API_KEY

# ---------- HELPERS ----------
def get_sale_date() -> datetime.date:
    """If before 1 AM EST, count sale toward previous calendar day."""
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    return (now - datetime.timedelta(days=1)).date() if now.hour < 1 else now.date()

def iso_date(dt: datetime.date) -> str:
    return dt.isoformat()

def human_date(dt: datetime.date) -> str:
    return dt.strftime("%A, %B %-d, %Y")

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
    all_phrases = load_json(PHRASES_FILE)
    if date_iso not in all_phrases:
        pool = DAILY_PHRASES.copy()
        random.shuffle(pool)
        all_phrases[date_iso] = pool
        save_json(PHRASES_FILE, all_phrases)
    return all_phrases[date_iso]

def load_sales():
    return load_json(SALES_FILE)

def save_sales(data):
    save_json(SALES_FILE, data)

def record_sale(session_id: str, discord_id: int, location: str, sale_date_iso: str):
    all_sales = load_sales()
    day = all_sales.setdefault(sale_date_iso, {"ATL": [], "FL": []})
    if session_id not in [s["session"] for s in day[location]]:
        day[location].append({"session": session_id, "user": discord_id})
        save_sales(all_sales)
    return len(day[location])

def get_counts():
    key = iso_date(get_sale_date())
    day = load_sales().get(key, {"ATL": [], "FL": []})
    return {"ATL": len(day["ATL"]), "FL": len(day["FL"])}

# ---------- FLASK & WEBHOOK ----------
app = Flask(__name__)

@app.route("/", methods=["GET","HEAD"])
def health():
    return "OK", 200

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
        uid   = int(meta.get("discord_id", 0))
        loc   = meta.get("location")
        sdate = meta.get("sale_date")
        sid   = sess.get("id")
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

# ---------- BUTTON VIEW ----------
class SkipButtonView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.refresh()

    def refresh(self):
        self.clear_items()
        cnts = get_counts()
        sd   = get_sale_date()

        atl_label = (
            f"Buy ATL Skip‑Line Pass\n{human_date(sd)} ({cnts['ATL']}/25)"
            if cnts["ATL"] < 25 else "ATL Sold Out"
        )
        fl_label = (
            f"Buy FL Skip‑Line Pass\n{human_date(sd)} ({cnts['FL']}/25)"
            if cnts["FL"] < 25 else "FL Sold Out"
        )

        self.add_item(ui.Button(
            label=atl_label,
            style=discord.ButtonStyle.success if cnts["ATL"] < 25 else discord.ButtonStyle.secondary,
            custom_id="buy_skip_atl",
            disabled=cnts["ATL"] >= 25
        ))
        self.add_item(ui.Button(
            label=fl_label,
            style=discord.ButtonStyle.success if cnts["FL"] < 25 else discord.ButtonStyle.secondary,
            custom_id="buy_skip_fl",
            disabled=cnts["FL"] >= 25
        ))

    async def _start_checkout(self, inter: Interaction, loc: str):
        # Create Stripe session, then send exactly one response
        iso = iso_date(get_sale_date())
        ensure_phrases_for(iso)
        price = PRICE_ID_ATL if loc == "ATL" else PRICE_ID_FL
        sess = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price, "quantity": 1}],
            mode="payment",
            success_url=SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=CANCEL_URL,
            metadata={
                "discord_id": str(inter.user.id),
                "location":   loc,
                "sale_date":  iso
            }
        )
        await inter.response.send_message(f"💳 Complete your purchase: {sess.url}", ephemeral=True)

    @ui.button(custom_id="buy_skip_atl")
    async def buy_atl(self, button: ui.Button, inter: Interaction):
        await self._start_checkout(inter, "ATL")

    @ui.button(custom_id="buy_skip_fl")
    async def buy_fl(self, button: ui.Button, inter: Interaction):
        await self._start_checkout(inter, "FL")

# ---------- SLASH COMMANDS ----------
@tree.command(name="setup_skip", description="(Owner) Post skip‑line buttons")
async def setup_skip(inter: Interaction):
    # only guild owner may run
    if inter.user.id != inter.guild.owner_id:
        return await inter.response.send_message("⛔ Only the owner.", ephemeral=True)

    view = SkipButtonView()
    bot.add_view(view)  # make persistent across restarts

    # 1) acknowledge once
    await inter.response.send_message("✅ Buttons posted.", ephemeral=True)

    # 2) post the actual buttons
    channel = bot.get_channel(SKIP_CHANNEL_ID)
    if not channel:
        # use followup because initial/only response is done
        return await inter.followup.send("❌ Bad SKIP_CHANNEL_ID.", ephemeral=True)

    await channel.send(
        "🎟️ **Skip The Line Passes**\nLimited to 25 per night, $25 each. Choose location & date:",
        view=view
    )

@tree.command(name="list_phrases", description="(Owner) Show tonight’s passphrases")
async def list_phrases(inter: Interaction):
    if inter.user.id != inter.guild.owner_id:
        return await inter.response.send_message("⛔ Only owner.", ephemeral=True)
    sd   = get_sale_date()
    iso  = iso_date(sd)
    pool = ensure_phrases_for(iso)
    text = f"**Passphrases for {human_date(sd)}**\n" + "\n".join(
        f"{i+1:2d}/25 — {p}" for i,p in enumerate(pool)
    )
    await inter.response.send_message(text, ephemeral=True)

@tree.command(name="list_sales", description="(Owner) List today's sales")
@app_commands.describe(location="ATL or FL")
@app_commands.choices(location=[
    app_commands.Choice(name="ATL", value="ATL"),
    app_commands.Choice(name="FL",  value="FL")
])
async def list_sales(inter: Interaction, location: str):
    if inter.user.id != inter.guild.owner_id:
        return await inter.response.send_message("⛔ Only owner.", ephemeral=True)
    day = load_sales().get(iso_date(get_sale_date()), {"ATL": [], "FL": []})
    lst = day.get(location, [])
    if not lst:
        return await inter.response.send_message(f"No sales for {location}.", ephemeral=True)
    lines = [
        f"{i+1:2d}. {bot.get_user(e['user']).display_name if bot.get_user(e['user']) else 'ID '+str(e['user'])} — `{e['session']}`"
        for i,e in enumerate(lst)
    ]
    text = f"**Sales for {location}:**\n" + "\n".join(lines)
    await inter.response.send_message(text, ephemeral=True)

# ---------- STARTUP & SYNC ----------
@bot.event
async def on_ready():
    keep_alive()
    bot.add_view(SkipButtonView())   # re‑register persistent view
    await tree.sync()
    print(f"✅ SkipBot running as {bot.user}")

# ---------- RUN ----------
if not DISCORD_TOKEN:
    raise ValueError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
