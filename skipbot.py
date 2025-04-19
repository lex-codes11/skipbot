# skipbot.py

import os, json, datetime, random, stripe
from threading  import Thread
from zoneinfo   import ZoneInfo

import discord
from discord     import ui, Interaction, app_commands
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
SKIP_CHANNEL_ID       = int(os.getenv("SKIP_CHANNEL_ID", "0"))

# (25 static pass‑phrases, shuffled daily)
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
    return dt.strftime("%A, %B %-d, %Y")

def load_json(path):
    with open(path, "r") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.exists(SALES_FILE):   save_json(SALES_FILE, {})
if not os.path.exists(PHRASES_FILE): save_json(PHRASES_FILE, {})

def ensure_phrases_for(date_iso: str):
    d = load_json(PHRASES_FILE)
    if date_iso not in d:
        pool = DAILY_PHRASES.copy()
        random.shuffle(pool)
        d[date_iso] = pool
        save_json(PHRASES_FILE, d)
    return d[date_iso]

def load_sales():
    return load_json(SALES_FILE)

def save_sales(all_s):
    save_json(SALES_FILE, all_s)

def record_sale(session_id: str, discord_id: int, location: str, sale_date_iso: str):
    all_s = load_sales()
    day   = all_s.setdefault(sale_date_iso, {"ATL": [], "FL": []})
    if session_id not in [x["session"] for x in day[location]]:
        day[location].append({"session": session_id, "user": discord_id})
        save_sales(all_s)
    return len(day[location])

def get_counts():
    day = load_sales().get(iso_date(get_sale_date()), {"ATL": [], "FL": []})
    return {"ATL": len(day["ATL"]), "FL": len(day["FL"])}

# ---------- FLASK WEBHOOK & HEALTHCHECK ----------
app = Flask(__name__)

@app.route("/", methods=["GET","HEAD"])
def healthcheck():
    return "OK", 200

@app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload, sig = request.get_data(), request.headers.get("Stripe-Signature","")
    try:
        evt = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return abort(400)
    if evt["type"] == "checkout.session.completed":
        sess = evt["data"]["object"]
        meta = sess.get("metadata",{})
        uid, loc, sd = int(meta.get("discord_id",0)), meta.get("location"), meta.get("sale_date")
        sid = sess.get("id")
        if loc and sd and sid:
            count = record_sale(sid, uid, loc, sd)
            user  = bot.get_user(uid)
            if user:
                discord.utils.asyncio.create_task(
                    user.send(f"✅ You’re pass **#{count}/25** for {loc} on {human_date(get_sale_date())}.")
                )
    return "", 200

def run_web():    app.run(host="0.0.0.0", port=8080)
def keep_alive(): Thread(target=run_web, daemon=True).start()

# ---------- DISCORD SETUP ----------
intents = discord.Intents.default()
intents.members = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------- PURCHASE BUTTON VIEW ----------
class SkipButtonView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.refresh()

    def refresh(self):
        self.clear_items()
        cnts = get_counts()
        sd   = get_sale_date()

        label_atl = (
            f"Buy ATL Skip‑Line Pass\n{human_date(sd)} ({cnts['ATL']}/25)"
            if cnts["ATL"] < 25 else "ATL Sold Out"
        )
        label_fl = (
            f"Buy FL Skip‑Line Pass\n{human_date(sd)} ({cnts['FL']}/25)"
            if cnts["FL"] < 25 else "FL Sold Out"
        )

        self.add_item(ui.Button(
            label=label_atl,
            style=discord.ButtonStyle.success if cnts["ATL"]<25 else discord.ButtonStyle.secondary,
            custom_id="buy_skip_atl",
            disabled=cnts["ATL"]>=25
        ))
        self.add_item(ui.Button(
            label=label_fl,
            style=discord.ButtonStyle.success if cnts["FL"]<25 else discord.ButtonStyle.secondary,
            custom_id="buy_skip_fl",
            disabled=cnts["FL"]>=25
        ))

    async def _start_checkout(self, inter: Interaction, loc: str):
        # defer immediately so no “interaction failed”
        await inter.response.defer(ephemeral=True)

        iso   = iso_date(get_sale_date())
        ensure_phrases_for(iso)
        price = PRICE_ID_ATL if loc=="ATL" else PRICE_ID_FL

        sess = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price, "quantity": 1}],
            mode="payment",
            success_url=SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=CANCEL_URL,
            metadata={"discord_id":str(inter.user.id),
                      "location":loc,
                      "sale_date":iso}
        )

        # now send them a link‑button to complete
        view = ui.View()
        view.add_item(ui.Button(label="🔗 Complete Purchase",
                                style=discord.ButtonStyle.link,
                                url=sess.url))
        await inter.followup.send(
            f"💳 {loc} pass for **{human_date(get_sale_date())}** — tap below to finish:",
            view=view,
            ephemeral=True
        )

    @ui.button(custom_id="buy_skip_atl")
    async def buy_atl(self, _btn, inter):
        await self._start_checkout(inter, "ATL")

    @ui.button(custom_id="buy_skip_fl")
    async def buy_fl(self, _btn, inter):
        await self._start_checkout(inter, "FL")

# ---------- ADMIN SLASH TO POST & PIN ONCE ----------
@tree.command(name="setup_skip", description="(Owner) Post & pin the two‑button sales msg")
async def setup_skip(inter: Interaction):
    if inter.user.id != inter.guild.owner_id:
        return await inter.response.send_message("⛔ Only the server owner.", ephemeral=True)

    view = SkipButtonView()
    bot.add_view(view)  # keep live across restarts

    channel = bot.get_channel(SKIP_CHANNEL_ID)
    if not channel:
        return await inter.response.send_message("❌ SKIP_CHANNEL_ID is bad.", ephemeral=True)

    # 1) Ack
    await inter.response.send_message("✅ Posted & pinned.", ephemeral=True)
    # 2) Post the pinned sales message
    msg = await channel.send(
        "🎟️ **Skip The Line Passes**\n"
        "Limited to 25 per night, $25 each.  Just tap a button to buy:",
        view=view
    )
    await msg.pin()

# ---------- STARTUP & SYNC ----------
@bot.event
async def on_ready():
    keep_alive()
    # re‑register our buttons so they keep working
    bot.add_view(SkipButtonView())
    await tree.sync()
    print(f"✅ SkipBot ready as {bot.user}")

# ---------- RUN ----------
if not DISCORD_TOKEN:
    raise ValueError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
