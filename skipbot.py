# skipbot.py

import os, json, datetime, random, stripe
from threading  import Thread
from zoneinfo   import ZoneInfo

import discord
from discord     import app_commands, ui, Interaction
from discord.ext import commands
from flask       import Flask, request, abort

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

# ---------- HELPERS & PERSISTENCE ----------
def get_sale_date() -> datetime.date:
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    return (now - datetime.timedelta(days=1)).date() if now.hour < 1 else now.date()

def iso_date(dt: datetime.date) -> str:
    return dt.isoformat()

def human_date(dt: datetime.date) -> str:
    return dt.strftime("%b %-d, %Y")

def load_json(path):
    with open(path, "r") as f: return json.load(f)

def save_json(path, data):
    with open(path, "w") as f: json.dump(data, f, indent=2)

os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.exists(SALES_FILE):   save_json(SALES_FILE, {})
if not os.path.exists(PHRASES_FILE): save_json(PHRASES_FILE, {})

def load_sales():
    return load_json(SALES_FILE)

def save_sales(all_s):
    save_json(SALES_FILE, all_s)

def get_counts():
    today = iso_date(get_sale_date())
    day   = load_sales().get(today, {"ATL": [], "FL": []})
    return {"ATL": len(day["ATL"]), "FL": len(day["FL"])}

def record_sale(session_id, discord_id, loc, date_iso):
    all_s = load_sales()
    day   = all_s.setdefault(date_iso, {"ATL": [], "FL": []})
    if session_id not in [x["session"] for x in day[loc]]:
        day[loc].append({"session": session_id, "user": discord_id})
        save_sales(all_s)
    return len(day[loc])

def ensure_phrases_for(date_iso):
    d = load_json(PHRASES_FILE)
    if date_iso not in d:
        pool = DAILY_PHRASES.copy(); random.shuffle(pool)
        d[date_iso] = pool; save_json(PHRASES_FILE, d)
    return d[date_iso]

# ---------- FLASK & STRIPE WEBHOOK ----------
app = Flask(__name__)

@app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload, sig = request.get_data(), request.headers.get("Stripe-Signature","")
    try:
        evt = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return abort(400)
    if evt["type"] == "checkout.session.completed":
        sess = evt["data"]["object"]
        meta = sess.get("metadata", {})
        uid, loc, sdate = int(meta.get("discord_id",0)), meta.get("location"), meta.get("sale_date")
        sid = sess.get("id")
        if loc and sdate and sid:
            cnt  = record_sale(sid, uid, loc, sdate)
            user = bot.get_user(uid)
            if user:
                discord.utils.asyncio.create_task(
                    user.send(
                        f"✅ Payment confirmed! You’re pass **#{cnt}/25** for {loc} on "
                        f"{human_date(get_sale_date())}."
                    )
                )
    return "", 200

def run_web():    app.run(host="0.0.0.0", port=8080)
def keep_alive(): Thread(target=run_web, daemon=True).start()

# ---------- DISCORD SETUP ----------
intents = discord.Intents.default(); intents.members = True
bot    = commands.Bot(command_prefix="!", intents=intents)
tree   = bot.tree

# ---------- URL BUTTON VIEW ----------
class URLView(ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(ui.Button(
            label="🔗 Complete Purchase",
            style=discord.ButtonStyle.link,
            url=url
        ))

# ---------- BUY PASS COMMAND ----------
@tree.command(name="buy_pass", description="Purchase a Skip‑Line Pass")
@app_commands.describe(location="ATL or FL")
@app_commands.choices(location=[
    app_commands.Choice(name="ATL", value="ATL"),
    app_commands.Choice(name="FL", value="FL")
])
async def buy_pass(interaction: Interaction, location: str):
    counts = get_counts()
    left   = 25 - counts[location]
    if left <= 0:
        return await interaction.response.send_message(
            f"❌ {location} is sold out for {human_date(get_sale_date())}.",
            ephemeral=True
        )

    # defer (gives you 15 min)
    await interaction.response.defer(ephemeral=True)

    iso   = iso_date(get_sale_date())
    ensure_phrases_for(iso)
    price = PRICE_ID_ATL if location=="ATL" else PRICE_ID_FL

    sess = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": price, "quantity":1}],
        mode="payment",
        success_url=SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=CANCEL_URL,
        metadata={
            "discord_id": str(interaction.user.id),
            "location":   location,
            "sale_date":  iso
        }
    )

    # 1) Notify in‑channel
    await interaction.followup.send(
        f"✅ You have **{left}** tickets left for **{location}** on **{human_date(get_sale_date())}**. "
        "Check your DMs for the purchase link!",
        ephemeral=True
    )

    # 2) DM the user the actual link
    view = URLView(sess.url)
    await interaction.user.send(
        f"💳 Click below to purchase your **{location}** pass (#{26-left}/25):",
        view=view
    )

# ---------- STARTUP & SYNC ----------
@bot.event
async def on_ready():
    keep_alive()
    await tree.sync()
    print(f"✅ SkipBot running as {bot.user}")

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
