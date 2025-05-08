# skipbot.py

import os, json, datetime, random
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
GUILD_ID        = int(os.getenv("GUILD_ID"))
VIP_CHANNEL_ID  = int(os.getenv("VIP_CHANNEL_ID"))

GUILD           = discord.Object(id=GUILD_ID)

stripe.api_key  = os.getenv("STRIPE_API_KEY")

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
    id_or_dob = ui.TextInput(label="Club ID or DOB (MMDDYY)", style=TextStyle.short)

    async def on_submit(self, inter: Interaction):
        code = "-".join(
            "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=3))
            for _ in range(3)
        )
        entry = {
            "user_id":    inter.user.id,
            "name":       inter.user.display_name,
            "last_name":  self.last_name.value,
            "id_or_dob":  self.id_or_dob.value,
            "code":       code
        }
        add_rsvp(entry)

        human = human_date(get_sale_date())
        await inter.response.send_message("✅ RSVP received! Check your DMs for your ticket.", ephemeral=True)
        await inter.user.send(
            f"🎟 **VIP RSVP Ticket**\n"
            f"Member: {inter.user.display_name}\n"
            f"Last Name: {self.last_name.value}\n"
            f"Club ID / DOB: {self.id_or_dob.value}\n"
            f"Valid Date: {human}\n"
            f"Code: `{code}`"
        )

# ---------- BUTTON VIEW ----------
class RSVPButtonView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(
        label="Get my VIP ticket for tonight",
        style=discord.ButtonStyle.primary,
        custom_id="vip_rsvp_button"
    )
    async def rsvp_button(self, button: ui.Button, inter: Interaction):
        if "VIP" not in [r.name for r in inter.user.roles]:
            return await inter.response.send_message("⛔ VIPs only.", ephemeral=True)
        await inter.response.send_modal(RSVPModal())

# ---------- STAFF COMMANDS ----------
class StaffCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="list_rsvps",
        description="(Staff) List tonight’s VIP RSVPs"
    )
    @app_commands.guilds(GUILD)
    async def list_rsvps(self, inter: Interaction):
        if not (inter.user.guild_permissions.manage_guild or
                "Staff" in [r.name for r in inter.user.roles]):
            return await inter.response.send_message("⛔ Staff only.", ephemeral=True)

        entries = get_todays_rsvps()
        if not entries:
            return await inter.response.send_message("No RSVPs yet.", ephemeral=True)

        lines = [f"**VIP RSVPs for {human_date(get_sale_date())}**"]
        for i,e in enumerate(entries, start=1):
            lines.append(
                f"{i:2d}. {e['name']} — Last: {e['last_name']} "
                f"— ID/DOB: {e['id_or_dob']} — Code: `{e['code']}`"
            )

        text = "\n".join(lines)
        for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
            await inter.response.send_message(chunk, ephemeral=True)

# ---------- FLASK HEALTH CHECK ----------
app = Flask(__name__)
@app.route("/", methods=["GET","HEAD"])
def health():
    return "OK", 200

# ---------- BOT SETUP ----------
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    # start Flask thread
    Thread(target=lambda: app.run(host="0.0.0.0", port=8080), daemon=True).start()

    # register & post the persistent button
    view = RSVPButtonView()
    bot.add_view(view)
    vip_ch = bot.get_channel(VIP_CHANNEL_ID)
    if vip_ch:
        await vip_ch.send(
            "🎉 **VIP RSVP for tonight**\nClick below to get your ticket:",
            view=view
        )

    # register staff cog & slash commands
    await bot.add_cog(StaffCommands(bot))
    await bot.tree.sync(guild=GUILD)

    print(f"✅ SkipBot ready as {bot.user} in guild {GUILD_ID}")

# ---------- RUN ----------
if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
