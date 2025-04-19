# skipbot.py

import os, json, datetime, random, stripe
from threading import Thread
from zoneinfo import ZoneInfo

import discord
from discord import ui, Interaction, app_commands
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
SKIP_CHANNEL_ID       = int(os.getenv("SKIP_CHANNEL_ID","0"))

DAILY_PHRASES = [
    "Pineapples","Kinkster","Certified Freak","Hot Wife","Stag Night",
    "Velvet Vixen","Playroom Pro","Voyeur Vision","After Dark",
    "Bare Temptation","Swing Set","Sultry Eyes","Naughty List",
    "Dom Curious","Unicorn Dust","Cherry Popper","Dirty Martini",
    "Lust Lounge","Midnight Tease","Fantasy Fuel","Room 69","Wet Bar",
    "No Limits","Satin Sheets","Wild Card"
]

stripe.api_key = STRIPE_API_KEY

# ---------- HELPERS & PERSISTENCE ----------
def get_sale_date() -> datetime.date:
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    return (now - datetime.timedelta(days=1)).date() if now.hour < 1 else now.date()

def iso_date(d: datetime.date) -> str:
    return d.isoformat()

def human_date(d: datetime.date) -> str:
    return d.strftime("%b %-d, %Y")

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

def record_sale(session_id, discord_id, loc, date_iso):
    all_s = load_json(SALES_FILE)
    day   = all_s.setdefault(date_iso, {"ATL": [], "FL": []})
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
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------- PERSISTENT BUTTON VIEW ----------
class SkipView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.message = None
        self.refresh()

    def refresh(self):
        self.clear_items()
        cnts = get_counts()
        date = human_date(get_sale_date())

        atl = f"ATL — {cnts['ATL']}/25 ({date})" if cnts['ATL']<25 else "ATL — SOLD OUT"
        fl  = f"FL — {cnts['FL']}/25 ({date})" if cnts['FL']<25 else "FL — SOLD OUT"

        self.add_item(ui.Button(
            label=atl, custom_id="buy_atl",
            style=discord.ButtonStyle.primary, disabled=(cnts['ATL']>=25)
        ))
        self.add_item(ui.Button(
            label=fl,  custom_id="buy_fl",
            style=discord.ButtonStyle.primary, disabled=(cnts['FL']>=25)
        ))

    async def create_session(self, interaction: Interaction, loc: str):
        iso = iso_date(get_sale_date())
        ensure_phrases_for(iso)
        price = PRICE_ID_ATL if loc=="ATL" else PRICE_ID_FL
        sess = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price":price,"quantity":1}],
            mode="payment",
            success_url=SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=CANCEL_URL,
            metadata={"discord_id":str(interaction.user.id),
                      "location":loc,"sale_date":iso}
        )
        return sess.url

    @ui.button(custom_id="buy_atl")
    async def buy_atl(self, button: ui.Button, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        url = await self.create_session(interaction, "ATL")
        await interaction.followup.send(f"💳 Complete your ATL purchase: {url}", ephemeral=True)
        self.refresh()
        if self.message:
            await self.message.edit(view=self)

    @ui.button(custom_id="buy_fl")
    async def buy_fl(self, button: ui.Button, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        url = await self.create_session(interaction, "FL")
        await interaction.followup.send(f"💳 Complete your FL purchase: {url}", ephemeral=True)
        self.refresh()
        if self.message:
            await self.message.edit(view=self)

# ---------- SLASH COMMAND ----------
@tree.command(name="setup_skip", description="(Owner) Post skip‑line buttons")
async def setup_skip(interaction: Interaction):
    # only owner
    if interaction.user.id != interaction.guild.owner_id:
        return await interaction.response.send_message("⛔ Only the owner.", ephemeral=True)

    view = SkipView()
    bot.add_view(view)  # persist

    # 1) defer to buy yourself time
    await interaction.response.defer(ephemeral=True)

    # 2) post the public buttons
    channel = bot.get_channel(SKIP_CHANNEL_ID)
    if not channel:
        return await interaction.followup.send("❌ Bad SKIP_CHANNEL_ID.", ephemeral=True)

    msg = await channel.send(
        "🎟️ **Skip The Line Passes** — click to buy:",
        view=view
    )
    view.message = msg

    # 3) confirm back to the invoker
    await interaction.followup.send("✅ Buttons posted.", ephemeral=True)

# ---------- STARTUP & SYNC ----------
@bot.event
async def on_ready():
    keep_alive()
    # re‑register view to keep buttons alive
    bot.add_view(SkipView())
    await tree.sync()
    print(f"✅ SkipBot online as {bot.user}")

# ---------- RUN ----------
if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
