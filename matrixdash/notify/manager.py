"""Turns the active alerts into notifications without spamming.

- a problem is only sent after it lasts `delay` minutes (flapping does not notify)
- while it lasts, a reminder every `repeat` minutes (0 = never)
- when it ends, a "resolvido" message, if the problem had been sent
- everything found in the same check goes in one message
- quiet hours and "silenciar por 1 hora" hold messages; what is still active goes out afterwards
Sending runs in its own thread, so a slow channel never delays data collection.
"""

from __future__ import annotations

import queue
import socket
import threading
import time
from typing import TYPE_CHECKING, Any

from .. import alerts, validators
from ..config import save_user_config
from ..theme import AMBER, CYAN, G5, RED
from .channels import PROBLEM, REMINDER, RESOLVED, TEST, Message, Notifier, from_config

if TYPE_CHECKING:
    from ..collector import Collector

# A problem must stay away this long before "resolvido" is sent (it may come right back).
SETTLE = 30


def in_window(now: str, start: str, end: str) -> bool:
    """'HH:MM' inside [start, end), the window may cross midnight."""
    return start <= now < end if start <= end else (now >= start or now < end)


def minutes(seconds: float) -> str:
    mins = int(seconds // 60)
    return f"{mins // 60}h{mins % 60:02d}" if mins >= 60 else f"{mins} min"


class AlertManager:
    def __init__(self, collector: Collector):
        self.collector = collector
        self.config = collector.config
        self.state: dict[str, dict[str, Any]] = {}
        self.mute_until = 0.0
        self.last_result = ""
        self.queue: queue.Queue[Message] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.host = socket.gethostname()

    @property
    def cfg(self) -> dict[str, Any]:
        return self.config.setdefault("alerts", {})

    def channels(self) -> list[Notifier]:
        return from_config(self.cfg)

    def delay(self) -> float:
        return max(0.0, float(self.cfg.get("delay", 2))) * 60

    def repeat(self) -> float:
        return max(0.0, float(self.cfg.get("repeat", 60))) * 60

    def silenced(self, now: float) -> bool:
        if now < self.mute_until:
            return True
        quiet = self.cfg.get("quiet") or {}
        if not quiet.get("enabled"):
            return False
        hhmm = time.strftime("%H:%M", time.localtime(now))
        return in_window(hhmm, str(quiet.get("from", "23:00")), str(quiet.get("to", "07:00")))

    # --- checks (collector thread, every second)

    def check(self, s, now: float | None = None):
        now = time.time() if now is None else now
        current = {a.key: a for a in alerts.active(s, self.collector.thresholds, self.collector.integrations)}
        for key, alert in current.items():
            st = self.state.setdefault(key, {"text": alert.text, "since": now, "sent": 0.0})
            st["text"], st["gone"] = alert.text, 0.0
        for key, st in self.state.items():
            if key not in current and not st.get("gone"):
                st["gone"] = now
        if not self.channels() or self.silenced(now):
            # Nothing sent now; problems that are still active go out when the silence ends.
            self.state = {k: st for k, st in self.state.items() if k in current or st["sent"]}
            return

        new, remind, resolved = [], [], []
        for key, st in list(self.state.items()):
            if key in current:
                if not st["sent"] and now - st["since"] >= self.delay():
                    new.append(st)
                elif st["sent"] and self.repeat() and now - st["sent"] >= self.repeat():
                    remind.append(st)
            elif now - st["gone"] >= SETTLE or not st["sent"]:
                if st["sent"]:
                    resolved.append(st)
                del self.state[key]
        if not (new or remind or resolved):
            return
        for st in new + remind:
            st["sent"] = now
        self.enqueue(self.compose(new, remind, resolved, now))

    def compose(self, new, remind, resolved, now: float) -> Message:
        lines, items = [], []
        for st in new:
            lines.append(f"🔴 {st['text']}")
            items.append({"key": "", "text": st["text"], "state": "problem", "since": st["since"]})
        for st in remind:
            lines.append(f"🟠 {st['text']} (há {minutes(now - st['since'])})")
            items.append({"key": "", "text": st["text"], "state": "problem", "since": st["since"]})
        for st in resolved:
            lines.append(f"✅ {st['text']} (durou {minutes(st['gone'] - st['since'])})")
            items.append({"key": "", "text": st["text"], "state": "resolved", "since": st["since"]})
        parts = []
        if new:
            parts.append(f"{len(new)} alerta{'s' if len(new) > 1 else ''} novo{'s' if len(new) > 1 else ''}")
        if remind:
            parts.append(f"{len(remind)} ainda ativo{'s' if len(remind) > 1 else ''}")
        if resolved:
            parts.append(f"{len(resolved)} resolvido{'s' if len(resolved) > 1 else ''}")
        kind = PROBLEM if new else REMINDER if remind else RESOLVED
        return Message(self.host, kind, f"{self.host}: " + " · ".join(parts), lines, items)

    # --- sending (own thread)

    def enqueue(self, msg: Message):
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._work, daemon=True)
            self.worker.start()
        self.queue.put(msg)

    def _work(self):
        while True:
            msg = self.queue.get()
            self.deliver(msg)

    def deliver(self, msg: Message) -> list[str]:
        ok, failed = [], []
        for ch in self.channels():
            try:
                ch.send(msg)
                ok.append(ch.name)
            except Exception as exc:
                failed.append(f"{ch.name}: {exc}")
        if ok:
            self.collector.emit("ALERTA", f"{msg.title} → {', '.join(ok)}"[:80], CYAN)
        for err in failed:
            self.collector.emit("ERROR", f"alerta não enviado {err}"[:80], RED)
        self.last_result = (f"enviado {time.strftime('%H:%M')} → {', '.join(ok)}" if ok else "") + \
                           (f" · falhou: {', '.join(f.split(':')[0] for f in failed)}" if failed else "")
        return failed

    def send_test(self):
        active = alerts.active(self.collector.snap, self.collector.thresholds, self.collector.integrations)
        lines = [f"Canais: {', '.join(ch.name for ch in self.channels())}",
                 f"Alertas ativos agora: {len(active)}"] + [f"• {a.text}" for a in active[:10]]
        self.enqueue(Message(self.host, TEST, f"🔔 {self.host}: teste do Matrix Dashboard", lines))

    def mute(self, seconds: float):
        self.mute_until = time.time() + seconds if seconds else 0.0

    # --- settings menu

    def menu_load(self, values: dict[str, str]):
        quiet = self.cfg.get("quiet") or {}
        values["al_delay"] = str(int(float(self.cfg.get("delay", 2))))
        values["al_repeat"] = str(int(float(self.cfg.get("repeat", 60))))
        values["al_quiet"] = "LIGADO" if quiet.get("enabled") else "DESLIGADO"
        values["al_quiet_from"] = str(quiet.get("from", "23:00"))
        values["al_quiet_to"] = str(quiet.get("to", "07:00"))

    def menu(self, m):
        menu = m.menu
        names = [ch.name for ch in self.channels()]
        status = "silenciado até " + time.strftime("%H:%M", time.localtime(self.mute_until)) \
            if time.time() < self.mute_until else self.last_result or (" · ".join(names) if names else "")
        m.header("ALERTAS", status or "nenhum canal: config.json → alerts (docs/INTEGRACOES.md)")
        if not names:
            return

        def save():
            v = menu.values
            quiet = {"enabled": v["al_quiet"] == "LIGADO", "from": v["al_quiet_from"], "to": v["al_quiet_to"]}
            update = {"delay": int(v["al_delay"]), "repeat": int(v["al_repeat"]), "quiet": quiet}
            self.cfg.update(update)
            try:
                save_user_config(menu.d.config_path, lambda raw: raw.setdefault("alerts", {}).update(update))
                menu.message = ("alertas salvos", G5)
            except (OSError, ValueError) as exc:
                menu.message = (f"erro ao salvar: {exc}"[:70], RED)

        def test():
            self.send_test()
            menu.message = (f"teste enviado para {', '.join(names)}", CYAN)

        def toggle_mute():
            muted = time.time() < self.mute_until
            self.mute(0 if muted else 3600)
            menu.message = ("alertas reativados", G5) if muted else ("alertas silenciados por 1 hora", AMBER)
            menu.build()

        m.field("al_delay", "Avisar depois de (min)", "o problema precisa durar isso · 0 = na hora",
                validators.int_range(0, 60), save)
        m.field("al_repeat", "Lembrar a cada (min)", "enquanto durar · 0 = só uma vez", validators.int_range(0, 1440), save)
        m.field("al_quiet", "Horário de silêncio", "segura os avisos no horário abaixo; os ativos saem depois",
                None, save, "choice", ["DESLIGADO", "LIGADO"])
        m.field("al_quiet_from", "  silêncio das", "HH:MM", validators.clock, save)
        m.field("al_quiet_to", "  até as", "HH:MM", validators.clock, save)
        m.action("Enviar mensagem de teste", f"manda um teste para {', '.join(names)}", test, CYAN)
        muted = time.time() < self.mute_until
        m.action("Reativar alertas" if muted else "Silenciar por 1 hora", "vale até reiniciar o painel", toggle_mute,
                 G5 if muted else AMBER)
