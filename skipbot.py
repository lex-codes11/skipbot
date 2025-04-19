# skipbot.py

import os, json, datetime, random, stripe
from threading import Thread
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from discord import ui
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

# ---------- HELPERS & PERSISTENCE ----------
def get_sale_date() -> datetime.date:
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    # sales before 1 AM count for previous night
    return (now - datetime.timedelta(days=1)).date() if now.hour < 1 else now.date()

def iso_date(d: datetime.date) -> str:
    return d.isoformat()

def human_date(d: datetime.date) -> str:
    return d.strftime("%b %-d, %Y")

os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.exists(SALES_FILE):   open(SALES_FILE,"w").write("{}")
if not os.path.exists(PHRASES_FILE): open(PHRASES_FILE,"w").write("{}")

def load_json(path):
    return json.load(open(path, "r"))

def save_json(path, data):
    json.dump(data, open(path, "w"), indent=2)

def get_counts():
    today = iso_date(get_sale_date())
    day   = load_json(SALES_FILE).get(today, {"ATL": [], "FL": []})
    return {"ATL": len(day["ATL"]), "FL": len(day["FL"])}

def record_sale(session_id, discord_id, loc, sale_date_iso):
    all_s = load_json(SALES_FILE)
    day   = all_s.setdefault(sale_date_iso, {"ATL": [], "FL": []})
    if session_id not in [x["session"] for x in day[loc]]:
        day[loc].append({"session": session_id, "user": discord_id})
        save_json(SALES_FILE, all_s)
    return len(day[loc])

def ensure_phrases_for(date_iso):
    p = load_json(PHRASES_FILE)
    if date_iso not in p:
        pool = DAILY_PHRASES.copy(); random.shuffle(pool)
        p[date_iso] = pool; save_json(PHRASES_FILE, p)
    return p[date_iso]

# ---------- FLASK & STRIPE WEBHOOK ----------
app = Flask(__name__)

@app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload, sig = request.get_data(), request.headers.get("Stripe-Signature","")
    try:
        ev = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return abort(400)
    if ev["type"] == "checkout.session.completed":
        sess = ev["data"]["object"]
        meta = sess.get("metadata", {})
        uid, loc, sdate = int(meta.get("discord_id",0)), meta.get("location"), meta.get("sale_date")
        sid = sess.get("id")
        if loc and sdate and sid:
            cnt = record_sale(sid, uid, loc, sdate)
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
intents = discord.Intents.default()
intents.members = True
bot    = commands.Bot(command_prefix="!", intents=intents)

# ---------- URL BUTTON VIEW ----------
class URLView(ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(ui.Button(label="🔗 Complete Purchase", style=discord.ButtonStyle.link, url=url))

# ---------- PURCHASE HANDLER ----------
async def handle_purchase(ctx, loc: str):
    counts = get_counts()
    left   = 25 - counts[loc]
    date   = get_sale_date()
    hdate  = human_date(date)

    if left <= 0:
        return await ctx.send(f"❌ **{loc}** is sold out for **{hdate}**.")

    # create session
    iso = iso_date(date)
    ensure_phrases_for(iso)
    price = PRICE_ID_ATL if loc=="ATL" else PRICE_ID_FL

    sess = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": price, "quantity":1}],
        mode="payment",
        success_url=SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=CANCEL_URL,
        metadata={"discord_id":str(ctx.author.id),"location":loc,"sale_date":iso}
    )

    view = URLView(sess.url)
    await ctx.send(
        f"{ctx.author.mention} 💳 Purchase your **{loc}** pass for **{hdate}** — "
        f"{left} tickets remaining:",
        view=view
    )

# ---------- PREFIX COMMANDS ----------
@bot.command(name="atl", help="Buy an ATL skip‑line pass")
async def atl(ctx):
    await handle_purchase(ctx, "ATL")

@bot.command(name="fl", help="Buy a FL skip‑line pass")
async def fl(ctx):
    await handle_purchase(ctx, "FL")

# ---------- STARTUP ----------
@bot.event
async def on_ready():
    keep_alive()
    print(f"✅ SkipBot running as {bot.user}")

# ---------- RUN ----------
if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
