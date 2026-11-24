"""Main loop, keyboard and layout."""

from __future__ import annotations

import json
import math
import os
import random
import select
import shutil
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import VERSION, autodetect
from .claude_usage import short_model
from .collector import Collector
from .config import DEFAULT_CONFIG, merge_config
from .fmt import fmt_rate, fmt_size, fmt_tokens, fmt_uptime
from .models import Container, Snapshot
from .render.canvas import Canvas
from .render.charts import graph_rows, heat_strip
from .render.portrait import Portrait
from .render.rain import MatrixRain
from .render.screen import Screen
from .settings_menu import SettingsMenu
from .shell import run_cmd
from .theme import (
    ALT_SCREEN,
    AMBER,
    BIG_DIGITS,
    CLEAR,
    CYAN,
    DAYS_PT,
    G0,
    G1,
    G2,
    G3,
    G4,
    G5,
    G6,
    HIDE_CURSOR,
    KEYCAP,
    KEYCAP_RED,
    LINUX_PALETTE,
    MAIN_SCREEN,
    MATRIX_CHARS,
    MONTHS_PT,
    NONE,
    RED,
    RESET,
    SGR_256,
    SGR_LINUX,
    SHOW_CURSOR,
    SPINNER,
    TEXT,
    WHITE,
    WRAP_OFF,
    WRAP_ON,
)
from .web_view import WebMirror


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
