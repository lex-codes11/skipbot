# skipbot.py

import os
import json
import datetime
import random
import stripe
from threading import Thread

import discord
from discord import app_commands, ui, Interaction
from discord.ext import commands
from flask import Flask, request, abort

# ---------- CONFIG ----------
DATA_DIR              = 'data'
SALES_FILE            = os.path.join(DATA_DIR, 'skip_sales.json')
PHRASES_FILE          = os.path.join(DATA_DIR, 'skip_passphrases.json')

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
def get_sale_date():
    now = datetime.datetime.now()
    if now.hour < 1:
        return now.date() - datetime.timedelta(days=1)
    return now.date()

def iso_date(dt):
    return dt.isoformat()

def human_date(dt):
    return dt.strftime("%A, %B %-d, %Y")

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

def get_counts():
    sales = load_sales()
    key = iso_date(get_sale_date())
    day = sales.get(key, {"ATL": [], "FL": []})
    return {"ATL": len(day["ATL"]), "FL": len(day["FL"])}

def record_sale(session_id, discord_id, location, sale_date_iso):
    all_sales = load_sales()
    day = all_sales.setdefault(sale_date_iso, {"ATL": [], "FL": []})
    if session_id not in [s["session"] for s in day[location]]:
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

    if event['type'] == 'checkout.session.completed':
        sess = event['data']['object']
        meta       = sess.get('metadata', {})
        user_id    = int(meta.get('discord_id', 0))
        location   = meta.get('location')
        sale_date  = meta.get('sale_date')
        count = record_sale(sess['id'], user_id, location, sale_date)
        user = bot.get_user(user_id)
        if user:
            discord.utils.asyncio.create_task(
                user.send(
                    f"✅ Payment confirmed! You are pass **#{count}/25** "
                    f"for {location} on {human_date(get_sale_date())}."
                )
            )
    return '', 200

def run_web():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    Thread(target=run_web, daemon=True).start()

# ---------- DISCORD BOT SETUP ----------
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

# ---------- BUTTON VIEW ----------
class SkipButtonView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.refresh()

    def refresh(self):
        self.clear_items()
        counts  = get_counts()
        sale_dt = get_sale_date()

        label_atl = (
            f"ATL {human_date(sale_dt)} • {counts['ATL']}/25"
            if counts['ATL'] < 25 else "ATL Sold Out"
        )
        label_fl = (
            f"FL {human_date(sale_dt)} • {counts['FL']}/25"
            if counts['FL'] < 25 else "FL Sold Out"
        )

        self.add_item(ui.Button(
            label=label_atl,
            style=discord.ButtonStyle.success if counts['ATL']<25 else discord.ButtonStyle.secondary,
            custom_id="buy_skip_atl",
            disabled=(counts['ATL']>=25)
        ))
        self.add_item(ui.Button(
            label=label_fl,
            style=discord.ButtonStyle.success if counts['FL']<25 else discord.ButtonStyle.secondary,
            custom_id="buy_skip_fl",
            disabled=(counts['FL']>=25)
        ))

    async def _start_checkout(self, interaction: Interaction, location: str):
        try:
            sale_date_iso = iso_date(get_sale_date())
            ensure_phrases_for(sale_date_iso)
            price = PRICE_ID_ATL if location == 'ATL' else PRICE_ID_FL
            sess = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{'price': price, 'quantity':1}],
                mode='payment',
                success_url=SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=CANCEL_URL,
                metadata={
                    'discord_id': str(interaction.user.id),
                    'location':   location,
                    'sale_date':  sale_date_iso
                }
            )
            await interaction.response.send_message(
                f"💳 Complete purchase: {sess.url}", ephemeral=True
            )
        except Exception as e:
            print("❌ Stripe checkout failed:", e)
            await interaction.response.send_message(
                "⚠️ Could not start checkout. Try again later.", ephemeral=True
            )

    @ui.button(custom_id="buy_skip_atl")
    async def buy_atl(self, interaction: Interaction, button: ui.Button):
        await self._start_checkout(interaction, 'ATL')

    @ui.button(custom_id="buy_skip_fl")
    async def buy_fl(self, interaction: Interaction, button: ui.Button):
        await self._start_checkout(interaction, 'FL')

# ---------- SLASH COMMANDS ----------
@tree.command(name="setup_skip", description="(Owner) Post skip‑line buttons")
async def setup_skip(interaction: Interaction):
    if interaction.user.id != interaction.guild.owner_id:
        return await interaction.response.send_message("⛔ Only the owner.", ephemeral=True)

    channel = bot.get_channel(SKIP_CHANNEL_ID)
    if not channel:
        return await interaction.response.send_message("❌ Bad SKIP_CHANNEL_ID.", ephemeral=True)

    view = SkipButtonView()
    bot.add_view(view)

    # DEFER and then follow up
    await interaction.response.defer(ephemeral=True)
    await channel.send(
        "🎟️ **Skip The Line Passes** — 25 max/night, $25 each. Choose your location & date:",
        view=view
    )
    await interaction.followup.send("✅ Buttons posted.", ephemeral=True)

# (other slash commands unchanged…)

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
