# skipbot.py

import os
import json
import datetime
import random
import stripe
import asyncio

from threading import Thread
from flask import Flask, request, abort

import discord
from discord import app_commands, ui, Interaction
from discord.ext import commands

# ---------- CONFIG ----------
DATA_DIR         = 'data'
SALES_FILE       = os.path.join(DATA_DIR, 'skip_sales.json')
PHRASES_FILE     = os.path.join(DATA_DIR, 'skip_passphrases.json')

DISCORD_TOKEN         = os.getenv('DISCORD_TOKEN')
STRIPE_API_KEY        = os.getenv('STRIPE_API_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
PRICE_ID_ATL          = os.getenv('PRICE_ID_ATL')
PRICE_ID_FL           = os.getenv('PRICE_ID_FL')
SUCCESS_URL           = os.getenv('SUCCESS_URL')
CANCEL_URL            = os.getenv('CANCEL_URL')
SKIP_CHANNEL_ID       = int(os.getenv('SKIP_CHANNEL_ID', '0'))

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
    now = datetime.datetime.now()
    return (now.date() - datetime.timedelta(days=1)) if now.hour < 1 else now.date()

def iso_date(d: datetime.date) -> str:
    return d.isoformat()

def human_date(d: datetime.date) -> str:
    return d.strftime("%A, %B %-d, %Y")

def human_short_date(d: datetime.date) -> str:
    return d.strftime("%b %-d")

def load_json(path):
    with open(path, 'r') as f:
        return json.load(f)

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

# ---------- PERSISTENCE SETUP ----------
os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.exists(SALES_FILE):
    save_json(SALES_FILE, {})
if not os.path.exists(PHRASES_FILE):
    save_json(PHRASES_FILE, {})

def ensure_phrases_for(date_iso):
    p = load_json(PHRASES_FILE)
    if date_iso not in p:
        pool = DAILY_PHRASES.copy()
        random.shuffle(pool)
        p[date_iso] = pool
        save_json(PHRASES_FILE, p)
    return p[date_iso]

def load_sales():
    return load_json(SALES_FILE)

def save_sales(data):
    save_json(SALES_FILE, data)

def get_counts():
    s = load_sales()
    key = iso_date(get_sale_date())
    day = s.get(key, {"ATL": [], "FL": []})
    return {"ATL": len(day["ATL"]), "FL": len(day["FL"])}

def record_sale(session_id, discord_id, location, sale_date_iso):
    all_sales = load_sales()
    day = all_sales.setdefault(sale_date_iso, {"ATL": [], "FL": []})
    if session_id not in [e["session"] for e in day[location]]:
        day[location].append({"session": session_id, "user": discord_id})
        save_sales(all_sales)
    return len(day[location])

# ---------- FLASK WEBHOOK ----------
app = Flask('')

@app.route('/stripe_webhook', methods=['POST'])
def stripe_webhook():
    payload, sig = request.get_data(), request.headers.get('Stripe-Signature','')
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return abort(400)
    if event['type']=="checkout.session.completed":
        sess = event['data']['object']
        meta = sess.get('metadata',{})
        uid   = int(meta.get('discord_id',0))
        loc   = meta.get('location')
        dt    = meta.get('sale_date')
        count = record_sale(sess['id'], uid, loc, dt)
        user  = bot.get_user(uid)
        if user:
            discord.utils.asyncio.create_task(
                user.send(f"✅ Payment confirmed! You are pass **#{count}/25** for {loc} on {human_date(get_sale_date())}.")
            )
    return '',200

def run_web():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    Thread(target=run_web, daemon=True).start()

# ---------- DISCORD BOT SETUP ----------
intents = discord.Intents.default()
intents.members = True
bot  = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

# ---------- BUTTON VIEW ----------
class SkipButtonView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.refresh()

    def refresh(self):
        self.clear_items()
        c = get_counts()
        d = get_sale_date()
        lbl_atl = f"ATL {human_short_date(d)} ({c['ATL']}/25)"
        lbl_fl  = f"FL  {human_short_date(d)} ({c['FL']}/25)"

        self.add_item(ui.Button(
            label=lbl_atl,
            style=discord.ButtonStyle.success if c['ATL']<25 else discord.ButtonStyle.secondary,
            custom_id="buy_skip_atl",
            disabled=(c['ATL']>=25)
        ))
        self.add_item(ui.Button(
            label=lbl_fl,
            style=discord.ButtonStyle.success if c['FL']<25 else discord.ButtonStyle.secondary,
            custom_id="buy_skip_fl",
            disabled=(c['FL']>=25)
        ))

    async def _start_checkout(self, inter: Interaction, loc: str):
        await inter.response.defer(ephemeral=True)
        sale_iso = iso_date(get_sale_date())
        ensure_phrases_for(sale_iso)
        price = PRICE_ID_ATL if loc=='ATL' else PRICE_ID_FL

        sess = await asyncio.to_thread(stripe.checkout.Session.create,
            payment_method_types=['card'],
            line_items=[{'price': price, 'quantity':1}],
            mode='payment',
            success_url = SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url  = CANCEL_URL,
            metadata={'discord_id':str(inter.user.id),'location':loc,'sale_date':sale_iso}
        )
        await inter.followup.send(f"💳 Complete purchase here:\n{sess.url}", ephemeral=True)

    @ui.button(custom_id="buy_skip_atl")
    async def buy_atl(self, b, inter: Interaction):
        await self._start_checkout(inter, 'ATL')

    @ui.button(custom_id="buy_skip_fl")
    async def buy_fl(self, b, inter: Interaction):
        await self._start_checkout(inter, 'FL')

# ---------- SLASH COMMANDS ----------
@tree.command(name="setup_skip", description="(Owner) Post skip‑line buttons")
async def setup_skip(inter: Interaction):
    if inter.user.id != inter.guild.owner_id:
        return await inter.response.send_message("⛔ Only the owner.", ephemeral=True)

    # 1) defer, then continue
    await inter.response.defer(ephemeral=True)

    channel = bot.get_channel(SKIP_CHANNEL_ID)
    if not channel:
        return await inter.followup.send("❌ Invalid SKIP_CHANNEL_ID.", ephemeral=True)

    view = SkipButtonView()
    bot.add_view(view)  # make persistent
    await channel.send(
        "🎟️ **Skip‑Line Passes** — 25 max/night, $25 each.\nPick your location & date:",
        view=view
    )

    # 2) final confirmation
    await inter.followup.send("✅ Buttons have been posted.", ephemeral=True)

@tree.command(name="list_phrases", description="(Owner) Show tonight’s passphrases")
async def list_phrases(inter: Interaction):
    if inter.user.id != inter.guild.owner_id:
        return await inter.response.send_message("⛔ Only the owner.", ephemeral=True)
    d   = get_sale_date(); iso = iso_date(d)
    p   = ensure_phrases_for(iso)
    txt = "**Passphrases for %s**\n" % human_date(d) + "\n".join(f"{i+1:2d}/25 — {w}" for i,w in enumerate(p))
    await inter.response.send_message(txt, ephemeral=True)

# … (repeat your list_sales/remove_sale/move_sale here) …

# ---------- STARTUP & SYNC ----------
@bot.event
async def on_ready():
    keep_alive()
    bot.add_view(SkipButtonView())
    await tree.sync()
    print(f"✅ SkipBot online as {bot.user}")

# ---------- RUN ----------
if not DISCORD_TOKEN:
    raise ValueError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
