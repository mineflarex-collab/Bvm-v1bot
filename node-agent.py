#!/usr/bin/env python3
"""
BVM Node Agent  -  Made by MineFlareX
Runs on a REMOTE node server (with LXD/LXC installed) so the main BVM bot can
create and manage VPS containers on it.

Usage:
    python3 node-agent.py --api_key=YOUR_KEY --port=5000
    python3 node-agent.py --api_key=YOUR_KEY --port=5000 --host=0.0.0.0

Then add the node in Discord:  !node create   (URL = http://NODE_IP:5000)
The API key is sent to you in DM by the bot when the node is created.

Only the Python standard library is needed (no pip packages).

Endpoints (all need ?api_key=KEY):
    GET  /api/ping
    GET  /api/get_host_stats
    POST /api/get_container_stats   {"container": "name"}
    POST /api/execute               {"command": "lxc ..."}   (only `lxc` commands)
"""
import argparse
import hmac
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

API_KEY = ""
EXEC_TIMEOUT = 300
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\-_.]{0,62}$')


# ---------------------------------------------------------------- helpers
def run(cmd, timeout=30):
    """Run a command (list) without a shell. Returns (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout} seconds"
    except Exception as e:
        return 1, "", str(e)


def _cpu_times():
    with open('/proc/stat') as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)


def host_cpu_percent():
    try:
        i1, t1 = _cpu_times()
        time.sleep(0.5)
        i2, t2 = _cpu_times()
        dt = t2 - t1
        return round(100.0 * (1 - (i2 - i1) / dt), 1) if dt > 0 else 0.0
    except Exception:
        return 0.0


def host_ram_percent():
    try:
        info = {}
        with open('/proc/meminfo') as f:
            for line in f:
                k, v = line.split(':', 1)
                info[k] = int(v.split()[0])
        total = info['MemTotal']
        avail = info.get('MemAvailable', info.get('MemFree', 0))
        return round((total - avail) / total * 100, 1) if total else 0.0
    except Exception:
        return 0.0


def _human(n):
    for unit in ('B', 'K', 'M', 'G', 'T'):
        if n < 1024 or unit == 'T':
            return f"{n:.0f}{unit}" if unit == 'B' else f"{n:.1f}{unit}"
        n /= 1024


def host_disk():
    try:
        u = shutil.disk_usage('/')
        return f"{_human(u.used)}/{_human(u.total)} ({u.used / u.total * 100:.0f}%)"
    except Exception:
        return "Unknown"


def container_stats(name):
    stats = {"status": "unknown", "cpu": 0.0,
             "ram": {"used": 0, "total": 0, "pct": 0.0},
             "disk": "Unknown", "uptime": "Unknown"}
    rc, out, _ = run(["lxc", "info", name], 20)
    for line in out.splitlines():
        if line.startswith("Status: "):
            stats["status"] = line.split(": ", 1)[1].strip().lower()
            break
    if stats["status"] != "running":
        return stats

    rc, out, _ = run(["lxc", "exec", name, "--", "top", "-bn1"], 20)
    for line in out.splitlines():
        if '%Cpu(s):' in line:
            m = re.search(r'([\d.]+)\s*id', line)
            if m:
                stats["cpu"] = round(100.0 - float(m.group(1)), 1)
            break

    rc, out, _ = run(["lxc", "exec", name, "--", "free", "-m"], 20)
    lines = out.splitlines()
    if len(lines) > 1:
        try:
            parts = lines[1].split()
            total, used = int(parts[1]), int(parts[2])
            stats["ram"] = {"used": used, "total": total,
                            "pct": (used / total * 100) if total else 0.0}
        except Exception:
            pass

    rc, out, _ = run(["lxc", "exec", name, "--", "df", "-h", "/"], 20)
    for line in out.splitlines():
        if '/dev/' in line and ' /' in line:
            parts = line.split()
            if len(parts) >= 5:
                stats["disk"] = f"{parts[2]}/{parts[1]} ({parts[4]})"
                break

    rc, out, _ = run(["lxc", "exec", name, "--", "uptime"], 20)
    if out.strip():
        stats["uptime"] = out.strip()
    return stats


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "BVMNodeAgent/1.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, query):
        supplied = (query.get("api_key") or [""])[0]
        if not API_KEY or not hmac.compare_digest(supplied.encode(), API_KEY.encode()):
            self._send(401, {"detail": "Invalid API key"})
            return False
        return True

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 1_000_000:
                return None
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return None

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._authorized(query):
            return
        if url.path == "/api/ping":
            self._send(200, {"status": "ok"})
        elif url.path == "/api/get_host_stats":
            self._send(200, {"cpu": host_cpu_percent(), "ram": host_ram_percent(), "disk": host_disk()})
        else:
            self._send(404, {"detail": "Not found"})

    def do_POST(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._authorized(query):
            return
        data = self._json_body()
        if data is None:
            self._send(400, {"detail": "Invalid JSON body"})
            return

        if url.path == "/api/get_container_stats":
            name = str(data.get("container", ""))
            if not NAME_RE.match(name):
                self._send(400, {"detail": "Invalid container name"})
                return
            self._send(200, container_stats(name))

        elif url.path == "/api/execute":
            command = str(data.get("command", ""))
            try:
                argv = shlex.split(command)
            except ValueError as e:
                self._send(400, {"detail": f"Bad command: {e}"})
                return
            # Safety: only plain `lxc ...` commands, never a shell.
            if not argv or argv[0] != "lxc":
                self._send(403, {"detail": "Only lxc commands are allowed"})
                return
            rc, out, err = run(argv, EXEC_TIMEOUT)
            self._send(200, {"returncode": rc, "stdout": out.strip(), "stderr": err.strip()})
        else:
            self._send(404, {"detail": "Not found"})


def main():
    global API_KEY
    ap = argparse.ArgumentParser(description="BVM Node Agent")
    ap.add_argument("--api_key", default=os.getenv("NODE_API_KEY", ""), help="API key (same as in the bot)")
    ap.add_argument("--port", type=int, default=5000, help="Port to listen on (default 5000)")
    ap.add_argument("--host", default="0.0.0.0", help="Address to bind (default 0.0.0.0)")
    args = ap.parse_args()
    if not args.api_key:
        raise SystemExit("Error: --api_key is required (or set NODE_API_KEY)")
    if shutil.which("lxc") is None:
        print("WARNING: `lxc` was not found on this server. Install LXD first.")
    API_KEY = args.api_key
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"BVM Node Agent listening on {args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopping...")


if __name__ == "__main__":
    main()
