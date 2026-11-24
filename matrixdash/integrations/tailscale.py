"""Tailscale: connection state, address and key expiry of this server."""

from __future__ import annotations

import json
import shutil
import time
from typing import Any

from ..models import Snapshot
from ..shell import run_cmd
from ..theme import AMBER, G5, RED
from .base import Integration


def installed() -> bool:
    return bool(shutil.which("tailscale"))


def status(timeout: float = 4.0) -> tuple[int, str]:
    """`tailscale status --json` (also read by the network map for the peers)."""
    return run_cmd(["tailscale", "status", "--json"], timeout=timeout)


def parse_time(value: str) -> float | None:
    """Epoch of a Tailscale UTC timestamp; None for empty or the zero date (0001-01-01)."""
    if not value or value.startswith("0001"):
        return None
    try:
        return time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except ValueError:
        return None


class TailscaleIntegration(Integration):
    key = "tailscale"

    def __init__(self, collector):
        super().__init__(collector)
        self.data: dict[str, Any] | None = None
        self.prev_state: str | None = None

    def enabled(self) -> bool:
        return installed()

    def prime(self):
        self.read()

    def collect_slow(self):
        self.read()

    def read(self):
        if not self.enabled():
            return
        code, out = status()
        if code != 0 and not out:
            self.data = {"state": "Unavailable"}
            return
        try:
            data = json.loads(out)
        except ValueError:
            return
        me = data.get("Self") or {}
        ips = data.get("TailscaleIPs") or []
        exp = parse_time(str(me.get("KeyExpiry") or ""))
        days = int((exp - time.time()) // 86400) if exp is not None else None
        self.data = {
            "state": str(data.get("BackendState", "")),
            "ip": next((ip for ip in ips if "." in ip), ips[0] if ips else ""),
            "online": bool(me.get("Online")),
            "expiry_days": days,
        }

    def fill(self, s: Snapshot):
        s.tailscale = self.data

    def detect_events(self, s: Snapshot):
        if s.tailscale is None:
            return
        state = str(s.tailscale.get("state", ""))
        if self.prev_state is not None and state != self.prev_state:
            if state == "Running":
                self.emit("NET", f"tailscale connected ({s.tailscale.get('ip', '')})", G5)
            elif state == "NeedsLogin":
                self.emit("NET", "tailscale logged out - remote access lost", RED)
            else:
                self.emit("NET", f"tailscale {state.lower()}", AMBER)
        self.prev_state = state

    def alerts(self, s: Snapshot) -> list[str]:
        ts = s.tailscale or {}
        if ts.get("state") == "NeedsLogin":
            return ["TAILSCALE DESLOGADO - SEM ACESSO REMOTO"]
        if ts.get("state") not in (None, "", "Running", "Unavailable"):
            return [f"TAILSCALE {str(ts.get('state')).upper()}"]
        if ts.get("expiry_days") is not None and ts["expiry_days"] < 7:
            return [f"TAILSCALE: CHAVE EXPIRA EM {ts['expiry_days']} DIAS"]
        return []

    def boot_lines(self, s: Snapshot) -> list[tuple[str, str]]:
        if s.tailscale and s.tailscale.get("ip"):
            return [("OK" if s.tailscale.get("online") else "WARN", f"tailscale  //  {s.tailscale['ip']}")]
        return []
