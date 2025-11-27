import discord
from discord.ext import commands, tasks
from discord import ui, app_commands
import os
import random
import string
import json
import subprocess
from dotenv import load_dotenv
import asyncio
import datetime
import docker
import time
import logging
import traceback
import aiohttp
import socket
import re
import psutil
import platform
import shutil
from typing import Optional, Literal
import sqlite3
import pickle
import base64
import threading
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit
import paramiko

# ------------------------- Logging -------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('snowcloud_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('SnowCloudBot')

# ------------------------- Environment -------------------------
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
HOST_IP = os.getenv('HOST_IP') or None
ADMIN_IDS = {int(id_) for id_ in os.getenv('ADMIN_IDS', '').split(',') if id_.strip()}
# OWNER_ID should be set in .env as number. If not set, fall back to first ADMIN_IDS or None
OWNER_ID = int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else (next(iter(ADMIN_IDS)) if ADMIN_IDS else None)
THUMBNAIL_URL = os.getenv('THUMBNAIL_URL', '')
BRAND_NAME = os.getenv('BRAND_NAME', 'SnowCloud')
WATERMARK = os.getenv('WATERMARK', f" {BRAND_NAME} Service")
WELCOME_MESSAGE = os.getenv('WELCOME_MESSAGE', f"Welcome To {BRAND_NAME}! Get Started With Us!")
MAX_VPS_PER_USER = int(os.getenv('MAX_VPS_PER_USER', '3'))
DEFAULT_OS_IMAGE = os.getenv('DEFAULT_OS_IMAGE', 'ubuntu:22.04')
DOCKER_NETWORK = os.getenv('DOCKER_NETWORK', 'bridge')
MAX_CONTAINERS = int(os.getenv('MAX_CONTAINERS', '100'))
DB_FILE = os.getenv('DB_FILE', 'snowcloud.db')
BACKUP_FILE = os.getenv('BACKUP_FILE', 'snowcloud_backup.pkl')
PORT_RANGE_START = int(os.getenv('PORT_RANGE_START', '20000'))
PORT_RANGE_END = int(os.getenv('PORT_RANGE_END', '30000'))

# Presence rotation (comma separated in .env) or default list
PRESENCE_ROTATION = [s.strip() for s in os.getenv('PRESENCE_ROTATION', f"{BRAND_NAME} VPS,Powering {BRAND_NAME},Made by gg").split(',') if s.strip()]
PRESENCE_INTERVAL = int(os.getenv('PRESENCE_INTERVAL', '15'))  # seconds

# ------------------------- Constants & Patterns -------------------------
MINER_PATTERNS = [
    'xmrig', 'ethminer', 'cgminer', 'sgminer', 'bfgminer',
    'minerd', 'cpuminer', 'cryptonight', 'stratum', 'pool'
]

DOCKERFILE_TEMPLATE = """
FROM {base_image}

# Prevent prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install systemd, sudo, SSH, Docker and other essential packages
RUN apt-get update && \\
    apt-get install -y systemd systemd-sysv dbus sudo \\
                       curl gnupg2 apt-transport-https ca-certificates \\
                       software-properties-common \\
                       docker.io openssh-server tmate && \\
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Root password
RUN echo "root:root" | chpasswd

# Enable SSH login
RUN mkdir /var/run/sshd && \\
    sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config && \\
    sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config

# Enable services on boot
RUN systemctl enable ssh && \\
    systemctl enable docker

# Branding customization
RUN echo '{welcome_message}' > /etc/motd && \\
    echo 'echo "{welcome_message}"' >> /root/.bashrc && \\
    echo '{watermark}' > /etc/machine-info && \\
    echo '{hostname}' > /etc/hostname

# Install additional useful packages
RUN apt-get update && \\
    apt-get install -y neofetch htop nano vim wget git tmux net-tools dnsutils iputils-ping && \\
    apt-get clean && \\
    rm -rf /var/lib/apt/lists/*

# Fix systemd inside container
STOPSIGNAL SIGRTMIN+3

# Boot into systemd (like a VM)
CMD ["/sbin/init"]
"""

# ------------------------- Database -------------------------
class Database:
    def __init__(self, db_file):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()
        self._initialize_settings()

    def _create_tables(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS vps_instances (
                token TEXT PRIMARY KEY,
                vps_id TEXT UNIQUE,
                container_id TEXT,
                memory INTEGER,
                cpu INTEGER,
                disk INTEGER,
                username TEXT,
                password TEXT,
                root_password TEXT,
                created_by TEXT,
                created_at TEXT,
                tmate_session TEXT,
                watermark TEXT,
                os_image TEXT,
                restart_count INTEGER DEFAULT 0,
                last_restart TEXT,
                status TEXT DEFAULT 'running',
                use_custom_image BOOLEAN DEFAULT 1,
                external_ssh_port INTEGER
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS usage_stats (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id TEXT PRIMARY KEY
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_users (
                user_id TEXT PRIMARY KEY
            )
        ''')
        self.conn.commit()

    def _initialize_settings(self):
        defaults = {
            'max_containers': str(MAX_CONTAINERS),
            'max_vps_per_user': str(MAX_VPS_PER_USER)
        }
        for key, value in defaults.items():
            self.cursor.execute('INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)', (key, value))
        self.cursor.execute('SELECT user_id FROM admin_users')
        for row in self.cursor.fetchall():
            try:
                ADMIN_IDS.add(int(row[0]))
            except:
                continue
        self.conn.commit()

    def get_setting(self, key, default=None):
        self.cursor.execute('SELECT value FROM system_settings WHERE key = ?', (key,))
        result = self.cursor.fetchone()
        return int(result[0]) if result else default

    def set_setting(self, key, value):
        self.cursor.execute('INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)', (key, str(value)))
        self.conn.commit()

    def get_stat(self, key, default=0):
        self.cursor.execute('SELECT value FROM usage_stats WHERE key = ?', (key,))
        result = self.cursor.fetchone()
        return result[0] if result else default

    def increment_stat(self, key, amount=1):
        current = self.get_stat(key)
        self.cursor.execute('INSERT OR REPLACE INTO usage_stats (key, value) VALUES (?, ?)', (key, current + amount))
        self.conn.commit()

    def get_vps_by_id(self, vps_id):
        self.cursor.execute('SELECT * FROM vps_instances WHERE vps_id = ?', (vps_id,))
        row = self.cursor.fetchone()
        if not row:
            return None, None
        columns = [desc[0] for desc in self.cursor.description]
        vps = dict(zip(columns, row))
        return vps['token'], vps

    def get_vps_by_token(self, token):
        self.cursor.execute('SELECT * FROM vps_instances WHERE token = ?', (token,))
        row = self.cursor.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in self.cursor.description]
        return dict(zip(columns, row))

    def get_user_vps_count(self, user_id):
        self.cursor.execute('SELECT COUNT(*) FROM vps_instances WHERE created_by = ?', (str(user_id),))
        return self.cursor.fetchone()[0]

    def get_user_vps(self, user_id):
        self.cursor.execute('SELECT * FROM vps_instances WHERE created_by = ?', (str(user_id),))
        columns = [desc[0] for desc in self.cursor.description]
        return [dict(zip(columns, row)) for row in self.cursor.fetchall()]

    def get_all_vps(self):
        self.cursor.execute('SELECT * FROM vps_instances')
        columns = [desc[0] for desc in self.cursor.description]
        return {row[0]: dict(zip(columns, row)) for row in self.cursor.fetchall()]

    def add_vps(self, vps_data):
        columns = ', '.join(vps_data.keys())
        placeholders = ', '.join('?' for _ in vps_data)
        self.cursor.execute(f'INSERT INTO vps_instances ({columns}) VALUES ({placeholders})', tuple(vps_data.values()))
        self.conn.commit()
        self.increment_stat('total_vps_created')

    def remove_vps(self, token):
        self.cursor.execute('DELETE FROM vps_instances WHERE token = ?', (token,))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def update_vps(self, token, updates):
        set_clause = ', '.join(f'{k} = ?' for k in updates)
        values = list(updates.values()) + [token]
        self.cursor.execute(f'UPDATE vps_instances SET {set_clause} WHERE token = ?', values)
        self.conn.commit()
        return self.cursor.rowcount > 0

    def is_user_banned(self, user_id):
        self.cursor.execute('SELECT 1 FROM banned_users WHERE user_id = ?', (str(user_id),))
        return self.cursor.fetchone() is not None

    def ban_user(self, user_id):
        self.cursor.execute('INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)', (str(user_id),))
        self.conn.commit()

    def unban_user(self, user_id):
        self.cursor.execute('DELETE FROM banned_users WHERE user_id = ?', (str(user_id),))
        self.conn.commit()

    def get_banned_users(self):
        self.cursor.execute('SELECT user_id FROM banned_users')
        return [row[0] for row in self.cursor.fetchall()]

    def add_admin(self, user_id):
        self.cursor.execute('INSERT OR IGNORE INTO admin_users (user_id) VALUES (?)', (str(user_id),))
        self.conn.commit()
        try:
            ADMIN_IDS.add(int(user_id))
        except:
            pass

    def remove_admin(self, user_id):
        self.cursor.execute('DELETE FROM admin_users WHERE user_id = ?', (str(user_id),))
        self.conn.commit()
        try:
            if int(user_id) in ADMIN_IDS:
                ADMIN_IDS.remove(int(user_id))
        except:
            pass

    def get_admins(self):
        self.cursor.execute('SELECT user_id FROM admin_users')
        return [row[0] for row in self.cursor.fetchall()]

    def get_used_ports(self):
        self.cursor.execute('SELECT external_ssh_port FROM vps_instances WHERE external_ssh_port IS NOT NULL')
        return {row[0] for row in self.cursor.fetchall()}

    def backup_data(self):
        data = {
            'vps_instances': self.get_all_vps(),
            'usage_stats': {},
            'system_settings': {},
            'banned_users': self.get_banned_users(),
            'admin_users': self.get_admins()
        }
        self.cursor.execute('SELECT * FROM usage_stats')
        for row in self.cursor.fetchall():
            data['usage_stats'][row[0]] = row[1]
        self.cursor.execute('SELECT * FROM system_settings')
        for row in self.cursor.fetchall():
            data['system_settings'][row[0]] = row[1]
        with open(BACKUP_FILE, 'wb') as f:
            pickle.dump(data, f)
        return True

    def restore_data(self):
        if not os.path.exists(BACKUP_FILE):
            return False
        try:
            with open(BACKUP_FILE, 'rb') as f:
                data = pickle.load(f)
            self.cursor.execute('DELETE FROM vps_instances')
            self.cursor.execute('DELETE FROM usage_stats')
            self.cursor.execute('DELETE FROM system_settings')
            self.cursor.execute('DELETE FROM banned_users')
            self.cursor.execute('DELETE FROM admin_users')
            for token, vps in data['vps_instances'].items():
                columns = ', '.join(vps.keys())
                placeholders = ', '.join('?' for _ in vps)
                self.cursor.execute(f'INSERT INTO vps_instances ({columns}) VALUES ({placeholders})', tuple(vps.values()))
            for key, value in data['usage_stats'].items():
                self.cursor.execute('INSERT INTO usage_stats (key, value) VALUES (?, ?)', (key, value))
            for key, value in data['system_settings'].items():
                self.cursor.execute('INSERT INTO system_settings (key, value) VALUES (?, ?)', (key, value))
            for user_id in data['banned_users']:
                self.cursor.execute('INSERT INTO banned_users (user_id) VALUES (?)', (user_id,))
            for user_id in data['admin_users']:
                self.cursor.execute('INSERT INTO admin_users (user_id) VALUES (?)', (user_id,))
                try:
                    ADMIN_IDS.add(int(user_id))
                except:
                    pass
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error restoring data: {e}")
            return False

    def close(self):
        self.conn.close()

# ------------------------- Bot -------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class SnowCloudBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db = Database(DB_FILE)
        self.session = None
        self.docker_client = None
        self.public_ip = None
        self.system_stats = {}
        self.presence_index = 0

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        try:
            self.docker_client = docker.from_env()
            logger.info("Docker client initialized")
            self.public_ip = HOST_IP or await self.get_public_ip()
            self.loop.create_task(self.update_system_stats())
            self.loop.create_task(self.anti_miner_monitor())
            await self.reconnect_containers()
        except Exception as e:
            logger.error(f"Failed setup_hook: {e}")
            self.docker_client = None

    async def get_public_ip(self):
        try:
            async with self.session.get('https://api.ipify.org') as resp:
                if resp.status == 200:
                    return await resp.text()
        except Exception:
            pass
        return '127.0.0.1'

    async def reconnect_containers(self):
        if not self.docker_client:
            return
        for token, vps in list(self.db.get_all_vps().items()):
            if vps.get('status') == 'running':
                try:
                    container = self.docker_client.containers.get(vps['container_id'])
                    if container.status != 'running':
                        container.start()
                except Exception:
                    self.db.remove_vps(token)

    async def anti_miner_monitor(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                for token, vps in self.db.get_all_vps().items():
                    if vps.get('status') != 'running':
                        continue
                    try:
                        container = self.docker_client.containers.get(vps['container_id'])
                        if container.status != 'running':
                            continue
                        exec_result = container.exec_run("ps aux")
                        output = exec_result.output.decode().lower()
                        for pattern in MINER_PATTERNS:
                            if pattern in output:
                                container.stop()
                                self.db.update_vps(token, {'status': 'suspended'})
                                try:
                                    owner = await self.fetch_user(int(vps['created_by']))
                                    await owner.send(f"⚠️ Your VPS was mining and has been suspended for safety..")
                                except:
                                    pass
                                break
                    except Exception as e:
                        logger.error(f"anti_miner inner error: {e}")
            except Exception as e:
                logger.error(f"anti_miner error: {e}")
            await asyncio.sleep(300)

    async def update_system_stats(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                cpu_percent = psutil.cpu_percent(interval=1)
                mem = psutil.virtual_memory()
                disk = psutil.disk_usage('/')
                net_io = psutil.net_io_counters()
                self.system_stats = {
                    'cpu_usage': cpu_percent,
                    'memory_usage': mem.percent,
                    'memory_used': mem.used / (1024 ** 3),
                    'memory_total': mem.total / (1024 ** 3),
                    'disk_usage': disk.percent,
                    'disk_used': disk.used / (1024 ** 3),
                    'disk_total': disk.total / (1024 ** 3),
                    'network_sent': net_io.bytes_sent / (1024 ** 2),
                    'network_recv': net_io.bytes_recv / (1024 ** 2),
                    'last_updated': time.time()
                }
            except Exception as e:
                logger.error(f"update_system_stats error: {e}")
            await asyncio.sleep(30)

    async def close(self):
        await super().close()
        if self.session:
            await self.session.close()
        if self.docker_client:
            self.docker_client.close()
        self.db.close()

bot = SnowCloudBot(command_prefix='/', intents=intents, help_command=None)

# ------------------------- Utilities -------------------------

def generate_token():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=24))

def generate_vps_id():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))

def get_available_port(db):
    used_ports = db.get_used_ports()
    while True:
        port = random.randint(PORT_RANGE_START, PORT_RANGE_END)
        if port not in used_ports:
            return port

# Helper to make embed with thumbnail and footer
def make_embed(title: str, color: discord.Color = discord.Color.blue()):
    embed = discord.Embed(title=title, color=color)
    if THUMBNAIL_URL:
        embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.set_footer(text='made by gg')
    return embed

# Check admin
def is_admin(ctx):
    try:
        user_id = ctx.user.id if isinstance(ctx, discord.Interaction) else ctx.author.id
        return user_id in ADMIN_IDS or (OWNER_ID and user_id == OWNER_ID)
    except Exception:
        return False

# Check owner-only
def is_owner_id(user_id: int):
    return OWNER_ID and int(user_id) == int(OWNER_ID)

# ------------------------- Presence rotation task -------------------------
@tasks.loop(seconds=PRESENCE_INTERVAL)
async def rotate_presence():
    if not bot.is_ready():
        return
    try:
        idx = bot.presence_index % len(PRESENCE_ROTATION)
        text = PRESENCE_ROTATION[idx]
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=text))
        bot.presence_index += 1
    except Exception as e:
        logger.error(f"rotate_presence error: {e}")

@bot.event
async def on_ready():
    logger.info(f'{bot.user} connected as SnowCloud bot')
    # Start rotating presence
    if not rotate_presence.is_running():
        rotate_presence.start()

# ------------------------- Commands -------------------------
@bot.hybrid_command(name='help', description='Show all commands')
async def show_commands(ctx):
    embed = make_embed(f"{BRAND_NAME} Bot Commands")
    embed.add_field(name='User Commands', value='''/create_vps - Create VPS (Admin)
/list - View your VPS
/connect_vps <token> - Connect to VPS
/vps_stats <vps_id> - VPS stats
''', inline=False)
    if is_admin(ctx):
        embed.add_field(name='Admin Commands', value='''/vps_list - Show all VPS
/add_admin <user> - Admin add karo (Owner se allow karega)
/remove_admin <user> - Admin remove karo (Owner only)
/list_admins - Admin list
''', inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name='add_admin', description='Add a new admin (Owner only)')
@app_commands.describe(user='User to make admin')
async def add_admin(ctx, user: discord.User):
    if not is_owner_id(ctx.author.id):
        await ctx.send('❌ Only the owner can add a new admin..', ephemeral=True)
        return
    bot.db.add_admin(user.id)
    await ctx.send(f'✅ {user.mention} has been added as an admin..', ephemeral=True)

@bot.hybrid_command(name='remove_admin', description='Remove an admin (Owner only)')
@app_commands.describe(user='User to remove from admin')
async def remove_admin(ctx, user: discord.User):
    if not is_owner_id(ctx.author.id):
        await ctx.send('❌ Only the owner can remove an admin..', ephemeral=True)
        return
    bot.db.remove_admin(user.id)
    await ctx.send(f'✅ {user.mention} has been removed from admin..', ephemeral=True)

@bot.hybrid_command(name='list_admins', description='List all admin users')
async def list_admins(ctx):
    if not is_admin(ctx):
        await ctx.send('❌ Only admins can use this..', ephemeral=True)
        return
    admins = bot.db.get_admins()
    text = ''
    for a in admins:
        text += f'<@{a}>\n'
    if OWNER_ID:
        text = f'<@{OWNER_ID}> (owner)\n' + text
    embed = make_embed('Admins')
    embed.description = text or 'No admins found.'
    await ctx.send(embed=embed, ephemeral=True)

# (Note: Other commands like create_vps, list, connect etc. can reuse make_embed and OWNER_ID checks)

# ------------------------- Final run -------------------------
if __name__ == '__main__':
    if not TOKEN:
        logger.error('DISCORD_TOKEN .env me set karein.')
    else:
        bot.run(TOKEN)
