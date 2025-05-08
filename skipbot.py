import os, json, datetime, random, asyncio
from threading import Thread
from zoneinfo import ZoneInfo

import stripe
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from flask import Flask, request, abort

# ---------- CONFIG ----------
DATA_DIR        = os.getenv("DATA_DIR", "data")
SALES_FILE      = os.path.join(DATA_DIR, "skip_sales.json")
PHRASES_FILE    = os.path.join(DATA_DIR, "skip_passphrases.json")
RSVP_FILE       = os.path.join(DATA_DIR, "vip_rsvps.json")

DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
STRIPE_API_KEY        = os.getenv("STRIPE_API_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
PRICE_ID_ATL          = os.getenv("PRICE_ID_ATL")
PRICE_ID_FL           = os.getenv("PRICE_ID_FL")
SUCCESS_URL           = os.getenv("SUCCESS_URL")
CANCEL_URL            = os.getenv("CANCEL_URL")
MAX_PER_NIGHT         = 25
GUILD_ID              = int(os.getenv("GUILD_ID"))
GUILD                 = discord.Object(id=GUILD_ID)

stripe.api_key = STRIPE_API_KEY

# ---------- STORAGE HELPERS ----------
def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def load_json(path):
    ensure_data_dir()
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)

def save_json(path, data):
    ensure_data_dir()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ---------- DATE HELPERS ----------
def get_sale_date():
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now.hour < 1:
        now -= datetime.timedelta(days=1)
    return now.date().isoformat()

def human_date(date_iso):
    return datetime.date.fromisoformat(date_iso).strftime("%A, %B %-d, %Y")

# ---------- PASSPHRASES ----------
def ensure_phrases_for(date_iso):
    data = load_json(PHRASES_FILE)
    if date_iso not in data:
        pool = [
            "Pineapples","Kinkster","Certified Freak","Hot Wife","Stag Night",
            "Velvet Vixen","Playroom Pro","Voyeur Vision","After Dark",
            "Bare Temptation","Swing Set","Sultry Eyes","Naughty List",
            "Dom Curious","UnicornDust","CherryPopper","DirtyMartini",
            "LustLounge","MidnightTease","FantasyFuel","Room69","WetBar",
            "NoLimits","SatinSheets","WildCard"
        ]
        random.shuffle(pool)
        data[date_iso] = pool
        save_json(PHRASES_FILE, data)
    return data[date_iso]

# ---------- SALES ----------
def load_sales():
    return load_json(SALES_FILE)

def save_sales(data):
    save_json(SALES_FILE, data)

def record_sale(session_id, discord_id, location, date_iso):
    all_sales = load_sales()
    day = all_sales.setdefault(date_iso, {"ATL": [], "FL": []})
    # dedupe
    day[location] = [s for s in day[location] if s["session"] != session_id]
    day[location].append({"session": session_id, "user": discord_id})
    save_sales(all_sales)
    return len(day[location])

def get_count(location):
    return len(load_sales().get(get_sale_date(), {}).get(location, []))

# ---------- VIP RSVPs ----------
def load_rsvps():
    return load_json(RSVP_FILE).get(get_sale_date(), [])

def save_rsvp_entry(entry):
    data = load_json(RSVP_FILE)
    day   = data.setdefault(get_sale_date(), [])
    day.append(entry)
    save_json(RSVP_FILE, data)

# ---------- AUTH HELPERS ----------
def is_owner(inter: Interaction):
    return inter.user.id == inter.guild.owner_id

def has_staff_role(inter: Interaction):
    return any(r.name.lower()=="staff" for r in inter.user.roles) or is_owner(inter)

# ---------- DISCORD SETUP ----------
intents = discord.Intents.default()
intents.members = True
bot    = commands.Bot(command_prefix="!", intents=intents)
tree   = bot.tree

# ---------- RSVP FLOW ----------
@tree.command(name="rsvp", description="(VIP) RSVP for tonight", guild=GUILD)
@app_commands.describe(last_name="Last name on your ID", id_or_dob="4‑digit club ID or DOB (MMDDYY)")
async def rsvp(inter: Interaction, last_name: str, id_or_dob: str):
    # only VIP role
    if "VIP" not in [r.name for r in inter.user.roles]:
        return await inter.response.send_message("⛔ VIPs only.", ephemeral=True)
    date_iso = get_sale_date()
    # generate unique code
    code = "-".join("".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=3)) for _ in range(3))
    entry = {
        "user_id":    inter.user.id,
        "name":       inter.user.display_name,
        "last_name":  last_name,
        "id_or_dob":  id_or_dob,
        "code":       code
    }
    save_rsvp_entry(entry)
    # DM them the ticket
    human = human_date(date_iso)
    await inter.response.send_message(f"✅ RSVP received! Check your DMs for your ticket.", ephemeral=True)
    await inter.user.send(
        f"🎟 **VIP RSVP Ticket**\n"
        f"Member: {inter.user.display_name}\n"
        f"Last Name: {last_name}\n"
        f"Club ID / DOB: {id_or_dob}\n"
        f"Valid Date: {human}\n"
        f"Code: `{code}`"
    )

# ---------- LIST RSVPs (STAFF/OWNER) ----------
@tree.command(name="list_rsvps", description="(Staff) List tonight’s VIP RSVPs", guild=GUILD)
async def list_rsvps(inter: Interaction):
    if not has_staff_role(inter):
        return await inter.response.send_message("⛔ Staff only.", ephemeral=True)
    rsvps = load_rsvps()
    if not rsvps:
        return await inter.response.send_message("No RSVPs for tonight yet.", ephemeral=True)
    lines = ["**VIP RSVPs for " + human_date(get_sale_date()) + "**"]
    for i, e in enumerate(rsvps, start=1):
        lines.append(f"{i:2d}. {e['name']} — Last: {e['last_name']} — ID/DOB: {e['id_or_dob']} — Code: `{e['code']}`")
    # split into chunks of ~1900 chars
    text = "\n".join(lines)
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await inter.response.send_message(chunk, ephemeral=True)

# ---------- FLASK / STRIPE WEBHOOK (unchanged) ----------
app = Flask(__name__)

@app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload, sig = request.get_data(), request.headers.get("Stripe-Signature","")
    try:
        ev = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return abort(400)
    if ev["type"] == "checkout.session.completed":
        s         = ev["data"]["object"]
        uid       = int(s["metadata"].get("discord_id",0))
        loc       = s["metadata"].get("location")
        date_iso  = s["metadata"].get("sale_date")
        sid       = s.get("id")
        cust      = s.get("customer_details",{})
        email     = cust.get("email","—")
        name      = cust.get("name","—")
        if loc and date_iso and sid:
            count = record_sale(sid, uid, loc, date_iso)
            # send DM ticket (reuse code from before)...
            # asyncio.run_coroutine_threadsafe(...)
    return "",200

def run_web():    app.run(host="0.0.0.0", port=8080)
def keep_alive(): Thread(target=run_web, daemon=True).start()

# ---------- STARTUP & SYNC ----------
@bot.event
async def on_ready():
    keep_alive()
    await tree.sync(guild=GUILD)
    print(f"✅ SkipBot online as {bot.user} in guild {GUILD_ID}")

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
