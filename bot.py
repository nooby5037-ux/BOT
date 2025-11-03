# bot_final_dedicated_autocreate.py
# Automatic dedicated KVM VM creation + HostForge features (owner DM with simple details)
#
# IMPORTANT:
# - This file will RUN privileged host commands (qemu-img, cloud-localds, virt-install, virsh, iptables).
# - You MUST run this bot as root (or via sudo) on a host that has KVM/libvirt installed.
# - Host prerequisites (run once):
#     apt update
#     apt install -y qemu-kvm libvirt-daemon-system libvirt-clients virtinst cloud-image-utils wget iptables
#
# Usage:
# - Place this file on host, set DISCORD_TOKEN and ADMIN_IDS in .env
# - Run as root: python3 bot_final_dedicated_autocreate.py
#
# Behavior:
# - /create_vps (admin only) will create a VM fully automatically, set NAT mapping, update DB and DM owner.
# - The bot stores basic info in SQLite DB (hostforge.db).
# - Commands: /create_vps, /list, /vps_list, /connect_vps, /vps_stats, /delete_vps, /change_ssh_password, admin utilities.

import os
import random
import string
import datetime
import asyncio
import shutil
import subprocess
import logging
import sqlite3
import time
import re
import json
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# ---------------- Config ----------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "903171487563923499").split(",") if x.strip()}
HOST_IP = os.getenv("HOST_IP")  # external IP (optional). If not set we'll try detect.
LIBVIRT_IMG_DIR = os.getenv("LIBVIRT_IMG_DIR", "/var/lib/libvirt/images")
BASE_IMAGE_URL = os.getenv("BASE_IMAGE_URL", "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img")
BASE_IMAGE_NAME = os.getenv("BASE_IMAGE_NAME", "ubuntu-22.04-cloud.img")
DB_FILE = os.getenv("DB_FILE", "hostforge.db")
PORT_RANGE_START = int(os.getenv("PORT_RANGE_START", "20000"))
PORT_RANGE_END = int(os.getenv("PORT_RANGE_END", "30000"))
WELCOME_MESSAGE = "Welcome To HostForge! Get Started With Us!"
WATERMARK = " VPS Service"

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("HostForgeAuto")

# ---------------- Database ----------------
class DB:
    def __init__(self, path=DB_FILE):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.cur = self.conn.cursor()
        self._init_tables()

    def _init_tables(self):
        self.cur.execute('''
        CREATE TABLE IF NOT EXISTS vps_instances (
            token TEXT PRIMARY KEY,
            vps_id TEXT UNIQUE,
            vm_name TEXT,
            disk_path TEXT,
            memory INTEGER,
            cpu INTEGER,
            disk INTEGER,
            username TEXT,
            password TEXT,
            root_password TEXT,
            created_by TEXT,
            created_at TEXT,
            status TEXT,
            external_ssh_port INTEGER,
            guest_ip TEXT
        )
        ''')
        self.conn.commit()

    def add_vps(self, data: dict):
        cols = ",".join(data.keys())
        placeholders = ",".join("?" for _ in data)
        self.cur.execute(f"INSERT INTO vps_instances ({cols}) VALUES ({placeholders})", tuple(data.values()))
        self.conn.commit()

    def update_vps(self, token, updates: dict):
        set_clause = ",".join(f"{k}=?" for k in updates.keys())
        vals = list(updates.values()) + [token]
        self.cur.execute(f"UPDATE vps_instances SET {set_clause} WHERE token=?", vals)
        self.conn.commit()

    def get_vps_by_token(self, token):
        self.cur.execute("SELECT * FROM vps_instances WHERE token=?", (token,))
        row = self.cur.fetchone()
        if not row:
            return None
        keys = [d[0] for d in self.cur.description]
        return dict(zip(keys, row))

    def get_vps_by_id(self, vps_id):
        self.cur.execute("SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,))
        row = self.cur.fetchone()
        if not row:
            return None, None
        keys = [d[0] for d in self.cur.description]
        return row[0], dict(zip(keys, row))  # token, dict

    def get_user_vps(self, user_id):
        self.cur.execute("SELECT * FROM vps_instances WHERE created_by=?", (str(user_id),))
        rows = self.cur.fetchall()
        keys = [d[0] for d in self.cur.description]
        return [dict(zip(keys, r)) for r in rows]

    def get_all_vps(self):
        self.cur.execute("SELECT * FROM vps_instances")
        rows = self.cur.fetchall()
        keys = [d[0] for d in self.cur.description]
        return {r[0]: dict(zip(keys, r)) for r in rows}

    def remove_vps(self, token):
        self.cur.execute("DELETE FROM vps_instances WHERE token=?", (token,))
        self.conn.commit()

    def get_used_ports(self):
        self.cur.execute("SELECT external_ssh_port FROM vps_instances WHERE external_ssh_port IS NOT NULL")
        return {r[0] for r in self.cur.fetchall()}

db = DB(DB_FILE)

# ---------------- Utilities ----------------
def rand_token(n=24):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=n))

def gen_vps_id():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))

def find_free_port():
    used = db.get_used_ports()
    while True:
        p = random.randint(PORT_RANGE_START, PORT_RANGE_END)
        if p not in used:
            return p

def ensure_base_image():
    os.makedirs(LIBVIRT_IMG_DIR, exist_ok=True)
    dest = os.path.join(LIBVIRT_IMG_DIR, BASE_IMAGE_NAME)
    if not os.path.exists(dest):
        logger.info("Base image not found, downloading...")
        cmd = ["wget", "-O", dest, BASE_IMAGE_URL]
        subprocess.run(cmd, check=True)
    return dest

def run_checked(cmd, timeout=600, capture_output=False):
    logger.info("RUN: " + " ".join(cmd))
    res = subprocess.run(cmd, stdout=subprocess.PIPE if capture_output else None,
                         stderr=subprocess.PIPE if capture_output else None, timeout=timeout, text=True)
    if capture_output:
        logger.debug("STDOUT: %s", res.stdout)
        logger.debug("STDERR: %s", res.stderr)
    if res.returncode != 0:
        raise RuntimeError(f"Command {' '.join(cmd)} failed (rc={res.returncode}) - stderr: {res.stderr if capture_output else 'see logs'}")
    return res.stdout if capture_output else ""

def get_guest_ip_by_virsh(vm_name, tries=8, wait=3) -> Optional[str]:
    # Try to parse virsh net-dhcp-leases default to find IP for vm_name
    for attempt in range(tries):
        try:
            out = run_checked(["virsh", "net-dhcp-leases", "default"], capture_output=True)
            # lines like: "  52:54:00:xx:xx:xx    192.168.122.123/24  hostforge-abc (id 3)"
            for line in out.splitlines():
                if vm_name in line:
                    # find ip pattern
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)/\d+", line)
                    if m:
                        return m.group(1)
            time.sleep(wait)
        except Exception as e:
            logger.debug("virsh lease check failed: %s", e)
            time.sleep(wait)
    return None

def detect_host_ip():
    if HOST_IP:
        return HOST_IP
    # try to get public ip via ip route
    try:
        out = run_checked(["curl", "-s", "https://api.ipify.org"], capture_output=True)
        out = out.strip()
        if re.match(r"\d+\.\d+\.\d+\.\d+", out):
            return out
    except Exception:
        pass
    # fallback to hostname -I first ip
    try:
        out = run_checked(["hostname", "-I"], capture_output=True)
        ip = out.strip().split()[0]
        return ip
    except Exception:
        return "127.0.0.1"

# ---------------- Discord bot ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)

def is_admin_ctx(ctx):
    try:
        uid = ctx.author.id if hasattr(ctx, "author") else ctx.user.id
        return uid in ADMIN_IDS
    except:
        return False

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (id: {bot.user.id})")

# ---------------- VM create flow (automatic) ----------------
def build_cloud_userdata(vm_name, root_password):
    ud = f"""#cloud-config
password: {root_password}
chpasswd:
  list:
    - root:{root_password}
  expire: False
ssh_pwauth: True
runcmd:
  - apt-get update || true
  - apt-get install -y qemu-guest-agent || true
  - systemctl enable --now qemu-guest-agent || true
"""
    return ud

def create_vm_and_configure(vm_name: str, memory_gb: int, cpu: int, disk_gb: int, root_password: str, host_port: int):
    """
    Performs:
    - ensure base image
    - qemu-img create overlay
    - create cloud-init seed iso
    - virt-install import
    - wait for guest ip
    - add iptables DNAT rule
    Returns guest_ip
    """
    base_img = ensure_base_image()
    vm_disk = os.path.join(LIBVIRT_IMG_DIR, f"{vm_name}.qcow2")
    seed_iso = os.path.join(LIBVIRT_IMG_DIR, f"{vm_name}-seed.iso")

    # create cloud-init files
    tmpdir = f"/tmp/hostforge_cloud_{vm_name}"
    if os.path.exists(tmpdir):
        shutil.rmtree(tmpdir)
    os.makedirs(tmpdir, exist_ok=True)
    user_data = build_cloud_userdata(vm_name, root_password)
    with open(os.path.join(tmpdir, "user-data"), "w") as f:
        f.write(user_data)
    with open(os.path.join(tmpdir, "meta-data"), "w") as f:
        f.write(f"instance-id: {vm_name}\nlocal-hostname: {vm_name}\n")

    # create seed iso
    run_checked(["cloud-localds", seed_iso, os.path.join(tmpdir, "user-data"), os.path.join(tmpdir, "meta-data")])

    # create qcow2 overlay
    run_checked(["qemu-img", "create", "-f", "qcow2", "-b", base_img, vm_disk, f"{disk_gb}G"])

    # run virt-install import
    mem_mb = int(memory_gb) * 1024
    virt_cmd = [
        "virt-install",
        "--name", vm_name,
        "--memory", str(mem_mb),
        "--vcpus", str(cpu),
        "--disk", f"path={vm_disk},format=qcow2",
        "--import",
        "--os-variant", "ubuntu22.04",
        "--network", "network=default",
        "--graphics", "none",
        "--noautoconsole",
        "--disk", f"path={seed_iso},device=cdrom"
    ]
    run_checked(virt_cmd, timeout=1200)

    # wait a bit and fetch guest IP
    guest_ip = get_guest_ip_by_virsh(vm_name, tries=20, wait=3)
    if not guest_ip:
        raise RuntimeError("Could not detect guest IP via virsh net-dhcp-leases")

    # add iptables NAT mapping: host_port -> guest_ip:22
    # first ensure rule doesn't already exist
    try:
        run_checked(["iptables", "-t", "nat", "-C", "PREROUTING", "-p", "tcp", "--dport", str(host_port), "-j", "DNAT", "--to-destination", f"{guest_ip}:22"])
    except Exception:
        # rule not present: add it
        run_checked(["iptables", "-t", "nat", "-A", "PREROUTING", "-p", "tcp", "--dport", str(host_port), "-j", "DNAT", "--to-destination", f"{guest_ip}:22"])
        run_checked(["iptables", "-t", "nat", "-A", "POSTROUTING", "-p", "tcp", "-d", guest_ip, "--dport", "22", "-j", "MASQUERADE"])

    return guest_ip

# ---------------- Commands ----------------
@bot.hybrid_command(name="create_vps", description="Create a new VPS (Admin only) - fully automatic")
@app_commands.describe(memory="Memory (GB)", cpu="CPU cores", disk="Disk (GB)", owner="Owner mention")
async def cmd_create_vps(ctx, memory: int, cpu: int, disk: int, owner: discord.Member):
    if not is_admin_ctx(ctx):
        await ctx.send("❌ Admins only", ephemeral=True)
        return

    # Ensure bot is run as root
    if os.geteuid() != 0:
        await ctx.send("❌ Bot must run as root for automatic VM creation. Run bot as root or use manual-script variant.", ephemeral=True)
        return

    # basic validation
    if memory < 1 or memory > 512:
        await ctx.send("❌ Memory must be between 1 and 512 GB", ephemeral=True); return
    if cpu < 1 or cpu > 64:
        await ctx.send("❌ CPU must be between 1 and 64", ephemeral=True); return
    if disk < 10 or disk > 2000:
        await ctx.send("❌ Disk must be between 10 and 2000 GB", ephemeral=True); return

    status_msg = await ctx.send("🚀 Creating dedicated VM (this may take a few minutes)...")

    vps_id = gen_vps_id()
    vm_name = f"hostforge-{vps_id.lower()}"
    token = rand_token()
    username = "root"
    password = "root"  # you can randomize later
    root_password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    host_port = find_free_port()
    created_at = str(datetime.datetime.now())

    # write DB record as pending
    db.add_vps({
        "token": token,
        "vps_id": vps_id,
        "vm_name": vm_name,
        "disk_path": os.path.join(LIBVIRT_IMG_DIR, f"{vm_name}.qcow2"),
        "memory": memory,
        "cpu": cpu,
        "disk": disk,
        "username": username,
        "password": password,  # stored password (for tmate / initial root)
        "root_password": root_password,
        "created_by": str(owner.id),
        "created_at": created_at,
        "status": "creating",
        "external_ssh_port": host_port,
        "guest_ip": None
    })

    try:
        # create vm and configure NAT
        guest_ip = create_vm_and_configure(vm_name, memory, cpu, disk, root_password, host_port)

        # update DB
        db.update_vps(token, {"guest_ip": guest_ip, "status": "running"})

        # detect host external ip
        host_ip = detect_host_ip()

        # send DM to owner with simple details
        embed = discord.Embed(title="🎉 Your HostForge VPS is ready!", color=discord.Color.green())
        embed.add_field(name="VPS ID", value=vps_id, inline=True)
        embed.add_field(name="VM Name", value=vm_name, inline=True)
        embed.add_field(name="Memory", value=f"{memory} GB", inline=True)
        embed.add_field(name="CPU Cores", value=str(cpu), inline=True)
        embed.add_field(name="Disk", value=f"{disk} GB", inline=True)
        embed.add_field(name="Username", value=username, inline=True)
        embed.add_field(name="Root password", value=f"||{root_password}||", inline=False)
        embed.add_field(name="SSH (host NAT)", value=f"```ssh root@{host_ip} -p {host_port}```", inline=False)
        embed.add_field(name="Direct guest (if on LAN)", value=f"```ssh root@{guest_ip} -p 22```", inline=False)
        await owner.send(embed=embed)
        await status_msg.edit(content=f"✅ VM {vm_name} created and NAT mapped. Owner DM'd with connection details.")
    except Exception as e:
        logger.exception("Error creating VM")
        # mark DB entry as errored
        db.update_vps(token, {"status": "error"})
        await status_msg.edit(content=f"❌ Error during VM creation: {e}. See logs.")
        # optional cleanup: remove disk and seed files if exist
        try:
            diskpath = os.path.join(LIBVIRT_IMG_DIR, f"{vm_name}.qcow2")
            seediso = os.path.join(LIBVIRT_IMG_DIR, f"{vm_name}-seed.iso")
            for p in (diskpath, seediso):
                if os.path.exists(p):
                    os.remove(p)
        except:
            pass

@bot.hybrid_command(name="list", description="List your VPS")
async def cmd_list(ctx):
    vps = db.get_user_vps(ctx.author.id)
    if not vps:
        await ctx.send("You have no VPS instances.", ephemeral=True); return
    embed = discord.Embed(title="Your VPS Instances", color=discord.Color.blue())
    for v in vps:
        embed.add_field(name=f"VPS {v['vps_id']}", value=f"Status: {v.get('status')}\nMemory: {v.get('memory')}GB\nCPU: {v.get('cpu')}\nDisk: {v.get('disk')}GB\nSSH Port: {v.get('external_ssh_port')}\nGuest IP: {v.get('guest_ip') or 'Not set'}", inline=False)
    await ctx.send(embed=embed, ephemeral=True)

@bot.hybrid_command(name="vps_list", description="List all VPS (admin only)")
async def cmd_vps_list(ctx):
    if not is_admin_ctx(ctx):
        await ctx.send("❌ Admins only", ephemeral=True); return
    allv = db.get_all_vps()
    if not allv:
        await ctx.send("No VPS records.", ephemeral=True); return
    embed = discord.Embed(title="All VPS Records", color=discord.Color.blue())
    for token, v in allv.items():
        owner = "Unknown"
        try:
            owner = (await bot.fetch_user(int(v.get("created_by")))).name
        except:
            pass
        embed.add_field(name=f"VPS {v['vps_id']}", value=f"Owner: {owner}\nStatus: {v.get('status')}\nMemory: {v.get('memory')}GB\nCPU: {v.get('cpu')}\nGuest IP: {v.get('guest_ip') or 'Not set'}\nHostPort: {v.get('external_ssh_port')}", inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="connect_vps", description="Get connection details for your VPS token")
@app_commands.describe(token="VPS token")
async def cmd_connect_vps(ctx, token: str):
    v = db.get_vps_by_token(token)
    if not v:
        await ctx.send("❌ Invalid token", ephemeral=True); return
    if str(ctx.author.id) != v.get("created_by") and not is_admin_ctx(ctx):
        await ctx.send("❌ You don't have permission to access this VPS", ephemeral=True); return
    host_ip = detect_host_ip()
    if v.get("guest_ip"):
        embed = discord.Embed(title=f"Connection for {v['vps_id']}", color=discord.Color.blue())
        embed.add_field(name="Host NAT SSH", value=f"```ssh root@{host_ip} -p {v.get('external_ssh_port')}```", inline=False)
        embed.add_field(name="Direct Guest SSH", value=f"```ssh root@{v.get('guest_ip')} -p 22```", inline=False)
        embed.add_field(name="Password", value=f"||{v.get('root_password')}||", inline=False)
        await ctx.author.send(embed=embed)
        await ctx.send("✅ I sent connection details to your DMs.", ephemeral=True)
    else:
        await ctx.author.send(f"Guest IP not set for {v['vps_id']}. Wait until VM finishes creating or ask admin to run /vps_list and see status.", ephemeral=True)
        await ctx.send("ℹ️ Details sent to DM.", ephemeral=True)

@bot.hybrid_command(name="vps_stats", description="Show VPS stored info")
@app_commands.describe(vps_id="VPS ID")
async def cmd_vps_stats(ctx, vps_id: str):
    token, v = db.get_vps_by_id(vps_id)
    if not v or (v.get("created_by") != str(ctx.author.id) and not is_admin_ctx(ctx)):
        await ctx.send("❌ Not found or no permission", ephemeral=True); return
    embed = discord.Embed(title=f"VPS {vps_id} info", color=discord.Color.blue())
    embed.add_field(name="Status", value=v.get("status"))
    embed.add_field(name="Memory", value=f"{v.get('memory')} GB")
    embed.add_field(name="CPU", value=str(v.get('cpu')))
    embed.add_field(name="Disk", value=f"{v.get('disk')} GB")
    embed.add_field(name="Guest IP", value=v.get('guest_ip') or "Not set")
    embed.add_field(name="External Port", value=str(v.get('external_ssh_port')))
    await ctx.send(embed=embed, ephemeral=True)

@bot.hybrid_command(name="delete_vps", description="Delete VPS record (Admin only)")
@app_commands.describe(vps_id="VPS ID")
async def cmd_delete_vps(ctx, vps_id: str):
    if not is_admin_ctx(ctx):
        await ctx.send("❌ Admins only", ephemeral=True); return
    token, v = db.get_vps_by_id(vps_id)
    if not v:
        await ctx.send("❌ VPS not found", ephemeral=True); return
    db.remove_vps(token)
    await ctx.send(f"✅ VPS record {vps_id} removed from DB. If VM exists, delete it manually with virsh destroy/undefine and remove disk.", ephemeral=True)

@bot.hybrid_command(name="change_ssh_password", description="Get instructions to change root password")
@app_commands.describe(vps_id="VPS ID")
async def cmd_change_pw(ctx, vps_id: str):
    token, v = db.get_vps_by_id(vps_id)
    if not v or v.get("created_by") != str(ctx.author.id):
        await ctx.send("❌ Not found or no permission", ephemeral=True); return
    if not v.get("guest_ip"):
        await ctx.send("❌ Guest IP not set yet. Wait until VM is created.", ephemeral=True); return
    newpw = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    # instruct owner to run command inside VM
    dm = f"SSH to your VM: ssh root@{v.get('guest_ip')}\nThen run as root:\n echo 'root:{newpw}' | chpasswd\nNew password: ||{newpw}||\nAfter applying, you can update bot DB if you want."
    try:
        await ctx.author.send(dm)
        await ctx.send("✅ Password instructions sent to DM.", ephemeral=True)
        db.update_vps(token, {"password": newpw, "root_password": newpw})
    except discord.Forbidden:
        await ctx.send("❌ Can't DM you. Enable DMs.", ephemeral=True)

# ---------------- Run bot ----------------
if __name__ == "__main__":
    if not TOKEN:
        print("DISCORD_TOKEN not set in .env")
        exit(1)
    # quick sanity: running as root recommended for auto mode (but we allow running non-root to fail gracefully)
    if os.geteuid() != 0:
        logger.warning("Bot is NOT running as root. Automatic VM creation will fail. Run as root for auto mode.")
    bot.run(TOKEN)
