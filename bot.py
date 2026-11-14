import discord
from discord.ext import commands
import asyncio
import subprocess
import json
from datetime import datetime, timedelta
import shlex
import logging
import shutil
import os
from typing import Optional, List, Dict, Any
import threading
import time
import sqlite3
import random
import requests
import secrets
import string
import re
import socket
import ipaddress
import glob

# Load environment variables
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
BOT_NAME = os.getenv('BOT_NAME', 'Bvm-v1')
PREFIX = os.getenv('PREFIX', '!')
ENV_PREFIX = PREFIX   # default from the service file (used by `prefix reset`)
YOUR_SERVER_IP = os.getenv('YOUR_SERVER_IP', '127.0.0.1')

# ---- Public IP detection (used for SSH login info shown to users) ----
_cached_public_ip = None

def get_public_ip() -> str:
    """Fetch the server's public IP via ifconfig.me, caching the result.
    Falls back to YOUR_SERVER_IP env var if the lookup fails."""
    global _cached_public_ip
    if _cached_public_ip:
        return _cached_public_ip
    try:
        resp = requests.get("https://ifconfig.me/ip", timeout=5)
        ip = resp.text.strip()
        if ip:
            _cached_public_ip = ip
            return ip
    except Exception as e:
        logger.warning(f"Failed to fetch public IP from ifconfig.me: {e}")
    return YOUR_SERVER_IP

# ---- Automatic IPv4 check (no on/off command) ----
# SSH access is always given through the Pinggy tunnel. When a VPS is created / started / reconnected the bot checks
# whether the node has a public IPv4: with IPv4 the VPS is IPv4 (its Pinggy tunnel runs over IPv4), without IPv4 the VPS is
# non-IPv4 and the notice "This VPS is not IPv4" is shown. The address given to the user is always the normal Pinggy hostname.
_cached_public_ipv4 = None
_ipv4_fail_ts = 0.0

def _is_public_ipv4(value) -> bool:
    try:
        ip = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return False
    return ip.version == 4 and ip.is_global

def get_public_ipv4_sync() -> Optional[str]:
    """The server's public IPv4 (asks IPv4-only services so an IPv6 answer can never sneak in). None if it has no IPv4."""
    global _cached_public_ipv4, _ipv4_fail_ts
    if _cached_public_ipv4:
        return _cached_public_ipv4
    if time.time() - _ipv4_fail_ts < 300 and not _is_public_ipv4(YOUR_SERVER_IP):
        return None   # looked up a moment ago and found no IPv4 - do not wait for the timeouts again
    for url in ("https://api.ipify.org", "https://ipv4.icanhazip.com", "https://v4.ident.me"):
        try:
            ip = requests.get(url, timeout=5).text.strip()
            if _is_public_ipv4(ip):
                _cached_public_ipv4 = ip
                return ip
        except Exception as e:
            logger.warning(f"IPv4 lookup via {url} failed: {e}")
    if _is_public_ipv4(YOUR_SERVER_IP):   # manual override from the service file / env
        return YOUR_SERVER_IP
    _ipv4_fail_ts = time.time()
    return None

def node_ipv4_sync(node: Dict) -> Optional[str]:
    """Public IPv4 of a node: the bot server's IPv4 for the local node, the node host's IPv4 (literal or A record) otherwise."""
    if node.get('is_local'):
        return get_public_ipv4_sync()
    from urllib.parse import urlparse
    host = urlparse(node.get('url') or '').hostname
    if not host:
        return None
    if _is_public_ipv4(host):
        return host
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            if _is_public_ipv4(info[4][0]):
                return info[4][0]
    except Exception:
        pass
    return None

async def node_has_ipv4(node_id: int) -> bool:
    node = get_node(node_id)
    if not node:
        return False
    return bool(await asyncio.to_thread(node_ipv4_sync, node))

async def check_node_ipv4(node_id: int, for_start: bool = False):
    """(ok, notice_text). (False, notice) when the node has no public IPv4.
    The notice is only SHOWN - nothing is blocked: the VPS is still created/started and gets a non-IPv4 Pinggy address.
    for_start=True is used when a VPS is switched on (start/restart)."""
    node = get_node(node_id)
    if not node:
        return False, f"Node `{node_id}` was not found."
    ip = await asyncio.to_thread(node_ipv4_sync, node)
    if ip:
        return True, None
    if for_start:
        return False, (f"❌ **This VPS is not IPv4.**\nNode **{node['name']}** has no public IPv4 address.\n"
                       f"The VPS is started anyway and connects through a **non-IPv4 Pinggy address**.")
    return False, (f"❌ **This VPS is not IPv4.**\nNode **{node['name']}** has no public IPv4 address.\n"
                   f"The VPS is still created and connects through a **non-IPv4 Pinggy address**.")
_raw_main_admin_ids = os.getenv('MAIN_ADMIN_ID', '1405866008127864852')
MAIN_ADMIN_IDS_ENV = [uid.strip() for uid in _raw_main_admin_ids.split(',') if uid.strip()]
MAIN_ADMIN_ID = int(MAIN_ADMIN_IDS_ENV[0])  # kept for backward-compat display purposes
VPS_USER_ROLE_ID = int(os.getenv('VPS_USER_ROLE_ID', '1210291131301101618'))
DEFAULT_STORAGE_POOL = os.getenv('DEFAULT_STORAGE_POOL', 'default')
BOT_VERSION = os.getenv('BOT_VERSION', '1.0-PRO')
BOT_DEVELOPER = os.getenv('BOT_DEVELOPER', 'MineFlareX')
BOT_IMAGE_URL = os.getenv('BOT_IMAGE_URL', 'https://raw.githubusercontent.com/mineflarex-collab/Bvm-v1bot/main/vps.png')

# OS Options for VPS Creation and Reinstall
OS_OPTIONS = [
    {"label": "Ubuntu 20.04 LTS", "value": "ubuntu:20.04"},
    {"label": "Ubuntu 22.04 LTS", "value": "ubuntu:22.04"},
    {"label": "Ubuntu 24.04 LTS", "value": "ubuntu:24.04"},
    {"label": "Debian 10 (Buster)", "value": "images:debian/10"},
    {"label": "Debian 11 (Bullseye)", "value": "images:debian/11"},
    {"label": "Debian 12 (Bookworm)", "value": "images:debian/12"},
    {"label": "Debian 13 (Trixie)", "value": "images:debian/13"},
]

# Configure logging to file and console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(f'{BOT_NAME.lower()}_vps_bot')

# Database setup
def get_db():
    conn = sqlite3.connect('vps.db')
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id TEXT PRIMARY KEY
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS main_admins (
        user_id TEXT PRIMARY KEY
    )''')
    for uid in MAIN_ADMIN_IDS_ENV:
        cur.execute('INSERT OR IGNORE INTO main_admins (user_id) VALUES (?)', (uid,))
    cur.execute('''CREATE TABLE IF NOT EXISTS nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        location TEXT,
        total_vps INTEGER,
        tags TEXT DEFAULT '[]',
        api_key TEXT,
        url TEXT,
        is_local INTEGER DEFAULT 0
    )''')
    # Add local node if not exists
    cur.execute('SELECT COUNT(*) FROM nodes WHERE is_local = 1')
    if cur.fetchone()[0] == 0:
        cur.execute('INSERT INTO nodes (name, location, total_vps, tags, api_key, url, is_local) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    ('Local Node', 'Local', 100, '[]', None, None, 1))  # Default capacity 100
    cur.execute('''CREATE TABLE IF NOT EXISTS vps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        node_id INTEGER NOT NULL DEFAULT 1,
        container_name TEXT UNIQUE NOT NULL,
        ram TEXT NOT NULL,
        cpu TEXT NOT NULL,
        storage TEXT NOT NULL,
        config TEXT NOT NULL,
        os_version TEXT DEFAULT 'ubuntu:22.04',
        status TEXT DEFAULT 'stopped',
        suspended INTEGER DEFAULT 0,
        whitelisted INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        shared_with TEXT DEFAULT '[]',
        suspension_history TEXT DEFAULT '[]',
        root_password TEXT DEFAULT ''
    )''')
    # Migrations
    cur.execute('PRAGMA table_info(vps)')
    info = cur.fetchall()
    columns = [col[1] for col in info]
    if 'os_version' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN os_version TEXT DEFAULT 'ubuntu:22.04'")
    if 'node_id' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN node_id INTEGER DEFAULT 1")
    if 'root_password' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN root_password TEXT DEFAULT ''")
    if 'expires_at' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN expires_at TEXT DEFAULT NULL")
    if 'expiry_notified' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN expiry_notified TEXT DEFAULT '[]'")
    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''')
    settings_init = [
        ('cpu_threshold', '90'),
        ('ram_threshold', '90'),
        ('default_expiry_days', '0'),     # 0 = new VPS never expire
        ('expiry_reminders', '1'),
        ('auto_suspend', '0'),            # usage guard (mining / abuse) - OFF by default, the usage alert only sends a DM
        ('auto_suspend_minutes', '5'),
    ]
    for key, value in settings_init:
        cur.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
    cur.execute('''CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        admin_id TEXT NOT NULL,
        admin_name TEXT,
        action TEXT NOT NULL,
        target TEXT,
        details TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS port_allocations (
        user_id TEXT PRIMARY KEY,
        allocated_ports INTEGER DEFAULT 0
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS port_forwards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        vps_container TEXT NOT NULL,
        vps_port INTEGER NOT NULL,
        host_port INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )''')
    conn.commit()
    conn.close()

def get_setting(key: str, default: Any = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

def get_nodes() -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM nodes')
    rows = cur.fetchall()
    conn.close()
    nodes = [dict(row) for row in rows]
    for node in nodes:
        node['tags'] = json.loads(node['tags'])
    return nodes

def get_node(node_id: int) -> Optional[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM nodes WHERE id = ?', (node_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        node = dict(row)
        node['tags'] = json.loads(node['tags'])
        return node
    return None

def get_current_vps_count(node_id: int) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM vps WHERE node_id = ?', (node_id,))
    count = cur.fetchone()[0]
    conn.close()
    return count

def get_vps_data() -> Dict[str, List[Dict[str, Any]]]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM vps')
    rows = cur.fetchall()
    conn.close()
    data = {}
    for row in rows:
        user_id = row['user_id']
        if user_id not in data:
            data[user_id] = []
        vps = dict(row)
        vps['shared_with'] = json.loads(vps['shared_with'])
        vps['suspension_history'] = json.loads(vps['suspension_history'])
        try:
            vps['expiry_notified'] = json.loads(vps.get('expiry_notified') or '[]')
        except Exception:
            vps['expiry_notified'] = []
        vps['suspended'] = bool(vps['suspended'])
        vps['whitelisted'] = bool(vps['whitelisted'])
        vps['os_version'] = vps.get('os_version', 'ubuntu:22.04')
        data[user_id].append(vps)
    return data

def get_admins() -> List[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM admins')
    rows = cur.fetchall()
    conn.close()
    return [row['user_id'] for row in rows]

def get_main_admins() -> List[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM main_admins')
    rows = cur.fetchall()
    conn.close()
    ids = [row['user_id'] for row in rows]
    return ids if ids else [str(MAIN_ADMIN_ID)]

def save_main_admins():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM main_admins')
    for uid in main_admin_ids:
        cur.execute('INSERT INTO main_admins (user_id) VALUES (?)', (uid,))
    conn.commit()
    conn.close()

def save_vps_data():
    conn = get_db()
    cur = conn.cursor()
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            shared_json = json.dumps(vps['shared_with'])
            history_json = json.dumps(vps['suspension_history'])
            suspended_int = 1 if vps['suspended'] else 0
            whitelisted_int = 1 if vps.get('whitelisted', False) else 0
            os_ver = vps.get('os_version', 'ubuntu:22.04')
            created_at = vps.get('created_at', datetime.now().isoformat())
            node_id = vps.get('node_id', 1)
            root_password = vps.get('root_password', '')
            expires_at = vps.get('expires_at')
            expiry_notified = json.dumps(vps.get('expiry_notified', []))
            if 'id' not in vps or vps['id'] is None:
                cur.execute('''INSERT INTO vps (user_id, node_id, container_name, ram, cpu, storage, config, os_version, status, suspended, whitelisted, created_at, shared_with, suspension_history, root_password, expires_at, expiry_notified)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (user_id, node_id, vps['container_name'], vps['ram'], vps['cpu'], vps['storage'], vps['config'],
                             os_ver, vps['status'], suspended_int, whitelisted_int,
                             created_at, shared_json, history_json, root_password, expires_at, expiry_notified))
                vps['id'] = cur.lastrowid
            else:
                cur.execute('''UPDATE vps SET user_id = ?, node_id = ?, container_name = ?, ram = ?, cpu = ?, storage = ?, config = ?, os_version = ?, status = ?, suspended = ?, whitelisted = ?, shared_with = ?, suspension_history = ?, root_password = ?, expires_at = ?, expiry_notified = ?
                               WHERE id = ?''',
                            (user_id, node_id, vps['container_name'], vps['ram'], vps['cpu'], vps['storage'], vps['config'],
                             os_ver, vps['status'], suspended_int, whitelisted_int, shared_json, history_json, root_password,
                             expires_at, expiry_notified, vps['id']))
    conn.commit()
    conn.close()

def save_admin_data():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM admins')
    for admin_id in admin_data['admins']:
        cur.execute('INSERT INTO admins (user_id) VALUES (?)', (admin_id,))
    conn.commit()
    conn.close()

# Port forwarding functions
def get_user_allocation(user_id: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT allocated_ports FROM port_allocations WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def get_user_used_ports(user_id: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM port_forwards WHERE user_id = ? AND vps_port != 22', (user_id,))   # the automatic SSH forward (port 22) is free
    row = cur.fetchone()
    conn.close()
    return row[0]

def allocate_ports(user_id: str, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO port_allocations (user_id, allocated_ports) VALUES (?, COALESCE((SELECT allocated_ports FROM port_allocations WHERE user_id = ?), 0) + ?)', (user_id, user_id, amount))
    conn.commit()
    conn.close()

def deallocate_ports(user_id: str, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE port_allocations SET allocated_ports = MAX(0, allocated_ports - ?) WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()

def _used_host_ports(node_id: int) -> set:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT host_port FROM port_forwards WHERE vps_container IN (SELECT container_name FROM vps WHERE node_id = ?)', (node_id,))
    used_ports = set(row[0] for row in cur.fetchall())
    conn.close()
    return used_ports

def _host_port_in_use_locally(port: int) -> bool:
    """True if something on THIS machine already listens on the port (TCP or UDP). Only meaningful for the local node."""
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        sock = socket.socket(socket.AF_INET, kind)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return True
        finally:
            sock.close()
    return False

def get_available_host_port(node_id: int) -> Optional[int]:
    used_ports = _used_host_ports(node_id)
    node = get_node(node_id)
    is_local = bool(node and node.get('is_local'))
    for _ in range(100):
        port = random.randint(20000, 50000)
        if port in used_ports:
            continue
        if is_local and _host_port_in_use_locally(port):
            continue
        return port
    return None

async def create_port_forward(user_id: str, container: str, vps_port: int, node_id: int, host_port: Optional[int] = None, internal: bool = False) -> Optional[int]:
    """Forwards TCP+UDP host_port -> VPS vps_port. host_port=None picks a random free port (20000-50000).
    A fixed host_port (e.g. the same as vps_port, which Pterodactyl Wings / game servers need) raises ValueError
    with a readable message if it cannot be used."""
    if vps_port == 22 and not internal:
        conn = get_db()
        has_ssh = conn.execute('SELECT 1 FROM port_forwards WHERE vps_container = ? AND vps_port = 22', (container,)).fetchone()
        conn.close()
        if has_ssh:
            raise ValueError("Port 22 (SSH) is already forwarded automatically - see your SSH login in the manage panel.")
    if host_port is None:
        host_port = get_available_host_port(node_id)
        if not host_port:
            return None
    else:
        if host_port < 1024 or host_port > 65535:
            raise ValueError("Host port must be between 1024 and 65535.")
        if host_port in _used_host_ports(node_id):
            raise ValueError(f"Host port {host_port} is already forwarded to another VPS on this node.")
        node = get_node(node_id)
        if node and node.get('is_local') and await asyncio.to_thread(_host_port_in_use_locally, host_port):
            raise ValueError(f"Host port {host_port} is already used by another program on the server.")
    tcp_added = False
    try:
        await execute_lxc(container, f"config device add {container} tcp_proxy_{host_port} proxy listen=tcp:0.0.0.0:{host_port} connect=tcp:127.0.0.1:{vps_port}", node_id=node_id)
        tcp_added = True
        await execute_lxc(container, f"config device add {container} udp_proxy_{host_port} proxy listen=udp:0.0.0.0:{host_port} connect=udp:127.0.0.1:{vps_port}", node_id=node_id)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO port_forwards (user_id, vps_container, vps_port, host_port, created_at) VALUES (?, ?, ?, ?, ?)',
                    (user_id, container, vps_port, host_port, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return host_port
    except Exception as e:
        logger.error(f"Failed to create port forward: {e}")
        if tcp_added:
            try:
                await execute_lxc(container, f"config device remove {container} tcp_proxy_{host_port}", node_id=node_id)
            except Exception:
                pass
        return None

async def remove_port_forward(forward_id: int, is_admin: bool = False, requester_id: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """Removes a port forward. Normal users (is_admin=False) can only remove
    their own forwards: pass requester_id to enforce ownership."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id, vps_container, host_port FROM port_forwards WHERE id = ?', (forward_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, None
    user_id, container, host_port = row
    if not is_admin and str(user_id) != str(requester_id):
        conn.close()
        return False, None
    node_id = find_node_id_for_container(container)
    try:
        await execute_lxc(container, f"config device remove {container} tcp_proxy_{host_port}", node_id=node_id)
        await execute_lxc(container, f"config device remove {container} udp_proxy_{host_port}", node_id=node_id)
        cur.execute('DELETE FROM port_forwards WHERE id = ?', (forward_id,))
        conn.commit()
        conn.close()
        return True, user_id
    except Exception as e:
        logger.error(f"Failed to remove port forward {forward_id}: {e}")
        conn.close()
        return False, None

def get_user_forwards(user_id: str) -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM port_forwards WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]

async def recreate_port_forwards(container_name: str) -> int:
    node_id = find_node_id_for_container(container_name)
    readded_count = 0
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT vps_port, host_port FROM port_forwards WHERE vps_container = ?', (container_name,))
    rows = cur.fetchall()
    for row in rows:
        vps_port = row['vps_port']
        host_port = row['host_port']
        try:
            await execute_lxc(container_name, f"config device add {container_name} tcp_proxy_{host_port} proxy listen=tcp:0.0.0.0:{host_port} connect=tcp:127.0.0.1:{vps_port}", node_id=node_id)
            await execute_lxc(container_name, f"config device add {container_name} udp_proxy_{host_port} proxy listen=udp:0.0.0.0:{host_port} connect=udp:127.0.0.1:{vps_port}", node_id=node_id)
            logger.info(f"Re-added port forward {host_port}->{vps_port} for {container_name}")
            readded_count += 1
        except Exception as e:
            logger.error(f"Failed to re-add port forward {host_port}->{vps_port} for {container_name}: {e}")
    conn.close()
    return readded_count

def node_public_host(node_id: int) -> str:
    """Public address people connect to for a node: the bot server's IPv4 for the local node, the node's own host otherwise."""
    node = get_node(node_id) or {}
    if node.get('is_local'):
        return get_public_ipv4_sync() or get_public_ip()
    from urllib.parse import urlparse
    return urlparse(node.get('url') or '').hostname or get_public_ip()

async def ensure_ssh_forward(container: str, node_id: int, owner_id) -> Optional[str]:
    """Gives the VPS a direct SSH address on the server's own IPv4: `IP:<random port>` -> VPS port 22.
    Reuses the existing forward (and re-adds its proxy device if it is missing). Returns 'host:port' or None."""
    conn = get_db()
    row = conn.execute('SELECT host_port FROM port_forwards WHERE vps_container = ? AND vps_port = 22 ORDER BY id LIMIT 1', (container,)).fetchone()
    conn.close()
    if row:
        host_port = int(row[0])
        try:
            devices = await execute_lxc(container, f"config device list {container}", node_id=node_id)
            if isinstance(devices, str) and f"tcp_proxy_{host_port}" not in devices:
                await recreate_port_forwards(container)
        except Exception as e:
            logger.warning(f"ensure_ssh_forward: could not verify devices of {container}: {e}")
    else:
        host_port = await create_port_forward(str(owner_id), container, 22, node_id, internal=True)
        if not host_port:
            return None
    return f"{node_public_host(node_id)}:{host_port}"

async def get_pinggy_access(container: str, node_id: int) -> Optional[str]:
    """Normal Pinggy hostname:port. On a node with public IPv4 the VPS tunnels over IPv4 (ssh -4), so the VPS is IPv4."""
    return await establish_pinggy_tunnel(container, node_id, ipv4=await node_has_ipv4(node_id))

async def establish_ssh_access(container: str, node_id: int, owner_id) -> Optional[str]:
    """SSH access is always given through the Pinggy tunnel (normal Pinggy hostname, no server IP / port forward,
    so no firewall ports are needed). IPv4 node -> the VPS is IPv4 inside; no IPv4 -> non-IPv4. Returns 'host:port' or None."""
    return await get_pinggy_access(container, node_id)

def find_node_id_for_container(container_name: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT node_id FROM vps WHERE container_name = ?', (container_name,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 1  # Default to local

# Initialize database
init_db()
PREFIX = get_setting('prefix', PREFIX) or PREFIX   # prefix chosen with the prefix command (saved in the DB)

# Load data at startup
vps_data = get_vps_data()
admin_data = {'admins': get_admins()}
main_admin_ids = set(get_main_admins())

# Global settings from DB
CPU_THRESHOLD = int(get_setting('cpu_threshold', 90))
RAM_THRESHOLD = int(get_setting('ram_threshold', 90))

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
def _prefix_callable(bot_, message):
    # reads the global every message, so a prefix change works instantly; @mention always works too
    return commands.when_mentioned_or(PREFIX)(bot_, message)

bot = commands.Bot(command_prefix=_prefix_callable, intents=intents, help_command=None)
BOT_START_TIME = time.time()  # used by the Uptime button

# Resource monitoring settings (logging only)
resource_monitor_active = True

# Helper function to truncate text
def truncate_text(text, max_length=1024):
    if not text:
        return text
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."

# Embed creation functions
def create_embed(title, description="", color=0x1a1a1a, bot_name=None):
    display_name = bot_name or BOT_NAME
    embed = discord.Embed(
        title=truncate_text(f"🌟 {display_name} - {title}", 256),
        description=truncate_text(description, 4096),
        color=color
    )
    embed.set_thumbnail(url=BOT_IMAGE_URL)
    embed.set_footer(text=f"{display_name} VPS Manager v{BOT_VERSION} • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                     icon_url=BOT_IMAGE_URL)
    return embed

def add_field(embed, name, value, inline=False):
    embed.add_field(
        name=truncate_text(f"▸ {name}", 256),
        value=truncate_text(value, 1024),
        inline=inline
    )
    return embed

def create_success_embed(title, description=""):
    return create_embed(title, description, color=0x00ff88)

def create_error_embed(title, description=""):
    return create_embed(title, description, color=0xff3366)

def create_info_embed(title, description=""):
    return create_embed(title, description, color=0x00ccff)

def create_warning_embed(title, description=""):
    return create_embed(title, description, color=0xffaa00)

# Admin checks
def is_admin():
    async def predicate(ctx):
        user_id = str(ctx.author.id)
        if user_id in main_admin_ids or user_id in admin_data.get("admins", []):
            return True
        raise commands.CheckFailure("You need admin permissions to use this command. Contact support.")
    predicate._bvm_admin = True   # marker used by the audit log hook
    return commands.check(predicate)

def is_main_admin():
    async def predicate(ctx):
        if str(ctx.author.id) in main_admin_ids:
            return True
        raise commands.CheckFailure("Only the main admin can use this command.")
    predicate._bvm_admin = True   # marker used by the audit log hook
    return commands.check(predicate)

# LXC command execution with multi-node support
async def execute_lxc(container_name: str, command: str, timeout=120, node_id: Optional[int] = None):
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)
    
    if not node:
        raise Exception(f"Node {node_id} not found")
    
    full_command = f"lxc {command}"
    
    if node['is_local']:
        try:
            cmd = shlex.split(full_command)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise asyncio.TimeoutError(f"Command timed out after {timeout} seconds")
            
            if proc.returncode != 0:
                error = stderr.decode().strip() if stderr else "Command failed with no error output"
                # Add more context to error
                raise Exception(f"Local LXC command failed: {error}\nCommand: {full_command}")
            return stdout.decode().strip() if stdout else True
        except asyncio.TimeoutError as te:
            logger.error(f"LXC command timed out: {full_command} - {str(te)}")
            raise
        except Exception as e:
            logger.error(f"LXC Error: {full_command} - {str(e)}")
            raise
    else:
        url = f"{node['url']}/api/execute"
        data = {"command": full_command}
        params = {"api_key": node["api_key"]}
        try:
            response = requests.post(url, json=data, params=params, timeout=timeout)
            
            # Try to get detailed error information
            try:
                error_detail = response.json()
                if 'detail' in error_detail:
                    error_msg = error_detail['detail']
                elif 'error' in error_detail:
                    error_msg = error_detail['error']
                else:
                    error_msg = response.text
            except:
                error_msg = response.text
            
            response.raise_for_status()
            
            res = response.json()
            if res.get("returncode", 1) != 0:
                stderr = res.get("stderr", "Command failed")
                # Add more context to remote error
                raise Exception(f"Remote LXC command failed on {node['name']}: {stderr}\nCommand: {full_command}")
            return res.get("stdout", True)
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Remote LXC error on node {node['name']} ({url}): {str(e)}")
            # Include URL and status code if available
            if hasattr(e.response, 'status_code'):
                raise Exception(f"Remote execution failed on {node['name']}: HTTP {e.response.status_code} - {str(e)}")
            else:
                raise Exception(f"Remote execution failed on {node['name']}: {str(e)}")

async def safe_start_container(container_name: str, node_id: int):
    """Starts a container, treating 'already running' as a success instead of an error.
    """
    try:
        await execute_lxc(container_name, f"start {container_name}", node_id=node_id)
    except Exception as e:
        err_text = str(e).lower()
        if "already running" in err_text or "is running" in err_text:
            logger.info(f"{container_name} was already running; treating start as success.")
        else:
            raise

# Kernel modules that Docker / Pterodactyl Wings / snap / Tailscale / KVM inside a VPS need.
HOST_MODULES = [
    "tun", "overlay", "br_netfilter", "bridge", "veth", "loop", "squashfs", "fuse",
    "ip_tables", "ip6_tables", "iptable_nat", "iptable_filter", "iptable_mangle", "ip6table_filter", "ip6table_nat",
    "nf_nat", "nf_conntrack", "nf_conntrack_netlink", "xt_conntrack", "xt_MASQUERADE", "xt_addrtype", "xt_nat",
    "xt_mark", "xt_comment", "xt_multiport", "xt_tcpudp", "xt_REDIRECT", "xt_owner", "nfnetlink",
    "vxlan", "netlink_diag", "vhost_net", "kvm", "kvm_intel", "kvm_amd",
]

# Modules that are put into the LXD `linux.kernel_modules` list of a VPS. LXD REFUSES to start a container when one
# of them cannot be loaded, so only modules the host really has are used (see _vps_kernel_modules()).
VPS_MODULE_CANDIDATES = [
    "overlay", "loop", "squashfs", "fuse", "br_netfilter", "veth", "bridge",
    "nf_nat", "nf_conntrack", "ip_tables", "ip6_tables", "iptable_nat", "iptable_filter", "iptable_mangle",
    "xt_conntrack", "xt_MASQUERADE", "xt_addrtype", "xt_mark", "nfnetlink", "vxlan", "netlink_diag",
]
VPS_MODULES_FALLBACK = "overlay,loop,nf_nat,ip_tables,ip6_tables,netlink_diag,br_netfilter"
LOOP_DEVICE_COUNT = 8   # /dev/loop0..7 are given to each VPS (snap needs loop devices)

def ensure_host_tun():
    """Prepares the host (local node) for Docker / Pterodactyl Wings / snap / Tailscale / KVM inside a VPS:
    loads the kernel modules, makes /dev/net/tun, /dev/fuse, /dev/loop-control and the loop devices.
    Best effort - never raises."""
    try:
        script = "; ".join(f"modprobe {m} 2>/dev/null" for m in HOST_MODULES) + "; "
        script += ("[ -c /dev/net/tun ] || (mkdir -p /dev/net && mknod /dev/net/tun c 10 200 && chmod 666 /dev/net/tun); "
                   "[ -c /dev/fuse ] || (mknod /dev/fuse c 10 229 && chmod 666 /dev/fuse); "
                   "[ -c /dev/loop-control ] || mknod /dev/loop-control c 10 237; "
                   f"for i in $(seq 0 {LOOP_DEVICE_COUNT - 1}); do [ -b /dev/loop$i ] || mknod /dev/loop$i b 7 $i; done; "
                   "true")
        subprocess.run(script, shell=True, timeout=60, capture_output=True)
    except Exception as e:
        logger.warning(f"ensure_host_tun failed: {e}")

def _vps_kernel_modules() -> str:
    """Comma separated module list for linux.kernel_modules - only modules this host can load (built in or available).
    Falls back to a small safe list if the host cannot be inspected."""
    ok = []
    try:
        for m in VPS_MODULE_CANDIDATES:
            if os.path.isdir(f"/sys/module/{m}"):
                ok.append(m)
                continue
            r = subprocess.run(["modprobe", "-n", m], capture_output=True, timeout=10)
            if r.returncode == 0:
                ok.append(m)
    except Exception as e:
        logger.warning(f"_vps_kernel_modules failed: {e}")
    return ",".join(ok) if ok else VPS_MODULES_FALLBACK

# Apply LXC config
async def apply_lxc_config(container_name: str, node_id: int):
    """Applies the Docker / Pterodactyl Wings / snap / Tailscale / KVM friendly LXC settings.
    Every setting is applied on its own: one unsupported option no longer stops the rest."""
    node = get_node(node_id)
    is_local = bool(node and node.get('is_local'))
    if is_local:
        await asyncio.to_thread(ensure_host_tun)
        kernel_modules = await asyncio.to_thread(_vps_kernel_modules)
    else:
        # A remote node cannot be inspected from here (node-agent.py loads the modules itself when it starts)
        kernel_modules = VPS_MODULES_FALLBACK

    async def _run(cmd: str) -> bool:
        try:
            await execute_lxc(container_name, cmd, node_id=node_id)
            return True
        except Exception as e:
            logger.warning(f"LXC config step failed for {container_name}: {cmd[:90]} -> {str(e)[:160]}")
            return False

    results = []
    for key, value in (
        ("security.nesting", "true"),
        ("security.privileged", "true"),
        ("security.syscalls.intercept.mknod", "true"),
        ("security.syscalls.intercept.setxattr", "true"),
        ("linux.kernel_modules", kernel_modules),
        ("limits.kernel.nofile", "1048576"),   # Docker / Wings open many files
    ):
        results.append(await _run(f"config set {container_name} {key} {value}"))
    if not results[4]:
        # a module list the node refused: retry with the small safe list so the VPS keeps its other settings
        await _run(f"config set {container_name} linux.kernel_modules {VPS_MODULES_FALLBACK}")

    # Devices that already exist are skipped. required=false: the VPS still starts if the host lacks the device.
    try:
        existing_raw = await execute_lxc(container_name, f"config device list {container_name}", node_id=node_id)
        existing = set(existing_raw.split()) if isinstance(existing_raw, str) else set()
    except Exception:
        existing = set()

    devices = [
        ("fuse", "unix-char path=/dev/fuse"),
        ("tun", "unix-char source=/dev/net/tun path=/dev/net/tun mode=0666 required=false"),
        # KVM: given whenever the host has /dev/kvm (VPS still starts on hosts without KVM)
        ("kvm", "unix-char source=/dev/kvm path=/dev/kvm mode=0666 required=false"),
        ("vhost-net", "unix-char source=/dev/vhost-net path=/dev/vhost-net mode=0666 required=false"),
        # snap: snapd mounts its packages (squashfs) through loop devices
        ("loop-control", "unix-char source=/dev/loop-control path=/dev/loop-control mode=0660 required=false"),
    ]
    for i in range(LOOP_DEVICE_COUNT):
        devices.append((f"loop{i}", f"unix-block source=/dev/loop{i} path=/dev/loop{i} mode=0660 required=false"))
    for dev_name, dev_args in devices:
        if dev_name in existing:
            continue
        await _run(f"config device add {container_name} {dev_name} {dev_args}")

    raw_lxc_config = (
        "lxc.apparmor.profile = unconfined\n"
        "lxc.apparmor.allow_nesting = 1\n"
        "lxc.apparmor.allow_incomplete = 1\n"
        "\n"
        "lxc.cap.drop =\n"
        "lxc.cgroup.devices.allow = a\n"
        "lxc.cgroup2.devices.allow = a\n"
        "\n"
        "lxc.mount.auto = proc:rw sys:rw cgroup:rw\n"
        "\n"
        "lxc.mount.entry = /dev/fuse dev/fuse none bind,create=file 0 0\n"
    )
    await _run(f"config set {container_name} raw.lxc '{raw_lxc_config}'")
    logger.info(f"LXC permissions applied to {container_name} on node {node_id}")
    return any(results)

# Apply internal permissions
async def apply_internal_permissions(container_name: str, node_id: int):
    try:
        await asyncio.sleep(8)
        commands = [
            "mkdir -p /etc/sysctl.d/",
            "echo 'net.ipv4.ip_unprivileged_port_start=0' > /etc/sysctl.d/99-custom.conf",
            "echo 'net.ipv4.ping_group_range=0 2147483647' >> /etc/sysctl.d/99-custom.conf",
            "echo 'fs.inotify.max_user_watches=524288' >> /etc/sysctl.d/99-custom.conf",
            "echo 'kernel.unprivileged_userns_clone=1' >> /etc/sysctl.d/99-custom.conf",
            "echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.d/99-custom.conf",
            "echo 'net.ipv6.conf.all.forwarding=1' >> /etc/sysctl.d/99-custom.conf",
            "echo 'vm.max_map_count=262144' >> /etc/sysctl.d/99-custom.conf",
            "echo 'fs.inotify.max_user_instances=8192' >> /etc/sysctl.d/99-custom.conf",
            "sysctl -p /etc/sysctl.d/99-custom.conf || true"
        ]
        for cmd in commands:
            try:
                await execute_lxc(container_name, f"exec {container_name} -- bash -c \"{cmd}\"", node_id=node_id)
            except Exception as cmd_error:
                logger.warning(f"Command failed in {container_name}: {cmd} - {cmd_error}")
        logger.info(f"Internal permissions applied to {container_name}")
    except Exception as e:
        logger.error(f"Failed to apply internal permissions to {container_name}: {e}")

def generate_password(length: int = 24) -> str:
    """Generate a random alnum-only password (safe to embed in shell commands)."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def generate_container_name(user, vps_count: int) -> str:
    """Builds the LXC container name (also the VPS hostname, shown as
    root@<name>:~#) in the format <username>-vps-<number>, where <number> is
    the global VPS number: the 1st VPS ever created gets 1, the next one
    (any player) gets 2, and so on. e.g. 'aayanraza-vps-2'.
    The counter is stored in settings so numbers are never reused, even if
    a VPS is deleted."""
    raw = (getattr(user, "name", None) or "").lower()
    base = re.sub(r'[^a-z0-9]+', '-', raw).strip('-')[:40].strip('-')
    if not base:
        base = f"user-{user.id}"
    elif not base[0].isalpha():
        base = f"u-{base}"
    all_names = [v.get('container_name') or '' for vlist in vps_data.values() for v in vlist]
    used = set(all_names)
    highest = 0
    for name in all_names:
        m = re.search(r'-vps-(\d+)$', name)
        if m:
            highest = max(highest, int(m.group(1)))
    try:
        stored = int(get_setting('vps_counter', 0))
    except (TypeError, ValueError):
        stored = 0
    counter = max(stored, highest, len(all_names))
    while True:
        counter += 1
        candidate = f"{base}-vps-{counter}"
        if candidate not in used:
            break
    set_setting('vps_counter', str(counter))
    return candidate

async def setup_ssh_access(container_name: str, node_id: int) -> str:
    """Installs openssh-server, sets a random root password, and enables
    password-based root SSH login inside the container. Returns the password."""
    password = generate_password()
    commands = [
        "DEBIAN_FRONTEND=noninteractive apt-get update -y",
        "DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server",
        f"echo 'root:{password}' | chpasswd",
        "printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n\\n' | cat - /etc/ssh/sshd_config > /tmp/sshd_config.new && mv /tmp/sshd_config.new /etc/ssh/sshd_config",
        "mkdir -p /run/sshd",
        "systemctl enable ssh 2>/dev/null || systemctl enable sshd 2>/dev/null || true",
        "systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || service sshd restart 2>/dev/null || true"
    ]
    for cmd in commands:
        try:
            await execute_lxc(container_name, f"exec {container_name} -- bash -c \"{cmd}\"", node_id=node_id, timeout=180)
        except Exception as cmd_error:
            logger.warning(f"SSH setup command failed in {container_name}: {cmd} - {cmd_error}")
    # Make sure password login really works (also over Tailscale): fixes sshd_config.d drop-ins, unlocks root, restarts sshd
    try:
        await ensure_ssh_password_login(container_name, node_id, password)
    except Exception as e:
        logger.warning(f"ensure_ssh_password_login failed for {container_name}: {e}")
    return password

# ---------------------------------------------------------------------------
# Pinggy.io SSH tunnel system (replaces local port-forwarding for VPS SSH)
# ---------------------------------------------------------------------------
PINGGY_LOG_PATH = "/root/.pinggy_tunnel.log"

def parse_pinggy_address(log_text: str) -> Optional[str]:
    """Extracts host:port from Pinggy's tcp:// forwarding line, stripping the tcp:// prefix."""
    if not log_text:
        return None
    match = re.search(r'tcp://([\w\.\-]+):(\d+)', log_text, re.IGNORECASE)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    return None

async def establish_pinggy_tunnel(container_name: str, node_id: int, retries: int = 4, wait_seconds: int = 5, ipv4: bool = False) -> Optional[str]:
    """
    Runs 'ssh -p 443 -R0:localhost:22 qr+tcp@free.pinggy.io' inside the container
    (equivalent to: lxc exec <container> -- bash, then running that ssh command and
    accepting the host-key prompt with 'yes' automatically), as a persistent background
    tunnel. Returns the assigned 'host:port' (with the tcp:// prefix stripped), or None
    if the address could not be determined.
    """
    try:
        # Make sure an SSH client exists inside the container
        await execute_lxc(
            container_name,
            f"exec {container_name} -- bash -c \"command -v ssh >/dev/null || (DEBIAN_FRONTEND=noninteractive apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-client)\"",
            node_id=node_id, timeout=180
        )
    except Exception as e:
        logger.warning(f"Pinggy: couldn't verify/install ssh client in {container_name}: {e}")

    # Kill any previous tunnel and clear the old log
    try:
        await execute_lxc(
            container_name,
            f"exec {container_name} -- bash -c \"pkill -f 'free.pinggy.io' >/dev/null 2>&1; rm -f {PINGGY_LOG_PATH}\"",
            node_id=node_id
        )
    except Exception:
        pass

    # Start the tunnel in the background. -o StrictHostKeyChecking=no auto-accepts the
    # host key prompt (the 'always yes' behavior), so it never blocks waiting for input.
    tunnel_cmd = (
        f"setsid nohup ssh {'-4 ' if ipv4 else ''}-p 443 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        "-o ServerAliveInterval=30 -R0:localhost:22 qr+tcp@free.pinggy.io "
        f"> {PINGGY_LOG_PATH} 2>&1 < /dev/null & disown"
    )
    try:
        await execute_lxc(
            container_name,
            f"exec {container_name} -- bash -c \"{tunnel_cmd}\"",
            node_id=node_id
        )
    except Exception as e:
        logger.error(f"Pinggy: failed to start tunnel in {container_name}: {e}")
        return None

    # Poll the log until the tcp:// address shows up
    for attempt in range(retries):
        await asyncio.sleep(wait_seconds)
        try:
            log_output = await execute_lxc(
                container_name,
                f"exec {container_name} -- cat {PINGGY_LOG_PATH}",
                node_id=node_id
            )
        except Exception:
            log_output = ""
        address = parse_pinggy_address(log_output if isinstance(log_output, str) else "")
        if address:
            return address
    logger.warning(f"Pinggy: no tunnel address found for {container_name} after {retries} attempts")
    return None

# ---------------------------------------------------------------------------
# SSHX web terminal (https://sshx.io) - gives a browser-based terminal link
# ---------------------------------------------------------------------------
SSHX_LOG_PATH = "/root/.sshx_session.log"

def parse_sshx_link(log_text: str) -> Optional[str]:
    """Extracts the https://sshx.io/s/<id>#<key> link from sshx output (ANSI codes ignored)."""
    if not log_text:
        return None
    match = re.search(r'https://sshx\.io/s/[A-Za-z0-9_\-]+#[A-Za-z0-9_\-]+', log_text)
    return match.group(0) if match else None

async def establish_sshx_session(container_name: str, node_id: int, retries: int = 6, wait_seconds: int = 4) -> Optional[str]:
    """Installs sshx inside the container if needed, starts it as a persistent
    background process and returns the web terminal link (or None on failure)."""
    try:
        await execute_lxc(
            container_name,
            f"exec {container_name} -- bash -c \"command -v sshx >/dev/null || (command -v curl >/dev/null || (DEBIAN_FRONTEND=noninteractive apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates); curl -sSf https://sshx.io/get | sh)\"",
            node_id=node_id, timeout=240
        )
    except Exception as e:
        logger.error(f"SSHX: install failed in {container_name}: {e}")
        return None

    # Stop any previous sshx session and clear the log
    try:
        await execute_lxc(
            container_name,
            f"exec {container_name} -- bash -c \"pkill -x sshx >/dev/null 2>&1; rm -f {SSHX_LOG_PATH}; true\"",
            node_id=node_id
        )
    except Exception:
        pass

    try:
        await execute_lxc(
            container_name,
            f"exec {container_name} -- bash -c \"setsid nohup sshx > {SSHX_LOG_PATH} 2>&1 < /dev/null & disown\"",
            node_id=node_id
        )
    except Exception as e:
        logger.error(f"SSHX: failed to start in {container_name}: {e}")
        return None

    for _ in range(retries):
        await asyncio.sleep(wait_seconds)
        try:
            log_output = await execute_lxc(container_name, f"exec {container_name} -- cat {SSHX_LOG_PATH}", node_id=node_id)
        except Exception:
            log_output = ""
        link = parse_sshx_link(log_output if isinstance(log_output, str) else "")
        if link:
            return link
    logger.warning(f"SSHX: no link found for {container_name} after {retries} attempts")
    return None

# Get or create VPS role
async def get_or_create_vps_role(guild):
    global VPS_USER_ROLE_ID

    me = guild.me
    if not me or not me.guild_permissions.manage_roles:
        return None

    role_name = f"{BOT_NAME} VPS User"

    # Try cached role
    if VPS_USER_ROLE_ID:
        role = guild.get_role(VPS_USER_ROLE_ID)
        if role and role < me.top_role:
            return role
        VPS_USER_ROLE_ID = None

    # Find by name
    role = discord.utils.get(guild.roles, name=role_name)
    if role:
        if role >= me.top_role:
            try:
                await role.delete(reason="Role above bot, recreating")
            except discord.Forbidden:
                return None
            role = None
        else:
            VPS_USER_ROLE_ID = role.id
            return role

    # Create safely below bot
    try:
        role = await guild.create_role(
            name=role_name,
            color=discord.Color.dark_purple(),
            permissions=discord.Permissions.none(),
            reason=f"{BOT_NAME} VPS User role"
        )
        await role.edit(position=me.top_role.position - 1)
        VPS_USER_ROLE_ID = role.id
        logger.info(f"Created VPS role: {role.id}")
        return role
    except Exception as e:
        logger.error(f"Failed to create VPS role: {e}")
        return None

# Host resource functions
def get_host_cpu_usage():
    """Host CPU usage in %, measured from /proc/stat over 0.5s (falls back to mpstat/top)."""
    try:
        def _sample():
            with open('/proc/stat') as f:
                vals = [int(x) for x in f.readline().split()[1:]]
            return vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
        i1, t1 = _sample()
        time.sleep(0.5)
        i2, t2 = _sample()
        if t2 > t1:
            return round(100.0 * (1 - (i2 - i1) / (t2 - t1)), 1)
    except Exception:
        pass
    return _get_host_cpu_usage_legacy()

def _get_host_cpu_usage_legacy():
    try:
        if shutil.which("mpstat"):
            result = subprocess.run(['mpstat', '1', '1'], capture_output=True, text=True)
            output = result.stdout
            for line in output.split('\n'):
                if 'all' in line and '%' in line:
                    parts = line.split()
                    idle = float(parts[-1])
                    return 100.0 - idle
        else:
            result = subprocess.run(['top', '-bn1'], capture_output=True, text=True)
            output = result.stdout
            for line in output.split('\n'):
                if '%Cpu(s):' in line:
                    parts = line.split()
                    us = float(parts[1])
                    sy = float(parts[3])
                    ni = float(parts[5])
                    id_ = float(parts[7])
                    wa = float(parts[9])
                    hi = float(parts[11])
                    si = float(parts[13])
                    st = float(parts[15])
                    usage = us + sy + ni + wa + hi + si + st
                    return usage
        return 0.0
    except Exception as e:
        logger.error(f"Error getting CPU usage: {e}")
        return 0.0

def get_host_ram_usage():
    try:
        result = subprocess.run(['free', '-m'], capture_output=True, text=True)
        lines = result.stdout.splitlines()
        if len(lines) > 1:
            mem = lines[1].split()
            total = int(mem[1])
            used = int(mem[2])
            return (used / total * 100) if total > 0 else 0.0
        return 0.0
    except Exception as e:
        logger.error(f"Error getting RAM usage: {e}")
        return 0.0

async def get_host_stats(node_id: int) -> Dict:
    node = get_node(node_id)
    if node['is_local']:
        return {"cpu": get_host_cpu_usage(), "ram": get_host_ram_usage()}
    else:
        url = f"{node['url']}/api/get_host_stats"
        params = {"api_key": node["api_key"]}
        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to get host stats from node {node['name']}: {e}")
            return {"cpu": 0.0, "ram": 0.0}

# ============================================================
#  Admin tools: audit log helper, auto alerts, maintenance mode
# ============================================================
MAIN_LOOP = None          # set in on_ready, used by the monitor thread to reach Discord
_alert_last: Dict[Any, float] = {}
_alert_breaches: Dict[Any, int] = {}
_node_down = set()
ALERT_COOLDOWN_SECONDS = 30 * 60

def is_admin_id(user_id) -> bool:
    uid = str(user_id)
    return uid in main_admin_ids or uid in admin_data.get("admins", [])

def log_audit(admin_id, admin_name: str, action: str, target: str = "", details: str = ""):
    """Stores one admin action in the audit_logs table (never raises)."""
    try:
        conn = get_db()
        conn.execute(
            'INSERT INTO audit_logs (ts, admin_id, admin_name, action, target, details) VALUES (?, ?, ?, ?, ?, ?)',
            (int(time.time()), str(admin_id), str(admin_name)[:80], str(action)[:60], str(target)[:120], str(details)[:300]))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")

def maintenance_on() -> bool:
    return str(get_setting('maintenance_mode', '0')) == '1'

def maintenance_reason() -> str:
    return str(get_setting('maintenance_reason', 'Scheduled maintenance'))

async def _deliver_admin_alert(title: str, message: str):
    embed = create_warning_embed(title, message)
    ch_id = get_setting('alert_channel', None)
    if ch_id:
        try:
            channel = bot.get_channel(int(ch_id)) or await bot.fetch_channel(int(ch_id))
            await channel.send(embed=embed)
            return
        except Exception as e:
            logger.error(f"Could not post alert to channel {ch_id}: {e} - falling back to DMs")
    for admin_id in list(main_admin_ids):
        try:
            user = bot.get_user(int(admin_id)) or await bot.fetch_user(int(admin_id))
            await user.send(embed=embed)
        except Exception as e:
            logger.error(f"Could not DM alert to {admin_id}: {e}")

def send_admin_alert(key, title: str, message: str, force: bool = False):
    """Thread-safe, rate-limited alert to the admins (alert channel or DMs)."""
    try:
        if str(get_setting('alerts_enabled', '1')) != '1' or MAIN_LOOP is None:
            return
        now = time.time()
        if not force and now - _alert_last.get(key, 0) < ALERT_COOLDOWN_SECONDS:
            return
        _alert_last[key] = now
        asyncio.run_coroutine_threadsafe(_deliver_admin_alert(title, message), MAIN_LOOP)
    except Exception as e:
        logger.error(f"send_admin_alert failed: {e}")

def node_ping_ok(node: Dict, timeout: int = 5) -> bool:
    try:
        r = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False

def _local_disk_percent() -> float:
    try:
        u = shutil.disk_usage('/')
        return u.used / u.total * 100
    except Exception:
        return 0.0

def _check_node_for_alerts(node: Dict):
    """Called every minute by the resource monitor thread for each node."""
    nid, name = node['id'], node['name']
    if not node['is_local']:
        if not node_ping_ok(node):
            _node_down.add(nid)
            send_admin_alert(f"node_down_{nid}", "🔴 Node Offline",
                             f"Node **{name}** (`{node.get('url')}`) is not responding to health checks.")
            return
        if nid in _node_down:
            _node_down.discard(nid)
            send_admin_alert(f"node_up_{nid}", "🟢 Node Back Online", f"Node **{name}** is responding again.", force=True)
    stats = asyncio.run(get_host_stats(nid))
    cpu = float(stats.get('cpu', 0) or 0)
    ram = float(stats.get('ram', 0) or 0)
    logger.info(f"Node {name}: CPU {cpu:.1f}%, RAM {ram:.1f}%")
    checks = [('cpu', cpu, CPU_THRESHOLD), ('ram', ram, RAM_THRESHOLD)]
    if node['is_local']:
        checks.append(('disk', _local_disk_percent(), 90))
    for kind, value, limit in checks:
        key = (nid, kind)
        if value > limit:
            _alert_breaches[key] = _alert_breaches.get(key, 0) + 1
            # alert only when the limit is exceeded 2 checks in a row (about 2 minutes)
            if _alert_breaches[key] >= 2:
                send_admin_alert(f"{kind}_{nid}", f"🔥 High {kind.upper()} on {name}",
                                 f"**{kind.upper()}** usage is **{value:.1f}%** (limit {limit}%).\n"
                                 f"Use `{PREFIX}health` / `{PREFIX}node-health` to investigate.")
        else:
            _alert_breaches[key] = 0

def resource_monitor():
    global resource_monitor_active
    backup_interval = 3600
    last_backup = time.time()
    while resource_monitor_active:
        try:
            nodes = get_nodes()
            for node in nodes:
                try:
                    _check_node_for_alerts(node)
                except Exception as e:
                    logger.error(f"Monitor check failed for node {node.get('name')}: {e}")
            if time.time() - last_backup > backup_interval:
                backup_name = f"vps_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                try:
                    shutil.copy('vps.db', backup_name)
                    if os.path.exists('vps.db-wal'):
                        shutil.copy('vps.db-wal', f"{backup_name}-wal")
                    if os.path.exists('vps.db-shm'):
                        shutil.copy('vps.db-shm', f"{backup_name}-shm")
                    logger.info(f"Database backup created: {backup_name}")
                    last_backup = time.time()
                except Exception as e:
                    logger.error(f"Failed to create DB backup: {e}")
            time.sleep(60)
        except Exception as e:
            logger.error(f"Error in resource monitor: {e}")
            time.sleep(60)

# Start resource monitoring thread
monitor_thread = threading.Thread(target=resource_monitor, daemon=True)
monitor_thread.start()

# Container stats with multi-node
async def get_container_stats(container_name: str, node_id: Optional[int] = None) -> Dict:
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)
    if node['is_local']:
        status = await get_container_status_local(container_name)
        cpu = await get_container_cpu_pct_local(container_name)
        ram = await get_container_ram_local(container_name)
        disk = await get_container_disk_local(container_name)
        uptime = await get_container_uptime_local(container_name)
        return {"status": status, "cpu": cpu, "ram": ram, "disk": disk, "uptime": uptime}
    else:
        url = f"{node['url']}/api/get_container_stats"
        data = {"container": container_name}
        params = {"api_key": node["api_key"]}
        try:
            response = requests.post(url, json=data, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to get container stats from node {node['name']}: {e}")
            return {"status": "unknown", "cpu": 0.0, "ram": {"used": 0, "total": 0, "pct": 0.0}, "disk": "Unknown", "uptime": "Unknown"}

async def get_container_status_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "info", container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        for line in output.splitlines():
            if line.startswith("Status: "):
                return line.split(": ", 1)[1].strip().lower()
        return "unknown"
    except Exception:
        return "unknown"

async def get_container_cpu_pct_local(container_name: str):
    try:
        # `top -bn2 -d1`: the FIRST iteration is the average since boot, the SECOND one is the real current load
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "top", "-bn2", "-d", "1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        idle = None
        for line in output.splitlines():
            if '%Cpu(s):' in line:
                m = re.search(r'([\d.,]+)\s*id', line)
                if m:
                    idle = float(m.group(1).replace(',', '.'))   # keep the LAST Cpu line
        if idle is None:
            return 0.0
        return max(0.0, min(100.0, 100.0 - idle))
    except Exception as e:
        logger.error(f"Error getting CPU for {container_name}: {e}")
        return 0.0

async def get_container_ram_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "free", "-m",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().splitlines()
        if len(lines) > 1:
            parts = lines[1].split()
            total = int(parts[1])
            used = int(parts[2])
            pct = (used / total * 100) if total > 0 else 0.0
            return {'used': used, 'total': total, 'pct': pct}
        return {'used': 0, 'total': 0, 'pct': 0.0}
    except Exception as e:
        logger.error(f"Error getting RAM for {container_name}: {e}")
        return {'used': 0, 'total': 0, 'pct': 0.0}

async def get_container_disk_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "df", "-h", "/",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().splitlines()
        for line in lines:
            if '/dev/' in line and ' /' in line:
                parts = line.split()
                if len(parts) >= 5:
                    used = parts[2]
                    size = parts[1]
                    perc = parts[4]
                    return f"{used}/{size} ({perc})"
        return "Unknown"
    except Exception:
        return "Unknown"

async def get_container_uptime_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "uptime",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip() if stdout else "Unknown"
    except Exception:
        return "Unknown"

async def get_container_status(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['status']

async def get_container_cpu(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return f"{stats['cpu']:.1f}%"

async def get_container_cpu_pct(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['cpu']

async def get_container_memory(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    ram = stats['ram']
    return f"{ram['used']}/{ram['total']} MB ({ram['pct']:.1f}%)"

async def get_container_ram_pct(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['ram']['pct']

async def get_container_disk(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['disk']

async def get_container_uptime(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['uptime']

def format_duration(seconds) -> str:
    """Turn seconds into a readable string like '2d 3h 14m 5s'."""
    try:
        seconds = int(seconds)
    except Exception:
        return "Unknown"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    if days or hours or minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)

def get_bot_uptime_seconds() -> float:
    return time.time() - BOT_START_TIME

def get_host_uptime_seconds() -> Optional[float]:
    try:
        with open('/proc/uptime') as f:
            return float(f.read().split()[0])
    except Exception:
        return None

async def get_container_uptime_seconds(container_name: str, node_id: Optional[int] = None) -> Optional[float]:
    """VPS (container) uptime in seconds. Works for local and remote nodes."""
    try:
        out = await execute_lxc(container_name, f"exec {container_name} -- cat /proc/uptime", timeout=20, node_id=node_id)
        if isinstance(out, str) and out.strip():
            return float(out.split()[0])
    except Exception as e:
        logger.error(f"Could not read uptime for {container_name}: {e}")
    return None

def get_uptime():
    try:
        result = subprocess.run(['uptime'], capture_output=True, text=True)
        return result.stdout.strip()
    except Exception:
        return "Unknown"

# Try to detect default storage pool or use common defaults
def get_default_storage_pool():
    try:
        result = subprocess.run(['lxc', 'storage', 'list', '--format', 'csv'], 
                              capture_output=True, text=True)
        lines = result.stdout.strip().split('\n')
        if lines and lines[0]:
            # Get first storage pool
            return lines[0].split(',')[0]
    except:
        pass
    return "default"  # Fallback to 'default'

DEFAULT_STORAGE_POOL = os.getenv('DEFAULT_STORAGE_POOL', get_default_storage_pool())

# Bot events
@bot.event
async def on_ready():
    global MAIN_LOOP
    global _expiry_task, _guard_task, _features_task, _usage_alert_task
    MAIN_LOOP = asyncio.get_running_loop()
    if _expiry_task is None or _expiry_task.done():
        _expiry_task = asyncio.create_task(expiry_loop())   # VPS expiry reminders + suspend
    if _usage_alert_task is None or _usage_alert_task.done():
        _usage_alert_task = asyncio.create_task(vps_usage_alert_loop())   # 24/7 CPU/RAM/disk >= limit -> DM main admins
    if _features_task is None or _features_task.done():
        _features_task = asyncio.create_task(vps_feature_upgrade_loop())   # tun/kvm/SSH fix for older VPS
    logger.info(f'{bot.user} has connected to Discord!')
    await refresh_presence()
    logger.info(f"{BOT_NAME} Bot is ready!")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=create_error_embed("Missing Argument", f"Please check command usage with `{PREFIX}help`."))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=create_error_embed("Invalid Argument", "Please check your input and try again."))
    elif isinstance(error, commands.CheckFailure):
        error_msg = str(error) if str(error) else "You need admin permissions for this command. Contact support."
        await ctx.send(embed=create_error_embed("Access Denied", error_msg))
    elif isinstance(error, discord.NotFound):
        await ctx.send(embed=create_error_embed("Error", "The requested resource was not found. Please try again."))
    else:
        logger.error(f"Command error: {error}")
        await ctx.send(embed=create_error_embed("System Error", "An unexpected error occurred. Support has been notified."))

# Bot commands
@bot.command(name='ping')
async def ping(ctx):
    latency = round(bot.latency * 1000)
    embed = create_success_embed("Pong!", f"{BOT_NAME} Bot latency: {latency}ms")
    await ctx.send(embed=embed)

@bot.command(name='uptime')
async def uptime(ctx):
    up = get_uptime()
    embed = create_info_embed("Host Uptime", up)
    await ctx.send(embed=embed)

@bot.command(name='thresholds')
@is_admin()
async def thresholds(ctx):
    embed = create_info_embed("Resource Thresholds", f"**CPU:** {CPU_THRESHOLD}%\n**RAM:** {RAM_THRESHOLD}%")
    await ctx.send(embed=embed)

@bot.command(name='set-threshold')
@is_admin()
async def set_threshold(ctx, cpu: int, ram: int):
    global CPU_THRESHOLD, RAM_THRESHOLD
    if cpu < 0 or ram < 0:
        await ctx.send(embed=create_error_embed("Invalid Thresholds", "Thresholds must be non-negative."))
        return
    CPU_THRESHOLD = cpu
    RAM_THRESHOLD = ram
    set_setting('cpu_threshold', str(cpu))
    set_setting('ram_threshold', str(ram))
    embed = create_success_embed("Thresholds Updated", f"**CPU:** {cpu}%\n**RAM:** {ram}%")
    await ctx.send(embed=embed)

@bot.command(name='set-status')
@is_admin()
async def set_status(ctx, activity_type: str, *, name: str):
    types = {
        'playing': discord.ActivityType.playing,
        'watching': discord.ActivityType.watching,
        'listening': discord.ActivityType.listening,
        'streaming': discord.ActivityType.streaming,
    }
    if activity_type.lower() not in types:
        await ctx.send(embed=create_error_embed("Invalid Type", "Valid types: playing, watching, listening, streaming"))
        return
    await bot.change_presence(activity=discord.Activity(type=types[activity_type.lower()], name=name))
    embed = create_success_embed("Status Updated", f"Set to {activity_type}: {name}")
    await ctx.send(embed=embed)

@bot.command(name="myvps")
async def my_vps(ctx):
    user_id = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])

    # ─── No VPS Case ───────────────────────────────────────────
    if not vps_list:
        embed = create_error_embed(
            "❌ No VPS Found",
            f"You don’t have any **{BOT_NAME} VPS** yet."
        )
        embed.add_field(
            name="🚀 Quick Actions",
            value=(
                f"• `{PREFIX}manage` – Manage VPS\n"
                f"• Contact an admin to request a VPS"
            ),
            inline=False
        )
        await ctx.send(embed=embed)
        return

    # ─── Embed ────────────────────────────────────────────────
    embed = create_info_embed(
        title="🖥️ My VPS Dashboard",
        description="Your personal VPS overview"
    )

    total_vps = len(vps_list)
    running = suspended = whitelisted = 0
    vps_cards = []

    # ─── VPS Processing ───────────────────────────────────────
    for i, vps in enumerate(vps_list, start=1):
        node = get_node(vps.get("node_id"))
        node_name = node["name"] if node else "Unknown"

        config = vps.get("config", "Custom")
        ram = vps.get("ram", "0GB")
        cpu = vps.get("cpu", "0")
        storage = vps.get("storage", "0GB")

        if vps.get("suspended"):
            status = "⛔ SUSPENDED"
            suspended += 1
        elif vps.get("status") == "running":
            status = "🟢 RUNNING"
            running += 1
        else:
            status = "🔴 STOPPED"

        if vps.get("whitelisted"):
            whitelisted += 1

        vps_cards.append(
            f"**{i}.** `{vps['container_name']}`\n"
            f"{status} • `{config}`\n"
            f"⚙️ `{ram}` RAM • `{cpu}` CPU • `{storage}` Disk\n"
            f"📍 Node: `{node_name}`"
        )

    # ─── Row 1 : Summary ──────────────────────────────────────
    embed.add_field(
        name="📊 Summary",
        value=(
            f"🖥️ `{total_vps}` VPS\n"
            f"🟢 `{running}` Running\n"
            f"⛔ `{suspended}` Suspended\n"
            f"✅ `{whitelisted}` Whitelisted"
        ),
        inline=True
    )

    embed.add_field(
        name="⚡ Quick Actions",
        value=(
            f"`{PREFIX}manage`\n"
            f"`{PREFIX}reinstall`\n"
            f"`{PREFIX}status`"
        ),
        inline=True
    )

    embed.add_field(
        name="🧭 Tip",
        value="Use **manage** to control your VPS",
        inline=True
    )

    # ─── VPS Cards (Full Width) ───────────────────────────────
    vps_text = "\n\n".join(vps_cards)
    for i in range(0, len(vps_text), 1024):
        embed.add_field(
            name="🖥️ Your VPS",
            value=vps_text[i:i + 1024],
            inline=False
        )

    embed.set_footer(text=f"{BOT_NAME} • VPS Control Panel")
    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)

@bot.command(name='lxc-list')
@is_admin()
async def lxc_list(ctx, node_id: int = 1):
    try:
        result = await execute_lxc("", "list", node_id=node_id)
        node = get_node(node_id)
        embed = create_info_embed(f"LXC Containers List on {node['name']}", result)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Error", str(e)))

class NodeSelectView(discord.ui.View):
    def __init__(self, ram: int, cpu: int, disk: int, user: discord.Member, ctx, days: Optional[int] = None):
        super().__init__(timeout=300)
        self.days = days
        self.ram = ram
        self.cpu = cpu
        self.disk = disk
        self.user = user
        self.ctx = ctx
        nodes = get_nodes()
        options = []
        for n in nodes:
            current_count = get_current_vps_count(n['id'])
            if current_count < n['total_vps']:
                options.append(discord.SelectOption(label=n['name'], value=str(n['id']), description=f"{n['location']} - Available: {n['total_vps'] - current_count}"))
        if not options:
            self.add_item(discord.ui.Select(placeholder="No available nodes", disabled=True))
        else:
            self.select = discord.ui.Select(placeholder="Select a Node for the VPS", options=options)
            self.select.callback = self.select_node
            self.add_item(self.select)

    async def select_node(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the command author can select."), ephemeral=True)
            return
        node_id = int(self.select.values[0])
        self.select.disabled = True
        await interaction.response.edit_message(view=self)
        ok, err = await check_node_ipv4(node_id)
        if not ok:
            await interaction.followup.send(embed=create_error_embed("This VPS is not IPv4", err))
        os_view = OSSelectView(self.ram, self.cpu, self.disk, self.user, self.ctx, node_id, self.days)
        await interaction.followup.send(embed=create_info_embed("Select OS", "Choose the OS for the VPS."), view=os_view)

class OSSelectView(discord.ui.View):
    def __init__(self, ram: int, cpu: int, disk: int, user: discord.Member, ctx, node_id: int, days: Optional[int] = None):
        super().__init__(timeout=300)
        self.days = days
        self.ram = ram
        self.cpu = cpu
        self.disk = disk
        self.user = user
        self.ctx = ctx
        self.node_id = node_id
        self.select = discord.ui.Select(
            placeholder="Select an OS for the VPS",
            options=[discord.SelectOption(label=o["label"], value=o["value"]) for o in OS_OPTIONS]
        )
        self.select.callback = self.select_os
        self.add_item(self.select)

    async def select_os(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the command author can select."), ephemeral=True)
            return
        os_version = self.select.values[0]
        self.select.disabled = True
        creating_embed = create_info_embed("Creating VPS", f"Deploying {os_version} VPS for {self.user.mention} on node {self.node_id}...")
        await interaction.response.edit_message(embed=creating_embed, view=self)
        user_id = str(self.user.id)
        if user_id not in vps_data:
            vps_data[user_id] = []
        vps_count = len(vps_data[user_id]) + 1
        container_name = generate_container_name(self.user, vps_count)
        ram_mb = self.ram * 1024
        try:
            await execute_lxc(container_name, f"init {os_version} {container_name} -s {DEFAULT_STORAGE_POOL}", node_id=self.node_id)
            await execute_lxc(container_name, f"config set {container_name} limits.memory {ram_mb}MB", node_id=self.node_id)
            await execute_lxc(container_name, f"config set {container_name} limits.cpu {self.cpu}", node_id=self.node_id)
            await execute_lxc(container_name, f"config device set {container_name} root size={self.disk}GB", node_id=self.node_id)
            await apply_lxc_config(container_name, self.node_id)
            await safe_start_container(container_name, self.node_id)
            await apply_internal_permissions(container_name, self.node_id)
            root_password = await setup_ssh_access(container_name, self.node_id)
            pinggy_address = await establish_ssh_access(container_name, self.node_id, user_id)
            config_str = f"{self.ram}GB RAM / {self.cpu} CPU / {self.disk}GB Disk"
            vps_info = {
                "container_name": container_name,
                "node_id": self.node_id,
                "ram": f"{self.ram}GB",
                "cpu": str(self.cpu),
                "storage": f"{self.disk}GB",
                "config": config_str,
                "os_version": os_version,
                "status": "running",
                "suspended": False,
                "whitelisted": False,
                "suspension_history": [],
                "created_at": datetime.now().isoformat(),
                "shared_with": [],
                "root_password": root_password,
                "pinggy_address": pinggy_address,
                "expires_at": expiry_from_days(self.days),
                "expiry_notified": [],
                "id": None
            }
            vps_data[user_id].append(vps_info)
            save_vps_data()
            if self.ctx.guild:
                vps_role = await get_or_create_vps_role(self.ctx.guild)
                if vps_role:
                    try:
                        await self.user.add_roles(vps_role, reason=f"{BOT_NAME} VPS ownership granted")
                    except discord.Forbidden:
                        logger.warning(f"Failed to assign VPS role to {self.user.name}")
            success_embed = create_success_embed("VPS Created Successfully")
            add_field(success_embed, "Owner", self.user.mention, True)
            add_field(success_embed, "VPS ID", f"#{vps_count}", True)
            add_field(success_embed, "Container", f"`{container_name}`", True)
            add_field(success_embed, "Node", get_node(self.node_id)['name'], True)
            add_field(success_embed, "Resources", f"**RAM:** {self.ram}GB\n**CPU:** {self.cpu} Cores\n**Storage:** {self.disk}GB", False)
            add_field(success_embed, "OS", os_version, True)
            add_field(success_embed, "Features", "Nesting, Privileged, FUSE, Kernel Modules (Docker Ready), Unprivileged Ports from 0", False)
            add_field(success_embed, "Disk Note", "Run `sudo resize2fs /` inside VPS if needed to expand filesystem.", False)
            await interaction.followup.send(embed=success_embed)
            dm_embed = create_success_embed("VPS Created!", f"Your VPS has been successfully deployed by an admin!")
            add_field(dm_embed, "VPS Details", f"**VPS ID:** #{vps_count}\n**Container Name:** `{container_name}`\n**Configuration:** {config_str}\n**Status:** Running\n**OS:** {os_version}\n**Created:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", False)
            if vps_info.get('expires_at'):
                add_field(dm_embed, "⏳ Expires", f"{expiry_text(vps_info['expires_at'])}\nYou will get a reminder before it expires. Contact the administrator to extend your expiry.", False)
            if pinggy_address:
                tunnel_host, tunnel_port = pinggy_address.split(":")
                add_field(dm_embed, "🔑 SSH Login", f"**Host:** `{tunnel_host}`\n**Port:** `{tunnel_port}`\n**User:** `root`\n**Password:** `{root_password}`\n\n```ssh root@{tunnel_host} -p {tunnel_port}```", False)
            else:
                add_field(dm_embed, "🔑 Root Password", f"`{root_password}`\n⚠️ SSH tunnel setup failed — use `{PREFIX}manage` → 🔌 Reconnect Tunnel to try again.", False)
            add_field(dm_embed, "Management", f"• Use `{PREFIX}manage` to start/stop/reinstall your VPS\n• Use `{PREFIX}manage` → 🌐 SSHX for SSH login + web terminal link\n• Contact admin for upgrades or issues", False)
            add_field(dm_embed, "Important Notes", "• Full root access via SSH\n• Docker-ready with nesting and privileged mode\n• Back up your data regularly\n• Change your root password after first login", False)
            try:
                await self.user.send(embed=dm_embed)
            except discord.Forbidden:
                await self.ctx.send(embed=create_info_embed("Notification Failed", f"Couldn't send DM to {self.user.mention}. Please ensure DMs are enabled."))
        except Exception as e:
            error_embed = create_error_embed("Creation Failed", f"Error: {str(e)}")
            await interaction.followup.send(embed=error_embed)

@bot.command(name='create')
@is_admin()
async def create_vps(ctx, ram: int, cpu: int, disk: int, user: discord.Member, days: int = None):
    """!create <ram_gb> <cpu> <disk_gb> @user [expiry_days]  (days: 0 = no expiry, omitted = !expiry-default)"""
    if ram <= 0 or cpu <= 0 or disk <= 0:
        await ctx.send(embed=create_error_embed("Invalid Specs", "RAM, CPU, and Disk must be positive integers."))
        return
    if days is not None and (days < 0 or days > 3650):
        await ctx.send(embed=create_error_embed("Invalid Days", "Expiry days must be between 0 (no expiry) and 3650."))
        return
    if days is None:
        dflt = int(get_setting('default_expiry_days', '0'))
        exp_txt = f"default expiry ({dflt} days)" if dflt > 0 else "no expiry"
    else:
        exp_txt = "no expiry" if days == 0 else f"expires in **{days} days**"
    embed = create_info_embed("VPS Creation", f"Creating VPS for {user.mention} with {ram}GB RAM, {cpu} CPU cores, {disk}GB Disk, {exp_txt}.\nSelect node below.")
    view = NodeSelectView(ram, cpu, disk, user, ctx, days)
    await ctx.send(embed=embed, view=view)

class ReinstallOSSelectView(discord.ui.View):
    def __init__(self, parent_view, container_name, owner_id, actual_idx, ram_gb, cpu, storage_gb, node_id):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.container_name = container_name
        self.owner_id = owner_id
        self.actual_idx = actual_idx
        self.ram_gb = ram_gb
        self.cpu = cpu
        self.storage_gb = storage_gb
        self.node_id = node_id
        self.select = discord.ui.Select(
            placeholder="Select an OS for the reinstall",
            options=[discord.SelectOption(label=o["label"], value=o["value"]) for o in OS_OPTIONS]
        )
        self.select.callback = self.select_os
        self.add_item(self.select)

    async def select_os(self, interaction: discord.Interaction):
        os_version = self.select.values[0]
        self.select.disabled = True
        creating_embed = create_info_embed("Reinstalling VPS", f"Deploying {os_version} for `{self.container_name}`...")
        await interaction.response.edit_message(embed=creating_embed, view=self)
        ram_mb = self.ram_gb * 1024
        try:
            # No need to delete again; already deleted in confirmation
            await execute_lxc(self.container_name, f"init {os_version} {self.container_name} -s {DEFAULT_STORAGE_POOL}", node_id=self.node_id)
            await execute_lxc(self.container_name, f"config set {self.container_name} limits.memory {ram_mb}MB", node_id=self.node_id)
            await execute_lxc(self.container_name, f"config set {self.container_name} limits.cpu {self.cpu}", node_id=self.node_id)
            await execute_lxc(self.container_name, f"config device set {self.container_name} root size={self.storage_gb}GB", node_id=self.node_id)
            await apply_lxc_config(self.container_name, self.node_id)
            await execute_lxc(self.container_name, f"start {self.container_name}", node_id=self.node_id)
            await apply_internal_permissions(self.container_name, self.node_id)
            root_password = await setup_ssh_access(self.container_name, self.node_id)
            pinggy_address = await establish_ssh_access(self.container_name, self.node_id, self.owner_id)
            target_vps = vps_data[self.owner_id][self.actual_idx]
            target_vps["os_version"] = os_version
            target_vps["status"] = "running"
            target_vps["suspended"] = False
            target_vps["created_at"] = datetime.now().isoformat()
            target_vps["root_password"] = root_password
            target_vps["pinggy_address"] = pinggy_address
            config_str = f"{self.ram_gb}GB RAM / {self.cpu} CPU / {self.storage_gb}GB Disk"
            target_vps["config"] = config_str
            save_vps_data()
            success_embed = create_success_embed("Reinstall Complete", f"VPS `{self.container_name}` has been successfully reinstalled!")
            add_field(success_embed, "Resources", f"**RAM:** {self.ram_gb}GB\n**CPU:** {self.cpu} Cores\n**Storage:** {self.storage_gb}GB", False)
            add_field(success_embed, "OS", os_version, True)
            add_field(success_embed, "Features", "Nesting, Privileged, FUSE, Kernel Modules (Docker Ready), Unprivileged Ports from 0", False)
            add_field(success_embed, "Disk Note", "Run `sudo resize2fs /` inside VPS if needed to expand filesystem.", False)
            await interaction.followup.send(embed=success_embed, ephemeral=True)
            try:
                owner_user = await bot.fetch_user(int(self.owner_id))
                dm_embed = create_success_embed("VPS Reinstalled!", f"Your VPS `{self.container_name}` was reinstalled with a new OS. Your SSH password has changed.")
                if pinggy_address:
                    tunnel_host, tunnel_port = pinggy_address.split(":")
                    add_field(dm_embed, "🔑 SSH Login", f"**Host:** `{tunnel_host}`\n**Port:** `{tunnel_port}`\n**User:** `root`\n**Password:** `{root_password}`\n\n```ssh root@{tunnel_host} -p {tunnel_port}```", False)
                else:
                    add_field(dm_embed, "🔑 Root Password", f"`{root_password}`\n⚠️ SSH tunnel setup failed — use `{PREFIX}manage` → 🔌 Reconnect Tunnel to try again.", False)
                await owner_user.send(embed=dm_embed)
            except Exception:
                pass
            self.stop()
        except Exception as e:
            error_embed = create_error_embed("Reinstall Failed", f"Error: {str(e)}")
            await interaction.followup.send(embed=error_embed, ephemeral=True)
            self.stop()

class ManageView(discord.ui.View):
    def __init__(self, user_id, vps_list, is_shared=False, owner_id=None, is_admin=False, actual_index: Optional[int] = None):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.vps_list = vps_list[:]
        self.selected_index = None
        self.is_shared = is_shared
        self.owner_id = owner_id or user_id
        self.is_admin = is_admin
        self.actual_index = actual_index
        self.indices = list(range(len(vps_list)))
        if self.is_shared and self.actual_index is None:
            raise ValueError("actual_index required for shared views")
        if len(vps_list) > 1:
            options = [
                discord.SelectOption(
                    label=f"VPS {i+1} ({v.get('config', 'Custom')})",
                    description=f"Status: {v.get('status', 'unknown')}",
                    value=str(i)
                ) for i, v in enumerate(vps_list)
            ]
            self.select = discord.ui.Select(placeholder="Select a VPS to manage", options=options)
            self.select.callback = self.select_vps
            self.add_item(self.select)
            self.initial_embed = create_embed("VPS Management", "Select a VPS from the dropdown menu below.", 0x1a1a1a)
            add_field(self.initial_embed, "Available VPS", "\n".join([f"**VPS {i+1}:** `{v['container_name']}` - Status: `{v.get('status', 'unknown').upper()}`" for i, v in enumerate(vps_list)]), False)
        else:
            self.selected_index = 0
            self.initial_embed = None
            self.add_action_buttons()

    async def get_initial_embed(self):
        if self.initial_embed is not None:
            return self.initial_embed
        self.initial_embed = await self.create_vps_embed(self.selected_index)
        return self.initial_embed

    async def create_vps_embed(self, index):
        vps = self.vps_list[index]
        node = get_node(vps['node_id'])
        node_name = node['name'] if node else "Unknown"
        status = vps.get('status', 'unknown')
        suspended = vps.get('suspended', False)
        whitelisted = vps.get('whitelisted', False)
        status_color = 0x00ff88 if status == 'running' and not suspended else 0xffaa00 if suspended else 0xff3366
        container_name = vps['container_name']
        stats = await get_container_stats(container_name)
        status_text = f"{stats['status'].upper()}"
        if suspended:
            status_text += " (SUSPENDED)"
        if whitelisted:
            status_text += " (WHITELISTED)"
        owner_text = ""
        if self.is_admin and self.owner_id != self.user_id:
            try:
                owner_user = await bot.fetch_user(int(self.owner_id))
                owner_text = f"\n**Owner:** {owner_user.mention}"
            except:
                owner_text = f"\n**Owner ID:** {self.owner_id}"
        embed = create_embed(
            f"VPS Management - VPS {index + 1}",
            f"Managing container: `{container_name}` on node {node_name}{owner_text}",
            status_color
        )
        resource_info = f"**Configuration:** {vps.get('config', 'Custom')}\n"
        resource_info += f"**Status:** `{status_text}`\n"
        resource_info += f"**RAM:** {vps['ram']}\n"
        resource_info += f"**CPU:** {vps['cpu']} Cores\n"
        resource_info += f"**Storage:** {vps['storage']}\n"
        resource_info += f"**OS:** {vps.get('os_version', 'ubuntu:22.04')}\n"
        resource_info += f"**Uptime:** {stats['uptime']}"
        add_field(embed, "📊 Allocated Resources", resource_info, False)
        if vps.get('expires_at'):
            add_field(embed, "⏳ Expiry", expiry_text(vps['expires_at']), False)
        if suspended:
            add_field(embed, "⚠️ Suspended", "This VPS is suspended. Contact an admin to unsuspend.", False)
        if whitelisted:
            add_field(embed, "✅ Whitelisted", "This VPS is exempt from auto-suspension.", False)
        live_stats = f"**CPU Usage:** {stats['cpu']:.1f}%\n**Memory:** {stats['ram']['used']}/{stats['ram']['total']} MB ({stats['ram']['pct']:.1f}%)\n**Disk:** {stats['disk']}"
        add_field(embed, "📈 Live Usage", live_stats, False)
        pinggy_address = vps.get('pinggy_address')
        if pinggy_address:
            tunnel_host, tunnel_port = pinggy_address.split(":")
            add_field(embed, "🔌 SSH Tunnel", f"**Host:** `{tunnel_host}`\n**Port:** `{tunnel_port}`\n```ssh root@{tunnel_host} -p {tunnel_port}```", False)
        else:
            add_field(embed, "🔌 SSH Tunnel", f"No active tunnel. Use 🔌 Reconnect Tunnel below.", False)
        add_field(embed, "🎮 Controls", "Use the buttons below to manage your VPS", False)
        return embed

    def add_action_buttons(self):
        if not self.is_shared and not self.is_admin:
            reinstall_button = discord.ui.Button(label="🔄 Reinstall", style=discord.ButtonStyle.danger)
            reinstall_button.callback = lambda inter: self.action_callback(inter, 'reinstall')
            self.add_item(reinstall_button)
        start_button = discord.ui.Button(label="▶ Start", style=discord.ButtonStyle.success)
        start_button.callback = lambda inter: self.action_callback(inter, 'start')
        stop_button = discord.ui.Button(label="⏸ Stop", style=discord.ButtonStyle.secondary)
        stop_button.callback = lambda inter: self.action_callback(inter, 'stop')
        sshx_button = discord.ui.Button(label="🌐 SSHX", style=discord.ButtonStyle.primary)
        sshx_button.callback = lambda inter: self.action_callback(inter, 'sshx')
        stats_button = discord.ui.Button(label="📊 Stats", style=discord.ButtonStyle.secondary)
        stats_button.callback = lambda inter: self.action_callback(inter, 'stats')
        restart_button = discord.ui.Button(label="🔁 Restart", style=discord.ButtonStyle.primary)
        restart_button.callback = lambda inter: self.action_callback(inter, 'restart')
        refresh_button = discord.ui.Button(label="🔃 Refresh", style=discord.ButtonStyle.secondary)
        refresh_button.callback = lambda inter: self.action_callback(inter, 'refresh')
        uptime_button = discord.ui.Button(label="⏱ Uptime", style=discord.ButtonStyle.secondary)
        uptime_button.callback = lambda inter: self.action_callback(inter, 'uptime')
        self.add_item(start_button)
        self.add_item(stop_button)
        self.add_item(restart_button)
        self.add_item(sshx_button)
        self.add_item(stats_button)
        self.add_item(refresh_button)
        self.add_item(uptime_button)
        if not self.is_shared:
            addport_button = discord.ui.Button(label="➕ Add Port", style=discord.ButtonStyle.primary)
            addport_button.callback = lambda inter: self.action_callback(inter, 'addport')
            self.add_item(addport_button)
            changepass_button = discord.ui.Button(label="🔐 Change Password", style=discord.ButtonStyle.danger)
            changepass_button.callback = lambda inter: self.action_callback(inter, 'changepass')
            self.add_item(changepass_button)
            reconnect_button = discord.ui.Button(label="🔌 Reconnect Tunnel", style=discord.ButtonStyle.primary)
            reconnect_button.callback = lambda inter: self.action_callback(inter, 'reconnect_tunnel')
            self.add_item(reconnect_button)

    async def select_vps(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "This is not your VPS!"), ephemeral=True)
            return
        self.selected_index = int(self.select.values[0])
        await interaction.response.defer()
        new_embed = await self.create_vps_embed(self.selected_index)
        self.clear_items()
        self.add_action_buttons()
        await interaction.edit_original_response(embed=new_embed, view=self)

    async def action_callback(self, interaction: discord.Interaction, action: str):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "This is not your VPS!"), ephemeral=True)
            return
        if self.selected_index is None:
            await interaction.response.send_message(embed=create_error_embed("No VPS Selected", "Please select a VPS first."), ephemeral=True)
            return
        if maintenance_on() and not is_admin_id(interaction.user.id) and action not in ('stats', 'refresh', 'uptime'):
            await interaction.response.send_message(embed=create_warning_embed("🛠️ Maintenance Mode", f"VPS operations are temporarily disabled.\n**Reason:** {maintenance_reason()}"), ephemeral=True)
            return
        actual_idx = self.actual_index if self.is_shared else self.indices[self.selected_index]
        expected_container = self.vps_list[self.selected_index]['container_name']
        owner_vps_list = vps_data.get(self.owner_id, [])
        if actual_idx >= len(owner_vps_list) or owner_vps_list[actual_idx].get('container_name') != expected_container:
            await interaction.response.send_message(embed=create_error_embed("VPS Changed", "This VPS was transferred, deleted or changed. Run the command again to open a fresh panel."), ephemeral=True)
            return
        target_vps = vps_data[self.owner_id][actual_idx]
        suspended = target_vps.get('suspended', False)
        if suspended and not self.is_admin and action not in ('stats', 'refresh', 'uptime'):
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "This VPS is suspended. Contact an admin to unsuspend."), ephemeral=True)
            return
        container_name = target_vps["container_name"]
        node_id = target_vps['node_id']
        if action == 'stats':
            stats = await get_container_stats(container_name, node_id)
            stats_embed = create_info_embed("📈 Live Statistics", f"Real-time stats for `{container_name}`")
            add_field(stats_embed, "Status", f"`{stats['status'].upper()}`", True)
            add_field(stats_embed, "CPU", f"{stats['cpu']:.1f}%", True)
            add_field(stats_embed, "Memory", f"{stats['ram']['used']}/{stats['ram']['total']} MB ({stats['ram']['pct']:.1f}%)", True)
            add_field(stats_embed, "Disk", stats['disk'], True)
            add_field(stats_embed, "Uptime", stats['uptime'], True)
            await interaction.response.send_message(embed=stats_embed, ephemeral=True)
            return
        if action == 'refresh':
            await interaction.response.defer()
            try:
                stats = await get_container_stats(container_name, node_id)
                live_status = str(stats.get('status', '')).lower()
                if live_status in ('running', 'stopped'):
                    target_vps['status'] = live_status
                    save_vps_data()
            except Exception as e:
                logger.error(f"Refresh failed for {container_name}: {e}")
            new_embed = await self.create_vps_embed(self.selected_index)
            await interaction.edit_original_response(embed=new_embed, view=self)
            return
        if action == 'uptime':
            await interaction.response.defer(ephemeral=True)
            bot_up = format_duration(get_bot_uptime_seconds())
            vps_secs = await get_container_uptime_seconds(container_name, node_id)
            vps_up = format_duration(vps_secs) if vps_secs is not None else "Unknown (VPS stopped?)"
            host_secs = get_host_uptime_seconds()
            uptime_embed = create_info_embed("⏱ Uptime", f"Uptime info for `{container_name}`")
            add_field(uptime_embed, "🤖 Bot Uptime", f"`{bot_up}`", True)
            add_field(uptime_embed, "🖥️ VPS Uptime", f"`{vps_up}`", True)
            if host_secs is not None:
                add_field(uptime_embed, "🏠 Host Uptime", f"`{format_duration(host_secs)}`", True)
            add_field(uptime_embed, "🚀 Bot Started", f"<t:{int(BOT_START_TIME)}:F> (<t:{int(BOT_START_TIME)}:R>)", False)
            await interaction.followup.send(embed=uptime_embed, ephemeral=True)
            return
        if action == 'reinstall':
            if self.is_shared or self.is_admin:
                await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the VPS owner can reinstall!"), ephemeral=True)
                return
            if suspended:
                await interaction.response.send_message(embed=create_error_embed("Cannot Reinstall", "Unsuspend the VPS first."), ephemeral=True)
                return
            ram_gb = int(target_vps['ram'].replace('GB', ''))
            cpu = int(target_vps['cpu'])
            storage_gb = int(target_vps['storage'].replace('GB', ''))
            confirm_embed = create_warning_embed("Reinstall Warning",
                f"⚠️ **WARNING:** This will erase all data on VPS `{container_name}` and reinstall a fresh OS.\n\n"
                f"This action cannot be undone. Continue?")
            class ConfirmView(discord.ui.View):
                def __init__(self, parent_view, container_name, owner_id, actual_idx, ram_gb, cpu, storage_gb, node_id):
                    super().__init__(timeout=60)
                    self.parent_view = parent_view
                    self.container_name = container_name
                    self.owner_id = owner_id
                    self.actual_idx = actual_idx
                    self.ram_gb = ram_gb
                    self.cpu = cpu
                    self.storage_gb = storage_gb
                    self.node_id = node_id

                @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
                async def confirm(self, inter: discord.Interaction, item: discord.ui.Button):
                    await inter.response.defer(ephemeral=True)
                    try:
                        await inter.followup.send(embed=create_info_embed("Deleting Container", f"Forcefully removing container `{self.container_name}`..."), ephemeral=True)
                        await execute_lxc(self.container_name, f"delete {self.container_name} --force", node_id=self.node_id)
                        os_view = ReinstallOSSelectView(self.parent_view, self.container_name, self.owner_id, self.actual_idx, self.ram_gb, self.cpu, self.storage_gb, self.node_id)
                        await inter.followup.send(embed=create_info_embed("Select OS", "Choose the new OS for reinstallation."), view=os_view, ephemeral=True)
                    except Exception as e:
                        await inter.followup.send(embed=create_error_embed("Delete Failed", f"Error: {str(e)}"), ephemeral=True)

                @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
                async def cancel(self, inter: discord.Interaction, item: discord.ui.Button):
                    new_embed = await self.parent_view.create_vps_embed(self.parent_view.selected_index)
                    await inter.response.edit_message(embed=new_embed, view=self.parent_view)

            await interaction.response.send_message(embed=confirm_embed, view=ConfirmView(self, container_name, self.owner_id, actual_idx, ram_gb, cpu, storage_gb, node_id), ephemeral=True)
            return
        if action == 'addport':
            owner_id = self.owner_id

            class AddPortModal(discord.ui.Modal, title="Add Port Forward"):
                vps_port_input = discord.ui.TextInput(label="VPS Port (1-65535)", placeholder="e.g. 8080", max_length=5)

                async def on_submit(self, modal_inter: discord.Interaction):
                    await modal_inter.response.defer(ephemeral=True)
                    try:
                        vps_port = int(self.vps_port_input.value)
                        if vps_port < 1 or vps_port > 65535:
                            raise ValueError
                    except ValueError:
                        await modal_inter.followup.send(embed=create_error_embed("Invalid Port", "Port must be a number between 1 and 65535."), ephemeral=True)
                        return
                    allocated = get_user_allocation(owner_id)
                    used = get_user_used_ports(owner_id)
                    if used >= allocated:
                        await modal_inter.followup.send(embed=create_error_embed("Quota Exceeded", f"No available port slots. Allocated: {allocated}, Used: {used}. Contact an admin for more."), ephemeral=True)
                        return
                    try:
                        host_port = await create_port_forward(owner_id, container_name, vps_port, node_id)
                    except ValueError as ve:
                        await modal_inter.followup.send(embed=create_error_embed("Not possible", str(ve)), ephemeral=True)
                        return
                    if host_port:
                        public_ip = get_public_ip()
                        success_embed = create_success_embed("Port Forward Created", f"VPS port `{vps_port}` forwarded to host port `{host_port}` (TCP & UDP).")
                        add_field(success_embed, "Access", f"`{public_ip}:{host_port}` → VPS:{vps_port}", False)
                        await modal_inter.followup.send(embed=success_embed, ephemeral=True)
                        try:
                            dm_user = await bot.fetch_user(int(owner_id))
                            dm_embed = create_success_embed("🔌 Port Forward Created", f"A new port forward was created for your VPS `{container_name}`.")
                            add_field(dm_embed, "Details", f"**VPS Port:** {vps_port}\n**External Port:** {host_port}\n**Access:** `{public_ip}:{host_port}` (TCP & UDP)", False)
                            await dm_user.send(embed=dm_embed)
                        except discord.Forbidden:
                            pass
                    else:
                        await modal_inter.followup.send(embed=create_error_embed("Failed", "Could not assign a host port. Try again later."), ephemeral=True)

            await interaction.response.send_modal(AddPortModal())
            return
        await interaction.response.defer(ephemeral=True)
        if self.is_admin and str(self.owner_id) != str(interaction.user.id):
            log_audit(interaction.user.id, str(interaction.user), f"manage:{action}", container_name, f"owner id {self.owner_id}")
        suspended = target_vps.get('suspended', False)
        if suspended:
            target_vps['suspended'] = False
            save_vps_data()
        if action == 'start':
            try:
                ok, err = await check_node_ipv4(node_id, for_start=True)
                if not ok:
                    await interaction.followup.send(embed=create_error_embed("This VPS is not IPv4", err), ephemeral=True)
                await safe_start_container(container_name, node_id)
                target_vps["status"] = "running"
                save_vps_data()
                await apply_internal_permissions(container_name, node_id)
                readded = await recreate_port_forwards(container_name)
                await interaction.followup.send(embed=create_success_embed("VPS Started", f"VPS `{container_name}` is now running! Re-added {readded} port forwards."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Start Failed", str(e)), ephemeral=True)
        elif action == 'restart':
            try:
                ok, err = await check_node_ipv4(node_id, for_start=True)
                if not ok:
                    await interaction.followup.send(embed=create_error_embed("This VPS is not IPv4", err), ephemeral=True)
                await interaction.followup.send(embed=create_info_embed("🔁 Restarting", f"Restarting `{container_name}`, please wait..."), ephemeral=True)
                try:
                    await execute_lxc(container_name, f"stop {container_name}", timeout=120, node_id=node_id)
                except Exception as stop_err:
                    if "not running" not in str(stop_err).lower() and "stopped" not in str(stop_err).lower():
                        raise
                await safe_start_container(container_name, node_id)
                target_vps["status"] = "running"
                save_vps_data()
                await apply_internal_permissions(container_name, node_id)
                readded = await recreate_port_forwards(container_name)
                await interaction.followup.send(embed=create_success_embed("VPS Restarted", f"VPS `{container_name}` restarted successfully! Re-added {readded} port forwards."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Restart Failed", str(e)), ephemeral=True)
        elif action == 'stop':
            try:
                await execute_lxc(container_name, f"stop {container_name}", timeout=120, node_id=node_id)
                target_vps["status"] = "stopped"
                save_vps_data()
                await interaction.followup.send(embed=create_success_embed("VPS Stopped", f"VPS `{container_name}` has been stopped!"), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Stop Failed", str(e)), ephemeral=True)
        elif action == 'sshx':
            if suspended:
                await interaction.followup.send(embed=create_error_embed("Access Denied", "Cannot access suspended VPS."), ephemeral=True)
                return
            if target_vps.get('status') != 'running':
                await interaction.followup.send(embed=create_error_embed("VPS Not Running", "Start the VPS first, then use SSHX."), ephemeral=True)
                return
            await interaction.followup.send(embed=create_info_embed("🌐 SSHX", "Setting up SSHX web terminal, this can take up to 40 seconds..."), ephemeral=True)
            try:
                sshx_link = await establish_sshx_session(container_name, node_id)
                pinggy_address = target_vps.get('pinggy_address')
                if not sshx_link and not pinggy_address:
                    await interaction.followup.send(embed=create_error_embed("SSHX Failed", "Could not create an SSHX link and there is no SSH tunnel. Make sure the VPS is running and try again."), ephemeral=True)
                    return
                dm_embed = create_success_embed("🌐 SSH + SSHX Access", f"Access details for VPS `{container_name}`")
                if pinggy_address:
                    tunnel_host, tunnel_port = pinggy_address.split(":")
                    ssh_text = f"**Host:** `{tunnel_host}`\n**Port:** `{tunnel_port}`\n**User:** `root`\n"
                    if not self.is_shared and target_vps.get('root_password'):
                        ssh_text += f"**Password:** `{target_vps['root_password']}`\n"
                    ssh_text += f"\n```ssh root@{tunnel_host} -p {tunnel_port}```"
                    add_field(dm_embed, "🔑 SSH Login", ssh_text, False)
                else:
                    add_field(dm_embed, "🔑 SSH Login", f"No active SSH tunnel. Use 🔌 Reconnect Tunnel to create one.", False)
                if sshx_link:
                    add_field(dm_embed, "🌐 SSHX Web Terminal", f"Open in your browser:\n{sshx_link}", False)
                else:
                    add_field(dm_embed, "🌐 SSHX Web Terminal", "Could not generate the SSHX link right now. Press 🌐 SSHX again.", False)
                add_field(dm_embed, "⚠️ Security", "These details are private. Do not share them.", False)
                try:
                    await interaction.user.send(embed=dm_embed)
                    await interaction.followup.send(embed=create_success_embed("SSHX Ready", "Check your DMs for the SSH login and SSHX link!"), ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send(embed=dm_embed, ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("SSHX Error", str(e)), ephemeral=True)
        elif action == 'changepass':
            try:
                new_password = generate_password()
                await execute_lxc(container_name, f"exec {container_name} -- bash -c \"echo 'root:{new_password}' | chpasswd\"", node_id=node_id, timeout=60)
                target_vps['root_password'] = new_password
                save_vps_data()
                try:
                    dm_user = await bot.fetch_user(int(self.owner_id))
                    dm_embed = create_success_embed("🔐 Root Password Changed", f"Your root password for VPS `{container_name}` has been changed.")
                    add_field(dm_embed, "New Password", f"`{new_password}`", False)
                    add_field(dm_embed, "⚠️ Security", "Keep this password private. Do not share it.", False)
                    await dm_user.send(embed=dm_embed)
                    await interaction.followup.send(embed=create_success_embed("Password Changed", "Check your DMs for the new password!"), ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send(embed=create_success_embed("Password Changed", f"New password: `{new_password}`\n(Enable DMs to receive future passwords privately)"), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Password Change Failed", str(e)), ephemeral=True)
        elif action == 'reconnect_tunnel':
            try:
                await interaction.followup.send(embed=create_info_embed("Reconnecting", "Setting up a fresh SSH tunnel, this can take up to 30 seconds..."), ephemeral=True)
                new_address = await establish_ssh_access(container_name, node_id, self.owner_id)
                target_vps['pinggy_address'] = new_address
                save_vps_data()
                if new_address:
                    tunnel_host, tunnel_port = new_address.split(":")
                    try:
                        dm_user = await bot.fetch_user(int(self.owner_id))
                        dm_embed = create_success_embed("🔌 SSH Tunnel Reconnected", f"A fresh SSH tunnel for VPS `{container_name}` is ready.")
                        add_field(dm_embed, "🔑 SSH Login", f"**Host:** `{tunnel_host}`\n**Port:** `{tunnel_port}`\n**User:** `root`\n\n```ssh root@{tunnel_host} -p {tunnel_port}```", False)
                        await dm_user.send(embed=dm_embed)
                        await interaction.followup.send(embed=create_success_embed("Tunnel Reconnected", "Check your DMs for the new address!"), ephemeral=True)
                    except discord.Forbidden:
                        await interaction.followup.send(embed=create_success_embed("Tunnel Reconnected", f"**Host:** `{tunnel_host}`\n**Port:** `{tunnel_port}`\n(Enable DMs to receive future addresses privately)"), ephemeral=True)
                else:
                    await interaction.followup.send(embed=create_error_embed("Reconnect Failed", "Could not establish a tunnel. Make sure the VPS is running and try again."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Reconnect Failed", str(e)), ephemeral=True)
        new_embed = await self.create_vps_embed(self.selected_index)
        await interaction.edit_original_response(embed=new_embed, view=self)

@bot.command(name='manage')
async def manage_vps(ctx, user: discord.Member = None):
    if user:
        if str(ctx.author.id) not in main_admin_ids and str(ctx.author.id) not in admin_data.get("admins", []):
            await ctx.send(embed=create_error_embed("Access Denied", "Only admins can manage other users' VPS."))
            return
        user_id = str(user.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            await ctx.send(embed=create_error_embed("No VPS Found", f"{user.mention} doesn't have any {BOT_NAME} VPS."))
            return
        view = ManageView(str(ctx.author.id), vps_list, is_admin=True, owner_id=user_id)
        await ctx.send(embed=create_info_embed(f"Managing {user.name}'s VPS", f"Managing VPS for {user.mention}"), view=view)
    else:
        user_id = str(ctx.author.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            embed = create_error_embed("No VPS Found", f"You don't have any {BOT_NAME} VPS. Contact an admin to create one.")
            add_field(embed, "Quick Actions", f"• `{PREFIX}manage` - Manage VPS\n• Contact admin for VPS creation", False)
            await ctx.send(embed=embed)
            return
        view = ManageView(user_id, vps_list)
        embed = await view.get_initial_embed()
        await ctx.send(embed=embed, view=view)

async def get_node_status(node_id: int) -> str:
    node = get_node(node_id)
    if not node:
        return "❓ Unknown"
    if node['is_local']:
        return "🟢 Online (Local)"
    try:
        response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
        if response.status_code == 200:
            return "🟢 Online"
        else:
            return "🔴 Offline"
    except Exception as e:
        logger.error(f"Failed to ping node {node['name']}: {e}")
        return "🔴 Offline"


def get_host_disk_usage():
    try:
        result = subprocess.run(['df', '-h', '/'], capture_output=True, text=True)
        lines = result.stdout.splitlines()
        if len(lines) > 1:
            parts = lines[1].split()
            total = parts[1]
            used = parts[2]
            perc = parts[4]
            return f"{used}/{total} ({perc})"
        return "Unknown"
    except Exception as e:
        logger.error(f"Error getting disk usage: {e}")
        return "Unknown"


async def get_host_stats(node_id: int) -> Dict:
    node = get_node(node_id)
    if node['is_local']:
        return {
            "cpu": get_host_cpu_usage(),
            "ram": get_host_ram_usage(),
            "disk": get_host_disk_usage()
        }
    else:
        url = f"{node['url']}/api/get_host_stats"
        params = {"api_key": node["api_key"]}
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            stats = response.json()
            # Fallbacks if remote API doesn't provide
            stats['disk'] = stats.get('disk', 'Unknown')
            return stats
        except Exception as e:
            logger.error(f"Failed to get host stats from node {node['name']}: {e}")
            return {"cpu": 0.0, "ram": 0.0, "disk": "Unknown"}


@bot.command(name='vps-list')
@is_admin()
async def vps_list(ctx, node_id: int = 1):
    node = get_node(node_id)
    if not node:
        await ctx.send(embed=create_error_embed("Node Not Found", f"Node ID {node_id} not found."))
        return

    # Get node status
    status = await get_node_status(node_id)
    is_online = status.startswith("🟢")

    # Get node resource stats (will use defaults if offline)
    stats = await get_host_stats(node_id)
    cpu_usage = stats.get('cpu', 0.0)
    ram_usage = stats.get('ram', 0.0)
    disk_usage = stats.get('disk', 'Unknown')

    # Resources field text (modern: compact inline stats with progress-like emojis)
    if is_online:
        resources_text = (
            f"**CPU** {cpu_usage:.0f}% {'█' * int(cpu_usage / 5) + '░' * (20 - int(cpu_usage / 5))} "
            f"\n**RAM** {ram_usage:.0f}% {'█' * int(ram_usage / 5) + '░' * (20 - int(ram_usage / 5))} "
            f"\n**Disk** {disk_usage}"
        )
    else:
        resources_text = "⚠️ Resources unavailable (Offline)"

    # Get VPS capacity
    current_vps = get_current_vps_count(node_id)
    total_capacity = node['total_vps']
    capacity_percent = (current_vps / total_capacity * 100) if total_capacity > 0 else 0
    capacity_text = f"{current_vps}/{total_capacity} ({capacity_percent:.0f}%)"

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM vps WHERE node_id = ?', (node_id,))
    rows = cur.fetchall()
    conn.close()

    total_vps = len(rows)

    # Modern counters: use more intuitive emojis and clean layout
    running = 0
    stopped = 0
    suspended = 0
    other = 0
    vps_info = []
    for i, row in enumerate(rows, 1):
        vps = dict(row)
        user_id = vps['user_id']
        try:
            user = await bot.fetch_user(int(user_id))
            username = user.name
        except:
            username = f"Unknown ({user_id})"

        status = vps.get('status', 'unknown')
        suspended_flag = vps.get('suspended', False)

        # Count logic: suspended first, then status if not suspended
        if suspended_flag:
            suspended += 1
        elif status == 'running':
            running += 1
        elif status == 'stopped':
            stopped += 1
        else:
            other += 1

        # Modern emoji: vibrant and status-specific
        status_emoji = "🟢" if status == 'running' and not suspended_flag else "🟡" if suspended_flag else "🔴"
        vps_status = status.upper()
        if suspended_flag:
            vps_status += " (SUSPENDED)"
        if vps.get('whitelisted', False):
            vps_status += " (WHITELISTED)"
        config = vps.get('config', 'Custom')
        vps_info.append(f"{status_emoji} **{i}.** {username} • `{vps['container_name']}`\n _{vps_status} | {config}_")

    # Create main embed (modern: gradient-inspired colors, clean typography)
    color = 0x10b981 if is_online else 0xef4444  # Teal green / Soft red for modern feel
    embed = create_embed(
        title=f"🖥️ VPS Dashboard - {node['name']}",
        description=f"**ID:** `{node_id}` | **Region:** {node['location']}\n*Updated: <t:{int(datetime.now().timestamp())}:R>*",
        color=color
    )
    embed.set_thumbnail(url=node.get('thumbnail_url', None))

    # Inline status and capacity for compact top row
    add_field(embed, "📡 **Status**", status, True)
    add_field(embed, "🗄️ **Capacity**", capacity_text, True)

    # Resources field with modern bar visualization
    add_field(embed, "📊 **Resources**", resources_text, False)

    # Summary field (modern: compact bullet-like with inline emojis)
    summary_text = (
        f"**Total:** {total_vps} 📊\n"
        f"**Running:** {running} 🟢\n"
        f"**Stopped:** {stopped} ⏸️\n"
        f"**Suspended:** {suspended} 🟡"
    )
    if other > 0:
        summary_text += f"\n**Other:** {other} ⚠️"
    add_field(embed, "📈 **Summary**", summary_text, True)

    # VPS List - chunked embeds with modern pagination
    if vps_info:
        chunk_size = 6  # Smaller chunks for cleaner mobile-friendly embeds
        chunks = [vps_info[i:i + chunk_size] for i in range(0, len(vps_info), chunk_size)]
        first_chunk_text = "\n".join(chunks[0])
        add_field(embed, "📋 **Active VPS (1/{len(chunks)})**", f"```{first_chunk_text}```", False)

        # Paginated follow-ups with consistent styling
        for idx, chunk in enumerate(chunks[1:], 2):
            page_embed = create_embed(
                title=f"🖥️ VPS Dashboard - {node['name']} (Page {idx}/{len(chunks)})",
                description=f"**ID:** `{node_id}` | **Region:** {node['location']}\n*Updated: <t:{int(datetime.now().timestamp())}:R>*",
                color=color
            )
            chunk_text = "\n".join(chunk)
            add_field(page_embed, "📋 **VPS List**", f"```{chunk_text}```", False)
            page_embed.set_footer(text=f"Total: {total_vps} VPS | Powered by Your Bot")
            await ctx.send(embed=page_embed)
    else:
        add_field(embed, "📋 **VPS List**", "No deployments yet. Launch one! 🚀", False)

    embed.set_footer(text=f"Refresh with {PREFIX}vps-list {node_id} | {len(vps_info)} shown")
    await ctx.send(embed=embed)

@bot.command(name='list-all')
@is_admin()
async def list_all_vps(ctx):
    total_vps = 0
    total_users = len(vps_data)
    running_vps = 0
    stopped_vps = 0
    suspended_vps = 0
    whitelisted_vps = 0
    vps_info = []
    user_summary = []
    for user_id, vps_list in vps_data.items():
        try:
            user = await bot.fetch_user(int(user_id))
            user_vps_count = len(vps_list)
            user_running = sum(1 for vps in vps_list if vps.get('status') == 'running' and not vps.get('suspended', False))
            user_stopped = sum(1 for vps in vps_list if vps.get('status') == 'stopped')
            user_suspended = sum(1 for vps in vps_list if vps.get('suspended', False))
            user_whitelisted = sum(1 for vps in vps_list if vps.get('whitelisted', False))
            total_vps += user_vps_count
            running_vps += user_running
            stopped_vps += user_stopped
            suspended_vps += user_suspended
            whitelisted_vps += user_whitelisted
            user_summary.append(f"**{user.name}** ({user.mention}) - {user_vps_count} VPS ({user_running} running, {user_suspended} suspended, {user_whitelisted} whitelisted)")
            for i, vps in enumerate(vps_list):
                node = get_node(vps['node_id'])
                node_name = node['name'] if node else "Unknown"
                status_emoji = "🟢" if vps.get('status') == 'running' and not vps.get('suspended', False) else "🟡" if vps.get('suspended', False) else "🔴"
                status_text = vps.get('status', 'unknown').upper()
                if vps.get('suspended', False):
                    status_text += " (SUSPENDED)"
                if vps.get('whitelisted', False):
                    status_text += " (WHITELISTED)"
                vps_info.append(f"{status_emoji} **{user.name}** - VPS {i+1}: `{vps['container_name']}` - {vps.get('config', 'Custom')} - {status_text} (Node: {node_name})")
        except discord.NotFound:
            vps_info.append(f"❓ Unknown User ({user_id}) - {len(vps_list)} VPS")
    embed = create_embed("All VPS Information", "Complete overview of all VPS deployments and user statistics", 0x1a1a1a)
    add_field(embed, "System Overview", f"**Total Users:** {total_users}\n**Total VPS:** {total_vps}\n**Running:** {running_vps}\n**Stopped:** {stopped_vps}\n**Suspended:** {suspended_vps}\n**Whitelisted:** {whitelisted_vps}", False)
    await ctx.send(embed=embed)
    if user_summary:
        embed = create_embed("User Summary", f"Summary of all users and their VPS", 0x1a1a1a)
        summary_text = "\n".join(user_summary)
        chunks = [summary_text[i:i+1024] for i in range(0, len(summary_text), 1024)]
        for idx, chunk in enumerate(chunks, 1):
            add_field(embed, f"Users (Part {idx})", chunk, False)
        await ctx.send(embed=embed)
    if vps_info:
        vps_text = "\n".join(vps_info)
        chunks = [vps_text[i:i+1024] for i in range(0, len(vps_text), 1024)]
        for idx, chunk in enumerate(chunks, 1):
            embed = create_embed(f"VPS Details (Part {idx})", "List of all VPS deployments", 0x1a1a1a)
            add_field(embed, "VPS List", chunk, False)
            await ctx.send(embed=embed)

@bot.command(name='manage-shared')
async def manage_shared_vps(ctx, owner: discord.Member, vps_number: int):
    owner_id = str(owner.id)
    user_id = str(ctx.author.id)
    if owner_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[owner_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or owner doesn't have a VPS."))
        return
    vps = vps_data[owner_id][vps_number - 1]
    if user_id not in vps.get("shared_with", []):
        await ctx.send(embed=create_error_embed("Access Denied", "You do not have access to this VPS."))
        return
    view = ManageView(user_id, [vps], is_shared=True, owner_id=owner_id, actual_index=vps_number - 1)
    embed = await view.get_initial_embed()
    await ctx.send(embed=embed, view=view)

@bot.command(name='share-user')
async def share_user(ctx, shared_user: discord.Member, vps_number: int):
    user_id = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or you don't have a VPS."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps:
        vps["shared_with"] = []
    if shared_user_id in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Already Shared", f"{shared_user.mention} already has access to this VPS!"))
        return
    vps["shared_with"].append(shared_user_id)
    save_vps_data()
    await ctx.send(embed=create_success_embed("VPS Shared", f"VPS #{vps_number} shared with {shared_user.mention}!"))
    try:
        await shared_user.send(embed=create_embed("VPS Access Granted", f"You have access to VPS #{vps_number} from {ctx.author.mention}. Use `{PREFIX}manage-shared {ctx.author.mention} {vps_number}`", 0x00ff88))
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("Notification Failed", f"Could not DM {shared_user.mention}"))

@bot.command(name='share-ruser')
async def revoke_share(ctx, shared_user: discord.Member, vps_number: int):
    user_id = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or you don't have a VPS."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps:
        vps["shared_with"] = []
    if shared_user_id not in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Not Shared", f"{shared_user.mention} doesn't have access to this VPS!"))
        return
    vps["shared_with"].remove(shared_user_id)
    save_vps_data()
    await ctx.send(embed=create_success_embed("Access Revoked", f"Access to VPS #{vps_number} revoked from {shared_user.mention}!"))
    try:
        await shared_user.send(embed=create_embed("VPS Access Revoked", f"Your access to VPS #{vps_number} by {ctx.author.mention} has been revoked.", 0xff3366))
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("Notification Failed", f"Could not DM {shared_user.mention}"))

@bot.command(name='ports-add-user')
@is_admin()
async def ports_add_user(ctx, amount: int, user: discord.Member):
    if amount <= 0:
        await ctx.send(embed=create_error_embed("Invalid Amount", "Amount must be a positive integer."))
        return
    user_id = str(user.id)
    allocate_ports(user_id, amount)
    embed = create_success_embed("Ports Allocated", f"Allocated {amount} port slots to {user.mention}.")
    add_field(embed, "Quota", f"Total: {get_user_allocation(user_id)} slots", False)
    await ctx.send(embed=embed)
    try:
        dm_embed = create_info_embed("Port Slots Allocated", f"You have been granted {amount} additional port forwarding slots by an admin.\nUse `{PREFIX}ports list` to view your quota and active forwards.")
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("DM Failed", f"Could not notify {user.mention} via DM."))

@bot.command(name='ports-remove-user')
@is_admin()
async def ports_remove_user(ctx, amount: int, user: discord.Member):
    if amount <= 0:
        await ctx.send(embed=create_error_embed("Invalid Amount", "Amount must be a positive integer."))
        return
    user_id = str(user.id)
    current = get_user_allocation(user_id)
    if amount > current:
        amount = current
    deallocate_ports(user_id, amount)
    remaining = get_user_allocation(user_id)
    embed = create_success_embed("Ports Deallocated", f"Removed {amount} port slots from {user.mention}.")
    add_field(embed, "Remaining Quota", f"{remaining} slots", False)
    await ctx.send(embed=embed)
    try:
        dm_embed = create_warning_embed("Port Slots Reduced", f"Your port forwarding quota has been reduced by {amount} slots by an admin.\nRemaining: {remaining} slots.")
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("DM Failed", f"Could not notify {user.mention} via DM."))

@bot.command(name='ports-revoke')
@is_admin()
async def ports_revoke(ctx, forward_id: int):
    success, user_id = await remove_port_forward(forward_id, is_admin=True)
    if success and user_id:
        try:
            user = await bot.fetch_user(int(user_id))
            dm_embed = create_warning_embed("Port Forward Revoked", f"One of your port forwards (ID: {forward_id}) has been revoked by an admin.")
            await user.send(embed=dm_embed)
        except:
            pass
        await ctx.send(embed=create_success_embed("Revoked", f"Port forward ID {forward_id} revoked."))
    else:
        await ctx.send(embed=create_error_embed("Failed", "Port forward ID not found or removal failed."))

@bot.command(name='ports')
async def ports_command(ctx, subcmd: str = None, *args):
    user_id = str(ctx.author.id)
    allocated = get_user_allocation(user_id)
    used = get_user_used_ports(user_id)
    available = allocated - used
    if subcmd is None:
        embed = create_info_embed("Port Forwarding Help", f"**Your Quota:** Allocated: {allocated}, Used: {used}, Available: {available}")
        add_field(embed, "Commands", f"{PREFIX}ports add <vps_num> <port>\n{PREFIX}ports add <vps_num> <port> same  (same port on the host - Wings/game servers)\n{PREFIX}ports add <vps_num> <port> <host_port>\n{PREFIX}ports add <vps_num> 25565-25570  (range)\n{PREFIX}ports list\n{PREFIX}ports remove <id>", False)
        await ctx.send(embed=embed)
        return
    if subcmd == 'add':
        usage = (f"Usage:\n`{PREFIX}ports add <vps_number> <vps_port>` - random host port\n"
                 f"`{PREFIX}ports add <vps_number> <vps_port> same` - SAME host port (Pterodactyl Wings 8080/2022, game servers)\n"
                 f"`{PREFIX}ports add <vps_number> <vps_port> <host_port>` - choose the host port (1024-65535)\n"
                 f"`{PREFIX}ports add <vps_number> 25565-25570` - a range, same ports on the host (1 quota slot per port, max 50)")
        if len(args) < 2:
            await ctx.send(embed=create_error_embed("Usage", usage))
            return
        try:
            vps_num = int(args[0])
            m = re.fullmatch(r'(\d+)-(\d+)', args[1])
            if m:
                first, last = int(m.group(1)), int(m.group(2))
                if first > last or last - first + 1 > 50:
                    raise ValueError
                port_pairs = [(p_, p_) for p_ in range(first, last + 1)]   # range: always the same port on the host
            else:
                vps_port = int(args[1])
                extra = args[2].lower() if len(args) > 2 else None
                if extra is None:
                    host_choice = None
                elif extra in ('same', 'same-port', 'samee'):
                    host_choice = vps_port
                else:
                    host_choice = int(extra)
                port_pairs = [(vps_port, host_choice)]
            if any(vp < 1 or vp > 65535 for vp, _ in port_pairs):
                raise ValueError
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid Input", "VPS number and ports must be integers (port: 1-65535, range max 50 ports).\n\n" + usage))
            return
        vps_list = vps_data.get(user_id, [])
        if vps_num < 1 or vps_num > len(vps_list):
            await ctx.send(embed=create_error_embed("Invalid VPS", f"Invalid VPS number (1-{len(vps_list)}). Use {PREFIX}myvps to list."))
            return
        vps = vps_list[vps_num - 1]
        container = vps['container_name']
        node_id = vps['node_id']
        if used + len(port_pairs) > allocated:
            await ctx.send(embed=create_error_embed("Quota Exceeded", f"You need {len(port_pairs)} slot(s) but only {max(0, allocated - used)} are available. Allocated: {allocated}, Used: {used}. Contact admin for more."))
            return
        done, failed = [], []
        for vp, hp in port_pairs:
            try:
                host_port = await create_port_forward(user_id, container, vp, node_id, hp)
            except ValueError as ve:
                failed.append(f"{vp}: {ve}")
                continue
            if host_port:
                done.append((vp, host_port))
            else:
                failed.append(f"{vp}: could not create the forward")
        if done:
            embed = create_success_embed("Port Forward Created", f"VPS #{vps_num}: {len(done)} port forward(s) created (TCP & UDP).")
            lines = [f"{get_public_ip()}:{hp} → VPS:{vp}" for vp, hp in done]
            add_field(embed, "Access", "\n".join(lines[:15]) + (f"\n... +{len(lines) - 15} more (see {PREFIX}ports list)" if len(lines) > 15 else ""), False)
            add_field(embed, "Quota Update", f"Used: {used + len(done)}/{allocated}", False)
            if failed:
                add_field(embed, "Failed", "\n".join(failed[:8])[:1000], False)
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=create_error_embed("Failed", ("\n".join(failed[:8]) if failed else "Could not assign host port. Try again later.")[:1500]))
    elif subcmd == 'list':
        forwards = get_user_forwards(user_id)
        embed = create_info_embed("Your Port Forwards", f"**Quota:** Allocated: {allocated}, Used: {used}, Available: {available}")
        if not forwards:
            add_field(embed, "Forwards", "No active port forwards.", False)
        else:
            text = []
            for f in forwards:
                vps_num = next((i+1 for i, v in enumerate(vps_data.get(user_id, [])) if v['container_name'] == f['vps_container']), 'Unknown')
                created = datetime.fromisoformat(f['created_at']).strftime('%Y-%m-%d %H:%M')
                text.append(f"**ID {f['id']}** - VPS #{vps_num}: {f['vps_port']} (TCP/UDP) → {f['host_port']} (Created: {created})")
            add_field(embed, "Active Forwards", "\n".join(text[:10]), False)
            if len(forwards) > 10:
                add_field(embed, "Note", f"Showing 10 of {len(forwards)}. Remove unused with {PREFIX}ports remove <id>.")
        await ctx.send(embed=embed)
    elif subcmd == 'remove':
        if len(args) < 1:
            await ctx.send(embed=create_error_embed("Usage", f"Usage: {PREFIX}ports remove <forward_id>"))
            return
        try:
            fid = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Forward ID must be an integer."))
            return
        success, _ = await remove_port_forward(fid, requester_id=user_id)
        if success:
            embed = create_success_embed("Removed", f"Port forward {fid} removed (TCP & UDP).")
            add_field(embed, "Quota Update", f"Used: {used - 1}/{allocated}", False)
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=create_error_embed("Not Found", f"Forward ID not found or it does not belong to you. Use {PREFIX}ports list."))
    else:
        await ctx.send(embed=create_error_embed("Invalid Subcommand", f"Use: add <vps_num> <port>, list, remove <id>"))


# ============================================================
#  !prefix  - change the bot command prefix (saved in the DB)
# ============================================================
@bot.command(name='prefix', aliases=['set-prefix'])
@is_main_admin()
async def prefix_cmd(ctx, new_prefix: str = None):
    """!prefix [new_prefix|reset]  - show / change the command prefix"""
    global PREFIX
    if new_prefix is None:
        e = create_info_embed("Command Prefix", f"Current prefix: `{PREFIX}`\nChange it: `{PREFIX}prefix <new>` (example: `{PREFIX}prefix 1` then use `1help`)\nBack to default (`{ENV_PREFIX}`): `{PREFIX}prefix reset`")
        add_field(e, "Forgot the prefix?", f"Mention the bot: {bot.user.mention} `prefix reset`", False)
        await ctx.send(embed=e)
        return
    new = ENV_PREFIX if new_prefix.lower() in ('reset', 'default') else new_prefix
    if len(new) > 5 or any(ch.isspace() for ch in new) or any(ch in new for ch in '`@#<>*_~|\\'):
        await ctx.send(embed=create_error_embed("Invalid Prefix", "Use 1-5 characters with no spaces, and avoid the characters @ # < > * _ ~ | and backslash/backtick.\nExamples: `1`, `?`, `$`, `bvm.`"))
        return
    if new == PREFIX:
        await ctx.send(embed=create_info_embed("No Change", f"The prefix is already `{PREFIX}`."))
        return
    old = PREFIX
    set_setting('prefix', new)
    PREFIX = new
    log_audit(ctx.author.id, str(ctx.author), "prefix-change", "", f"{old} -> {new}")
    e = create_success_embed("Prefix Changed", f"The prefix is now `{new}` (was `{old}`).\nExample: `{new}help`")
    add_field(e, "Forgot it?", f"Mention the bot: {bot.user.mention} `prefix reset`", False)
    await ctx.send(embed=e)

# ============================================================
#  !fix-vps  - re-apply Docker / Pterodactyl / Tailscale settings to existing VPS
# ============================================================
_features_task = None

async def upgrade_vps_features(v: Dict[str, Any], do_cfg: bool = True) -> Dict[str, bool]:
    """Brings ONE existing VPS up to date: tun/KVM/limits (LXC config, live), SSH password login (sshd fix, root unlock)
    and a tailscaled restart if Tailscale runs in userspace mode. The root password and all data are NOT touched."""
    name = v['container_name']
    node = v.get('node_id', 1)
    res = {"cfg": False, "running": False, "ssh": False}
    if do_cfg:
        res["cfg"] = bool(await apply_lxc_config(name, node))
    ok, _ = await _lxc_sh(name, node, "echo up", timeout=20)
    res["running"] = ok
    if ok and not v.get('suspended'):
        ssh = await ensure_ssh_password_login(name, node, None)
        res["ssh"] = bool(ssh["listening"])
        await _lxc_sh(name, node, "command -v tailscale >/dev/null 2>&1 && [ -c /dev/net/tun ] && { ip link show tailscale0 >/dev/null 2>&1 || systemctl restart tailscaled; }; true", timeout=60)
    return res

async def vps_feature_upgrade_loop():
    """Runs for the whole life of the bot. Every cycle it upgrades each VPS that is not upgraded yet.
    Stopped VPS are retried in the next cycle (SSH needs a running VPS), so every VPS is covered sooner or later."""
    await asyncio.sleep(30)
    while True:
        try:
            try:
                state = json.loads(get_setting('features_v4', '{}'))
            except Exception:
                state = {}
            try:
                old_cfg = set(json.loads(get_setting('features_v3_done', '[]')))
            except Exception:
                old_cfg = set()
            todo = 0
            for vl in list(vps_data.values()):
                for v in list(vl):
                    name = v.get('container_name')
                    st = state.get(name) or {"cfg": name in old_cfg, "ssh": False, "tries": 0}
                    state[name] = st
                    if st["cfg"] and st["ssh"]:
                        continue
                    todo += 1
                    try:
                        res = await upgrade_vps_features(v, do_cfg=not st["cfg"])
                        st["cfg"] = st["cfg"] or res["cfg"]
                        if res["running"] and not v.get('suspended'):
                            st["tries"] = st.get("tries", 0) + 1
                            if res["ssh"] or st["tries"] >= 3:
                                st["ssh"] = True
                    except Exception as e:
                        logger.warning(f"Feature upgrade failed for {name}: {e}")
                    set_setting('features_v4', json.dumps(state))
                    await asyncio.sleep(3)
            if todo:
                logger.info(f"VPS feature upgrade cycle finished ({todo} VPS processed)")
        except Exception as e:
            logger.error(f"vps_feature_upgrade_loop error: {e}")
        await asyncio.sleep(1200)   # 20 minutes

async def _lxc_sh(container_name: str, node_id: int, cmd: str, timeout: int = 60):
    """Runs a shell command inside a container. Returns (ok, output)."""
    try:
        out = await execute_lxc(container_name, f'exec {container_name} -- bash -c "{cmd}"', node_id=node_id, timeout=timeout)
        return True, (out if isinstance(out, str) else "").strip()
    except Exception as e:
        return False, str(e)[:200]

async def ensure_ssh_password_login(container_name: str, node_id: int, root_password: Optional[str] = None) -> Dict[str, Any]:
    """Makes sure root can log in over SSH with a password - also through the Tailscale IP.
    Installs openssh-server if missing, forces PermitRootLogin/PasswordAuthentication = yes (main file AND
    /etc/ssh/sshd_config.d drop-ins, which cloud images use to turn passwords off), unlocks root, re-sets the
    saved root password and restarts sshd. Never touches user data."""
    steps = [
        "command -v sshd >/dev/null 2>&1 || (DEBIAN_FRONTEND=noninteractive apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server)",
        "mkdir -p /run/sshd /etc/ssh/sshd_config.d",
        "sed -i -E 's/^[[:space:]]*(PasswordAuthentication|PermitRootLogin|KbdInteractiveAuthentication)[[:space:]]+(no|prohibit-password|without-password)/\\1 yes/' /etc/ssh/sshd_config.d/*.conf 2>/dev/null || true",
        "sed -i -E '/^[[:space:]]*#?[[:space:]]*(PermitRootLogin|PasswordAuthentication|KbdInteractiveAuthentication|UsePAM)[[:space:]]/d' /etc/ssh/sshd_config",
        "printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\nKbdInteractiveAuthentication yes\\nUsePAM yes\\n\\n' | cat - /etc/ssh/sshd_config > /tmp/sshd_config.new && mv /tmp/sshd_config.new /etc/ssh/sshd_config",
        "passwd -u root >/dev/null 2>&1 || true",
    ]
    if root_password:
        steps.append(f"echo 'root:{root_password}' | chpasswd")
    errors = []
    for cmd in steps:
        ok, out = await _lxc_sh(container_name, node_id, cmd, timeout=240)
        if not ok:
            errors.append(out[:120])
    ok, out = await _lxc_sh(container_name, node_id, "sshd -t 2>&1 && echo syntax-ok")
    syntax_ok = ok and "syntax-ok" in out
    if not syntax_ok:
        errors.append(f"sshd config test failed: {out[:120]}")
    await _lxc_sh(container_name, node_id,
                  "systemctl enable ssh 2>/dev/null || systemctl enable sshd 2>/dev/null || true; "
                  "systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || service sshd restart 2>/dev/null || /usr/sbin/sshd 2>/dev/null || true")
    await asyncio.sleep(2)
    ok, listening = await _lxc_sh(container_name, node_id, "ss -ltn 2>/dev/null | grep -c ':22 ' || true")
    ok2, eff = await _lxc_sh(container_name, node_id, "sshd -T 2>/dev/null | grep -E '^(permitrootlogin|passwordauthentication) ' | tr '\\n' ' '")
    return {"listening": listening.strip() not in ("", "0"), "effective": eff.strip(), "errors": errors}

async def _vps_feature_report(container_name: str, node_id: int) -> Dict[str, Any]:
    """Looks inside a RUNNING container (read-only checks)."""
    async def sh(cmd):
        ok, out = await _lxc_sh(container_name, node_id, cmd, timeout=40)
        return out if ok else "ERR:" + out[:80]
    r: Dict[str, Any] = {}
    tun = await sh("[ -c /dev/net/tun ] && echo ok || echo missing")
    r['running'] = not tun.startswith("ERR:")
    r['tun_ok'] = (tun == "ok")
    r['tun'] = "✅ /dev/net/tun present" if tun == "ok" else f"❌ /dev/net/tun missing ({tun[:60]}) - restart the VPS once, then run this again"
    dk = await sh("command -v docker >/dev/null 2>&1 && (docker info --format '{{.Driver}}' 2>/dev/null || echo daemon-down) || echo none")
    if dk == "none":
        r['docker'] = "ℹ️ Docker not installed inside the VPS (Pterodactyl Wings needs it)"
    elif dk == "daemon-down":
        r['docker'] = "⚠️ Docker installed but the daemon is not running (`systemctl start docker`)"
    elif dk == "overlay2":
        r['docker'] = "✅ Docker works (overlay2)"
    else:
        r['docker'] = f"⚠️ Docker storage driver is `{dk}` (overlay2 is best; a ZFS/btrfs LXD pool can cause this)"
    loop = await sh("[ -c /dev/loop-control ] && echo ok || echo missing")
    sn = await sh("command -v snap >/dev/null 2>&1 && (timeout 20 snap version >/dev/null 2>&1 && echo works || echo broken) || echo none")
    r['snap_ok'] = (sn == "works")
    if sn == "works":
        r['snap'] = "✅ snap works"
    elif sn == "none":
        r['snap'] = ("ℹ️ snap not installed (install: `apt install snapd`, or run the setup-snap command)"
                     + ("" if loop == "ok" else "\n❌ /dev/loop-control missing - restart the VPS once"))
    else:
        r['snap'] = "⚠️ snap is installed but not responding - run the setup-snap command to repair it"
    kvm = await sh("[ -c /dev/kvm ] && echo ok || echo missing")
    r['kvm'] = ("✅ /dev/kvm available - KVM / nested virtualization (QEMU, Android emulators...) works" if kvm == "ok"
                else "ℹ️ /dev/kvm not available (host has no KVM or it is not enabled in the BIOS / hypervisor)")
    ts = await sh("command -v tailscale >/dev/null 2>&1 && echo installed || echo none")
    r['ts_installed'] = (ts == "installed")
    if ts == "installed":
        mode = await sh("ip link show tailscale0 >/dev/null 2>&1 && echo kernel || echo userspace")
        ip = await sh("tailscale ip -4 2>/dev/null | head -1")
        ip = ip if ip and not ip.startswith("ERR:") else "not logged in (`tailscale up`)"
        r['tailscale'] = (f"✅ Tailscale installed, IP: `{ip}`\n"
                          + ("✅ kernel mode (tailscale0) - SSH to the Tailscale IP works" if mode == "kernel"
                             else "❌ userspace mode - incoming SSH over Tailscale will NOT work. Restart tailscaled (done automatically by this command)"))
    else:
        r['tailscale'] = "ℹ️ Tailscale not installed yet"
    return r

@bot.command(name='fix-vps')
@is_admin()
async def fix_vps(ctx, container_name: str):
    """!fix-vps <container|all>  - fix Docker/Pterodactyl/Tailscale + SSH password login (no data touched)"""
    if container_name.lower() == 'all':
        targets = [v for vl in vps_data.values() for v in vl]
        await ctx.send(embed=create_info_embed("Fixing all VPS", f"Upgrading {len(targets)} VPS one by one (tun, KVM, SSH password login, Tailscale). Running VPS are not restarted. This can take a while..."))
        cfg_ok = ssh_ok = stopped = 0
        for v in targets:
            try:
                res = await upgrade_vps_features(v)
                cfg_ok += 1 if res["cfg"] else 0
                ssh_ok += 1 if res["ssh"] else 0
                stopped += 0 if res["running"] else 1
            except Exception as e:
                logger.error(f"fix-vps all: {v['container_name']}: {e}")
        await ctx.send(embed=create_success_embed("Done", f"VPS processed: {len(targets)}\nLXC settings (tun/KVM/limits) applied: {cfg_ok}\nSSH password login verified: {ssh_ok}\nStopped VPS: {stopped} (SSH fix is retried automatically when they run)\nRoot passwords were not changed."))
        return
    _, vps = find_vps_by_container(container_name)
    if vps is None:
        await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS named `{container_name}`. Use `{PREFIX}list-all`."))
        return
    node_id = vps.get('node_id', 1)
    msg = await ctx.send(embed=create_info_embed("Fixing VPS", f"Applying settings to `{container_name}` (data is not touched, this can take about a minute)..."))
    await apply_lxc_config(container_name, node_id)
    report = await _vps_feature_report(container_name, node_id)
    if not report['running']:
        await msg.edit(embed=create_warning_embed("Settings applied", f"`{container_name}` is stopped. Start it, then run `{PREFIX}fix-vps {container_name}` again (SSH + Tailscale checks need a running VPS)."))
        return
    # SSH password login (works for the normal tunnel and for the Tailscale IP)
    ssh = await ensure_ssh_password_login(container_name, node_id, vps.get('root_password') or None)
    # Direct SSH on the server IPv4 (random port per VPS) instead of the Pinggy tunnel
    owner_uid, _vrec = find_vps_by_container(container_name)
    direct_addr = await establish_ssh_access(container_name, node_id, owner_uid) if owner_uid else None
    if direct_addr and _vrec is not None:
        _vrec['pinggy_address'] = direct_addr
        save_vps_data()
    # Tailscale must restart once so it picks up /dev/net/tun (it falls back to userspace mode without it)
    restarted = False
    if report['ts_installed'] and report['tun_ok']:
        ok, _ = await _lxc_sh(container_name, node_id, "systemctl restart tailscaled", timeout=60)
        restarted = ok
        if ok:
            await asyncio.sleep(4)
            report = await _vps_feature_report(container_name, node_id)
    embed = create_success_embed("VPS Fixed", f"Settings applied to `{container_name}`.")
    ssh_line = ("✅ sshd running on port 22" if ssh['listening'] else "❌ sshd is NOT listening on port 22")
    ssh_line += f"\n`{ssh['effective'] or 'could not read sshd settings'}`"
    if vps.get('root_password'):
        ssh_line += "\n✅ saved root password re-applied (user `root`)"
    else:
        ssh_line += "\n⚠️ no saved root password - use 🔐 Change Password in the manage panel"
    if ssh['errors']:
        ssh_line += "\n⚠️ " + " | ".join(ssh['errors'])[:300]
    add_field(embed, "SSH password login", ssh_line, False)
    if direct_addr:
        h_, p_ = direct_addr.rsplit(":", 1)
        add_field(embed, "SSH address (Pinggy)", f"```ssh root@{h_} -p {p_}```", False)
    ts_text = f"{report['tun']}\n{report['tailscale']}"
    if restarted:
        ts_text += "\n🔄 tailscaled restarted"
    if not report['ts_installed']:
        ts_text += "\nInstall: `curl -fsSL https://tailscale.com/install.sh | sh` then `tailscale up`"
    add_field(embed, "Tailscale", ts_text[:1000], False)
    add_field(embed, "Pterodactyl / Docker", f"{report['docker']}\nAfter installing Docker, Wings needs the daemon running.", False)
    add_field(embed, "Snap", report['snap'], False)
    add_field(embed, "KVM", report['kvm'], False)
    add_field(embed, "Extra setup", f"`{PREFIX}setup-docker {container_name}` installs/repairs Docker (Pterodactyl Wings)\n`{PREFIX}setup-snap {container_name}` installs/repairs snap", False)
    await msg.edit(embed=embed)

# ============================================================
#  !setup-docker / !setup-snap  - install + repair Docker (Pterodactyl Wings) and snap inside a running VPS
# ============================================================
DOCKER_DAEMON_JSON = '{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}'
DOCKER_DAEMON_JSON_CGROUPFS = '{"exec-opts":["native.cgroupdriver=cgroupfs"],"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}'

def _b64_write_cmd(text: str, path: str) -> str:
    """Shell command that writes `text` to `path` (base64 avoids every quoting problem inside lxc exec)."""
    import base64
    return f"echo {base64.b64encode(text.encode()).decode()} | base64 -d > {path}"

async def _find_running_vps(ctx, container_name: str):
    """Returns the vps dict if it exists and is running, else sends an error and returns None."""
    _, vps = find_vps_by_container(container_name)
    if vps is None:
        await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS named `{container_name}`. Use `{PREFIX}list-all`."))
        return None
    ok, _out = await _lxc_sh(container_name, vps.get('node_id', 1), "echo up", timeout=20)
    if not ok:
        await ctx.send(embed=create_error_embed("VPS Not Running", f"`{container_name}` is not running. Start it first."))
        return None
    return vps

@bot.command(name='setup-docker', aliases=['fix-docker', 'setup-wings'])
@is_admin()
async def setup_docker_cmd(ctx, container_name: str):
    """!setup-docker <container>  - install / repair Docker inside the VPS (needed by Pterodactyl Wings)"""
    vps = await _find_running_vps(ctx, container_name)
    if vps is None:
        return
    node_id = vps.get('node_id', 1)
    msg = await ctx.send(embed=create_info_embed("Docker Setup", f"Setting up Docker inside `{container_name}` (can take 2-5 minutes)..."))
    await apply_lxc_config(container_name, node_id)
    await apply_internal_permissions(container_name, node_id)
    steps = []
    ok, out = await _lxc_sh(container_name, node_id, "command -v docker >/dev/null 2>&1 && echo yes || echo no", timeout=30)
    if not (ok and out.strip() == "yes"):
        ok, out = await _lxc_sh(container_name, node_id,
            "export DEBIAN_FRONTEND=noninteractive; apt-get update -y >/dev/null 2>&1; "
            "apt-get install -y curl ca-certificates iptables >/dev/null 2>&1; "
            "curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && sh /tmp/get-docker.sh >/tmp/get-docker.log 2>&1; "
            "command -v docker >/dev/null 2>&1 && echo installed || (tail -n 5 /tmp/get-docker.log; echo FAILED)", timeout=900)
        steps.append("✅ Docker installed" if ok and "installed" in out and "FAILED" not in out else f"❌ Docker install failed: {out[-200:]}")
    else:
        steps.append("✅ Docker already installed")
    # iptables-legacy is the most reliable backend inside LXC (nft often is not available to the container)
    await _lxc_sh(container_name, node_id,
        "command -v update-alternatives >/dev/null 2>&1 && [ -x /usr/sbin/iptables-legacy ] && "
        "(update-alternatives --set iptables /usr/sbin/iptables-legacy; update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy) >/dev/null 2>&1; true", timeout=60)
    await _lxc_sh(container_name, node_id, "mkdir -p /etc/docker && [ -s /etc/docker/daemon.json ] || (" + _b64_write_cmd(DOCKER_DAEMON_JSON, "/etc/docker/daemon.json") + ")", timeout=30)
    await _lxc_sh(container_name, node_id, "systemctl enable docker >/dev/null 2>&1; systemctl enable containerd >/dev/null 2>&1; systemctl restart docker", timeout=120)
    await asyncio.sleep(4)
    ok, drv = await _lxc_sh(container_name, node_id, "docker info --format '{{.Driver}}' 2>&1 | head -n 1", timeout=60)
    if ok and drv.strip() == "overlay2":
        steps.append("✅ Docker daemon running (overlay2)")
    elif ok and drv.strip() and "Cannot connect" not in drv and "error" not in drv.lower():
        steps.append(f"⚠️ Docker running with storage driver `{drv.strip()}` (overlay2 is best; a ZFS/btrfs LXD pool causes this)")
    else:
        # Retry once with the cgroupfs driver (fixes 'cgroup' errors on some hosts)
        await _lxc_sh(container_name, node_id, _b64_write_cmd(DOCKER_DAEMON_JSON_CGROUPFS, "/etc/docker/daemon.json") + "; systemctl restart docker", timeout=120)
        await asyncio.sleep(4)
        ok, drv = await _lxc_sh(container_name, node_id, "docker info --format '{{.Driver}}' 2>&1 | head -n 1", timeout=60)
        if ok and drv.strip() and "Cannot connect" not in drv and "error" not in drv.lower():
            steps.append(f"✅ Docker running after cgroupfs fix (`{drv.strip()}`)")
        else:
            ok, logs = await _lxc_sh(container_name, node_id, "journalctl -u docker -n 6 --no-pager 2>&1 | tail -c 400", timeout=30)
            steps.append(f"❌ Docker daemon is not running.\n```{logs[-350:]}```")
    ok, test = await _lxc_sh(container_name, node_id, "timeout 90 docker run --rm hello-world 2>&1 | grep -c 'Hello from Docker' || true", timeout=120)
    steps.append("✅ `docker run hello-world` works" if ok and test.strip() == "1" else "⚠️ test container (`hello-world`) did not run - if Docker is up, check internet/DNS inside the VPS")
    embed = create_success_embed("Docker Setup Finished", f"`{container_name}`")
    add_field(embed, "Result", "\n".join(steps)[:1000], False)
    add_field(embed, "Pterodactyl Wings", (
        "1. Install Wings from the Pterodactyl docs, paste the node config from the panel (`/etc/pterodactyl/config.yml`).\n"
        f"2. Forward the SAME ports from the host: `{PREFIX}ports add <vps_num> 8080 same`, `{PREFIX}ports add <vps_num> 2022 same` and your game ports, e.g. `{PREFIX}ports add <vps_num> 25565-25575`.\n"
        "3. In the panel set the node FQDN to the server's public IP/domain, Daemon Port 8080, SFTP 2022.\n"
        "4. `systemctl enable --now wings`. If the heart is still red the panel cannot reach port 8080 - check the forward and `systemctl status wings`."), False)
    await msg.edit(embed=embed)

@bot.command(name='setup-snap', aliases=['fix-snap'])
@is_admin()
async def setup_snap_cmd(ctx, container_name: str):
    """!setup-snap <container>  - install / repair snap (snapd) inside the VPS"""
    vps = await _find_running_vps(ctx, container_name)
    if vps is None:
        return
    node_id = vps.get('node_id', 1)
    msg = await ctx.send(embed=create_info_embed("Snap Setup", f"Setting up snap inside `{container_name}` (can take 1-3 minutes)..."))
    await apply_lxc_config(container_name, node_id)
    steps = []
    ok, out = await _lxc_sh(container_name, node_id, "[ -c /dev/loop-control ] && echo yes || echo no", timeout=30)
    loop_ok = ok and out.strip() == "yes"
    if not loop_ok:
        steps.append("⚠️ /dev/loop-control is missing - the loop devices were just added to the VPS, **restart the VPS once** (`!manage` → Restart) and run this command again. snap can still work through squashfuse.")
    ok, out = await _lxc_sh(container_name, node_id,
        "export DEBIAN_FRONTEND=noninteractive; apt-get update -y >/dev/null 2>&1; "
        "apt-get install -y snapd squashfs-tools fuse3 squashfuse >/dev/null 2>&1 || apt-get install -y snapd squashfs-tools fuse squashfuse >/dev/null 2>&1; "
        "command -v snap >/dev/null 2>&1 && echo installed || echo FAILED", timeout=600)
    if not (ok and "installed" in out and "FAILED" not in out):
        steps.append(f"❌ Could not install snapd: {out[-200:]}")
    else:
        steps.append("✅ snapd installed")
        await _lxc_sh(container_name, node_id,
            "systemctl unmask snapd.service snapd.socket >/dev/null 2>&1; systemctl enable --now snapd.socket >/dev/null 2>&1; "
            "systemctl restart snapd >/dev/null 2>&1; ln -sf /var/lib/snapd/snap /snap 2>/dev/null; true", timeout=120)
        await asyncio.sleep(5)
        ok, ver = await _lxc_sh(container_name, node_id, "timeout 60 snap wait system seed.loaded >/dev/null 2>&1; timeout 30 snap version 2>&1 | head -n 2", timeout=120)
        if ok and "snapd" in ver:
            steps.append("✅ snapd is running\n```" + ver[:150] + "```")
            ok, t = await _lxc_sh(container_name, node_id, "timeout 150 snap install hello-world 2>&1 | tail -n 2", timeout=200)
            if ok and ("installed" in t or "already installed" in t):
                steps.append("✅ `snap install` works")
                await _lxc_sh(container_name, node_id, "snap remove hello-world >/dev/null 2>&1; true", timeout=60)
            else:
                steps.append(f"⚠️ `snap install hello-world` failed: {t[-200:]}\nTry restarting the VPS once, then `{PREFIX}setup-snap {container_name}` again.")
        else:
            steps.append("❌ snapd does not answer. Restart the VPS once and run this command again.")
    embed = create_success_embed("Snap Setup Finished", f"`{container_name}`")
    add_field(embed, "Result", "\n".join(steps)[:1000], False)
    await msg.edit(embed=embed)

# ============================================================
#  VPS TRANSFER  (!transfer-vps)
#  Moves ownership of a VPS from one user to another.
#  SAFE BY DESIGN: the LXC container is never stopped, copied,
#  renamed or deleted - only the ownership records change, so all
#  files / installed software / data inside the VPS stay intact.
# ============================================================
_transfer_lock = asyncio.Lock()

def find_vps_by_container(container_name: str):
    """Returns (owner_id, vps_dict) for a container, or (None, None)."""
    for uid, vlist in vps_data.items():
        for v in vlist:
            if v.get('container_name') == container_name:
                return uid, v
    return None, None

def _db_transfer_vps(container_name: str, old_id: str, new_id: str, shares_json: str) -> int:
    """Changes the owner in ONE database transaction (all-or-nothing).
    Moves the VPS record, its port forwards and the matching port quota.
    Returns the number of port forwards moved."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        cur.execute("UPDATE vps SET user_id = ?, shared_with = ? WHERE container_name = ? AND user_id = ?",
                    (new_id, shares_json, container_name, old_id))
        if cur.rowcount != 1:
            raise RuntimeError("VPS record not found or its owner changed in the meantime.")
        cur.execute("SELECT COUNT(*) FROM port_forwards WHERE vps_container = ?", (container_name,))
        moved = cur.fetchone()[0]
        if moved:
            cur.execute("UPDATE port_forwards SET user_id = ? WHERE vps_container = ?", (new_id, container_name))
            cur.execute("UPDATE port_allocations SET allocated_ports = MAX(0, allocated_ports - ?) WHERE user_id = ?", (moved, old_id))
            cur.execute("INSERT OR IGNORE INTO port_allocations (user_id, allocated_ports) VALUES (?, 0)", (new_id,))
            cur.execute("UPDATE port_allocations SET allocated_ports = allocated_ports + ? WHERE user_id = ?", (moved, new_id))
        conn.commit()
        return moved
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

async def perform_vps_transfer(guild, admin, container_name: str, new_member: discord.Member,
                               keep_shares: bool, reset_password: bool) -> Dict[str, Any]:
    """Runs the transfer. Raises ValueError with a readable message when it cannot proceed."""
    new_id = str(new_member.id)
    async with _transfer_lock:
        old_id, vps = find_vps_by_container(container_name)
        if vps is None:
            raise ValueError(f"VPS `{container_name}` no longer exists.")
        if old_id == new_id:
            raise ValueError("That user already owns this VPS.")

        # Shares: the new owner no longer needs one; the others are kept or revoked.
        old_shares = list(vps.get('shared_with', []))
        new_shares = [s for s in old_shares if s != new_id] if keep_shares else []
        new_shares = [s for s in new_shares if s != old_id]

        # 1) Ownership change = a single DB transaction. Nothing else is touched before it succeeds.
        try:
            moved_ports = await asyncio.to_thread(_db_transfer_vps, container_name, old_id, new_id, json.dumps(new_shares))
        except Exception as e:
            raise ValueError(f"Database update failed, nothing was changed: {e}")

        # 2) Mirror the change in memory.
        vps_data[old_id] = [v for v in vps_data.get(old_id, []) if v is not vps]
        if not vps_data[old_id]:
            del vps_data[old_id]
        vps['user_id'] = new_id
        vps['shared_with'] = new_shares
        vps_data.setdefault(new_id, []).append(vps)

    result = {
        "old_id": old_id, "moved_ports": moved_ports,
        "shares_revoked": 0 if keep_shares else len(old_shares),
        "password": None, "password_reset": False, "password_error": None,
        "role_notes": [],
    }
    node_id = vps.get('node_id', 1)

    # 3) Optional: new root password (old owner knew the old one). chpasswd never touches data.
    if reset_password:
        try:
            new_password = generate_password()
            await execute_lxc(container_name, f"exec {container_name} -- bash -c \"echo 'root:{new_password}' | chpasswd\"",
                              node_id=node_id, timeout=60)
            vps['root_password'] = new_password
            save_vps_data()
            result["password_reset"] = True
        except Exception as e:
            result["password_error"] = str(e)[:300]
    result["password"] = vps.get('root_password') or None

    # 4) VPS role: give it to the new owner, remove it from the old one if they have no VPS left.
    if guild:
        try:
            role = await get_or_create_vps_role(guild)
            if role:
                if role not in new_member.roles:
                    await new_member.add_roles(role, reason="VPS transferred to this user")
                old_member = guild.get_member(int(old_id))
                if old_member and old_id not in vps_data and role in old_member.roles:
                    await old_member.remove_roles(role, reason="No VPS ownership after transfer")
        except Exception as e:
            result["role_notes"].append(f"Role update skipped: {str(e)[:100]}")

    log_audit(admin.id, str(admin), "vps-transfer", container_name,
              f"{old_id} -> {new_id} | ports moved: {moved_ports} | keep_shares={keep_shares} | password_reset={result['password_reset']}")
    return result


class TransferConfirmView(discord.ui.View):
    def __init__(self, admin, container_name: str, old_id: str, new_member: discord.Member,
                 keep_shares: bool, reset_password: bool, guild):
        super().__init__(timeout=60)
        self.admin = admin
        self.container_name = container_name
        self.old_id = old_id
        self.new_member = new_member
        self.keep_shares = keep_shares
        self.reset_password = reset_password
        self.guild = guild
        self.message = None
        self.finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin.id:
            await interaction.response.send_message("Only the admin who started this transfer can use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Transfer", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return
        self.finished = True
        await interaction.response.edit_message(
            embed=create_info_embed("Transferring VPS", f"Moving `{self.container_name}` to {self.new_member.mention}... (the VPS is not touched)"),
            view=None)
        self.stop()
        try:
            res = await perform_vps_transfer(self.guild, self.admin, self.container_name, self.new_member,
                                             self.keep_shares, self.reset_password)
        except ValueError as e:
            await interaction.edit_original_response(embed=create_error_embed("Transfer Failed", str(e)), view=None)
            return
        except Exception as e:
            logger.error(f"VPS transfer crashed: {e}")
            await interaction.edit_original_response(embed=create_error_embed("Transfer Failed", f"Unexpected error: {str(e)[:500]}"), view=None)
            return

        _, vps = find_vps_by_container(self.container_name)
        new_pos = len(vps_data.get(str(self.new_member.id), []))
        done = create_success_embed("✅ VPS Transferred", f"`{self.container_name}` now belongs to {self.new_member.mention}. All VPS data is intact.")
        add_field(done, "From", f"<@{res['old_id']}>", True)
        add_field(done, "To", self.new_member.mention, True)
        add_field(done, "New VPS number", f"#{new_pos} (use `{PREFIX}manage`)", True)
        add_field(done, "Port forwards moved", str(res["moved_ports"]), True)
        add_field(done, "Shared access", "Kept" if self.keep_shares else f"Revoked ({res['shares_revoked']} user(s))", True)
        if res["password_reset"]:
            pw_text = "New root password generated (sent to the new owner by DM)"
        elif self.reset_password:
            pw_text = f"⚠️ Reset FAILED (VPS stopped?): `{res['password_error']}`\nThe old password is still active. Start the VPS and run the password change from `{PREFIX}manage`."
        else:
            pw_text = "Unchanged (old owner still knows it)"
        add_field(done, "Root password", pw_text, False)
        for note in res["role_notes"]:
            add_field(done, "Note", note, False)
        await interaction.edit_original_response(embed=done, view=None)

        # DM the new owner (credentials only by DM, never in the channel)
        dm = create_success_embed("🎁 A VPS was transferred to you", f"Admin {self.admin.mention} transferred VPS `{self.container_name}` to you.")
        add_field(dm, "Plan", str(vps.get('config', 'Custom')), True)
        add_field(dm, "Expiry", expiry_text(vps.get('expires_at')), True)
        pinggy = vps.get('pinggy_address')
        if pinggy and ':' in pinggy and res["password"]:
            host, port = pinggy.split(":", 1)
            add_field(dm, "🔑 SSH Login", f"**Host:** `{host}`\n**Port:** `{port}`\n**User:** `root`\n**Password:** `{res['password']}`\n\n```ssh root@{host} -p {port}```", False)
        elif res["password"]:
            add_field(dm, "🔑 Root Password", f"`{res['password']}`\nUse `{PREFIX}manage` → 🔌 Reconnect Tunnel to get an SSH address.", False)
        add_field(dm, "Manage it", f"Use `{PREFIX}manage`. Keep your password private.", False)
        dm_failed = []
        try:
            await self.new_member.send(embed=dm)
        except Exception:
            dm_failed.append(self.new_member.mention)
        # Tell the previous owner
        try:
            old_user = await bot.fetch_user(int(res["old_id"]))
            await old_user.send(embed=create_warning_embed("VPS Transferred", f"Your VPS `{self.container_name}` was transferred to another user by an admin. You no longer have access to it."))
        except Exception:
            pass
        if dm_failed:
            await interaction.followup.send(embed=create_info_embed(
                "DM failed", f"Could not DM {', '.join(dm_failed)} (DMs closed). They can use `{PREFIX}manage` → 🌐 SSHX to receive the SSH login."), ephemeral=True)

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return
        self.finished = True
        await interaction.response.edit_message(embed=create_info_embed("Transfer Cancelled", "Nothing was changed."), view=None)
        self.stop()

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        if self.message:
            try:
                await self.message.edit(embed=create_info_embed("Transfer Expired", "No confirmation received. Nothing was changed."), view=None)
            except Exception:
                pass


@bot.command(name='transfer-vps')
@is_admin()
async def transfer_vps(ctx, container_name: str, new_owner: discord.Member, *flags):
    """!transfer-vps <container> @new_owner [keep-shares] [keep-password]"""
    flags = {f.lower() for f in flags}
    unknown = flags - {'keep-shares', 'keep-password'}
    if unknown:
        await ctx.send(embed=create_error_embed("Unknown option", f"`{', '.join(sorted(unknown))}`\nValid options: `keep-shares`, `keep-password`."))
        return
    keep_shares = 'keep-shares' in flags
    reset_password = 'keep-password' not in flags

    if new_owner.bot:
        await ctx.send(embed=create_error_embed("Invalid User", "A VPS cannot be transferred to a bot."))
        return
    old_id, vps = find_vps_by_container(container_name)
    if vps is None:
        await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS named `{container_name}`. Use `{PREFIX}list-all` to see the container names."))
        return
    if old_id == str(new_owner.id):
        await ctx.send(embed=create_error_embed("Same Owner", f"{new_owner.mention} already owns this VPS."))
        return

    conn = get_db()
    n_ports = conn.execute("SELECT COUNT(*) FROM port_forwards WHERE vps_container = ?", (container_name,)).fetchone()[0]
    conn.close()
    embed = create_warning_embed("Confirm VPS Transfer", "Please check the details. **The VPS itself is not stopped, copied or deleted - all data stays.**")
    add_field(embed, "Container", f"`{container_name}`", True)
    add_field(embed, "Plan", str(vps.get('config', 'Custom')), True)
    add_field(embed, "Status", f"{vps.get('status', 'unknown')}{' (suspended)' if vps.get('suspended') else ''}", True)
    add_field(embed, "From", f"<@{old_id}>", True)
    add_field(embed, "To", new_owner.mention, True)
    add_field(embed, "Expiry", f"{expiry_text(vps.get('expires_at'))} (stays the same)", True)
    add_field(embed, "Port forwards", f"{n_ports} will move with the VPS", True)
    add_field(embed, "Shared access", "Kept" if keep_shares else f"Will be revoked ({len(vps.get('shared_with', []))} user(s))", True)
    add_field(embed, "Root password", "Will be reset and DMed to the new owner" if reset_password else "Unchanged (old owner still knows it)", True)
    add_field(embed, "Note", "The previous owner loses all access. The container name stays the same.", False)
    view = TransferConfirmView(ctx.author, container_name, old_id, new_owner, keep_shares, reset_password, ctx.guild)
    view.message = await ctx.send(embed=embed, view=view)


async def delete_container_safely(container_name: str, node_id: int):
    """Deletes an LXC container. Returns (True, message) on success or when the
    container no longer exists, and (False, error) when deletion really failed."""
    try:
        await execute_lxc(container_name, f"delete {container_name} --force", node_id=node_id)
        return True, "Container deleted successfully."
    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ["not found", "does not exist", "no such container"]):
            return True, "Container was already gone (record cleaned up)."
        return False, str(e)

def purge_vps_records(user_id: str, container_name: str):
    """Removes a VPS from the database and from memory (after its container is gone)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM vps WHERE container_name = ?", (container_name,))
    cur.execute("DELETE FROM port_forwards WHERE vps_container = ?", (container_name,))
    conn.commit()
    conn.close()
    if user_id in vps_data:
        vps_data[user_id] = [v for v in vps_data[user_id] if v.get('container_name') != container_name]
        if not vps_data[user_id]:
            del vps_data[user_id]

@bot.command(name='delete-vps')
@is_admin()
async def delete_vps(ctx, user: discord.Member, vps_number: int, *, reason: str = "No reason"):
    user_id = str(user.id)

    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed(
            "Invalid VPS",
            "Invalid VPS number or user doesn't have that VPS."
        ))
        return

    vps = vps_data[user_id][vps_number - 1]
    container_name = vps["container_name"]
    node_id = vps.get("node_id", 1)

    await ctx.send(embed=create_info_embed(
        "Deleting VPS",
        f"Removing VPS #{vps_number} for {user.mention}..."
    ))

    node_result = "Not checked"

    # 1️⃣ Delete the container first. If this fails (and the container still
    # exists) we STOP and keep the database record, so nothing is orphaned.
    ok, detail = await delete_container_safely(container_name, node_id)
    if not ok:
        await ctx.send(embed=create_error_embed(
            "Delete Failed",
            f"The container `{container_name}` could not be deleted, so the VPS record was **kept**.\n"
            f"```{detail[:900]}```\nFix the node/container problem and run the command again."
        ))
        return
    node_result = detail

    # 2️⃣ DELETE FROM DATABASE (THIS IS THE FIX)
    conn = get_db()
    cur = conn.cursor()

    cur.execute("DELETE FROM vps WHERE container_name = ?", (container_name,))
    cur.execute("DELETE FROM port_forwards WHERE vps_container = ?", (container_name,))

    conn.commit()
    conn.close()

    # 3️⃣ Remove from memory
    del vps_data[user_id][vps_number - 1]
    if not vps_data[user_id]:
        del vps_data[user_id]

        # Remove VPS role if needed
        if ctx.guild:
            role = await get_or_create_vps_role(ctx.guild)
            if role and role in user.roles:
                try:
                    await user.remove_roles(role, reason="No VPS ownership")
                except discord.Forbidden:
                    logger.warning(f"Failed to remove VPS role from {user.name}")

    save_vps_data()

    # 4️⃣ Success embed
    embed = create_success_embed("🌟 UnixNodes - VPS Deleted Successfully")
    add_field(embed, "Owner", user.mention, True)
    add_field(embed, "VPS Number", f"#{vps_number}", True)
    add_field(embed, "Container", container_name, False)
    add_field(embed, "Node Result", node_result, False)
    add_field(embed, "Reason", reason, False)

    await ctx.send(embed=embed)

async def change_vps_resources(ctx, user, vps_number: int, ram: int, cpu: int, disk: int, mode: str):
    """Shared logic for !add-resources (mode='add') and !resize-vps (mode='set').
    A value of 0 means 'leave this resource unchanged'."""
    if min(ram, cpu, disk) < 0:
        await ctx.send(embed=create_error_embed("Invalid Values", "RAM, CPU and Disk must be 0 or positive (0 = leave unchanged)."))
        return
    if ram == 0 and cpu == 0 and disk == 0:
        await ctx.send(embed=create_error_embed("Missing Parameters", "Give at least one of RAM, CPU or Disk greater than 0."))
        return
    user_id = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list:
        await ctx.send(embed=create_error_embed("No VPS Found", f"{user.mention} doesn't have any VPS."))
        return
    if vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("VPS Not Found", f"{user.mention} has {len(vps_list)} VPS. Use a VPS number from 1 to {len(vps_list)}."))
        return
    vps_index = vps_number - 1
    found_vps = vps_list[vps_index]
    container_name = found_vps['container_name']
    node_id = found_vps['node_id']
    was_running = found_vps.get('status') == 'running' and not found_vps.get('suspended', False)
    if was_running:
        await ctx.send(embed=create_info_embed("Stopping VPS", f"Stopping `{container_name}` to apply resource changes..."))
        try:
            await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
            found_vps['status'] = 'stopped'
            save_vps_data()
        except Exception as e:
            await ctx.send(embed=create_error_embed("Stop Failed", f"Error stopping VPS: {str(e)}"))
            return
    changes = []
    try:
        cur_ram = int(str(found_vps['ram']).replace('GB', ''))
        cur_cpu = int(found_vps['cpu'])
        cur_disk = int(str(found_vps['storage']).replace('GB', ''))
        new_ram, new_cpu, new_disk = cur_ram, cur_cpu, cur_disk
        if ram > 0:
            new_ram = cur_ram + ram if mode == "add" else ram
            await execute_lxc(container_name, f"config set {container_name} limits.memory {new_ram * 1024}MB", node_id=node_id)
            changes.append(f"RAM: {cur_ram}GB -> {new_ram}GB")
        if cpu > 0:
            new_cpu = cur_cpu + cpu if mode == "add" else cpu
            await execute_lxc(container_name, f"config set {container_name} limits.cpu {new_cpu}", node_id=node_id)
            changes.append(f"CPU: {cur_cpu} -> {new_cpu} cores")
        if disk > 0:
            new_disk = cur_disk + disk if mode == "add" else disk
            await execute_lxc(container_name, f"config device set {container_name} root size={new_disk}GB", node_id=node_id)
            changes.append(f"Disk: {cur_disk}GB -> {new_disk}GB")
        found_vps['ram'] = f"{new_ram}GB"
        found_vps['cpu'] = str(new_cpu)
        found_vps['storage'] = f"{new_disk}GB"
        found_vps['config'] = f"{new_ram}GB RAM / {new_cpu} CPU / {new_disk}GB Disk"
        vps_data[user_id][vps_index] = found_vps
        save_vps_data()
        if was_running:
            await safe_start_container(container_name, node_id)
            found_vps['status'] = 'running'
            save_vps_data()
            await apply_internal_permissions(container_name, node_id)
            await recreate_port_forwards(container_name)
        title = "Resources Added" if mode == "add" else "VPS Resized"
        embed = create_success_embed(title, f"Updated VPS #{vps_number} (`{container_name}`) of {user.mention}")
        add_field(embed, "Changes Applied", "\n".join(changes), False)
        if disk > 0:
            add_field(embed, "Disk Note", "Run `sudo resize2fs /` inside the VPS to expand the filesystem.", False)
        await ctx.send(embed=embed)
    except Exception as e:
        if was_running and found_vps.get('status') == 'stopped':
            try:
                await safe_start_container(container_name, node_id)
                found_vps['status'] = 'running'
                save_vps_data()
            except Exception:
                pass
        await ctx.send(embed=create_error_embed("Resource Change Failed", f"Error: {str(e)}"))

@bot.command(name='add-resources')
@is_admin()
async def add_resources(ctx, ram: int, cpu: int, disk: int, user: discord.Member, vps_number: int = 1):
    """!add-resources <ram_gb> <cpu_cores> <disk_gb> @user [vps_number]  (0 = add nothing for that resource)"""
    await change_vps_resources(ctx, user, vps_number, ram, cpu, disk, mode="add")


@bot.command(name='status')
@is_admin()
async def system_status(ctx):
    """
    Show complete system status including:
    - Bot uptime
    - Total nodes & their status
    - Running/stopped nodes count
    - Total RAM/CPU/DISK allocated vs free
    - Total VPS & users
    - Running/stopped/suspended VPS counts
    - Total admin users
    - Whitelisted VPS
    """
    
    # Start timing for response time
    start_time = time.time()
    
    # Get bot uptime
    bot_start_time = datetime.now() - datetime.fromtimestamp(start_time - bot.latency)
    bot_uptime = str(bot_start_time).split('.')[0]  # Remove microseconds
    
    # Get total nodes
    nodes = get_nodes()
    total_nodes = len(nodes)
    
    # Node status counters
    running_nodes = 0
    stopped_nodes = 0
    local_nodes = 0
    remote_nodes = 0
    
    # Node resource tracking
    total_node_cpu_allocated = 0
    total_node_ram_allocated = 0
    total_node_disk_allocated = 0
    total_node_cpu_free = 0
    total_node_ram_free = 0
    total_node_disk_free = 0
    
    # VPS counters
    total_vps = 0
    total_users = len(vps_data)
    running_vps = 0
    stopped_vps = 0
    suspended_vps = 0
    whitelisted_vps = 0
    
    # Admin counters
    total_admins = len(admin_data.get("admins", [])) + 1  # +1 for main admin
    
    # Port statistics
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT SUM(allocated_ports) FROM port_allocations")
    total_ports_allocated = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM port_forwards")
    total_ports_used = cur.fetchone()[0] or 0
    conn.close()
    
    # Resource counters for all VPS
    total_ram_allocated = 0
    total_cpu_allocated = 0
    total_disk_allocated = 0
    
    # Process all VPS data
    for user_id, vps_list in vps_data.items():
        total_vps += len(vps_list)
        
        for vps in vps_list:
            # Count status
            if vps.get('suspended', False):
                suspended_vps += 1
            elif vps.get('status') == 'running':
                running_vps += 1
            else:
                stopped_vps += 1
            
            # Count whitelisted
            if vps.get('whitelisted', False):
                whitelisted_vps += 1
            
            # Calculate allocated resources
            try:
                ram_gb = int(vps['ram'].replace('GB', ''))
                total_ram_allocated += ram_gb
            except:
                pass
            
            try:
                cpu_cores = int(vps['cpu'])
                total_cpu_allocated += cpu_cores
            except:
                pass
            
            try:
                disk_gb = int(vps['storage'].replace('GB', ''))
                total_disk_allocated += disk_gb
            except:
                pass
    
    # Check node status and calculate free resources
    node_statuses = []
    
    for node in nodes:
        # Determine node type
        if node['is_local']:
            local_nodes += 1
            node_type = "🖥️ Local"
        else:
            remote_nodes += 1
            node_type = "🌐 Remote"
        
        # Check node status
        if node['is_local']:
            status = "🟢 Online"
            running_nodes += 1
            
            # Get local resources (approximate)
            try:
                # Get system memory
                mem_result = subprocess.run(['free', '-m'], capture_output=True, text=True)
                mem_lines = mem_result.stdout.splitlines()
                if len(mem_lines) > 1:
                    mem = mem_lines[1].split()
                    total_ram_mb = int(mem[1])
                    used_ram_mb = int(mem[2])
                    free_ram_mb = total_ram_mb - used_ram_mb
                    total_ram_gb = total_ram_mb / 1024
                    free_ram_gb = free_ram_mb / 1024
                else:
                    total_ram_gb = 0
                    free_ram_gb = 0
                
                # Get CPU cores
                cpu_result = subprocess.run(['nproc'], capture_output=True, text=True)
                total_cpu = int(cpu_result.stdout.strip()) if cpu_result.stdout.strip() else 0
                
                # Get disk space
                disk_result = subprocess.run(['df', '-h', '/'], capture_output=True, text=True)
                disk_lines = disk_result.stdout.splitlines()
                if len(disk_lines) > 1:
                    disk_parts = disk_lines[1].split()
                    total_disk_str = disk_parts[1]
                    # Convert to GB
                    if 'T' in total_disk_str:
                        total_disk = float(total_disk_str.replace('T', '')) * 1024
                    elif 'G' in total_disk_str:
                        total_disk = float(total_disk_str.replace('G', ''))
                    elif 'M' in total_disk_str:
                        total_disk = float(total_disk_str.replace('M', '')) / 1024
                    else:
                        total_disk = 0
                else:
                    total_disk = 0
                
                # Calculate free resources (simplified - actual would need more complex logic)
                free_cpu = max(0, total_cpu - (total_cpu_allocated // total_nodes))  # Approximate
                free_disk = max(0, total_disk - (total_disk_allocated // total_nodes))  # Approximate
                
                # Update totals
                total_node_ram_allocated += total_ram_gb - free_ram_gb
                total_node_cpu_allocated += total_cpu - free_cpu
                total_node_disk_allocated += total_disk - free_disk
                total_node_ram_free += free_ram_gb
                total_node_cpu_free += free_cpu
                total_node_disk_free += free_disk
                
            except Exception as e:
                logger.error(f"Error getting local node resources: {e}")
                status = "⚠️ Unknown"
                total_node_ram_free = 0
                total_node_cpu_free = 0
                total_node_disk_free = 0
        else:
            # Check remote node status
            try:
                response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
                if response.status_code == 200:
                    status = "🟢 Online"
                    running_nodes += 1
                else:
                    status = "🔴 Offline"
                    stopped_nodes += 1
            except:
                status = "🔴 Offline"
                stopped_nodes += 1
        
        # Get current VPS count on this node
        node_vps_count = get_current_vps_count(node['id'])
        capacity = node['total_vps']
        usage_percentage = (node_vps_count / capacity * 100) if capacity > 0 else 0
        
        node_statuses.append(
            f"**{node['name']}** ({node_type})\n"
            f"📍 {node['location']} • 📊 {node_vps_count}/{capacity} VPS ({usage_percentage:.0f}%)\n"
            f"Status: {status}"
        )
    
    # Calculate response time
    response_time = (time.time() - start_time) * 1000
    
    # Create main embed
    embed = create_embed(
        title="📊 System Status Dashboard",
        description=f"**{BOT_NAME}** - Complete System Overview\n*Generated in {response_time:.0f}ms*",
        color=0x1a1a1a
    )
    
    # Bot & Uptime Section
    add_field(embed, "🤖 Bot Status", 
        f"**Uptime:** {bot_uptime}\n"
        f"**Latency:** {round(bot.latency * 1000)}ms\n"
        f"**Version:** {BOT_VERSION}\n"
        f"**Developer:** {BOT_DEVELOPER}", 
        True)
    
    # Nodes Section
    add_field(embed, "🌐 Nodes Overview",
        f"**Total Nodes:** {total_nodes}\n"
        f"**Running:** {running_nodes} 🟢\n"
        f"**Stopped:** {stopped_nodes} 🔴\n"
        f"**Local/Remote:** {local_nodes}/{remote_nodes}",
        True)
    
    # VPS & Users Section
    add_field(embed, "👥 Users & VPS",
        f"**Total Users:** {total_users}\n"
        f"**Total VPS:** {total_vps}\n"
        f"**Running:** {running_vps} 🟢\n"
        f"**Stopped:** {stopped_vps} 🔴\n"
        f"**Suspended:** {suspended_vps} 🟡\n"
        f"**Whitelisted:** {whitelisted_vps} ✅",
        True)
    
    # Resources Section - Allocated vs Free
    add_field(embed, "💾 Resource Allocation",
        f"**RAM Allocated:** {total_ram_allocated} GB\n"
        f"**RAM Free:** {total_node_ram_free:.1f} GB\n"
        f"**CPU Allocated:** {total_cpu_allocated} Cores\n"
        f"**CPU Free:** {total_node_cpu_free:.1f} Cores\n"
        f"**Disk Allocated:** {total_disk_allocated} GB\n"
        f"**Disk Free:** {total_node_disk_free:.1f} GB",
        True)
    
    # System & Admin Section
    add_field(embed, "⚙️ System Information",
        f"**Total Admins:** {total_admins}\n"
        f"**Main Admin(s):** {', '.join(f'<@{mid}>' for mid in main_admin_ids)}\n"
        f"**Ports Allocated:** {total_ports_allocated}\n"
        f"**Ports In Use:** {total_ports_used}\n"
        f"**Ports Available:** {total_ports_allocated - total_ports_used}",
        True)
    
    # Node Details Section (if any nodes exist)
    if node_statuses:
        # Split node statuses into chunks if too long
        node_text = "\n\n".join(node_statuses)
        chunks = [node_text[i:i+1024] for i in range(0, len(node_text), 1024)]
        
        for idx, chunk in enumerate(chunks, 1):
            title = "📡 Node Details" if idx == 1 else f"📡 Node Details (Part {idx})"
            add_field(embed, title, chunk, False)
    
    # System Health Indicator
    health_status = "✅ Excellent"
    health_color = 0x00ff88
    
    if running_nodes == 0:
        health_status = "🔴 Critical - No nodes running"
        health_color = 0xff3366
    elif stopped_nodes > 0:
        health_status = "🟡 Warning - Some nodes offline"
        health_color = 0xffaa00
    elif total_vps == 0:
        health_status = "ℹ️ No VPS deployed"
        health_color = 0x00ccff
    
    add_field(embed, "🏥 System Health", health_status, False)
    
    # Footer with current time
    embed.set_footer(text=f"{BOT_NAME} System Status • Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    icon_url=BOT_IMAGE_URL)
    
    await ctx.send(embed=embed)


@bot.command(name='status-summary')
@is_admin()
async def status_summary(ctx):
    """
    Quick summary of system status
    """
    # Get quick stats
    nodes = get_nodes()
    total_nodes = len(nodes)
    running_nodes = 0
    
    for node in nodes:
        if node['is_local']:
            running_nodes += 1
        else:
            try:
                response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=3)
                if response.status_code == 200:
                    running_nodes += 1
            except:
                pass
    
    total_vps = sum(len(vps_list) for vps_list in vps_data.values())
    total_users = len(vps_data)
    
    # Count VPS status
    running_vps = 0
    stopped_vps = 0
    suspended_vps = 0
    
    for vps_list in vps_data.values():
        for vps in vps_list:
            if vps.get('suspended', False):
                suspended_vps += 1
            elif vps.get('status') == 'running':
                running_vps += 1
            else:
                stopped_vps += 1
    
    embed = create_success_embed(
        "📈 Quick Status Summary",
        f"**Nodes:** {running_nodes}/{total_nodes} 🟢\n"
        f"**VPS:** {total_vps} total\n"
        f"• Running: {running_vps} 🟢\n"
        f"• Stopped: {stopped_vps} 🔴\n"
        f"• Suspended: {suspended_vps} 🟡\n"
        f"**Users:** {total_users} 👥\n"
        f"**Bot Latency:** {round(bot.latency * 1000)}ms"
    )
    
    embed.set_footer(text=f"Use '{PREFIX}status' for detailed information")
    await ctx.send(embed=embed)

@bot.command(name='admin-add')
@is_main_admin()
async def admin_add(ctx, user: discord.Member):
    user_id = str(user.id)
    if user_id in main_admin_ids:
        await ctx.send(embed=create_error_embed("Already Admin", "This user is already the main admin!"))
        return
    if user_id in admin_data.get("admins", []):
        await ctx.send(embed=create_error_embed("Already Admin", f"{user.mention} is already an admin!"))
        return
    admin_data["admins"].append(user_id)
    save_admin_data()
    await ctx.send(embed=create_success_embed("Admin Added", f"{user.mention} is now an admin!"))
    try:
        await user.send(embed=create_embed("🎉 Admin Role Granted", f"You are now an admin by {ctx.author.mention}", 0x00ff88))
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("Notification Failed", f"Could not DM {user.mention}"))

@bot.command(name='admin-remove')
@is_main_admin()
async def admin_remove(ctx, user: discord.Member):
    user_id = str(user.id)
    if user_id in main_admin_ids:
        await ctx.send(embed=create_error_embed("Cannot Remove", "You cannot remove the main admin!"))
        return
    if user_id not in admin_data.get("admins", []):
        await ctx.send(embed=create_error_embed("Not Admin", f"{user.mention} is not an admin!"))
        return
    admin_data["admins"].remove(user_id)
    save_admin_data()
    await ctx.send(embed=create_success_embed("Admin Removed", f"{user.mention} is no longer an admin!"))
    try:
        await user.send(embed=create_embed("⚠️ Admin Role Revoked", f"Your admin role was removed by {ctx.author.mention}", 0xff3366))
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("Notification Failed", f"Could not DM {user.mention}"))

@bot.command(name='add-admin')
@is_main_admin()
async def add_admin_id(ctx, user_id: str):
    """Add a main admin by raw Discord user ID (works even if the user isn't in this server)."""
    if not user_id.isdigit():
        await ctx.send(embed=create_error_embed("Invalid ID", "Please provide a numeric Discord user ID."))
        return
    if user_id in main_admin_ids:
        await ctx.send(embed=create_error_embed("Already Admin", f"`{user_id}` is already a main admin!"))
        return
    main_admin_ids.add(user_id)
    save_main_admins()
    await ctx.send(embed=create_success_embed("Main Admin Added", f"`{user_id}` is now a main admin!"))
    try:
        user = await bot.fetch_user(int(user_id))
        await user.send(embed=create_embed("🎉 Main Admin Access Granted", f"You are now a main admin of {BOT_NAME}, granted by {ctx.author.mention}", 0x00ff88))
    except Exception:
        pass

@bot.command(name='rm-admin')
@is_main_admin()
async def rm_admin_id(ctx, user_id: str):
    """Remove a main admin by raw Discord user ID."""
    if user_id not in main_admin_ids:
        await ctx.send(embed=create_error_embed("Not Admin", f"`{user_id}` is not a main admin!"))
        return
    if len(main_admin_ids) <= 1:
        await ctx.send(embed=create_error_embed("Cannot Remove", "At least one main admin must remain."))
        return
    main_admin_ids.discard(user_id)
    save_main_admins()
    await ctx.send(embed=create_success_embed("Main Admin Removed", f"`{user_id}` is no longer a main admin!"))
    try:
        user = await bot.fetch_user(int(user_id))
        await user.send(embed=create_embed("⚠️ Main Admin Access Revoked", f"Your main admin role was removed by {ctx.author.mention}", 0xff3366))
    except Exception:
        pass

@bot.command(name='admin-list')
@is_main_admin()
async def admin_list(ctx):
    admins = admin_data.get("admins", [])
    embed = create_embed("👑 Admin Team", "Current administrators:", 0x1a1a1a)
    main_admin_list = []
    for main_id in main_admin_ids:
        try:
            main_user = await bot.fetch_user(int(main_id))
            main_admin_list.append(f"• {main_user.mention} (ID: {main_id})")
        except:
            main_admin_list.append(f"• Unknown User (ID: {main_id})")
    add_field(embed, "🔰 Main Admin(s)", "\n".join(main_admin_list) or "None set", False)
    if admins:
        admin_list = []
        for admin_id in admins:
            try:
                admin_user = await bot.fetch_user(int(admin_id))
                admin_list.append(f"• {admin_user.mention} (ID: {admin_id})")
            except:
                admin_list.append(f"• Unknown User (ID: {admin_id})")
        admin_text = "\n".join(admin_list)
        add_field(embed, "🛡️ Admins", admin_text, False)
    else:
        add_field(embed, "🛡️ Admins", "No additional admins", False)
    await ctx.send(embed=embed)

@bot.command(name="userinfo")
@is_admin()
async def user_info(ctx, user: discord.Member):
    user_id = str(user.id)
    vps_list = vps_data.get(user_id, [])

    # ─── Embed ─────────────────────────────────────────────────
    embed = create_embed(
        title="👤 User Dashboard",
        description=f"Statistics & resources for {user.mention}",
        color=0x1A1A1A
    )

    # ─── Row 1 : User Info ─────────────────────────────────────
    embed.add_field(
        name="👤 User",
        value=(
            f"**Name:** `{user.name}`\n"
            f"**ID:** `{user.id}`\n"
            f"**Joined:** `{user.joined_at.strftime('%Y-%m-%d') if user.joined_at else 'Unknown'}`"
        ),
        inline=True
    )

    is_admin_user = user_id in main_admin_ids or user_id in admin_data.get("admins", [])
    embed.add_field(
        name="🛡️ Admin",
        value="✅ Yes" if is_admin_user else "❌ No",
        inline=True
    )

    embed.add_field(
        name="🖥️ VPS Count",
        value=f"`{len(vps_list)}` VPS",
        inline=True
    )

    # ─── If VPS Exists ─────────────────────────────────────────
    if vps_list:
        total_ram = total_cpu = total_storage = 0
        running = suspended = whitelisted = 0

        vps_lines = []

        for i, vps in enumerate(vps_list, start=1):
            node = get_node(vps.get("node_id"))
            node_name = node["name"] if node else "Unknown"

            ram = int(vps.get("ram", "0GB").replace("GB", ""))
            storage = int(vps.get("storage", "0GB").replace("GB", ""))
            cpu = int(vps.get("cpu", 0))

            total_ram += ram
            total_storage += storage
            total_cpu += cpu

            if vps.get("suspended"):
                status = "⛔ SUSPENDED"
                suspended += 1
            elif vps.get("status") == "running":
                status = "🟢 RUNNING"
                running += 1
            else:
                status = "🔴 STOPPED"

            if vps.get("whitelisted"):
                whitelisted += 1

            vps_lines.append(
                f"**{i}.** `{vps['container_name']}`\n"
                f"{status} | `{ram}GB` RAM • `{cpu}` CPU • `{storage}GB` Disk\n"
                f"📍 Node: `{node_name}`"
            )

        # ─── Row 2 : VPS Summary ────────────────────────────────
        embed.add_field(
            name="📊 VPS Summary",
            value=(
                f"🖥️ `{len(vps_list)}` Total\n"
                f"🟢 `{running}` Running\n"
                f"⛔ `{suspended}` Suspended\n"
                f"✅ `{whitelisted}` Whitelisted"
            ),
            inline=True
        )

        embed.add_field(
            name="📈 Resources",
            value=(
                f"**RAM:** `{total_ram} GB`\n"
                f"**CPU:** `{total_cpu} Cores`\n"
                f"**Disk:** `{total_storage} GB`"
            ),
            inline=True
        )

        port_quota = get_user_allocation(user_id)
        port_used = get_user_used_ports(user_id)

        embed.add_field(
            name="🌐 Ports",
            value=f"`{port_used}/{port_quota}` Used",
            inline=True
        )

        # ─── VPS List (Split if needed) ────────────────────────
        vps_text = "\n\n".join(vps_lines)
        for i in range(0, len(vps_text), 1024):
            embed.add_field(
                name="📋 VPS List",
                value=vps_text[i:i + 1024],
                inline=False
            )

    else:
        embed.add_field(
            name="🖥️ VPS",
            value="❌ No VPS assigned",
            inline=False
        )

    embed.set_footer(text="UnixNodes • User Resource Dashboard")
    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)

@bot.command(name="serverstats")
@is_admin()
async def server_stats(ctx):
    # ─── Counts ────────────────────────────────────────────────
    total_users = len(vps_data)
    total_admins = len(admin_data.get("admins", [])) + 1
    total_vps = sum(len(vps_list) for vps_list in vps_data.values())

    total_ram = total_cpu = total_storage = 0
    running_vps = suspended_vps = stopped_vps = 0
    whitelisted_vps = 0

    # ─── VPS Data ──────────────────────────────────────────────
    for vps_list in vps_data.values():
        for vps in vps_list:
            total_ram += int(vps.get("ram", "0GB").replace("GB", ""))
            total_storage += int(vps.get("storage", "0GB").replace("GB", ""))
            total_cpu += int(vps.get("cpu", 0))

            if vps.get("status") == "running":
                if vps.get("suspended", False):
                    suspended_vps += 1
                else:
                    running_vps += 1
            else:
                stopped_vps += 1

            if vps.get("whitelisted", False):
                whitelisted_vps += 1

    # ─── Ports ─────────────────────────────────────────────────
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT SUM(allocated_ports) FROM port_allocations")
    total_ports_allocated = cur.fetchone()[0] or 0

    cur.execute("SELECT COUNT(*) FROM port_forwards")
    total_ports_used = cur.fetchone()[0] or 0
    conn.close()

    # ─── Embed ─────────────────────────────────────────────────
    embed = create_embed(
        title="📊 Server Statistics",
        description="**Live Infrastructure Dashboard**",
        color=0x1A1A1A
    )

    # ── Row 1 ──────────────────────────────────────────────────
    embed.add_field(
        name="👥 Users",
        value=f"`{total_users}` Users\n`{total_admins}` Admins",
        inline=True
    )

    embed.add_field(
        name="🖥️ VPS",
        value=(
            f"Total: `{total_vps}`\n"
            f"🟢 `{running_vps}` Running\n"
            f"⛔ `{suspended_vps}` Suspended"
        ),
        inline=True
    )

    embed.add_field(
        name="📌 Status",
        value=(
            f"🔴 `{stopped_vps}` Stopped\n"
            f"✅ `{whitelisted_vps}` Whitelisted"
        ),
        inline=True
    )

    # ── Row 2 ──────────────────────────────────────────────────
    embed.add_field(
        name="📈 RAM",
        value=f"`{total_ram} GB`",
        inline=True
    )

    embed.add_field(
        name="⚙️ CPU",
        value=f"`{total_cpu} Cores`",
        inline=True
    )

    embed.add_field(
        name="💾 Storage",
        value=f"`{total_storage} GB`",
        inline=True
    )

    # ── Row 3 ──────────────────────────────────────────────────
    embed.add_field(
        name="🌐 Ports Allocated",
        value=f"`{total_ports_allocated}`",
        inline=True
    )

    embed.add_field(
        name="🔌 Ports In Use",
        value=f"`{total_ports_used}`",
        inline=True
    )

    embed.add_field(
        name="📊 Utilization",
        value=(
            f"`{total_ports_used}/{total_ports_allocated}`"
            if total_ports_allocated else "`N/A`"
        ),
        inline=True
    )

    embed.set_footer(text="UnixNodes • Real-Time Monitoring")
    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)

@bot.command(name='vpsinfo')
@is_admin()
async def vps_info(ctx, container_name: str = None):
    if not container_name:
        all_vps = []
        for user_id, vps_list in vps_data.items():
            try:
                user = await bot.fetch_user(int(user_id))
                for i, vps in enumerate(vps_list):
                    node = get_node(vps['node_id'])
                    node_name = node['name'] if node else "Unknown"
                    status_text = vps.get('status', 'unknown').upper()
                    if vps.get('suspended', False):
                        status_text += " (SUSPENDED)"
                    if vps.get('whitelisted', False):
                        status_text += " (WHITELISTED)"
                    all_vps.append(f"**{user.name}** - VPS {i+1}: `{vps['container_name']}` - {status_text} (Node: {node_name})")
            except:
                pass
        vps_text = "\n".join(all_vps)
        chunks = [vps_text[i:i+1024] for i in range(0, len(vps_text), 1024)]
        for idx, chunk in enumerate(chunks, 1):
            embed = create_embed(f"🖥️ All VPS (Part {idx})", f"List of all VPS deployments", 0x1a1a1a)
            add_field(embed, "VPS List", chunk, False)
            await ctx.send(embed=embed)
    else:
        found_vps = None
        found_user = None
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    found_vps = vps
                    found_user = await bot.fetch_user(int(user_id))
                    break
            if found_vps:
                break
        if not found_vps:
            await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS found with container name: `{container_name}`"))
            return
        node = get_node(found_vps['node_id'])
        node_name = node['name'] if node else "Unknown"
        suspended_text = " (SUSPENDED)" if found_vps.get('suspended', False) else ""
        whitelisted_text = " (WHITELISTED)" if found_vps.get('whitelisted', False) else ""
        embed = create_embed(f"🖥️ VPS Information - {container_name}", f"Details for VPS owned by {found_user.mention}{suspended_text}{whitelisted_text} on node {node_name}", 0x1a1a1a)
        add_field(embed, "👤 Owner", f"**Name:** {found_user.name}\n**ID:** {found_user.id}", False)
        add_field(embed, "📊 Specifications", f"**RAM:** {found_vps['ram']}\n**CPU:** {found_vps['cpu']} Cores\n**Storage:** {found_vps['storage']}", False)
        add_field(embed, "📈 Status", f"**Current:** {found_vps.get('status', 'unknown').upper()}{suspended_text}{whitelisted_text}\n**Suspended:** {found_vps.get('suspended', False)}\n**Whitelisted:** {found_vps.get('whitelisted', False)}\n**Created:** {found_vps.get('created_at', 'Unknown')}", False)
        if 'config' in found_vps:
            add_field(embed, "⚙️ Configuration", f"**Config:** {found_vps['config']}", False)
        add_field(embed, "⏳ Expiry", expiry_text(found_vps.get('expires_at')), False)
        if found_vps.get('shared_with'):
            shared_users = []
            for shared_id in found_vps['shared_with']:
                try:
                    shared_user = await bot.fetch_user(int(shared_id))
                    shared_users.append(f"• {shared_user.mention}")
                except:
                    shared_users.append(f"• Unknown User ({shared_id})")
            shared_text = "\n".join(shared_users)
            add_field(embed, "🔗 Shared With", shared_text, False)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM port_forwards WHERE vps_container = ?', (container_name,))
        port_count = cur.fetchone()[0]
        conn.close()
        add_field(embed, "🌐 Active Ports", f"{port_count} forwarded ports (TCP/UDP)", False)
        await ctx.send(embed=embed)

@bot.command(name='restart-vps')
@is_admin()
async def restart_vps(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    ok, err = await check_node_ipv4(node_id, for_start=True)
    if not ok:
        await ctx.send(embed=create_error_embed("This VPS is not IPv4", err))
    await ctx.send(embed=create_info_embed("Restarting VPS", f"Restarting VPS `{container_name}`..."))
    try:
        await execute_lxc(container_name, f"restart {container_name}", node_id=node_id)
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    vps['status'] = 'running'
                    save_vps_data()
                    break
        await apply_internal_permissions(container_name, node_id)
        await recreate_port_forwards(container_name)
        await ctx.send(embed=create_success_embed("VPS Restarted", f"VPS `{container_name}` has been restarted successfully!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restart Failed", f"Error: {str(e)}"))

@bot.command(name='exec')
@is_admin()
async def execute_command(ctx, container_name: str, *, command: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Executing Command", f"Running command in VPS `{container_name}`..."))
    try:
        output = await execute_lxc(container_name, f"exec {container_name} -- bash -c \"{command}\"", node_id=node_id)
        embed = create_embed(f"Command Output - {container_name}", f"Command: `{command}`", 0x1a1a1a)
        if output.strip():
            if len(output) > 1000:
                output = output[:1000] + "\n... (truncated)"
            add_field(embed, "📤 Output", f"```\n{output}\n```", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Execution Failed", f"Error: {str(e)}"))

@bot.command(name='stop-vps-all')
@is_admin()
async def stop_all_vps(ctx):
    embed = create_warning_embed("Stopping All VPS", "⚠️ **WARNING:** This will stop ALL running VPS on all nodes.\n\nThis action cannot be undone. Continue?")
    class ConfirmView(discord.ui.View):
        def __init__(self, author_id: int):
            super().__init__(timeout=60)
            self.author_id = author_id

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.author_id:
                await interaction.response.send_message(
                    embed=create_error_embed("Access Denied", "Only the admin who ran this command can use these buttons."),
                    ephemeral=True)
                return False
            return True

        @discord.ui.button(label="Stop All VPS", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.defer()
            try:
                stopped_count = 0
                nodes = get_nodes()
                for node in nodes:
                    if node['is_local']:
                        proc = await asyncio.create_subprocess_exec(
                            "lxc", "stop", "--all", "--force",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        stdout, stderr = await proc.communicate()
                        if proc.returncode != 0:
                            logger.error(f"Failed to stop all on local node: {stderr.decode()}")
                            continue
                    else:
                        url = f"{node['url']}/api/execute"
                        data = {"command": "lxc stop --all --force"}
                        params = {"api_key": node["api_key"]}
                        response = requests.post(url, json=data, params=params)
                        if response.status_code != 200:
                            logger.error(f"Failed to stop all on node {node['name']}")
                            continue
                    for user_id, vps_list in vps_data.items():
                        for vps in vps_list:
                            if vps.get('node_id') == node['id'] and vps.get('status') == 'running':
                                vps['status'] = 'stopped'
                                vps['suspended'] = False
                                stopped_count += 1
                save_vps_data()
                embed = create_success_embed("All VPS Stopped", f"Successfully stopped {stopped_count} VPS across all nodes.")
                await interaction.followup.send(embed=embed)
            except Exception as e:
                embed = create_error_embed("Error", f"Error stopping VPS: {str(e)}")
                await interaction.followup.send(embed=embed)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.edit_message(embed=create_info_embed("Operation Cancelled", "The stop all VPS operation has been cancelled."))

    await ctx.send(embed=embed, view=ConfirmView(ctx.author.id))

@bot.command(name='cpu-monitor')
@is_admin()
async def resource_monitor_control(ctx, action: str = "status"):
    global resource_monitor_active
    if action.lower() == "status":
        status = "Active" if resource_monitor_active else "Inactive"
        embed = create_embed("Resource Monitor Status", f"Resource monitoring is currently **{status}** (logging + 24/7 usage alerts to the main admins)", 0x00ccff if resource_monitor_active else 0xffaa00)
        add_field(embed, "Thresholds", f"{CPU_THRESHOLD}% CPU / {RAM_THRESHOLD}% RAM usage", True)
        add_field(embed, "Check Interval", f"60 seconds (all nodes)", True)
        await ctx.send(embed=embed)
    elif action.lower() == "enable":
        resource_monitor_active = True
        await ctx.send(embed=create_success_embed("Resource Monitor Enabled", "Resource monitoring has been enabled."))
    elif action.lower() == "disable":
        resource_monitor_active = False
        await ctx.send(embed=create_warning_embed("Resource Monitor Disabled", "Resource monitoring has been disabled."))
    else:
        await ctx.send(embed=create_error_embed("Invalid Action", f"Use: `{PREFIX}cpu-monitor <status|enable|disable>`"))

@bot.command(name='resize-vps')
@is_admin()
async def resize_vps(ctx, ram: int, cpu: int, disk: int, user: discord.Member, vps_number: int = 1):
    """!resize-vps <ram_gb> <cpu_cores> <disk_gb> @user [vps_number]  (0 = keep current value)"""
    await change_vps_resources(ctx, user, vps_number, ram, cpu, disk, mode="set")


@bot.command(name='clone-vps')
@is_admin()
async def clone_vps(ctx, container_name: str, new_name: str = None):
    if not new_name:
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        new_name = f"{BOT_NAME.lower()}-{container_name}-clone-{timestamp}"
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Cloning VPS", f"Cloning VPS `{container_name}` to `{new_name}`..."))
    try:
        found_vps = None
        user_id = None
        for uid, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    found_vps = vps
                    user_id = uid
                    break
            if found_vps:
                break
        if not found_vps:
            await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS found with container name: `{container_name}`"))
            return
        await execute_lxc(container_name, f"copy {container_name} {new_name}", node_id=node_id)
        await apply_lxc_config(new_name, node_id)
        await execute_lxc(new_name, f"start {new_name}", node_id=node_id)
        await apply_internal_permissions(new_name, node_id)
        await recreate_port_forwards(new_name)
        if user_id not in vps_data:
            vps_data[user_id] = []
        new_vps = found_vps.copy()
        new_vps['container_name'] = new_name
        new_vps['status'] = 'running'
        new_vps['suspended'] = False
        new_vps['whitelisted'] = False
        new_vps['suspension_history'] = []
        new_vps['created_at'] = datetime.now().isoformat()
        new_vps['shared_with'] = []
        new_vps['expires_at'] = default_expiry_iso() or found_vps.get('expires_at')
        new_vps['expiry_notified'] = []
        new_vps['id'] = None
        vps_data[user_id].append(new_vps)
        save_vps_data()
        embed = create_success_embed("VPS Cloned", f"Successfully cloned VPS `{container_name}` to `{new_name}`")
        add_field(embed, "New VPS Details", f"**RAM:** {new_vps['ram']}\n**CPU:** {new_vps['cpu']} Cores\n**Storage:** {new_vps['storage']}", False)
        add_field(embed, "Features", "Nesting, Privileged, FUSE, Kernel Modules (Docker Ready), Unprivileged Ports from 0", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Clone Failed", f"Error: {str(e)}"))

@bot.command(name='migrate-vps')
@is_admin()
async def migrate_vps(ctx, container_name: str, target_node_id: int):
    node_id = find_node_id_for_container(container_name)
    target_node = get_node(target_node_id)
    if not target_node:
        await ctx.send(embed=create_error_embed("Invalid Node", "Target node not found."))
        return
    await ctx.send(embed=create_info_embed("Migrating VPS", f"Migrating VPS `{container_name}` to node {target_node['name']}..."))
    try:
        await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
        temp_name = f"{BOT_NAME.lower()}-{container_name}-temp-{int(time.time())}"
        await execute_lxc(container_name, f"copy {container_name} {temp_name} -s {DEFAULT_STORAGE_POOL}", node_id=target_node_id)
        await execute_lxc(container_name, f"delete {container_name} --force", node_id=node_id)
        await execute_lxc(temp_name, f"rename {temp_name} {container_name}", node_id=target_node_id)
        await apply_lxc_config(container_name, target_node_id)
        await safe_start_container(container_name, target_node_id)
        await apply_internal_permissions(container_name, target_node_id)
        await recreate_port_forwards(container_name)
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    vps['node_id'] = target_node_id
                    vps['status'] = 'running'
                    vps['suspended'] = False
                    save_vps_data()
                    break
        await ctx.send(embed=create_success_embed("VPS Migrated", f"Successfully migrated VPS `{container_name}` to node {target_node['name']}"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Migration Failed", f"Error: {str(e)}"))

@bot.command(name='vps-stats')
@is_admin()
async def vps_stats(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Gathering Statistics", f"Collecting statistics for VPS `{container_name}`..."))
    try:
        stats = await get_container_stats(container_name, node_id)
        embed = create_embed(f"📊 VPS Statistics - {container_name}", f"Resource usage statistics", 0x1a1a1a)
        add_field(embed, "📈 Status", f"**{stats['status'].upper()}**", False)
        add_field(embed, "💻 CPU Usage", f"**{stats['cpu']:.1f}%**", True)
        add_field(embed, "🧠 Memory Usage", f"**{stats['ram']['used']}/{stats['ram']['total']} MB ({stats['ram']['pct']:.1f}%)**", True)
        add_field(embed, "💾 Disk Usage", f"**{stats['disk']}**", True)
        add_field(embed, "⏱️ Uptime", f"**{stats['uptime']}**", True)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Statistics Failed", f"Error: {str(e)}"))


@bot.command(name='node-check')
@is_admin()
async def node_check(ctx, node_id: int):
    """Check node status and available storage pools"""
    node = get_node(node_id)
    if not node:
        await ctx.send(embed=create_error_embed("Node Not Found", f"Node ID {node_id} not found."))
        return
    
    embed = create_info_embed(f"Node Check - {node['name']}", 
                             f"Checking status and configuration of node {node['name']}...")
    
    # Check if node is reachable
    status = await get_node_status(node_id)
    add_field(embed, "📡 Connection Status", status, False)
    
    if status.startswith("🟢"):
        # Try to get storage pools
        try:
            pools_output = await execute_lxc("", "storage list", node_id=node_id, timeout=30)
            add_field(embed, "💾 Available Storage Pools", f"```{pools_output}```", False)
            
            # Try to get default profile
            try:
                profile_output = await execute_lxc("", "profile list", node_id=node_id, timeout=30)
                add_field(embed, "📋 Available Profiles", f"```{profile_output[:500]}...```", False)
            except Exception as e:
                add_field(embed, "📋 Profiles", f"Error: {str(e)[:200]}", False)
                
        except Exception as e:
            add_field(embed, "💾 Storage Pools", f"Error: {str(e)[:200]}", False)
        
        # Check remote API endpoint
        try:
            test_response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
            add_field(embed, "🔌 API Endpoint", f"✅ Reachable\nURL: {node['url']}", False)
        except Exception as e:
            add_field(embed, "🔌 API Endpoint", f"❌ Unreachable\nError: {str(e)[:200]}", False)
    else:
        add_field(embed, "⚠️ Status", "Node is offline or unreachable", False)
    
    await ctx.send(embed=embed)

@bot.command(name='vps-network')
@is_admin()
async def vps_network(ctx, container_name: str, action: str, value: str = None):
    node_id = find_node_id_for_container(container_name)
    if action.lower() not in ["list", "add", "remove", "limit"]:
        await ctx.send(embed=create_error_embed("Invalid Action", f"Use: `{PREFIX}vps-network <container> <list|add|remove|limit> [value]`"))
        return
    try:
        if action.lower() == "list":
            output = await execute_lxc(container_name, f"exec {container_name} -- ip addr", node_id=node_id)
            if len(output) > 1000:
                output = output[:1000] + "\n... (truncated)"
            embed = create_embed(f"🌐 Network Interfaces - {container_name}", "Network configuration", 0x1a1a1a)
            add_field(embed, "Interfaces", f"```\n{output}\n```", False)
            await ctx.send(embed=embed)
        elif action.lower() == "limit" and value:
            await execute_lxc(container_name, f"config device set {container_name} eth0 limits.egress {value}", node_id=node_id)
            await execute_lxc(container_name, f"config device set {container_name} eth0 limits.ingress {value}", node_id=node_id)
            await ctx.send(embed=create_success_embed("Network Limited", f"Set network limit to {value} for `{container_name}`"))
        elif action.lower() == "add" and value:
            await execute_lxc(container_name, f"config device add {container_name} eth1 nic nictype=bridged parent={value}", node_id=node_id)
            await ctx.send(embed=create_success_embed("Network Added", f"Added network interface to VPS `{container_name}` with bridge `{value}`"))
        elif action.lower() == "remove" and value:
            await execute_lxc(container_name, f"config device remove {container_name} {value}", node_id=node_id)
            await ctx.send(embed=create_success_embed("Network Removed", f"Removed network interface `{value}` from VPS `{container_name}`"))
        else:
            await ctx.send(embed=create_error_embed("Invalid Parameters", "Please provide valid parameters for the action"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Network Management Failed", f"Error: {str(e)}"))

@bot.command(name='vps-processes')
@is_admin()
async def vps_processes(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Gathering Processes", f"Listing processes in VPS `{container_name}`..."))
    try:
        output = await execute_lxc(container_name, f"exec {container_name} -- ps aux", node_id=node_id)
        if len(output) > 1000:
            output = output[:1000] + "\n... (truncated)"
        embed = create_embed(f"⚙️ Processes - {container_name}", "Running processes", 0x1a1a1a)
        add_field(embed, "Process List", f"```\n{output}\n```", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Process Listing Failed", f"Error: {str(e)}"))

@bot.command(name='vps-logs')
@is_admin()
async def vps_logs(ctx, container_name: str, lines: int = 50):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Gathering Logs", f"Fetching last {lines} lines from VPS `{container_name}`..."))
    try:
        output = await execute_lxc(container_name, f"exec {container_name} -- journalctl -n {lines}", node_id=node_id)
        if len(output) > 1000:
            output = output[:1000] + "\n... (truncated)"
        embed = create_embed(f"📋 Logs - {container_name}", f"Last {lines} log lines", 0x1a1a1a)
        add_field(embed, "System Logs", f"```\n{output}\n```", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Log Retrieval Failed", f"Error: {str(e)}"))

@bot.command(name='vps-uptime')
@is_admin()
async def vps_uptime(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    uptime = await get_container_uptime(container_name, node_id)
    embed = create_info_embed("VPS Uptime", f"Uptime for `{container_name}`: {uptime}")
    await ctx.send(embed=embed)

@bot.command(name='suspend-vps')
@is_admin()
async def suspend_vps(ctx, container_name: str, *, reason: str = "Admin action"):
    node_id = find_node_id_for_container(container_name)
    found = False
    for uid, lst in vps_data.items():
        for vps in lst:
            if vps['container_name'] == container_name:
                if vps.get('status') != 'running':
                    await ctx.send(embed=create_error_embed("Cannot Suspend", "VPS must be running to suspend."))
                    return
                try:
                    await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
                    vps['status'] = 'stopped'
                    vps['suspended'] = True
                    if 'suspension_history' not in vps:
                        vps['suspension_history'] = []
                    vps['suspension_history'].append({
                        'time': datetime.now().isoformat(),
                        'reason': reason,
                        'by': f"{ctx.author.name} ({ctx.author.id})"
                    })
                    save_vps_data()
                except Exception as e:
                    await ctx.send(embed=create_error_embed("Suspend Failed", str(e)))
                    return
                try:
                    owner = await bot.fetch_user(int(uid))
                    embed = create_warning_embed("🚨 VPS Suspended", f"Your VPS `{container_name}` has been suspended by an admin.\n\n**Reason:** {reason}\n\nContact an admin to unsuspend.")
                    await owner.send(embed=embed)
                except Exception as dm_e:
                    logger.error(f"Failed to DM owner {uid}: {dm_e}")
                await ctx.send(embed=create_success_embed("VPS Suspended", f"VPS `{container_name}` suspended. Reason: {reason}"))
                found = True
                break
        if found:
            break
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))

@bot.command(name='unsuspend-vps')
@is_admin()
async def unsuspend_vps(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    found = False
    for uid, lst in vps_data.items():
        for vps in lst:
            if vps['container_name'] == container_name:
                if not vps.get('suspended', False):
                    await ctx.send(embed=create_error_embed("Not Suspended", "VPS is not suspended."))
                    return
                try:
                    vps['suspended'] = False
                    vps['status'] = 'running'
                    await safe_start_container(container_name, node_id)
                    await apply_internal_permissions(container_name, node_id)
                    await recreate_port_forwards(container_name)
                    save_vps_data()
                    await ctx.send(embed=create_success_embed("VPS Unsuspended", f"VPS `{container_name}` unsuspended and started."))
                    found = True
                except Exception as e:
                    await ctx.send(embed=create_error_embed("Start Failed", str(e)))
                try:
                    owner = await bot.fetch_user(int(uid))
                    embed = create_success_embed("🟢 VPS Unsuspended", f"Your VPS `{container_name}` has been unsuspended by an admin.\nYou can now manage it again.")
                    await owner.send(embed=embed)
                except Exception as dm_e:
                    logger.error(f"Failed to DM owner {uid} about unsuspension: {dm_e}")
                break
        if found:
            break
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))

@bot.command(name='suspension-logs')
@is_admin()
async def suspension_logs(ctx, container_name: str = None):
    if container_name:
        found = None
        for lst in vps_data.values():
            for vps in lst:
                if vps['container_name'] == container_name:
                    found = vps
                    break
            if found:
                break
        if not found:
            await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))
            return
        history = found.get('suspension_history', [])
        if not history:
            await ctx.send(embed=create_info_embed("No Suspensions", f"No suspension history for `{container_name}`."))
            return
        embed = create_embed("Suspension History", f"For `{container_name}`")
        text = []
        for h in sorted(history, key=lambda x: x['time'], reverse=True)[:10]:
            t = datetime.fromisoformat(h['time']).strftime('%Y-%m-%d %H:%M:%S')
            text.append(f"**{t}** - {h['reason']} (by {h['by']})")
        add_field(embed, "History", "\n".join(text), False)
        if len(history) > 10:
            add_field(embed, "Note", "Showing last 10 entries.")
        await ctx.send(embed=embed)
    else:
        all_logs = []
        for uid, lst in vps_data.items():
            for vps in lst:
                h = vps.get('suspension_history', [])
                for event in sorted(h, key=lambda x: x['time'], reverse=True):
                    t = datetime.fromisoformat(event['time']).strftime('%Y-%m-%d %H:%M')
                    all_logs.append(f"**{t}** - VPS `{vps['container_name']}` (Owner: <@{uid}>) - {event['reason']} (by {event['by']})")
        if not all_logs:
            await ctx.send(embed=create_info_embed("No Suspensions", "No suspension events recorded."))
            return
        logs_text = "\n".join(all_logs)
        chunks = [logs_text[i:i+1024] for i in range(0, len(logs_text), 1024)]
        for idx, chunk in enumerate(chunks, 1):
            embed = create_embed(f"Suspension Logs (Part {idx})", f"Global suspension events (newest first)")
            add_field(embed, "Events", chunk, False)
            await ctx.send(embed=embed)

@bot.command(name='apply-permissions')
@is_admin()
async def apply_permissions(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Applying Permissions", f"Applying advanced permissions to `{container_name}`..."))
    try:
        status = await get_container_status(container_name, node_id)
        was_running = status == 'running'
        if was_running:
            await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
        await apply_lxc_config(container_name, node_id)
        await safe_start_container(container_name, node_id)
        await apply_internal_permissions(container_name, node_id)
        await recreate_port_forwards(container_name)
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    vps['status'] = 'running'
                    vps['suspended'] = False
                    save_vps_data()
                    break
        await ctx.send(embed=create_success_embed("Permissions Applied", f"Advanced permissions applied to VPS `{container_name}`. Docker-ready with unprivileged ports!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Apply Failed", f"Error: {str(e)}"))

@bot.command(name='resource-check')
@is_admin()
async def resource_check(ctx):
    suspended_count = 0
    embed = create_info_embed("Resource Check", "Checking all running VPS for high resource usage...")
    msg = await ctx.send(embed=embed)
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            if vps.get('status') == 'running' and not vps.get('suspended', False) and not vps.get('whitelisted', False):
                container = vps['container_name']
                node_id = vps['node_id']
                stats = await get_container_stats(container, node_id)
                cpu = stats['cpu']
                ram = stats['ram']['pct']
                if cpu > CPU_THRESHOLD or ram > RAM_THRESHOLD:
                    reason = f"High resource usage: CPU {cpu:.1f}%, RAM {ram:.1f}% (threshold: {CPU_THRESHOLD}% CPU / {RAM_THRESHOLD}% RAM)"
                    logger.warning(f"Suspending {container}: {reason}")
                    try:
                        await execute_lxc(container, f"stop {container}", node_id=node_id)
                        vps['status'] = 'stopped'
                        vps['suspended'] = True
                        if 'suspension_history' not in vps:
                            vps['suspension_history'] = []
                        vps['suspension_history'].append({
                            'time': datetime.now().isoformat(),
                            'reason': reason,
                            'by': 'Manual Resource Check'
                        })
                        save_vps_data()
                        try:
                            owner = await bot.fetch_user(int(user_id))
                            warn_embed = create_warning_embed("🚨 VPS Auto-Suspended", f"Your VPS `{container}` has been suspended due to high resource usage.\n\n**Reason:** {reason}\n\nContact admin to unsuspend and address the issue.")
                            await owner.send(embed=warn_embed)
                        except Exception as dm_e:
                            logger.error(f"Failed to DM owner {user_id}: {dm_e}")
                        suspended_count += 1
                    except Exception as e:
                        logger.error(f"Failed to suspend {container}: {e}")
    final_embed = create_info_embed("Resource Check Complete", f"Checked all VPS. Suspended {suspended_count} high-usage VPS.")
    await msg.edit(embed=final_embed)

@bot.command(name='whitelist-vps')
@is_admin()
async def whitelist_vps(ctx, container_name: str, action: str):
    if action.lower() not in ['add', 'remove']:
        await ctx.send(embed=create_error_embed("Invalid Action", f"Use: `{PREFIX}whitelist-vps <container> <add|remove>`"))
        return
    found = False
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            if vps['container_name'] == container_name:
                if action.lower() == 'add':
                    vps['whitelisted'] = True
                    msg = "added to whitelist (exempt from auto-suspension)"
                else:
                    vps['whitelisted'] = False
                    msg = "removed from whitelist"
                save_vps_data()
                await ctx.send(embed=create_success_embed("Whitelist Updated", f"VPS `{container_name}` {msg}."))
                found = True
                break
        if found:
            break
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))

@bot.command(name='snapshot')
@is_admin()
async def snapshot_vps(ctx, container_name: str, snap_name: str = "snap0"):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Creating Snapshot", f"Creating snapshot '{snap_name}' for `{container_name}`..."))
    try:
        await execute_lxc(container_name, f"snapshot {container_name} {snap_name}", node_id=node_id)
        await ctx.send(embed=create_success_embed("Snapshot Created", f"Snapshot '{snap_name}' created for VPS `{container_name}`."))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Snapshot Failed", f"Error: {str(e)}"))

@bot.command(name='list-snapshots')
@is_admin()
async def list_snapshots(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    try:
        result = await execute_lxc(container_name, f"snapshot list {container_name}", node_id=node_id)
        embed = create_info_embed(f"Snapshots for {container_name}", result)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("List Failed", f"Error: {str(e)}"))

@bot.command(name='restore-snapshot')
@is_admin()
async def restore_snapshot(ctx, container_name: str, snap_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_warning_embed("Restore Snapshot", f"Restoring snapshot '{snap_name}' for `{container_name}` will overwrite current state. Continue?"))
    class RestoreConfirm(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.button(label="Confirm Restore", style=discord.ButtonStyle.danger)
        async def confirm(self, inter: discord.Interaction, item: discord.ui.Button):
            await inter.response.defer()
            try:
                await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
                await execute_lxc(container_name, f"restore {container_name} {snap_name}", node_id=node_id)
                await safe_start_container(container_name, node_id)
                await apply_internal_permissions(container_name, node_id)
                await recreate_port_forwards(container_name)
                for uid, lst in vps_data.items():
                    for vps in lst:
                        if vps['container_name'] == container_name:
                            vps['status'] = 'running'
                            vps['suspended'] = False
                            save_vps_data()
                            break
                await inter.followup.send(embed=create_success_embed("Snapshot Restored", f"Restored '{snap_name}' for VPS `{container_name}`."))
            except Exception as e:
                await inter.followup.send(embed=create_error_embed("Restore Failed", f"Error: {str(e)}"))

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, inter: discord.Interaction, item: discord.ui.Button):
            await inter.response.edit_message(embed=create_info_embed("Cancelled", "Snapshot restore cancelled."))

    await ctx.send(view=RestoreConfirm())

@bot.command(name='backup-db')
@is_admin()
async def backup_db(ctx):
    backup_name = f"vps_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    try:
        shutil.copy('vps.db', backup_name)
        if os.path.exists('vps.db-wal'):
            shutil.copy('vps.db-wal', f"{backup_name}-wal")
        if os.path.exists('vps.db-shm'):
            shutil.copy('vps.db-shm', f"{backup_name}-shm")
        await ctx.send(embed=create_success_embed("DB Backup Created", f"Backup saved as `{backup_name}` (and WAL/SHM if applicable)."))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Backup Failed", f"Error: {str(e)}"))

@bot.command(name='repair-ports')
@is_admin()
async def repair_ports(ctx, container_name: str):
    await ctx.send(embed=create_info_embed("Repairing Ports", f"Re-adding port forward devices for `{container_name}`..."))
    try:
        readded = await recreate_port_forwards(container_name)
        await ctx.send(embed=create_success_embed("Ports Repaired", f"Re-added {readded} port forwards for `{container_name}`."))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Repair Failed", f"Error: {str(e)}"))

@bot.command(name='about')
async def about(ctx):
    embed = discord.Embed(
        title="☁️ ABOUT US",
        description=(
            "Our VPS Bot is an automated VPS management system designed to create "
            "and manage virtual servers through simple Discord commands.\n\n"
            "👨‍💻 **Developed by MineFlareX**\n\n"
            "🚀 **Reliable • Fast • Efficient**"
        ),
        color=0x5865F2
    )
    embed.set_thumbnail(url=BOT_IMAGE_URL)
    await ctx.send(embed=embed)


@bot.command(name='quickhelp')
async def quick_help(ctx):
    """Show quick reference for common tasks"""
    user_id = str(ctx.author.id)
    is_admin_user = user_id in main_admin_ids or user_id in admin_data.get("admins", [])
    
    embed = create_info_embed("🚀 Quick Help Reference", 
        f"Quick reference for common tasks. Use `{PREFIX}help` for complete command list.")
    
    # Common user tasks
    add_field(embed, "👤 For Users", 
        f"• `{PREFIX}myvps` - List your VPS\n"
        f"• `{PREFIX}manage` - Start/stop/manage VPS\n"
        f"• `{PREFIX}ports` - Manage port forwarding\n"
        f"• `{PREFIX}share-user @user 1` - Share VPS #1\n"
        f"• `{PREFIX}about` - Bot information", False)
    
    # VPS management
    add_field(embed, "🖥️ VPS Control", 
        f"• In `{PREFIX}manage`: Click ▶ to start VPS\n"
        f"• In `{PREFIX}manage`: Click ⏸ to stop VPS\n"
        f"• In `{PREFIX}manage`: Click 🔑 for SSH access\n"
        f"• In `{PREFIX}manage`: Click 📊 for live stats\n"
        f"• In `{PREFIX}manage`: Click 🔄 to reinstall OS", False)
    
    # Troubleshooting
    add_field(embed, "🔧 Common Issues", 
        f"• Ports not working? Use `{PREFIX}repair-ports <container>` (admin)\n"
        "• VPS suspended? Contact admin to unsuspend\n"
        "• Need more resources? Contact admin for upgrade\n"
        "• SSH not working? Try reinstall with different OS", False)
    
    if is_admin_user:
        add_field(embed, "🛡️ Admin Quick Actions", 
            f"• `{PREFIX}create 2 2 20 @user` - Create 2GB/2CPU/20GB VPS\n"
            f"• `{PREFIX}userinfo @user` - Check user details\n"
            f"• `{PREFIX}node list` - List all nodes\n"
            f"• `{PREFIX}serverstats` - System overview\n"
            f"• `{PREFIX}suspend-vps <container> <reason>` - Suspend VPS", False)
    
    embed.set_footer(text=f"{BOT_NAME} VPS Manager • Use {PREFIX}help for complete command list")
    await ctx.send(embed=embed)

@bot.command(name='help-search')
async def help_search(ctx, *, search_term: str = None):
    """Search for commands"""
    if not search_term:
        await show_help(ctx)
        return
    
    search_term = search_term.lower()
    user_id = str(ctx.author.id)
    is_admin_user = user_id in main_admin_ids or user_id in admin_data.get("admins", [])
    is_main_admin_user = user_id in main_admin_ids
    
    # Build complete command list based on permissions
    all_commands = []
    
    # User commands (always available)
    user_categories = ["user", "vps", "ports", "system", "bot"]
    for cat in user_categories:
        all_commands.extend(HelpView(ctx).command_categories[cat]["commands"])
    
    # Admin commands
    if is_admin_user:
        all_commands.extend(HelpView(ctx).command_categories["admin"]["commands"])
        all_commands.extend(HelpView(ctx).command_categories["nodes"]["commands"])
    
    # Main admin commands
    if is_main_admin_user:
        all_commands.extend(HelpView(ctx).command_categories["main_admin"]["commands"])
    
    # Search through commands
    matches = []
    for cmd, desc in all_commands:
        if (search_term in cmd.lower() or search_term in desc.lower()):
            matches.append((cmd, desc))
    
    if not matches:
        embed = create_info_embed("🔍 No Results Found",
            f"No commands found matching '{search_term}'. Try a different search term.")
        await ctx.send(embed=embed)
        return
    
    # Show results
    embed = create_info_embed(f"🔍 Search Results for '{search_term}'",
        f"Found {len(matches)} command(s) matching your search.")
    
    # Group matches by category
    results_text = "\n".join([f"**{cmd}** - {desc}" for cmd, desc in matches[:15]])
    add_field(embed, "Matching Commands", results_text, False)
    
    if len(matches) > 15:
        add_field(embed, "Note", f"Showing 15 of {len(matches)} matches. Try a more specific search.", False)
    
    embed.set_footer(text=f"{BOT_NAME} VPS Manager • Use {PREFIX}help for complete list")
    await ctx.send(embed=embed)    

@bot.command(name='node')
@is_admin()
async def node_cmd(ctx, sub: str, *args):
    if sub == 'create':
        await ctx.send("Enter node name:")
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel
        name = (await bot.wait_for('message', check=check)).content.strip()
        await ctx.send("Enter location:")
        location = (await bot.wait_for('message', check=check)).content.strip()
        await ctx.send("Enter total VPS capacity:")
        total_vps_str = (await bot.wait_for('message', check=check)).content.strip()
        try:
            total_vps = int(total_vps_str)
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid Input", "Total VPS must be an integer."))
            return
        await ctx.send("Enter tags (comma separated):")
        tags_str = (await bot.wait_for('message', check=check)).content.strip()
        tags = [t.strip() for t in tags_str.split(',') if t.strip()]
        tags_json = json.dumps(tags)
        await ctx.send("Enter node URL (e.g., http://ip:port) or leave blank for local:")
        url_str = (await bot.wait_for('message', check=check)).content.strip()
        url = url_str if url_str else None
        is_local = 1 if not url else 0
        api_key = None if is_local else ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=32))
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute('INSERT INTO nodes (name, location, total_vps, tags, api_key, url, is_local) VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (name, location, total_vps, tags_json, api_key, url, is_local))
            conn.commit()
            node_id = cur.lastrowid
            embed = create_success_embed("Node Created", f"ID: {node_id}\nName: {name}\nLocation: {location}\nCapacity: {total_vps}\nTags: {', '.join(tags)}")
            if not is_local:
                add_field(embed, "URL", url, False)
                add_field(embed, "API Key", "🔒 Sent to you in DM (never shown in public channels).", False)
            await ctx.send(embed=embed)
            if not is_local:
                key_embed = create_info_embed("🔑 Node API Key", f"Node **{name}** (ID: {node_id})")
                add_field(key_embed, "API Key", f"`{api_key}`", False)
                add_field(key_embed, "Setup", f"Run on the node server:\n```python3 node-agent.py --api_key={api_key} --port=PORT```", False)
                add_field(key_embed, "⚠️ Security", "Keep this key private.", False)
                try:
                    await ctx.author.send(embed=key_embed)
                except discord.Forbidden:
                    await ctx.send(embed=create_warning_embed("DM Failed", f"Could not DM you the API key. Enable DMs from server members, then use `{PREFIX}node edit` and choose 'y' to regenerate the key."))
        except sqlite3.IntegrityError:
            await ctx.send(embed=create_error_embed("Error", "Node name already exists."))
        conn.close()
    elif sub == 'list':
        nodes = get_nodes()
        embed = create_info_embed("Nodes List", "")
        for n in nodes:
            status = "Local" if n['is_local'] else "Down"
            if not n['is_local']:
                try:
                    response = requests.get(f"{n['url']}/api/ping", params={'api_key': n['api_key']}, timeout=5)
                    status = "Up" if response.status_code == 200 else "Down"
                except:
                    pass
            field = f"ID: {n['id']}\nName: {n['name']}\nLocation: {n['location']}\nCapacity: {n['total_vps']}\nTags: {', '.join(n['tags'])}\nStatus: {status}"
            if not n['is_local']:
                field += f"\nURL: {n['url']}"
            add_field(embed, f"Node {n['id']}", field, False)
        await ctx.send(embed=embed)
    elif sub == 'edit':
        if not args:
            await ctx.send(embed=create_error_embed("Usage", f"{PREFIX}node edit <id>"))
            return
        try:
            node_id = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Node ID must be an integer."))
            return
        node = get_node(node_id)
        if not node:
            await ctx.send(embed=create_error_embed("Not Found", "Node not found."))
            return
        await ctx.send(f"Editing node {node['name']}. Enter new name ( . to skip):")
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel
        new_name = (await bot.wait_for('message', check=check)).content.strip()
        if new_name != '.':
            node['name'] = new_name
        await ctx.send("New location ( . to skip):")
        new_loc = (await bot.wait_for('message', check=check)).content.strip()
        if new_loc != '.':
            node['location'] = new_loc
        await ctx.send("New total VPS capacity ( . to skip):")
        new_total = (await bot.wait_for('message', check=check)).content.strip()
        if new_total != '.':
            node['total_vps'] = int(new_total)
        await ctx.send("New tags (comma separated, . to skip):")
        new_tags = (await bot.wait_for('message', check=check)).content.strip()
        if new_tags != '.':
            node['tags'] = json.dumps([t.strip() for t in new_tags.split(',') if t.strip()])
        if not node['is_local']:
            await ctx.send("New URL ( . to skip):")
            new_url = (await bot.wait_for('message', check=check)).content.strip()
            if new_url != '.':
                node['url'] = new_url
            await ctx.send("Regenerate API key? (y/n):")
            regen = (await bot.wait_for('message', check=check)).content.strip().lower()
            if regen == 'y':
                node['api_key'] = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=32))
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE nodes SET name=?, location=?, total_vps=?, tags=?, api_key=?, url=? WHERE id=?',
                    (node['name'], node['location'], node['total_vps'], json.dumps(node['tags']), node.get('api_key'), node.get('url'), node_id))
        conn.commit()
        conn.close()
        embed = create_success_embed("Node Updated", f"ID: {node_id}\nName: {node['name']}\nLocation: {node['location']}\nCapacity: {node['total_vps']}\nTags: {', '.join(node['tags'])}")
        if not node['is_local']:
            add_field(embed, "URL", node['url'], False)
            add_field(embed, "API Key", "🔒 Sent to you in DM (never shown in public channels).", False)
        await ctx.send(embed=embed)
        if not node['is_local']:
            key_embed = create_info_embed("🔑 Node API Key", f"Node **{node['name']}** (ID: {node_id})")
            add_field(key_embed, "API Key", f"`{node['api_key']}`", False)
            add_field(key_embed, "Setup", f"Run on the node server:\n```python3 node-agent.py --api_key={node['api_key']} --port=PORT```", False)
            add_field(key_embed, "⚠️ Security", "Keep this key private.", False)
            try:
                await ctx.author.send(embed=key_embed)
            except discord.Forbidden:
                await ctx.send(embed=create_warning_embed("DM Failed", "Could not DM you the API key. Enable DMs from server members and regenerate the key."))
    
    # NEW: Add delete subcommand
    elif sub == 'delete':
        if not args:
            await ctx.send(embed=create_error_embed("Usage", f"{PREFIX}node delete <id> [force]"))
            return
        
        try:
            node_id = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Node ID must be an integer."))
            return
        
        force = False
        if len(args) > 1 and args[1].lower() == 'force':
            force = True
        elif len(args) > 1:
            await ctx.send(embed=create_error_embed("Invalid Argument", "Optional argument must be 'force'."))
            return
        
        node = get_node(node_id)
        if not node:
            await ctx.send(embed=create_error_embed("Not Found", "Node not found."))
            return
        
        # Check if this is the local node
        if node['is_local']:
            await ctx.send(embed=create_error_embed("Cannot Delete", "Cannot delete the local node."))
            return
        
        # Check if node has any VPS assigned
        vps_count = get_current_vps_count(node_id)
        if not force and vps_count > 0:
            await ctx.send(embed=create_error_embed("Cannot Delete", 
                f"Node has {vps_count} VPS assigned. Migrate or delete them first, or use 'force' to delete all VPS and the node."))
            return
        
        # Prepare warning message
        warning_msg = f"Are you sure you want to delete node **{node['name']}** (ID: {node_id})?\n\n"
        warning_msg += f"**Location:** {node['location']}\n"
        warning_msg += f"**Tags:** {', '.join(node['tags'])}\n\n"
        if force and vps_count > 0:
            warning_msg += f"**WARNING: Force mode will delete all {vps_count} VPS on this node first!**\n\n"
        warning_msg += "This action cannot be undone!"
        
        embed = create_warning_embed("⚠️ Delete Node", warning_msg)
        
        class ConfirmDelete(discord.ui.View):
            def __init__(self, node_id, node_name, force, vps_count):
                super().__init__(timeout=60)
                self.node_id = node_id
                self.node_name = node_name
                self.force = force
                self.vps_count = vps_count
            
            @discord.ui.button(label="Delete Node", style=discord.ButtonStyle.danger)
            async def confirm(self, inter: discord.Interaction, item: discord.ui.Button):
                if str(inter.user.id) != str(ctx.author.id):
                    await inter.response.send_message(
                        embed=create_error_embed("Access Denied", "Only the command author can confirm."),
                        ephemeral=True
                    )
                    return
                
                await inter.response.defer()
                
                conn = get_db()
                cur = conn.cursor()
                
                failed = []
                deleted_vps = 0
                if self.force and self.vps_count > 0:
                    # Force mode: delete the REAL containers first. A record is only
                    # removed when its container is really gone.
                    conn.close()
                    for uid in list(vps_data.keys()):
                        for v in list(vps_data.get(uid, [])):
                            if v.get('node_id') != self.node_id:
                                continue
                            cname = v['container_name']
                            ok, detail = await delete_container_safely(cname, self.node_id)
                            if ok:
                                purge_vps_records(uid, cname)
                                deleted_vps += 1
                            else:
                                failed.append(f"`{cname}`: {detail[:120]}")
                    save_vps_data()
                    if failed:
                        await inter.followup.send(embed=create_error_embed(
                            "Node Delete Aborted",
                            f"Deleted {deleted_vps} VPS, but {len(failed)} could not be deleted, so the node was **kept**:\n" + "\n".join(failed[:10])))
                        self.stop()
                        return
                    conn = get_db()
                    cur = conn.cursor()
                
                # Delete the node from database
                cur.execute('DELETE FROM nodes WHERE id = ?', (self.node_id,))
                
                conn.commit()
                conn.close()
                
                msg = f"Node **{self.node_name}** (ID: {self.node_id}) has been deleted."
                if self.force and self.vps_count > 0:
                    msg += f" All {deleted_vps} VPS on the node were also deleted (containers removed)."
                
                success_embed = create_success_embed("Node Deleted", msg)
                await inter.followup.send(embed=success_embed)
                self.stop()
            
            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
            async def cancel(self, inter: discord.Interaction, item: discord.ui.Button):
                if str(inter.user.id) != str(ctx.author.id):
                    await inter.response.send_message(
                        embed=create_error_embed("Access Denied", "Only the command author can cancel."),
                        ephemeral=True
                    )
                    return
                
                await inter.response.edit_message(
                    embed=create_info_embed("Deletion Cancelled", "Node deletion was cancelled."),
                    view=None
                )
                self.stop()
        
        await ctx.send(embed=embed, view=ConfirmDelete(node_id, node['name'], force, vps_count))
    
    elif sub == 'status':
        # New: Check node status
        if not args:
            await ctx.send(embed=create_error_embed("Usage", f"{PREFIX}node status <id>"))
            return
        
        try:
            node_id = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Node ID must be an integer."))
            return
        
        node = get_node(node_id)
        if not node:
            await ctx.send(embed=create_error_embed("Not Found", "Node not found."))
            return
        
        embed = create_info_embed(f"Node Status - {node['name']}")
        
        if node['is_local']:
            status = "🟢 Local Node"
            cpu_usage = get_host_cpu_usage()
            ram_usage = get_host_ram_usage()
            add_field(embed, "Status", status, True)
            add_field(embed, "CPU Usage", f"{cpu_usage:.1f}%", True)
            add_field(embed, "RAM Usage", f"{ram_usage:.1f}%", True)
        else:
            try:
                response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
                if response.status_code == 200:
                    status = "🟢 Online"
                    try:
                        stats_response = requests.get(f"{node['url']}/api/get_host_stats", 
                                                    params={'api_key': node['api_key']}, 
                                                    timeout=5)
                        if stats_response.status_code == 200:
                            stats = stats_response.json()
                            cpu_usage = stats.get('cpu', 0.0)
                            ram_usage = stats.get('ram', 0.0)
                            add_field(embed, "CPU Usage", f"{cpu_usage:.1f}%", True)
                            add_field(embed, "RAM Usage", f"{ram_usage:.1f}%", True)
                    except:
                        cpu_usage = "Unknown"
                        ram_usage = "Unknown"
                else:
                    status = "🔴 Offline"
            except:
                status = "🔴 Offline"
            
            add_field(embed, "Status", status, True)
        
        vps_count = get_current_vps_count(node_id)
        capacity = node['total_vps']
        usage_percentage = (vps_count / capacity * 100) if capacity > 0 else 0
        
        add_field(embed, "VPS Capacity", f"{vps_count}/{capacity} ({usage_percentage:.1f}%)", True)
        add_field(embed, "Location", node['location'], True)
        add_field(embed, "Tags", ", ".join(node['tags']), True)
        
        if not node['is_local']:
            add_field(embed, "URL", node['url'], False)
        
        await ctx.send(embed=embed)
    
    else:
        # Show help for node command
        embed = create_info_embed("Node Management", 
            f"Manage multi-node infrastructure for {BOT_NAME}")

class HelpView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.current_category = "user"
        # Command categories
        self.command_categories = {
            "user": {
                "name": "👤 User Commands",
                "commands": [
                    (f"{PREFIX}ping", "Check bot latency"),
                    (f"{PREFIX}uptime", "Show host uptime"),
                    (f"{PREFIX}about", "About us"),
                    (f"{PREFIX}myvps", "List your VPS"),
                    (f"{PREFIX}myexpiry", "See when your VPS expire"),
                    (f"{PREFIX}manage [@user]", "Manage your VPS or another user's VPS (Admin only)"),
                    (f"{PREFIX}share-user @user <vps_number>", "Share VPS access"),
                    (f"{PREFIX}share-ruser @user <vps_number>", "Revoke VPS access"),
                    (f"{PREFIX}manage-shared @owner <vps_number>", "Manage shared VPS")
                ]
            },
            "vps": {
                "name": "🖥️ VPS Management",
                "commands": [
                    (f"{PREFIX}myvps", "List your VPS"),
                    (f"{PREFIX}vpsinfo [container]", "VPS information"),
                    (f"{PREFIX}vps-stats <container>", "VPS stats"),
                    (f"{PREFIX}vps-uptime <container>", "VPS uptime"),
                    (f"{PREFIX}vps-processes <container>", "List processes"),
                    (f"{PREFIX}vps-logs <container> [lines]", "Show logs"),
                    (f"{PREFIX}restart-vps <container>", "Restart VPS"),
                    (f"{PREFIX}clone-vps <container> [new_name]", "Clone VPS"),
                    (f"{PREFIX}snapshot <container> [snap_name]", "Create snapshot"),
                    (f"{PREFIX}list-snapshots <container>", "List snapshots"),
                    (f"{PREFIX}restore-snapshot <container> <snap_name>", "Restore snapshot")
                ]
            },
            "ports": {
                "name": "🔌 Port Forwarding",
                "commands": [
                    (f"{PREFIX}ports [add <vps_num> <port> | list | remove <id>]", "Manage port forwards (TCP/UDP)"),
                    (f"{PREFIX}ports-add-user <amount> @user", "Allocate port slots to user (Admin only)"),
                    (f"{PREFIX}ports-remove-user <amount> @user", "Deallocate port slots from user (Admin only)"),
                    (f"{PREFIX}ports-revoke <id>", "Revoke specific port forward (Admin only)")
                ]
            },
            "system": {
                "name": "⚙️ System Commands",
                "commands": [
                    (f"{PREFIX}serverstats", "Server statistics"),
                    (f"{PREFIX}resource-check", "Check and suspend high-usage VPS (Admin only)"),
                    (f"{PREFIX}cpu-monitor <status|enable|disable>", "Resource monitor control (logging only)"),
                    (f"{PREFIX}thresholds", "View resource thresholds"),
                    (f"{PREFIX}set-threshold <cpu> <ram>", "Set resource thresholds (Admin only)"),
                    (f"{PREFIX}set-status <type> <name>", "Set bot status (Admin only)")
                ]
            },
            "nodes": {
                "name": "🌐 Node Management",
                "commands": [
                    (f"{PREFIX}node create", "Create a new node (Admin only)"),
                    (f"{PREFIX}node list", "List all nodes (Admin only)"),
                    (f"{PREFIX}node status <id>", "Check node status (Admin only)"),
                    (f"{PREFIX}node edit <id>", "Edit node details (Admin only)"),
                    (f"{PREFIX}node delete <id>", "Delete a node (Admin only)"),
                    (f"{PREFIX}node migrate <from> <to>", "Migrate VPS between nodes (Admin only)"),
                    (f"{PREFIX}lxc-list [node_id]", "List LXC containers on node (Admin only)")
                ],
                "admin_only": True
            },
            "bot": {
                "name": "🤖 Bot Control",
                "commands": [
                    (f"{PREFIX}ping", "Check bot latency"),
                    (f"{PREFIX}uptime", "Show host uptime"),
                    (f"{PREFIX}help", "Show this help menu"),
                    (f"{PREFIX}set-status <type> <name>", "Set bot status (Admin only)")
                ]
            },
            "admin": {
                "name": "🛡️ Admin Commands",
                "commands": [
                    (f"{PREFIX}lxc-list", "List all LXC containers"),
                    (f"{PREFIX}create <ram_gb> <cpu_cores> <disk_gb> @user [expiry_days]", "Create VPS with OS selection (+ optional expiry days)"),
                    (f"{PREFIX}delete-vps @user <vps_number> [reason]", "Delete user's VPS"),
                    (f"{PREFIX}transfer-vps <container> @new_owner [keep-shares] [keep-password]", "Transfer a VPS to another user (data is kept)"),
                    (f"{PREFIX}fix-vps <container|all>", "Fix Docker/Pterodactyl/Tailscale + SSH password login on a VPS"),
                    (f"{PREFIX}setup-docker <container>", "Install/repair Docker (Pterodactyl Wings) inside a VPS"),
                    (f"{PREFIX}setup-snap <container>", "Install/repair snap inside a VPS"),
                    (f"{PREFIX}add-resources <ram_gb> <cpu> <disk_gb> @user [vps_no]", "Add resources to VPS"),
                    (f"{PREFIX}resize-vps <ram_gb> <cpu> <disk_gb> @user [vps_no]", "Resize VPS resources"),
                    (f"{PREFIX}suspend-vps <container> [reason]", "Suspend VPS"),
                    (f"{PREFIX}unsuspend-vps <container>", "Unsuspend VPS"),
                    (f"{PREFIX}suspension-logs [container]", "View suspension logs"),
                    (f"{PREFIX}whitelist-vps <container> <add|remove>", "Whitelist VPS from auto-suspend"),
                    (f"{PREFIX}userinfo @user", "User information"),
                    (f"{PREFIX}list-all", "List all VPS"),
                    (f"{PREFIX}exec <container> <command>", "Execute command"),
                    (f"{PREFIX}stop-vps-all", "Stop all VPS"),
                    (f"{PREFIX}migrate-vps <container> <pool>", "Migrate VPS"),
                    (f"{PREFIX}vps-network <container> <action> [value]", "Network management"),
                    (f"{PREFIX}apply-permissions <container>", "Apply Docker-ready permissions"),
                    (f"{PREFIX}audit [count] [@admin]", "Activity log - which admin did what"),
                    (f"{PREFIX}health", "Host CPU, RAM, disk and network"),
                    (f"{PREFIX}node-health", "Live status of all nodes"),
                    (f"{PREFIX}alerts [on|off|channel #ch]", "Auto alerts for high CPU/RAM/node down"),
                    (f"{PREFIX}maintenance <on [reason]|off>", "Temporarily disable VPS operations for users"),
                    (f"{PREFIX}backup-status", "Last database backup info"),
                    (f"{PREFIX}diagnostics [container]", "Detect VPS problems"),
                    (f"{PREFIX}dashboard", "Overview of all VPS and nodes"),
                    (f"{PREFIX}set-expiry <container> <days|YYYY-MM-DD|none>", "Set a VPS expiry date"),
                    (f"{PREFIX}expiry-add <container> <days>", "Add days to a VPS expiry (alias: extend-vps)"),
                    (f"{PREFIX}expiry-remove <container> <days>", "Remove days from a VPS expiry"),
                    (f"{PREFIX}expiry-list [days]", "VPS expiring soon / already expired"),
                    (f"{PREFIX}expiry-default [days|off]", "Default expiry for new VPS"),
                    (f"{PREFIX}expiry-reminders [on|off]", "Expiry reminders + auto-suspend on/off"),
                ],
                "admin_only": True
            },
            "main_admin": {
                "name": "👑 Main Admin Commands",
                "commands": [
                    (f"{PREFIX}admin-add @user", "Add admin (mention)"),
                    (f"{PREFIX}admin-remove @user", "Remove admin (mention)"),
                    (f"{PREFIX}add-admin <user_id>", "Add main admin (by ID)"),
                    (f"{PREFIX}rm-admin <user_id>", "Remove main admin (by ID)"),
                    (f"{PREFIX}admin-list", "List admins"),
                    (f"{PREFIX}prefix [new|reset]", "Show / change the bot command prefix")
                ],
                "admin_only": True,
                "main_admin_only": True
            }
        }
        self.update_select()
        self.update_embed()
        self.add_item(self.select)

    def update_select(self):
        """Update the category selection dropdown based on user permissions"""
        self.select = discord.ui.Select(placeholder="Select Category", options=[])
        user_id = str(self.ctx.author.id)
        is_admin_user = user_id in main_admin_ids or user_id in admin_data.get("admins", [])
        is_main_admin_user = user_id in main_admin_ids
       
        # Add all categories that user has access to
        options = []
        # Always show basic categories
        basic_categories = ["user", "vps", "ports", "system", "bot"]
        for category in basic_categories:
            options.append(discord.SelectOption(
                label=self.command_categories[category]["name"],
                value=category,
                emoji=self.get_category_emoji(category)
            ))
       
        # Add nodes category if admin
        if is_admin_user:
            options.append(discord.SelectOption(
                label=self.command_categories["nodes"]["name"],
                value="nodes",
                emoji=self.get_category_emoji("nodes")
            ))
       
        # Add admin categories if user has permissions
        if is_admin_user:
            options.append(discord.SelectOption(
                label=self.command_categories["admin"]["name"],
                value="admin",
                emoji=self.get_category_emoji("admin")
            ))
       
        if is_main_admin_user:
            options.append(discord.SelectOption(
                label=self.command_categories["main_admin"]["name"],
                value="main_admin",
                emoji=self.get_category_emoji("main_admin")
            ))
       
        self.select.options = options
        self.select.callback = self.select_callback
   
    async def select_callback(self, interaction: discord.Interaction):
        """Handle category selection"""
        if interaction.user != self.ctx.author:
            await interaction.response.send_message("This menu is not for you!", ephemeral=True)
            return
        
        self.current_category = interaction.data['values'][0]
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)

    def get_category_emoji(self, category):
        """Get emoji for each category"""
        emojis = {
            "user": "👤",
            "vps": "🖥️",
            "ports": "🔌",
            "system": "⚙️",
            "bot": "🤖",
            "nodes": "🌐",
            "admin": "🛡️",
            "main_admin": "👑"
        }
        return emojis.get(category, "📁")
   
    def update_embed(self):
        """Update the embed based on current category and user permissions"""
        category_data = self.command_categories[self.current_category]
        # Create embed with category-specific styling
        colors = {
            "user": 0x3498db, # Blue
            "vps": 0x2ecc71, # Green
            "ports": 0xe74c3c, # Red
            "system": 0xf39c12, # Orange
            "bot": 0x9b59b6, # Purple
            "nodes": 0x1abc9c, # Teal
            "admin": 0xe67e22, # Carrot
            "main_admin": 0xf1c40f # Yellow
        }
        color = colors.get(self.current_category, 0x1a1a1a)
       
        title = f"📚 BVM BOT Command Help - {category_data['name']}"
        description = f"**{category_data['name']}**\nUse the dropdown below to switch categories."
       
        # Add helpful tips based on category
        tips = {
            "user": f"Tip: Use `{PREFIX}myvps` to see all your VPS and `{PREFIX}manage` to control them.",
            "vps": "Tip: Snapshots are useful before making major changes to your VPS.",
            "ports": "Tip: Port forwards work for both TCP and UDP protocols.",
            "system": "Tip: Set thresholds to monitor resource usage across nodes.",
            "nodes": f"Tip: Use `{PREFIX}node list` to see all available nodes and their status.",
            "admin": f"Tip: Always check `{PREFIX}userinfo @user` before modifying VPS.",
            "main_admin": "Tip: Be careful when adding/removing admin privileges."
        }
       
        if self.current_category in tips:
            description += f"\n\n💡 {tips[self.current_category]}"
       
        self.embed = create_embed(title, description, color, bot_name="BVM BOT")
       
        # Add commands to embed
        command_lines = [f"**{cmd}** - {desc}" for cmd, desc in category_data["commands"]]
        for idx, chunk in enumerate(_chunk_lines(command_lines)):
            add_field(self.embed, "Commands" if idx == 0 else "Commands (continued)", chunk, False)
       
        # Add appropriate footer based on category
        footers = {
            "user": f"BVM BOT VPS Manager • User Commands • Need help? Contact admin",
            "vps": f"BVM BOT VPS Manager • VPS Management • Snapshots • Cloning",
            "ports": f"BVM BOT VPS Manager • Port Forwarding • TCP/UDP Support",
            "system": f"BVM BOT VPS Manager • System Monitoring • Resource Management",
            "nodes": f"BVM BOT VPS Manager • Multi-Node Management • Distributed Infrastructure",
            "bot": f"BVM BOT VPS Manager • Bot Control • Status Management",
            "admin": f"BVM BOT VPS Manager • Admin Panel • Restricted Access",
            "main_admin": f"BVM BOT VPS Manager • Main Admin • Full System Control"
        }
       
        self.embed.set_footer(text=footers.get(self.current_category, f"BVM BOT VPS Manager"))


@bot.command(name='help')
async def show_help(ctx):
    """Display the interactive help menu"""
    view = HelpView(ctx)
    await ctx.send(embed=view.embed, view=view)


# Command aliases for typos and convenience
@bot.command(name='mangage')
async def manage_typo(ctx):
    await ctx.send(embed=create_info_embed("Command Correction", f"Did you mean `{PREFIX}manage`? Use the correct command."))


@bot.command(name='commands')
async def commands_alias(ctx):
    """Alias for help command"""
    await show_help(ctx)


@bot.command(name='stats')
async def stats_alias(ctx):
    if str(ctx.author.id) in main_admin_ids or str(ctx.author.id) in admin_data.get("admins", []):
        await server_stats(ctx)
    else:
        await ctx.send(embed=create_error_embed("Access Denied", "This command requires admin privileges."))


@bot.command(name='info')
async def info_alias(ctx, user: discord.Member = None):
    if str(ctx.author.id) in main_admin_ids or str(ctx.author.id) in admin_data.get("admins", []):
        if user:
            await user_info(ctx, user)
        else:
            await ctx.send(embed=create_error_embed("Usage", f"Please specify a user: `{PREFIX}info @user`"))
    else:
        await ctx.send(embed=create_error_embed("Access Denied", "This command requires admin privileges."))
# ============================================================
#  Admin commands: !audit !health !node-health !alerts !maintenance
#                  !backup-status !diagnostics !dashboard
# ============================================================
READONLY_ADMIN_COMMANDS = {
    'audit', 'health', 'node-health', 'backup-status', 'diagnostics', 'dashboard',
    'lxc-list', 'list-all', 'userinfo', 'thresholds', 'admin-list', 'suspension-logs',
    'vps-list', 'node-check', 'info', 'stats', 'cpu-monitor',
    'expiry-list', 'expiry-reminders',
    'transfer-vps',   # logged explicitly as 'vps-transfer' once the admin confirms
}
MAINTENANCE_BLOCKED_COMMANDS = {'manage', 'manage-shared', 'ports'}

def usage_bar(pct, width: int = 10) -> str:
    pct = max(0.0, min(100.0, float(pct)))
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)

def level_emoji(pct, limit) -> str:
    if pct >= limit:
        return "🔴"
    if pct >= limit - 15:
        return "🟡"
    return "🟢"

def format_bytes(n) -> str:
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024

def _read_net_bytes():
    rx = tx = 0
    try:
        with open('/proc/net/dev') as f:
            lines = f.readlines()[2:]
        for line in lines:
            name, data = line.split(':', 1)
            name = name.strip()
            if name == 'lo' or name.startswith(('veth', 'lxdbr', 'lxcbr', 'docker', 'br-', 'virbr')):
                continue
            parts = data.split()
            rx += int(parts[0])
            tx += int(parts[8])
    except Exception:
        pass
    return rx, tx

def _meminfo():
    info = {}
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                k, v = line.split(':', 1)
                info[k] = int(v.split()[0]) * 1024
        total = info['MemTotal']
        avail = info.get('MemAvailable', info.get('MemFree', 0))
        swap_total = info.get('SwapTotal', 0)
        swap_used = swap_total - info.get('SwapFree', 0)
        return total, total - avail, swap_total, swap_used
    except Exception:
        return 0, 0, 0, 0

def _tcp_latency_ms(host: str = '1.1.1.1', port: int = 443, timeout: float = 3.0):
    try:
        t = time.time()
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return (time.time() - t) * 1000
    except Exception:
        return None

def _parse_gb(value) -> int:
    try:
        return int(re.sub(r'[^0-9]', '', str(value)) or 0)
    except Exception:
        return 0

def _chunk_lines(lines: List[str], limit: int = 1000) -> List[str]:
    chunks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    return chunks

# ---------------------------------------------------------------- global hooks
@bot.check
async def maintenance_gate(ctx):
    """While maintenance mode is ON, normal users cannot run VPS operations."""
    cmd = ctx.command
    if cmd is not None and cmd.qualified_name in MAINTENANCE_BLOCKED_COMMANDS \
            and maintenance_on() and not is_admin_id(ctx.author.id):
        raise commands.CheckFailure(
            f"🛠️ **Maintenance mode is ON** - VPS operations are temporarily disabled.\n"
            f"**Reason:** {maintenance_reason()}\nPlease try again later.")
    return True

@bot.event
async def on_command_completion(ctx):
    """Automatically records every state-changing admin command in the audit log."""
    try:
        cmd = ctx.command
        if cmd is None or cmd.qualified_name in READONLY_ADMIN_COMMANDS:
            return
        if not any(getattr(chk, '_bvm_admin', False) for chk in cmd.checks):
            return
        target = ", ".join(m.name for m in ctx.message.mentions)
        log_audit(ctx.author.id, str(ctx.author), cmd.qualified_name, target, ctx.message.content)
    except Exception as e:
        logger.error(f"Audit hook failed: {e}")

# ---------------------------------------------------------------- !audit
@bot.command(name='audit')
@is_admin()
async def audit_cmd(ctx, *args):
    """!audit [count] [@admin]  - who did what (latest admin actions)"""
    limit = 15
    for a in args:
        if a.isdigit():
            limit = max(1, min(int(a), 25))
    admin_filter = ctx.message.mentions[0] if ctx.message.mentions else None
    conn = get_db()
    cur = conn.cursor()
    if admin_filter:
        cur.execute('SELECT * FROM audit_logs WHERE admin_id = ? ORDER BY id DESC LIMIT ?', (str(admin_filter.id), limit))
    else:
        cur.execute('SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?', (limit,))
    rows = cur.fetchall()
    cur.execute('SELECT COUNT(*) FROM audit_logs')
    total = cur.fetchone()[0]
    conn.close()
    title = "📜 Audit Log" + (f" - {admin_filter.display_name}" if admin_filter else "")
    if not rows:
        await ctx.send(embed=create_info_embed(title, "No admin actions have been recorded yet."))
        return
    lines = []
    for r in rows:
        line = f"<t:{r['ts']}:R> **{r['admin_name']}** ran `{r['action']}`"
        if r['target']:
            line += f" → {r['target']}"
        if r['details']:
            detail_text = str(r['details'])[:110].replace('`', "'")
            line += f"\n└ `{detail_text}`"
        lines.append(line)
    text = "\n".join(lines)
    embed = create_info_embed(title, text[:4000])
    embed.set_footer(text=f"Showing {len(rows)} of {total} recorded actions • {BOT_NAME} Audit")
    await ctx.send(embed=embed)

# ---------------------------------------------------------------- !health
@bot.command(name='health')
@is_admin()
async def health_cmd(ctx):
    """!health - host CPU, RAM, disk and network"""
    msg = await ctx.send(embed=create_info_embed("🩺 System Health", "Collecting host metrics (about 2 seconds)..."))
    rx1, tx1 = _read_net_bytes()
    t1 = time.time()
    cpu = await asyncio.to_thread(get_host_cpu_usage)
    rx2, tx2 = _read_net_bytes()
    dt = max(time.time() - t1, 0.1)
    rx_rate, tx_rate = (rx2 - rx1) / dt, (tx2 - tx1) / dt
    mem_total, mem_used, swap_total, swap_used = _meminfo()
    ram_pct = (mem_used / mem_total * 100) if mem_total else 0.0
    try:
        du = shutil.disk_usage('/')
        disk_pct = du.used / du.total * 100
        disk_text = f"{format_bytes(du.used)} / {format_bytes(du.total)}"
    except Exception:
        disk_pct, disk_text = 0.0, "Unknown"
    latency = await asyncio.to_thread(_tcp_latency_ms)
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0.0
    cores = os.cpu_count() or 1
    worst = max(cpu / max(CPU_THRESHOLD, 1), ram_pct / max(RAM_THRESHOLD, 1), disk_pct / 90)
    if worst >= 1:
        status, color = "🔴 Critical", 0xff3366
    elif worst >= 0.85:
        status, color = "🟡 Warning", 0xffaa00
    else:
        status, color = "🟢 Healthy", 0x00ff88
    embed = create_embed("🩺 System Health", f"**Overall:** {status}", color)
    add_field(embed, "CPU", f"{level_emoji(cpu, CPU_THRESHOLD)} `{usage_bar(cpu)}` **{cpu:.1f}%**\nCores: {cores}\nLoad: {load1:.2f} / {load5:.2f} / {load15:.2f}", True)
    add_field(embed, "RAM", f"{level_emoji(ram_pct, RAM_THRESHOLD)} `{usage_bar(ram_pct)}` **{ram_pct:.1f}%**\n{format_bytes(mem_used)} / {format_bytes(mem_total)}", True)
    swap_line = f"\nSwap: {format_bytes(swap_used)} / {format_bytes(swap_total)}" if swap_total else "\nSwap: none"
    add_field(embed, "Disk (/)", f"{level_emoji(disk_pct, 90)} `{usage_bar(disk_pct)}` **{disk_pct:.1f}%**\n{disk_text}{swap_line}", True)
    net_text = f"⬇️ {format_bytes(rx_rate)}/s  ⬆️ {format_bytes(tx_rate)}/s\nTotal: ⬇️ {format_bytes(rx2)}  ⬆️ {format_bytes(tx2)}\n"
    net_text += f"Internet: 🟢 {latency:.0f} ms" if latency is not None else "Internet: 🔴 unreachable"
    add_field(embed, "Network", net_text, False)
    up = get_host_uptime_seconds()
    add_field(embed, "Uptime", f"Host: {format_duration(up) if up is not None else 'Unknown'}\nBot: {format_duration(get_bot_uptime_seconds())}", True)
    add_field(embed, "Discord Latency", f"{round(bot.latency * 1000)} ms", True)
    await msg.edit(embed=embed)

# ---------------------------------------------------------------- !node-health
async def collect_node_health(node: Dict) -> Dict:
    info = {"node": node, "online": False, "latency": None, "cpu": None, "ram": None, "disk": "Unknown"}
    if node['is_local']:
        info["online"] = True
        info["latency"] = 0
        info["cpu"] = await asyncio.to_thread(get_host_cpu_usage)
        info["ram"] = await asyncio.to_thread(get_host_ram_usage)
        info["disk"] = await asyncio.to_thread(get_host_disk_usage)
        return info
    t = time.time()
    ok = await asyncio.to_thread(node_ping_ok, node, 5)
    if not ok:
        return info
    info["online"] = True
    info["latency"] = (time.time() - t) * 1000
    try:
        r = await asyncio.to_thread(lambda: requests.get(f"{node['url']}/api/get_host_stats", params={'api_key': node['api_key']}, timeout=10))
        r.raise_for_status()
        stats = r.json()
        info["cpu"] = float(stats.get('cpu', 0) or 0)
        info["ram"] = float(stats.get('ram', 0) or 0)
        info["disk"] = stats.get('disk', 'Unknown')
    except Exception as e:
        logger.error(f"node-health: stats failed for {node['name']}: {e}")
    return info

@bot.command(name='node-health')
@is_admin()
async def node_health_cmd(ctx):
    """!node-health - live status of all nodes"""
    msg = await ctx.send(embed=create_info_embed("🛰️ Node Health", "Checking all nodes..."))
    nodes = get_nodes()
    results = await asyncio.gather(*[collect_node_health(n) for n in nodes])
    online = sum(1 for r in results if r["online"])
    color = 0x00ff88 if online == len(results) else (0xffaa00 if online else 0xff3366)
    embed = create_embed("🛰️ Node Health", f"**{online}/{len(results)}** nodes online", color)
    for r in results[:24]:
        n = r["node"]
        used = get_current_vps_count(n['id'])
        running = sum(1 for lst in vps_data.values() for v in lst if v.get('node_id') == n['id'] and v.get('status') == 'running')
        if not r["online"]:
            value = f"🔴 **OFFLINE**\nURL: `{n.get('url')}`\nVPS: {used}/{n['total_vps']}"
        else:
            kind = "Local" if n['is_local'] else f"{r['latency']:.0f} ms"
            cpu_t = f"{r['cpu']:.1f}%" if r['cpu'] is not None else "n/a"
            ram_t = f"{r['ram']:.1f}%" if r['ram'] is not None else "n/a"
            value = (f"🟢 **ONLINE** • {kind}\nCPU {cpu_t} • RAM {ram_t}\nDisk {r['disk']}\n"
                     f"VPS: {used}/{n['total_vps']} ({running} running)")
        add_field(embed, f"{n['name']} (ID {n['id']})", value, True)
    await msg.edit(embed=embed)

# ---------------------------------------------------------------- !alerts
@bot.command(name='alerts')
@is_admin()
async def alerts_cmd(ctx, *args):
    """!alerts [status|on|off|channel #channel|channel off]"""
    action = args[0].lower() if args else "status"
    if action == "on":
        set_setting('alerts_enabled', '1')
        await ctx.send(embed=create_success_embed("Alerts Enabled", "Admins will be alerted on high CPU/RAM/disk and node outages."))
    elif action == "off":
        set_setting('alerts_enabled', '0')
        await ctx.send(embed=create_warning_embed("Alerts Disabled", "Automatic alerts are now turned off."))
    elif action == "channel":
        if len(args) > 1 and args[1].lower() == "off":
            set_setting('alert_channel', '')
            await ctx.send(embed=create_success_embed("Alert Channel Cleared", "Alerts will be sent to the main admins by DM."))
        elif ctx.message.channel_mentions:
            ch = ctx.message.channel_mentions[0]
            set_setting('alert_channel', str(ch.id))
            await ctx.send(embed=create_success_embed("Alert Channel Set", f"Alerts will be posted in {ch.mention}.\n⚠️ Use a **private admin-only** channel."))
        else:
            await ctx.send(embed=create_error_embed("Usage", f"`{PREFIX}alerts channel #channel` or `{PREFIX}alerts channel off`"))
    else:
        enabled = str(get_setting('alerts_enabled', '1')) == '1'
        ch_id = get_setting('alert_channel', '')
        embed = create_info_embed("🔔 Auto Alerts", "Alerts are checked every minute by the resource monitor.")
        add_field(embed, "Status", "🟢 Enabled" if enabled else "🔴 Disabled", True)
        add_field(embed, "Send To", f"<#{ch_id}>" if ch_id else "DM to main admins", True)
        add_field(embed, "Triggers", f"CPU > {CPU_THRESHOLD}%, RAM > {RAM_THRESHOLD}%, local disk > 90% (2 checks in a row), node offline.\nSame alert repeats at most every 30 min.", False)
        add_field(embed, "Change", f"`{PREFIX}alerts on|off`\n`{PREFIX}alerts channel #channel|off`\n`{PREFIX}set-threshold <cpu> <ram>`", False)
        await ctx.send(embed=embed)

# ---------------------------------------------------------------- !maintenance
async def refresh_presence():
    try:
        if maintenance_on():
            await bot.change_presence(status=discord.Status.dnd, activity=discord.Activity(type=discord.ActivityType.playing, name="🛠️ Maintenance mode"))
        else:
            await bot.change_presence(status=discord.Status.online, activity=discord.Activity(type=discord.ActivityType.watching, name="Made By MineFlareX BVM VPS Deploy Machine"))
    except Exception as e:
        logger.error(f"Presence update failed: {e}")

@bot.command(name='maintenance')
@is_admin()
async def maintenance_cmd(ctx, action: str = "status", *, reason: str = "Scheduled maintenance"):
    """!maintenance <on [reason]|off|status>"""
    action = action.lower()
    if action == "on":
        set_setting('maintenance_mode', '1')
        set_setting('maintenance_reason', reason[:200])
        await refresh_presence()
        embed = create_warning_embed("🛠️ Maintenance Mode ON", f"**Reason:** {reason[:200]}")
        add_field(embed, "Disabled for users", f"`{PREFIX}manage` (all VPS buttons), `{PREFIX}manage-shared`, `{PREFIX}ports`", False)
        add_field(embed, "Still working", f"Admin commands, running VPS, `{PREFIX}myvps`, `{PREFIX}ping`, `{PREFIX}about`, `{PREFIX}help`, and the Stats / Refresh / Uptime buttons.", False)
        await ctx.send(embed=embed)
    elif action == "off":
        set_setting('maintenance_mode', '0')
        await refresh_presence()
        await ctx.send(embed=create_success_embed("✅ Maintenance Mode OFF", "VPS operations are enabled again for everyone."))
    else:
        if maintenance_on():
            embed = create_warning_embed("🛠️ Maintenance Mode: ON", f"**Reason:** {maintenance_reason()}")
        else:
            embed = create_success_embed("✅ Maintenance Mode: OFF", "Everything is running normally.")
        add_field(embed, "Usage", f"`{PREFIX}maintenance on [reason]`\n`{PREFIX}maintenance off`", False)
        await ctx.send(embed=embed)

# ---------------------------------------------------------------- !backup-status
@bot.command(name='backup-status')
@is_admin()
async def backup_status_cmd(ctx):
    """!backup-status - last database backup"""
    files = sorted(glob.glob('vps_backup_*.db'), key=os.path.getmtime, reverse=True)
    embed_color = 0x00ff88
    if not files:
        embed = create_warning_embed("💾 Backup Status", "No database backups found yet.")
        add_field(embed, "Info", f"Automatic backups run every 1 hour while the bot is running (the first one 1 hour after the bot starts).\nCreate one now with `{PREFIX}backup-db`.", False)
        await ctx.send(embed=embed)
        return
    latest = files[0]
    age = time.time() - os.path.getmtime(latest)
    total_size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
    if age > 2 * 3600:
        status, embed_color = f"🔴 Overdue - last backup is {format_duration(age)} old", 0xff3366
    elif age > 3600 + 600:
        status, embed_color = "🟡 Slightly late", 0xffaa00
    else:
        status = "🟢 Up to date"
    embed = create_embed("💾 Backup Status", f"**{status}**", embed_color)
    add_field(embed, "Last Backup", f"`{latest}`\n<t:{int(os.path.getmtime(latest))}:F> (<t:{int(os.path.getmtime(latest))}:R>)\nSize: {format_bytes(os.path.getsize(latest))}", False)
    add_field(embed, "Stored Backups", f"{len(files)} files • {format_bytes(total_size)} total", True)
    try:
        add_field(embed, "Live Database", f"vps.db • {format_bytes(os.path.getsize('vps.db'))}", True)
    except Exception:
        pass
    upd = glob.glob(os.path.join(os.getenv('BVM_INSTALL_DIR', '/root'), 'update-backups', '*'))
    if upd:
        add_field(embed, "Update Backups", f"{len(upd)} snapshots from `install.sh` option 3", True)
    add_field(embed, "Schedule", f"Automatic: every 1 hour\nManual: `{PREFIX}backup-db`", False)
    if len(files) > 48:
        add_field(embed, "💡 Tip", "Many old backups are stored. Delete old `vps_backup_*.db` files to save disk space.", False)
    await ctx.send(embed=embed)

# ---------------------------------------------------------------- !diagnostics
def _find_vps_by_container(container: str):
    for uid, lst in vps_data.items():
        for v in lst:
            if v.get('container_name') == container:
                return uid, v
    return None, None

async def _lxc_status(container: str, node_id: int) -> Optional[str]:
    """Returns the real container status, 'missing' if it does not exist, None if unknown."""
    try:
        out = await execute_lxc(container, f"info {container}", timeout=20, node_id=node_id)
        for line in str(out).splitlines():
            if line.startswith("Status:"):
                return line.split(":", 1)[1].strip().lower()
        return None
    except Exception as e:
        err = str(e).lower()
        if "not found" in err or "does not exist" in err:
            return "missing"
        return None

async def run_vps_diagnostics(uid: str, vps: Dict, deep: bool = True) -> List[tuple]:
    """Returns a list of (level, problem, suggested fix). level: ok / warn / crit."""
    results = []
    container = vps['container_name']
    node = get_node(vps.get('node_id', 1))
    if not node:
        return [("crit", f"Node ID {vps.get('node_id')} no longer exists", "Move or delete this VPS record.")]
    if not node['is_local'] and not await asyncio.to_thread(node_ping_ok, node, 5):
        return [("crit", f"Node **{node['name']}** is offline", f"Check the node server and `node-agent.py`. See `{PREFIX}node-health`.")]
    real = await _lxc_status(container, node['id'])
    if real == "missing":
        return [("crit", "Container does not exist on the node (database record is orphaned)", f"Delete the record with `{PREFIX}delete-vps` or restore a snapshot.")]
    if real is None:
        return [("warn", "Could not read container status", "The node may be busy or LXD may be unresponsive.")]
    db_status = str(vps.get('status', 'unknown')).lower()
    if real != db_status:
        results.append(("warn", f"Status mismatch: database says `{db_status}` but container is `{real}`",
                        f"Press 🔃 Refresh in `{PREFIX}manage`, or use `{PREFIX}restart-vps {container}`."))
    if vps.get('suspended') and real == 'running':
        results.append(("warn", "VPS is marked suspended but is still running", f"Run `{PREFIX}suspend-vps {container}` again or `{PREFIX}unsuspend-vps {container}`."))
    # port forwards present in LXC config?
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT host_port FROM port_forwards WHERE vps_container = ?', (container,))
        ports = [r[0] for r in cur.fetchall()]
        conn.close()
        if ports:
            devices = str(await execute_lxc(container, f"config device show {container}", timeout=20, node_id=node['id']))
            missing = [p for p in ports if f"tcp_proxy_{p}" not in devices]
            if missing:
                results.append(("warn", f"{len(missing)} port forward(s) missing from LXC config (host ports: {', '.join(map(str, missing[:5]))})", f"Run `{PREFIX}repair-ports {container}`."))
    except Exception as e:
        logger.error(f"diagnostics: port check failed for {container}: {e}")
    if real != 'running':
        if not results:
            results.append(("ok", f"Container is `{real}` - nothing else to check", ""))
        return results
    if deep:
        try:
            stats = await get_container_stats(container, node['id'])
            cpu = float(stats.get('cpu', 0) or 0)
            ram_pct = float(stats.get('ram', {}).get('pct', 0) or 0)
            if cpu >= 90:
                results.append(("warn", f"High CPU usage: {cpu:.1f}%", f"Check running processes with `{PREFIX}vps-processes`."))
            if ram_pct >= 90:
                results.append(("warn", f"High RAM usage: {ram_pct:.1f}%", f"Give more RAM with `{PREFIX}add-resources`."))
            m = re.search(r'\((\d+)%\)', str(stats.get('disk', '')))
            if m and int(m.group(1)) >= 90:
                results.append(("warn", f"Disk almost full: {stats.get('disk')}", f"Give more disk with `{PREFIX}add-resources`."))
        except Exception as e:
            results.append(("warn", f"Could not read resource usage: {str(e)[:80]}", ""))
        try:
            await execute_lxc(container, f"exec {container} -- ping -c 1 -W 2 1.1.1.1", timeout=15, node_id=node['id'])
            try:
                await execute_lxc(container, f"exec {container} -- getent hosts google.com", timeout=15, node_id=node['id'])
            except Exception:
                results.append(("warn", "DNS is not working inside the VPS (IP internet works)", "Check `/etc/resolv.conf` inside the VPS."))
        except Exception:
            results.append(("crit", "No internet connection inside the VPS", f"Check the network: `{PREFIX}vps-network {container} ...` or restart the VPS."))
    if not results:
        results.append(("ok", "No problems detected", ""))
    return results

@bot.command(name='diagnostics')
@is_admin()
async def diagnostics_cmd(ctx, container_name: str = None):
    """!diagnostics [container] - detect VPS problems"""
    icons = {"ok": "🟢", "warn": "🟡", "crit": "🔴"}
    if container_name:
        uid, vps = _find_vps_by_container(container_name)
        if not vps:
            await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS with container name `{container_name}`. Use `{PREFIX}list-all`."))
            return
        msg = await ctx.send(embed=create_info_embed("🔍 Diagnostics", f"Running full checks on `{container_name}`..."))
        results = await run_vps_diagnostics(uid, vps, deep=True)
        worst = "crit" if any(r[0] == "crit" for r in results) else "warn" if any(r[0] == "warn" for r in results) else "ok"
        color = {"ok": 0x00ff88, "warn": 0xffaa00, "crit": 0xff3366}[worst]
        embed = create_embed(f"🔍 Diagnostics - {container_name}", f"**Result:** {icons[worst]} {'No problems' if worst == 'ok' else 'Problems found'}", color)
        for level, problem, fix in results:
            add_field(embed, f"{icons[level]} {'OK' if level == 'ok' else 'Problem'}", problem + (f"\n💡 {fix}" if fix else ""), False)
        await msg.edit(embed=embed)
        return
    total = sum(len(v) for v in vps_data.values())
    msg = await ctx.send(embed=create_info_embed("🔍 Diagnostics", f"Scanning {total} VPS (quick scan)..."))
    sem = asyncio.Semaphore(4)
    problems = []

    async def scan(uid, v):
        async with sem:
            try:
                res = await run_vps_diagnostics(uid, v, deep=False)
            except Exception as e:
                res = [("warn", f"Scan error: {str(e)[:80]}", "")]
        bad = [r for r in res if r[0] != "ok"]
        if bad:
            problems.append((v['container_name'], bad))

    await asyncio.gather(*[scan(uid, v) for uid, lst in vps_data.items() for v in lst])
    # containers that exist on a node but are not tracked by the bot
    tracked = {v['container_name'] for lst in vps_data.values() for v in lst}
    untracked = []
    for node in get_nodes():
        try:
            if not node['is_local'] and not await asyncio.to_thread(node_ping_ok, node, 5):
                continue
            out = await execute_lxc("", "list --format csv -c n", timeout=30, node_id=node['id'])
            if out is True:
                out = ""
            for name in str(out).splitlines():
                name = name.strip()
                if name and name not in tracked:
                    untracked.append(f"{name} ({node['name']})")
        except Exception:
            pass
    healthy = total - len(problems)
    color = 0x00ff88 if not problems and not untracked else 0xffaa00
    embed = create_embed("🔍 Diagnostics Summary", f"Scanned **{total}** VPS: 🟢 **{healthy}** healthy • 🔴/🟡 **{len(problems)}** with problems", color)
    if problems:
        lines = []
        for name, bad in problems[:15]:
            lines.append(f"{icons[bad[0][0]]} `{name}` - {bad[0][1][:90]}")
        for chunk in _chunk_lines(lines)[:3]:
            add_field(embed, "Problem VPS", chunk, False)
        if len(problems) > 15:
            add_field(embed, "More", f"...and {len(problems) - 15} more.", False)
    if untracked:
        add_field(embed, "Untracked containers (exist on node, not in bot database)", ", ".join(untracked[:15])[:1000], False)
    add_field(embed, "Next step", f"Run `{PREFIX}diagnostics <container>` for a full check (CPU/RAM/disk/internet/DNS).", False)
    await msg.edit(embed=embed)

# ---------------------------------------------------------------- !dashboard
@bot.command(name='dashboard')
@is_admin()
async def dashboard_cmd(ctx):
    """!dashboard - overview of all VPS"""
    msg = await ctx.send(embed=create_info_embed("📊 Admin Dashboard", "Collecting data from all nodes..."))
    nodes = get_nodes()
    # real container statuses per node (falls back to database status)
    real: Dict[str, str] = {}
    online_nodes = 0
    for node in nodes:
        try:
            if not node['is_local'] and not await asyncio.to_thread(node_ping_ok, node, 5):
                continue
            online_nodes += 1
            out = await execute_lxc("", "list --format csv -c ns", timeout=30, node_id=node['id'])
            if out is True:
                out = ""
            for line in str(out).splitlines():
                if ',' in line:
                    n, s = line.split(',', 1)
                    real[n.strip()] = s.strip().lower()
        except Exception as e:
            logger.error(f"dashboard: could not list node {node['name']}: {e}")
    all_vps = [v for lst in vps_data.values() for v in lst]
    running = stopped = unknown = 0
    for v in all_vps:
        s = real.get(v['container_name'], str(v.get('status', 'unknown')).lower())
        if s == 'running':
            running += 1
        elif s == 'stopped':
            stopped += 1
        else:
            unknown += 1
    suspended = sum(1 for v in all_vps if v.get('suspended'))
    whitelisted = sum(1 for v in all_vps if v.get('whitelisted'))
    ram = sum(_parse_gb(v.get('ram')) for v in all_vps)
    cpu = sum(_parse_gb(v.get('cpu')) for v in all_vps)
    disk = sum(_parse_gb(v.get('storage')) for v in all_vps)
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM port_forwards')
    forwards = cur.fetchone()[0]
    cur.execute('SELECT COALESCE(SUM(allocated_ports),0) FROM port_allocations')
    slots = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM audit_logs WHERE ts > ?', (int(time.time()) - 86400,))
    actions_24h = cur.fetchone()[0]
    conn.close()
    files = sorted(glob.glob('vps_backup_*.db'), key=os.path.getmtime, reverse=True)
    backup_text = f"{format_duration(time.time() - os.path.getmtime(files[0]))} ago" if files else "none yet"
    all_online = online_nodes == len(nodes)
    embed = create_embed("📊 Admin Dashboard", f"Overview of **{BOT_NAME}** • generated <t:{int(time.time())}:R>", 0x00ff88 if all_online and not unknown else 0xffaa00)
    add_field(embed, "🖥️ VPS", f"Total: **{len(all_vps)}** • Users: **{len(vps_data)}**\n🟢 Running: {running}\n🔴 Stopped: {stopped}\n❔ Unknown/other: {unknown}\n⏸️ Suspended: {suspended} • ✅ Whitelisted: {whitelisted}", True)
    add_field(embed, "📦 Allocated", f"RAM: {ram} GB\nCPU: {cpu} cores\nDisk: {disk} GB", True)
    add_field(embed, "🔌 Ports", f"Active forwards: {forwards}\nAllocated slots: {slots}", True)
    node_lines = []
    for n in nodes:
        used = get_current_vps_count(n['id'])
        node_lines.append(f"{n['name']}: {used}/{n['total_vps']} VPS")
    add_field(embed, f"🛰️ Nodes ({online_nodes}/{len(nodes)} online)", "\n".join(node_lines)[:1000] or "No nodes", False)
    maint = f"🛠️ ON ({maintenance_reason()})" if maintenance_on() else "✅ OFF"
    alerts = "🟢 On" if str(get_setting('alerts_enabled', '1')) == '1' else "🔴 Off"
    add_field(embed, "⚙️ System", f"Maintenance: {maint}\nAlerts: {alerts}\nLast DB backup: {backup_text}\nAdmin actions (24h): {actions_24h}", True)
    add_field(embed, "🤖 Bot", f"Uptime: {format_duration(get_bot_uptime_seconds())}\nLatency: {round(bot.latency * 1000)} ms\nVersion: {BOT_VERSION}", True)
    exp_n, soon_n = expiry_counts()
    add_field(embed, "⏳ Expiry", f"🔴 Expired: {exp_n}\n🟡 Expiring in 7 days: {soon_n}", True)
    add_field(embed, "More", f"`{PREFIX}health` `{PREFIX}node-health` `{PREFIX}diagnostics` `{PREFIX}audit` `{PREFIX}backup-status`", False)
    await msg.edit(embed=embed)

# ============================================================
#  VPS Expiry System + Auto-Suspend
#  - every VPS can have an expiry date (expires_at); !create can set it
#  - automatic reminders to the owner (DM) and the admins before expiry
#  - when a VPS expires it is SUSPENDED automatically (renewing it un-suspends it)
#  - usage guard: a VPS above the CPU/RAM threshold for several minutes is
#    SUSPENDED automatically (mining / abuse protection)
# ============================================================
# (seconds before expiry, label) - a reminder is sent once per threshold
EXPIRY_THRESHOLDS = [(7 * 86400, "7 days"), (3 * 86400, "3 days"), (86400, "1 day"), (3 * 3600, "3 hours")]
EXPIRY_CHECK_INTERVAL = 600     # seconds between expiry checks
EXPIRED_REASON_PREFIX = "VPS expired"
USAGE_GUARD_INTERVAL = 60       # seconds between usage checks
MAX_AUTO_SUSPEND_PER_CYCLE = 3  # more offenders than this in ONE cycle = probably a measuring problem
_expiry_task = None
_guard_task = None
_usage_since: Dict[str, float] = {}
_guard_alert_ts = 0.0

def parse_expires(val) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val))
    except Exception:
        return None

def expiry_from_days(days) -> Optional[str]:
    """days=None -> the !expiry-default value; 0 -> no expiry; N -> now + N days."""
    if days is None:
        try:
            days = int(get_setting('default_expiry_days', '0'))
        except Exception:
            days = 0
    if days <= 0:
        return None
    return (datetime.now() + timedelta(days=days)).isoformat()

def default_expiry_iso() -> Optional[str]:
    return expiry_from_days(None)

def expiry_text(val) -> str:
    exp = parse_expires(val)
    if not exp:
        return "♾️ No expiry"
    ts = int(exp.timestamp())
    if exp <= datetime.now():
        return f"🔴 **Expired** <t:{ts}:R> (<t:{ts}:f>)"
    return f"<t:{ts}:f> (<t:{ts}:R>)"

def find_vps_record(container_name: str):
    for uid, lst in vps_data.items():
        for vps in lst:
            if vps['container_name'] == container_name:
                return uid, vps
    return None, None

def parse_expiry_input(text: str):
    """Returns (ok, value). value is a datetime, or None for 'no expiry'."""
    t = text.strip().lower()
    if t in ('none', 'never', 'off', 'remove', 'clear'):
        return True, None
    if t.isdigit():
        days = int(t)
        if 1 <= days <= 3650:
            return True, datetime.now() + timedelta(days=days)
        return False, None
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            d = datetime.strptime(text.strip(), fmt)
            if fmt == '%Y-%m-%d':
                d = d.replace(hour=23, minute=59)
            return True, d
        except ValueError:
            continue
    return False, None

def premark_thresholds(vps):
    """After the expiry was changed: do not send reminders for thresholds that are already behind us."""
    exp = parse_expires(vps.get('expires_at'))
    if not exp:
        vps['expiry_notified'] = []
        return
    remaining = (exp - datetime.now()).total_seconds()
    vps['expiry_notified'] = [str(t) for t, _ in EXPIRY_THRESHOLDS if remaining <= t]

async def _dm_user(user_id, embed) -> bool:
    try:
        user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
        await user.send(embed=embed)
        return True
    except Exception as e:
        logger.warning(f"Could not DM {user_id}: {e}")
        return False

async def suspend_vps_record(vps, reason: str, by: str) -> bool:
    """Stops the container (if running), marks the VPS suspended and stores it in the suspension history."""
    container = vps['container_name']
    node_id = vps.get('node_id') or find_node_id_for_container(container)
    try:
        if vps.get('status') == 'running':
            await execute_lxc(container, f"stop {container}", node_id=node_id)
    except Exception as e:
        logger.error(f"Auto-suspend: could not stop {container}: {e}")
        return False
    vps['status'] = 'stopped'
    vps['suspended'] = True
    vps.setdefault('suspension_history', []).append({'time': datetime.now().isoformat(), 'reason': reason, 'by': by})
    save_vps_data()
    return True

async def unsuspend_if_expiry_suspended(vps) -> bool:
    """Renewing a VPS that was suspended BECAUSE it expired starts it again. Manual suspends are never touched."""
    hist = vps.get('suspension_history') or []
    if not vps.get('suspended') or not hist or not str(hist[-1].get('reason', '')).startswith(EXPIRED_REASON_PREFIX):
        return False
    container = vps['container_name']
    node_id = vps.get('node_id') or find_node_id_for_container(container)
    try:
        vps['suspended'] = False
        vps['status'] = 'running'
        await safe_start_container(container, node_id)
        await apply_internal_permissions(container, node_id)
        await recreate_port_forwards(container)
        save_vps_data()
        return True
    except Exception as e:
        logger.error(f"Could not restart {container} after renewal: {e}")
        vps['suspended'] = True
        vps['status'] = 'stopped'
        return False

async def expiry_check_once():
    """Reminds owner + admins before expiry and SUSPENDS the VPS when it expires."""
    if str(get_setting('expiry_reminders', '1')) != '1':
        return
    now = datetime.now()
    due = []
    for uid, lst in list(vps_data.items()):
        for vps in list(lst):
            exp = parse_expires(vps.get('expires_at'))
            if not exp:
                continue
            notified = vps.setdefault('expiry_notified', [])
            remaining = (exp - now).total_seconds()
            if remaining <= 0:
                if 'expired' in notified:
                    continue
                notified.extend(str(t) for t, _ in EXPIRY_THRESHOLDS if str(t) not in notified)
                notified.append('expired')
                due.append(dict(uid=uid, vps=vps, exp=exp, kind='expired', label=''))
            else:
                crossed = [(t, lbl) for t, lbl in EXPIRY_THRESHOLDS if remaining <= t]
                if not crossed or all(str(t) in notified for t, _ in crossed):
                    continue
                # mark every crossed threshold so a late bot never sends several reminders at once
                notified.extend(str(t) for t, _ in crossed if str(t) not in notified)
                due.append(dict(uid=uid, vps=vps, exp=exp, kind='reminder', label=crossed[-1][1]))
    if not due:
        return
    save_vps_data()   # mark as notified first, so a crash can never cause repeat spam
    admin_lines = []
    for d in due:
        uid, vps, exp = d['uid'], d['vps'], d['exp']
        container, config = vps['container_name'], vps.get('config', '')
        ts = int(exp.timestamp())
        if d['kind'] == 'expired':
            ok = True
            if not vps.get('suspended'):
                ok = await suspend_vps_record(vps, f"{EXPIRED_REASON_PREFIX} on {exp.strftime('%Y-%m-%d %H:%M')}", "Auto Expiry")
            embed = create_error_embed("⌛ VPS Expired & Suspended",
                f"Your VPS `{container}` ({config}) **expired** <t:{ts}:R> (<t:{ts}:f>) and has been **suspended**.\n"
                f"Contact the administrator to extend your expiry and get it back.")
            suffix = "" if ok else " • ⚠️ auto-suspend FAILED - suspend it manually"
            icon, state = "⌛", "expired + suspended"
        else:
            embed = create_warning_embed(f"⏳ VPS expires in {d['label']}",
                f"Your VPS `{container}` ({config}) will expire in **{d['label']}** — <t:{ts}:f> (<t:{ts}:R>).\n"
                f"Contact the administrator to extend your expiry, otherwise it will be suspended.")
            suffix, icon, state = "", "⏳", f"expires in {d['label']}"
        delivered = await _dm_user(uid, embed)
        admin_lines.append(f"{icon} `{container}` • <@{uid}> • {state} • <t:{ts}:R>" + suffix + ("" if delivered else " • ⚠️ DM failed"))
    expired_n = sum(1 for d in due if d['kind'] == 'expired')
    title = "⌛ VPS Expired - Suspended" if expired_n else "⏳ VPS Expiry Reminder"
    text = "\n".join(admin_lines)[:3500]
    text += f"\n\nRenew with `{PREFIX}expiry-add <container> <days>` • see all: `{PREFIX}expiry-list`"
    await _deliver_admin_alert(title, text)

async def expiry_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await expiry_check_once()
        except Exception as e:
            logger.error(f"Expiry check failed: {e}")
        await asyncio.sleep(EXPIRY_CHECK_INTERVAL)

def expiry_counts():
    now = datetime.now()
    expired = soon = 0
    for lst in vps_data.values():
        for vps in lst:
            exp = parse_expires(vps.get('expires_at'))
            if not exp:
                continue
            if exp <= now:
                expired += 1
            elif (exp - now).total_seconds() <= 7 * 86400:
                soon += 1
    return expired, soon

# ---------------------------------------------------------------- usage guard (mining / abuse)
async def _sample_stats(container: str, node_id):
    """Runs the (partly blocking) stats call in a worker thread so the bot never freezes."""
    return await asyncio.to_thread(lambda: asyncio.run(get_container_stats(container, node_id)))

async def usage_guard_once():
    """Suspends running VPS that stay above the CPU/RAM threshold for N minutes in a row."""
    global _guard_alert_ts
    if str(get_setting('auto_suspend', '1')) != '1':
        _usage_since.clear()
        return
    try:
        minutes = max(1, int(get_setting('auto_suspend_minutes', '5')))
    except Exception:
        minutes = 5
    now = time.time()
    targets = []
    for uid, lst in list(vps_data.items()):
        for vps in list(lst):
            if vps.get('status') == 'running' and not vps.get('suspended') and not vps.get('whitelisted'):
                targets.append((uid, vps))
    live = {v['container_name'] for _, v in targets}
    for key in list(_usage_since):
        if key not in live:
            _usage_since.pop(key, None)
    sem = asyncio.Semaphore(5)

    async def probe(uid, vps):
        async with sem:
            try:
                return uid, vps, await _sample_stats(vps['container_name'], vps.get('node_id'))
            except Exception as e:
                logger.warning(f"Usage guard: stats failed for {vps['container_name']}: {e}")
                return None

    results = await asyncio.gather(*[probe(u, v) for u, v in targets])
    offenders = []
    for r in results:
        if not r:
            continue
        uid, vps, st = r
        c = vps['container_name']
        if str(st.get('status', '')).lower() != 'running':
            _usage_since.pop(c, None)
            continue
        try:
            cpu = float(st.get('cpu') or 0)
            ram = float((st.get('ram') or {}).get('pct') or 0)
        except Exception:
            continue
        if cpu > CPU_THRESHOLD or ram > RAM_THRESHOLD:
            since = _usage_since.setdefault(c, now)
            if now - since >= minutes * 60:
                offenders.append((uid, vps, cpu, ram, now - since))
        else:
            _usage_since.pop(c, None)
    if not offenders:
        return
    # Safety net: many VPS "abusing" at the same moment is almost surely a measuring problem, not mining.
    if len(offenders) > MAX_AUTO_SUSPEND_PER_CYCLE:
        if now - _guard_alert_ts > ALERT_COOLDOWN_SECONDS:
            _guard_alert_ts = now
            names = ", ".join(f"`{o[1]['container_name']}`" for o in offenders[:15])
            await _deliver_admin_alert("⚠️ Auto-Suspend Paused",
                f"**{len(offenders)} VPS** are above the limit at the same time ({names}).\n"
                f"That looks like a measuring problem, so nobody was suspended automatically. "
                f"Check `{PREFIX}health` and use `{PREFIX}suspend-vps <container>` if needed.")
        return
    lines = []
    for uid, vps, cpu, ram, secs in offenders:
        c = vps['container_name']
        mins = max(1, int(secs // 60))
        reason = (f"High resource usage for {mins} min: CPU {cpu:.0f}%, RAM {ram:.0f}% "
                  f"(limit {CPU_THRESHOLD}% CPU / {RAM_THRESHOLD}% RAM) - possible mining/abuse")
        logger.warning(f"Auto-suspending {c}: {reason}")
        ok = await suspend_vps_record(vps, reason, "Auto Usage Guard")
        _usage_since.pop(c, None)
        if not ok:
            lines.append(f"⚠️ `{c}` • <@{uid}> • CPU {cpu:.0f}% RAM {ram:.0f}% • auto-suspend FAILED")
            continue
        embed = create_error_embed("🚨 VPS Suspended - High Usage",
            f"Your VPS `{c}` was **automatically suspended** because it used **CPU {cpu:.0f}% / RAM {ram:.0f}%** "
            f"(limit {CPU_THRESHOLD}% / {RAM_THRESHOLD}%) for {mins}+ minutes.\n"
            f"Crypto mining and other abuse are not allowed. Contact the administrator.")
        delivered = await _dm_user(uid, embed)
        lines.append(f"🚨 `{c}` • <@{uid}> • CPU {cpu:.0f}% RAM {ram:.0f}% for {mins} min" + ("" if delivered else " • ⚠️ DM failed"))
    if lines:
        await _deliver_admin_alert("🚨 VPS Auto-Suspended (high usage)",
            "\n".join(lines)[:3500] + f"\n\nUnsuspend with `{PREFIX}unsuspend-vps <container>` • whitelist with `{PREFIX}whitelist-vps <container> add`")

async def usage_guard_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await usage_guard_once()
        except Exception as e:
            logger.error(f"Usage guard failed: {e}")
        await asyncio.sleep(USAGE_GUARD_INTERVAL)

# ---------------------------------------------------------------- VPS usage guard (always on: suspend + DM)
USAGE_ALERT_INTERVAL = 60            # seconds between checks (24/7)
USAGE_ALERT_COOLDOWN = 30 * 60       # same VPS + same resource is reported again only after 30 minutes
_usage_alert_task = None
_vps_alert_last: Dict[Any, float] = {}

def _disk_pct_from_text(text) -> float:
    """'3.2G/10G (32%)' -> 32.0"""
    m = re.search(r'\((\d+(?:\.\d+)?)%\)', str(text or ''))
    return float(m.group(1)) if m else 0.0

USAGE_ALERT_LIMIT = 95.0   # always on, fixed: CPU / RAM / disk of a VPS at or above 95% -> suspend + DM

async def _dm_main_admins(embed) -> int:
    sent = 0
    for admin_id in list(main_admin_ids):
        try:
            user = bot.get_user(int(admin_id)) or await bot.fetch_user(int(admin_id))
            await user.send(embed=embed)
            sent += 1
        except Exception as e:
            logger.error(f"Could not DM usage alert to {admin_id}: {e}")
    return sent

async def vps_usage_alert_once():
    """Checks CPU / RAM / disk of every running VPS (every minute). A VPS at or above USAGE_ALERT_LIMIT is
    SUSPENDED right away and the owner + all main admins get a DM. Whitelisted VPS (!whitelist-vps) are never
    suspended, they only trigger the DM."""
    global _guard_alert_ts
    limit = USAGE_ALERT_LIMIT
    now = time.time()
    targets = []
    for uid, lst in list(vps_data.items()):
        for vps in list(lst):
            if vps.get('status') == 'running' and not vps.get('suspended'):
                targets.append((uid, vps))
    sem = asyncio.Semaphore(5)

    async def probe(uid, vps):
        async with sem:
            try:
                return uid, vps, await _sample_stats(vps['container_name'], vps.get('node_id'))
            except Exception as e:
                logger.warning(f"Usage alert: stats failed for {vps['container_name']}: {e}")
                return None

    results = await asyncio.gather(*[probe(u, v) for u, v in targets])
    offenders = []   # (uid, vps, values, [hot kinds])
    for r in results:
        if not r:
            continue
        uid, vps, st = r
        c = vps['container_name']
        if str(st.get('status', '')).lower() != 'running':
            continue
        try:
            values = {
                'CPU': float(st.get('cpu') or 0),
                'RAM': float((st.get('ram') or {}).get('pct') or 0),
                'DISK': _disk_pct_from_text(st.get('disk')),
            }
        except Exception:
            continue
        hot = [k for k, v in values.items() if v >= limit]
        if hot:
            offenders.append((uid, vps, values, hot))
        else:
            for k in values:
                _vps_alert_last.pop((c, k), None)
    if not offenders:
        return

    def _fmt(values, hot):
        return " • ".join(f"**{k} {values[k]:.0f}%**" for k in hot) + f"\n   CPU {values['CPU']:.0f}% | RAM {values['RAM']:.0f}% | Disk {values['DISK']:.0f}%"

    # Safety net: many VPS "hot" in the same minute is almost surely a measuring problem -> DM only, nobody suspended.
    if len(offenders) > MAX_AUTO_SUSPEND_PER_CYCLE:
        if now - _guard_alert_ts >= USAGE_ALERT_COOLDOWN:
            _guard_alert_ts = now
            lines = [f"🔥 `{o[1]['container_name']}` • <@{o[0]}> • {_fmt(o[2], o[3])}" for o in offenders[:12]]
            await _dm_main_admins(create_warning_embed(f"⚠️ {len(offenders)} VPS at/above {limit:.0f}% at the same time",
                "\n".join(lines)[:3500] + f"\n\nThat many at once looks like a measuring problem, so **nobody was suspended**. "
                f"Check `{PREFIX}health`, suspend manually with `{PREFIX}suspend-vps <container>`."))
        return

    admin_lines = []
    for uid, vps, values, hot in offenders:
        c = vps['container_name']
        node = get_node(vps.get('node_id', 1)) or {}
        head = f"`{c}` • <@{uid}> • node {node.get('name', '?')} • "
        if vps.get('whitelisted'):
            # never suspended - DM only, and not more often than every 30 minutes per VPS
            fresh = [k for k in hot if now - _vps_alert_last.get((c, k), 0) >= USAGE_ALERT_COOLDOWN]
            if fresh:
                for k in fresh:
                    _vps_alert_last[(c, k)] = now
                admin_lines.append(f"🔥 {head}{_fmt(values, hot)}\n   whitelisted - not suspended")
            continue
        reason = "High resource usage: " + ", ".join(f"{k} {values[k]:.0f}%" for k in hot) + f" (limit {limit:.0f}%)"
        logger.warning(f"Auto-suspending {c}: {reason}")
        ok = await suspend_vps_record(vps, reason, "Auto Usage Guard")
        if not ok:
            admin_lines.append(f"⚠️ {head}{_fmt(values, hot)}\n   auto-suspend FAILED - suspend it manually")
            continue
        embed = create_error_embed("🚨 VPS Suspended - High Usage",
            f"Your VPS `{c}` was **automatically suspended** because it reached **{', '.join(f'{k} {values[k]:.0f}%' for k in hot)}** "
            f"(limit {limit:.0f}%).\nContact the administrator.")
        delivered = await _dm_user(uid, embed)
        admin_lines.append(f"🚨 {head}{_fmt(values, hot)}\n   ✅ suspended" + ("" if delivered else " • ⚠️ owner DM failed"))
    if admin_lines:
        await _dm_main_admins(create_warning_embed(f"🚨 High VPS Usage (>= {limit:.0f}%)",
            "\n".join(admin_lines)[:3600] + f"\n\nUnsuspend: `{PREFIX}unsuspend-vps <container>` • whitelist: `{PREFIX}whitelist-vps <container> add`"))
        logger.warning(f"Usage guard: {len(admin_lines)} VPS handled - DM sent to main admins")

async def vps_usage_alert_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await vps_usage_alert_once()
        except Exception as e:
            logger.error(f"VPS usage alert loop failed: {e}")
        await asyncio.sleep(USAGE_ALERT_INTERVAL)

# ---------------------------------------------------------------- !myexpiry
@bot.command(name='myexpiry')
async def myexpiry_cmd(ctx):
    """!myexpiry - when do my VPS expire"""
    lst = vps_data.get(str(ctx.author.id), [])
    if not lst:
        await ctx.send(embed=create_info_embed("No VPS", "You don't have any VPS."))
        return
    lines = [f"**{i}.** `{v['container_name']}` — {expiry_text(v.get('expires_at'))}" for i, v in enumerate(lst, 1)]
    await ctx.send(embed=create_info_embed("⏳ Your VPS Expiry", "\n".join(lines)[:4000]))

async def _after_expiry_change(ctx, uid, vps, title: str, dm_text: str):
    """Shared tail of set-expiry / expiry-add / expiry-remove."""
    premark_thresholds(vps)
    restarted = await unsuspend_if_expiry_suspended(vps)
    save_vps_data()
    container = vps['container_name']
    exp_txt = expiry_text(vps.get('expires_at'))
    extra = "\n🟢 The VPS was suspended because it expired - it has been **started again**." if restarted else ""
    if vps.get('suspended') and not restarted:
        extra = f"\nℹ️ `{container}` is still suspended. Use `{PREFIX}unsuspend-vps {container}` if needed."
    await ctx.send(embed=create_success_embed(title, f"`{container}` → {exp_txt}{extra}"))
    if vps.get('expires_at'):
        await _dm_user(uid, create_info_embed("⏳ VPS Expiry Updated",
            f"{dm_text}\nNew expiry: {exp_txt}" + ("\n🟢 Your VPS is running again." if restarted else "")))

# ---------------------------------------------------------------- !set-expiry
@bot.command(name='set-expiry')
@is_admin()
async def set_expiry_cmd(ctx, container_name: str, *, value: str):
    """!set-expiry <container> <days|YYYY-MM-DD|none>"""
    uid, vps = find_vps_record(container_name)
    if not vps:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))
        return
    ok, new_exp = parse_expiry_input(value)
    if not ok:
        await ctx.send(embed=create_error_embed("Invalid Value",
            f"Use days (`30`), a date (`2026-12-31` or `2026-12-31 18:00`) or `none`.\n"
            f"Example: `{PREFIX}set-expiry {container_name} 30`"))
        return
    if new_exp is not None and new_exp <= datetime.now():
        await ctx.send(embed=create_error_embed("Invalid Date", "The expiry date must be in the future."))
        return
    vps['expires_at'] = new_exp.isoformat() if new_exp else None
    await _after_expiry_change(ctx, uid, vps, "Expiry Updated", f"The expiry of your VPS `{container_name}` was set by an administrator.")

# ---------------------------------------------------------------- !expiry-add (alias !extend-vps)
@bot.command(name='expiry-add', aliases=['extend-vps'])
@is_admin()
async def expiry_add_cmd(ctx, container_name: str, days: int):
    """!expiry-add <container> <days> - add days to a VPS expiry"""
    uid, vps = find_vps_record(container_name)
    if not vps:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))
        return
    if days < 1 or days > 3650:
        await ctx.send(embed=create_error_embed("Invalid Days", "Days must be between 1 and 3650."))
        return
    now = datetime.now()
    current = parse_expires(vps.get('expires_at'))
    base = current if current and current > now else now   # from the old expiry if still active, else from today
    vps['expires_at'] = (base + timedelta(days=days)).isoformat()
    await _after_expiry_change(ctx, uid, vps, f"Added {days} Days",
                               f"An administrator added **{days} days** to your VPS `{container_name}`.")

# ---------------------------------------------------------------- !expiry-remove
@bot.command(name='expiry-remove')
@is_admin()
async def expiry_remove_cmd(ctx, container_name: str, days: int):
    """!expiry-remove <container> <days> - remove days from a VPS expiry"""
    uid, vps = find_vps_record(container_name)
    if not vps:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))
        return
    if days < 1 or days > 3650:
        await ctx.send(embed=create_error_embed("Invalid Days", "Days must be between 1 and 3650."))
        return
    current = parse_expires(vps.get('expires_at'))
    if not current:
        await ctx.send(embed=create_error_embed("No Expiry", f"`{container_name}` has no expiry. Set one with `{PREFIX}set-expiry {container_name} <days>`."))
        return
    new_exp = current - timedelta(days=days)
    if new_exp <= datetime.now():
        await ctx.send(embed=create_error_embed("Too Many Days",
            f"Removing **{days}** days would put the expiry in the past ({expiry_text(current.isoformat())} now).\n"
            f"Remove fewer days, or use `{PREFIX}suspend-vps {container_name}`."))
        return
    vps['expires_at'] = new_exp.isoformat()
    await _after_expiry_change(ctx, uid, vps, f"Removed {days} Days",
                               f"An administrator removed **{days} days** from your VPS `{container_name}`.")

# ---------------------------------------------------------------- !expiry-list
@bot.command(name='expiry-list')
@is_admin()
async def expiry_list_cmd(ctx, days: int = 7):
    """!expiry-list [days] - expired VPS and VPS expiring within N days"""
    days = max(1, min(days, 3650))
    now = datetime.now()
    rows = []
    for uid, lst in vps_data.items():
        for vps in lst:
            exp = parse_expires(vps.get('expires_at'))
            if exp and (exp - now).total_seconds() <= days * 86400:
                rows.append((exp, uid, vps['container_name']))
    rows.sort()
    if not rows:
        await ctx.send(embed=create_success_embed("⏳ Expiry List", f"No VPS expired or expiring within {days} days."))
        return
    lines = []
    for exp, uid, container in rows:
        ts = int(exp.timestamp())
        icon = "🔴" if exp <= now else "🟡"
        lines.append(f"{icon} `{container}` • <@{uid}> • <t:{ts}:f> (<t:{ts}:R>)")
    shown = lines[:25]
    text = "\n".join(shown)
    if len(lines) > len(shown):
        text += f"\n…and {len(lines) - len(shown)} more"
    await ctx.send(embed=create_info_embed(f"⏳ Expiry List (next {days} days)", text[:4000]))

# ---------------------------------------------------------------- !expiry-default
@bot.command(name='expiry-default')
@is_admin()
async def expiry_default_cmd(ctx, value: str = None):
    """!expiry-default [days|off] - expiry given to every NEW VPS when !create has no days"""
    if value is None:
        d = int(get_setting('default_expiry_days', '0'))
        text = f"New VPS expire after **{d} days**." if d > 0 else "New VPS have **no expiry**."
        await ctx.send(embed=create_info_embed("⏳ Default Expiry", text + f"\nChange: `{PREFIX}expiry-default <days|off>`\n(Existing VPS are not changed - use `{PREFIX}set-expiry`.)"))
        return
    v = value.lower()
    if v in ('off', 'none', 'never', '0'):
        set_setting('default_expiry_days', '0')
        await ctx.send(embed=create_success_embed("Default Expiry", "New VPS will have **no expiry**."))
    elif v.isdigit() and 1 <= int(v) <= 3650:
        set_setting('default_expiry_days', v)
        await ctx.send(embed=create_success_embed("Default Expiry", f"New VPS will expire after **{v} days**."))
    else:
        await ctx.send(embed=create_error_embed("Invalid Value", "Use a number of days (1-3650) or `off`."))

# ---------------------------------------------------------------- !expiry-reminders
@bot.command(name='expiry-reminders')
@is_admin()
async def expiry_reminders_cmd(ctx, state: str = None):
    """!expiry-reminders [on|off] - reminders AND auto-suspend on expiry"""
    if state is None:
        on = str(get_setting('expiry_reminders', '1')) == '1'
        labels = ", ".join(lbl for _, lbl in EXPIRY_THRESHOLDS)
        await ctx.send(embed=create_info_embed("⏳ Expiry Reminders",
            f"Status: {'🟢 ON' if on else '🔴 OFF'}\nOwner (DM) and admins are reminded {labels} before expiry. "
            f"When a VPS expires it is **suspended automatically**.\nTurning this OFF stops reminders and expiry suspension."))
        return
    if state.lower() not in ('on', 'off'):
        await ctx.send(embed=create_error_embed("Invalid Value", f"Use `{PREFIX}expiry-reminders on` or `off`."))
        return
    set_setting('expiry_reminders', '1' if state.lower() == 'on' else '0')
    await ctx.send(embed=create_success_embed("Expiry Reminders", f"Expiry reminders + auto-suspend are now **{state.upper()}**."))

# Run the bot
if __name__ == "__main__":
    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)
    else:
        logger.error("No Discord token found in DISCORD_TOKEN environment variable.")
