"""Background data collection."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import threading
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

import psutil

from . import ROOT, autodetect
from .claude_usage import UsageTracker
from .fmt import fmt_rate, fmt_size
from .models import ClaudeInfo, Container, Event, Snapshot
from .netmap import NetMapper
from .runtime import ContainerRuntime, from_config
from .shell import run_cmd
from .sysinfo import (
    get_local_ip,
    get_temperature,
    http_probe,
    iface_for_ip,
    tcp_latency,
)
from .theme import AMBER, CYAN, G3, G4, G5, RED


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

    The fast loop (cpu, memory, network, containers, claude) builds a new
    Snapshot every second; the slow loop (container stats, HTTP probes,
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
        self.runtimes: list[ContainerRuntime] = from_config(config)
        self.owner: dict[str, ContainerRuntime] = {}
        self.container_stats: dict[str, tuple[float | None, str]] = {}
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
        self.netmapper = NetMapper(config, self.iface, self.runtimes,
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

    def runtime_for(self, name: str) -> ContainerRuntime | None:
        """Runtime that owns a container (the first one when the container is unknown)."""
        return self.owner.get(name) or (self.runtimes[0] if self.runtimes else None)

    def refresh_containers(self):
        """Containers of every runtime, with the stats of the last slow cycle merged in."""
        result: list[Container] = []
        owner: dict[str, ContainerRuntime] = {}
        reachable = False
        for rt in self.runtimes:
            found = rt.containers()
            if found is None:
                continue
            reachable = True
            for ct in found:
                if ct.name.lower() in self.hidden:
                    continue
                running = ct.state == "running"
                cpu, mem = self.container_stats.get(ct.name, (None, ""))
                ct.cpu = cpu if running else None
                ct.mem = mem if running else ""
                owner[ct.name] = rt
                result.append(ct)
        self.docker_ok = reachable
        self.containers = result
        self.owner = owner

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
        rt = self.runtime_for(name)
        return bool(rt and rt.run_sidecar(name, "matrix-tc", script, timeout=30.0))

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
            day_file = ROOT / ".icloud_day.json"
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

        rt = self.runtime_for(name)
        state, started = rt.state(name) if rt else ("missing", "")
        prev = self.icloud or {}
        info: dict[str, Any] = {
            "container": name, "state": state, "items": items, "bytes": total_bytes,
            "total": prev.get("total", 0), "speed": self.icloud_speed if state == "running" else 0.0,
            "today": total_bytes - self.icloud_day[1], "current": prev.get("current", ""),
            "limit": str(cfg.get("limit", "off")), "status": "paused" if state != "running" else "starting",
            "next_check": None,
        }
        if state == "running":
            lines = rt.logs(name, since="3h", tail=4000, timeout=8) if rt else []
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
                full = "\n".join(rt.logs(name, timeout=30)) if rt else ""
                found = re.findall(r"Downloading (\d+) original", full)
                if found:
                    info["total"] = int(found[-1])
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
        stats: dict[str, tuple[float | None, str]] = {}
        reachable = False
        for rt in self.runtimes:
            found = rt.stats()
            if found is not None:
                reachable = True
                stats.update(found)
        if reachable:  # keep the last numbers while the runtime does not answer
            self.container_stats = stats

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
        for check in autodetect.live_checks(self.config.get("checks"), self.runtimes):
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
            self.refresh_containers()
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
        self.refresh_containers()
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
