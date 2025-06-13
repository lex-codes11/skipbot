# skipbot.py

import os, json, datetime, random, asyncio
from threading import Thread
from zoneinfo import ZoneInfo

import discord
from discord import ui, app_commands, Interaction, TextStyle
from discord.ext import commands
from flask import Flask
import stripe

# ---------- CONFIG ----------
DATA_DIR        = os.getenv("DATA_DIR", "data")
RSVP_FILE       = os.path.join(DATA_DIR, "vip_rsvps.json")
DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN")
STRIPE_API_KEY  = os.getenv("STRIPE_API_KEY")
GUILD_ID        = int(os.getenv("GUILD_ID"))
VIP_CHANNEL_ID  = int(os.getenv("VIP_CHANNEL_ID"))
GUILD           = discord.Object(id=GUILD_ID)

stripe.api_key  = STRIPE_API_KEY

# ---------- STORAGE HELPERS ----------
def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def load_rsvps() -> dict:
    ensure_data_dir()
    if not os.path.exists(RSVP_FILE):
        return {}
    with open(RSVP_FILE, "r") as f:
        return json.load(f)

def save_rsvps(data: dict):
    ensure_data_dir()
    with open(RSVP_FILE, "w") as f:
        json.dump(data, f, indent=2)

def add_rsvp(entry: dict):
    data = load_rsvps()
    key  = get_sale_date()
    day  = data.setdefault(key, [])
    day.append(entry)
    save_rsvps(data)

def clear_old_rsvps():
    """Keep only today’s key in the RSVP_FILE."""
    data = load_rsvps()
    today = get_sale_date()
    if today in data:
        data = {today: data[today]}
    else:
        data = {}
    save_rsvps(data)

def get_todays_rsvps() -> list:
    return load_rsvps().get(get_sale_date(), [])

# ---------- DATE HELPERS ----------
def get_sale_date() -> str:
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now.hour < 1:
        now -= datetime.timedelta(days=1)
    return now.date().isoformat()

def human_date(iso: str) -> str:
    return datetime.date.fromisoformat(iso).strftime("%A, %B %-d, %Y")

# ---------- RSVP MODAL ----------
class RSVPModal(ui.Modal, title="VIP RSVP"):
    last_name = ui.TextInput(label="Last name on your ID", style=TextStyle.short)
    id_or_dob = ui.TextInput(
        label="Membership Number (4 digits) or DOB (MMDDYY)",
        style=TextStyle.short,
        placeholder="e.g. 1234 or 010190"
    )

    async def on_submit(self, inter: Interaction):
        key = self.id_or_dob.value.strip()
        if not (key.isdigit() and len(key) in (4,6)):
            return await inter.response.send_message(
                "❌ Must be exactly 4 digits (membership #) or 6 digits (DOB MMDDYY).",
                ephemeral=True
            )
        today_entries = get_todays_rsvps()
        if any(r["user_id"] == inter.user.id for r in today_entries):
            return await inter.response.send_message("❌ You’ve already RSVPed for tonight.", ephemeral=True)
        if any(r["id_or_dob"] == key for r in today_entries):
            return await inter.response.send_message("❌ That membership # has already been used.", ephemeral=True)

        # build entry
        code = "-".join(
            "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=3))
            for _ in range(3)
        )
        human = human_date(get_sale_date())
        entry = {
            "user_id":    inter.user.id,
            "name":       inter.user.display_name,
            "last_name":  self.last_name.value.strip(),
            "id_or_dob":  key,
            "code":       code
        }
        add_rsvp(entry)

        await inter.response.send_message("✅ RSVP received! Check your DMs for your ticket.", ephemeral=True)
        await inter.user.send(
            f"🎟 **VIP RSVP Ticket**\n"
            f"Member: {inter.user.display_name}\n"
            f"Last Name: {self.last_name.value}\n"
            f"Membership #: {key}\n"
            f"Valid Date: {human}\n"
            f"Code: `{code}`"
        )

# ---------- BUTTON VIEW ----------
class RSVPButtonView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ui.Button(
            label="Get my VIP ticket for tonight",
            style=discord.ButtonStyle.primary,
            custom_id="vip_rsvp_button"
        ))

    @ui.button(custom_id="vip_rsvp_button")
    async def rsvp_button(self, button: ui.Button, inter: Interaction):
        if "VIP" not in [r.name for r in inter.user.roles]:
            return await inter.response.send_message("⛔ VIPs only.", ephemeral=True)
        await inter.response.send_modal(RSVPModal())

# ---------- STAFF COMMANDS ----------
class StaffCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="list_rsvps", description="(Staff) List tonight’s RSVPs")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guilds(GUILD)
    async def list_rsvps(self, inter: Interaction):
        entries = get_todays_rsvps()
        if not entries:
            return await inter.response.send_message("No RSVPs yet.", ephemeral=True)

        lines = [f"**VIP RSVPs for {human_date(get_sale_date())}**"]
        for i, e in enumerate(entries, start=1):
            lines.append(
                f"{i:2d}. {e['name']} — Last: {e['last_name']} "
                f"— Membership #: {e['id_or_dob']} — Code: `{e['code']}`"
            )

        text = "\n".join(lines)
        for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
            await inter.response.send_message(chunk, ephemeral=True)

# ---------- HEALTH CHECK + FLASK ----------
app = Flask(__name__)
@app.route("/", methods=["GET","HEAD"])
def health():
    return "OK", 200

# ---------- BOT SETUP ----------
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def daily_reset_loop():
    """Runs forever: at 1 AM EST each day clear RSVPs & repost button."""
    while True:
        now = datetime.datetime.now(ZoneInfo("America/New_York"))
        # compute next 1:00 AM
        next_reset = (now + datetime.timedelta(days=1)).replace(hour=1, minute=0, second=0, microsecond=0)
        wait = (next_reset - now).total_seconds()
        await asyncio.sleep(wait)

        # clear yesterday’s RSVPs
        clear_old_rsvps()

        # repost fresh button
        channel = bot.get_channel(VIP_CHANNEL_ID)
        if channel:
            view = RSVPButtonView()
            bot.add_view(view)
            await channel.send("🎉 **VIP RSVP for tonight**\nClick below to get your ticket:", view=view)

@bot.event
async def on_ready():
    # start health check server
    Thread(target=lambda: app.run(host="0.0.0.0", port=8080), daemon=True).start()

    # initial post of RSVP button
    view = RSVPButtonView()
    bot.add_view(view)
    ch = bot.get_channel(VIP_CHANNEL_ID)
    if ch:
        await ch.send("🎉 **VIP RSVP for tonight**\nClick below to get your ticket:", view=view)

    # staff cog & commands
    await bot.add_cog(StaffCommands(bot))
    await bot.tree.sync(guild=GUILD)

    # kick off the daily reset task
    bot.loop.create_task(daily_reset_loop())

    print(f"✅ VIPBot ready as {bot.user}")

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
