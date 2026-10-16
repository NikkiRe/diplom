"""sim-mcp: рассылка уведомлений и жизненный цикл сессий.

* медленная/замороженная сессия не держит часы дольше notify_wait_s и не теряет подписок (раньше: одна задержка
  5 с — и подписки сессии навсегда пропадали), очередь объединяет повторы и ограничена числом ресурсов,
  доставка возобновляется, когда клиент снова читает;
* брошенные сессии (агент убит без DELETE) закрываются по session_idle_timeout, им ничего не отправляется,
  стоимость шага часов от них не растёт;
* валидаторы схем строятся один раз (jsonschema.validate проверял схему на каждом вызове).
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyUrl

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.daygen.generator import DayConfig, generate_day  # noqa: E402
from bench.simmcp.server import SimMCP, _Peer  # noqa: E402
from bench.tests.test_simmcp_server import ServerProc, make_day  # noqa: E402

DAY = generate_day(DayConfig(seed=11, profile="H", source="synthetic", synthetic_situations=2))


class FakeSession:
    """Сессия MCP: send_notification можно «заморозить» (клиент не читает GET-поток)."""

    def __init__(self, blocked: bool = False):
        self.got: list[tuple[str, list[str]]] = []
        self.blocked = blocked
        self.release = anyio.Event()

    async def send_notification(self, n) -> None:
        if self.blocked:
            await self.release.wait()
        p = n.root.params
        self.got.append((str(p.uri), (p.meta.model_extra if p.meta else {}).get("hw/changed")))


class NotifyQueueTest(unittest.TestCase):
    def test_stalled_session_does_not_block_clock_and_keeps_subscriptions(self):
        h, ev = DAY
        times = sorted({e["t"] for e in ev if e["kind"] == "sensor"})[:400]
        all_uris = {f"hw://state/{d['id']}" for d in h["devices"]}

        async def main():
            sim = SimMCP(h, ev, notify_wait_s=0.2)
            async with anyio.create_task_group() as tg:
                sim._tg = tg
                fast, slow = FakeSession(), FakeSession(blocked=True)
                for key, fs in (("fast", fast), ("slow", slow)):
                    p = sim.peers[key] = _Peer(key, None, fs)
                    p.subs = set(all_uris)
                durations = []
                for t in times:
                    t0 = time.monotonic()
                    await sim.advance(t)
                    durations.append(time.monotonic() - t0)
                slow_peer = sim.peers["slow"]
                res = {"durations": durations, "slow_pending": len(slow_peer.pending),
                       "slow_subs": set(slow_peer.subs), "fast_got": len(fast.got), "stalls": sim.stats["notify_stalls"]}
                slow.blocked = False
                slow.release.set()
                await anyio.sleep(0.05)
                res["slow_got_after_release"] = len(slow.got)
                res["slow_pending_after"] = len(slow_peer.pending)
                n0 = len(slow.got)
                nxt = next(e["t"] for e in ev if e["kind"] == "sensor" and e["t"] > times[-1])
                await sim.advance(nxt)
                await anyio.sleep(0.05)
                res["slow_new"] = len(slow.got) - n0
                tg.cancel_scope.cancel()
            return res

        r = anyio.run(main)
        self.assertLess(r["durations"][0], 0.5)                 # первый шаг ждал медленную не дольше notify_wait_s
        self.assertLess(max(r["durations"][1:]), 0.15)          # дальше часы её не ждут
        self.assertEqual(r["stalls"], 1)
        self.assertEqual(r["slow_subs"], {f"hw://state/{d['id']}" for d in DAY[0]["devices"]})   # подписки целы
        self.assertLessEqual(r["slow_pending"], len(DAY[0]["devices"]))   # очередь объединяет повторы
        self.assertGreater(r["fast_got"], 300)                  # быстрая сессия получила всё
        self.assertGreater(r["slow_got_after_release"], 1)      # доставка возобновилась
        self.assertEqual(r["slow_pending_after"], 0)
        self.assertGreater(r["slow_new"], 0)                    # и новые изменения снова доходят

    def test_coalesced_notification_carries_union_of_changed_props(self):
        h, ev = DAY

        async def main():
            sim = SimMCP(h, ev, notify_wait_s=0)
            async with anyio.create_task_group() as tg:
                sim._tg = tg
                fs = FakeSession(blocked=True)
                p = sim.peers["k"] = _Peer("k", None, fs)
                p.subs = {"hw://state/light_kitchen", "hw://state/plug_living_room"}
                await sim.call("write_property", {"device": "plug_living_room", "property": "on", "value": True})
                await anyio.sleep(0.02)          # задача рассылки взяла уведомление и «зависла» на отправке
                await sim.call("write_property", {"device": "light_kitchen", "property": "on", "value": True})
                await sim.call("write_property", {"device": "light_kitchen", "property": "brightness", "value": 30})
                pending = {u: set(v) for u, v in p.pending.items()}
                fs.blocked = False
                fs.release.set()
                await anyio.sleep(0.05)
                tg.cancel_scope.cancel()
            return pending, fs.got

        pending, got = anyio.run(main)
        # первая запись ушла в «зависшую» отправку; две следующие объединены в одно уведомление
        self.assertEqual(pending, {"hw://state/light_kitchen": {"on", "brightness"}})
        self.assertEqual(got[-1], ("hw://state/light_kitchen", ["brightness", "on"]))
        self.assertEqual(len(got), 2)

    def test_inline_state_in_meta(self):
        h, ev = DAY
        sim = SimMCP(h, ev, inline_state=True)
        sim.world.write("light_kitchen", "on", True)
        n = sim._notification("hw://state/light_kitchen", {"on"})
        extra = n.root.params.meta.model_extra
        self.assertEqual(extra, {"hw/changed": ["on"], "device": "light_kitchen", "state": {"on": True}})
        plain = SimMCP(h, ev)._notification("hw://state/light_kitchen", {"on"}).root.params.meta.model_extra
        self.assertEqual(plain, {"hw/changed": ["on"]})                # как у hw-mcp


class ValidatorCacheTest(unittest.TestCase):
    def test_schema_checked_once_not_per_call(self):
        from jsonschema import Draft202012Validator
        h, ev = DAY
        sim = SimMCP(h, ev)
        calls = {"n": 0}
        orig = Draft202012Validator.check_schema

        def counting(schema, *a, **kw):
            calls["n"] += 1
            return orig(schema, *a, **kw)
        Draft202012Validator.check_schema = staticmethod(counting)
        try:
            for _ in range(50):
                asyncio.run(sim.call("read_property", {"device": "light_kitchen", "property": "on"}))
            res = asyncio.run(sim.call("read_property", {"device": "light_kitchen"}))
        finally:
            Draft202012Validator.check_schema = orig
        self.assertEqual(calls["n"], 0)
        self.assertEqual(json.loads(res.content[0].text)["error"], "invalid_args")
        self.assertIn("property", json.loads(res.content[0].text)["detail"])


class AbandonedSessionsTest(unittest.IsolatedAsyncioTestCase):
    """Агент убит без DELETE: сессия без GET-потока не получает уведомлений и закрывается по простою."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.day, cls.h, cls.ev = make_day(cls.tmp, seed=23)
        cls.srv = ServerProc(cls.day, cls.tmp, "--session-idle-timeout", "1.5")

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls._tmp.cleanup()

    async def test_abandoned_sessions_reaped_and_not_notified(self):
        url = self.srv.base + "/mcp"
        n = 8
        for _ in range(n):
            async with streamable_http_client(url, terminate_on_close=False) as (rd, wr, _), \
                    ClientSession(rd, wr) as cs:
                await cs.initialize()
                for d in self.h["devices"][:3]:
                    await cs.subscribe_resource(AnyUrl(f"hw://state/{d['id']}"))
        async with httpx.AsyncClient(base_url=self.srv.base, timeout=10) as http:
            hz = (await http.get("/healthz")).json()
            self.assertEqual(hz["sessions"], n)
            self.assertEqual(hz["subscribed"], n)
            self.assertEqual(hz["streaming"], 0)                     # GET-потоков нет — слать некому
            sensors = sorted({e["t"] for e in self.ev if e["kind"] == "sensor"})
            for t in sensors[:200]:
                await http.post("/control/clock", json={"t": t})
            hz = (await http.get("/healthz")).json()
            self.assertEqual(hz["notify_sent"], 0)
            self.assertEqual(hz["notify_stalls"], 0)
            self.assertLessEqual(hz["pending"], 3 * n)                # очереди ограничены числом подписок
            await asyncio.sleep(2.5)                                  # > session_idle_timeout
            hz = (await http.get("/healthz")).json()
            self.assertEqual((hz["sessions"], hz["sdk_sessions"]), (0, 0))
            self.assertEqual(hz["sessions_closed"], n)


if __name__ == "__main__":
    unittest.main()
