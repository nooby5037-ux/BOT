# bot_final_dedicated_template.py
# Safe template: preserves your bot structure and commands.
# /create_vps will PREPARE everything and write a shell script that MUST be run as root on the host.
# This avoids running privileged commands from the bot process.
#
# NOTE: You must install required host packages yourself:
# sudo apt update
# sudo apt install -y qemu-kvm libvirt-daemon-system libvirt-clients virtinst cloud-image-utils iptables

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

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# ------------- Config / load env --------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "903171487563923499").split(",") if x.strip()}
WATERMARK = " VPS Service"
WELCOME_MESSAGE = "Welcome To  ! Get Started With Us!"
MAX_VPS_PER_USER = int(os.getenv("MAX_VPS_PER_USER", "3"))
LIBVIRT_IMG_DIR = "/var/lib/libvirt/images"
BASE_IMAGE_URL = "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"
BASE_IMAGE_NAME = "ubuntu-22.04-cloud.img"
PORT_RANGE_START = 20000
PORT_RANGE_END = 30000
DB_FILE = "hostforge.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("HostForgeTemplate")

# ---------- Simple DB helper (sqlite, similar to your file) ----------
class DB:
    def __init__(self, dbfile=DB_FILE):
        self.conn = sqlite3.connect(dbfile, check_same_thread=False)
        self.cur = self.conn.cursor()
        self._init_tables()

    def _init_tables(self):
        self.cur.execute('''CREATE TABLE IF NOT EXISTS vps_instances (
            token TEXT PRIMARY KEY,
            vps_id TEXT UNIQUE,
            disk_path TEXT,
            memory INTEGER,
            cpu INTEGER,
            disk INTEGER,
            username TEXT,
            password TEXT,
            created_by TEXT,
            created_at TEXT,
            status TEXT,
            external_ssh_port INTEGER,
            guest_ip TEXT
        )''')
        self.conn.commit()

    def add_vps(self, data):
        cols = ",".join(data.keys())
        placeholders = ",".join("?" for _ in data)
        self.cur.execute(f"INSERT INTO vps_instances ({cols}) VALUES ({placeholders})", tuple(data.values()))
        self.conn.commit()

    def update_vps(self, token, updates):
        set_clause = ",".join(f"{k}=?" for k in updates.keys())
        vals = list(updates.values()) + [token]
        self.cur.execute(f"UPDATE vps_instances SET {set_clause} WHERE token=?", vals)
        self.conn.commit()

    def get_vps_by_token(self, token):
        self.cur.execute("SELECT * FROM vps_instances WHERE token=?", (token,))
        row = self.cur.fetchone()
        if not row: return None
        keys = [d[0] for d in self.cur.description]
        return dict(zip(keys, row))

    def get_used_ports(self):
        self.cur.execute("SELECT external_ssh_port FROM vps_instances WHERE external_ssh_port IS NOT NULL")
        return {r[0] for r in self.cur.fetchall()}

db = DB(DB_FILE)

# ---------- Utility helpers ----------
def generate_token(n=24):
    return ''.join(random.choices(string.ascii_letters+string.digits, k=n))

def generate_vps_id():
    return ''.join(random.choices(string.ascii_uppercase+string.digits, k=10))

def find_free_host_port():
    used = db.get_used_ports()
    while True:
        p = random.randint(PORT_RANGE_START, PORT_RANGE_END)
        if p not in used:
            return p

def ensure_image_download_instructions():
    """
    This does NOT download image automatically in this safe template.
    Instead it returns the recommended command that admin should run once on host
    to download the base image.
    """
    os.makedirs(LIBVIRT_IMG_DIR, exist_ok=True)
    dest = os.path.join(LIBVIRT_IMG_DIR, BASE_IMAGE_NAME)
    cmd = f"sudo mkdir -p {LIBVIRT_IMG_DIR} && sudo wget -O {dest} {BASE_IMAGE_URL}"
    return dest, cmd

def build_cloud_init_userdata(vm_name, root_password):
    """Return string content for user-data (cloud-init). Admin will write it to files when running script."""
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

def build_create_vm_script(vm_name, memory_gb, cpu_cores, disk_gb, root_password, base_image_path, seed_iso_path, vm_disk_path, host_port):
    """
    Return a shell script (string) that admin can run as root to:
      - create qcow2 overlay
      - create cloud-init ISO using cloud-localds
      - run virt-install (import)
      - (optionally) print virsh net-dhcp-leases to fetch guest IP
      - add iptables rules for port forwarding
    NOTE: This script is provided for manual run only. Bot will NOT execute it.
    """
    # Use cloud-localds to create iso from user-data/meta-data
    user_data = build_cloud_init_userdata(vm_name, root_password)
    # The script writes the user-data/meta-data files and runs commands.
    script = f"""#!/bin/bash
set -euo pipefail
echo "[hostforge] Starting VM creation for {vm_name}"

BASE_IMG="{base_image_path}"
VM_DISK="{vm_disk_path}"
SEED_ISO="{seed_iso_path}"
VM_NAME="{vm_name}"
MEM_MB=$(( {memory_gb} * 1024 ))
CPU_CORES={cpu_cores}
DISK_GB={disk_gb}
HOST_PORT={host_port}

# write user-data and meta-data
TMPDIR="/tmp/hostforge_cloud_{vm_name}"
mkdir -p "$TMPDIR"
cat > "$TMPDIR/user-data" <<'USERDATA'
{user_data}
USERDATA

cat > "$TMPDIR/meta-data" <<'METADATA'
instance-id: {vm_name}
local-hostname: {vm_name}
METADATA

echo "[hostforge] Creating seed ISO..."
cloud-localds "$SEED_ISO" "$TMPDIR/user-data" "$TMPDIR/meta-data"

echo "[hostforge] Creating qcow2 overlay..."
qemu-img create -f qcow2 -b "$BASE_IMG" "$VM_DISK" "${{DISK_GB}}G"

echo "[hostforge] Running virt-install (import)..."
virt-install --name "$VM_NAME" --memory "$MEM_MB" --vcpus "$CPU_CORES" \\
  --disk path="$VM_DISK",format=qcow2 --import --os-variant ubuntu22.04 \\
  --network network=default --graphics none --noautoconsole --disk path="$SEED_ISO",device=cdrom

echo "[hostforge] VM created. Waiting a bit for network..."
sleep 5

echo "[hostforge] Checking DHCP leases (may take some time)..."
virsh net-dhcp-leases default || true

# Add iptables forwarding from host port to new guest - NOTE: guest IP may change; check virsh leases and update if needed
GUEST_IP=""
echo "Please run: virsh domiflist $VM_NAME  # and virsh net-dhcp-leases default to find guest IP"
echo "Once you have guest IP, you can add iptables NAT rule (example):"
echo "sudo iptables -t nat -A PREROUTING -p tcp --dport $HOST_PORT -j DNAT --to-destination <GUEST_IP>:22"
echo "sudo iptables -t nat -A POSTROUTING -p tcp -d <GUEST_IP> --dport 22 -j MASQUERADE"

echo "[hostforge] DONE. If you want the script to attempt adding rules automatically, edit it with known guest IP."
"""
    return script

# ---------- Discord bot setup ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='/', intents=intents)

def is_admin_ctx(ctx):
    try:
        uid = ctx.author.id if hasattr(ctx, "author") else ctx.user.id
        return uid in ADMIN_IDS
    except:
        return False

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (id: {bot.user.id})")

# ---------- create_vps command (admin only) ----------
@bot.hybrid_command(name='create_vps', description='Create a new VPS (Admin only)')
@app_commands.describe(
    memory="Memory in GB",
    cpu="CPU cores",
    disk="Disk space in GB",
    owner="User who will own the VPS"
)
async def create_vps_command(ctx, memory: int, cpu: int, disk: int, owner: discord.Member):
    if not is_admin_ctx(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        if memory < 1 or memory > 512:
            await ctx.send("❌ Memory must be between 1GB and 512GB", ephemeral=True)
            return
        if cpu < 1 or cpu > 32:
            await ctx.send("❌ CPU cores must be between 1 and 32", ephemeral=True)
            return
        if disk < 10 or disk > 2000:
            await ctx.send("❌ Disk space must be between 10GB and 2000GB", ephemeral=True)
            return

        status_msg = await ctx.send("🚀 Preparing Dedicated KVM VPS (script will be created).")

        # Create identifiers
        vps_id = generate_vps_id()
        vm_name = f"hostforge-{vps_id.lower()}"
        token = generate_token()
        username = "root"
        password = "root"  # you can randomize here if you want
        external_port = find_free_host_port()

        # Provide instructions to download base image (admin must run once)
        base_image_path = os.path.join(LIBVIRT_IMG_DIR, BASE_IMAGE_NAME)
        dest, download_cmd = ensure_image_download_instructions()

        # Prepare paths for vm disk and seed iso
        vm_disk_path = os.path.join(LIBVIRT_IMG_DIR, f"{vm_name}.qcow2")
        seed_iso_path = os.path.join(LIBVIRT_IMG_DIR, f"{vm_name}-seed.iso")
        script_path = f"/tmp/create_{vm_name}.sh"

        # Build the shell script (do NOT execute it here)
        script_content = build_create_vm_script(vm_name, memory, cpu, disk, password, base_image_path, seed_iso_path, vm_disk_path, external_port)

        # Write the script to /tmp (so admin can SCP or check it)
        with open(script_path, "w") as f:
            f.write(script_content)
        os.chmod(script_path, 0o700)

        # Save record to DB with status 'pending-manual'
        created_at = str(datetime.datetime.now())
        db.add_vps({
            "token": token,
            "vps_id": vps_id,
            "disk_path": vm_disk_path,
            "memory": memory,
            "cpu": cpu,
            "disk": disk,
            "username": username,
            "password": password,
            "created_by": str(owner.id),
            "created_at": created_at,
            "status": "pending-manual",
            "external_ssh_port": external_port,
            "guest_ip": None
        })

        # DM owner/admin with script + instructions (sensitive info via DM)
        try:
            dm_text = (
                f"🎉 HostForge VPS prepared (manual step required).\n\n"
                f"VPS ID: {vps_id}\n"
                f"VM Name: {vm_name}\n"
                f"Memory: {memory}GB, CPU: {cpu}, Disk: {disk}GB\n"
                f"Username: root\n"
                f"Password: ||{password}||\n\n"
                "Follow these steps on the HOST (run as root or with sudo):\n\n"
                f"1) If you haven't downloaded the base image, run once:\n   {download_cmd}\n\n"
                f"2) Run the prepared script (runs qemu-img, cloud-localds, virt-install):\n   sudo bash {script_path}\n\n"
                "3) After VM is created, find guest IP:\n   sudo virsh domiflist {vm_name}\n   sudo virsh net-dhcp-leases default\n\n"
                f"4) Add iptables DNAT rules to forward host port {external_port} -> guest_ip:22\n   (example):\n   sudo iptables -t nat -A PREROUTING -p tcp --dport {external_port} -j DNAT --to-destination <GUEST_IP>:22\n   sudo iptables -t nat -A POSTROUTING -p tcp -d <GUEST_IP> --dport 22 -j MASQUERADE\n\n"
                "5) Once done, update the DB entry to set status='running' and guest_ip to the found IP,\n"
                "   or run the bot's /sync_vms (if implemented) to detect and update.\n\n"
                "-----\n"
                "The script is at: " + script_path + "\n"
                "If you want, you can run it manually or inspect it before running.\n"
            )
            await owner.send(dm_text)
            await status_msg.edit(content=f"✅ Prepared script for {owner.mention}. Script saved at `{script_path}`. DM sent with instructions.")
        except discord.Forbidden:
            await status_msg.edit(content=f"❌ Could not DM the owner ({owner.mention}). Script saved at `{script_path}` on host.")
    except Exception as e:
        logger.exception("Error preparing KVM VPS")
        await ctx.send(f"❌ Error: {e}")

# ---------- optional helper command for admins to get the script content ----------
@bot.hybrid_command(name='get_vm_script', description='Get prepared VM creation script (admin only)')
@app_commands.describe(vps_id="VPS ID")
async def get_vm_script(ctx, vps_id: str):
    if not is_admin_ctx(ctx):
        await ctx.send("❌ Admins only", ephemeral=True)
        return
    # fetch by vps_id
    cur = db.cur
    cur.execute("SELECT token, disk_path FROM vps_instances WHERE vps_id=?", (vps_id,))
    r = cur.fetchone()
    if not r:
        await ctx.send("❌ Not found", ephemeral=True)
        return
    token = r[0]
    vm_disk = r[1]
    vm_name = f"hostforge-{vps_id.lower()}"
    script_path = f"/tmp/create_{vm_name}.sh"
    if os.path.exists(script_path):
        with open(script_path, "r") as f:
            content = f.read()
        # send as file in DM
        try:
            await ctx.author.send(file=discord.File(script_path))
            await ctx.send("✅ Script sent to your DMs.", ephemeral=True)
        except discord.Forbidden:
            await ctx.send("❌ Can't DM you, but script exists on host at: " + script_path, ephemeral=True)
    else:
        await ctx.send("⚠️ Script file not found on host. Maybe it was removed.", ephemeral=True)

# ---------- run the bot ----------
if __name__ == "__main__":
    if not TOKEN:
        print("DISCORD_TOKEN not set in .env")
        exit(1)
    bot.run(TOKEN)
