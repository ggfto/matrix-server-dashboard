"""Alert notifications: when messages go out, what they say, and the request each channel makes."""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scenes

from matrixdash.notify import channels
from matrixdash.notify.channels import PROBLEM, RESOLVED, Message
from matrixdash.notify.manager import AlertManager, in_window

T = scenes.WALL


class Recorder(channels.Notifier):
    name = "fake"

    def __init__(self):
        super().__init__({})
        self.sent: list[Message] = []

    def configured(self):
        return True

    def send(self, msg):
        self.sent.append(msg)


def setup(config=None):
    random.seed(1)
    d = scenes.demo.make_dashboard(160, 46)
    d.config["alerts"] = {"delay": 2, "repeat": 60, "quiet": {"enabled": False}, **(config or {})}
    mgr = AlertManager(d.collector)
    rec = Recorder()
    mgr.channels = lambda: [rec]
    mgr.enqueue = lambda msg: mgr.deliver(msg)  # synchronous in tests
    s = d.collector.snap
    s.cpu_hist = []
    s.ram = 40.0
    return d, mgr, rec, s


class Manager(unittest.TestCase):
    def test_delay_reminder_and_resolution(self):
        d, mgr, rec, s = setup()
        s.ram = 95.0
        mgr.check(s, T)
        mgr.check(s, T + 60)
        self.assertEqual(rec.sent, [])  # shorter than the 2 min delay
        mgr.check(s, T + 125)
        self.assertEqual(len(rec.sent), 1)
        self.assertEqual(rec.sent[0].kind, PROBLEM)
        self.assertIn("🔴 RAM 95%", rec.sent[0].lines)
        mgr.check(s, T + 600)
        self.assertEqual(len(rec.sent), 1)  # no repeat before 60 min
        mgr.check(s, T + 125 + 3600)
        self.assertEqual(rec.sent[-1].lines, ["🟠 RAM 95% (há 1h02)"])
        s.ram = 40.0
        mgr.check(s, T + 4000)
        mgr.check(s, T + 4010)
        self.assertEqual(len(rec.sent), 2)  # resolution waits until the problem stays away
        mgr.check(s, T + 4031)
        self.assertEqual(rec.sent[-1].kind, RESOLVED)
        self.assertEqual(rec.sent[-1].lines, ["✅ RAM 95% (durou 1h06)"])
        self.assertEqual(mgr.state, {})

    def test_flapping_problem_is_never_sent(self):
        d, mgr, rec, s = setup()
        for i in range(5):
            s.ram = 95.0 if i % 2 == 0 else 40.0
            mgr.check(s, T + i * 50)
        self.assertEqual(rec.sent, [])

    def test_alerts_found_together_go_in_one_message(self):
        d, mgr, rec, s = setup({"delay": 0})
        s.ram = 95.0
        s.units = [("NGINX", "nginx", "failed"), ("SSH", "ssh", "active")]
        mgr.check(s, T)
        self.assertEqual(len(rec.sent), 1)
        self.assertEqual(rec.sent[0].title, f"{mgr.host}: 2 alertas novos")
        self.assertEqual(sorted(rec.sent[0].lines), ["🔴 NGINX FAILED", "🔴 RAM 95%"])

    def test_changing_numbers_keep_the_same_alert(self):
        d, mgr, rec, s = setup({"delay": 0})
        s.ram = 91.0
        mgr.check(s, T)
        s.ram = 97.0
        mgr.check(s, T + 30)
        self.assertEqual(len(rec.sent), 1)

    def test_quiet_hours_hold_then_send(self):
        d, mgr, rec, s = setup({"delay": 0, "quiet": {"enabled": True, "from": "14:00", "to": "15:00"}})
        s.ram = 95.0
        mgr.check(s, T)  # 14:32 UTC, inside the window
        self.assertEqual(rec.sent, [])
        mgr.check(s, T + 3600)  # 15:32
        self.assertEqual(len(rec.sent), 1)

    def test_mute(self):
        d, mgr, rec, s = setup({"delay": 0})
        mgr.mute_until = T + 3600
        s.ram = 95.0
        mgr.check(s, T)
        self.assertEqual(rec.sent, [])
        mgr.check(s, T + 3601)
        self.assertEqual(len(rec.sent), 1)

    def test_nothing_without_channels(self):
        d, mgr, rec, s = setup({"delay": 0})
        mgr.channels = lambda: []
        s.ram = 95.0
        mgr.check(s, T)
        self.assertEqual(rec.sent, [])

    def test_failures_become_console_events(self):
        d, mgr, rec, s = setup()

        class Broken(Recorder):
            name = "broken"

            def send(self, msg):
                raise RuntimeError("HTTP 401")
        mgr.channels = lambda: [rec, Broken()]
        d.collector.events.clear()
        failed = mgr.deliver(Message("h", PROBLEM, "t", ["x"]))
        self.assertEqual(failed, ["broken: HTTP 401"])
        self.assertEqual([e.tag for e in d.collector.events], ["ALERTA", "ERROR"])

    def test_window(self):
        self.assertTrue(in_window("23:30", "23:00", "07:00"))
        self.assertTrue(in_window("06:59", "23:00", "07:00"))
        self.assertFalse(in_window("12:00", "23:00", "07:00"))


class Menu(unittest.TestCase):
    def test_section_saves_and_tests(self):
        d, mgr, rec, s = setup()
        d.collector.notifier = mgr
        with tempfile.TemporaryDirectory() as tmp:
            d.config_path = Path(tmp) / "config.json"
            d.config_path.write_text(json.dumps({"alerts": {"telegram": {"token": "t", "chat_id": "1"}}}))
            menu = d.menu
            menu.open()
            keys = [it.get("key") for it in menu.items]
            self.assertIn("al_delay", keys)
            menu.values["al_delay"] = "5"
            next(it for it in menu.items if it.get("key") == "al_delay")["save"]()
            saved = json.loads(d.config_path.read_text())["alerts"]
            self.assertEqual((saved["delay"], saved["telegram"]["token"]), (5, "t"))
            next(it for it in menu.items if it.get("label") == "Enviar mensagem de teste")["action"]()
            self.assertEqual(rec.sent[-1].kind, "test")


class Storage(unittest.TestCase):
    def test_saving_keeps_private_permissions(self):
        import os
        import stat
        from matrixdash.config import save_user_config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{}")
            os.chmod(path, 0o600)
            save_user_config(path, lambda raw: raw.update(theme="gelo"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class Channels(unittest.TestCase):
    def capture(self, notifier, msg=None):
        calls = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def urlopen(req, timeout=10):
            calls.append((req.full_url, dict(req.header_items()), json.loads(req.data)))
            return Resp()
        with mock.patch.object(channels.urllib.request, "urlopen", urlopen):
            notifier.send(msg or Message("srv", PROBLEM, "srv: 1 alerta novo", ["🔴 RAM 95%"],
                                         [{"text": "RAM 95%", "state": "problem", "since": T}]))
        return calls[0]

    def test_telegram(self):
        url, _, body = self.capture(channels.Telegram({"token": "123:ABC", "chat_id": "-100"}))
        self.assertEqual(url, "https://api.telegram.org/bot123:ABC/sendMessage")
        self.assertEqual(body["chat_id"], "-100")
        self.assertEqual(body["text"], "srv: 1 alerta novo\n🔴 RAM 95%")

    def test_discord(self):
        url, _, body = self.capture(channels.Discord({"webhook": "https://discord.com/api/webhooks/1/x"}))
        self.assertEqual(url, "https://discord.com/api/webhooks/1/x")
        embed = body["embeds"][0]
        self.assertEqual((embed["title"], embed["description"], embed["color"]),
                         ("srv: 1 alerta novo", "🔴 RAM 95%", 0xE0302A))

    def test_ntfy_json_publish_with_token(self):
        url, headers, body = self.capture(channels.Ntfy({"topic": "meu-servidor", "token": "tk_1"}))
        self.assertEqual(url, "https://ntfy.sh")
        self.assertEqual(headers["Authorization"], "Bearer tk_1")
        self.assertEqual((body["topic"], body["priority"], body["tags"]), ("meu-servidor", 4, ["rotating_light"]))

    def test_webhook(self):
        url, headers, body = self.capture(channels.Webhook({"url": "http://n8n.local/hook", "headers": {"X-Key": "k"}}))
        self.assertEqual(headers["X-key"], "k")
        self.assertEqual((body["host"], body["kind"], body["alerts"][0]["state"]), ("srv", "problem", "problem"))

    def test_only_configured_channels(self):
        found = channels.from_config({"telegram": {"token": "x"}, "discord": {"webhook": "https://d"},
                                      "ntfy": {"topic": ""}, "webhook": {"url": "ftp://no"}})
        self.assertEqual([c.name for c in found], ["discord"])


if __name__ == "__main__":
    unittest.main()
