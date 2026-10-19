"""neurobot estop: аварийная защёлка safety_gate (PLAN §13.3) на подставном графе — поставить может кто угодно (тема
/neurobot/estop), снять — только человек в терминале со словом подтверждения и только root: запрос снятия — файл в
каталоге root, а не команда в теме (by самозаявлено); формат команды и запроса; хранение защёлки подставным
safety_gate (файл в StateDirectory переживает рестарт) и его снятие тем же файлом. Только stdlib, Python 3.8+."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

from neurobot_deploy import __main__ as cli  # noqa: E402
from neurobot_deploy import estop, rosgraph  # noqa: E402
from neurobot_deploy.config import Config  # noqa: E402

CFG = Config({}, "/e/n.env")
OK = {"state": "ok", "reasons": [], "min_clearance_m": 1.2}
LATCHED = {"state": "estop", "reasons": ["estop: human: демо", "stale_scan: /scan — 0.6 с без данных"],
           "min_clearance_m": 0.4, "estop": {"engaged": True, "since": 1700000000.5, "reasons": ["estop: human: демо"]}}


class FakeGate:
    """safety_gate в графе: держит защёлку, отвечает состоянием; subscribers=0 — его нет."""

    def __init__(self, state=None, subscribers=1, obeys=True):
        self.state, self.subscribers, self.obeys, self.sent, self.requests = state, subscribers, obeys, [], []

    def ros(self) -> estop.Ros:
        return estop.Ros(self.get, self.send, lambda cmd, reason: {"cmd": cmd, "by": "human", "reason": reason},
                         self.wait)

    def get(self, cfg):
        return self.state

    def send(self, cfg, message, done):
        self.sent.append(message)
        if self.subscribers and self.obeys:
            self.state = dict(LATCHED if message["cmd"] == "engage" else OK)
        return self.subscribers, self.state

    def wait(self, cfg, done, timeout):
        return self.state

    def write(self, directory, state, reason):
        """Как write_release: запрос виден safety_gate, он снимает (obeys) или отказывает."""
        self.requests.append((directory, state["estop"]["since"], state["estop"]["reasons"], reason))
        if self.obeys:
            self.state = dict(OK)
        return os.path.join(directory, "нет-такого-запроса.json")


def run(fn, *args) -> tuple:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = fn(*args)
    return code, err.getvalue()


class EngageTest(unittest.TestCase):
    def test_anyone_can_engage(self):
        gate = FakeGate(dict(OK))
        code, err = run(estop.engage, CFG, gate.ros(), "демо")
        self.assertEqual(code, 0)
        self.assertEqual(gate.sent, [{"cmd": "engage", "by": "human", "reason": "демо"}])
        self.assertIn("neurobot-estop: safety: estop_engaged: защёлка стоит: состояние estop", err)

    def test_no_gate_or_not_confirmed(self):
        code, err = run(estop.engage, CFG, FakeGate(subscribers=0).ros(), "")
        self.assertEqual(code, 1)
        self.assertIn("estop_failed: никто не слушает /neurobot/estop", err)
        self.assertIn("sudo systemctl stop neurobot-safety", err)
        code, err = run(estop.engage, CFG, FakeGate(dict(OK), obeys=False).ros(), "")
        self.assertEqual(code, 1)
        self.assertIn("safety_gate не подтвердил защёлку: состояние ok", err)


class ReleaseTest(unittest.TestCase):
    def release(self, gate, tty=True, answer="release\n", euid=0):
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return answer
        code, err = run(estop.release, CFG, gate.ros(), "убрали коробку", tty, ask, euid, gate.write)
        return code, err, asked

    def test_only_human_at_terminal_and_root(self):
        gate = FakeGate(dict(LATCHED))
        code, err, asked = self.release(gate, tty=False)
        self.assertEqual((code, gate.requests, asked), (1, [], []))
        self.assertIn("estop_denied: снять защёлку может только человек в терминале", err)
        code, err, asked = self.release(gate, euid=1000)
        self.assertEqual((code, gate.requests, asked), (1, [], []))
        self.assertIn("estop_denied: снять защёлку может только root: sudo neurobot estop release", err)

    def test_confirmation_word_then_request_file(self):
        gate = FakeGate(dict(LATCHED))
        code, err, asked = self.release(gate, answer="да\n")
        self.assertEqual((code, gate.requests), (1, []))
        self.assertIn("estop_cancelled", err)
        self.assertIn("причины: estop: human: демо", asked[0])
        code, err, _ = self.release(gate)
        self.assertEqual(code, 0)
        self.assertEqual(gate.sent, [])  # по теме release не уходит
        self.assertEqual(gate.requests, [("/run/neurobot-estop", 1700000000.5, ["estop: human: демо"],
                                          "убрали коробку")])
        self.assertIn("estop_released: защёлка снята: состояние ok", err)

    def test_request_file_is_root_only(self):
        me = os.getuid()  # в проде — root (0); тест — от своего пользователя
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "estop")
            path = estop.write_release(d, LATCHED, "проверка", owner=me)
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            self.assertEqual((doc["since"], doc["reasons"], doc["reason"]),
                             (1700000000.5, ["estop: human: демо"], "проверка"))
            self.assertEqual((os.stat(d).st_mode & 0o777, os.stat(path).st_mode & 0o777), (0o755, 0o644))
            self.assertEqual(os.listdir(d), ["release.json"])  # временный файл переименован
            with self.assertRaisesRegex(estop.ReleaseError, "не каталог root"):
                estop.write_release(d, LATCHED, "", owner=me + 1)
            os.chmod(d, 0o777)
            with self.assertRaisesRegex(estop.ReleaseError, "не каталог root"):
                estop.write_release(d, LATCHED, "", owner=me)

    def test_nothing_to_release_or_no_gate(self):
        code, err, asked = self.release(FakeGate(dict(OK)))
        self.assertEqual((code, asked), (0, []))
        self.assertIn("estop_absent", err)
        code, err, _ = self.release(FakeGate(None))
        self.assertEqual(code, 1)
        self.assertIn("нет /neurobot/safety_state", err)
        code, err, _ = self.release(FakeGate(dict(LATCHED), obeys=False))
        self.assertEqual(code, 1)
        self.assertIn("safety_gate не снял защёлку", err)
        self.assertIn("журнале neurobot-safety", err)


class MessagesTest(unittest.TestCase):
    def test_estop_command_is_json_with_id(self):
        a, b = rosgraph.estop_message("engage", "коробка на пути"), rosgraph.estop_message("engage", "")
        self.assertEqual({k: a[k] for k in ("cmd", "by", "reason")},
                         {"cmd": "engage", "by": "human", "reason": "коробка на пути"})
        self.assertNotEqual(a["id"], b["id"])  # повтор одной команды safety_gate отличит от новой
        self.assertEqual(json.loads(json.dumps(a, ensure_ascii=False)), a)


@unittest.skipIf(importlib.util.find_spec("rclpy"), "rclpy есть: путь без ROS здесь не проверить")
class CliTest(unittest.TestCase):
    def test_without_ros_estop_names_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.path.join(tmp, "n.env")
            with open(env, "w", encoding="utf-8") as f:
                f.write("NEUROBOT_ROS_SETUP={}\n".format(os.path.join(tmp, "нет.bash")))
            code, err = run(cli.main, ["--env", env, "estop", "engage"])
        self.assertEqual(code, 1)
        self.assertIn("neurobot-estop: safety: ros_unavailable: rclpy недоступен", err)


class LatchFileTest(unittest.TestCase):
    """Подставной safety_gate хранит защёлку в $STATE_DIRECTORY/estop.json: рестарт процесса её не снимает."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("fake_component", str(DEPLOY / "fakes/fake_component.py"))
        cls.fake = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fake)

    def test_roundtrip_missing_and_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "estop.json")
            self.assertEqual(self.fake.load_latch(path), [])  # файла нет — защёлки не было
            self.fake.save_latch(path, ["estop: human: демо"])
            self.assertEqual(self.fake.load_latch(path), ["estop: human: демо"])
            self.assertEqual(os.listdir(tmp), ["estop.json"])  # временный файл переименован
            self.fake.save_latch(path, [])
            self.assertEqual(self.fake.load_latch(path), [])
            for text in ("{", '{"engaged": true}', "[]"):
                with self.subTest(text=text):
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(text)
                    (reason,) = self.fake.load_latch(path)  # не читается — стоим до release человеком
                    self.assertIn("не читается", reason)
        self.assertEqual(self.fake.load_latch(""), [])  # без StateDirectory — только память

    def test_release_request_like_safety_gate(self):
        me = os.getuid()
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "estop")
            self.assertIsNone(self.fake.release_request(d, me))
            estop.write_release(d, LATCHED, "", owner=me)
            self.assertEqual(self.fake.release_request(d, me)["since"], 1700000000.5)
            self.assertIsNone(self.fake.release_request(d, me + 1))  # не root — не верим
            os.chmod(d, 0o777)
            self.assertIsNone(self.fake.release_request(d, me))


if __name__ == "__main__":
    unittest.main()
