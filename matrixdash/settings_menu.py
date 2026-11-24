"""Settings screen (hotkey C)."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import autodetect
from .config import DEFAULT_CONFIG
from .render.canvas import Canvas
from .render.rain import MatrixRain
from .shell import run_cmd
from .sysinfo import get_local_ip, iface_for_ip, read_ipv4
from .theme import AMBER, CYAN, G1, G2, G3, G4, G5, G6, RED, SPINNER, TEXT, WHITE

if TYPE_CHECKING:
    from .dashboard import Dashboard


NETCFG_HELPER = "/usr/local/sbin/matrix-netcfg"
RATE_RE = re.compile(r"^(off|\d+(\.\d+)?[KMG]?)$", re.I)
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


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
