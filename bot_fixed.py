#!/usr/bin/env python3
"""
Fixed Discord VPS Bot (Nextcord) - bot_fixed.py

This is a cleaned version of the user's bot.py with the following fixes:
- Removed unsupported `sync_commands` parameter from Bot init.
- Added explicit application command sync in on_ready.
- Replaced incorrect Button decorator usage with @nextcord.ui.button and correct callback signatures.
- Removed incorrect usage of Button as decorator which caused "'Button' object is not callable".
- Kept original logic and structure otherwise intact.
- Saved to /mnt/data/bot_fixed.py for download.

Note: This file still assumes Nextcord library is installed and compatible.
"""

import os
import json
import uuid
import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import nextcord
from nextcord import Interaction, Embed, SlashOption
from nextcord.ext import commands, tasks
from nextcord.ui import Modal, TextInput, Button, View, Select

from dotenv import load_dotenv

# attempt to import pylxd; if not available we'll gracefully degrade
try:
    import pylxd
    PYLXD_AVAILABLE = True
except Exception:
    PYLXD_AVAILABLE = False

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID") else None
BOT_LOGO_URL = os.getenv("BOT_LOGO_URL", "")

DATA_DIR = "./"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
PLANS_FILE = os.path.join(DATA_DIR, "plans.json")
VPS_FILE = os.path.join(DATA_DIR, "vps.json")
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")
INVITES_FILE = os.path.join(DATA_DIR, "invites.json")

# Create intents
intents = nextcord.Intents.default()
intents.members = True
intents.guilds = True
intents.messages = True

# NOTE: removed sync_commands=True which is not a valid parameter for many nextcord versions
bot = commands.Bot(intents=intents, command_prefix="!")

# Locks for file IO
file_lock = asyncio.Lock()

# Utility JSON helpers
async def ensure_json_files():
    files_defaults = {
        USERS_FILE: {},
        PLANS_FILE: {},
        VPS_FILE: {},
        ADMINS_FILE: {"admins": []},
        INVITES_FILE: {}
    }
    for path, default in files_defaults.items():
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, indent=4)

async def read_json(path: str) -> Any:
    async with file_lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

async def write_json(path: str, data: Any):
    async with file_lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, default=str)

# Points helpers
async def get_user_points(user_id: int) -> int:
    users = await read_json(USERS_FILE)
    return int(users.get(str(user_id), {}).get("points", 0))

async def add_user_points(user_id: int, delta: int):
    users = await read_json(USERS_FILE)
    user = users.get(str(user_id), {})
    user["points"] = int(user.get("points", 0)) + int(delta)
    users[str(user_id)] = user
    await write_json(USERS_FILE, users)

async def set_user_points(user_id: int, points: int):
    users = await read_json(USERS_FILE)
    user = users.get(str(user_id), {})
    user["points"] = int(points)
    users[str(user_id)] = user
    await write_json(USERS_FILE, users)

# Plans helpers
def calculate_points_required(cpu_cores: int, ram_gb: int, disk_gb: int) -> int:
    # Deterministic formula: cpu*50 + ram*10 + disk*1
    return int(cpu_cores) * 50 + int(ram_gb) * 10 + int(disk_gb) * 1

async def add_plan(plan_data: Dict[str, Any]) -> str:
    plans = await read_json(PLANS_FILE)
    plan_id = str(uuid.uuid4())[:8]
    plans[plan_id] = plan_data
    await write_json(PLANS_FILE, plans)
    return plan_id

# VPS helpers
def generate_vps_id() -> str:
    return str(uuid.uuid4())[:12]

async def save_vps(vps_id: str, vps_data: Dict[str, Any]):
    vps_all = await read_json(VPS_FILE)
    vps_all[vps_id] = vps_data
    await write_json(VPS_FILE, vps_all)

async def delete_vps_entry(vps_id: str):
    vps_all = await read_json(VPS_FILE)
    if vps_id in vps_all:
        del vps_all[vps_id]
        await write_json(VPS_FILE, vps_all)

# Admins helpers
async def is_admin(user_id: int) -> bool:
    if OWNER_ID and user_id == OWNER_ID:
        return True
    admins = await read_json(ADMINS_FILE)
    return str(user_id) in [str(x) for x in admins.get("admins", [])]

async def add_admin(user_id: int):
    admins = await read_json(ADMINS_FILE)
    current = admins.get("admins", [])
    if str(user_id) not in [str(x) for x in current]:
        current.append(int(user_id))
        admins["admins"] = current
        await write_json(ADMINS_FILE, admins)

async def remove_admin(user_id: int):
    admins = await read_json(ADMINS_FILE)
    current = [int(x) for x in admins.get("admins", [])]
    if user_id in current:
        current.remove(user_id)
        admins["admins"] = current
        await write_json(ADMINS_FILE, admins)

# LXD helpers
class LXDManager:
    def __init__(self):
        self.client = None
        if PYLXD_AVAILABLE:
            try:
                self.client = pylxd.Client()
            except Exception:
                self.client = None

    def available(self) -> bool:
        return self.client is not None

    def create_container(self, name: str, image_alias: str, cpu: int, ram_gb: int, disk_gb: int, root_password: str) -> Dict[str, Any]:
        if not self.available():
            return {"success": False, "error": "pylxd not available or LXD not accessible"}

        config = {
            "name": name,
            "source": {"type": "image", "alias": image_alias},
            "config": {},
            "devices": {}
        }

        config["profiles"] = []
        try:
            container = self.client.containers.create(config, wait=True)
            container.start(wait=True)
            try:
                cmds = ["sh", "-lc", f"echo 'root:{root_password}' | chpasswd"]
                exec_result = container.execute(cmds, environment=None)
            except Exception:
                pass
            return {"success": True, "container_name": container.name}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def start(self, container_name: str) -> Dict[str, Any]:
        if not self.available():
            return {"success": False, "error": "pylxd unavailable"}
        try:
            cont = self.client.containers.get(container_name)
            cont.start(wait=True)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def stop(self, container_name: str) -> Dict[str, Any]:
        if not self.available():
            return {"success": False, "error": "pylxd unavailable"}
        try:
            cont = self.client.containers.get(container_name)
            cont.stop(wait=True)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def restart(self, container_name: str) -> Dict[str, Any]:
        if not self.available():
            return {"success": False, "error": "pylxd unavailable"}
        try:
            cont = self.client.containers.get(container_name)
            cont.restart(wait=True)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete(self, container_name: str) -> Dict[str, Any]:
        if not self.available():
            return {"success": False, "error": "pylxd unavailable"}
        try:
            cont = self.client.containers.get(container_name)
            if cont.status.lower() == "running":
                cont.stop(wait=True)
            cont.delete(wait=True)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def stats(self, container_name: str) -> Dict[str, Any]:
        if not self.available():
            return {"success": False, "error": "pylxd unavailable"}
        try:
            cont = self.client.containers.get(container_name)
            st = cont.state()
            return {"success": True, "memory": st.memory, "cpu": st.cpu, "status": cont.status}
        except Exception as e:
            return {"success": False, "error": str(e)}


lxd = LXDManager()

# Invite cache manager
invites_cache: Dict[int, Dict[str, int]] = {}  # guild_id -> {code: uses}

async def populate_invites_cache():
    invites_cache.clear()
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            invites_cache[guild.id] = {inv.code: inv.uses for inv in invites}
        except Exception:
            invites_cache[guild.id] = {}

# -- UI Components --

# Plan Add Modal (owner only)
class PlanAddModal(Modal):
    def __init__(self, *, title: str = "Add VPS Plan"):
        super().__init__(title=title)
        self.name = TextInput(label="Plan Name", placeholder="e.g. Basic", max_length=50)
        self.cpu = TextInput(label="CPU cores (int)", placeholder="e.g. 1", max_length=4)
        self.ram = TextInput(label="RAM (GB)", placeholder="e.g. 1", max_length=4)
        self.disk = TextInput(label="Disk (GB)", placeholder="e.g. 10", max_length=6)
        self.description = TextInput(label="Description", style=nextcord.TextInputStyle.paragraph, max_length=500)

        self.add_item(self.name)
        self.add_item(self.cpu)
        self.add_item(self.ram)
        self.add_item(self.disk)
        self.add_item(self.description)

    async def callback(self, interaction: Interaction):
        if OWNER_ID and interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the bot owner can add plans.", ephemeral=True)
            return

        try:
            cpu = int(self.cpu.value)
            ram = int(self.ram.value)
            disk = int(self.disk.value)
        except ValueError:
            await interaction.response.send_message("CPU, RAM, and Disk must be integers.", ephemeral=True)
            return

        points = calculate_points_required(cpu, ram, disk)
        plan_data = {
            "name": self.name.value,
            "cpu": cpu,
            "ram": ram,
            "disk": disk,
            "description": self.description.value,
            "points_required": points,
            "created_at": datetime.utcnow().isoformat()
        }
        plan_id = await add_plan(plan_data)

        embed = Embed(title="Plan Added", description=f"Plan `{plan_data['name']}` added with ID `{plan_id}`", color=0x00FF00)
        embed.set_thumbnail(url=BOT_LOGO_URL)
        embed.add_field(name="CPU", value=str(cpu))
        embed.add_field(name="RAM (GB)", value=str(ram))
        embed.add_field(name="Disk (GB)", value=str(disk))
        embed.add_field(name="Points Required", value=str(points))
        embed.set_footer(text="Made by GG!")

        await interaction.response.send_message(embed=embed)

# Password modal for claim (after plan & OS selected)
class ClaimPasswordModal(Modal):
    def __init__(self, plan_id: str, os_choice: str):
        super().__init__(title="Enter root password")
        self.plan_id = plan_id
        self.os_choice = os_choice
        self.password = TextInput(label="Root password", placeholder="Choose a strong password", min_length=6, max_length=128)
        self.add_item(self.password)

    async def callback(self, interaction: Interaction):
        user_id = interaction.user.id
        plan_id = self.plan_id
        os_choice = self.os_choice.lower()
        plans = await read_json(PLANS_FILE)
        if plan_id not in plans:
            await interaction.response.send_message("Selected plan no longer exists.", ephemeral=True)
            return

        plan = plans[plan_id]
        required = plan.get("points_required", 0)
        user_points = await get_user_points(user_id)
        if user_points < required:
            await interaction.response.send_message(f"You need {required} points to claim this plan but you have {user_points}.", ephemeral=True)
            return

        # deduct points
        await add_user_points(user_id, -required)

        vps_id = generate_vps_id()
        container_name = f"vps-{vps_id}"
        image_alias = "ubuntu/22.04" if "ubuntu" in os_choice else "debian/11"
        tm_link = f"https://tmate.io/t/{vps_id}"  # placeholder tmate link

        vps_entry = {
            "vps_id": vps_id,
            "owner_id": user_id,
            "plan_id": plan_id,
            "os": os_choice,
            "password": self.password.value,
            "container_name": container_name,
            "tm_link": tm_link,
            "status": "creating",
            "created_at": datetime.utcnow().isoformat(),
            "suspended": False,
            "suspended_at": None
        }

        await save_vps(vps_id, vps_entry)

        # Try to create container if pylxd available (best-effort)
        create_result = None
        if lxd.available():
            try:
                create_result = lxd.create_container(name=container_name, image_alias=image_alias,
                                                     cpu=plan.get("cpu", 1),
                                                     ram_gb=plan.get("ram", 1),
                                                     disk_gb=plan.get("disk", 10),
                                                     root_password=self.password.value)
            except Exception as e:
                create_result = {"success": False, "error": str(e)}
        else:
            create_result = {"success": False, "error": "LXD/pylxd not available on host; created VPS entry only."}

        # Update vps entry status based on create_result
        if create_result and create_result.get("success"):
            vps_entry["status"] = "running"
            vps_entry["container_name"] = create_result.get("container_name", container_name)
            await save_vps(vps_id, vps_entry)
            embed = Embed(title="VPS Claimed", description=f"Your VPS `{vps_id}` has been created.", color=0x00FF00)
            embed.add_field(name="Plan", value=plan.get("name"))
            embed.add_field(name="OS", value=os_choice.capitalize())
            embed.add_field(name="Points Deducted", value=str(required))
            embed.add_field(name="SSH (tmate)", value=tm_link)
            embed.set_thumbnail(url=BOT_LOGO_URL)
            embed.set_footer(text="Made by GG!")
            await interaction.response.send_message(embed=embed)
        else:
            # creation failed - keep entry with status 'suspended' or 'failed'
            vps_entry["status"] = "failed"
            vps_entry["create_error"] = create_result.get("error") if create_result else "Unknown error"
            await save_vps(vps_id, vps_entry)
            embed = Embed(title="VPS Claim - Pending", description=f"Your VPS `{vps_id}` claim was recorded but container creation failed.", color=0xFFFF00)
            embed.add_field(name="Reason", value=str(vps_entry.get("create_error")))
            embed.add_field(name="Plan", value=plan.get("name"))
            embed.add_field(name="Points Deducted", value=str(required))
            embed.set_thumbnail(url=BOT_LOGO_URL)
            embed.set_footer(text="Made by GG!")
            await interaction.response.send_message(embed=embed, ephemeral=False)

# Select View for claim (plan select + OS select)
class ClaimSelectView(View):
    def __init__(self, user: nextcord.User, timeout: Optional[float] = 120):
        super().__init__(timeout=timeout)
        self.user = user
        self.selected_plan = None
        self.selected_os = None

    @nextcord.ui.select(placeholder="Select a plan", min_values=1, max_values=1, custom_id="claim_plan_select")
    async def plan_select(self, select: Select, interaction: Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This is not your claim session.", ephemeral=True)
            return
        self.selected_plan = select.values[0]
        if self.selected_os:
            modal = ClaimPasswordModal(plan_id=self.selected_plan, os_choice=self.selected_os)
            await interaction.response.send_modal(modal)
        else:
            await interaction.response.send_message(f"Plan `{self.selected_plan}` selected. Now choose OS.", ephemeral=True)

    @nextcord.ui.select(placeholder="Select OS (Ubuntu/Debian)", min_values=1, max_values=1, custom_id="claim_os_select",
                         options=[nextcord.SelectOption(label="Ubuntu", value="Ubuntu"),
                                  nextcord.SelectOption(label="Debian", value="Debian")])
    async def os_select(self, select: Select, interaction: Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This is not your claim session.", ephemeral=True)
            return
        self.selected_os = select.values[0]
        if self.selected_plan:
            modal = ClaimPasswordModal(plan_id=self.selected_plan, os_choice=self.selected_os)
            await interaction.response.send_modal(modal)
        else:
            await interaction.response.send_message(f"OS `{self.selected_os}` selected. Now choose plan.", ephemeral=True)

# Manage View: After selecting a VPS provide action buttons
class ManageView(View):
    def __init__(self, vps_id: str, owner: nextcord.User, timeout: Optional[float] = 120):
        super().__init__(timeout=timeout)
        self.vps_id = vps_id
        self.owner = owner
        # note: URL button for SSH is added dynamically by the caller when creating this view

    @nextcord.ui.button(style=nextcord.ButtonStyle.green, label="Start", custom_id="btn_start")
    async def start_button(self, button: Button, interaction: Interaction):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("You can't manage this VPS.", ephemeral=True)
            return
        vps_all = await read_json(VPS_FILE)
        vps = vps_all.get(self.vps_id)
        if not vps:
            await interaction.response.send_message("VPS not found.", ephemeral=True)
            return
        container = vps.get("container_name")
        if lxd.available():
            res = lxd.start(container)
            if res.get("success"):
                vps["status"] = "running"
                await save_vps(self.vps_id, vps)
                await interaction.response.send_message("Container started.", ephemeral=True)
            else:
                await interaction.response.send_message(f"Failed to start: {res.get('error')}", ephemeral=True)
        else:
            await interaction.response.send_message("LXD not available on host; no action taken.", ephemeral=True)

    @nextcord.ui.button(style=nextcord.ButtonStyle.danger, label="Stop", custom_id="btn_stop")
    async def stop_button(self, button: Button, interaction: Interaction):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("You can't manage this VPS.", ephemeral=True)
            return
        vps_all = await read_json(VPS_FILE)
        vps = vps_all.get(self.vps_id)
        if not vps:
            await interaction.response.send_message("VPS not found.", ephemeral=True)
            return
        container = vps.get("container_name")
        if lxd.available():
            res = lxd.stop(container)
            if res.get("success"):
                vps["status"] = "stopped"
                await save_vps(self.vps_id, vps)
                await interaction.response.send_message("Container stopped.", ephemeral=True)
            else:
                await interaction.response.send_message(f"Failed to stop: {res.get('error')}", ephemeral=True)
        else:
            await interaction.response.send_message("LXD not available on host; no action taken.", ephemeral=True)

    @nextcord.ui.button(style=nextcord.ButtonStyle.secondary, label="Restart", custom_id="btn_restart")
    async def restart_button(self, button: Button, interaction: Interaction):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("You can't manage this VPS.", ephemeral=True)
            return
        vps_all = await read_json(VPS_FILE)
        vps = vps_all.get(self.vps_id)
        if not vps:
            await interaction.response.send_message("VPS not found.", ephemeral=True)
            return
        container = vps.get("container_name")
        if lxd.available():
            res = lxd.restart(container)
            if res.get("success"):
                vps["status"] = "running"
                await save_vps(self.vps_id, vps)
                await interaction.response.send_message("Container restarted.", ephemeral=True)
            else:
                await interaction.response.send_message(f"Failed to restart: {res.get('error')}", ephemeral=True)
        else:
            await interaction.response.send_message("LXD not available on host; no action taken.", ephemeral=True)

    @nextcord.ui.button(style=nextcord.ButtonStyle.blurple, label="Stats", custom_id="btn_stats")
    async def stats_button(self, button: Button, interaction: Interaction):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("You can't manage this VPS.", ephemeral=True)
            return
        vps_all = await read_json(VPS_FILE)
        vps = vps_all.get(self.vps_id)
        if not vps:
            await interaction.response.send_message("VPS not found.", ephemeral=True)
            return
        container = vps.get("container_name")
        if lxd.available():
            res = lxd.stats(container)
            if res.get("success"):
                embed = Embed(title=f"Stats for {self.vps_id}")
                embed.set_thumbnail(url=BOT_LOGO_URL)
                embed.add_field(name="Status", value=res.get("status"))
                embed.add_field(name="Memory", value=str(res.get("memory", {})))
                embed.add_field(name="CPU", value=str(res.get("cpu", {})))
                embed.set_footer(text="Made by GG!")
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(f"Failed to get stats: {res.get('error')}", ephemeral=True)
        else:
            await interaction.response.send_message("LXD not available on host; no stats.", ephemeral=True)

# -- Slash Commands --

@bot.event
async def on_ready():
    await ensure_json_files()
    await populate_invites_cache()
    # Try to sync application commands (slash) - safe to call if supported
    try:
        await bot.sync_application_commands()
    except Exception:
        pass
    print(f"Bot ready. Logged in as {bot.user} (ID: {bot.user.id})")
    change_status.start()
    suspend_monitor.start()

# rotate status every 5 seconds
statuses = [
    ("Watching TOTALSERVER", nextcord.ActivityType.watching),
    ("Powering SnowCloud", nextcord.ActivityType.playing),
    ("Made by GG!", nextcord.ActivityType.playing)
]
status_index = 0

@tasks.loop(seconds=5.0)
async def change_status():
    global status_index
    try:
        name, type_ = statuses[status_index % len(statuses)]
        await bot.change_presence(activity=nextcord.Activity(type=type_, name=name))
        status_index += 1
    except Exception:
        pass

# Suspend monitor: check vpses if owner lost points
@tasks.loop(seconds=30.0)
async def suspend_monitor():
    vps_all = await read_json(VPS_FILE)
    plans = await read_json(PLANS_FILE)
    users = await read_json(USERS_FILE)
    now = datetime.utcnow()
    for vps_id, v in list(vps_all.items()):
        owner_id = int(v.get("owner_id"))
        plan_id = v.get("plan_id")
        plan = plans.get(plan_id)
        if not plan:
            continue
        required = int(plan.get("points_required", 0))
        owner_points = int(users.get(str(owner_id), {}).get("points", 0))
        suspended = v.get("suspended", False)
        suspended_at = v.get("suspended_at")
        if owner_points < required and not suspended:
            cont_name = v.get("container_name")
            if lxd.available():
                try:
                    lxd.stop(cont_name)
                except Exception:
                    pass
            v["suspended"] = True
            v["suspended_at"] = datetime.utcnow().isoformat()
            v["status"] = "suspended"
            await save_vps(vps_id, v)
            try:
                user = await bot.fetch_user(owner_id)
                await user.send(f"Your VPS `{vps_id}` has been suspended because your points ({owner_points}) are below the required {required}. You have 1 hour to restore points or it will be deleted.")
            except Exception:
                pass
        elif suspended:
            if suspended_at:
                suspended_time = datetime.fromisoformat(suspended_at)
                if datetime.utcnow() - suspended_time > timedelta(hours=1):
                    cont_name = v.get("container_name")
                    if lxd.available():
                        try:
                            lxd.delete(cont_name)
                        except Exception:
                            pass
                    await delete_vps_entry(vps_id)
                    try:
                        user = await bot.fetch_user(owner_id)
                        await user.send(f"Your VPS `{vps_id}` was deleted because it remained suspended for more than 1 hour.")
                    except Exception:
                        pass

# Invite tracking
@bot.event
async def on_guild_join(guild):
    try:
        invites = await guild.invites()
        invites_cache[guild.id] = {inv.code: inv.uses for inv in invites}
    except Exception:
        invites_cache[g.id] = {}

@bot.event
async def on_invite_create(invite):
    g_id = invite.guild.id
    try:
        invites_cache[g_id][invite.code] = invite.uses
    except Exception:
        invites_cache[g_id] = invites_cache.get(g_id, {})
        invites_cache[g_id][invite.code] = invite.uses

@bot.event
async def on_invite_delete(invite):
    g_id = invite.guild.id
    try:
        if invite.code in invites_cache.get(g_id, {}):
            del invites_cache[g_id][invite.code]
    except Exception:
        pass

@bot.event
async def on_member_join(member):
    g = member.guild
    try:
        new_invites = await g.invites()
    except Exception:
        new_invites = []
    previous = invites_cache.get(g.id, {})
    used_inviter = None
    for inv in new_invites:
        prev_uses = previous.get(inv.code, 0)
        if inv.uses > prev_uses:
            used_inviter = inv.inviter
            break
    invites_cache[g.id] = {inv.code: inv.uses for inv in new_invites}
    if used_inviter:
        await add_user_points(used_inviter.id, 1)
        invites_json = await read_json(INVITES_FILE)
        invites_json[str(used_inviter.id)] = invites_json.get(str(used_inviter.id), 0) + 1
        await write_json(INVITES_FILE, invites_json)
        try:
            await used_inviter.send(f"You gained 1 point because {member} joined using your invite. (+1 point)")
        except Exception:
            pass

# -- Slash commands implementation --

@bot.slash_command(name="plan-add", description="Owner: Add a VPS plan")
async def plan_add(interaction: Interaction):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Only the bot owner can add plans.", ephemeral=True)
        return
    modal = PlanAddModal()
    await interaction.response.send_modal(modal)

@bot.slash_command(name="plans", description="List all VPS plans")
async def plans_cmd(interaction: Interaction):
    plans = await read_json(PLANS_FILE)
    if not plans:
        embed = Embed(title="Plans", description="No plans available.", color=0xFF0000)
        embed.set_thumbnail(url=BOT_LOGO_URL)
        embed.set_footer(text="Made by GG!")
        await interaction.response.send_message(embed=embed)
        return
    embed = Embed(title="Available VPS Plans", color=0x00AAFF)
    embed.set_thumbnail(url=BOT_LOGO_URL)
    for pid, p in plans.items():
        name = p.get("name")
        desc = p.get("description", "")
        cpu = p.get("cpu")
        ram = p.get("ram")
        disk = p.get("disk")
        points = p.get("points_required")
        embed.add_field(name=f"{name} — ID: {pid}", value=f"CPU: {cpu} cores | RAM: {ram}GB | Disk: {disk}GB\nPoints: {points}\n{desc}", inline=False)
    embed.set_footer(text="Made by GG!")
    await interaction.response.send_message(embed=embed)

@bot.slash_command(name="claim-vps", description="Claim a VPS plan")
async def claim_vps(interaction: Interaction):
    plans = await read_json(PLANS_FILE)
    if not plans:
        await interaction.response.send_message("No plans available to claim.", ephemeral=True)
        return

    view = ClaimSelectView(user=interaction.user)
    options = []
    for pid, p in plans.items():
        label = f"{p.get('name')} ({p.get('cpu')}c/{p.get('ram')}GB/{p.get('disk')}GB)"
        desc = f"Points: {p.get('points_required')}"
        options.append(nextcord.SelectOption(label=label, value=pid, description=str(desc)[:100]))
    for child in view.children:
        if isinstance(child, nextcord.ui.Select) and getattr(child, "custom_id", "") == "claim_plan_select":
            child.options = options
    await interaction.response.send_message("Select plan and OS to claim a VPS:", view=view, ephemeral=True)

@bot.slash_command(name="manage", description="Manage your VPSes")
async def manage_cmd(interaction: Interaction):
    vps_all = await read_json(VPS_FILE)
    user_vps = [v for v in vps_all.values() if int(v.get("owner_id")) == interaction.user.id]
    if not user_vps:
        embed = Embed(title="Manage VPS", description="You have no VPSes.", color=0xFFAA00)
        embed.set_thumbnail(url=BOT_LOGO_URL)
        embed.set_footer(text="Made by GG!")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    options = []
    for v in user_vps:
        vid = v.get("vps_id")
        status = v.get("status", "unknown")
        label = f"{vid} ({status})"
        desc = f"Plan: {v.get('plan_id')}"
        options.append(nextcord.SelectOption(label=label, value=vid, description=desc[:100]))

    class VPSSelectView(View):
        @nextcord.ui.select(placeholder="Select one of your VPSes", min_values=1, max_values=1)
        async def select_callback(self, select: Select, interaction2: Interaction):
            if interaction2.user.id != interaction.user.id:
                await interaction2.response.send_message("This is not your session.", ephemeral=True)
                return
            selected_vid = select.values[0]
            vps_entry = vps_all.get(selected_vid)
            if not vps_entry:
                await interaction2.response.send_message("VPS not found.", ephemeral=True)
                return
            plan = (await read_json(PLANS_FILE)).get(vps_entry.get("plan_id"), {})
            embed = Embed(title=f"VPS {selected_vid}", color=0x00FFAA)
            embed.set_thumbnail(url=BOT_LOGO_URL)
            embed.add_field(name="Plan", value=plan.get("name", vps_entry.get("plan_id")))
            embed.add_field(name="OS", value=vps_entry.get("os"))
            embed.add_field(name="Status", value=vps_entry.get("status"))
            embed.add_field(name="SSH (tmate)", value=vps_entry.get("tm_link"))
            embed.set_footer(text="Made by GG!")
            view = ManageView(vps_id=selected_vid, owner=interaction.user)
            # Add URL SSH button dynamically
            url_btn = Button(style=nextcord.ButtonStyle.url, label="SSH (tmate)", url=vps_entry.get("tm_link"))
            view.add_item(url_btn)
            await interaction2.response.send_message(embed=embed, view=view, ephemeral=True)

    v = VPSSelectView()
    for child in v.children:
        if isinstance(child, nextcord.ui.Select):
            child.options = options
    await interaction.response.send_message("Select a VPS to manage:", view=v, ephemeral=True)

@bot.slash_command(name="points", description="Show your points")
async def points_cmd(interaction: Interaction):
    pts = await get_user_points(interaction.user.id)
    embed = Embed(title=f"{interaction.user.name}'s Points", description=f"You have {pts} points.", color=0x00AAFF)
    embed.set_thumbnail(url=BOT_LOGO_URL)
    embed.set_footer(text="Made by GG!")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.slash_command(name="leaderboard", description="Top users by points")
async def leaderboard_cmd(interaction: Interaction):
    users = await read_json(USERS_FILE)
    if not users:
        await interaction.response.send_message("No points data yet.", ephemeral=True)
        return
    arr = []
    for uid, data in users.items():
        pts = int(data.get("points", 0))
        arr.append((int(uid), pts))
    arr.sort(key=lambda x: x[1], reverse=True)
    top = arr[:10]
    desc = ""
    for idx, (uid, pts) in enumerate(top, start=1):
        user = await bot.fetch_user(uid)
        desc += f"{idx}. {user.name} — {pts} pts\n"
    embed = Embed(title="Leaderboard", description=desc, color=0xFFD700)
    embed.set_thumbnail(url=BOT_LOGO_URL)
    embed.set_footer(text="Made by GG!")
    await interaction.response.send_message(embed=embed)

# Admin commands: add/remove admins
@bot.slash_command(name="admin-add", description="Owner: Add an admin")
async def admin_add(interaction: Interaction, user: nextcord.User = SlashOption(description="User to make admin")):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Only owner can add admins.", ephemeral=True)
        return
    await add_admin(user.id)
    await interaction.response.send_message(f"Added {user} as admin.", ephemeral=True)

@bot.slash_command(name="admin-remove", description="Owner: Remove an admin")
async def admin_remove(interaction: Interaction, user: nextcord.User = SlashOption(description="Admin to remove")):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Only owner can remove admins.", ephemeral=True)
        return
    await remove_admin(user.id)
    await interaction.response.send_message(f"Removed {user} from admins.", ephemeral=True)

# Admin create VPS manually (admins only): modal with target user, cpu,ram,disk,os,password
class AdminCreateVPSModal(Modal):
    def __init__(self, target_id: int):
        super().__init__(title="Admin Create VPS")
        self.target_id = target_id
        self.plan_name = TextInput(label="Plan name", placeholder="Name for this manual plan", max_length=50)
        self.cpu = TextInput(label="CPU cores (int, max 50)", placeholder="e.g. 2", max_length=4)
        self.ram = TextInput(label="RAM (GB, max 50)", placeholder="e.g. 4", max_length=4)
        self.disk = TextInput(label="Disk (GB, max 50)", placeholder="e.g. 20", max_length=6)
        self.os = TextInput(label="OS (Ubuntu/Debian)", placeholder="Ubuntu or Debian", max_length=20)
        self.password = TextInput(label="Root password", placeholder="Root password", min_length=6, max_length=128)
        self.add_item(self.plan_name)
        self.add_item(self.cpu)
        self.add_item(self.ram)
        self.add_item(self.disk)
        self.add_item(self.os)
        self.add_item(self.password)

    async def callback(self, interaction: Interaction):
        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("You are not an admin.", ephemeral=True)
            return
        try:
            cpu = int(self.cpu.value); ram = int(self.ram.value); disk = int(self.disk.value)
        except ValueError:
            await interaction.response.send_message("CPU/RAM/Disk must be integers.", ephemeral=True)
            return
        if ram > 50 or disk > 50:
            await interaction.response.send_message("Admins cannot create VPS with RAM or Disk > 50GB.", ephemeral=True)
            return
        points_req = calculate_points_required(cpu, ram, disk)
        plan_data = {
            "name": self.plan_name.value,
            "cpu": cpu,
            "ram": ram,
            "disk": disk,
            "description": f"Admin created plan for user {self.target_id}",
            "points_required": points_req,
            "created_at": datetime.utcnow().isoformat()
        }
        plan_id = await add_plan(plan_data)
        vps_id = generate_vps_id()
        container_name = f"vps-{vps_id}"
        image_alias = "ubuntu/22.04" if "ubuntu" in self.os.value.lower() else "debian/11"
        tm_link = f"https://tmate.io/t/{vps_id}"
        vps_entry = {
            "vps_id": vps_id,
            "owner_id": self.target_id,
            "plan_id": plan_id,
            "os": self.os.value,
            "password": self.password.value,
            "container_name": container_name,
            "tm_link": tm_link,
            "status": "creating",
            "created_at": datetime.utcnow().isoformat(),
            "suspended": False,
            "suspended_at": None,
            "admin_created_by": interaction.user.id
        }
        await save_vps(vps_id, vps_entry)
        if lxd.available():
            res = lxd.create_container(name=container_name, image_alias=image_alias,
                                       cpu=cpu, ram_gb=ram, disk_gb=disk, root_password=self.password.value)
            if res.get("success"):
                vps_entry["status"] = "running"
                vps_entry["container_name"] = res.get("container_name", container_name)
                await save_vps(vps_id, vps_entry)
                await interaction.response.send_message(f"VPS {vps_id} created for user {self.target_id}.", ephemeral=True)
            else:
                vps_entry["status"] = "failed"
                vps_entry["create_error"] = res.get("error")
                await save_vps(vps_id, vps_entry)
                await interaction.response.send_message(f"VPS {vps_id} recorded but container creation failed: {res.get('error')}", ephemeral=True)
        else:
            await interaction.response.send_message(f"VPS {vps_id} recorded (LXD unavailable).", ephemeral=True)

@bot.slash_command(name="admin-create-vps", description="Admin: Create VPS for a user (limits: 50GB RAM/Disk)")
async def admin_create_vps(interaction: Interaction, target: nextcord.User = SlashOption(description="User to create VPS for")):
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message("You are not an admin.", ephemeral=True)
        return
    modal = AdminCreateVPSModal(target_id=target.id)
    await interaction.response.send_modal(modal)

# -- Error handling for commands
@bot.event
async def on_application_command_error(interaction: Interaction, error):
    try:
        await interaction.response.send_message(f"Error: {str(error)}", ephemeral=True)
    except Exception:
        pass

# Entry point
if __name__ == "__main__":
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TOKEN_HERE":
        print("Please set BOT_TOKEN in the .env file.")
    else:
        bot.run(BOT_TOKEN)
