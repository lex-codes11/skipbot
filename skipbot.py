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
        # 1) Must be digits, length 4 or 6
        if not (key.isdigit() and len(key) in (4,6)):
            await inter.response.send_message(
                "❌ Entry must be exactly 4 digits (membership #) or 6 digits (DOB MMDDYY).",
                ephemeral=True
            )
            return

        # 2) Prevent multiple RSVPs per user
        todays = get_todays_rsvps()
        if any(r["user_id"] == inter.user.id for r in todays):
            await inter.response.send_message(
                "❌ You’ve already RSVPed for tonight.", ephemeral=True
            )
            return

        # 3) Prevent reusing the same membership # (only 4-digit keys)
        if len(key)==4 and any(r["id_or_dob"] == key for r in todays):
            await inter.response.send_message(
                "❌ That membership # has already been used tonight.", ephemeral=True
            )
            return

        # 4) All good → generate code, save & send ticket
        code = "-".join(
            "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=3))
            for _ in range(3)
        )
        human = human_date(get_sale_date())
        entry = {
            "user_id":   inter.user.id,
            "name":      inter.user.display_name,
            "last_name": self.last_name.value.strip(),
            "id_or_dob": key,
            "code":      code
        }
        add_rsvp(entry)

        await inter.response.send_message(
            "✅ RSVP received! Check your DMs for your ticket.", ephemeral=True
        )
        await inter.user.send(
            f"🎟 **VIP RSVP Ticket**\n"
            f"Member: {inter.user.display_name}\n"
            f"Last Name: {self.last_name.value.strip()}\n"
            f"Membership #: {key if len(key)==4 else '—'}\n"
            f"DOB: {key if len(key)==6 else '—'}\n"
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
    async def rsvp_button(self, interaction: Interaction, button: ui.Button):
        # ensure VIP role
        if "VIP" not in [r.name for r in interaction.user.roles]:
            return await interaction.response.send_message(
                "⛔ VIPs only.", ephemeral=True
            )

        # open the modal
        await interaction.response.send_modal(RSVPModal())

        # disable **this button instance** so the same user can't click it again
        button.disabled = True
        await interaction.message.edit(view=self)

# ---------- STAFF COG ----------
class StaffCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="list_rsvps",
        description="(Staff) List tonight’s VIP RSVPs"
    )
    @app_commands.guilds(GUILD)
    async def list_rsvps(self, inter: Interaction):
        # only staff or owner
        if not (inter.user.guild_permissions.manage_guild or
                "Staff" in [r.name for r in inter.user.roles]):
            return await inter.response.send_message(
                "⛔ Staff only.", ephemeral=True
            )

        entries = get_todays_rsvps()
        if not entries:
            return await inter.response.send_message("No RSVPs yet.", ephemeral=True)

        lines = [f"**VIP RSVPs for {human_date(get_sale_date())}**"]
        for i,e in enumerate(entries, start=1):
            lines.append(
                f"{i:2d}. {e['name']} — Last: {e['last_name']} "
                f"— Membership #: {e['id_or_dob']} — Code: `{e['code']}`"
            )

        text = "\n".join(lines)
        for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
            await inter.response.send_message(chunk, ephemeral=True)

# ---------- FLASK HEALTH CHECK ----------
app = Flask(__name__)
@app.route("/", methods=["GET","HEAD"])
def health():
    return "VIPBot OK", 200

# ---------- BOT SETUP ----------
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    # start health-check server
    Thread(target=lambda: app.run(host="0.0.0.0", port=8080), daemon=True).start()

    # install the view and post the button
    view  = RSVPButtonView()
    bot.add_view(view)
    vip_ch = bot.get_channel(VIP_CHANNEL_ID)
    if vip_ch:
        await vip_ch.send(
            "🎉 **VIP RSVP for tonight**\nClick below to get your ticket:",
            view=view
        )

    # add the staff cog and sync only to your guild
    await bot.add_cog(StaffCommands(bot))
    await bot.tree.sync(guild=GUILD)

    print(f"✅ VIPBot ready as {bot.user}")

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
