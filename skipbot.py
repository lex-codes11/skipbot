# skipbot.py

import os, json, datetime, random, asyncio
from threading    import Thread
from zoneinfo     import ZoneInfo

import stripe
import discord
from discord       import app_commands, Interaction
from discord.ext   import commands
from flask         import Flask, request, abort, render_template_string

# ---------- CONFIG ----------
DATA_DIR              = "data"
SALES_FILE            = os.path.join(DATA_DIR, "skip_sales.json")
PHRASES_FILE          = os.path.join(DATA_DIR, "skip_passphrases.json")

DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
STRIPE_API_KEY        = os.getenv("STRIPE_API_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
PRICE_ID_ATL          = os.getenv("PRICE_ID_ATL")
PRICE_ID_FL           = os.getenv("PRICE_ID_FL")
SUCCESS_URL           = os.getenv("SUCCESS_URL")  # e.g. https://trapezeclubs.com/success
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
        pool = DAILY_PHRASES.copy()
        random.shuffle(pool)
        data[date_iso] = pool
        save_json(PHRASES_FILE, data)
    return data[date_iso]

def load_sales() -> dict:
    return load_json(SALES_FILE)

def save_sales(data: dict):
    save_json(SALES_FILE, data)

def record_sale(sess_id: str, discord_id: int, location: str, date_iso: str) -> int:
    sales = load_sales()
    day   = sales.setdefault(date_iso, {"ATL": [], "FL": []})
    if sess_id not in [s["session"] for s in day[location]]:
        day[location].append({"session": sess_id, "user": discord_id})
        save_sales(sales)
    return len(day[location])

def get_count(location: str) -> int:
    return len(load_sales().get(get_sale_date(), {}).get(location, []))

# ---------- FLASK APP & WEBHOOK ----------
app = Flask(__name__)

# 1) Stripe webhook to record sale + DM ticket
@app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload, sig = request.get_data(), request.headers.get("Stripe-Signature", "")
    try:
        ev = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return abort(400)
    if ev["type"] == "checkout.session.completed":
        sess    = ev["data"]["object"]
        meta    = sess.get("metadata", {})
        uid     = int(meta.get("discord_id", 0))
        loc     = meta.get("location")
        date_iso= meta.get("sale_date")
        sid     = sess.get("id")
        if loc and date_iso and sid:
            cnt = record_sale(sid, uid, loc, date_iso)
            # pick the nth phrase
            phrase = ensure_phrases_for(date_iso)[cnt-1]
            # DM on bot loop
            coro = handle_ticket(uid, loc, date_iso, cnt, phrase, sid, sess)
            asyncio.run_coroutine_threadsafe(coro, bot.loop)
    return "", 200

# 2) Success page: shows ticket in browser
SUCCESS_TEMPLATE = """
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Your Skip‑Line Pass</title>
  <style>
    body { font-family:sans-serif; background:#f7f7f7; padding:2em; }
    .ticket { background:#fff; border:2px solid #e91e63;
              border-radius:8px; padding:2em; display:inline-block; }
    h1 { margin-top:0;color:#e91e63; }
    .field { font-weight:bold; width:120px; display:inline-block; }
  </style>
</head>
<body>
  <div class="ticket">
    <h1>🎟 Skip The Line Pass</h1>
    <p><span class="field">Passphrase:</span> <strong>{{ phrase }}</strong></p>
    <p><span class="field">Ticket #:</span> {{ count }}/{{ max_night }}</p>
    <p><span class="field">Member:</span> {{ member }}</p>
    <p><span class="field">Valid Date:</span> {{ human_date }}</p>
    <p><span class="field">Confirmation #:</span> {{ session_id }}</p>
    <p><span class="field">Email:</span> {{ email or "—" }}</p>
  </div>
</body>
</html>
"""

@app.route("/success")
def success_page():
    session_id = request.args.get("session_id")
    if not session_id: abort(400, "Missing session_id")
    # retrieve & expand customer email
    try:
        sess = stripe.checkout.Session.retrieve(
            session_id, expand=["customer_details"]
        )
    except:
        abort(400, "Invalid session_id")
    meta     = sess.metadata or {}
    date_iso = meta.get("sale_date"); loc = meta.get("location")
    if not (date_iso and loc): abort(400, "Incomplete metadata")
    # find index
    entries = load_sales().get(date_iso, {}).get(loc, [])
    for i,e in enumerate(entries, start=1):
        if e["session"] == session_id:
            count = i
            break
    else:
        abort(404, "Sale not recorded")
    # phrase & member & email
    phrase = ensure_phrases_for(date_iso)[count-1]
    user   = bot.get_user(int(meta.get("discord_id",0)))
    member = user.display_name if user else "Unknown"
    email  = sess.customer_details.email if sess.customer_details else None

    return render_template_string(
      SUCCESS_TEMPLATE,
      phrase=phrase,
      count=count,
      max_night=MAX_PER_NIGHT,
      member=member,
      human_date=human_date(date_iso),
      session_id=session_id,
      email=email
    )

def run_web():    app.run(host="0.0.0.0", port=8080)
def keep_alive(): Thread(target=run_web, daemon=True).start()

# ---------- DISCORD BOT & TICKET DM ----------
intents = discord.Intents.default(); intents.members = True
bot     = commands.Bot(command_prefix="!", intents=intents)
tree    = bot.tree

DAILY_PHRASES = [
  "Pineapples","Kinkster","Certified Freak","Hot Wife","Stag Night",
  "Velvet Vixen","Playroom Pro","Voyeur Vision","After Dark","Bare Temptation",
  "Swing Set","Sultry Eyes","Naughty List","Dom Curious","Unicorn Dust",
  "Cherry Popper","Dirty Martini","Lust Lounge","Midnight Tease","Fantasy Fuel",
  "Room 69","Wet Bar","No Limits","Satin Sheets","Wild Card"
]

async def handle_ticket(
    uid: int, loc: str, date_iso: str, count: int,
    phrase: str, session_id: str, sess_obj
):
    human = human_date(date_iso)
    user  = await bot.fetch_user(uid)
    # 1) Confirmation DM
    await user.send(
      f"✅ Payment confirmed! You’re pass **#{count}/{MAX_PER_NIGHT}** "
      f"for **{loc}** on **{human}**."
    )
    # 2) Ticket DM
    ticket = (
      "🎟 **Skip The Line Pass**\n"
      f"Passphrase: **{phrase}**\n"
      f"Member: {user.display_name}\n"
      f"Valid Date: {human}\n"
      f"Confirmation #: {session_id}\n"
      f"Email: {sess_obj.customer_details.email or '—'}"
    )
    await user.send(ticket)

# ---------- PURCHASE COMMANDS ----------
@tree.command(name="atl", description="Purchase an ATL pass")
async def atl(inter: Interaction):
    date_iso = get_sale_date(); human = human_date(date_iso)
    sold = get_count("ATL"); left = MAX_PER_NIGHT - sold
    if left<=0:
      return await inter.response.send_message(
        f"❌ ATL sold out ({sold}/{MAX_PER_NIGHT}).", ephemeral=True
      )
    sess = stripe.checkout.Session.create(
      payment_method_types=["card"],
      line_items=[{"price":PRICE_ID_ATL,"quantity":1}],
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
      f"💳 {left} left for ATL on **{human}** – complete purchase: {sess.url}",
      ephemeral=True
    )

@tree.command(name="fl", description="Purchase an FL pass")
async def fl(inter: Interaction):
    date_iso = get_sale_date(); human = human_date(date_iso)
    sold = get_count("FL"); left = MAX_PER_NIGHT - sold
    if left<=0:
      return await inter.response.send_message(
        f"❌ FL sold out ({sold}/{MAX_PER_NIGHT}).", ephemeral=True
      )
    sess = stripe.checkout.Session.create(
      payment_method_types=["card"],
      line_items=[{"price":PRICE_ID_FL,"quantity":1}],
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
      f"💳 {left} left for FL on **{human}** – complete purchase: {sess.url}",
      ephemeral=True
    )

# ---------- STARTUP & SYNC ----------
@bot.event
async def on_ready():
    keep_alive()
    await tree.sync()
    print(f"✅ SkipBot online as {bot.user}")

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
