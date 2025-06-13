# skipbot.py
import os, json, datetime, random, asyncio
from threading    import Thread
from zoneinfo     import ZoneInfo

import discord
from discord      import ui, app_commands, Interaction, TextStyle
from discord.ext  import commands, tasks
from flask        import Flask
import stripe

# ---------- CONFIG ----------
DATA_DIR         = os.getenv("DATA_DIR", "data")
RSVP_FILE        = os.path.join(DATA_DIR, "vip_rsvps.json")
DISCORD_TOKEN    = os.getenv("DISCORD_TOKEN")
STRIPE_API_KEY   = os.getenv("STRIPE_API_KEY")
GUILD_ID         = int(os.getenv("GUILD_ID"))
VIP_CHANNEL_ID   = int(os.getenv("VIP_CHANNEL_ID"))
GUILD            = discord.Object(id=GUILD_ID)
MAX_PER_NIGHT    = 25

stripe.api_key   = STRIPE_API_KEY

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

def clear_today_rsvps():
    data = load_rsvps()
    key  = get_sale_date()
    if key in data:
        data[key] = []
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
        # validation
        if not (key.isdigit() and len(key) in (4, 6)):
            return await inter.response.send_message(
                "❌ Must be exactly 4 digits (membership #) or 6 digits (DOB MMDDYY).",
                ephemeral=True
            )
        todays = get_todays_rsvps()
        # per-user
        if any(r["user_id"] == inter.user.id for r in todays):
            return await inter.response.send_message(
                "❌ You’ve already RSVPed for tonight.", ephemeral=True
            )
        # membership # dedupe; DOB may repeat
        if len(key) == 4 and any(r["id_or_dob"] == key for r in todays):
            return await inter.response.send_message(
                "❌ That membership # has already been used.", ephemeral=True
            )

        # generate code
        code = "-".join(
            "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=3))
            for _ in range(3)
        )
        entry = {
            "user_id":   inter.user.id,
            "name":      inter.user.display_name,
            "last_name": self.last_name.value.strip(),
            "id_or_dob": key,
            "code":      code
        }
        add_rsvp(entry)

        # ack & DM
        human = human_date(get_sale_date())
        await inter.response.send_message(
            "✅ RSVP received! Check your DMs for your ticket.", ephemeral=True
        )
        await inter.user.send(
            f"🎟 **VIP RSVP Ticket**\n"
            f"Member: {inter.user.display_name}\n"
            f"Last Name: {self.last_name.value.strip()}\n"
            f"Membership #: {key}\n"
            f"Valid Date: {human}\n"
            f"Code: `{code}`"
        )
        # disable button for this user until reset
        inter.client.dispatch("rsvp_submitted", inter.user.id)

# ---------- BUTTON VIEW ----------
class RSVPButtonView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.button = ui.Button(
            label="Get my VIP ticket for tonight",
            style=discord.ButtonStyle.primary,
            custom_id="vip_rsvp_button"
        )
        self.add_item(self.button)

    @ui.button(custom_id="vip_rsvp_button")
    async def rsvp_button(self, button: ui.Button, inter: Interaction):
        # VIP only
        if "VIP" not in [r.name for r in inter.user.roles]:
            return await inter.response.send_message("⛔ VIPs only.", ephemeral=True)
        # show modal
        await inter.response.send_modal(RSVPModal())

    def disable_for_user(self, user_id: int):
        # once someone RSVPs, disable the button (globally)
        self.button.disabled = True

# ---------- STAFF COMMANDS ----------
class StaffCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="list_rsvps", description="(Staff) List tonight’s VIP RSVPs")
    @app_commands.guilds(GUILD)
    async def list_rsvps(self, inter: Interaction):
        if not (inter.user.guild_permissions.manage_guild or
                "Staff" in [r.name for r in inter.user.roles]):
            return await inter.response.send_message("⛔ Staff only.", ephemeral=True)

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

# ---------- FLASK HEALTH CHECK ----------
app = Flask(__name__)
@app.route("/", methods=["GET","HEAD"])
def health():
    return "OK", 200

# ---------- BOT SETUP ----------
intents = discord.Intents.default()
intents.members = True
bot     = commands.Bot(command_prefix="!", intents=intents)
view    = RSVPButtonView()

@bot.event
async def on_ready():
    print(f"✅ VIPBot ready as {bot.user}")
    # health-check
    Thread(target=lambda: app.run(host="0.0.0.0", port=8080), daemon=True).start()
    # clear yesterday
    clear_today_rsvps()
    # post button
    bot.add_view(view)
    ch = bot.get_channel(VIP_CHANNEL_ID) or await bot.fetch_channel(VIP_CHANNEL_ID)
    if ch:
        await ch.send("🎉 **VIP RSVP for tonight**\nClick below to get your ticket:", view=view)
    # start reset task
    daily_reset.start()
    # register staff cog
    await bot.add_cog(StaffCommands(bot))
    await bot.tree.sync(guild=GUILD)

@bot.event
async def on_rsvp_submitted(user_id: int):
    # disable the button view when someone RSVPs
    view.disable_for_user(user_id)

@tasks.loop(time=datetime.time(hour=1, minute=0, tzinfo=ZoneInfo("America/New_York")))
async def daily_reset():
    # runs each night at 1 AM EST
    clear_today_rsvps()
    view.button.disabled = False
    ch = bot.get_channel(VIP_CHANNEL_ID) or await bot.fetch_channel(VIP_CHANNEL_ID)
    if ch:
        await ch.send("🔄 The VIP RSVP button has been reset for tonight — click below!", view=view)

# ---------- RUN ----------
if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
bot.run(DISCORD_TOKEN)
