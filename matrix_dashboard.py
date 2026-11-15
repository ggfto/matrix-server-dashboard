#!/usr/bin/env python3
"""
Matrix Server Dashboard
Terminal dashboard for a Linux server's local monitor (tty1), SSH or browser, with:
- Animated boot sequence and wipe transition into the dashboard
- Matrix rain background that also falls behind the portrait
- ASCII portrait shaded by character density, with decode reveal,
  scan band, shimmer and glitch bursts
- CPU (total, per core, history), RAM, swap, temperature, load, disks
- Every Docker container: state, uptime, health, CPU/MEM, HTTP endpoint probe
- Host systemd units
- Claude Code agent (when installed): processes, sessions, remote-control, version, CPU/MEM
- Auto-discovery of disks, host services, containers and their endpoints (autodetect.py)
- Network throughput graphs and latency
- Live event console with typing effect
- Data collection in background threads and diff-based rendering

Only glyphs present in the console font (Uni2-Fixed16) are used.
Keyboard shortcuts (H shows them on screen): C settings menu, W network map, X terminal,
N speed test, E eco mode, T screen off, M rain, I portrait, G glitch, L redraw,
D power off, R reboot, Q quit. D, R and Q ask for confirmation.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import random
import re
import select
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil
from PIL import Image, ImageOps

import autodetect
from claude_usage import UsageTracker, short_model
from netmap import NetMapper
from web_view import WebMirror

VERSION = "3.0"

RESET = "\x1b[0m"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
ALT_SCREEN = "\x1b[?1049h"
MAIN_SCREEN = "\x1b[?1049l"
CLEAR = "\x1b[2J"
WRAP_OFF = "\x1b[?7l"
WRAP_ON = "\x1b[?7h"

# Style ids. G0..G6 is a green ramp from almost black to pale green.
NONE, G0, G1, G2, G3, G4, G5, G6, RED, AMBER, CYAN, TEXT, WHITE, KEYCAP, KEYCAP_RED = range(15)
GREENS = (G0, G1, G2, G3, G4, G5, G6)

SGR_256 = {
    NONE: "0", G0: "38;5;22", G1: "38;5;28", G2: "38;5;34", G3: "38;5;40",
    G4: "38;5;46", G5: "38;5;83", G6: "38;5;157",
    RED: "38;5;196", AMBER: "38;5;214", CYAN: "38;5;51", TEXT: "38;5;151", WHITE: "38;5;231",
    KEYCAP: "38;5;16;48;5;40", KEYCAP_RED: "38;5;231;48;5;160",
}

# The Linux console only has 16 colors, so its palette is reprogrammed
# (OSC P) with shades of green and the styles are mapped onto those slots.
# Every SGR starts with 0 because bright colors on the console set the bold
# attribute, which would otherwise leak into the next color.
LINUX_PALETTE = {
    0: "000000", 1: "ff3b3b", 2: "00c040", 3: "ffaf00",
    4: "005a1e", 5: "00902e", 6: "00e0ff", 7: "9ec99e",
    8: "06240f", 9: "ff5f5f", 10: "39ff6a", 11: "ffd75f",
    12: "c6ffd2", 13: "1fdc58", 14: "8cffff", 15: "ffffff",
}
SGR_LINUX = {
    NONE: "0", G0: "90", G1: "34", G2: "35", G3: "32", G4: "95", G5: "92", G6: "94",
    RED: "91", AMBER: "33", CYAN: "36", TEXT: "37", WHITE: "97",
    KEYCAP: "30;42", KEYCAP_RED: "97;41",
}

# Rain glyphs dimmed when they fall behind the portrait (indexed by style id).
DIM_LUT = [NONE, G0, G0, G0, G1, G1, G1, G2, G1, G1, G1, G1, G2, G1, G1]

MATRIX_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ$+-*/=%#&<>[]{}|:;~^¦§¤¥£ÆØÞßµ"
GLITCH_CHARS = "01#$%@&*<>/\\|=+"
SCRAMBLE_CHARS = "0123456789ABCDEF#$%&*+=<>"
BORDER_CHARS = set("─│╭╮╰╯═║╔╗╚╝┄")
SPINNER = "-\\|/"

DAYS_PT = ["SEG", "TER", "QUA", "QUI", "SEX", "SAB", "DOM"]
MONTHS_PT = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]

BIG_DIGITS = {
    "0": ("┏━┓", "┃ ┃", "┗━┛"),
    "1": ("╺┓ ", " ┃ ", "╺┻╸"),
    "2": ("╺━┓", "┏━┛", "┗━╸"),
    "3": ("╺━┓", " ━┫", "╺━┛"),
    "4": ("╻ ╻", "┗━┫", "  ╹"),
    "5": ("┏━╸", "┗━┓", "╺━┛"),
    "6": ("┏━╸", "┣━┓", "┗━┛"),
    "7": ("╺━┓", "  ┃", "  ╹"),
    "8": ("┏━┓", "┣━┫", "┗━┛"),
    "9": ("┏━┓", "┗━┫", "╺━┛"),
}

DEFAULT_CONFIG: dict[str, Any] = {
    # "" = "<HOSTNAME> // MATRIX SERVER MONITOR"
    "title": "",
    # name shown on the boot screen; "" = the Linux user
    "operator": "",
    "brand": "MATRIX SECURE TERMINAL",
    # photo (png/jpg) or ready ASCII art (.txt); missing file = server name in big letters
    "image": "assets/portrait.png",
    "fps": 12,
    "matrix_density": 0.45,
    "boot_animation": True,
    "console_palette": True,
    "ping_host": "1.1.1.1",
    "ping_host6": "2606:4700:4700::1111",
    # Lists with "auto" are filled by autodetect.py; extra items next to "auto" are kept.
    "mounts": ["auto"],
    "thresholds": {
        "cpu": 85,
        "ram": 85,
        "disk": 90,
        "temperature": 80
    },
    "systemd_units": ["auto"],
    "checks": ["auto"],
    "hide_containers": [],
    # optional integrations, off unless configured
    "backup_state": "",
    "icloud": {},
    "network_interface": "auto",
    # "auto" = show the Claude Code panel only when Claude Code is installed
    "claude": "auto",
    "claude_plan": "",
    # control: keys/clicks from the page; terminal: a shell in the browser (no password!)
    "web": {"enabled": False, "port": 8095, "fps": 5, "control": False, "terminal": False,
            "allow": ["auto"]},
    "claude_limits_file": "~/.claude/statusline-last.json",
    "screen_off": {"enabled": False, "from": "00:00", "to": "07:00"},
}


# ---------------------------------------------------------------- data model

@dataclass
class Container:
    name: str
    state: str
    health: str
    age: str
    exit_code: str
    image: str
    ports: str
    cpu: float | None = None
    mem: str = ""


@dataclass
class ClaudeInfo:
    running: bool = False
    version: str = ""
    sessions: int = 0
    remote: str = ""
    procs: int = 0
    cpu: float = 0.0
    rss: int = 0
    since: float = 0.0
    working: bool = False


@dataclass
class Event:
    wall: float
    mono: float
    tag: str
    text: str
    style: int


@dataclass
class Snapshot:
    hostname: str = "-"
    kernel: str = "-"
    cores: int = 1
    cpu: float = 0.0
    per_core: list[float] = field(default_factory=list)
    ram: float = 0.0
    ram_used: int = 0
    ram_total: int = 0
    swap: float = 0.0
    swap_used: int = 0
    swap_total: int = 0
    load: tuple[float, float, float] = (0.0, 0.0, 0.0)
    uptime_s: int = 0
    temperature: float | None = None
    ip: str = "-"
    iface: str = "-"
    procs: int = 0
    disks: list[tuple[str, float, int, int]] = field(default_factory=list)
    down: float = 0.0
    up: float = 0.0
    latency: float | None = None
    internet: bool = False
    net4: bool = False
    net6: bool | None = None
    lat4: float | None = None
    lat6: float | None = None
    docker_ok: bool = False
    containers: list[Container] = field(default_factory=list)
    flagged: set[str] = field(default_factory=set)
    units: list[tuple[str, str, str]] = field(default_factory=list)
    probes: dict[str, tuple[bool, int, float | None, str]] = field(default_factory=dict)
    claude: ClaudeInfo = field(default_factory=ClaudeInfo)
    backup: dict[str, Any] | None = None
    tailscale: dict[str, Any] | None = None
    icloud: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    netmap: dict[str, Any] | None = None
    nettraffic: dict[str, Any] | None = None
    cpu_hist: list[float] = field(default_factory=list)
    down_hist: list[float] = field(default_factory=list)
    up_hist: list[float] = field(default_factory=list)
    lat_hist: list[float] = field(default_factory=list)
    claude_hist: list[float] = field(default_factory=list)


# ------------------------------------------------------------------- helpers

def run_cmd(args: list[str], timeout: float = 2.0) -> tuple[int, str]:
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout.strip()
    except Exception:
        return 1, ""


def get_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return "-"
    finally:
        sock.close()


def iface_for_ip(ip: str) -> str:
    try:
        for name, addrs in psutil.net_if_addrs().items():
            if any(a.family == socket.AF_INET and a.address == ip for a in addrs):
                return name
    except Exception:
        pass
    return "-"


def get_temperature() -> float | None:
    try:
        temps = psutil.sensors_temperatures(fahrenheit=False)
    except Exception:
        return None
    for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
        valid = [e.current for e in temps.get(key, []) if e.current is not None]
        if valid:
            return max(valid)
    for entries in temps.values():
        valid = [e.current for e in entries if e.current is not None]
        if valid:
            return max(valid)
    return None


def tcp_latency(host: str, port: int, timeout: float = 1.0) -> float | None:
    started = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return (time.monotonic() - started) * 1000
    except OSError:
        return None


_UNVERIFIED_SSL = ssl._create_unverified_context()


def http_probe(url: str, host_header: str = "", timeout: float = 1.5) -> tuple[bool, int, float | None]:
    headers = {"User-Agent": f"matrix-dashboard/{VERSION}"}
    if host_header:
        headers["Host"] = host_header
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_UNVERIFIED_SSL) as response:
            code = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception:
        return False, 0, None
    return code < 500, code, (time.monotonic() - started) * 1000


AGE_RE = re.compile(r"(\d+|About an?|Less than a|an?)\s+(second|minute|hour|day|week|month|year)", re.I)
AGE_UNITS = {"second": "s", "minute": "m", "hour": "h", "day": "d", "week": "w", "month": "mo", "year": "y"}
PORT_RE = re.compile(r"(0\.0\.0\.0|\[::\]|127\.0\.0\.1):(\d+)->")


def short_age(status: str) -> str:
    match = AGE_RE.search(status)
    if not match:
        return "-"
    num = match.group(1)
    if num.isdigit():
        n = num
    elif num.lower().startswith("less"):
        n = "<1"
    else:
        n = "1"
    return f"{n}{AGE_UNITS[match.group(2).lower()]}"


def short_ports(ports: str) -> str:
    seen: list[str] = []
    for host, port in PORT_RE.findall(ports):
        label = f"lo:{port}" if host == "127.0.0.1" else f":{port}"
        if label not in seen and f":{port}" not in seen:
            seen.append(label)
    return ",".join(seen[:2]) if seen else "-"


def parse_size(text: str) -> float:
    match = re.match(r"([\d.]+)\s*([KMGT]?i?B)", text.strip())
    if not match:
        return 0.0
    mult = {"B": 1, "KB": 1e3, "KiB": 1024, "MB": 1e6, "MiB": 1024**2, "GB": 1e9, "GiB": 1024**3, "TB": 1e12, "TiB": 1024**4}
    return float(match.group(1)) * mult.get(match.group(2), 1)


def fmt_size(value: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            if unit in ("B", "K", "M"):
                return f"{value:.0f}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}T"


def fmt_tokens(value: float) -> str:
    value = float(value or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if value >= div:
            return f"{value / div:.1f}{unit}"
    return f"{value:.0f}"


def fmt_rate(value: float) -> str:
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    val = max(0.0, float(value))
    for unit in units:
        if val < 1024 or unit == units[-1]:
            return f"{val:.0f} {unit}" if unit == "B/s" else f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} GB/s"


def fmt_uptime(seconds: float) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}h {mins:02d}m"
    if hours:
        return f"{hours}h {mins:02d}m"
    return f"{mins}m"


def graph_rows(values: list[float], width: int, height: int, vmax: float | None = None) -> list[str]:
    """Multi-row bar graph using only █▒░, top row first."""
    vals = values[-width:]
    vals = [0.0] * (width - len(vals)) + vals
    top = vmax if vmax else max(max(vals, default=0.0), 1e-9)
    rows = []
    for r in range(height - 1, -1, -1):
        line = []
        for v in vals:
            f = max(0.0, v) / top * height - r
            if f >= 0.75:
                line.append("█")
            elif f >= 0.45:
                line.append("▒")
            elif f >= 0.12:
                line.append("░")
            else:
                line.append(" ")
        rows.append("".join(line))
    return rows


def heat_strip(values: list[float], width: int, vmax: float | None = None) -> str:
    vals = values[-width:]
    top = vmax if vmax else max(max(vals, default=0.0), 1e-9)
    out = []
    for v in vals:
        f = max(0.0, v) / top
        out.append("█" if f > 0.75 else "▒" if f > 0.4 else "░" if f > 0.1 else "·")
    return "·" * (width - len(out)) + "".join(out)


def merge_config(path: Path) -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
            for key, value in user.items():
                if key.startswith("_"):
                    continue
                if isinstance(value, dict) and isinstance(config.get(key), dict):
                    config[key].update(value)
                else:
                    config[key] = value
        except Exception as exc:
            print(f"Could not read {path}: {exc}", file=sys.stderr)
    return autodetect.resolve(config)


def claude_version_fallback() -> str:
    base = Path.home() / ".local/share/claude/versions"
    try:
        names = [p.name for p in base.iterdir() if re.fullmatch(r"[\d.]+", p.name)]
        names.sort(key=lambda v: tuple(int(x) for x in v.split(".")))
        return names[-1] if names else ""
    except OSError:
        return ""


# ----------------------------------------------------------------- collector

class Collector:
    """Gathers server data in two background threads.

    The fast loop (cpu, memory, network, docker ps, claude) builds a new
    Snapshot every second; the slow loop (docker stats, HTTP probes,
    systemctl) feeds results that the fast loop merges in. The renderer only
    reads `self.snap`, which is replaced atomically.
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.snap = Snapshot()
        self.events: deque[Event] = deque(maxlen=200)
        self.stop = threading.Event()
        self.ping_host = str(config.get("ping_host", "1.1.1.1"))
        self.ping_host6 = str(config.get("ping_host6", "2606:4700:4700::1111"))
        # Consecutive failures per family; a link only counts as down after 3.
        self.net_fails = {"v4": 0, "v6": 0}
        self.net_lat: dict[str, float | None] = {"v4": None, "v6": None}
        self.has_v6 = False
        self.prev_net: dict[str, bool] = {}
        self.hidden = {str(x).lower() for x in config.get("hide_containers", [])}
        self.thresholds = config.get("thresholds", {})

        self.cpu_hist: deque[float] = deque(maxlen=300)
        self.down_hist: deque[float] = deque(maxlen=300)
        self.up_hist: deque[float] = deque(maxlen=300)
        self.lat_hist: deque[float] = deque(maxlen=120)
        self.claude_hist: deque[float] = deque(maxlen=120)

        self.hostname = socket.gethostname()
        self.kernel = os.uname().release
        self.cores = psutil.cpu_count() or 1
        psutil.cpu_percent(percpu=True)
        psutil.cpu_percent()

        self.ip = get_local_ip()
        self.iface = iface_for_ip(self.ip)
        self.last_net = self._net_counters()
        self.last_net_at = time.monotonic()

        self.containers: list[Container] = []
        self.docker_ok = False
        self.flagged: set[str] = set()
        self.claude = ClaudeInfo()
        self.claude_procs: dict[int, psutil.Process] = {}
        self.claude_idle_since = time.monotonic()
        self.latency: float | None = None
        self.docker_stats: dict[str, tuple[float, str]] = {}
        self.units: list[tuple[str, str, str]] = []
        self.probes: dict[str, tuple[bool, int, float | None, str]] = {}

        self.prev_containers: dict[str, tuple[str, str]] | None = None
        self.prev_units: dict[str, str] | None = None
        self.prev_probes: dict[str, bool] = {}
        self.prev_sessions: int | None = None
        self.prev_internet: bool | None = None
        self.alert_state: dict[str, bool] = {}
        self.backup_path = Path(os.path.expanduser(str(config.get("backup_state") or ""))) if config.get("backup_state") else None
        self.backup: dict[str, Any] | None = None
        self.prev_backup_status: str | None = None
        self.tailscale: dict[str, Any] | None = None
        self.prev_tailscale: str | None = None
        self.icloud: dict[str, Any] | None = None
        self.icloud_prev: tuple[float, int] | None = None
        self.icloud_speed = 0.0
        self.icloud_day = ("", 0)
        self.icloud_applied: tuple[str, str] | None = None
        self.prev_icloud_status: str | None = None
        self.usage_tracker = UsageTracker()
        self.usage: dict[str, Any] | None = None
        self.usage_at = 0.0
        self.plan: dict[str, Any] | None = None
        self.probe_cycle = 0
        self.last_probe_event = time.monotonic()
        self.netmap_styles = {"new": AMBER, "gone": G3}
        self.netmapper = NetMapper(config, self.iface,
                                   lambda tag, text, kind: self.emit(tag, text, self.netmap_styles.get(kind, G4)))

    # --- events

    def emit(self, tag: str, text: str, style: int = G4):
        self.events.append(Event(time.time(), time.monotonic(), tag, text, style))

    # --- collectors

    def _net_counters(self) -> tuple[int, int]:
        try:
            per = psutil.net_io_counters(pernic=True)
            if self.iface in per:
                io = per[self.iface]
            else:
                io = psutil.net_io_counters()
            return io.bytes_recv, io.bytes_sent
        except Exception:
            return 0, 0

    def _docker_ps(self):
        code, out = run_cmd(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}"],
            timeout=4.0,
        )
        if code != 0:
            self.docker_ok = False
            self.containers = []
            return
        self.docker_ok = True
        result = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            name, state, status, image = parts[:4]
            if name.lower() in self.hidden:
                continue
            ports = parts[4] if len(parts) > 4 else ""
            health = ""
            hm = re.search(r"\((healthy|unhealthy|health: starting)\)", status)
            if hm:
                health = "starting" if "starting" in hm.group(1) else hm.group(1)
            em = re.search(r"Exited \((\d+)\)", status)
            cpu, mem = self.docker_stats.get(name, (None, ""))
            result.append(Container(
                name=name,
                state=state.lower(),
                health=health,
                age=short_age(status),
                exit_code=em.group(1) if em else "",
                image=image,
                ports=short_ports(ports),
                cpu=cpu if state.lower() == "running" else None,
                mem=mem if state.lower() == "running" else "",
            ))
        self.containers = result

    def _claude(self):
        if not self.config.get("claude"):
            return
        info = ClaudeInfo()
        seen: set[int] = set()
        version = ""
        for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
            try:
                cmd = proc.info.get("cmdline") or []
                if not cmd:
                    continue
                exe = cmd[0]
                if os.path.basename(exe) != "claude" and "/claude/versions/" not in exe:
                    continue
                pid = proc.info["pid"]
                seen.add(pid)
                cached = self.claude_procs.get(pid)
                if cached is None:
                    cached = proc
                    self.claude_procs[pid] = proc
                    cached.cpu_percent(None)
                cpu = cached.cpu_percent(None)
                rss = cached.memory_info().rss
                info.procs += 1
                info.cpu += cpu
                info.rss += rss
                created = proc.info.get("create_time") or 0.0
                if not info.since or created < info.since:
                    info.since = created
                vm = re.search(r"/versions/([\d.]+)", exe)
                if vm:
                    version = vm.group(1)
                if "remote-control" in cmd:
                    if "--name" in cmd and cmd.index("--name") + 1 < len(cmd):
                        info.remote = cmd[cmd.index("--name") + 1]
                    else:
                        info.remote = "on"
                else:
                    info.sessions += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        for pid in list(self.claude_procs):
            if pid not in seen:
                del self.claude_procs[pid]
        info.running = info.procs > 0
        info.version = version or claude_version_fallback()
        info.working = info.running and info.cpu >= 4.0
        self.claude = info

    def _backup(self):
        if not self.backup_path:
            return
        try:
            data = json.loads(self.backup_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.backup = {"status": "not_started"}
            return
        except (OSError, ValueError):
            return
        # The wrapper writes every 5 s while running; a stale file means it died.
        if data.get("status") == "running" and time.time() - float(data.get("updated", 0)) > 90:
            data["status"] = "stalled"
        self.backup = data

    def _tailscale(self):
        if not shutil.which("tailscale"):
            return
        code, out = run_cmd(["tailscale", "status", "--json"], timeout=4.0)
        if code != 0 and not out:
            self.tailscale = {"state": "Unavailable"}
            return
        try:
            data = json.loads(out)
        except ValueError:
            return
        me = data.get("Self") or {}
        ips = data.get("TailscaleIPs") or []
        expiry = me.get("KeyExpiry") or ""
        days = None
        if expiry and not expiry.startswith("0001"):
            try:
                exp = time.mktime(time.strptime(expiry[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
                days = int((exp - time.time()) // 86400)
            except ValueError:
                days = None
        self.tailscale = {
            "state": str(data.get("BackendState", "")),
            "ip": next((ip for ip in ips if "." in ip), ips[0] if ips else ""),
            "online": bool(me.get("Online")),
            "expiry_days": days,
        }

    def apply_icloud_limit(self, name: str, limit: str) -> bool:
        """Shape the download of the iCloud container inside its own network namespace."""
        script = "tc qdisc del dev eth0 ingress 2>/dev/null; "
        if limit.lower() != "off":
            mbytes = float(limit.rstrip("Mm"))
            script += ("tc qdisc add dev eth0 handle ffff: ingress && "
                       f"tc filter add dev eth0 parent ffff: protocol all u32 match u32 0 0 "
                       f"police rate {int(mbytes * 8)}mbit burst 1m drop flowid :1")
        else:
            script += "true"
        code, _ = run_cmd(["docker", "run", "--rm", "--network", f"container:{name}", "--cap-add", "NET_ADMIN",
                           "matrix-tc", "sh", "-c", script], timeout=30.0)
        return code == 0

    def _icloud(self):
        cfg = self.config.get("icloud") or {}
        name = str(cfg.get("container") or "")
        if not name:
            return
        folder = str(cfg.get("folder") or "/data/fotos/icloud")
        items = total_bytes = 0
        for root, _, files in os.walk(folder):
            for fname in files:
                try:
                    total_bytes += os.lstat(os.path.join(root, fname)).st_size
                except OSError:
                    continue
                # Live Photos are saved as photo + "_HEVC.MOV"; count them once.
                if not fname.upper().endswith("_HEVC.MOV"):
                    items += 1
        now = time.monotonic()
        if self.icloud_prev is not None and now > self.icloud_prev[0]:
            inst = max(0.0, (total_bytes - self.icloud_prev[1]) / (now - self.icloud_prev[0]))
            self.icloud_speed = inst if not self.icloud_speed else self.icloud_speed * 0.5 + inst * 0.5
        self.icloud_prev = (now, total_bytes)
        today = time.strftime("%Y-%m-%d")
        if self.icloud_day[0] != today:
            # Persist the day's starting point so "today" survives dashboard restarts.
            day_file = Path(__file__).resolve().parent / ".icloud_day.json"
            try:
                saved = json.loads(day_file.read_text())
            except (OSError, ValueError):
                saved = {}
            if saved.get("date") == today:
                self.icloud_day = (today, int(saved.get("bytes", total_bytes)))
            else:
                self.icloud_day = (today, total_bytes)
                try:
                    day_file.write_text(json.dumps({"date": today, "bytes": total_bytes}))
                except OSError:
                    pass

        code, out = run_cmd(["docker", "inspect", "-f", "{{.State.Status}} {{.State.StartedAt}}", name], timeout=4.0)
        state, started = (out.split() + ["", ""])[:2] if code == 0 else ("missing", "")
        prev = self.icloud or {}
        info: dict[str, Any] = {
            "container": name, "state": state, "items": items, "bytes": total_bytes,
            "total": prev.get("total", 0), "speed": self.icloud_speed if state == "running" else 0.0,
            "today": total_bytes - self.icloud_day[1], "current": prev.get("current", ""),
            "limit": str(cfg.get("limit", "off")), "status": "paused" if state != "running" else "starting",
            "next_check": None,
        }
        if state == "running":
            try:
                proc = subprocess.run(["docker", "logs", "--since", "3h", "--tail", "4000", name],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=8)
                lines = proc.stdout.splitlines()
            except Exception:
                lines = []
            status, last_ts, auth_error = "starting", "", False
            for line in lines:
                if "DEBUG" in line[:30]:
                    if "Authenticating" in line:
                        last_ts = line[:19]
                        status = "authenticating"
                    continue
                m = re.search(r"Downloading (\d+) original", line)
                if m:
                    info["total"] = int(m.group(1))
                    status, auth_error = "downloading", False
                m = re.search(r"Downloaded /data/(.+)$", line)
                if m:
                    info["current"] = m.group(1).strip()
                    status, auth_error = "downloading", False
                m = re.search(r"Waiting for (\d+) sec", line)
                if m:
                    status = "idle"
                    try:
                        ts = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
                        info["next_check"] = ts + int(m.group(1))
                    except ValueError:
                        pass
                if re.search(r"(?i)(invalid email|password|authenticat|two-factor|2fa|mfa).*(fail|invalid|required|expired|error)", line):
                    auth_error = True
            if status == "authenticating" and last_ts:
                try:
                    waited = time.time() - time.mktime(time.strptime(last_ts, "%Y-%m-%d %H:%M:%S"))
                    if waited > 180:
                        auth_error = True
                except ValueError:
                    pass
            if not info["total"]:
                # The "Downloading N" line may be older than the window above.
                try:
                    full = subprocess.run(["docker", "logs", name], stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True, timeout=30).stdout
                    found = re.findall(r"Downloading (\d+) original", full)
                    if found:
                        info["total"] = int(found[-1])
                except Exception:
                    pass
            info["status"] = "login" if auth_error and status != "downloading" else status
            desired = info["limit"]
            if self.icloud_applied != (started, desired):
                if self.apply_icloud_limit(name, desired):
                    self.icloud_applied = (started, desired)
        self.icloud = info

    def _usage(self):
        if not self.config.get("claude"):
            return
        tracker = self.usage_tracker
        tracker.refresh()
        summary = tracker.summary()
        summary["hourly"] = tracker.hourly(24)
        summary["daily"] = tracker.daily(14)
        today = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        week_start = today - time.localtime().tm_wday * 86400
        summary["weeks"] = [(week_start - i * 7 * 86400,
                             tracker.window(week_start - i * 7 * 86400, week_start - (i - 1) * 7 * 86400))
                            for i in range(4)]
        self.usage = summary
        self.usage_at = time.time()

    def _plan_limits(self):
        """Plan usage windows saved by the Claude Code statusline script."""
        if not self.config.get("claude"):
            return
        path = Path(os.path.expanduser(str(self.config.get("claude_limits_file") or "")))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            self.plan = None
            return
        limits = data.get("rate_limits") or {}
        windows = {}
        for key in ("five_hour", "seven_day"):
            w = limits.get(key) or {}
            pct = w.get("used_percentage", w.get("utilization"))
            if pct is not None:
                windows[key] = {"pct": float(pct), "resets": float(w.get("resets_at") or 0)}
        self.plan = {"windows": windows, "updated": mtime, "plan": str(self.config.get("claude_plan") or "")}

    def _slow_once(self):
        code, out = run_cmd(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
            timeout=15.0,
        )
        if code == 0:
            stats = {}
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                try:
                    cpu = float(parts[1].strip().rstrip("%"))
                except ValueError:
                    cpu = None
                stats[parts[0]] = (cpu, fmt_size(parse_size(parts[2].split("/")[0])))
            self.docker_stats = stats

        units_cfg = self.config.get("systemd_units", [])
        if units_cfg:
            names = [str(u.get("unit", "")) for u in units_cfg]
            _, out = run_cmd(["systemctl", "is-active", *names], timeout=3.0)
            states = out.splitlines()
            units = []
            for i, unit in enumerate(units_cfg):
                state = states[i].strip() if i < len(states) else "unknown"
                units.append((str(unit.get("name", unit.get("unit", "?"))), str(unit.get("unit", "")), state))
            self.units = units

        self._tailscale()
        self._icloud()
        if self.config.get("claude") and time.time() - self.usage_at > 55:
            self._usage()

        probes = {}
        for check in autodetect.live_checks(self.config.get("checks")):
            name = str(check.get("name", "CHECK"))
            key = str(check.get("container") or name)
            if check.get("url"):
                ok, code, ms = http_probe(str(check["url"]), str(check.get("host_header", "")))
                probes[key] = (ok, code, ms, name)
            elif check.get("tcp_host") and check.get("tcp_port"):
                ms = tcp_latency(str(check["tcp_host"]), int(check["tcp_port"]))
                probes[key] = (ms is not None, 0, ms, name)
        self.probes = probes

    def collect_fast(self, tick: int):
        now = time.monotonic()
        s = Snapshot(hostname=self.hostname, kernel=self.kernel, cores=self.cores)

        s.per_core = psutil.cpu_percent(percpu=True)
        s.cpu = sum(s.per_core) / max(1, len(s.per_core))
        vm = psutil.virtual_memory()
        s.ram, s.ram_used, s.ram_total = vm.percent, vm.total - vm.available, vm.total
        sw = psutil.swap_memory()
        s.swap, s.swap_used, s.swap_total = sw.percent, sw.used, sw.total
        try:
            s.load = os.getloadavg()
        except OSError:
            s.load = (0.0, 0.0, 0.0)
        s.uptime_s = int(time.time() - psutil.boot_time())
        s.procs = len(psutil.pids())
        if tick % 2 == 0 or self.snap.temperature is None:
            s.temperature = get_temperature()
        else:
            s.temperature = self.snap.temperature

        if tick % 15 == 0:
            self.ip = get_local_ip()
            self.iface = iface_for_ip(self.ip)
        s.ip, s.iface = self.ip, self.iface

        for mount in self.config.get("mounts", ["/"]):
            if not os.path.exists(mount):
                continue
            try:
                du = psutil.disk_usage(mount)
                s.disks.append((mount, du.percent, du.used, du.total))
            except Exception:
                pass

        rx, tx = self._net_counters()
        dt = max(0.001, now - self.last_net_at)
        s.down = max(0.0, (rx - self.last_net[0]) / dt)
        s.up = max(0.0, (tx - self.last_net[1]) / dt)
        self.last_net, self.last_net_at = (rx, tx), now

        s.net4 = self.net_fails["v4"] < 3
        s.net6 = (self.net_fails["v6"] < 3) if self.has_v6 else None
        s.lat4, s.lat6 = self.net_lat["v4"], self.net_lat["v6"]
        s.internet = s.net4 or bool(s.net6)
        s.latency = s.lat4 if s.lat4 is not None else s.lat6
        if s.latency is not None:
            self.lat_hist.append(s.latency)

        if tick % 4 == 0:
            self._docker_ps()
        if tick % 2 == 0:
            self._claude()
            self._backup()
        if tick % 5 == 0 or self.plan is None:
            self._plan_limits()

        s.docker_ok = self.docker_ok
        s.containers = self.containers
        s.units = self.units
        s.probes = self.probes
        s.claude = self.claude
        s.backup = self.backup
        s.tailscale = self.tailscale
        s.icloud = self.icloud
        s.usage = self.usage
        s.plan = self.plan
        s.netmap = self.netmapper.result
        s.nettraffic = self.netmapper.traffic

        self.cpu_hist.append(s.cpu)
        self.down_hist.append(s.down)
        self.up_hist.append(s.up)
        self.claude_hist.append(s.claude.cpu if s.claude.running else 0.0)
        s.cpu_hist = list(self.cpu_hist)
        s.down_hist = list(self.down_hist)
        s.up_hist = list(self.up_hist)
        s.lat_hist = list(self.lat_hist)
        s.claude_hist = list(self.claude_hist)

        self._detect_events(s)
        s.flagged = set(self.flagged)
        self.snap = s

    # --- event detection

    def _alert(self, key: str, active: bool, on_text: str, off_text: str):
        before = self.alert_state.get(key, False)
        if active and not before:
            self.emit("ALERT", on_text, RED)
        elif before and not active:
            self.emit("OK", off_text, G5)
        self.alert_state[key] = active

    def _detect_events(self, s: Snapshot):
        if s.docker_ok:
            current = {c.name: (c.state, c.health) for c in s.containers}
            if self.prev_containers is not None:
                for name, (state, health) in current.items():
                    old = self.prev_containers.get(name)
                    if old is None:
                        self.emit("DOCKER", f"{name} created ({state})", CYAN)
                    elif old[0] != state:
                        style = G5 if state == "running" else RED
                        self.emit("DOCKER", f"{name} {old[0]} -> {state}", style)
                        if state == "running":
                            self.flagged.discard(name)
                        else:
                            self.flagged.add(name)
                    elif old[1] != health and health:
                        style = G5 if health == "healthy" else AMBER if health == "starting" else RED
                        self.emit("DOCKER", f"{name} health: {health}", style)
                for name in self.prev_containers:
                    if name not in current:
                        self.emit("DOCKER", f"{name} removed", AMBER)
                        self.flagged.discard(name)
            self.prev_containers = current

        if s.units:
            current_units = {label: state for label, _, state in s.units}
            if self.prev_units is not None:
                for label, state in current_units.items():
                    old = self.prev_units.get(label)
                    if old and old != state:
                        self.emit("SYSTEMD", f"{label.lower()} {old} -> {state}", G5 if state == "active" else RED)
            self.prev_units = current_units

        for key, (ok, code, _, name) in s.probes.items():
            old = self.prev_probes.get(key)
            if old is not None and old != ok:
                if ok:
                    self.emit("HTTP", f"{name.lower()} endpoint back ({code})", G5)
                else:
                    self.emit("HTTP", f"{name.lower()} endpoint not responding", RED)
            self.prev_probes[key] = ok

        c = s.claude
        if self.prev_sessions is not None and c.sessions != self.prev_sessions:
            if c.sessions > self.prev_sessions:
                self.emit("CLAUDE", f"session started ({c.sessions} active)", CYAN)
            else:
                self.emit("CLAUDE", f"session ended ({c.sessions} active)", CYAN)
        self.prev_sessions = c.sessions
        now = time.monotonic()
        if c.working:
            if now - self.claude_idle_since > 30:
                self.emit("CLAUDE", "agent is working", CYAN)
            self.claude_idle_since = now

        if s.icloud is not None:
            ic_status = str(s.icloud.get("status", ""))
            if self.prev_icloud_status is not None and ic_status != self.prev_icloud_status:
                text, style = {
                    "downloading": ("icloud: baixando fotos", CYAN),
                    "idle": ("icloud: tudo baixado, aguardando novidades", G5),
                    "paused": ("icloud: download pausado", AMBER),
                    "login": ("icloud: precisa de novo login (porta 8090)", RED),
                }.get(ic_status, (f"icloud: {ic_status}", G4))
                self.emit("ICLOUD", text, style)
            self.prev_icloud_status = ic_status

        if s.tailscale is not None:
            ts_state = str(s.tailscale.get("state", ""))
            if self.prev_tailscale is not None and ts_state != self.prev_tailscale:
                if ts_state == "Running":
                    self.emit("NET", f"tailscale connected ({s.tailscale.get('ip', '')})", G5)
                elif ts_state == "NeedsLogin":
                    self.emit("NET", "tailscale logged out - remote access lost", RED)
                else:
                    self.emit("NET", f"tailscale {ts_state.lower()}", AMBER)
            self.prev_tailscale = ts_state

        if s.backup is not None:
            status = str(s.backup.get("status", ""))
            if self.prev_backup_status is not None and status != self.prev_backup_status:
                labels = {
                    "running": ("backup started", CYAN), "done": ("backup complete, drive in sync", G5),
                    "paused": ("backup paused", AMBER), "limit": ("daily upload limit reached", AMBER),
                    "error": ("backup finished with errors", RED), "stalled": ("backup process stopped responding", RED),
                    "idle": ("backup waiting", G4),
                }
                text, style = labels.get(status, (f"backup {status}", G4))
                self.emit("BACKUP", text, style)
            self.prev_backup_status = status

        for fam, up in (("IPv4", s.net4), ("IPv6", s.net6)):
            if up is None:
                continue
            old = self.prev_net.get(fam)
            if old is not None and old != up:
                if up:
                    self.emit("NET", f"internet {fam} restored", G5)
                else:
                    self.emit("NET", f"internet {fam} lost", RED)
            self.prev_net[fam] = up

        th = self.thresholds
        recent_cpu = list(self.cpu_hist)[-5:]
        self._alert("cpu", len(recent_cpu) == 5 and min(recent_cpu) >= float(th.get("cpu", 85)),
                    f"cpu sustained above {th.get('cpu', 85)}%", "cpu back to normal")
        self._alert("ram", s.ram >= float(th.get("ram", 85)),
                    f"memory at {s.ram:.0f}%", "memory back to normal")
        if s.temperature is not None:
            self._alert("temp", s.temperature >= float(th.get("temperature", 80)),
                        f"temperature at {s.temperature:.0f}°C", "temperature back to normal")
        for mount, pct, _, _ in s.disks:
            self._alert(f"disk{mount}", pct >= float(th.get("disk", 90)),
                        f"disk {mount} at {pct:.0f}%", f"disk {mount} back to normal")

        if now - self.last_probe_event >= 9:
            self.last_probe_event = now
            self._probe_line(s)

    def _probe_line(self, s: Snapshot):
        running = sum(1 for c in s.containers if c.state == "running")
        lines = [
            f"cpu {s.cpu:.1f}%  load {s.load[0]:.2f}  procs {s.procs}"
            + (f"  temp {s.temperature:.0f}°C" if s.temperature is not None else ""),
            f"net down {fmt_rate(s.down)}  up {fmt_rate(s.up)}  "
            + "  ".join(f"{fam} " + (f"{lat:.0f} ms" if up and lat is not None else "down")
                        for fam, up, lat in (("v4", s.net4, s.lat4), ("v6", s.net6, s.lat6)) if up is not None),
            f"docker {running}/{len(s.containers)} containers running",
            f"memory {fmt_size(s.ram_used)} / {fmt_size(s.ram_total)}  swap {s.swap:.0f}%",
        ]
        for mount, pct, used, total in s.disks:
            lines.append(f"disk {mount} {pct:.0f}% used  {fmt_size(total - used)} free")
        if s.claude.running:
            state = "working" if s.claude.working else "idle"
            lines.append(f"claude code {state}  sessions {s.claude.sessions}  mem {fmt_size(s.claude.rss)}")
        elif self.config.get("claude"):
            lines.append("claude code not running")
        text = lines[self.probe_cycle % len(lines)]
        self.probe_cycle += 1
        self.emit("PROBE", text, G3)

    # --- threads

    def _fast_loop(self):
        tick = 1
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.collect_fast(tick)
            except Exception as exc:
                self.emit("ERROR", f"collector: {exc}"[:80], RED)
            tick += 1
            self.stop.wait(max(0.05, 1.0 - (time.monotonic() - started)))

    def _slow_loop(self):
        while not self.stop.is_set():
            try:
                self._slow_once()
            except Exception as exc:
                self.emit("ERROR", f"slow collector: {exc}"[:80], RED)
            self.stop.wait(12.0)

    def prime(self):
        self._docker_ps()
        self._claude()
        self._net_probe()
        units_cfg = self.config.get("systemd_units", [])
        if units_cfg:
            names = [str(u.get("unit", "")) for u in units_cfg]
            _, out = run_cmd(["systemctl", "is-active", *names], timeout=3.0)
            states = out.splitlines()
            self.units = [
                (str(u.get("name", u.get("unit", "?"))), str(u.get("unit", "")),
                 states[i].strip() if i < len(states) else "unknown")
                for i, u in enumerate(units_cfg)
            ]
        self._backup()
        self._tailscale()
        self._usage()
        time.sleep(0.15)
        self.collect_fast(0)

    def _net_probe(self):
        self.has_v6 = any(
            a.family == socket.AF_INET6 and not a.address.startswith("fe80")
            for addrs in psutil.net_if_addrs().values() for a in addrs
        )
        for fam, host in (("v4", self.ping_host), ("v6", self.ping_host6)):
            if fam == "v6" and not self.has_v6:
                continue
            # Port 443 is open on networks that block outbound DNS (53).
            ms = tcp_latency(host, 443, timeout=3.0)
            if ms is None:
                self.net_fails[fam] += 1
                if self.net_fails[fam] >= 3:
                    self.net_lat[fam] = None
            else:
                self.net_fails[fam] = 0
                self.net_lat[fam] = ms

    def _net_loop(self):
        while not self.stop.is_set():
            try:
                self._net_probe()
            except Exception:
                pass
            self.stop.wait(2.0)

    def _netmap_loop(self):
        interval = max(30.0, float((self.config.get("netmap") or {}).get("interval", 60)))
        while not self.stop.is_set():
            try:
                self.netmapper.scan()
            except Exception as exc:
                self.emit("ERROR", f"netmap: {exc}"[:80], RED)
            self.stop.wait(interval)

    def _traffic_loop(self):
        while not self.stop.is_set():
            try:
                self.netmapper.sample_traffic()
            except Exception as exc:
                self.emit("ERROR", f"traffic: {exc}"[:80], RED)
            self.stop.wait(3.0)

    # --- export for the web hub (server-hub reads this file)

    EVENT_LEVEL = {RED: "critical", AMBER: "warning", G5: "good", CYAN: "info"}

    def export_snapshot(self, path: Path):
        s = self.snap
        data = asdict(s)
        data["flagged"] = sorted(s.flagged)
        data["events"] = [
            {"t": e.wall, "tag": e.tag, "text": e.text, "level": self.EVENT_LEVEL.get(e.style, "neutral")}
            for e in list(self.events)[-80:]
        ]
        data["exported_at"] = time.time()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, default=str, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _export_loop(self):
        path = Path(os.path.expanduser(str(self.config.get("export_path") or "~/.cache/server-hub/snapshot.json")))
        path.parent.mkdir(parents=True, exist_ok=True)
        while not self.stop.is_set():
            try:
                self.export_snapshot(path)
            except Exception as exc:
                self.emit("ERROR", f"export: {exc}"[:80], RED)
                self.stop.wait(10.0)
            self.stop.wait(1.0)

    def start(self):
        threading.Thread(target=self._export_loop, daemon=True).start()
        threading.Thread(target=self._net_loop, daemon=True).start()
        threading.Thread(target=self._traffic_loop, daemon=True).start()
        threading.Thread(target=self._netmap_loop, daemon=True).start()
        threading.Thread(target=self._fast_loop, daemon=True).start()
        threading.Thread(target=self._slow_loop, daemon=True).start()


# -------------------------------------------------------------------- canvas

class Canvas:
    __slots__ = ("w", "h", "chars", "styles")

    def __init__(self, width: int, height: int):
        self.w = max(1, width)
        self.h = max(1, height)
        self.chars = [[" "] * self.w for _ in range(self.h)]
        self.styles = [[NONE] * self.w for _ in range(self.h)]

    def put(self, x: int, y: int, text: str, style: int = G3):
        if y < 0 or y >= self.h or not text:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        if x >= self.w:
            return
        text = text[: self.w - x]
        n = len(text)
        self.chars[y][x:x + n] = text
        self.styles[y][x:x + n] = [style] * n

    def put_sparse(self, x: int, y: int, text: str, style: int = G3):
        if y < 0 or y >= self.h:
            return
        row_c, row_s = self.chars[y], self.styles[y]
        for i, ch in enumerate(text):
            xx = x + i
            if ch != " " and 0 <= xx < self.w:
                row_c[xx] = ch
                row_s[xx] = style

    def style_at(self, x: int, y: int, style: int):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.styles[y][x] = style

    def fill(self, x: int, y: int, w: int, h: int):
        x0, x1 = max(0, x), min(self.w, x + w)
        if x1 <= x0:
            return
        for yy in range(max(0, y), min(self.h, y + h)):
            self.chars[yy][x0:x1] = [" "] * (x1 - x0)
            self.styles[yy][x0:x1] = [NONE] * (x1 - x0)


    def box(self, x: int, y: int, w: int, h: int, title: str = "", right: str = "",
            style: int = G2, title_style: int = G5, clear: bool = True, double: bool = False):
        if w < 4 or h < 2:
            return
        if clear:
            self.fill(x + 1, y + 1, w - 2, h - 2)
        if double:
            tl, tr, bl, br, hz, vt = "╔", "╗", "╚", "╝", "═", "║"
        else:
            tl, tr, bl, br, hz, vt = "╭", "╮", "╰", "╯", "─", "│"
        self.put(x, y, tl + hz * (w - 2) + tr, style)
        self.put(x, y + h - 1, bl + hz * (w - 2) + br, style)
        right_x = x + w - 1
        for yy in range(max(0, y + 1), min(self.h, y + h - 1)):
            rc, rs = self.chars[yy], self.styles[yy]
            if 0 <= x < self.w:
                rc[x], rs[x] = vt, style
            if 0 <= right_x < self.w:
                rc[right_x], rs[right_x] = vt, style
        if title:
            label = f" {title} "[: max(0, w - 6)]
            self.put(x + 2, y, label, title_style)
        if right:
            label = f" {right} "
            if len(label) < w - 8 - len(title):
                self.put(x + w - len(label) - 2, y, label, G3)

    def pulse(self, x: int, y: int, w: int, h: int, pos: float, length: int = 7):
        """Light segment travelling around a box border."""
        per = 2 * (w - 1) + 2 * (h - 1)
        if per <= 0:
            return
        for k in range(length):
            p = int(pos - k) % per
            if p < w - 1:
                px, py = x + p, y
            elif p < (w - 1) + (h - 1):
                px, py = x + w - 1, y + p - (w - 1)
            elif p < 2 * (w - 1) + (h - 1):
                px, py = x + w - 1 - (p - (w - 1) - (h - 1)), y + h - 1
            else:
                px, py = x, y + h - 1 - (p - 2 * (w - 1) - (h - 1))
            if 0 <= px < self.w and 0 <= py < self.h and self.chars[py][px] in BORDER_CHARS:
                self.styles[py][px] = G6 if k == 0 else G5 if k < 3 else G4


class Screen:
    """Turns canvases into terminal output, sending only the cells that changed."""

    def __init__(self, sgr: dict[int, str]):
        self.sgr = {k: f"\x1b[{v}m" if v == "0" else f"\x1b[0;{v}m" for k, v in sgr.items()}
        self.prev: Canvas | None = None

    def invalidate(self):
        self.prev = None

    def plain(self, c: Canvas) -> str:
        out = []
        for y in range(c.h):
            cur = None
            for ch, st in zip(c.chars[y], c.styles[y]):
                if st != cur:
                    out.append(self.sgr.get(st, self.sgr[G3]))
                    cur = st
                out.append(ch)
            out.append(RESET)
            if y < c.h - 1:
                out.append("\n")
        return "".join(out)

    def frame(self, c: Canvas) -> str:
        prev = self.prev
        self.prev = c
        if prev is None or prev.w != c.w or prev.h != c.h:
            out = [RESET, CLEAR]
            for y in range(c.h):
                out.append(f"\x1b[{y + 1};1H")
                cur = None
                for ch, st in zip(c.chars[y], c.styles[y]):
                    if st != cur:
                        out.append(self.sgr.get(st, self.sgr[G3]))
                        cur = st
                    out.append(ch)
            return "".join(out)

        out = []
        cur = None
        sgr = self.sgr
        for y in range(c.h):
            rc, rs = c.chars[y], c.styles[y]
            pc, ps = prev.chars[y], prev.styles[y]
            if rc == pc and rs == ps:
                continue
            changed = [i for i, (a, b, sa, sb) in enumerate(zip(rc, pc, rs, ps)) if a != b or sa != sb]
            runs = []
            start = end = changed[0]
            for i in changed[1:]:
                if i - end > 5:
                    runs.append((start, end))
                    start = i
                end = i
            runs.append((start, end))
            for start, end in runs:
                out.append(f"\x1b[{y + 1};{start + 1}H")
                for i in range(start, end + 1):
                    st = rs[i]
                    if st != cur:
                        out.append(sgr.get(st, sgr[G3]))
                        cur = st
                    out.append(rc[i])
        return "".join(out)


# ----------------------------------------------------------------- animation

class MatrixRain:
    def __init__(self, width: int, height: int, density: float):
        self.width = width
        self.height = height
        self.density = max(0.05, min(1.0, density))
        self.columns: list[dict[str, Any]] = []
        self._build()

    def _new_column(self, x: int, initial: bool) -> dict[str, Any]:
        return {
            "x": x,
            "y": random.uniform(-self.height, self.height) if initial else random.uniform(-20, -1),
            "speed": random.uniform(0.3, 1.25),
            "trail": random.randint(6, 18),
            "glyphs": [random.choice(MATRIX_CHARS) for _ in range(self.height + 20)],
        }

    def _build(self):
        self.columns = [
            self._new_column(x, True)
            for x in range(0, self.width, 2)
            if random.random() <= self.density
        ]

    def resize(self, width: int, height: int):
        if width != self.width or height != self.height:
            self.width = width
            self.height = height
            self._build()

    def update(self):
        for i, col in enumerate(self.columns):
            col["y"] += col["speed"]
            glyphs = col["glyphs"]
            for _ in range(2):
                glyphs[random.randrange(len(glyphs))] = random.choice(MATRIX_CHARS)
            if col["y"] - col["trail"] > self.height:
                self.columns[i] = self._new_column(col["x"], False)

    def draw(self, canvas: Canvas, dim_rect: tuple[int, int, int, int] | None = None):
        h = canvas.h
        dx0, dy0, dx1, dy1 = dim_rect if dim_rect else (-1, -1, -1, -1)
        for col in self.columns:
            x = col["x"]
            if x >= canvas.w:
                continue
            dim_col = dx0 <= x < dx1
            head = int(col["y"])
            trail = col["trail"]
            glyphs = col["glyphs"]
            for t in range(trail):
                y = head - t
                if not (0 <= y < h):
                    continue
                ratio = t / trail
                if t == 0:
                    style = G6
                elif ratio < 0.15:
                    style = G5
                elif ratio < 0.35:
                    style = G4
                elif ratio < 0.6:
                    style = G3
                elif ratio < 0.8:
                    style = G2
                else:
                    style = G1
                if dim_col and dy0 <= y < dy1:
                    style = DIM_LUT[style]
                canvas.chars[y][x] = glyphs[y % len(glyphs)]
                canvas.styles[y][x] = style


class Portrait:
    """ASCII portrait shaded by density, with decode, scan, shimmer and glitch."""

    SHADE = {" ": 0, ".": 1, "`": 1, "'": 1, ",": 1, ":": 2, ";": 2, "-": 2, "~": 3, "=": 3,
             "+": 4, "*": 4, "x": 4, "#": 5, "%": 6, "@": 6, "&": 6, "$": 6}
    SHADE_STYLE = (NONE, G1, G2, G3, G4, G5, G6, WHITE, WHITE)

    def __init__(self, path: Path, fallback_text: str = ""):
        self.path = path
        # Shown (as big shaded letters) when there is no image file.
        self.fallback_text = fallback_text
        self.cache_key: tuple[int, int] | None = None
        self.lines: list[str] = []
        self.rows: list[dict[str, Any]] = []
        self.art_w = 0
        self.reveal_at: list[list[float]] = []

    def natural_size(self) -> tuple[int, int]:
        if self.path.suffix.lower() in {".txt", ".asc"} and self.path.exists():
            raw = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            return (max((len(r) for r in raw), default=0), len(raw))
        return (100, 40)

    def _banner_image(self) -> Image.Image:
        """Server name in big letters with a soft glow, used when no portrait is configured."""
        from PIL import ImageDraw, ImageFilter, ImageFont
        words = [w for w in re.split(r"[\s._-]+", self.fallback_text.upper()) if w][:3] or ["MATRIX"]
        try:
            font = ImageFont.load_default(size=96)
        except TypeError:  # Pillow < 10.1 has only a tiny bitmap font
            font = ImageFont.load_default()
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        boxes = [probe.textbbox((0, 0), w, font=font) for w in words]
        tw = max(b[2] - b[0] for b in boxes) + 40
        line_h = max(b[3] - b[1] for b in boxes) + 30
        img = Image.new("L", (tw, line_h * len(words) + 20), 0)
        draw = ImageDraw.Draw(img)
        for i, (w, b) in enumerate(zip(words, boxes)):
            draw.text(((tw - (b[2] - b[0])) // 2 - b[0], 10 + i * line_h - b[1]), w, fill=255, font=font)
        glow = img.filter(ImageFilter.GaussianBlur(6)).point(lambda v: v // 2)
        return Image.composite(img, glow, img)

    def _render_image(self, max_w: int, max_h: int) -> list[str]:
        if max_w < 4 or max_h < 2:
            return ["IMAGE NOT FOUND"]
        if not self.path.exists() and not self.fallback_text:
            return ["IMAGE NOT FOUND"]

        if self.path.suffix.lower() in {".txt", ".asc"} and self.path.exists():
            raw = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            if not raw:
                return ["ASCII FILE EMPTY"]
            source_w = max(len(line) for line in raw)
            step_x = max(1, (source_w + max_w - 1) // max_w)
            step_y = max(1, (len(raw) + max_h - 1) // max_h)
            return [row[::step_x][:max_w] for row in raw[::step_y][:max_h]]

        if self.path.exists():
            image = Image.open(self.path)
            image = ImageOps.exif_transpose(image).convert("L")
        else:
            image = self._banner_image()
        extrema = image.getextrema()
        if extrema[1] - extrema[0] > 20:
            image = ImageOps.autocontrast(image)
        # Terminal characters are roughly twice as tall as they are wide.
        aspect = image.width / max(1, image.height)
        target_w = max_w
        target_h = max(1, int((target_w / aspect) * 0.48))
        if target_h > max_h:
            target_h = max_h
            target_w = max(1, int(target_h * aspect / 0.48))
        target_w = min(target_w, max_w)
        image = image.resize((target_w, target_h))
        ramp = " .:-=+*#%@"
        pixels = image.tobytes()
        lines = []
        for y in range(target_h):
            row = []
            for x in range(target_w):
                value = pixels[y * target_w + x]
                row.append(" " if value < 16 else ramp[int((value / 255) * (len(ramp) - 1))])
            lines.append("".join(row).rstrip())
        return lines

    def _prepare(self, max_w: int, max_h: int):
        key = (max_w, max_h)
        if key == self.cache_key:
            return
        try:
            self.lines = self._render_image(max_w, max_h)
        except Exception as exc:
            self.lines = [f"IMAGE ERROR: {exc}"[:max_w]]
        self.cache_key = key
        self.art_w = max((len(l) for l in self.lines), default=0)
        self.rows = []
        n = max(1, len(self.lines))
        self.reveal_at = []
        for i, line in enumerate(self.lines):
            shades = [self.SHADE.get(ch, 4 if ch != " " else 0) for ch in line]
            runs = []
            start = None
            for j, ch in enumerate(line + " "):
                if ch != " " and start is None:
                    start = j
                elif ch == " " and start is not None:
                    runs.append((start, j))
                    start = None
            styles = [
                [self.SHADE_STYLE[min(len(self.SHADE_STYLE) - 1, s + boost)] if s else NONE for s in shades]
                for boost in range(3)
            ]
            self.rows.append({"chars": list(line), "shades": shades, "runs": runs, "styles": styles})
            self.reveal_at.append([0.2 + (i / n) * 1.6 + random.random() * 1.0 for _ in line])

    REVEAL_END = 3.2

    def draw(self, c: Canvas, x: int, y: int, w: int, h: int, t: float,
             glitch_rows: dict[int, int] | None = None) -> float:
        """Draws the portrait and returns the scan progress (0..1)."""
        self._prepare(max(1, w), max(1, h))
        ah = len(self.rows)
        ox = x + max(0, (w - self.art_w) // 2)
        oy = y + max(0, (h - ah) // 2)

        if t < self.REVEAL_END:
            for i, row in enumerate(self.rows):
                yy = oy + i
                if not (0 <= yy < c.h):
                    continue
                rc, rs = c.chars[yy], c.styles[yy]
                times = self.reveal_at[i]
                base = row["styles"][0]
                for j, ch in enumerate(row["chars"]):
                    if ch == " ":
                        continue
                    xx = ox + j
                    if not (0 <= xx < c.w):
                        continue
                    rt = times[j]
                    if t < rt - 0.55:
                        continue
                    if t < rt:
                        rc[xx] = random.choice(SCRAMBLE_CHARS)
                        rs[xx] = G4
                    elif t < rt + 0.14:
                        rc[xx] = ch
                        rs[xx] = WHITE
                    else:
                        rc[xx] = ch
                        rs[xx] = base[j]
            return 0.0

        period = ah + 14
        scan = (t * 7.0) % period - 7
        shimmer_period = self.art_w + 90
        for i, row in enumerate(self.rows):
            yy = oy + i
            if not (0 <= yy < c.h):
                continue
            d = abs(i - scan)
            boost = 2 if d < 0.6 else 1 if d < 2.6 else 0
            chars, styles = row["chars"], row["styles"][boost]
            rc, rs = c.chars[yy], c.styles[yy]
            shift = 0
            if glitch_rows and i in glitch_rows:
                shift = glitch_rows[i]
            if boost == 2:
                for xx in range(x, min(c.w, x + w)):
                    if rc[xx] == " " or rs[xx] in (G0, G1):
                        rc[xx] = "·"
                        rs[xx] = G2
            for a, b in row["runs"]:
                xa, xb = ox + a + shift, ox + b + shift
                ca, cb = max(xa, x), min(xb, x + w, c.w)
                if cb <= ca:
                    continue
                rc[ca:cb] = chars[a + (ca - xa):b - (xb - cb)]
                rs[ca:cb] = styles[a + (ca - xa):b - (xb - cb)]
            if shift:
                # Chromatic ghost of the torn row plus random noise.
                for a, b in row["runs"]:
                    for j in range(a, b):
                        if random.random() < 0.18:
                            xx = ox + j
                            if x <= xx < min(c.w, x + w):
                                rc[xx] = random.choice(GLITCH_CHARS)
                                rs[xx] = CYAN if random.random() < 0.6 else WHITE
            else:
                # Diagonal shimmer band sweeping across the face.
                cx = int((t * 26 - i * 1.3) % shimmer_period) - 40
                if 0 <= cx + 4 and cx - 4 < self.art_w and boost < 2:
                    brighter = row["styles"][boost + 1]
                    for j in range(max(0, cx - 4), min(len(chars), cx + 5)):
                        if chars[j] != " ":
                            xx = ox + j
                            if x <= xx < min(c.w, x + w):
                                rs[xx] = brighter[j]
        return max(0.0, min(1.0, scan / max(1, ah)))


# ----------------------------------------------------------------- dashboard

# ------------------------------------------------------------ settings menu

NETCFG_HELPER = "/usr/local/sbin/matrix-netcfg"
RATE_RE = re.compile(r"^(off|\d+(\.\d+)?[KMG]?)$", re.I)
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def read_ipv4(iface: str) -> dict[str, str]:
    """Current IPv4 settings of an interface, without root."""
    info = {"mode": "FIXO", "ip": "", "prefix": "", "gateway": "", "dns": "", "mac": "", "ipv6": ""}
    _, out = run_cmd(["ip", "-o", "addr", "show", "dev", iface])
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", out)
    if m:
        info["ip"], info["prefix"] = m.group(1), m.group(2)
    m6 = re.search(r"inet6 ([0-9a-f:]+)/\d+ scope global", out)
    if m6:
        info["ipv6"] = m6.group(1)
    _, route = run_cmd(["ip", "-4", "route", "show", "default", "dev", iface])
    gw = re.search(r"via (\S+)", route)
    info["gateway"] = gw.group(1) if gw else ""
    if "proto dhcp" in route:
        info["mode"] = "DHCP"
    _, dns = run_cmd(["resolvectl", "dns", iface])
    servers = [x for x in dns.split(":", 1)[-1].split() if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", x)]
    info["dns"] = ", ".join(servers)
    try:
        info["mac"] = Path(f"/sys/class/net/{iface}/address").read_text().strip()
    except OSError:
        pass
    return info


class SettingsMenu:
    """Keyboard-driven settings screen (hotkey C): network, backup, panel, containers."""

    def __init__(self, dash: "Dashboard"):
        self.d = dash
        self.active = False
        self.items: list[dict[str, Any]] = []
        self.values: dict[str, str] = {}
        self.sysinfo: dict[str, str] = {}
        self.cursor = 0
        self.scroll = 0
        self.edit_buffer: str | None = None
        self.message: tuple[str, int] | None = None
        self.net_dirty = False
        self.has_backup = False
        self.busy = ""

    # --- data

    def iface(self) -> str:
        iface = str(self.d.config.get("network_interface") or self.d.collector.snap.iface or "-")
        return iface if iface not in ("-", "auto") else autodetect.default_iface()

    def backup_config_path(self) -> Path | None:
        return self.d.backup_dir / "backup.json" if self.d.backup_dir else None

    def load(self):
        net = read_ipv4(self.iface())
        self.sysinfo = net
        v = self.values
        v["net_mode"] = net["mode"]
        v["net_ip"] = net["ip"]
        v["net_prefix"] = net["prefix"] or "24"
        v["net_gateway"] = net["gateway"]
        v["net_dns"] = net["dns"] or net["gateway"]
        self.net_dirty = False

        cfg = self.d.config
        v["operator"] = self.d.operator
        v["fps"] = str(self.d.fps)
        v["density"] = f"{float(cfg.get('matrix_density', 0.45)):.2f}"
        screen = cfg.get("screen_off") or {}
        v["screen_off"] = "LIGADO" if screen.get("enabled") else "DESLIGADO"
        v["screen_from"] = str(screen.get("from", "00:00"))
        v["screen_to"] = str(screen.get("to", "07:00"))
        th = cfg.get("thresholds", {})
        for key in ("cpu", "ram", "disk", "temperature"):
            v[f"th_{key}"] = str(th.get(key, DEFAULT_CONFIG["thresholds"][key]))

        path = self.backup_config_path()
        bcfg = {}
        if path and path.exists():
            try:
                bcfg = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                bcfg = {}
        slots = []
        for part in str(bcfg.get("bwlimit", "08:00,3M 23:00,8M")).split():
            if "," in part:
                slots.append(part.split(",", 1))
        if len(slots) >= 2:
            (v["bk_day_from"], v["bk_day_rate"]), (v["bk_night_from"], v["bk_night_rate"]) = slots[0], slots[1]
        else:
            v["bk_day_from"], v["bk_day_rate"], v["bk_night_from"], v["bk_night_rate"] = "08:00", "3M", "23:00", "8M"
        v["bk_daily"] = str(bcfg.get("daily_sync_at", "02:00"))
        v["ic_limit"] = str((cfg.get("icloud") or {}).get("limit", "off"))
        self.has_backup = bool(path and path.exists())

    def build(self):
        items: list[dict[str, Any]] = []

        def header(label: str, info: str = ""):
            items.append({"kind": "header", "label": label, "info": info})

        def field(key: str, label: str, hint: str, validate, save=None, kind: str = "text", options=None):
            items.append({"kind": kind, "key": key, "label": label, "hint": hint,
                          "validate": validate, "save": save, "options": options})

        def action(label: str, hint: str, fn, style: int = G5):
            items.append({"kind": "action", "label": label, "hint": hint, "action": fn, "style": style})

        net_now = self.sysinfo
        header("REDE", f"atual: {net_now['ip']}/{net_now['prefix']} via {net_now['gateway'] or '-'} ({net_now['mode']})")
        if not os.path.exists(NETCFG_HELPER):
            items.append({"kind": "header", "label": "  só leitura",
                          "info": "para editar a rede daqui: sudo ./install_hotkeys_sudo.sh (Ubuntu/netplan)"})
        else:
            field("net_mode", "Modo IPv4", "FIXO = IP definido aqui · DHCP = o roteador escolhe", None,
                  self.mark_net, "choice", ["FIXO", "DHCP"])
            if self.values.get("net_mode") == "FIXO":
                field("net_ip", "Endereço IP", "ex.: 192.168.1.100", self.valid_ipv4, self.mark_net)
                field("net_prefix", "Prefixo (máscara)", "24 = 255.255.255.0", self.valid_prefix, self.mark_net)
                field("net_gateway", "Gateway (roteador)", "ex.: 192.168.1.1", self.valid_ipv4, self.mark_net)
                field("net_dns", "DNS", "separe com vírgula · ex.: 192.168.1.1, 1.1.1.1", self.valid_dns, self.mark_net)
            action("Aplicar configuração de rede", "pede confirmação · se o roteador não responder, volta sozinho",
                   self.apply_network, AMBER)

        if self.has_backup:
            header("BACKUP GOOGLE DRIVE", "vale na hora, sem reiniciar o backup")
            field("bk_day_rate", "Limite de dia", "ex.: 3M (MB/s) · off = sem limite", self.valid_rate, self.save_backup)
            field("bk_day_from", "Dia começa às", "HH:MM", self.valid_time, self.save_backup)
            field("bk_night_rate", "Limite de noite", "ex.: 8M (MB/s) · off = sem limite", self.valid_rate, self.save_backup)
            field("bk_night_from", "Noite começa às", "HH:MM", self.valid_time, self.save_backup)
            field("bk_daily", "Sincronização diária às", "HH:MM · depois da cópia inicial", self.valid_time, self.save_backup)

        if (self.d.config.get("icloud") or {}).get("container"):
            ic = self.d.collector.snap.icloud or {}
            header("ICLOUD → SERVIDOR", f"{ic.get('items', 0)}/{ic.get('total', 0) or '?'} itens · {ic.get('status', '-')}")
            field("ic_limit", "Limite de download", "a Apple entrega ~5 MB/s · limite vale na hora",
                  None, self.save_icloud, "choice", ["off", "1M", "2M", "3M", "5M"])
            running = ic.get("state") == "running"
            action("Retomar download" if not running else "Pausar download",
                   "para/liga o container do iCloud (mesmo que a tecla P)", self.toggle_icloud_menu, AMBER)

        header("PAINEL")
        field("operator", "Nome do operador", "aparece no retrato e no boot", self.valid_name, self.save_panel)
        field("fps", "Quadros por segundo", "2 a 30 · menos = menos CPU", self.valid_int(2, 30), self.save_panel)
        field("density", "Densidade da chuva", "0.05 a 1.00", self.valid_float(0.05, 1.0), self.save_panel)
        field("screen_off", "Apagar tela automático", "apaga o monitor no horário abaixo · tecla acende por 5 min",
              None, self.save_panel, "choice", ["DESLIGADO", "LIGADO"])
        field("screen_from", "  apagar às", "HH:MM", self.valid_time, self.save_panel)
        field("screen_to", "  acender às", "HH:MM", self.valid_time, self.save_panel)
        field("th_cpu", "Alerta de CPU (%)", "10 a 100", self.valid_int(10, 100), self.save_panel)
        field("th_ram", "Alerta de RAM (%)", "10 a 100", self.valid_int(10, 100), self.save_panel)
        field("th_disk", "Alerta de disco (%)", "10 a 100", self.valid_int(10, 100), self.save_panel)
        field("th_temperature", "Alerta de temperatura (°C)", "40 a 110", self.valid_int(40, 110), self.save_panel)

        snap = self.d.collector.snap
        header("CONTAINERS", "◂ ▸ escolhe a ação · OK executa")
        policies = self.restart_policies()
        for ct in sorted(snap.containers, key=lambda x: x.name):
            actions = ["reiniciar", "desligar"] if ct.state == "running" else ["ligar"]
            items.append({"kind": "container", "label": ct.name, "state": ct.state, "actions": actions,
                          "choice": 0, "policy": policies.get(ct.name, ""),
                          "hint": (f"◂ ▸ escolhe: {' / '.join(actions)} · OK executa" if len(actions) > 1
                                   else f"OK para {actions[0]} {ct.name}")})

        header("SISTEMA")
        ts = snap.tailscale or {}
        info = [
            ("Hostname", snap.hostname),
            ("Interface", f"{self.iface()} · MAC {net_now.get('mac', '')}"),
            ("IPv4", f"{net_now['ip']}/{net_now['prefix']} via {net_now['gateway'] or '-'}"),
            ("IPv6", net_now.get("ipv6", "") or "-"),
            ("DNS em uso", net_now.get("dns", "") or "-"),
            ("Tailscale", f"{ts.get('ip', '-')} · {ts.get('state', '-')}"
                          + (f" · chave expira em {ts['expiry_days']}d" if ts.get("expiry_days") is not None else "")),
            ("Kernel", snap.kernel),
        ]
        for label, value in info:
            items.append({"kind": "info", "label": label, "value": value})
        self.items = items

    # --- validators (return error text or None)

    @staticmethod
    def valid_ipv4(value: str) -> str | None:
        try:
            ipaddress.IPv4Address(value.strip())
            return None
        except ValueError:
            return "IP inválido"

    @staticmethod
    def valid_prefix(value: str) -> str | None:
        return None if value.isdigit() and 8 <= int(value) <= 30 else "use um número de 8 a 30"

    @staticmethod
    def valid_dns(value: str) -> str | None:
        parts = [x.strip() for x in value.split(",") if x.strip()]
        if not 1 <= len(parts) <= 4:
            return "informe de 1 a 4 servidores"
        for part in parts:
            try:
                ipaddress.ip_address(part)
            except ValueError:
                return f"DNS inválido: {part}"
        return None

    @staticmethod
    def valid_rate(value: str) -> str | None:
        return None if RATE_RE.match(value.strip()) else "use algo como 3M, 500K ou off"

    @staticmethod
    def valid_time(value: str) -> str | None:
        return None if TIME_RE.match(value.strip()) else "use HH:MM, ex.: 23:00"

    @staticmethod
    def valid_name(value: str) -> str | None:
        return None if 1 <= len(value.strip()) <= 20 else "de 1 a 20 caracteres"

    @staticmethod
    def valid_int(lo: int, hi: int):
        return lambda value: None if value.strip().isdigit() and lo <= int(value) <= hi else f"número de {lo} a {hi}"

    @staticmethod
    def valid_float(lo: float, hi: float):
        def check(value: str) -> str | None:
            try:
                return None if lo <= float(value) <= hi else f"número de {lo} a {hi}"
            except ValueError:
                return f"número de {lo} a {hi}"
        return check

    # --- savers

    def save_icloud(self):
        err = self.d.set_icloud_limit(self.values["ic_limit"])
        limit = self.values["ic_limit"]
        self.message = (err, RED) if err else (f"limite do iCloud: {'sem limite' if limit == 'off' else limit + 'B/s'}", G5)

    def toggle_icloud_menu(self):
        self.d.toggle_icloud()
        self.message = ("aguarde alguns segundos...", CYAN)

    def mark_net(self):
        self.net_dirty = True
        self.message = ("rede alterada · use 'Aplicar configuração de rede' para valer", AMBER)

    def save_backup(self):
        path = self.backup_config_path()
        if not path:
            return
        v = self.values
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            cfg["bwlimit"] = (f"{v['bk_day_from']},{v['bk_day_rate'].upper().replace('OFF', 'off')} "
                              f"{v['bk_night_from']},{v['bk_night_rate'].upper().replace('OFF', 'off')}")
            cfg["daily_sync_at"] = v["bk_daily"]
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            self.message = ("backup salvo · limite novo vale em segundos", G5)
            self.d.collector.emit("CONFIG", f"backup: {cfg['bwlimit']} · diário {cfg['daily_sync_at']}", CYAN)
        except (OSError, ValueError) as exc:
            self.message = (f"erro ao salvar: {exc}"[:70], RED)

    def save_panel(self):
        v = self.values
        d = self.d
        cfg = d.config
        d.operator = v["operator"].strip().upper()
        cfg["operator"] = d.operator
        d.fps = int(v["fps"])
        cfg["fps"] = d.fps
        density = float(v["density"])
        if abs(density - float(cfg.get("matrix_density", 0.45))) > 1e-6:
            cfg["matrix_density"] = round(density, 2)
            d.rain = MatrixRain(d.width, d.height, density)
        cfg["screen_off"] = {"enabled": v["screen_off"] == "LIGADO", "from": v["screen_from"], "to": v["screen_to"]}
        for key in ("cpu", "ram", "disk", "temperature"):
            cfg["thresholds"][key] = int(v[f"th_{key}"])
        try:
            raw = json.loads(d.config_path.read_text(encoding="utf-8")) if d.config_path.exists() else {}
            for key in ("operator", "fps", "matrix_density", "screen_off", "thresholds"):
                if key in cfg:
                    raw[key] = cfg[key]
            tmp = d.config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, d.config_path)
            self.message = ("painel salvo", G5)
        except (OSError, ValueError) as exc:
            self.message = (f"erro ao salvar: {exc}"[:70], RED)

    # --- actions

    def run_bg(self, label: str, fn):
        if self.busy:
            self.message = (f"aguarde: {self.busy}", AMBER)
            return
        self.busy = label
        self.message = (f"{label}...", CYAN)

        def worker():
            try:
                fn()
            finally:
                self.busy = ""
        threading.Thread(target=worker, daemon=True).start()

    def apply_network(self):
        v = self.values
        if v["net_mode"] == "DHCP":
            args = ["dhcp"]
            lines = ["Modo: IP automático (DHCP)", "O IP do servidor passa a ser escolhido pelo roteador."]
        else:
            for key, check in (("net_ip", self.valid_ipv4), ("net_prefix", self.valid_prefix),
                               ("net_gateway", self.valid_ipv4), ("net_dns", self.valid_dns)):
                err = check(v[key])
                if err:
                    self.message = (f"{err} ({key.replace('net_', '')})", RED)
                    return
            iface = ipaddress.IPv4Interface(f"{v['net_ip']}/{v['net_prefix']}")
            if ipaddress.IPv4Address(v["net_gateway"]) not in iface.network:
                self.message = (f"gateway fora da rede {iface.network}", RED)
                return
            dns = ",".join(x.strip() for x in v["net_dns"].split(",") if x.strip())
            args = ["static", str(iface), v["net_gateway"], dns]
            lines = [f"IP fixo: {iface}", f"Gateway: {v['net_gateway']}   DNS: {dns}"]
        lines += ["A rede pisca por alguns segundos (Tailscale reconecta).",
                  "Se o roteador não responder em 30 s, volta a config anterior."]
        self.d.confirm("APLICAR REDE", lines, lambda: self.run_bg("aplicando rede", lambda: self._apply_network(args)), 15)

    def _apply_network(self, args: list[str]):
        try:
            result = subprocess.run(["sudo", "-n", NETCFG_HELPER, *args],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
        except Exception as exc:
            self.message = (f"falhou: {exc}"[:70], RED)
            return
        if "password is required" in result.stderr or "not allowed" in result.stderr or result.returncode == 1 and not result.stdout:
            self.message = ("sem permissão: rode  sudo ~/matrix-server-dashboard/install_hotkeys_sudo.sh", RED)
            self.d.collector.emit("ERROR", "rede: falta instalar o ajudante (install_hotkeys_sudo.sh)", RED)
            return
        try:
            data = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            data = {"ok": False, "message": (result.stderr or "resposta inválida").strip()[:70]}
        ok = bool(data.get("ok"))
        self.message = (str(data.get("message", "")), G5 if ok else RED)
        self.d.collector.emit("NET", f"config de rede: {data.get('message', '')}"[:80], G5 if ok else RED)
        self.d.notify(str(data.get("message", "")), G5 if ok else RED, 6)
        if ok:
            self.d.collector.ip = get_local_ip()
            self.d.collector.iface = iface_for_ip(self.d.collector.ip)
        self.load()
        self.build()

    @staticmethod
    def restart_policies() -> dict[str, str]:
        code, ids = run_cmd(["docker", "ps", "-aq"], timeout=4.0)
        if code != 0 or not ids:
            return {}
        _, out = run_cmd(["docker", "inspect", "-f", "{{.Name}} {{.HostConfig.RestartPolicy.Name}}", *ids.split()],
                         timeout=6.0)
        result = {}
        for line in out.splitlines():
            parts = line.strip().lstrip("/").split()
            if len(parts) == 2:
                result[parts[0]] = parts[1]
        return result

    CONTAINER_VERBS = {
        "reiniciar": ("restart", "reiniciado"),
        "desligar": ("stop", "desligado"),
        "ligar": ("start", "ligado"),
    }

    def container_action(self, name: str, verb: str, policy: str):
        cmd, done = self.CONTAINER_VERBS[verb]

        def run():
            code, _ = run_cmd(["docker", cmd, name], timeout=90)
            ok = code == 0
            self.message = (f"{name} {done}" if ok else f"falha ao {verb} {name}", G5 if ok else RED)
            self.d.collector.emit("DOCKER", f"{name} {done} pelo menu" if ok else f"falha ao {verb} {name}",
                                  G5 if ok else RED)
            # Refresh the list right away so the row shows the new state and actions.
            self.d.collector._docker_ps()
            self.d.collector.snap.containers = self.d.collector.containers
            current = name
            self.build()
            self.cursor = next((i for i, it in enumerate(self.items)
                                if it["kind"] == "container" and it["label"] == current), self.cursor)

        if verb == "desligar":
            lines = [f"Container: {name}", "Os serviços dele ficam fora do ar até você ligar de novo."]
            if policy == "always":
                lines.append("Atenção: ele volta a ligar sozinho se o servidor reiniciar.")
            else:
                lines.append("Ele continua desligado mesmo se o servidor reiniciar.")
            if name in ("cloudflared", "traefik"):
                lines.append("CUIDADO: este container dá acesso externo a outros serviços.")
        elif verb == "ligar":
            lines = [f"Container: {name}", "O container será iniciado com a configuração atual."]
        else:
            lines = [f"Container: {name}", "Os serviços dele ficam fora do ar por alguns segundos."]
        self.d.confirm(f"{verb.upper()} CONTAINER", lines, lambda: self.run_bg(f"{verb} {name}", run))

    # --- navigation

    def open(self):
        self.load()
        self.build()
        self.active = True
        self.edit_buffer = None
        self.scroll = 0
        self.cursor = self.next_selectable(-1, 1)
        self.message = None

    def close(self):
        self.active = False
        self.edit_buffer = None
        if self.net_dirty:
            self.d.notify("alterações de rede não aplicadas foram descartadas", AMBER, 4)
        else:
            self.d.notify("configurações fechadas", G4, 2)

    @staticmethod
    def selectable(item: dict[str, Any]) -> bool:
        return item["kind"] not in ("header", "info")

    def next_selectable(self, start: int, step: int) -> int:
        i = start + step
        while 0 <= i < len(self.items):
            if self.selectable(self.items[i]):
                return i
            i += step
        return start if 0 <= start < len(self.items) else 0

    def handle(self, key: str):
        if not self.items:
            return
        item = self.items[self.cursor]
        if self.edit_buffer is not None:
            if key == "esc":
                self.edit_buffer = None
                self.message = ("edição cancelada", G4)
            elif key == "enter":
                value = self.edit_buffer.strip()
                err = item["validate"](value) if item.get("validate") else None
                if err:
                    self.message = (err, RED)
                    return
                self.values[item["key"]] = value
                self.edit_buffer = None
                if item.get("save"):
                    item["save"]()
            elif key == "backspace":
                self.edit_buffer = self.edit_buffer[:-1]
            elif len(key) == 1 and key.isprintable() and len(self.edit_buffer) < 40:
                self.edit_buffer += key
            return

        if key in ("esc", "c", "C", "q", "Q"):
            self.close()
        elif key == "up":
            self.cursor = self.next_selectable(self.cursor, -1)
        elif key == "down":
            self.cursor = self.next_selectable(self.cursor, 1)
        elif key in ("enter", "left", "right", " "):
            kind = item["kind"]
            if kind == "container":
                if key in ("left", "right"):
                    step = -1 if key == "left" else 1
                    item["choice"] = (item["choice"] + step) % len(item["actions"])
                elif key == "enter":
                    self.container_action(item["label"], item["actions"][item["choice"]], item.get("policy", ""))
            elif kind == "choice":
                opts = item["options"]
                idx = opts.index(self.values[item["key"]]) if self.values[item["key"]] in opts else 0
                idx = (idx + (-1 if key == "left" else 1)) % len(opts)
                self.values[item["key"]] = opts[idx]
                if item.get("save"):
                    item["save"]()
                if item["key"] == "net_mode":
                    current = self.items[self.cursor]["label"]
                    self.build()
                    self.cursor = next((i for i, it in enumerate(self.items) if it.get("label") == current), self.cursor)
            elif key == "enter" and kind == "text":
                self.edit_buffer = self.values.get(item["key"], "")
                self.message = ("digite e confirme com OK · ESC cancela", CYAN)
            elif key == "enter" and kind == "action":
                item["action"]()

    # --- drawing

    def draw(self, c: Canvas, t: float):
        bw = min(104, c.w - 6)
        bh = min(c.h - 4, 46)
        bx, by = (c.w - bw) // 2, (c.h - bh) // 2
        c.box(bx, by, bw, bh, "▸ CONFIGURAÇÕES", "↑↓ navegar · OK editar · ESC fechar", G4, G6)
        c.pulse(bx, by, bw, bh, t * 30, 10)
        ix, iw = bx + 3, bw - 6
        value_x = ix + 32
        list_top, list_h = by + 2, bh - 6
        if self.cursor < self.scroll:
            self.scroll = max(0, self.cursor - 1)
        if self.cursor >= self.scroll + list_h:
            self.scroll = self.cursor - list_h + 1
        blink = int(t * 2.5) % 2 == 0
        for row, idx in enumerate(range(self.scroll, min(len(self.items), self.scroll + list_h))):
            it = self.items[idx]
            yy = list_top + row
            sel = idx == self.cursor
            kind = it["kind"]
            if kind == "header":
                c.put(ix, yy, "━━ " + it["label"] + " ", G5)
                info = it.get("info", "")
                fill_x = ix + len(it["label"]) + 4
                c.put(fill_x, yy, "─" * max(0, iw - len(it["label"]) - 4), G1)
                if info:
                    c.put(ix + iw - len(info), yy, f" {info}", G3)
                continue
            if sel:
                c.put(ix, yy, "▶", G6)
            label_style = WHITE if sel else TEXT
            if kind == "info":
                c.put(ix + 2, yy, it["label"], G3)
                c.put(value_x, yy, str(it["value"])[: iw - 32], TEXT)
            elif kind == "action":
                c.put(ix + 2, yy, f"▸ {it['label']}", it.get("style", G5) if not sel else G6)
                if self.net_dirty and "rede" in it["label"]:
                    c.put(value_x + 10, yy, "● alterações pendentes", AMBER if blink else G3)
            elif kind == "container":
                running = it["state"] == "running"
                c.put(ix + 2, yy, "●", G5 if running else AMBER)
                c.put(ix + 4, yy, it["label"], label_style)
                c.put(value_x, yy, "RUNNING" if running else "DESLIGADO", G5 if running else AMBER)
                if it.get("policy") == "always" and sel:
                    c.put(value_x + 11, yy, "always", G2)
                if sel:
                    verb = it["actions"][it["choice"]]
                    style = RED if verb == "desligar" else CYAN
                    arrows = len(it["actions"]) > 1
                    c.put(value_x + 19, yy, f"◂ {verb.upper()} ▸" if arrows else f"[ OK = {verb.upper()} ]", style)
            else:
                c.put(ix + 2, yy, it["label"], label_style)
                if sel and self.edit_buffer is not None:
                    c.put(value_x, yy, self.edit_buffer, CYAN)
                    if blink:
                        c.put(value_x + len(self.edit_buffer), yy, "█", CYAN)
                else:
                    value = self.values.get(it["key"], "")
                    if kind == "choice":
                        value = f"◂ {value} ▸" if sel else value
                    pending = it["key"].startswith("net_") and self.net_dirty
                    c.put(value_x, yy, value[: iw - 32], AMBER if pending else G5)
        if self.scroll > 0:
            c.put(bx + bw - 3, list_top, "▲", G4)
        if self.scroll + list_h < len(self.items):
            c.put(bx + bw - 3, list_top + list_h - 1, "▼", G4)

        c.put(ix, by + bh - 3, "─" * iw, G1)
        hint = self.items[self.cursor].get("hint", "") if self.items else ""
        if self.busy:
            right, right_style = f"{SPINNER[int(t * 10) % 4]} {self.busy}", CYAN
        elif self.message:
            right, right_style = self.message
        else:
            right, right_style = "", G3
        right = right[:iw]
        c.put(ix, by + bh - 2, hint[: max(0, iw - len(right) - 3)], G3)
        if right:
            c.put(ix + iw - len(right), by + bh - 2, right, right_style)


class Dashboard:
    HEADER_H = 5

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.base_dir = config_path.resolve().parent
        self.config = merge_config(config_path)
        image_path = Path(self.config.get("image", "assets/portrait.txt"))
        if not image_path.is_absolute():
            image_path = self.base_dir / image_path
        self.portrait = Portrait(image_path, socket.gethostname())
        self.user = autodetect.current_user()
        self.operator = str(self.config.get("operator") or self.user).upper()

        self.fps = max(2, min(30, int(self.config.get("fps", 12))))
        self.thresholds = self.config.get("thresholds", {})
        self.matrix_enabled = True
        self.image_enabled = True
        self.boot_enabled = bool(self.config.get("boot_animation", True))

        self.linux_console = os.environ.get("TERM", "") == "linux"
        self.use_palette = self.linux_console and bool(self.config.get("console_palette", True))
        self.screen = Screen(SGR_LINUX if self.linux_console else SGR_256)

        size = shutil.get_terminal_size((120, 40))
        self.width = max(40, size.columns)
        self.height = max(18, size.lines)
        self.rain = MatrixRain(self.width, self.height, float(self.config.get("matrix_density", 0.45)))

        self.collector = Collector(self.config)
        self.eased: dict[str, float] = {}
        self.instant = False
        self.art_size: tuple[int, int] | None = None
        self.graph_cache: dict[tuple, list[str]] = {}
        self.graph_cache_snap: Snapshot | None = None
        self.start = time.monotonic()
        self.boot_lines: list[tuple[str, str]] = []
        self.next_glitch = self.start + random.uniform(6, 10)
        self.glitch_until = 0.0
        self.glitch_rows: dict[int, int] = {}
        self.last_full_redraw = self.start
        self.stop = False
        self.eco = False
        self.blanked = False
        self.help_open = False
        self.modal: dict[str, Any] | None = None
        self.toast: tuple[str, int, float] | None = None
        self.speedtest_running = False
        backup_state = str(self.config.get("backup_state") or "")
        self.backup_dir = Path(os.path.expanduser(backup_state)).parent if backup_state else None
        self.menu = SettingsMenu(self)
        self.web: WebMirror | None = None
        self.usage_mode = "auto"
        self.netmap_open = False
        self.auto_blanked = False
        self.wake_until = 0.0
        self.old_term = None

    # --- terminal

    def setup_terminal(self):
        if sys.stdin.isatty():
            try:
                self.old_term = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
            except Exception:
                self.old_term = None
        out = [ALT_SCREEN, CLEAR, HIDE_CURSOR, WRAP_OFF]
        if self.use_palette:
            out += [f"\x1b]P{n:X}{rgb}" for n, rgb in LINUX_PALETTE.items()]
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def restore_terminal(self):
        if self.old_term is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_term)
            except Exception:
                pass
        out = [RESET, WRAP_ON, SHOW_CURSOR]
        if self.use_palette:
            out.append("\x1b]R")
        out.append(MAIN_SCREEN)
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def open_shell(self):
        """Hand the local console to a login shell; the dashboard resumes when it exits."""
        if not sys.stdin.isatty():
            self.notify("sem console local: use o terminal da página web", AMBER)
            return
        self.collector.emit("SYSTEM", "terminal local aberto", CYAN)
        self.restore_terminal()
        sys.stdout.write("\x1b[2J\x1b[H\x1b[32m== TERMINAL · digite exit (ou Ctrl+D) para voltar ao painel ==\x1b[0m\n\n")
        sys.stdout.flush()
        shell = os.environ.get("SHELL") or "/bin/bash"
        try:
            subprocess.call([shell, "-l"], cwd=os.path.expanduser("~"),
                            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        except OSError as exc:
            self.collector.emit("ERROR", f"terminal: {exc}"[:80], RED)
        finally:
            self.setup_terminal()
            self.screen.invalidate()
            self.collector.emit("SYSTEM", "terminal local fechado", G4)

    def read_key(self):
        if self.web:
            for key in self.web.pop_keys():
                # A web viewer's X opens the terminal in the browser, never the local console.
                if key.lower() != "x":
                    self.handle_key(key)
        if not sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
        try:
            while select.select([fd], [], [], 0)[0]:
                data = os.read(fd, 64).decode("utf-8", errors="ignore")
                i = 0
                while i < len(data):
                    ch = data[i]
                    if ch == "\x1b":
                        if i == len(data) - 1 and select.select([fd], [], [], 0.03)[0]:
                            data += os.read(fd, 64).decode("utf-8", errors="ignore")
                        # Arrow/function keys arrive as escape sequences.
                        j = i + 1
                        if j < len(data) and data[j] in "[O":
                            j += 1
                            while j < len(data) and not data[j].isalpha() and data[j] != "~":
                                j += 1
                            final = data[j] if j < len(data) else ""
                            j += 1
                            self.handle_key({"A": "up", "B": "down", "C": "right", "D": "left"}.get(final, ""))
                        else:
                            self.handle_key("esc")
                        i = j
                        continue
                    if ch in ("\r", "\n"):
                        self.handle_key("enter")
                    elif ch in ("\x7f", "\x08"):
                        self.handle_key("backspace")
                    else:
                        self.handle_key(ch)
                    i += 1
        except Exception:
            pass

    # --- hotkeys

    def notify(self, text: str, style: int = G5, seconds: float = 3.5):
        self.toast = (text, style, time.monotonic() + seconds)

    def confirm(self, title: str, lines: list[str], action, seconds: float = 10.0):
        self.modal = {"title": title, "lines": lines, "action": action, "deadline": time.monotonic() + seconds}

    def handle_key(self, key: str):
        if self.blanked:
            self.blanked = False
            if self.auto_blanked:
                self.auto_blanked = False
                self.wake_until = time.monotonic() + 300
                self.notify("tela ligada por 5 min", G5, 3)
            else:
                self.notify("tela ligada", G5, 2)
            self.screen.invalidate()
            return
        k = key.lower()
        if self.modal:
            modal, self.modal = self.modal, None
            if k in ("s", "y"):
                modal["action"]()
            else:
                self.notify("cancelado", G4, 2)
            return
        if self.menu.active:
            self.menu.handle(key)
            return
        if self.help_open:
            self.help_open = False
            if k in ("h", "?", "esc", "enter", " ", ""):
                return
        if k in ("h", "?"):
            self.help_open = True
        elif k == "c":
            self.menu.open()
        elif k == "m":
            self.matrix_enabled = not self.matrix_enabled
            self.notify(f"chuva matrix {'ligada' if self.matrix_enabled else 'desligada'}", G5, 2)
        elif k == "i":
            self.image_enabled = not self.image_enabled
            self.notify(f"retrato {'ligado' if self.image_enabled else 'desligado'}", G5, 2)
        elif k == "g":
            self.next_glitch = time.monotonic()
        elif k in ("l", "\x0c"):
            self.screen.invalidate()
            self.notify("tela redesenhada", G4, 2)
        elif k == "e":
            self.eco = not self.eco
            self.matrix_enabled = not self.eco
            self.notify(f"modo economia {'ligado: 4 fps, sem chuva' if self.eco else 'desligado'}", G5)
        elif k == "t":
            self.blanked = True
            self.screen.invalidate()
        elif k == "b":
            self.toggle_backup()
        elif k == "s":
            self.backup_now()
        elif k == "v":
            self.toggle_turbo()
        elif k == "p":
            self.toggle_icloud()
        elif k == "u":
            idx = (self.USAGE_MODES.index(self.usage_mode) + 1) % len(self.USAGE_MODES)
            self.usage_mode = self.USAGE_MODES[idx]
            self.notify({"auto": "retrato e relatório de uso alternando", "retrato": "retrato fixo",
                         "relatório": "relatório de uso do Claude fixo"}[self.usage_mode], G5, 2.5)
        elif k == "n":
            self.start_speedtest()
        elif k == "x":
            self.open_shell()
        elif k == "w":
            self.netmap_open = not self.netmap_open
            self.screen.invalidate()
            if self.netmap_open and not self.collector.snap.netmap:
                self.notify("mapeando a rede... primeira varredura leva ~5 s", CYAN, 4)
        elif k == "d":
            self.confirm("DESLIGAR O SERVIDOR", [
                "Todos os serviços e containers serão parados.",
                "Para ligar de novo é preciso acesso físico (ou Wake-on-LAN).",
            ], lambda: self.power("poweroff"))
        elif k == "r":
            self.confirm("REINICIAR O SERVIDOR", [
                "O servidor fica fora do ar por 1 a 2 minutos.",
                "Painel e serviços habilitados voltam sozinhos.",
            ], lambda: self.power("reboot"))
        elif k == "q":
            self.confirm("FECHAR O PAINEL", [
                "O monitor fica sem o painel até reiniciar o serviço",
                "(ou o servidor). Os serviços continuam rodando.",
            ], lambda: setattr(self, "stop", True))
        elif k not in ("", "esc", "enter", " ", "up", "down", "left", "right", "backspace"):
            self.notify(f"tecla '{k}' sem função · H mostra os atalhos", G3, 2.5)

    def update_auto_blank(self):
        sched = self.config.get("screen_off") or {}
        if not sched.get("enabled"):
            if self.auto_blanked:
                self.auto_blanked = self.blanked = False
                self.screen.invalidate()
            return
        now = time.strftime("%H:%M")
        start, end = str(sched.get("from", "00:00")), str(sched.get("to", "07:00"))
        in_window = start <= now < end if start <= end else (now >= start or now < end)
        busy = self.menu.active or self.modal is not None
        if in_window and not self.blanked and not busy and time.monotonic() > self.wake_until:
            self.blanked = self.auto_blanked = True
            self.screen.invalidate()
        elif not in_window and self.auto_blanked:
            self.auto_blanked = self.blanked = False
            self.screen.invalidate()

    def power(self, verb: str):
        label = "desligamento" if verb == "poweroff" else "reinício"
        self.collector.emit("SYSTEM", f"{label} solicitado pelo teclado", AMBER)
        try:
            result = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", verb],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=15)
            ok = result.returncode == 0
        except Exception:
            ok = False
        if ok:
            self.notify("DESLIGANDO..." if verb == "poweroff" else "REINICIANDO...", AMBER, 30)
        else:
            self.notify("sem permissão: rode  sudo ./install_hotkeys_sudo.sh", RED, 8)
            self.collector.emit("ERROR", f"{label} negado: falta a regra do sudo", RED)

    def toggle_backup(self):
        if not self.backup_dir or not self.backup_dir.exists():
            self.notify("backup não configurado", AMBER)
            return
        pause = self.backup_dir / "PAUSE"
        try:
            if pause.exists():
                pause.unlink()
                (self.backup_dir / "RUN_NOW").touch()
                self.notify("backup retomado · inicia em até 1 min", G5)
                self.collector.emit("BACKUP", "retomado pelo teclado", G5)
            else:
                pause.touch()
                self.notify("backup pausado · B de novo para retomar", AMBER)
                self.collector.emit("BACKUP", "pausado pelo teclado", AMBER)
        except OSError as exc:
            self.notify(f"erro: {exc}"[:60], RED)

    def toggle_icloud(self):
        name = str((self.config.get("icloud") or {}).get("container") or "")
        if not name:
            self.notify("download do iCloud não configurado", AMBER)
            return
        running = str((self.collector.snap.icloud or {}).get("state", "")) == "running"
        verb, cmd = ("pausado", "stop") if running else ("retomado", "start")

        def run():
            code, _ = run_cmd(["docker", cmd, name], timeout=60)
            if code == 0:
                self.notify(f"download do iCloud {verb}", AMBER if cmd == "stop" else G5)
                self.collector.emit("ICLOUD", f"download {verb} pelo teclado", AMBER if cmd == "stop" else G5)
                self.collector._icloud()
            else:
                self.notify(f"falha ao {cmd} {name}", RED)

        self.notify("pausando o download do iCloud..." if running else "retomando o download do iCloud...", CYAN, 3)
        threading.Thread(target=run, daemon=True).start()

    def set_icloud_limit(self, limit: str) -> str | None:
        cfg = self.config.setdefault("icloud", {})
        cfg["limit"] = limit
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            raw.setdefault("icloud", {}).update({"limit": limit})
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, self.config_path)
        except (OSError, ValueError) as exc:
            return f"erro ao salvar: {exc}"
        name = str(cfg.get("container") or "")

        def run():
            if self.collector.apply_icloud_limit(name, limit):
                self.collector.icloud_applied = None  # re-checked by the collector with the new value
                self.collector.emit("ICLOUD", f"limite de download: {'sem limite' if limit == 'off' else limit + 'B/s'}", CYAN)
        threading.Thread(target=run, daemon=True).start()
        return None

    def toggle_turbo(self):
        if not self.backup_dir or not self.backup_dir.exists():
            self.notify("backup não configurado", AMBER)
            return
        turbo = self.backup_dir / "TURBO"
        try:
            if turbo.exists():
                turbo.unlink()
                self.notify("rede LIMITADA · backup volta aos horários (3M dia / 8M noite)", G5, 5)
                self.collector.emit("BACKUP", "banda limitada pelo teclado", G5)
            else:
                turbo.touch()
                self.notify("rede LIBERADA · backup sem limite · V de novo para limitar", AMBER, 5)
                self.collector.emit("BACKUP", "turbo: banda liberada pelo teclado", AMBER)
        except OSError as exc:
            self.notify(f"erro: {exc}"[:60], RED)

    def backup_now(self):
        if not self.backup_dir or not self.backup_dir.exists():
            self.notify("backup não configurado", AMBER)
            return
        status = str((self.collector.snap.backup or {}).get("status", ""))
        if status == "running":
            self.notify("backup já está rodando", G4)
            return
        try:
            (self.backup_dir / "PAUSE").unlink(missing_ok=True)
            (self.backup_dir / "RUN_NOW").touch()
        except OSError as exc:
            self.notify(f"erro: {exc}"[:60], RED)
            return
        self.notify("sincronização solicitada · inicia em até 1 min", G5)
        self.collector.emit("BACKUP", "sincronização manual solicitada", CYAN)

    def start_speedtest(self):
        if self.speedtest_running:
            self.notify("teste de velocidade já em andamento", G4)
            return
        self.speedtest_running = True
        self.notify("testando velocidade da internet...", CYAN, 6)
        self.collector.emit("NET", "teste de velocidade iniciado", CYAN)
        threading.Thread(target=self._speedtest, daemon=True).start()

    def _speedtest(self):
        try:
            started = time.monotonic()
            req = urllib.request.Request("https://speed.cloudflare.com/__down?bytes=25000000",
                                         headers={"User-Agent": f"matrix-dashboard/{VERSION}"})
            got = 0
            with urllib.request.urlopen(req, timeout=30) as resp:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    got += len(chunk)
            down = got * 8 / (time.monotonic() - started) / 1e6
            payload = os.urandom(10_000_000)
            started = time.monotonic()
            req = urllib.request.Request("https://speed.cloudflare.com/__up", data=payload, method="POST",
                                         headers={"User-Agent": f"matrix-dashboard/{VERSION}",
                                                  "Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()
            up = len(payload) * 8 / (time.monotonic() - started) / 1e6
            self.collector.emit("NET", f"velocidade: down {down:.0f} Mbps · up {up:.0f} Mbps", G5)
            self.notify(f"down {down:.0f} Mbps · up {up:.0f} Mbps", G5, 8)
        except Exception as exc:
            self.collector.emit("NET", f"teste de velocidade falhou: {exc}"[:70], RED)
            self.notify("teste de velocidade falhou", RED)
        finally:
            self.speedtest_running = False

    # --- helpers

    def ease(self, key: str, target: float, rate: float = 0.22) -> float:
        if self.instant:
            return target
        cur = self.eased.get(key, 0.0)
        cur += (target - cur) * rate
        if abs(target - cur) < 0.05:
            cur = target
        self.eased[key] = cur
        return cur

    def graph(self, name: str, values: list[float], width: int, height: int, vmax: float | None = None) -> list[str]:
        # History only changes once per second, so graphs are rebuilt per snapshot.
        if self.graph_cache_snap is not self.collector.snap:
            self.graph_cache = {}
            self.graph_cache_snap = self.collector.snap
        key = (name, width, height, vmax)
        if key not in self.graph_cache:
            self.graph_cache[key] = graph_rows(values, width, height, vmax)
        return self.graph_cache[key]

    def level_style(self, metric: str, value: float) -> int:
        threshold = float(self.thresholds.get(metric, 999))
        if value >= threshold:
            return RED
        if value >= threshold * 0.8:
            return AMBER
        return G5

    def bar(self, c: Canvas, x: int, y: int, width: int, pct: float, t: float, style: int = G5, phase: float = 0.0):
        width = max(4, width)
        pct = max(0.0, min(100.0, pct))
        exact = width * pct / 100
        filled = int(exact)
        frac = exact - filled
        c.put(x, y, "░" * width, G1)
        if filled:
            c.put(x, y, "█" * filled, style)
        if filled < width and frac >= 0.35:
            c.put(x + filled, y, "▒", style)
        if filled > 2 and style == G5:
            sheen = int((t * 18 + phase) % (width + 25))
            if sheen < filled:
                c.style_at(x + sheen, y, G6)

    def backup_ticker(self, s: Snapshot) -> list[str]:
        b = s.backup
        if not b or b.get("status") == "not_started":
            return []
        if b.get("initial_complete") and b.get("status") != "running":
            return ["GOOGLE DRIVE BACKUP IN SYNC"]
        total = float(b.get("source_bytes") or 0)
        pct = min(100.0, float(b.get("uploaded_bytes") or 0) / total * 100) if total else 0.0
        turbo = " · TURBO" if self.turbo_on() else ""
        items = [f"BACKUP {pct:.1f}% · {str(b.get('status', '')).upper()}{turbo}"]
        windows = (s.plan or {}).get("windows") or {}
        if windows:
            parts = [f"{lbl} {windows[k]['pct']:.0f}%" for k, lbl in (("five_hour", "5H"), ("seven_day", "SEMANA")) if k in windows]
            items.append("CLAUDE " + " · ".join(parts))
        ic = s.icloud or {}
        if ic.get("total"):
            ic_pct = min(100.0, ic.get("items", 0) / ic["total"] * 100)
            items.append(f"ICLOUD {ic_pct:.1f}% · {ic.get('items', 0)}/{ic['total']} ITENS")
        return items

    def alerts(self, s: Snapshot) -> list[str]:
        out = []
        th = self.thresholds
        recent = s.cpu_hist[-5:]
        if len(recent) == 5 and min(recent) >= float(th.get("cpu", 85)):
            out.append(f"CPU {s.cpu:.0f}%")
        if s.ram >= float(th.get("ram", 85)):
            out.append(f"RAM {s.ram:.0f}%")
        if s.temperature is not None and s.temperature >= float(th.get("temperature", 80)):
            out.append(f"TEMP {s.temperature:.0f}°C")
        for mount, pct, _, _ in s.disks:
            if pct >= float(th.get("disk", 90)):
                out.append(f"DISK {mount} {pct:.0f}%")
        if not s.internet:
            out.append("INTERNET OFFLINE")
        elif not s.net4:
            out.append("IPV4 OFFLINE (IPV6 OK) - CONTAINERS SEM INTERNET")
        elif s.net6 is False:
            out.append("IPV6 OFFLINE (IPV4 OK)")
        for ct in s.containers:
            if ct.state in ("restarting", "dead") or ct.health == "unhealthy" or ct.name in s.flagged:
                out.append(f"{ct.name.upper()} {ct.health.upper() if ct.health == 'unhealthy' else ct.state.upper()}")
        for label, _, state in s.units:
            if state != "active":
                out.append(f"{label} {state.upper()}")
        five = ((s.plan or {}).get("windows") or {}).get("five_hour")
        if five and five["pct"] >= 100 and five["resets"] > time.time():
            out.append(f"CLAUDE: LIMITE DE 5H ESGOTADO - RENOVA ÀS {time.strftime('%H:%M', time.localtime(five['resets']))}")
        if (s.icloud or {}).get("status") == "login":
            out.append("ICLOUD PRECISA DE LOGIN - ABRA A PORTA 8090")
        ts = s.tailscale or {}
        if ts.get("state") == "NeedsLogin":
            out.append("TAILSCALE DESLOGADO - SEM ACESSO REMOTO")
        elif ts.get("state") not in (None, "", "Running", "Unavailable"):
            out.append(f"TAILSCALE {str(ts.get('state')).upper()}")
        elif ts.get("expiry_days") is not None and ts["expiry_days"] < 7:
            out.append(f"TAILSCALE: CHAVE EXPIRA EM {ts['expiry_days']} DIAS")
        return out

    # --- boot

    def build_boot_lines(self, s: Snapshot):
        running = sum(1 for ct in s.containers if ct.state == "running")
        lines = [
            ("SYS", f"{self.config.get('brand') or 'MATRIX SECURE TERMINAL'} v{VERSION}  ::  node {s.hostname}"),
            ("OK", f"kernel {s.kernel}"),
            ("OK", f"cpu {s.cores} cores  //  memory {fmt_size(s.ram_total)}"),
        ]
        for mount, _, _, total in s.disks:
            lines.append(("OK", f"mount {mount:<8} {fmt_size(total)}"))
        lines.append(("OK" if s.docker_ok else "FAIL", f"docker engine  //  {running}/{len(s.containers)} containers running"))
        active = sum(1 for _, _, st in s.units if st == "active")
        if s.units:
            lines.append(("OK" if active == len(s.units) else "WARN", f"host services  //  {active}/{len(s.units)} active"))
        lines.append(("OK", f"network {s.iface}  //  {s.ip}"))
        lines.append((
            "OK" if s.net4 and s.net6 is not False else "WARN" if s.internet else "FAIL",
            "uplink  //  " + "   ".join(f"{fam} " + (f"{lat:.0f} ms" if up and lat is not None else "down")
                                       for fam, up, lat in (("IPv4", s.net4, s.lat4), ("IPv6", s.net6, s.lat6))
                                       if up is not None),
        ))
        if s.claude.running:
            lines.append(("OK", f"claude code agent v{s.claude.version}  //  online"))
        elif self.config.get("claude"):
            lines.append(("WARN", "claude code agent  //  not running"))
        if s.tailscale and s.tailscale.get("ip"):
            lines.append(("OK" if s.tailscale.get("online") else "WARN", f"tailscale  //  {s.tailscale['ip']}"))
        lines.append(("OK", "decrypting identity matrix"))
        lines.append(("SYS", f"LINK ESTABLISHED. WELCOME, {self.operator}."))
        self.boot_lines = lines

    def boot_duration(self) -> float:
        return 0.28 * len(self.boot_lines) + 0.9

    def draw_boot(self, c: Canvas, t: float):
        bw = min(80, c.w - 4)
        bh = len(self.boot_lines) + 7
        bx = (c.w - bw) // 2
        by = max(0, (c.h - bh) // 2)
        c.box(bx, by, bw, bh, "BOOT // INITIALIZING", "", G2, G5)
        c.pulse(bx, by, bw, bh, t * 40, 10)
        for i, (status, text) in enumerate(self.boot_lines):
            started = i * 0.28
            if t < started:
                break
            visible = text[: int((t - started) * 110)]
            yy = by + 2 + i
            if status == "SYS":
                c.put(bx + 3, yy, visible, G6 if i else G5)
            else:
                tag_style = {"OK": G5, "WARN": AMBER, "FAIL": RED}[status]
                done = len(visible) == len(text)
                c.put(bx + 3, yy, "[      ]", G2)
                if done:
                    c.put(bx + 5, yy, f"{status:^4}", tag_style)
                else:
                    c.put(bx + 5, yy, SPINNER[int(t * 12) % 4], G4)
                c.put(bx + 12, yy, visible, TEXT if done else G4)
        progress = min(1.0, t / max(0.1, self.boot_duration() - 0.5))
        pw = bw - 18
        self.bar(c, bx + 3, by + bh - 3, pw, progress * 100, t)
        c.put(bx + pw + 5, by + bh - 3, f"{progress * 100:5.1f}%", G5)
        if int(t * 3) % 2 == 0:
            c.put(bx + 3, by + bh - 2, "_", G5)

    # --- panels

    def draw_header(self, c: Canvas, s: Snapshot, t: float, alerts: list[str]):
        w = c.w
        c.box(0, 0, w, self.HEADER_H, "", "", RED if alerts and int(t * 2) % 2 else G2, G5, double=True)

        title = f"◈ {self.config.get('title', DEFAULT_CONFIG['title'])}"
        hi = int(t * 16) % (len(title) + 40)
        for i, ch in enumerate(title):
            style = WHITE if abs(i - hi) <= 1 else G6 if abs(i - hi) <= 3 else G5
            c.put(3 + i, 1, ch, style)

        now = time.localtime()
        clock = time.strftime("%H%M%S", now)
        glyphs = [clock[0], clock[1], ":", clock[2], clock[3], ":", clock[4], clock[5]]
        clock_w = sum(2 if g == ":" else 4 for g in glyphs) - 1
        cx = w - clock_w - 3
        colon_on = (time.time() % 1.0) < 0.5
        gx = cx
        for idx, g in enumerate(glyphs):
            if g == ":":
                if colon_on:
                    c.put(gx, 1, "•", G4)
                    c.put(gx, 3, "•", G4)
                gx += 2
                continue
            style = G4 if idx >= 6 else G6
            for r in range(3):
                c.put_sparse(gx, 1 + r, BIG_DIGITS[g][r], style)
            gx += 4

        date = f"{DAYS_PT[now.tm_wday]} {now.tm_mday:02d} {MONTHS_PT[now.tm_mon - 1]} {now.tm_year}"
        c.put(cx - len(date) - 4, 1, date, G4)

        info = [
            ("HOST", s.hostname), ("IP", f"{s.ip} ({s.iface})"), ("KERNEL", s.kernel.split("-generic")[0]),
            ("UPTIME", fmt_uptime(s.uptime_s)), ("OPERATOR", self.operator),
        ]
        xx = 3
        limit = cx - 4
        for idx, (label, value) in enumerate(info):
            seg = len(label) + len(value) + 5
            if xx + seg > limit:
                break
            if idx:
                c.put(xx - 3, 2, " ▸ ", G1)
            c.put(xx, 2, label, G2)
            c.put(xx + len(label) + 1, 2, value, TEXT)
            xx += seg

        tick_w = max(10, limit - 3)
        if alerts:
            items = [f"ALERT: {a}" for a in alerts]
            style = RED
        else:
            running = sum(1 for ct in s.containers if ct.state == "running")
            active = sum(1 for _, _, st in s.units if st == "active")
            items = [
                "ALL SYSTEMS NOMINAL",
                f"{running}/{len(s.containers)} CONTAINERS RUNNING",
                f"{active}/{len(s.units)} HOST SERVICES ACTIVE",
            ] + ([
                (f"CLAUDE CODE ONLINE · {s.claude.sessions} SESSION{'S' if s.claude.sessions != 1 else ''}"
                 if s.claude.running else "CLAUDE CODE OFFLINE"),
            ] if self.config.get("claude") else []) + [
                f"LINK {s.latency:.0f} MS" if s.latency is not None else "LINK TIMEOUT",
            ] + self.backup_ticker(s) + [
            ] + [f"{m} {p:.0f}% USED" for m, p, _, _ in s.disks]
            style = G4
        text = "   ▸   ".join(items) + "   ▸   "
        offset = int(t * 9) % len(text)
        loop = (text * (tick_w // len(text) + 2))[offset:offset + tick_w]
        c.put(3, 3, loop, style)

    def draw_system(self, c: Canvas, s: Snapshot, x: int, y: int, w: int, h: int, t: float):
        c.box(x, y, w, h, "▸ SYSTEM // TELEMETRY", f"{s.cores} CORES", G2, G5)
        c.pulse(x, y, w, h, t * 14 + 30)
        ix, iw = x + 2, w - 4
        bw = max(8, iw - 30)
        row = y + 1
        bottom = y + h - 1

        def line(label: str, pct: float, metric: str, extra: str = "", phase: float = 0.0):
            nonlocal row
            if row >= bottom:
                return
            val = self.ease(label, pct)
            style = self.level_style(metric, pct)
            c.put(ix, row, label, G3)
            self.bar(c, ix + 7, row, bw, val, t, style, phase)
            c.put(ix + 8 + bw, row, f"{val:5.1f}%", style if style != G5 else G6)
            if extra:
                c.put(ix + 15 + bw, row, extra[: max(0, iw - bw - 15)], TEXT)
            row += 1

        line("CPU", s.cpu, "cpu", f"load {s.load[0]:.2f}")
        if row < bottom and s.per_core:
            per_w = max(18, iw // max(1, min(len(s.per_core), 4)))
            core_bw = max(4, per_w - 13)
            for idx, pct in enumerate(s.per_core[:4]):
                cx = ix + idx * per_w
                if cx + per_w > ix + iw + 1:
                    break
                val = self.ease(f"core{idx}", pct)
                c.put(cx, row, f"c{idx}", G2)
                self.bar(c, cx + 3, row, core_bw, val, t, self.level_style("cpu", pct), idx * 7)
                c.put(cx + 4 + core_bw, row, f"{val:3.0f}%", G4)
            row += 1

        graph_h = 3 if h >= 15 else 2 if h >= 12 else 0
        if graph_h and row + graph_h < bottom:
            rows = self.graph("cpu", s.cpu_hist, iw, graph_h, 100.0)
            c.put(ix, row + graph_h - 1, "·" * iw, G0)
            for r, text in enumerate(rows):
                c.put_sparse(ix, row + r, text, G5 if r == 0 else G4 if r < graph_h - 1 else G3)
            label = f" cpu · last {iw}s "
            c.put(ix + iw - len(label), row, label, G2)
            row += graph_h

        line("RAM", s.ram, "ram", f"{fmt_size(s.ram_used)} / {fmt_size(s.ram_total)}", 9)
        if s.swap_total:
            line("SWAP", s.swap, "ram", f"{fmt_size(s.swap_used)} / {fmt_size(s.swap_total)}", 17)
        if s.temperature is not None:
            if row < bottom:
                temp = self.ease("temp", s.temperature)
                style = self.level_style("temperature", s.temperature)
                c.put(ix, row, "TEMP", G3)
                self.bar(c, ix + 7, row, bw, temp, t, style, 23)
                c.put(ix + 8 + bw, row, f"{temp:5.1f}°C", style if style != G5 else G6)
                row += 1
        if row < bottom:
            c.put(ix, row, "LOAD", G3)
            c.put(ix + 7, row, f"{s.load[0]:.2f}  {s.load[1]:.2f}  {s.load[2]:.2f}", TEXT)
            c.put(ix + 30, row, "PROCS", G3)
            c.put(ix + 36, row, str(s.procs), TEXT)
            if iw > 60:
                c.put(ix + 44, row, "UP", G3)
                c.put(ix + 47, row, fmt_uptime(s.uptime_s), TEXT)
            row += 1
        if row < bottom - len(s.disks):
            c.put(ix, row, "┄" * iw, G1)
            row += 1
        for mount, pct, used, total in s.disks:
            if row >= bottom:
                break
            label = mount if len(mount) <= 6 else "~" + mount[-5:]
            val = self.ease(f"disk{mount}", pct)
            style = self.level_style("disk", pct)
            c.put(ix, row, label, G3)
            self.bar(c, ix + 7, row, bw, val, t, style, 31)
            c.put(ix + 8 + bw, row, f"{val:5.1f}%", style if style != G5 else G6)
            c.put(ix + 15 + bw, row, f"{fmt_size(used)} / {fmt_size(total)}"[: max(0, iw - bw - 15)], TEXT)
            row += 1

    def draw_services(self, c: Canvas, s: Snapshot, x: int, y: int, w: int, h: int, t: float):
        running = sum(1 for ct in s.containers if ct.state == "running")
        active = sum(1 for _, _, st in s.units if st == "active")
        right = f"{running}/{len(s.containers)} UP"
        c.box(x, y, w, h, "▸ SERVICES // DOCKER · HOST · AI", right, G2, G5)
        c.pulse(x, y, w, h, t * 14 + 140)
        ix, iw = x + 2, w - 4
        row = y + 1
        bottom = y + h - 1
        blink = int(t * 2) % 2 == 0
        wide = iw >= 70

        def section(label: str, right_label: str = "", style: int = G4):
            nonlocal row
            if row >= bottom:
                return
            c.put(ix, row, "━━ " + label + " ", style)
            fill_x = ix + len(label) + 4
            fill_w = iw - len(label) - 4 - (len(right_label) + 2 if right_label else 0)
            c.put(fill_x, row, "─" * max(0, fill_w), G1)
            if right_label:
                c.put(ix + iw - len(right_label), row, right_label, G3)
            row += 1

        # Column layout: dot name state up health cpu mem endpoint
        name_w = 17 if wide else 15
        col_state = ix + 2 + name_w + 1
        col_up = col_state + 9
        col_health = col_up + 6
        col_cpu = col_health + 10
        col_mem = col_cpu + 7
        col_probe = col_mem + 7

        section("CONTAINERS", f"{running}/{len(s.containers)} running")
        if not s.docker_ok:
            c.put(ix + 2, row, "docker unavailable", RED)
            row += 1
        else:
            if row < bottom:
                c.put(ix + 2, row, "NAME", G2)
                c.put(col_state, row, "STATE", G2)
                c.put(col_up, row, "AGE", G2)
                c.put(col_health, row, "HEALTH", G2)
                if wide:
                    c.put(col_cpu, row, "CPU", G2)
                    c.put(col_mem, row, "MEM", G2)
                    c.put(col_probe, row, "ENDPOINT", G2)
                row += 1

            def order(ct: Container):
                bad = ct.state != "running" or ct.health == "unhealthy"
                return (0 if ct.name in s.flagged else 1 if bad and ct.state != "exited" else 2 if ct.state == "running" else 3, ct.name)

            containers = sorted(s.containers, key=order)
            reserve = 3 + len(s.units) + 5
            room = max(1, bottom - row - reserve)
            shown = containers
            scroll = None
            if len(containers) > room and room > 2:
                # Not everything fits: keep problem containers pinned on top and
                # let the others scroll one row every few seconds, wrapping around.
                slots = room - 1
                pinned = [ct for ct in containers if order(ct)[0] < 2][: max(0, slots - 2)]
                rest = [ct for ct in containers if ct not in pinned]
                visible = slots - len(pinned)
                start = int(t / 2.5) % len(rest)
                shown = pinned + [rest[(start + i) % len(rest)] for i in range(min(visible, len(rest)))]
                scroll = (start, visible, len(rest), len(pinned))
            for idx, ct in enumerate(shown):
                if row >= bottom:
                    break
                flagged = ct.name in s.flagged
                if ct.state == "running" and ct.health in ("", "healthy"):
                    pulse = (math.sin(t * 2.2 + idx * 0.9) + 1) / 2
                    dot_style, state_style, state = (G6 if pulse > 0.7 else G5 if pulse > 0.3 else G4), G5, "RUNNING"
                elif ct.state == "running":
                    dot_style, state_style = AMBER if ct.health == "starting" else RED, AMBER if ct.health == "starting" else RED
                    state = "RUNNING"
                elif ct.state in ("restarting", "dead") or flagged:
                    dot_style = RED if blink else G0
                    state_style, state = RED, ct.state.upper()[:8]
                elif ct.state == "exited":
                    dot_style, state_style, state = AMBER, AMBER, "STOPPED"
                else:
                    dot_style, state_style, state = G2, G2, ct.state.upper()[:8]
                c.put(ix, row, "●", dot_style)
                c.put(ix + 2, row, ct.name[:name_w], TEXT if ct.state == "running" else G3)
                c.put(col_state, row, state, state_style)
                c.put(col_up, row, ct.age, G4 if ct.state == "running" else G2)
                if ct.state == "exited":
                    c.put(col_health, row, f"exit {ct.exit_code}"[:9], AMBER if ct.exit_code not in ("", "0") else G2)
                elif ct.health:
                    hs = G5 if ct.health == "healthy" else AMBER if ct.health == "starting" else RED
                    c.put(col_health, row, ct.health[:9], hs)
                else:
                    c.put(col_health, row, "-", G1)
                if wide:
                    if ct.cpu is not None:
                        c.put(col_cpu, row, f"{ct.cpu:4.1f}%", AMBER if ct.cpu > 50 else G4)
                    else:
                        c.put(col_cpu, row, "-", G1)
                    c.put(col_mem, row, ct.mem or "-", G4 if ct.mem else G1)
                    probe = s.probes.get(ct.name)
                    if probe and ct.state == "running":
                        ok, code, ms, _ = probe
                        if ok:
                            label = f"{code} {ms:.0f}ms" if code else f"tcp {ms:.0f}ms"
                            c.put(col_probe, row, label[: ix + iw - col_probe], G5)
                        else:
                            c.put(col_probe, row, "NO REPLY", RED if blink else AMBER)
                    else:
                        c.put(col_probe, row, ct.ports[: ix + iw - col_probe], G2)
                row += 1
            if scroll and row < bottom:
                start, visible, total, pinned_n = scroll
                first, last = start + 1, (start + visible - 1) % total + 1
                label = f"↓ rolando · {first}-{last} de {total}"
                if pinned_n:
                    label += f" · {pinned_n} com problema fixo{'s' if pinned_n > 1 else ''} no topo"
                c.put(ix + 2, row, label[: iw - 4], G2)
                # Position marker of the scroll window.
                bar_w = 12
                pos = int(start / max(1, total) * bar_w)
                c.put(ix + iw - bar_w, row, "·" * bar_w, G1)
                c.put(ix + iw - bar_w + pos, row, "█", G4)
                row += 1

        if s.units and row < bottom - 1:
            row += 1 if bottom - row > len(s.units) + 6 else 0
            section("HOST", f"{active}/{len(s.units)} active")
            for label, unit, state in s.units:
                if row >= bottom:
                    break
                ok = state == "active"
                state_label, state_style = state.upper()[:8], G5 if ok else RED
                detail, detail_style = unit, G2
                ts = s.tailscale if unit == "tailscaled" else None
                if ok and ts:
                    ts_state = ts.get("state")
                    if ts_state == "Running":
                        state_label = "ONLINE"
                        days = ts.get("expiry_days")
                        detail = ts.get("ip", "") + (f"  key {days}d" if days is not None and days < 30 else "")
                        detail_style = AMBER if days is not None and days < 7 else G4
                    elif ts_state == "NeedsLogin":
                        ok, state_label, state_style = False, "LOGGED OUT", RED
                        detail, detail_style = "login: tailscale up", AMBER
                    elif ts_state:
                        ok, state_label, state_style = False, str(ts_state).upper()[:10], AMBER
                c.put(ix, row, "●", (G5 if (math.sin(t * 2 + row) > -0.3) else G4) if ok else (RED if blink else G0))
                c.put(ix + 2, row, label[:name_w], TEXT)
                c.put(col_state, row, state_label, state_style)
                c.put(col_up + (2 if len(state_label) > 8 else 0), row, detail[: iw - (col_up - ix) - 2], detail_style)
                row += 1
            for key, (ok, code, ms, name) in s.probes.items():
                if row >= bottom or any(ct.name == key for ct in s.containers):
                    continue
                c.put(ix, row, "●", G5 if ok else RED)
                c.put(ix + 2, row, name[:name_w], TEXT)
                c.put(col_state, row, "UP" if ok else "DOWN", G5 if ok else RED)
                if ms is not None:
                    c.put(col_up, row, f"{code or 'tcp'} {ms:.0f}ms", G4)
                row += 1

        if row < bottom - 1 and self.config.get("claude"):
            row += 1 if bottom - row > 4 else 0
            cl = s.claude
            section("AI AGENT // CLAUDE CODE", f"v{cl.version}" if cl.version else "", CYAN)
            if row < bottom:
                if cl.running:
                    pulse = (math.sin(t * 3.0) + 1) / 2
                    c.put(ix, row, "●", CYAN if pulse > 0.4 else G4)
                    c.put(ix + 2, row, "CLAUDE CODE", WHITE)
                    c.put(col_state, row, "ONLINE", CYAN)
                    sess = f"{cl.sessions} session{'s' if cl.sessions != 1 else ''}"
                    c.put(col_up, row, sess, TEXT)
                    if cl.remote and wide:
                        c.put(col_up + len(sess) + 3, row, f"remote ◂ {cl.remote} ▸", G4)
                else:
                    c.put(ix, row, "●", RED if blink else G0)
                    c.put(ix + 2, row, "CLAUDE CODE", G3)
                    c.put(col_state, row, "OFFLINE", RED)
                    c.put(col_up, row, "no claude process found", G2)
                row += 1
            if cl.running and row < bottom:
                state = "WORKING" if cl.working else "IDLE"
                spin = SPINNER[int(t * 10) % 4] if cl.working else "·"
                c.put(ix + 2, row, "└", G2)
                c.put(ix + 4, row, f"{spin} {state}", CYAN if cl.working else G4)
                info = f"cpu {cl.cpu:4.1f}%  mem {fmt_size(cl.rss)}  up {fmt_uptime(time.time() - cl.since)}"
                c.put(col_state + 2, row, info, TEXT)
                hx = col_state + 2 + len(info) + 2
                hw = ix + iw - hx
                if hw >= 8:
                    c.put(hx, row, heat_strip(s.claude_hist, hw, max(10.0, max(s.claude_hist[-hw:], default=0))), CYAN)
                row += 1
            if s.plan and s.plan["windows"] and row < bottom:
                c.put(ix + 2, row, "└", G2)
                c.put(ix + 4, row, "plano", G3)
                xx = col_state + 2
                for key, label in (("five_hour", "5h"), ("seven_day", "sem")):
                    w = s.plan["windows"].get(key)
                    if not w:
                        continue
                    style = self.limit_style(w["pct"])
                    c.put(xx, row, label, G3)
                    self.bar(c, xx + len(label) + 1, row, 8, w["pct"], t, style)
                    txt = f"{w['pct']:3.0f}% {self.reset_label(w['resets'], key)}"
                    c.put(xx + len(label) + 10, row, txt, style if style != G5 else TEXT)
                    xx += len(label) + 11 + len(txt) + 2
                row += 1
            if s.usage and row < bottom:
                u = s.usage
                c.put(ix + 2, row, "└", G2)
                c.put(ix + 4, row, "uso", G3)
                line = (f"hoje {fmt_tokens(u['today']['tokens'])} ${u['today']['cost']:.2f}  ·  "
                        f"semana ${u['week']['cost']:.2f}  ·  5h ${u['last5h']['cost']:.2f}")
                c.put(col_state + 2, row, line[: ix + iw - col_state - 2], CYAN)
                row += 1

    @staticmethod
    def limit_style(pct: float) -> int:
        return RED if pct >= 90 else AMBER if pct >= 70 else G5

    @staticmethod
    def reset_label(resets: float, key: str) -> str:
        if not resets:
            return ""
        if resets <= time.time():
            return "renovado"
        lt = time.localtime(resets)
        if key == "five_hour" or resets - time.time() < 20 * 3600:
            return time.strftime("até %H:%M", lt)
        return f"até {DAYS_PT[lt.tm_wday].lower()} {time.strftime('%H:%M', lt)}"

    def draw_plan_limits(self, c: Canvas, s: Snapshot, x: int, y: int, w: int, t: float) -> int:
        """Two wide bars like the claude.ai 'usage limits' card. Returns rows used."""
        plan = s.plan or {}
        windows = plan.get("windows") or {}
        name = f" · {plan['plan']}" if plan.get("plan") else ""
        age = time.time() - float(plan.get("updated") or 0)
        fresh = "agora" if age < 90 else f"há {int(age // 60)} min"
        label = "━━ LIMITES DO PLANO" + name + " "
        right = f" atualizado {fresh}" if windows else " aguardando dados do Claude Code"
        c.put(x, y, label, CYAN)
        c.put(x + len(label), y, "─" * max(0, w - len(label) - len(right)), G1)
        c.put(x + w - len(right), y, right, G3 if age < 600 else AMBER)
        row = y + 1
        for key, label in (("five_hour", "Limite de 5 horas"), ("seven_day", "Semanal · todos os modelos")):
            win = windows.get(key)
            if not win:
                continue
            style = self.limit_style(win["pct"])
            info = f"{self.reset_label(win['resets'], key):>14}  {win['pct']:3.0f}%"
            c.put(x, row, label, TEXT)
            c.put(x + w - len(info), row, info, style if style != G5 else G6)
            bar_x = x + 28
            self.bar(c, bar_x, row, max(8, w - 28 - len(info) - 2), win["pct"], t, style)
            row += 1
        return row - y

    USAGE_MODES = ("auto", "retrato", "relatório")

    def usage_page_active(self, s: Snapshot, t: float) -> bool:
        if not s.usage or self.usage_mode == "retrato":
            return False
        if self.usage_mode == "relatório":
            return True
        # auto: 40 s portrait, 20 s report
        return (t % 60) >= 40

    def draw_usage(self, c: Canvas, s: Snapshot, x: int, y: int, w: int, h: int, t: float):
        u = s.usage or {}
        mode = "" if self.usage_mode == "auto" else " · fixo"
        c.box(x, y, w, h, "▸ CLAUDE CODE // RELATÓRIO DE USO", f"U alterna{mode}", G2, G5)
        c.pulse(x, y, w, h, t * 18)
        ix, iw = x + 2, w - 4
        bottom = y + h - 1
        row = y + 1
        # Bars grow in when the page appears.
        grow = min(1.0, ((t % 60) - 40) / 1.2) if self.usage_mode == "auto" else 1.0
        grow = max(0.05, grow)

        def heading(label: str, right: str = ""):
            nonlocal row
            if row >= bottom:
                return
            c.put(ix, row, "━━ " + label + " ", CYAN)
            fill = iw - len(label) - 4 - (len(right) + 1 if right else 0)
            c.put(ix + len(label) + 4, row, "─" * max(0, fill), G1)
            if right:
                c.put(ix + iw - len(right), row, right, G3)
            row += 1

        if s.plan:
            row += self.draw_plan_limits(c, s, ix, row, iw, t) + 1

        # Summary cards
        cards = [("HOJE", u.get("today")), ("ÚLTIMAS 5H", u.get("last5h")), ("ESTA SEMANA", u.get("week")),
                 ("30 DIAS", u.get("last30d")), ("TOTAL", u.get("total"))]
        card_w = iw // len(cards)
        for i, (label, d) in enumerate(cards):
            d = d or {}
            cx = ix + i * card_w
            c.put(cx, row, label, G4)
            c.put(cx, row + 1, f"${d.get('cost', 0):,.2f}", WHITE if i == 0 else G6)
            c.put(cx, row + 2, f"{fmt_tokens(d.get('tokens', 0))} tokens", TEXT)
            c.put(cx, row + 3, f"{int(d.get('messages', 0))} respostas", G3)
            c.put(cx, row + 4, f"saída {fmt_tokens(d.get('output', 0))}", G3)
        row += 6

        # Hourly chart
        hourly = u.get("hourly") or []
        heading("POR HORA · ÚLTIMAS 24H", "custo equivalente em US$")
        chart_h = 5 if h >= 38 else 3
        if hourly and row + chart_h + 1 < bottom:
            values = [d["cost"] * grow for _, d in hourly]
            vmax = max(max(values), 0.01)
            bar_w, gap = max(1, (iw - 8) // len(values) - 1), 1
            for r in range(chart_h):
                level = chart_h - r
                c.put(ix, row + r, f"{vmax * level / chart_h:6.2f}" if r in (0, chart_h - 1) else "", G2)
                for i, v in enumerate(values):
                    f = v / vmax * chart_h - (level - 1)
                    ch = "█" if f >= 0.75 else "▒" if f >= 0.45 else "░" if f >= 0.12 else ""
                    if ch:
                        style = CYAN if i == len(values) - 1 else G5 if r < 2 else G4
                        c.put(ix + 8 + i * (bar_w + gap), row + r, ch * bar_w, style)
            row += chart_h
            for i, (hour, _) in enumerate(hourly):
                if i % 3 == 0:
                    c.put(ix + 8 + i * (bar_w + gap), row, time.strftime("%Hh", time.localtime(hour)), G3)
            row += 2

        # Daily chart
        daily = u.get("daily") or []
        heading("POR DIA · ÚLTIMOS 14 DIAS")
        chart_h = 4 if h >= 38 else 3
        if daily and row + chart_h + 2 < bottom:
            values = [d["cost"] * grow for _, d in daily]
            vmax = max(max(values), 0.01)
            slot = (iw - 8) // len(values)
            bar_w = max(1, slot - 2)
            for r in range(chart_h):
                level = chart_h - r
                for i, v in enumerate(values):
                    f = v / vmax * chart_h - (level - 1)
                    ch = "█" if f >= 0.75 else "▒" if f >= 0.45 else "░" if f >= 0.12 else ""
                    if ch:
                        c.put(ix + 8 + i * slot, row + r, ch * bar_w, CYAN if i == len(values) - 1 else G5)
            row += chart_h
            for i, (day, d) in enumerate(daily):
                bx = ix + 8 + i * slot
                c.put(bx, row, time.strftime("%d/%m", time.localtime(day))[:slot - 1], G3)
                if d["cost"] > 0:
                    c.put(bx, row + 1, f"{d['cost']:.1f}"[:slot - 1], TEXT)
            row += 3

        # Weeks + models side by side
        if row + 2 < bottom:
            half = iw // 2
            top = row
            heading("SEMANAS")
            for start, d in u.get("weeks") or []:
                if row >= bottom:
                    break
                label = "esta semana" if start == (u.get("weeks") or [(0, 0)])[0][0] else time.strftime("sem %d/%m", time.localtime(start))
                c.put(ix, row, f"{label:<12}", G3)
                c.put(ix + 13, row, f"{'$' + format(d['cost'], ',.2f'):>10}", G6 if d["cost"] else G2)
                c.put(ix + 24, row, f"{fmt_tokens(d['tokens']):>7} tok", TEXT)
                row += 1
            row_after_weeks = row
            row = top
            mx = ix + half + 2
            c.put(mx, row, "━━ POR MODELO ", CYAN)
            c.put(mx + 14, row, "─" * max(0, half - 16), G1)
            row += 1
            models = u.get("models") or []
            total_cost = sum(m[1]["cost"] for m in models) or 1
            for model, d in models[:4]:
                if row >= bottom:
                    break
                share = d["cost"] / total_cost * 100
                c.put(mx, row, f"{short_model(model):<11}", WHITE)
                c.put(mx + 12, row, f"{'$' + format(d['cost'], ',.2f'):>10}", G6)
                c.put(mx + 23, row, f"{int(d['messages']):>5} resp", G3)
                self.bar(c, mx + 34, row, max(4, half - 42), share, t, CYAN)
                c.put(mx + 36 + max(4, half - 42), row, f"{share:3.0f}%", G4)
                row += 1
            row = max(row, row_after_weeks) + 1

        # Token breakdown for today
        today = u.get("today") or {}
        if row + 1 < bottom and today:
            heading("TOKENS DE HOJE")
            parts = [("entrada", today.get("input", 0)), ("saída", today.get("output", 0)),
                     ("escrita cache", today.get("cache_write", 0)), ("leitura cache", today.get("cache_read", 0))]
            xx = ix
            for label, val in parts:
                txt = f"{label} {fmt_tokens(val)}"
                c.put(xx, row, label, G3)
                c.put(xx + len(label) + 1, row, fmt_tokens(val), TEXT)
                xx += len(txt) + 4
            row += 1
            if row < bottom - 1:
                since = u.get("since")
                since_txt = time.strftime("%d/%m %H:%M", time.localtime(since)) if since else "-"
                c.put(ix, row, f"sessões: {u.get('active_sessions', 0)} ativa(s) · {u.get('sessions', 0)} no total"
                               f" · registros desde {since_txt}", G3)
            row += 2
        if row < bottom:
            note = "Custo estimado pelo preço de API. Assinatura não cobra por token; limites do plano não ficam no servidor."
            c.put(ix, bottom - 1, note[:iw], G2)

    def draw_portrait(self, c: Canvas, x: int, y: int, w: int, h: int, t: float):
        c.box(x, y, w, h, "▸ IDENTITY // OPERATOR", f"◈ {self.operator}", G2, G5, clear=False)
        c.pulse(x, y, w, h, t * 18)
        rows = self.glitch_rows if time.monotonic() < self.glitch_until else None
        progress = self.portrait.draw(c, x + 1, y + 1, w - 2, h - 2, t, rows)

        # HUD overlays on the bottom border.
        if t < Portrait.REVEAL_END:
            pct = min(100.0, t / Portrait.REVEAL_END * 100)
            label = f" DECODING {pct:5.1f}% "
            c.put(x + 2, y + h - 1, label, G6)
            right = " IDENTITY MATRIX LOADING "
            c.put(x + w - len(right) - 2, y + h - 1, right, AMBER if int(t * 4) % 2 else G4)
        else:
            sw = 16
            c.put(x + 2, y + h - 1, " SCAN ", G4)
            self.bar(c, x + 8, y + h - 1, sw, progress * 100, t, G4)
            c.put(x + 9 + sw, y + h - 1, f" {progress * 100:3.0f}% ", G4)
            verified = int(t * 1.5) % 3 != 0
            label = f" ID {self.operator} · MATCH 99.7% · "
            c.put(x + w - len(label) - 12, y + h - 1, label, G4)
            c.put(x + w - 12, y + h - 1, "[VERIFIED]" if verified else "[        ]", G6)
        if rows:
            c.put(x + w // 2 - 9, y, " SIGNAL INTERFERENCE ", RED)

    @staticmethod
    def ago(ts: float) -> str:
        if not ts:
            return "-"
        sec = max(0, time.time() - ts)
        if sec < 90:
            return "agora"
        if sec < 3600:
            return f"{sec / 60:.0f}min"
        if sec < 86400:
            return f"{sec / 3600:.0f}h"
        return f"{sec / 86400:.0f}d"

    LINK_LABELS = {"cabo": ("CABO", G5), "wifi": ("WI-FI", CYAN), "gw": ("ROUTER", AMBER), "?": ("?", G2)}

    @staticmethod
    def rate_short(v: float) -> str:
        if v < 1:
            return "0"
        for unit in ("B", "K", "M", "G"):
            if v < 1024 or unit == "G":
                return f"{v:.0f}{unit}" if unit in ("B", "K") or v >= 100 else f"{v:.1f}{unit}"
            v /= 1024
        return "?"

    def put_rate(self, c: Canvas, x: int, y: int, rx: float, tx: float, width: int = 13, dim: bool = False):
        """'▼rx ▲tx' from the device's point of view; idle traffic is drawn dim."""
        if width <= 0:
            return
        down, up = self.rate_short(rx), self.rate_short(tx)
        c.put(x, y, ("▼" + down)[:width], G1 if dim or rx < 1 else G6 if rx > 1e6 else G5)
        xx = x + len(down) + 2
        if xx < x + width:
            c.put(xx, y, ("▲" + up)[: x + width - xx], G1 if dim or tx < 1 else G4 if tx > 1e6 else G3)

    def draw_netmap(self, c: Canvas, s: Snapshot, x: int, y: int, w: int, h: int, t: float):
        nm = s.netmap or {}
        tr = s.nettraffic or {}
        tpeers = tr.get("peers") or {}
        lan = nm.get("lan") or {}
        devices = lan.get("devices") or []
        peers = nm.get("tailscale") or []
        nets = nm.get("docker") or []
        scanned = f"varredura há {time.time() - nm['at']:.0f}s" if nm.get("at") else "varrendo..."
        c.box(x, y, w, h, "▸ NETWORK MAP // DISPOSITIVOS · TRÁFEGO", f"W fecha · {scanned}", G2, G5)
        c.pulse(x, y, w, h, t * 18 + 30)
        ix, iw = x + 2, w - 4
        bottom = y + h - 1
        if not nm:
            spin = SPINNER[int(t * 8) % 4]
            c.put(ix, y + 2, f"{spin} varrendo {s.iface}, tailscale e docker...", G4)
            return

        def traffic_of(ips) -> tuple[float, float, list[float], list[str]]:
            rx = tx = 0.0
            hist: list[float] = []
            svc: list[str] = []
            for ip in ips:
                p = tpeers.get(ip)
                if not p:
                    continue
                rx += p["rx"]
                tx += p["tx"]
                ph = p.get("hist") or []
                hist = [a + b for a, b in zip(hist, ph)] if len(hist) == len(ph) else (hist or ph)
                svc += [x_ for x_ in p.get("svc", []) if x_ not in svc]
            return rx, tx, hist, svc

        # Peers that are neither LAN devices nor Tailscale: the server's own internet traffic.
        local_ips = {d["ip"] for d in devices} | {ip for d in devices for ip in d.get("ip6s", [])}
        ts_ips = {p["ip"] for p in peers}
        inet_rx = sum(p["rx"] for ip, p in tpeers.items() if ip not in local_ips and ip not in ts_ips
                      and not ip.startswith(("100.", "fd7a:")))
        inet_tx = sum(p["tx"] for ip, p in tpeers.items() if ip not in local_ips and ip not in ts_ips
                      and not ip.startswith(("100.", "fd7a:")))

        online = [d for d in devices if d["online"]]
        counts = {k: sum(1 for d in online if d["link"] == k) for k in ("cabo", "wifi", "?")}
        ts_on = sum(1 for p in peers if p["online"])
        n_cont = sum(len(n["members"]) for n in nets)
        ifs = tr.get("ifaces") or {}

        # Topology strip: internet -> router -> lan -> this server, with live interface rates.
        row = y + 1
        router = next((d for d in devices if d["role"] == "router"), None)
        me = next((d for d in devices if d["role"] == "server"), None)
        net_ok = s.net4 or bool(s.net6)
        lan_if = ifs.get(s.iface) or {}
        ts_if = ifs.get("tailscale0") or {}
        parts = [("INTERNET", G5 if net_ok else RED), (" ══ ", G2),
                 (f"ROUTER {router['ip'] if router else lan.get('gateway', '?')}", AMBER), (" ══ ", G2),
                 (f"LAN {lan.get('subnet', '')}", TEXT), (" ══ ", G2),
                 (f"{(me or {}).get('name', s.hostname)} {(me or {}).get('ip', s.ip)}", G6),
                 (f" ({(me or {}).get('link', 'cabo')})", G3)]
        if lan_if:
            parts += [(f"  {s.iface} ", G2), (f"▼{self.rate_short(lan_if['rx'])} ▲{self.rate_short(lan_if['tx'])}", G5)]
        if s.tailscale and s.tailscale.get("ip"):
            parts += [(" ┄┄ ", G2), (f"TAILSCALE {s.tailscale['ip']}", CYAN)]
            if ts_if:
                parts += [(f" ▼{self.rate_short(ts_if['rx'])} ▲{self.rate_short(ts_if['tx'])}", CYAN)]
        xx = ix
        for text, style in parts:
            c.put(xx, row, text[: max(0, ix + iw - xx)], style)
            xx += len(text)
        row += 1
        stats = [("●", G5, f"{len(online)} online"), ("", G5, f"cabo {counts['cabo']}"),
                 ("", CYAN, f"wi-fi {counts['wifi']}"), ("", G2, f"? {counts['?']}"),
                 ("", G1, f"offline {len(devices) - len(online)}"),
                 ("", CYAN, f"tailscale {ts_on}/{len(peers)}"), ("", G4, f"docker {n_cont} em {len(nets)} redes"),
                 ("", G3, f"internet do servidor ▼{self.rate_short(inet_tx)} ▲{self.rate_short(inet_rx)}")]
        xx = ix
        for dot, style, text in stats:
            if dot:
                c.put(xx, row, dot, style)
                xx += 2
            c.put(xx, row, text[: max(0, ix + iw - xx)], style if style not in (G1, G2) else G3)
            xx += len(text) + 3
        row += 2

        wide = iw >= 150
        lan_w = iw * 2 // 3 if wide else iw
        side_x = ix + lan_w + 3 if wide else ix
        side_w = iw - lan_w - 3 if wide else iw

        def section(sx: int, sy: int, sw: int, label: str, right: str = "", style: int = G4):
            c.put(sx, sy, "━━ " + label + " ", style)
            fx = sx + len(label) + 4
            fw = sw - len(label) - 4 - (len(right) + 2 if right else 0)
            c.put(fx, sy, "─" * max(0, fw), G1)
            if right:
                c.put(sx + sw - len(right), sy, right, G3)

        # --- LAN table
        section(ix, row, lan_w, "LAN // CABO · WI-FI", f"{len(online)}/{len(devices)}")
        row += 1
        show_vendor = lan_w >= 100
        show_hist = lan_w >= 88
        cols = [("NOME", 20), ("IP", 15)]
        if show_vendor:
            cols.append(("FABRICANTE", 14))
        cols += [("LINK", 7), ("PING", 6), ("TRÁFEGO", 13)]
        if show_hist:
            cols.append(("ATIVIDADE", 10))
        used = 2 + sum(cw + 1 for _, cw in cols)
        cols.append(("CONEXÕES COM O SERVIDOR", max(0, lan_w - used)))
        xx = ix + 2
        for label, cw in cols:
            c.put(xx, row, label[:cw], G2)
            xx += cw + 1
        row += 1
        lan_bottom = bottom - 2 if wide else min(bottom - 2, row + max(3, (bottom - row) // 2))
        blink = int(t * 2) % 2 == 0
        shown = 0
        for d in devices:
            if row >= lan_bottom:
                break
            on = d["online"]
            new = on and bool(d.get("new"))
            rx_srv, tx_srv, hist, svc = traffic_of([d["ip"], *d.get("ip6s", [])]) if d["role"] != "server" else (0, 0, [], [])
            busy = on and (rx_srv + tx_srv) > 10_000
            c.put(ix, row, "●", (AMBER if blink else G6) if new else (G6 if busy and blink else G5) if on else G1)
            name = d["name"] or ("ROUTER" if d["role"] == "router" else "-")
            xx = ix + 2
            for label, cw in cols:
                if label == "NOME":
                    c.put(xx, row, name[:cw], WHITE if d["role"] == "server" else TEXT if on else G2)
                elif label == "IP":
                    c.put(xx, row, (d["ip"] or "-")[:cw], G4 if on else G2)
                elif label == "FABRICANTE":
                    c.put(xx, row, (d["vendor"] or "-")[:cw], G3 if on else G1)
                elif label == "LINK":
                    link, lstyle = self.LINK_LABELS.get(d["link"], ("?", G2))
                    if d.get("est") and d["link"] != "?":
                        link += "~"
                    c.put(xx, row, link[:cw], lstyle if on else G2)
                elif label == "PING":
                    ms = d.get("ms")
                    if on:
                        c.put(xx, row, f"{ms:.0f}ms" if ms is not None else "-", AMBER if ms and ms > 80 else G4)
                    else:
                        c.put(xx, row, self.ago(d.get("last", 0))[:cw], G2)
                elif label == "TRÁFEGO":
                    if d["role"] == "server":
                        c.put(xx, row, "(este)", G2)
                    elif on:
                        self.put_rate(c, xx, row, rx_srv, tx_srv, cw)
                    else:
                        c.put(xx, row, "offline", G1)
                elif label == "ATIVIDADE":
                    if on and hist and d["role"] != "server":
                        c.put(xx, row, heat_strip(hist, cw, max(max(hist), 50_000.0)), G4)
                    elif on:
                        c.put(xx, row, "·" * cw, G0)
                elif cw > 0:
                    if d["role"] == "server":
                        text, st = "este servidor", G2
                    elif d["role"] == "router":
                        text, st = (" ".join(svc) or "gateway da internet"), G3
                    else:
                        text, st = (" ".join(svc) if svc else "-" if on else ""), G4 if svc else G1
                    c.put(xx, row, text[:cw], st)
                xx += cw + 1
            row += 1
            shown += 1
        if shown < len(devices) and row < bottom:
            c.put(ix + 2, row, f"+{len(devices) - shown} dispositivos não cabem na tela", G2)
        note = ("tráfego = só o que passa pelo servidor (▼ o aparelho recebe · ▲ envia); "
                "a internet dos outros aparelhos vai direto ao router · ~ wi-fi estimado")
        c.put(ix, bottom - 1, note[:iw], G1)

        # --- Tailscale and Docker (right column, or below the LAN table)
        row = y + 4 if wide else lan_bottom + 1
        if row < bottom - 1:
            section(side_x, row, side_w, "TAILSCALE // VPN", f"{ts_on}/{len(peers)} online", CYAN)
            row += 1
            for p in peers:
                if row >= bottom - 1:
                    break
                on = p["online"]
                rx_p, tx_p, _, _ = traffic_of([p["ip"]])
                c.put(side_x, row, "●", CYAN if on else G1)
                c.put(side_x + 2, row, p["name"][:18], TEXT if on else G2)
                c.put(side_x + 21, row, p["ip"][:15], G4 if on else G2)
                if on:
                    self.put_rate(c, side_x + 37, row, rx_p, tx_p, 13)
                    info = ("exit · " if p.get("exit") else "") + (p["path"] or p["os"])
                else:
                    c.put(side_x + 37, row, f"visto {self.ago(p['last'])}", G2)
                    info = p["os"]
                c.put(side_x + 51, row, info[: max(0, side_w - 51)], G3 if on else G1)
                row += 1
            row += 1
        conts = tr.get("containers") or {}
        if row < bottom - 1:
            total_rx = sum(v["rx"] for v in conts.values())
            total_tx = sum(v["tx"] for v in conts.values())
            section(side_x, row, side_w, "DOCKER // CONTAINERS",
                    f"▼{self.rate_short(total_rx)} ▲{self.rate_short(total_tx)}", G4)
            row += 1
            net_of = {m[0]: n["name"] for n in nets for m in n["members"]}
            names = sorted({m[0] for n in nets for m in n["members"]},
                           key=lambda nm_: (-(conts.get(nm_, {}).get("rx", 0) + conts.get(nm_, {}).get("tx", 0)), nm_))
            for name in names:
                if row >= bottom - 1:
                    break
                cs = conts.get(name)
                busy = cs and cs["rx"] + cs["tx"] > 10_000
                c.put(side_x, row, "●", G6 if busy else G5)
                c.put(side_x + 2, row, name[:22], TEXT)
                c.put(side_x + 25, row, net_of.get(name, "")[:16], G2)
                if cs:
                    # Container counters are its own eth0: rx = what the container receives.
                    self.put_rate(c, side_x + 42, row, cs["rx"], cs["tx"], max(0, min(13, side_w - 42)))
                else:
                    c.put(side_x + 42, row, "rede do host"[: max(0, side_w - 42)], G2)
                row += 1
            empty = [n["name"] for n in nets if not n["members"]]
            if empty and row < bottom - 1:
                c.put(side_x, row, ("redes vazias: " + " ".join(empty))[:side_w], G1)

    def draw_network(self, c: Canvas, s: Snapshot, x: int, y: int, w: int, h: int, t: float):
        lan = (s.netmap or {}).get("lan") or {}
        n_on = sum(1 for d in lan.get("devices", []) if d["online"])
        c.box(x, y, w, h, "▸ NETWORK // LIVE", f"{s.iface} · {n_on} disp · W mapa" if lan else s.iface, G2, G5)
        c.pulse(x, y, w, h, t * 14 + 60)
        ix, iw = x + 2, w - 4
        row = y + 1
        down_hist, up_hist = s.down_hist[-iw:], s.up_hist[-iw:]
        down_peak = max(down_hist, default=0.0)
        up_peak = max(up_hist, default=0.0)
        if iw >= 70:
            c.put(ix, row, "▼ DOWN", G5)
            c.put(ix + 7, row, f"{fmt_rate(s.down):>11}", G6)
            c.put(ix + 20, row, f"peak {fmt_rate(down_peak)}", G2)
            half = ix + iw // 2 + 2
            c.put(half, row, "▲ UP", G3)
            c.put(half + 5, row, f"{fmt_rate(s.up):>11}", G5)
            c.put(half + 18, row, f"peak {fmt_rate(up_peak)}"[: max(0, ix + iw - half - 18)], G2)
        else:
            c.put(ix, row, "▼", G5)
            c.put(ix + 2, row, fmt_rate(s.down), G6)
            half = ix + iw // 2
            c.put(half, row, "▲", G3)
            c.put(half + 2, row, fmt_rate(s.up), G5)
            c.put(ix + iw - 8, row, f"last {iw}s"[:8], G1)
        row += 1
        graph_h = max(1, (h - 5) // 2)
        for name, hist, peak, styles in (("down", down_hist, down_peak, (G5, G4)), ("up", up_hist, up_peak, (G3, G2))):
            rows = self.graph(name, hist, iw, graph_h, max(peak, 1024.0))
            c.put(ix, row + graph_h - 1, "·" * iw, G0)
            for r, text in enumerate(rows):
                c.put_sparse(ix, row + r, text, styles[0] if r == 0 else styles[1])
            row += graph_h
        if row < y + h - 1:
            blink = int(t * 2) % 2
            all_ok = s.net4 and s.net6 is not False
            dot = G5 if all_ok else AMBER if s.internet else (RED if blink else G0)
            c.put(ix, row, "●", dot)
            xx = ix + 2
            for fam, up, lat in (("v4", s.net4, s.lat4), ("v6", s.net6, s.lat6)):
                if up is None:
                    continue
                c.put(xx, row, fam, G3)
                label = f"{lat:.0f}ms" if up and lat is not None else "DOWN"
                c.put(xx + 3, row, label, TEXT if up else RED)
                xx += 4 + max(len(label), 5) + 1
            sx = ix + 26
            sw = ix + iw - sx
            if sw > 6:
                c.put(sx, row, heat_strip(s.lat_hist, sw, max(50.0, max(s.lat_hist[-sw:], default=0))), G3)

    def draw_icloud(self, c: Canvas, s: Snapshot, x: int, y: int, w: int, h: int, t: float, page: str = ""):
        ic = s.icloud or {}
        status = str(ic.get("status", "starting"))
        blink = int(t * 2) % 2 == 0
        spin = SPINNER[int(t * 10) % 4]
        label, style = {
            "downloading": (f"{spin} BAIXANDO", CYAN),
            "idle": ("● EM DIA", G5),
            "paused": ("● PAUSADO", AMBER),
            "login": ("● PRECISA LOGIN", RED if blink else G0),
            "authenticating": (f"{spin} ENTRANDO NO ICLOUD", AMBER),
        }.get(status, (f"{spin} INICIANDO", G4))
        c.box(x, y, w, h, "▸ ICLOUD // FOTOS", page, RED if status == "login" else G2, G5)
        c.pulse(x, y, w, h, t * 14 + 240)
        ix, iw = x + 2, w - 4
        row, bottom = y + 1, y + h - 1
        c.put(ix, row, label, style)
        route = "iCloud → /data/fotos/icloud"
        c.put(ix + iw - len(route), row, route, G2)
        row += 1
        total, items = int(ic.get("total") or 0), int(ic.get("items") or 0)
        pct = min(100.0, items / total * 100) if total else 0.0
        if row < bottom:
            bw = iw - 8
            self.bar(c, ix, row, bw, self.ease("icloud", pct), t, CYAN if status == "downloading" else G5)
            c.put(ix + bw + 1, row, f"{pct:5.1f}%", G6)
            row += 1
        if row < bottom:
            c.put(ix, row, f"{items} / {total or '?'} itens · {fmt_size(float(ic.get('bytes', 0)))}", TEXT)
            today = f"hoje {fmt_size(float(ic.get('today', 0)))}"
            c.put(ix + iw - len(today), row, today, G4)
            row += 1
        if row < bottom:
            speed = float(ic.get("speed") or 0)
            limit = str(ic.get("limit", "off"))
            c.put(ix, row, f"▼ {fmt_rate(speed)}", G5 if status == "downloading" else G3)
            c.put(ix + 14, row, "sem limite" if limit == "off" else f"limite {limit}", G2 if limit == "off" else AMBER)
            if status == "downloading" and speed > 64 * 1024 and total > items and items:
                avg = float(ic.get("bytes", 0)) / items
                eta = fmt_uptime((total - items) * avg / speed)
                c.put(ix + iw - len(eta) - 4, row, f"ETA {eta}", CYAN)
            row += 1
        if row < bottom:
            if status == "downloading" and ic.get("current"):
                name = str(ic["current"])
                room = iw - 2
                if len(name) > room:
                    name = "~" + name[-(room - 1):]
                c.put(ix, row, "»", G4)
                c.put(ix + 2, row, name, TEXT)
            elif status == "idle" and ic.get("next_check"):
                mins = max(0, int((float(ic["next_check"]) - time.time()) // 60))
                c.put(ix, row, f"próxima verificação em {mins} min", G4)
            elif status == "login":
                c.put(ix, row, "abra http://<servidor>:8090 e faça o login", AMBER)
            elif status == "paused":
                c.put(ix, row, "P retoma o download", G2)
            row += 1
        if row < bottom:
            c.put(ix, row, "P pausar/retomar · C → ICLOUD p/ limite", G1)

    def draw_backup(self, c: Canvas, s: Snapshot, x: int, y: int, w: int, h: int, t: float, page: str = ""):
        b = s.backup or {"status": "not_started"}
        status = str(b.get("status", "not_started"))
        phase = str(b.get("phase", ""))
        run = b.get("run") or {}
        blink = int(t * 2) % 2 == 0
        spin = SPINNER[int(t * 10) % 4]
        if status == "running":
            label, style = {
                "scanning": (f"{spin} SCANNING", AMBER),
                "comparing": (f"{spin} COMPARING", CYAN),
            }.get(phase, (f"{spin} UPLOADING", G5))
        else:
            label, style = {
                "done": ("● IN SYNC", G5),
                "idle": ("● WAITING", G4),
                "paused": ("● PAUSED", AMBER),
                "limit": ("● DAILY LIMIT", AMBER),
                "error": ("● ERROR", RED),
                "stalled": ("● STOPPED", RED if blink else G0),
                "not_started": ("● NOT STARTED", G2),
            }.get(status, (f"● {status.upper()}", G3))
        c.box(x, y, w, h, "▸ BACKUP // GOOGLE DRIVE", page, RED if status in ("error", "stalled") else G2, G5)
        c.pulse(x, y, w, h, t * 14 + 200)
        ix, iw = x + 2, w - 4
        row = y + 1
        bottom = y + h - 1
        dest = str(b.get("dest", "")).split(":", 1)[-1] or "Google Drive"
        c.put(ix, row, label, style)
        route = f"{b.get('source', '/data')} → {dest}"
        c.put(ix + iw - len(route), row, route[: max(0, iw - len(label) - 2)], G2)
        row += 1

        total = float(b.get("source_bytes") or 0)
        done = float(b.get("uploaded_bytes") or 0)
        if b.get("initial_complete"):
            done = total
        pct = min(100.0, done / total * 100) if total else 0.0
        if row < bottom:
            bw = iw - 8
            self.bar(c, ix, row, bw, self.ease("backup", pct), t, G5 if status != "error" else RED)
            c.put(ix + bw + 1, row, f"{pct:5.1f}%", G6)
            row += 1
        if row < bottom:
            c.put(ix, row, f"{fmt_size(done)} / {fmt_size(total)}" if total else "scanning source", TEXT)
            today = (b.get("today") or {})
            if today.get("date") == time.strftime("%Y-%m-%d"):
                label_today = f"today {fmt_size(float(today.get('bytes', 0)))}"
                c.put(ix + iw - len(label_today), row, label_today, G4)
            row += 1
        if row < bottom:
            if status == "running":
                speed = float(run.get("speed") or 0)
                limit = str(run.get("bwlimit") or "")
                c.put(ix, row, f"▲ {fmt_rate(speed)}", G5)
                if self.turbo_on():
                    c.put(ix + 14, row, "TURBO" if blink else "     ", AMBER)
                    c.put(ix + 20, row, "sem limite", AMBER)
                elif limit:
                    c.put(ix + 14, row, ("sem limite" if limit == "off" else f"limite {limit}")[:18], G2)
                if total > done:
                    # Average speed is meaningless while Drive is still creating folders.
                    secs = (total - done) / speed if speed >= 64 * 1024 else None
                    eta = fmt_uptime(secs) if secs is not None and secs < 90 * 86400 else "--"
                    c.put(ix + iw - len(eta) - 4, row, f"ETA {eta}", CYAN)
            else:
                last = b.get("last_run") or {}
                if last:
                    when = time.strftime("%d/%m %H:%M", time.localtime(float(last.get("ended", 0))))
                    c.put(ix, row, f"last run {when} · {last.get('result', '?')}"[:iw], G3)
                else:
                    c.put(ix, row, "waiting for first run", G2)
            row += 1
        if row < bottom:
            files = run.get("transferring") or []
            if status == "running" and files:
                cur = files[int(t / 3) % len(files)]
                name = str(cur.get("name", ""))
                tail = f" {int(cur.get('percentage', 0)):3d}%"
                room = iw - 2 - len(tail)
                if len(name) > room:
                    name = "~" + name[-(room - 1):]
                c.put(ix, row, "»", G4)
                c.put(ix + 2, row, name, TEXT)
                c.put(ix + iw - len(tail), row, tail, G4)
            elif status == "running":
                c.put(ix, row, f"checked {run.get('checks', 0)} files · copied {run.get('transfers', 0)}"[:iw], G3)
            elif status == "paused":
                c.put(ix, row, "resume: rm ~/backup-gdrive/PAUSE"[:iw], G2)
            elif status == "done" and b.get("last_success"):
                when = time.strftime("%d/%m %H:%M", time.localtime(float(b["last_success"])))
                c.put(ix, row, f"in sync since {when}"[:iw], G4)
            row += 1
        if row < bottom:
            errors = int(run.get("errors", 0)) if run else int((b.get("last_run") or {}).get("errors", 0))
            if status == "running":
                info = f"copied {run.get('transfers', 0)} · checked {run.get('checks', 0)}"
            else:
                info = f"{b.get('source_files', 0)} files tracked" if b.get("source_files") else ""
            c.put(ix, row, info[: iw - 12], G2)
            err = f"errors {errors}"
            c.put(ix + iw - len(err), row, err, RED if errors else G2)

    def draw_console(self, c: Canvas, x: int, y: int, w: int, h: int, t: float):
        events = list(self.collector.events)
        c.box(x, y, w, h, f"▸ CONSOLE // {self.user}@{self.collector.hostname}", f"{len(events)} events", G2, G5)
        c.pulse(x, y, w, h, t * 14 + 90)
        ix, iw = x + 2, w - 4
        lines = h - 3
        visible = events[-lines:] if lines > 0 else []
        now = time.monotonic()
        row = y + 1
        tag_w = 8
        for idx, ev in enumerate(visible):
            stamp = time.strftime("%H:%M:%S", time.localtime(ev.wall))
            text = ev.text
            is_last = idx == len(visible) - 1
            if is_last:
                text = text[: max(0, int((now - ev.mono) * 70))]
            age_style = G2 if idx < len(visible) - 3 else G3
            c.put(ix, row, stamp, age_style)
            c.put(ix + 9, row, f"{ev.tag:<{tag_w}}"[:tag_w], ev.style)
            c.put(ix + 10 + tag_w, row, text[: iw - 10 - tag_w], TEXT if ev.style in (G3, G4) else ev.style)
            row += 1
        prompt = f"{self.user}@{self.collector.hostname}:~$ "
        py = y + h - 2
        c.put(ix, py, prompt, G5)
        if int(t * 2.5) % 2 == 0:
            c.put(ix + len(prompt), py, "█", G5)

    HOTKEYS = [
        ("H", "ajuda"), ("C", "config"), ("B", "backup"), ("S", "sync agora"), ("V", "rede"), ("P", "icloud"), ("U", "uso IA"), ("W", "mapa rede"), ("X", "terminal"), ("N", "teste rede"), ("E", "economia"),
        ("T", "apagar tela"), ("M", "chuva"), ("I", "retrato"), ("D", "desligar"), ("R", "reiniciar"),
    ]

    def available_hotkeys(self) -> list[tuple[str, str]]:
        """Only the keys whose feature exists on this server."""
        skip = set()
        if not self.backup_dir:
            skip |= {"B", "S", "V"}
        if not (self.config.get("icloud") or {}).get("container"):
            skip.add("P")
        if not self.config.get("claude"):
            skip.add("U")
        return [(k, lbl) for k, lbl in self.HOTKEYS if k not in skip]

    def turbo_on(self) -> bool:
        return bool(self.backup_dir and (self.backup_dir / "TURBO").exists())

    def draw_keybar(self, c: Canvas, s: Snapshot, t: float):
        """Hotkey strip drawn over the bottom border of the last row of panels."""
        paused = str((s.backup or {}).get("status", "")) == "paused"
        items = []
        for key, label in self.available_hotkeys():
            if key == "B":
                label = "retomar backup" if paused else "pausar backup"
            if key == "E" and self.eco:
                label = "economia ON"
            if key == "P":
                label = "retomar icloud" if str((s.icloud or {}).get("state", "running")) != "running" else "pausar icloud"
            if key == "V":
                label = "limitar rede" if self.turbo_on() else "liberar rede"
            items.append((key, label))
        total = sum(len(lbl) + 5 for _, lbl in items) - 2
        while total > c.w - 4 and len(items) > 3:
            items.pop(-3)
            total = sum(len(lbl) + 5 for _, lbl in items) - 2
        x = max(1, (c.w - total) // 2)
        y = c.h - 1
        c.put(x - 1, y, " ", NONE)
        for key, label in items:
            danger = key in ("D", "R")
            c.put(x, y, f" {key} ", KEYCAP_RED if danger else KEYCAP)
            c.put(x + 3, y, f" {label} ", G4 if not danger else AMBER)
            x += len(label) + 5

    def draw_overlays(self, c: Canvas, s: Snapshot, t: float):
        now = time.monotonic()
        if self.menu.active:
            self.menu.draw(c, t)
        if self.help_open:
            rows = [
                ("C", "configurações: rede, painel, containers"),
                ("B", "pausar / retomar o backup do Google Drive"),
                ("S", "sincronizar o backup agora"),
                ("V", "liberar / limitar a banda do backup (turbo)"),
                ("P", "pausar / retomar o download do iCloud"),
                ("U", "relatório de uso do Claude: auto / retrato / relatório"),
                ("W", "mapa da rede: LAN (cabo / wi-fi), Tailscale, Docker"),
                ("X", "abrir terminal (exit volta ao painel; na web abre no navegador)"),
                ("N", "teste de velocidade da internet (~35 MB)"),
                ("E", "modo economia: 4 fps e sem chuva"),
                ("T", "apagar a tela (qualquer tecla liga de novo)"),
                ("M", "ligar / desligar a chuva Matrix"),
                ("I", "mostrar / esconder o retrato"),
                ("G", "disparar um glitch no retrato"),
                ("L", "redesenhar a tela"),
                ("", ""),
                ("D", "DESLIGAR o servidor (pede confirmação)"),
                ("R", "REINICIAR o servidor (pede confirmação)"),
                ("Q", "fechar o painel (pede confirmação)"),
            ]
            have = {k for k, _ in self.available_hotkeys()}
            rows = [r for r in rows if r[0] not in ("B", "S", "V", "P", "U") or r[0] in have]
            bw, bh = 64, len(rows) + 6
            bx, by = (c.w - bw) // 2, max(1, (c.h - bh) // 2)
            c.box(bx, by, bw, bh, "▸ ATALHOS DO TECLADO", "H fecha", G4, G6)
            c.pulse(bx, by, bw, bh, t * 30, 10)
            for i, (key, text) in enumerate(rows):
                if not key:
                    continue
                danger = key in ("D", "R", "Q")
                c.put(bx + 3, by + 2 + i, f" {key} ", KEYCAP_RED if danger else KEYCAP)
                c.put(bx + 8, by + 2 + i, text, AMBER if danger else TEXT)
            c.put(bx + 3, by + bh - 2, "qualquer tecla fecha esta janela", G2)
        if self.modal:
            left = self.modal["deadline"] - now
            if left <= 0:
                self.modal = None
                self.notify("cancelado (tempo esgotado)", G4, 2)
            else:
                lines = self.modal["lines"]
                bw = max(56, max(len(l) for l in lines) + 8)
                bh = len(lines) + 7
                bx, by = (c.w - bw) // 2, max(1, (c.h - bh) // 2)
                border = RED if int(t * 3) % 2 else AMBER
                c.box(bx, by, bw, bh, f"!! {self.modal['title']} !!", "", border, RED)
                for i, line in enumerate(lines):
                    c.put(bx + 3, by + 2 + i, line, TEXT)
                yy = by + 3 + len(lines)
                c.put(bx + 3, yy, " S ", KEYCAP_RED)
                c.put(bx + 7, yy, "confirmar", WHITE)
                c.put(bx + 19, yy, "qualquer outra tecla cancela", G3)
                c.put(bx + bw - 8, yy, f"{left:4.0f}s", AMBER)
        if self.toast:
            text, style, until = self.toast
            if now > until:
                self.toast = None
            else:
                bw = len(text) + 6
                bx, by = (c.w - bw) // 2, c.h - 14
                c.box(bx, by, bw, 3, "", "", style, style)
                c.put(bx + 3, by + 1, text, style if style != G4 else TEXT)

    def draw_compact(self, c: Canvas, s: Snapshot, t: float):
        alerts = self.alerts(s)
        c.box(0, 0, c.w, 3, "◈ MATRIX SERVER", time.strftime("%H:%M:%S"), RED if alerts else G2, G5)
        c.put(2, 1, f"{s.hostname}  {s.ip}  up {fmt_uptime(s.uptime_s)}"[: c.w - 4], TEXT)
        rest = c.h - 3
        sys_h = min(14, max(9, rest // 2))
        self.draw_system(c, s, 0, 3, c.w, sys_h, t)
        if rest - sys_h >= 5:
            self.draw_services(c, s, 0, 3 + sys_h, c.w, rest - sys_h, t)

    # --- frame

    def update_glitch(self, now: float):
        if now >= self.next_glitch:
            n_rows = len(self.portrait.rows) or 40
            start = random.randrange(max(1, n_rows - 4))
            self.glitch_rows = {}
            for i in range(random.randint(1, 4)):
                self.glitch_rows[start + i] = random.choice([-5, -4, -3, -2, 2, 3, 4, 6])
            if random.random() < 0.4:
                self.glitch_rows[random.randrange(n_rows)] = random.choice([-8, 8])
            self.glitch_until = now + random.uniform(0.15, 0.35)
            self.next_glitch = now + random.uniform(5.0, 11.0)

    def render_canvas(self, elapsed: float) -> Canvas:
        size = shutil.get_terminal_size((120, 40))
        self.width = max(40, size.columns)
        self.height = max(18, size.lines)
        self.rain.resize(self.width, self.height)
        c = Canvas(self.width, self.height)

        s = self.collector.snap
        boot_t = self.boot_duration() if self.boot_enabled and self.boot_lines else 0.0
        booting = elapsed < boot_t
        compact = self.width < 100 or self.height < 30

        header_h = self.HEADER_H
        bottom_h = 9 if self.height >= 44 else 7
        body_y = header_h
        body_h = self.height - header_h - bottom_h
        if self.art_size is None:
            self.art_size = self.portrait.natural_size()
        show_portrait = self.image_enabled and self.width >= 140 and body_h >= 24 and not compact
        if show_portrait:
            portrait_w = min(self.art_size[0] + 4, self.width - 72)
            left_w = self.width - portrait_w
        else:
            portrait_w = 0
            left_w = self.width

        if self.matrix_enabled:
            self.rain.update()
            dim_rect = None
            if show_portrait and not booting:
                dim_rect = (left_w + 1, body_y + 1, left_w + portrait_w - 1, body_y + body_h - 1)
            self.rain.draw(c, dim_rect)

        if booting:
            self.draw_boot(c, elapsed)
            return c

        t = elapsed - boot_t
        now = time.monotonic()
        self.update_glitch(now)

        if compact:
            self.draw_compact(c, s, t)
            return c

        alerts = self.alerts(s)
        self.draw_header(c, s, t, alerts)

        if self.netmap_open:
            self.draw_netmap(c, s, 0, body_y, self.width, body_h, t)
        elif show_portrait or left_w < 150:
            system_h = 15 if body_h >= 38 else max(10, body_h // 2 - 2)
            self.draw_system(c, s, 0, body_y, left_w, system_h, t)
            self.draw_services(c, s, 0, body_y + system_h, left_w, body_h - system_h, t)
        else:
            sys_w = left_w * 2 // 5
            self.draw_system(c, s, 0, body_y, sys_w, body_h, t)
            self.draw_services(c, s, sys_w, body_y, left_w - sys_w, body_h, t)
        if show_portrait and not self.netmap_open:
            if self.usage_page_active(s, t):
                self.draw_usage(c, s, left_w, body_y, portrait_w, body_h, t)
            else:
                self.draw_portrait(c, left_w, body_y, portrait_w, body_h, t)

        bottom_y = self.height - bottom_h
        if s.backup is not None and self.width >= 150:
            net_w = max(48, self.width * 31 // 100)
            backup_w = max(46, self.width * 30 // 100)
            self.draw_network(c, s, 0, bottom_y, net_w, bottom_h, t)
            ic_status = str((s.icloud or {}).get("status", ""))
            if s.icloud and ic_status == "login":
                self.draw_icloud(c, s, net_w, bottom_y, backup_w, bottom_h, t, "!")
            elif s.icloud:
                # Drive and iCloud share the slot, alternating every 8 s.
                if int(t / 8) % 2 == 0:
                    self.draw_backup(c, s, net_w, bottom_y, backup_w, bottom_h, t, "1/2")
                else:
                    self.draw_icloud(c, s, net_w, bottom_y, backup_w, bottom_h, t, "2/2")
            else:
                self.draw_backup(c, s, net_w, bottom_y, backup_w, bottom_h, t)
            self.draw_console(c, net_w + backup_w, bottom_y, self.width - net_w - backup_w, bottom_h, t)
        else:
            net_w = left_w if show_portrait else self.width // 2
            self.draw_network(c, s, 0, bottom_y, net_w, bottom_h, t)
            self.draw_console(c, net_w, bottom_y, self.width - net_w, bottom_h, t)

        self.draw_keybar(c, s, t)
        self.draw_overlays(c, s, t)

        # Wipe transition right after the boot sequence.
        if self.boot_enabled and t < 1.0:
            wipe = int(self.width * (t / 1.0))
            for yy in range(self.height):
                rc, rs = c.chars[yy], c.styles[yy]
                if wipe < self.width:
                    rc[wipe:] = [" "] * (self.width - wipe)
                    rs[wipe:] = [NONE] * (self.width - wipe)
                for k, (ch, st) in enumerate((("█", G6), ("▒", G5), ("░", G3))):
                    xx = wipe - k
                    if 0 <= xx < self.width:
                        rc[xx], rs[xx] = (random.choice(MATRIX_CHARS), G6) if k == 0 and random.random() < 0.3 else (ch, st)
        return c

    def run(self, once: bool = False, at: float | None = None):
        self.collector.prime()
        self.build_boot_lines(self.collector.snap)
        self.collector.emit("SYSTEM", f"matrix dashboard v{VERSION} online", G5)
        self.collector.emit("SYSTEM", f"watching {len(self.collector.snap.containers)} containers, "
                                      f"{len(self.collector.snap.units)} host units", G4)

        if once:
            self.instant = True
            self.collector._slow_once()
            self.collector._docker_ps()
            self.collector.collect_fast(1)
            elapsed = at if at is not None else self.boot_duration() + 12.0
            for _ in range(3):
                self.rain.update()
            print(self.screen.plain(self.render_canvas(elapsed)))
            return

        self.collector.start()
        web_cfg = self.config.get("web") or {}
        if web_cfg.get("enabled"):
            try:
                self.web = WebMirror(web_cfg, lambda: Screen(SGR_LINUX),
                                     {n: rgb for n, rgb in LINUX_PALETTE.items()}, self.collector.hostname)
                self.web.start()
                self.collector.emit("SYSTEM", f"web mirror on port {self.web.port}", G4)
            except OSError as exc:
                self.web = None
                self.collector.emit("ERROR", f"web mirror failed: {exc}"[:80], RED)
        self.setup_terminal()
        try:
            signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop", True))
            signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        except Exception:
            pass

        frame_time = 1.0 / self.fps
        self.start = time.monotonic()
        try:
            while not self.stop:
                started = time.monotonic()
                self.read_key()
                if started - self.last_full_redraw > 120:
                    self.screen.invalidate()
                    self.last_full_redraw = started
                self.update_auto_blank()
                if self.blanked:
                    blank = Canvas(self.width, self.height)
                    sys.stdout.write(self.screen.frame(blank))
                    sys.stdout.flush()
                    # The monitor is dark, but remote viewers still get the dashboard.
                    if self.web and self.web.clients:
                        self.web.publish(self.render_canvas(started - self.start))
                    time.sleep(0.2)
                    continue
                canvas = self.render_canvas(started - self.start)
                sys.stdout.write(self.screen.frame(canvas))
                sys.stdout.flush()
                if self.web and self.web.clients:
                    self.web.publish(canvas)
                spent = time.monotonic() - started
                budget = 0.25 if self.eco else frame_time
                if spent < budget:
                    time.sleep(budget - spent)
        finally:
            self.collector.stop.set()
            self.restore_terminal()


def main():
    parser = argparse.ArgumentParser(description="Matrix-style terminal server dashboard")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--once", action="store_true", help="Render one frame and exit")
    parser.add_argument("--at", type=float, default=None, help="With --once: seconds since start to render")
    parser.add_argument("--detect", action="store_true", help="Print what was auto-detected on this server and exit")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    if args.detect:
        print(autodetect.report(merge_config(config_path)))
        return

    dashboard = Dashboard(config_path)
    dashboard.run(once=args.once, at=args.at)


if __name__ == "__main__":
    main()
