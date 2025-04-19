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
def get_sale_date() -> datetime.date:
    """Return today's date, or if before 1 AM EST, treat as yesterday."""
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    return (now - datetime.timedelta(days=1)).date() if now.hour < 1 else now.date()

def iso_date(dt: datetime.date) -> str:
    return dt.isoformat()

def human_date(dt: datetime.date) -> str:
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

def ensure_phrases_for(date_iso: str):
    all_p = load_json(PHRASES_FILE)
    if date_iso not in all_p:
        pool = DAILY_PHRASES.copy()
        random.shuffle(pool)
        all_p[date_iso] = pool
        save_json(PHRASES_FILE, all_p)
    return all_p[date_iso]

def load_sales():
    return load_json(SALES_FILE)

def save_sales(data):
    save_json(SALES_FILE, data)

def get_counts():
    sales = load_sales()
    day = sales.get(iso_date(get_sale_date()), {"ATL": [], "FL": []})
    return {"ATL": len(day["ATL"]), "FL": len(day["FL"])}

def record_sale(session_id: str, discord_id: int, location: str, sale_date_iso: str):
    all_s = load_sales()
    day = all_s.setdefault(sale_date_iso, {"ATL": [], "FL": []})
    if session_id not in [s["session"] for s in day[location]]:
        day[location].append({"session": session_id, "user": discord_id})
        save_sales(all_s)
    return len(day[location])

# ---------- FLASK WEBHOOK & HEALTH-CHECK ----------
app = Flask('')

@app.route('/', methods=['GET','HEAD'])
def health():
    return "SkipBot OK", 200

@app.route('/stripe_webhook', methods=['POST'])
def stripe_webhook():
    payload, sig = request.get_data(), request.headers.get('Stripe-Signature','')
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return abort(400)
    if event['type'] == "checkout.session.completed":
        sess   = event['data']['object']
        meta   = sess.get('metadata', {})
        uid    = int(meta.get('discord_id', 0))
        loc    = meta.get('location')
        s_date = meta.get('sale_date')
        sid    = sess.get('id')
        if loc and s_date and sid:
            cnt = record_sale(sid, uid, loc, s_date)
            user = bot.get_user(uid)
            if user:
                # notify purchaser
                discord.utils.asyncio.create_task(
                    user.send(f"✅ You’re pass **#{cnt}/25** for {loc} on {human_date(get_sale_date())}.")
                )
    return '', 200

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
        cnts = get_counts()
        sd   = get_sale_date()

        atl_lbl = (
            f"Buy ATL Skip‑Line Pass\n{human_date(sd)} ({cnts['ATL']}/25)"
            if cnts['ATL'] < 25 else "ATL Sold Out"
        )
        fl_lbl  = (
            f"Buy FL Skip‑Line Pass\n{human_date(sd)} ({cnts['FL']}/25)"
            if cnts['FL'] < 25 else "FL Sold Out"
        )

        self.add_item(ui.Button(
            label=atl_lbl,
            style=discord.ButtonStyle.success if cnts['ATL'] < 25 else discord.ButtonStyle.secondary,
            custom_id="buy_skip_atl",
            disabled=cnts['ATL'] >= 25
        ))
        self.add_item(ui.Button(
            label=fl_lbl,
            style=discord.ButtonStyle.success if cnts['FL'] < 25 else discord.ButtonStyle.secondary,
            custom_id="buy_skip_fl",
            disabled=cnts['FL'] >= 25
        ))

    async def _checkout(self, inter: Interaction, loc: str):
        # 1) defer to ACK immediately
        await inter.response.defer(ephemeral=True)
        # 2) ensure today's phrases exist
        iso = iso_date(get_sale_date())
        ensure_phrases_for(iso)
        # 3) create Stripe session
        price = PRICE_ID_ATL if loc=='ATL' else PRICE_ID_FL
        sess = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{'price': price, 'quantity': 1}],
            mode='payment',
            success_url=SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=CANCEL_URL,
            metadata={
                'discord_id': str(inter.user.id),
                'location':    loc,
                'sale_date':   iso
            }
        )
        # 4) follow up with link
        await inter.followup.send(f"💳 Complete your purchase: {sess.url}", ephemeral=True)

    @ui.button(custom_id="buy_skip_atl")
    async def buy_atl(self, _btn: ui.Button, inter: Interaction):
        await self._checkout(inter, 'ATL')

    @ui.button(custom_id="buy_skip_fl")
    async def buy_fl(self, _btn: ui.Button, inter: Interaction):
        await self._checkout(inter, 'FL')

# ---------- SLASH COMMANDS ----------
@tree.command(name="setup_skip", description="(Owner) Post skip‑line buttons")
async def setup_skip(inter: Interaction):
    # only the server owner may call
    if inter.user.id != inter.guild.owner_id:
        return await inter.response.send_message("⛔ Only the owner.", ephemeral=True)

    view = SkipButtonView()
    bot.add_view(view)  # persist across restarts

    # 1) acknowledge
    await inter.response.defer(ephemeral=True)
    # 2) post into your designated channel
    channel = bot.get_channel(SKIP_CHANNEL_ID)
    if not channel:
        return await inter.followup.send("❌ Bad SKIP_CHANNEL_ID.", ephemeral=True)

    await channel.send(
        "🎟️ **Skip The Line Passes**\nLimited to 25 per night, $25 each. Choose location & date:",
        view=view
    )
    # 3) confirm to command issuer
    await inter.followup.send("✅ Buttons have been posted.", ephemeral=True)

@tree.command(name="list_phrases", description="(Owner) Show tonight’s passphrases")
async def list_phrases(inter: Interaction):
    if inter.user.id != inter.guild.owner_id:
        return await inter.response.send_message("⛔ Only the owner.", ephemeral=True)
    sd      = get_sale_date()
    iso     = iso_date(sd)
    phrases = ensure_phrases_for(iso)
    text    = "**Passphrases for %s**\n%s" % (
        human_date(sd),
        "\n".join(f"{i+1:2d}/25 — {p}" for i,p in enumerate(phrases))
    )
    await inter.response.send_message(text, ephemeral=True)

@tree.command(
    name="list_sales",
    description="(Owner) List today's sales for a location"
)
@app_commands.describe(location="ATL or FL")
@app_commands.choices(location=[
    app_commands.Choice(name="ATL", value="ATL"),
    app_commands.Choice(name="FL",  value="FL")
])
async def list_sales(inter: Interaction, location: str):
    if inter.user.id != inter.guild.owner_id:
        return await inter.response.send_message("⛔ Only the owner.", ephemeral=True)
    day = load_sales().get(iso_date(get_sale_date()), {"ATL": [], "FL": []})
    entries = day.get(location, [])
    if not entries:
        return await inter.response.send_message(
            f"No sales for {location}.", ephemeral=True)
    lines = []
    for i,e in enumerate(entries, start=1):
        u = bot.get_user(e["user"])
        name = u.display_name if u else f"ID {e['user']}"
        lines.append(f"{i:2d}. {name} — session `{e['session']}`")
    text = f"**Sales for {location}:**\n" + "\n".join(lines)
    await inter.response.send_message(text, ephemeral=True)

@tree.command(
    name="remove_sale",
    description="(Owner) Remove a sale by number"
)
@app_commands.describe(
    location="ATL or FL",
    index="Sale number from /list_sales"
)
@app_commands.choices(location=[
    app_commands.Choice(name="ATL", value="ATL"),
    app_commands.Choice(name="FL",  value="FL")
])
async def remove_sale(inter: Interaction, location: str, index: int):
    if inter.user.id != inter.guild.owner_id:
        return await inter.response.send_message("⛔ Only the owner.", ephemeral=True)
    sales = load_sales()
    day = sales.setdefault(iso_date(get_sale_date()), {"ATL": [], "FL": []})
    lst = day.get(location, [])
    if index<1 or index>len(lst):
        return await inter.response.send_message(
            f"❌ {location} has {len(lst)} sales.", ephemeral=True)
    removed = lst.pop(index-1)
    save_sales(sales)
    u = bot.get_user(removed["user"])
    name = u.display_name if u else f"ID {removed['user']}"
    await inter.response.send_message(
        f"🗑️ Removed #{index} for {location} — {name}.", ephemeral=True)

@tree.command(
    name="move_sale",
    description="(Owner) Move a sale between locations"
)
@app_commands.describe(
    from_loc="From (ATL/FL)",
    to_loc="To (ATL/FL)",
    index="Sale number from /list_sales"
)
@app_commands.choices(
    from_loc=[app_commands.Choice(name="ATL", value="ATL"),
              app_commands.Choice(name="FL",  value="FL")],
    to_loc  =[app_commands.Choice(name="ATL", value="ATL"),
              app_commands.Choice(name="FL",  value="FL")]
)
async def move_sale(inter: Interaction, from_loc: str, to_loc: str, index: int):
    if inter.user.id != inter.guild.owner_id:
        return await inter.response.send_message("⛔ Only the owner.", ephemeral=True)
    if from_loc == to_loc:
        return await inter.response.send_message(
            "❌ from_loc and to_loc must differ.", ephemeral=True)
    sales = load_sales()
    day = sales.setdefault(iso_date(get_sale_date()), {"ATL": [], "FL": []})
    src, dst = day[from_loc], day[to_loc]
    if index<1 or index>len(src):
        return await inter.response.send_message(
            f"❌ {from_loc} has {len(src)} sales.", ephemeral=True)
    entry = src.pop(index-1)
    dst.append(entry)
    save_sales(sales)
    u = bot.get_user(entry["user"])
    name = u.display_name if u else f"ID {entry['user']}"
    await inter.response.send_message(
        f"🔀 Moved #{index} from {from_loc} to {to_loc} for {name}.",
        ephemeral=True
    )

# ---------- STARTUP & SYNC ----------
@bot.event
async def on_ready():
    keep_alive()
    bot.add_view(SkipButtonView())      # re‑register buttons after restart
    await tree.sync()
    print(f"✅ SkipBot online as {bot.user}")

# ---------- RUN ----------
if not DISCORD_TOKEN:
    raise ValueError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
