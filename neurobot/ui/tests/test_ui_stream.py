"""Пульт: формат потока neurobot-ui/1 и бандла (stream.py) — проверка сообщений и типов полей, порядок, запись и
чтение .gz, кадр SSE живого шлюза; подключение в середине потока (hello с active, снимок, кольцевой буфер) даёт
клиенту то же, что поток с начала, — в зеркале Timeline на Python и в самом web/lib/timeline.js через node (если он
есть). Только stdlib, Python 3.8+."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from neurobot.ui import replay, stream  # noqa: E402

SAMPLE = ROOT / "neurobot" / "ui" / "samples" / "demo_hall.json.gz"
TIMELINE_JS = ROOT / "neurobot" / "ui" / "web" / "lib" / "timeline.js"


def bundle(msgs):
    return {"format": stream.BUNDLE_FORMAT, "stream_format": stream.FORMAT, "geometry": {}, "alarms": {},
            "compare_rows": [], "runs": [{"id": "r", "label": "r", "system": "a1", "source": "запись", "t_start": 0,
                                          "t_end": 10, "summary": {}, "stream": msgs}]}


class MessageTest(unittest.TestCase):
    def test_valid_messages(self):
        for m in (
            {"k": "safety", "t": 1.0, "state": "slow", "reasons": ["lidar_blind: …"], "level": 0,
             "min_clearance_m": 0.4},
            {"k": "cmd", "t": 1.0, "topic": "out", "v": 0.15, "w": 0.0, "d_stop_m": 0.1},
            {"k": "gesture", "t": 2.0, "id": "seg1", "outcome": "done"},
            {"k": "k1", "t": 2.0, "last": {"n": 0}, "run": {"n": 0}},
            {"k": "llm", "t": 3.0, "kind": "request", "prompt_tokens": 10},
            {"k": "alarm", "t": 3.0, "key": "obstacle", "code": "obstacle", "prio": 2, "on": True, "text": "…"},
            {"k": "hello", "t": 0.0, "format": stream.FORMAT, "source": "двойник"},
            {"k": "hello", "t": 5.0, "format": stream.FORMAT, "source": "робот", "active": [
                {"key": "shock", "code": "shock", "prio": 1, "text": "удар", "n": 1, "since": 4.0, "src": "safety"}]},
            {"k": "gesture", "t": 2.0, "id": "seg2", "kind": "gesture", "name": None, "conf": None, "rtf_ok": True},
            {"k": "hb", "t": 4.0},
            {"k": "end", "t": 9.0},
        ):
            stream.check(m)

    def test_rejects(self):
        bad = [
            ({"k": "nope", "t": 1}, "неизвестный вид"),
            ({"k": "hb"}, "t — не число"),
            ({"k": "hb", "t": float("nan")}, "t — не число"),
            ({"k": "safety", "t": 1, "state": "slow", "reasons": []}, "нет полей level"),
            ({"k": "safety", "t": 1, "state": "fast", "reasons": [], "level": 0}, "state"),
            ({"k": "cmd", "t": 1, "topic": "/cmd_vel", "v": 0, "w": 0}, "topic"),
            ({"k": "hb", "t": 1, "x": 1}, "лишние поля x"),
            ({"k": "alarm", "t": 1, "key": "a", "code": "a", "prio": 0, "on": True, "text": ""}, "prio"),
            ({"k": "alarm", "t": 1, "key": "a", "code": "a", "prio": 3, "on": True, "text": ""}, "prio"),  # 2 уровня
            ({"k": "alarm", "t": 1, "key": "a", "code": "a", "prio": True, "on": True, "text": ""}, "prio"),
            ({"k": "hello", "t": 1, "format": stream.FORMAT, "source": "стенд"}, "source"),
            ({"k": "hello", "t": 1, "format": "neurobot-ui/2", "source": "робот"}, "format"),
            ({"k": "hello", "t": 1, "format": stream.FORMAT, "source": "робот",
              "active": [{"key": "a", "code": "a", "prio": 1, "text": "x"}]}, "alarm: t"),          # нет since
            # типы и значения, а не только наличие полей
            ({"k": "safety", "t": 1, "state": "slow", "reasons": "lidar_blind", "level": 0}, "reasons"),
            ({"k": "safety", "t": 1, "state": "slow", "reasons": [1], "level": 0}, "reasons"),
            ({"k": "safety", "t": 1, "state": "slow", "reasons": [], "level": 3}, "level"),
            ({"k": "safety", "t": 1, "state": "slow", "reasons": [], "level": 0, "min_clearance_m": "0,4"},
             "min_clearance_m"),
            ({"k": "cmd", "t": 1, "topic": "out", "v": "0.1", "w": 0}, "v"),
            ({"k": "cmd", "t": 1, "topic": "out", "v": 0.1, "w": None}, "w"),
            ({"k": "cmd", "t": 1, "topic": "out", "v": float("inf"), "w": 0}, "v"),
            ({"k": "gesture", "t": 1, "id": "g", "outcome": "ok"}, "outcome"),
            ({"k": "gesture", "t": 1, "id": "g", "early": 0}, "early"),
            ({"k": "units", "t": 1, "up": 1.5, "total": 2}, "up"),
            ({"k": "llm", "t": 1, "kind": "gesture", "prompt_tokens": True}, "prompt_tokens"),
        ]
        for m, why in bad:
            with self.assertRaisesRegex(stream.FormatError, why):
                stream.check(m)

    def test_kinds_spec_is_consistent(self):
        # каждое поле описано типом, который понимает проверка
        for k, (need, opt) in stream.KINDS.items():
            for f, spec in list(need.items()) + list(opt.items()):
                stream._ok(None, spec) if isinstance(spec, tuple) else stream._ok(None, spec.rstrip("?") + "?")

    def test_order(self):
        self.assertEqual(stream.check_stream([{"k": "hb", "t": 1}, {"k": "hb", "t": 1}, {"k": "hb", "t": 2}]), 3)
        with self.assertRaisesRegex(stream.FormatError, "убывает"):
            stream.check_stream([{"k": "hb", "t": 2}, {"k": "hb", "t": 1}])

    def test_sse_frame(self):
        frame = stream.sse({"k": "hb", "t": 1.5}, seq=7).decode("utf-8")
        self.assertEqual(frame, 'id: 7\ndata: {"k":"hb","t":1.5}\n\n')
        with self.assertRaises(ValueError):
            stream.dumps({"k": "hb", "t": float("inf")})


class BundleTest(unittest.TestCase):
    def test_roundtrip_gz_is_deterministic(self):
        doc = bundle([{"k": "hb", "t": 1.0}, {"k": "end", "t": 2.0}])
        with tempfile.TemporaryDirectory() as tmp:
            a, b = os.path.join(tmp, "a.json.gz"), os.path.join(tmp, "b.json.gz")
            stream.write_bundle(a, doc)
            stream.write_bundle(b, doc)
            with open(a, "rb") as fa, open(b, "rb") as fb:
                self.assertEqual(fa.read(), fb.read())
            self.assertEqual(stream.read_bundle(a), json.loads(stream.dumps(doc)))
            plain = os.path.join(tmp, "c.json")
            stream.write_bundle(plain, doc)
            self.assertEqual(stream.check_bundle(stream.read_bundle(plain)), ["r"])

    def test_bundle_checks(self):
        doc = bundle([{"k": "hb", "t": 1.0}])
        doc["runs"][0]["source"] = "робот"
        with self.assertRaisesRegex(stream.FormatError, "source"):
            stream.check_bundle(doc)
        doc = bundle([{"k": "hb", "t": 1.0}])
        doc["runs"].append(dict(doc["runs"][0]))
        with self.assertRaisesRegex(stream.FormatError, "повторяются"):
            stream.check_bundle(doc)
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "x.json")
            with open(p, "w") as f:
                json.dump({"format": "другое"}, f)
            with self.assertRaisesRegex(stream.FormatError, "не бандл"):
                stream.read_bundle(p)



# ---- подключение в середине потока

def fold(msgs, T):
    """Зеркало Timeline на момент T: стоящие тревоги, жесты (слитые по id), последнее сообщение видов состояния."""
    alarms, gestures, last = {}, {}, {}
    for m in msgs:
        if m["t"] > T or m["k"] == "hello":
            continue
        k = m["k"]
        if k == "alarm":
            a = alarms.setdefault(m["key"], {})
            if m["on"] and not a.get("on"):
                a["since"] = m["t"]
            a.update(prio=m["prio"], text=m["text"], on=m["on"], n=m.get("n"))
        elif k == "gesture":
            g = gestures.setdefault(m["id"], {"first": m["t"]})
            g.update({f: v for f, v in m.items() if f not in ("k", "t")})
        elif k in stream.STATE_KINDS or k == "cmd":
            last[k + (":" + m["topic"] if k == "cmd" else "")] = m
    return {k: a for k, a in alarms.items() if a["on"]}, gestures, last


def cut(msgs, b, c):
    """Подключение в момент c с буфером (b, c]: последовательность connect и живой хвост."""
    evicted = stream.State()
    for m in msgs:
        if m["t"] <= b:
            evicted.push(m)
    buf = [m for m in msgs if b < m["t"] <= c]
    seq = stream.connect({"t": c, "source": "двойник"}, evicted, buf)
    return seq, seq + [m for m in msgs if m["t"] > c]


def open_at(msgs, b):
    """id жестов, незакрытых на момент b или начатых позже: их клиент обязан видеть целиком."""
    seen, closed = set(), set()
    for m in msgs:
        if m["k"] != "gesture":
            continue
        if m["t"] <= b:
            seen.add(m["id"])
            if "outcome" in m:
                closed.add(m["id"])
        else:
            seen.add(m["id"])
    return seen - closed


class ConnectTest(unittest.TestCase):
    """Клиент подключился в момент C (буфер шлюза — с B): на любой T ≥ C у него те же стоящие тревоги, жесты,
    незакрытые на B или начатые позже, и последние значения, что у клиента, который видел поток с начала."""

    @classmethod
    def setUpClass(cls):
        from test_ui_replay import make_run
        cls.tmp = tempfile.TemporaryDirectory()
        make_run(cls.tmp.name)
        syn = replay.build_run(cls.tmp.name)["stream"]
        # синтетика: защёлка 1060–1062, blocked 1066–1070, молчание 1081–1084, юнит лежит 1030–1033, жесты 1010–1075
        cls.cases = [(syn, b, c) for b, c in ((1005, 1012), (1011, 1031), (1031.5, 1061), (1061, 1067),
                                              (1068, 1083), (1070, 1100))]
        doc = stream.read_bundle(str(SAMPLE))
        a1 = next(r for r in doc["runs"] if r["system"] == "a1")["stream"]
        t0, t1 = a1[0]["t"], a1[-1]["t"]
        cls.cases += [(a1, t0 + (t1 - t0) * f - stream.BUFFER_S, t0 + (t1 - t0) * f)
                      for f in (0.27, 0.3, 0.52, 0.61, 0.9)]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def times(self, msgs, c):
        end = msgs[-1]["t"]
        return [c, c + 0.5, c + 3.1, c + 30, (c + end) / 2, end]

    def test_snapshot_plus_tail_equals_full_stream(self):
        for msgs, b, c in self.cases:
            seq, client = cut(msgs, b, c)
            self.assertEqual(stream.check_connect(client), len(client))
            want = open_at(msgs, b)
            for T in self.times(msgs, c):
                fa, fg, fl = fold(msgs, T)
                ca, cg, cl = fold(client, T)
                self.assertEqual(ca, fa, (b, c, T))
                self.assertEqual(cl, fl, (b, c, T))
                self.assertEqual({i: cg[i] for i in want if i in cg}, {i: fg[i] for i in want if i in fg}, (b, c, T))
                self.assertTrue(want & set(fg) <= set(cg), (b, c, T))
            # hello.active — тревоги, стоящие в момент подключения
            fa, _, _ = fold(msgs, c)
            self.assertEqual({a["key"]: (a["prio"], a["text"], a["n"], a["since"]) for a in seq[0]["active"]},
                             {k: (a["prio"], a["text"], a["n"], a["since"]) for k, a in fa.items()}, (b, c))

    def test_cases_cover_standing_alarms_and_open_gestures(self):
        # подключение среди стоящих тревог и начатых жестов — иначе тест выше ничего не доказывает
        self.assertTrue(any(self.state(m, b).alarms for m, b, c in self.cases))     # тревога стоит на начало буфера
        self.assertTrue(any(self.state(m, b).gestures for m, b, c in self.cases))   # жест начат до буфера
        self.assertTrue(any(fold(m, c)[0] for m, b, c in self.cases))             # тревога стоит при подключении

    @staticmethod
    def state(msgs, b):
        s = stream.State()
        for m in msgs:
            if m["t"] <= b:
                s.push(m)
        return s

    def test_resume_sends_only_missed(self):
        msgs, b, c = self.cases[3]
        buf = [m for m in msgs if b < m["t"] <= c]
        seq = stream.connect({"t": c, "source": "двойник"}, self.state(msgs, b), buf, resume=True)
        self.assertTrue(seq[0]["resume"])
        self.assertEqual(seq[1:], buf)
        stream.check_connect(seq)
        with self.assertRaisesRegex(stream.FormatError, "hello"):
            stream.check_connect(buf)

    @unittest.skipUnless(shutil.which("node"), "нет node: timeline.js проверяет e2e.py в браузере")
    def test_timeline_js_same(self):
        # тот же опыт на настоящем web/lib/timeline.js: alarms(T) и gestures(T)
        jobs = []
        for msgs, b, c in self.cases[:4] + self.cases[-2:]:
            _, client = cut(msgs, b, c)
            jobs.append({"full": msgs, "client": client, "T": self.times(msgs, c), "want": sorted(open_at(msgs, b))})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jobs.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(jobs, f, ensure_ascii=False)
            code = ("import { Timeline } from %s; import { readFileSync } from 'node:fs';"
                    "const jobs = JSON.parse(readFileSync(process.argv[1], 'utf8'));"
                    "const strip = (g) => { const o = { ...g }; delete o.t; delete o.k; delete o.last; return o; };"
                    "const view = (tl, T, want) => ({ alarms: tl.alarms(T).filter((a) => a.on)"
                    "  .map((a) => [a.key, a.prio, a.text, a.n, a.since]).sort(),"
                    "  gestures: Object.fromEntries(tl.gestures(T).filter((g) => want.includes(g.id))"
                    "  .map((g) => [g.id, strip(g)])) });"
                    "console.log(JSON.stringify(jobs.map((j) => {"
                    "  const a = new Timeline(j.full), b = new Timeline(j.client);"
                    "  return j.T.map((T) => [view(a, T, j.want), view(b, T, j.want)]); })));"
                    % json.dumps(TIMELINE_JS.as_uri()))
            out = subprocess.run(["node", "--input-type=module", "-e", code, path], capture_output=True, text=True,
                                 timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        for job in json.loads(out.stdout):
            for full, client in job:
                self.assertEqual(client, full)


if __name__ == "__main__":
    unittest.main()
