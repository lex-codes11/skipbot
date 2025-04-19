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
from discord import app_commands, Interaction
from discord.ext import commands
from flask import Flask, request, abort

# ---------- CONFIG ----------
DATA_DIR              = os.getenv("DATA_DIR", "data")
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

# Guild‑scoped commands (register instantly)
GUILD_ID = int(os.getenv("GUILD_ID"))
GUILD    = discord.Object(id=GUILD_ID)

stripe.api_key = STRIPE_API_KEY

# ---------- HELPERS & STORAGE ----------
def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def load_json(path: str) -> dict:
    ensure_data_dir()
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)

def save_json(path: str, data: dict):
    ensure_data_dir()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def get_sale_date() -> str:
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now.hour < 1:
        now -= datetime.timedelta(days=1)
    return now.date().isoformat()

def human_date(date_iso: str) -> str:
    return datetime.date.fromisoformat(date_iso).strftime("%A, %B %-d, %Y")

def ensure_phrases_for(date_iso: str) -> list[str]:
    data = load_json(PHRASES_FILE)
    if date_iso not in data:
        pool = [
            "Pineapples","Kinkster","Certified Freak","Hot Wife","Stag Night",
            "Velvet Vixen","Playroom Pro","Voyeur Vision","After Dark",
            "Bare Temptation","Swing Set","Sultry Eyes","Naughty List",
            "Dom Curious","Unicorn Dust","Cherry Popper","Dirty Martini",
            "Lust Lounge","Midnight Tease","Fantasy Fuel","Room 69","Wet Bar",
            "No Limits","Satin Sheets","Wild Card"
        ]
        random.shuffle(pool)
        data[date_iso] = pool
        save_json(PHRASES_FILE, data)
    return data[date_iso]

def load_sales() -> dict:
    return load_json(SALES_FILE)

def save_sales(data: dict):
    save_json(SALES_FILE, data)

def record_sale(session_id: str, discord_id: int, location: str,
                date_iso: str, position: int = None) -> int:
    sales = load_sales()
    day   = sales.setdefault(date_iso, {"ATL": [], "FL": []})
    # remove duplicates
    day[location] = [s for s in day[location] if s["session"] != session_id]
    entry = {"session": session_id, "user": discord_id}
    if position and 1 <= position <= len(day[location]):
        day[location].insert(position-1, entry)
    else:
        day[location].append(entry)
    save_sales(sales)
    return len(day[location])

def get_count(location: str) -> int:
    return len(load_sales().get(get_sale_date(), {}).get(location, []))

def is_owner(inter: Interaction) -> bool:
    return inter.user.id == inter.guild.owner_id

# ---------- FLASK / STRIPE WEBHOOK ----------
app = Flask(__name__)

@app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload, sig = request.get_data(), request.headers.get("Stripe-Signature","")
    try:
        ev = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return abort(400)
    if ev["type"] == "checkout.session.completed":
        s = ev["data"]["object"]
        uid      = int(s["metadata"].get("discord_id", 0))
        loc      = s["metadata"].get("location")
        date_iso = s["metadata"].get("sale_date")
        sid      = s.get("id")
        if loc and date_iso and sid:
            record_sale(sid, uid, loc, date_iso)
    return "", 200

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    Thread(target=run_web, daemon=True).start()

# ---------- DISCORD SETUP ----------
intents = discord.Intents.default()
intents.members = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------- USER SLASH COMMANDS ----------
@tree.command(name="atl", description="Purchase an ATL Skip‑Line pass", guild=GUILD)
async def atl(inter: Interaction):
    sold = get_count("ATL")
    left = MAX_PER_NIGHT - sold
    if left <= 0:
        return await inter.response.send_message(
            f"❌ ATL is sold out ({sold}/{MAX_PER_NIGHT}).", ephemeral=True
        )
    date_iso = get_sale_date()
    human    = human_date(date_iso)
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

@tree.command(name="fl", description="Purchase an FL Skip‑Line pass", guild=GUILD)
async def fl(inter: Interaction):
    sold = get_count("FL")
    left = MAX_PER_NIGHT - sold
    if left <= 0:
        return await inter.response.send_message(
            f"❌ FL is sold out ({sold}/{MAX_PER_NIGHT}).", ephemeral=True
        )
    date_iso = get_sale_date()
    human    = human_date(date_iso)
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

# ---------- OWNER‐ONLY SLASH COMMANDS ----------
@tree.command(name="export_sales", description="(Owner) Export sales + passphrases", guild=GUILD)
@app_commands.check(is_owner)
@app_commands.describe(date="YYYY‑MM‑DD (defaults to today)")
async def export_sales(inter: Interaction, date: str = None):
    date_iso = date or get_sale_date()
    sales    = load_sales().get(date_iso, {"ATL": [], "FL": []})
    phrases  = ensure_phrases_for(date_iso)
    lines = [f"**Sales for {human_date(date_iso)}**"]
    for loc in ("ATL","FL"):
        lines.append(f"\n__{loc}__:")
        if not sales[loc]:
            lines.append("  (none)")
        for i, s in enumerate(sales[loc], start=1):
            user = await bot.fetch_user(s["user"])
            name = user.display_name if user else str(s["user"])
            p    = phrases[i-1] if i-1 < len(phrases) else "—"
            lines.append(f"  {i:2d}. {name} — `{p}`")
    text = "\n".join(lines)
    await inter.response.send_message("Here’s the export:", ephemeral=True)
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await inter.followup.send(chunk)

@export_sales.error
async def export_sales_on_error(inter: Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await inter.response.send_message("⛔ Only the owner can use this.", ephemeral=True)

@tree.command(name="list_phrases", description="(Owner) Show tonight’s passphrases", guild=GUILD)
@app_commands.check(is_owner)
async def list_phrases(inter: Interaction):
    date_iso = get_sale_date()
    phrases  = ensure_phrases_for(date_iso)
    human    = human_date(date_iso)
    lines = [f"**Passphrases for {human}:**"]
    for i,p in enumerate(phrases, start=1):
        lines.append(f"{i:2d}/25 — `{p}`")
    await inter.response.send_message("\n".join(lines), ephemeral=True)

@list_phrases.error
async def list_phrases_on_error(inter: Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await inter.response.send_message("⛔ Only the owner can use this.", ephemeral=True)

@tree.command(name="add_sale", description="(Owner) Add a sale manually", guild=GUILD)
@app_commands.check(is_owner)
@app_commands.describe(location="ATL or FL", member="Which member to add", position="Slot number (1…n)")
@app_commands.choices(location=[
    app_commands.Choice(name="ATL", value="ATL"),
    app_commands.Choice(name="FL", value="FL")
])
async def add_sale(inter: Interaction, location: str, member: discord.Member, position: int=None):
    date_iso = get_sale_date()
    sid      = f"manual-{member.id}-{int(datetime.datetime.now().timestamp())}"
    cnt      = record_sale(sid, member.id, location, date_iso, position)
    await inter.response.send_message(f"✅ Added {member.display_name} to {location} as #{cnt}.", ephemeral=True)

@tree.command(name="remove_sale", description="(Owner) Remove a sale", guild=GUILD)
@app_commands.check(is_owner)
@app_commands.describe(location="ATL or FL", index="Sale slot number to remove")
@app_commands.choices(location=[
    app_commands.Choice(name="ATL", value="ATL"),
    app_commands.Choice(name="FL", value="FL")
])
async def remove_sale(inter: Interaction, location: str, index: int):
    date_iso = get_sale_date()
    sales    = load_sales()
    day      = sales.get(date_iso, {"ATL": [], "FL": []})
    if 1 <= index <= len(day[location]):
        removed = day[location].pop(index-1)
        save_sales(sales)
        user = await bot.fetch_user(removed["user"])
        name = user.display_name if user else str(removed["user"])
        await inter.response.send_message(f"🗑️ Removed {name} from {location}.", ephemeral=True)
    else:
        await inter.response.send_message("❌ Invalid index.", ephemeral=True)

@tree.command(name="move_sale", description="(Owner) Move a sale ATL↔FL", guild=GUILD)
@app_commands.check(is_owner)
@app_commands.describe(from_loc="From (ATL/FL)", to_loc="To (ATL/FL)", index="Slot number to move")
@app_commands.choices(
    from_loc=[app_commands.Choice(name="ATL", value="ATL"), app_commands.Choice(name="FL", value="FL")],
    to_loc=  [app_commands.Choice(name="ATL", value="ATL"), app_commands.Choice(name="FL", value="FL")]
)
async def move_sale(inter: Interaction, from_loc: str, to_loc: str, index: int):
    if from_loc == to_loc:
        return await inter.response.send_message("❌ from_loc and to_loc must differ.", ephemeral=True)
    date_iso = get_sale_date()
    sales    = load_sales()
    day      = sales.get(date_iso, {"ATL": [], "FL": []})
    src, dst = day[from_loc], day[to_loc]
    if 1 <= index <= len(src):
        entry = src.pop(index-1)
        dst.append(entry)
        save_sales(sales)
        user = await bot.fetch_user(entry["user"])
        await inter.response.send_message(f"🔀 Moved {user.display_name if user else entry['user']} → {to_loc}.", ephemeral=True)
    else:
        await inter.response.send_message("❌ Invalid index.", ephemeral=True)

# ---------- STARTUP & SYNC ----------
@bot.event
async def on_ready():
    keep_alive()
    # register everything into your guild immediately
    await tree.sync(guild=GUILD)
    print(f"✅ SkipBot online as {bot.user} in guild {GUILD_ID}")

# ---------- RUN ----------
if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
