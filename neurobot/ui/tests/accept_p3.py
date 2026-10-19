"""Приёмка П3 пульта на цифровом двойнике (docs/ui/PLAN.md §0а, §7 — уменьшенный объём): HTTP и Playwright
(Chromium headless) с Mac через SSH-туннель — тем же путём, что ноутбук оператора к роботу; замеры в ВМ —
neurobot/deploy/verify/k6.py (случай ui) и twin_p3.py.

  .venv-ui/bin/python neurobot/ui/tests/accept_p3.py STEP… --out DIR [--shots DIR] [--vm sim]

  stop    U2: СТОП из пульта — POST /api/stop через туннель (заголовки, cookie, CSRF и issued_at — как у кнопки),
          n = 30, робот едет манёвром жеста 0,15 м/с (k6.py --cases ui --ui-trigger stdin), страница пульта открыта;
          ноль в /cmd_vel p95 ≤ 100 мс, после стопа поехал 0 из 30. Часы ноутбука → ВМ — сдвигом по /api/meta перед
          каждым стопом (запрос с наименьшим RTT из 5, ошибка ≤ RTT/2)
  estop   U3: «Поставить защёлку» кнопкой страницы с подтверждением, n = 10: ESTOP на экране ≤ 0,3 с после щелчка
          (подтверждение safety_gate — по ответу шлюза), кнопки «снять» нет, POST /api/release — 404; снятие
          человеком (pty в ВМ) — экран уходит с ESTOP («не взведён», если успел показаться) ≤ 1 с после ответа
          «release»; счётчики safety_gate до и после
  res     U6: ресурсы шлюза при 0 и 1 вкладке, 3 вкладках одного браузера (один поток SSE) и 3 браузерах (три
          потока): PSS, CPU (ядро ВМ и A57 по 7-zip, budget/cpucal), трафик сокетов шлюза; шаг — прогрев 30 с и
          300 с замера, выборка раз в 5 с; doctor не запускается
  res-summary, k1-summary  пересчитать сводку U6 из raw/u6_*.jsonl или сравнение K1 из raw/u7 (без замера)
  k1soak  U7 + U9: K1 в коротких прогонах (≈12 мин, одно расписание: twin_p3.py k1) — без пульта, с пультом ×2, без
          пульта; сравнение — p50 и p95 против размаха внутри условия и по одним и тем же жестам (ранговый ДИ медианы
          разности); на время прогонов с пультом — 30 мин открытой страницы: события error у EventSource, новые hello,
          «нет связи», наибольший промежуток между сообщениями, ошибки консоли; PSS шлюза раз в 30 с и его наклон
Итоги — DIR/p3_STEP.json, сырые выборки — DIR/raw/, кадры — SHOTS. Числа — стенд в общей ВМ: ориентир.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, HERE)

import p3lib  # noqa: E402
from e2e import text  # noqa: E402
from e2e_live import Tunnel, free_port, wait  # noqa: E402

K6 = os.path.join(ROOT, "neurobot", "deploy", "verify", "k6.py")
P3 = os.path.join(HERE, "twin_p3.py")
VMDIR = "/var/tmp/nb-ui-p3"
SEVENZA = "/home/chosenundead.guest/budget_20261016/tools/p7zip16/usr/lib/p7zip/7za"   # p7zip 16.02 (budget prepare)
LIMITS = {"stop_p95_s": 0.1, "estop_s": 0.3, "release_s": 1.0, "pss_mib": 80.0, "cpu_a57_pct": 5.0,
          "traffic_KBps": 15.0}

# инструментовка страницы (только у теста): события EventSource, промежутки между сообщениями, смена строки
# состояния safety_gate по часам браузера, момент щелчка по кнопкам команд
INIT_JS = r"""
(() => {
  const C = window.__p3 = { open: 0, error: 0, closed: 0, msgs: 0, hello: 0, resumed: 0, maxGap: 0, gaps: [],
    last: 0, st: [], click: null };
  const ES = window.EventSource;
  window.EventSource = class extends ES {
    constructor(url, opts) {
      super(url, opts);
      this.addEventListener('open', () => { C.open += 1; });
      this.addEventListener('error', () => { C.error += 1; if (this.readyState === 2) C.closed += 1; });
      this.addEventListener('message', (e) => {
        const now = performance.now();
        if (C.last) {
          const g = now - C.last;
          if (g > C.maxGap) C.maxGap = g;
          if (g > 1500 && C.gaps.length < 200) C.gaps.push([Date.now() / 1000, g]);
        }
        C.last = now; C.msgs += 1;
        if (e.data.indexOf('"k":"hello"') >= 0) {           // шлюз пишет JSON без пробелов (stream.dumps)
          C.hello += 1;
          if (e.data.indexOf('"resume":true') >= 0) C.resumed += 1;
        }
      });
    }
  };
  document.addEventListener('click', (e) => {
    const b = e.target && e.target.closest && e.target.closest('[data-testid=estop-confirm],[data-testid=stop]');
    if (b) C.click = { id: b.dataset.testid, t: Date.now() / 1000 };
  }, true);
  const rec = () => {
    const a = document.querySelector('[data-testid=status-safety]');
    const s = a ? a.innerText.replace(/\s+/g, ' ').trim() : '';
    const L = C.st; const last = L[L.length - 1];
    if (!last || last.s !== s) { L.push({ t: Date.now() / 1000, s }); if (L.length > 4000) L.splice(0, 1000); }
  };
  new MutationObserver(rec).observe(document, { subtree: true, childList: true, characterData: true });
})();
"""


class Vm:
    """Команды в ВМ (root, окружение ROS) через limactl."""

    def __init__(self, name: str):
        self.name = name

    def cmd(self, script: str, *args: str) -> list:
        inner = "source /opt/ros/jazzy/setup.bash >/dev/null; cd /; PYTHONDONTWRITEBYTECODE=1 python3 {} {}".format(
            shlex.quote(script), " ".join(shlex.quote(a) for a in args))
        return ["limactl", "shell", "--workdir", "/", self.name, "--", "sudo", "bash", "-c", inner]

    def json(self, script: str, *args: str, timeout: float = 300.0) -> dict:
        r = subprocess.run(self.cmd(script, *args), capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError("{} {}: {}".format(os.path.basename(script), " ".join(args),
                                                  (r.stderr or r.stdout)[-600:]))
        return json.loads(r.stdout.strip().splitlines()[-1])

    def sh(self, command: str, timeout: float = 120.0, check: bool = True) -> str:
        r = subprocess.run(["limactl", "shell", "--workdir", "/", self.name, "--", "sudo", "bash", "-c", command],
                           capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError("{}: {}".format(command[:80], (r.stderr or r.stdout)[-400:]))
        return r.stdout

    def link(self) -> str:
        return self.sh("neurobot ui-url").strip().splitlines()[-1]

    def fetch_dir(self, src: str, dst: str) -> None:
        os.makedirs(dst, exist_ok=True)
        self.sh("chmod -R a+rX {}".format(shlex.quote(src)))
        subprocess.run(["limactl", "copy", "-r", "{}:{}".format(self.name, src.rstrip("/") + "/."), dst], check=True,
                       capture_output=True)

    def ui(self, action: str) -> None:
        self.sh("systemctl {} neurobot-ui".format(action))
        if action in ("start", "restart"):
            end = time.monotonic() + 60
            while time.monotonic() < end:
                if self.sh("curl -s -o /dev/null -w '%{http_code}' -H 'Host: 127.0.0.1:18120' "
                           "http://127.0.0.1:18120/healthz", check=False).strip() == "200":
                    return
                time.sleep(0.5)
            raise RuntimeError("пульт не поднялся за 60 с")


class Session:
    """Вход по одноразовой ссылке через туннель и запросы, как у страницы: cookie сессии, Origin, CSRF; одно
    соединение HTTP/1.1 (браузер тоже держит keep-alive)."""

    def __init__(self, port: int, link: str):
        self.port, self.host = port, "127.0.0.1:%d" % port
        self.conn = None
        self.cookie = ""
        st, hdr, _ = self.req("GET", "/login?code=" + link.split("code=", 1)[1].strip())
        if st != 303:
            raise RuntimeError("вход по ссылке: HTTP %d" % st)
        self.cookie = hdr["set-cookie"].split(";", 1)[0]
        st, _, body = self.req("GET", "/api/meta")
        self.csrf = json.loads(body)["csrf"]

    def req(self, method: str, path: str, body=None, headers=None, retry: bool = True):
        if self.conn is None:
            self.conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": self.host}
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(headers or {})
        try:
            self.conn.request(method, path, body=body, headers=h)
            r = self.conn.getresponse()
            data = r.read()
        except (OSError, http.client.HTTPException):
            self.conn.close()
            self.conn = None
            if not retry:
                raise
            return self.req(method, path, body, headers, retry=False)
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data

    def offset(self, n: int = 5):
        """(часы ВМ − часы Mac, RTT) по /api/meta."""
        s = []
        for _ in range(n):
            t1 = time.time()
            _, _, body = self.req("GET", "/api/meta")
            s.append((t1, json.loads(body)["t"], time.time()))
        return p3lib.clock_offset(s)

    def post(self, path: str, doc: dict):
        st, _, body = self.req("POST", path, json.dumps(doc).encode(), {
            "Origin": "http://" + self.host, "X-Neurobot-CSRF": self.csrf, "Content-Type": "application/json"},
            retry=False)
        try:
            return st, json.loads(body or b"{}")
        except ValueError:
            return st, {"raw": body[:200].decode("utf-8", "replace")}


class Browser:
    """Chromium headless: контексты — «браузеры» (у каждого свой вход, свои BroadcastChannel и Web Locks), страницы —
    вкладки; у каждой страницы инструментовка INIT_JS и счёт ошибок консоли."""

    def __init__(self, pw, base: str, vm: Vm):
        self.base, self.vm = base, vm
        self.browser = pw.chromium.launch()
        self.errors = []

    def context(self, theme: str = "light", size=(1920, 1080)):
        ctx = self.browser.new_context(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1,
                                       reduced_motion="reduce", color_scheme=theme, locale="ru-RU")
        ctx.add_init_script(INIT_JS)
        page = self.page(ctx)
        page.goto(self.base + "/login?code=" + self.vm.link().split("code=", 1)[1])
        page.wait_for_selector("[data-testid=source]", timeout=30000)
        return ctx, page

    def page(self, ctx, path: str = ""):
        page = ctx.new_page()
        page.on("console", lambda m: self.errors.append("console %s: %s" % (m.type, m.text))
                if m.type == "error" else None)
        page.on("pageerror", lambda e: self.errors.append("pageerror: %s" % e))
        if path:
            page.goto(self.base + path)
            page.wait_for_selector("[data-testid=source]", timeout=30000)
        return page

    def close(self):
        self.browser.close()


def live_ok(page) -> bool:
    if not page.query_selector("[data-testid=link]") or page.query_selector("[data-testid=nolink]"):
        return False
    return "ЖИВОЙ" in text(page, "[data-testid=source]")


def save(out: str, name: str, doc: dict) -> str:
    path = os.path.join(out, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return path


# ---------------------------------------------------------------------------------------------------------- U2

def step_stop(a, vm: Vm) -> dict:
    from playwright.sync_api import sync_playwright
    home = vm.json(P3, "home")
    vm.sh("mkdir -p {}/u2".format(VMDIR))
    port = free_port()
    tun = Tunnel(a.vm, port)
    tun.up()
    base = "http://127.0.0.1:%d" % port
    rep = {"step": "U2 СТОП из пульта через туннель", "home": home, "tunnel_port": port, "mac": []}
    try:
        with sync_playwright() as pw:
            br = Browser(pw, base, vm)
            ctx, page = br.context()
            page.goto(base + "/#/overview")
            page.wait_for_selector("[data-testid=stop]")
            wait(page, lambda: live_ok(page), 20)
            sess = Session(port, vm.link())
            out_vm = "{}/u2/k6_ui_{}.json".format(VMDIR, time.strftime("%H%M%S"))
            args = ["--out", out_vm, "--runs", str(a.runs), "--cases", "ui", "--ui-trigger", "stdin",
                    "--heading", "-90"]
            with open(os.path.join(a.raw, "u2_k6.log"), "w") as err:
                proc = subprocess.Popen(vm.cmd(K6, *args), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                        text=True, bufsize=1)
                for line in proc.stdout:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("k6") != "ui-ready":
                        continue
                    off, rtt = sess.offset(5)
                    t_post = time.time()
                    try:
                        st, doc = sess.post("/api/stop", {"reason": "приёмка П3: U2 через туннель",
                                                          "issued_at": round(t_post + off, 3)})
                    except (OSError, http.client.HTTPException) as e:
                        st, doc = 0, {"error": str(e)}
                    t_resp = time.time()
                    proc.stdin.write(json.dumps({"t_post": t_post + off, "t_resp": t_resp + off, "status": st,
                                                 "res": doc, "offset_err_s": round(rtt / 2, 4)}) + "\n")
                    proc.stdin.flush()
                    rep["mac"].append({"run": d["run"], "offset_s": round(off, 4), "rtt_s": round(rtt, 4),
                                       "status": st, "http_s": round(t_resp - t_post, 4),
                                       "confirmed_s": doc.get("confirmed_s"), "ok": doc.get("ok")})
                    if d["run"] == 0:
                        page.wait_for_timeout(300)
                        page.screenshot(path=os.path.join(a.shots, "live_overview_after_stop.png"))
                rc = proc.wait(180)
            rep["k6_rc"] = rc
            k6doc = json.loads(vm.sh("cat " + out_vm))
            save(a.raw, "u2_k6_ui.json", k6doc)
            page.wait_for_timeout(1000)
            page.screenshot(path=os.path.join(a.shots, "live_overview_after_30_stops.png"))
            rep["console_errors"] = br.errors[:10]
            br.close()
    finally:
        tun.down()
    u = k6doc.get("ui_stop", {})
    rtts = sorted(m["rtt_s"] for m in rep["mac"])
    rep.update(n=len(u.get("runs", [])), zero_s=u.get("zero_s"), gateway_s=u.get("gateway_s"), stop_s=u.get("stop_s"),
               req_zero_s=u.get("req_zero_s"), call_s=u.get("call_s"), moved_after_zero=u.get("moved_after_zero"),
               copies=u.get("copies"), http_status=u.get("status"), skipped=u.get("skipped"),
               offset_err_max_s=round(rtts[-1] / 2, 4) if rtts else None,
               rtt_p50_s=rtts[len(rtts) // 2] if rtts else None)
    z = rep["zero_s"] or {}
    rep["verdict"] = {"p95_le_100ms": p3lib.verdict(z.get("p95"), LIMITS["stop_p95_s"]),
                      "moved_after_stop_0": rep["moved_after_zero"] == 0, "n30": z.get("n") == a.runs}
    return rep


# ---------------------------------------------------------------------------------------------------------- U3

def first_after(st, t0, pred):
    return next((x for x in st if x["t"] >= t0 and pred(x["s"])), None)


def step_estop(a, vm: Vm) -> dict:
    from playwright.sync_api import sync_playwright
    port = free_port()
    tun = Tunnel(a.vm, port)
    tun.up()
    base = "http://127.0.0.1:%d" % port
    rep = {"step": "U3 защёлка из пульта и снятие человеком", "runs": []}
    rep["counters_before"] = vm.json(P3, "counters")
    try:
        with sync_playwright() as pw:
            br = Browser(pw, base, vm)
            ctx, page = br.context()
            page.goto(base + "/#/safety")
            page.wait_for_selector("[data-testid=estop]", timeout=20000)
            wait(page, lambda: live_ok(page), 20)
            sess = Session(port, vm.link())
            for i in range(a.estops):
                armed = wait(page, lambda: (lambda s: s and "ESTOP" not in s and "unarmed" not in s)(
                    text(page, "[data-testid=status-safety]")), 20, 0.1)
                row = {"run": i, "armed_before": bool(armed)}
                page.click("[data-testid=estop]")
                page.wait_for_selector("[data-testid=estop-confirm]")
                with page.expect_response(lambda r: r.url.endswith("/api/estop"), timeout=8000) as ri:
                    page.click("[data-testid=estop-confirm]")
                resp = ri.value
                body = resp.json()
                shown = wait(page, lambda: "ESTOP" in text(page, "[data-testid=status-safety]"), 5, 0.05)
                p3 = page.evaluate("window.__p3")
                t_click = (p3.get("click") or {}).get("t")
                hit = first_after(p3["st"], t_click or 0, lambda s: "ESTOP" in s) if t_click else None
                row.update(http=resp.status, ok=body.get("ok"), gateway_confirmed_s=body.get("confirmed_s"),
                           screen_s=round(hit["t"] - t_click, 3) if hit else None, shown=bool(shown),
                           result=text(page, "[data-testid=estop-box]")[:100])
                if i == 0:
                    page.wait_for_selector("[data-testid=latch]", timeout=5000)
                    latch = text(page, "[data-testid=latch]")
                    buttons = [b.inner_text() for b in page.query_selector_all("button")]
                    # CSRF — своей сессии страницы (у Session другой вход): иначе 403, а не «маршрута нет»
                    st404 = page.evaluate("""() => fetch('api/meta').then((r) => r.json()).then((m) => fetch(
                        'api/release', {method: 'POST', headers: {'Content-Type': 'application/json',
                        'X-Neurobot-CSRF': m.csrf}, body: '{}'})).then((r) => r.status)""")
                    rep["latch_card"] = {"has_release_command": "sudo neurobot estop release" in latch,
                                         "release_buttons": [b for b in buttons if "сня" in b.lower()
                                                             and "нельзя" not in b.lower()],
                                         "release_route": st404, "text": latch[:240]}
                    page.screenshot(path=os.path.join(a.shots, "live_estop_card.png"))
                    br.errors[:] = [e for e in br.errors if "404" not in e]   # ожидаемый 404 снятия — не ошибка
                page.wait_for_timeout(500)
                off, rtt = sess.offset(5)
                rel = vm.json(P3, "release", "--watch", "2", timeout=90)
                back = wait(page, lambda: "ESTOP" not in text(page, "[data-testid=status-safety]"), 8, 0.05)
                if i == 0 and back:
                    page.screenshot(path=os.path.join(a.shots, "live_after_release.png"))
                page.wait_for_timeout(1500)
                st = page.evaluate("window.__p3.st")
                t_ans = rel.get("t_answer", rel.get("t_exit", 0.0)) - off
                hit = first_after(st, t_ans - rtt, lambda s: "ESTOP" not in s)
                seen = [x["s"] for x in st if x["t"] >= t_ans - rtt][:4]
                graph = rel.get("graph") or []
                g_free = next((g for g in graph if g["t"] >= rel.get("t_answer", 0) and g["state"] != "estop"), None)
                g_unarmed = [g for g in graph if g["reason"] == "unarmed"]
                row.update(graph_release_s=round(g_free["t"] - rel["t_answer"], 3) if g_free and "t_answer" in rel
                           else None, graph_after=[g["state"] + " " + g["reason"] for g in graph
                                                   if g_free and g["t"] >= g_free["t"]][:3],
                           graph_unarmed_msgs=len(g_unarmed))
                row.update(release_rc=rel.get("rc"), release_cli_s=round(rel["t_exit"] - rel["t_answer"], 3)
                           if "t_answer" in rel else None,
                           release_screen_s=round(hit["t"] - t_ans, 3) if hit else None,
                           after_release=seen, unarmed_shown=any("unarmed" in s for s in seen),
                           offset_err_s=round(rtt / 2, 4))
                rep["runs"].append(row)
            rep["console_errors"] = br.errors[:10]
            br.close()
    finally:
        tun.down()
    rep["counters_after"] = vm.json(P3, "counters")
    c0, c1 = rep["counters_before"]["counters"], rep["counters_after"]["counters"]
    rep["counters_delta"] = {k: (c1.get(k) or 0) - (c0.get(k) or 0)
                             for k in ("estop_engaged", "estop_released", "release_denied", "stop_now")}
    rs = rep["runs"]
    scr = sorted(r["screen_s"] for r in rs if r["screen_s"] is not None)
    gw = sorted(r["gateway_confirmed_s"] for r in rs if r["gateway_confirmed_s"] is not None)
    rel_s = sorted(r["release_screen_s"] for r in rs if r["release_screen_s"] is not None)
    g_rel = sorted(r["graph_release_s"] for r in rs if r.get("graph_release_s") is not None)
    rep["summary"] = {"n": len(rs), "screen_s": stats(scr), "gateway_confirmed_s": stats(gw),
                      "release_screen_s": stats(rel_s), "release_graph_s": stats(g_rel),
                      "unarmed_shown": sum(1 for r in rs if r["unarmed_shown"]),
                      "unarmed_in_graph": sum(1 for r in rs if r.get("graph_unarmed_msgs")),
                      "http": sorted({r["http"] for r in rs})}
    rep["verdict"] = {"estop_screen_le_0_3s": bool(scr) and scr[-1] <= LIMITS["estop_s"] and len(scr) == len(rs),
                      "no_release_in_ui": rep.get("latch_card", {}).get("release_route") == 404
                      and not rep.get("latch_card", {}).get("release_buttons"),
                      "release_denied_0": rep["counters_delta"]["release_denied"] == 0,
                      "release_screen_le_1s": bool(rel_s) and rel_s[-1] <= LIMITS["release_s"]
                      and len(rel_s) == len(rs)}
    return rep


def stats(v):
    if not v:
        return {"n": 0}
    return {"n": len(v), "min": v[0], "p50": v[len(v) // 2], "max": v[-1]}


# ---------------------------------------------------------------------------------------------------------- U6

def cpucal(vm: Vm, raw: str) -> dict:
    out = "{}/u6/cpucal_host.json".format(VMDIR)
    vm.sh("mkdir -p {0}/u6 && rm -f {1} && cd {2} && PYTHONDONTWRITEBYTECODE=1 python3 -m neurobot.sim.budget.cpucal "
          "host --bin {3} --cpu 5 --repeats 3 --out {1} >/dev/null".format(VMDIR, out, shlex.quote(ROOT), SEVENZA),
          timeout=400)
    doc = json.loads(vm.sh("cat " + out))
    save(raw, "u6_cpucal_host.json", doc)
    return doc


def samples_of(vm: Vm, path: str):
    return [json.loads(x) for x in vm.sh("cat " + path).splitlines() if x.strip()]


def step_res(a, vm: Vm) -> dict:
    from playwright.sync_api import sync_playwright
    cal = cpucal(vm, a.raw)
    ratio = cal["ratio"]
    port = free_port()
    tun = Tunnel(a.vm, port)
    tun.up()
    base = "http://127.0.0.1:%d" % port
    rep = {"step": "U6 ресурсы шлюза при 0, 1 и 3 вкладках", "cpucal": {k: cal.get(k) for k in (
        "host_single_mips", "host_single_median", "nano_single_mips", "ratio", "load1")}, "steps": {}}
    layouts = (("0", []), ("1", [["overview"]]), ("3tabs", [["overview", "safety", "trends"]]),
               ("3browsers", [["overview"], ["overview"], ["overview"]]))
    try:
        with sync_playwright() as pw:
            br = Browser(pw, base, vm)
            for name, layout in layouts:
                ctxs = []
                for tabs in layout:
                    ctx, page = br.context()
                    page.goto("{}/#/{}".format(base, tabs[0]))
                    pages = [page] + [br.page(ctx, "/#/" + t) for t in tabs[1:]]
                    ctxs.append((ctx, pages))
                for _, pages in ctxs:
                    for p in pages:
                        wait(p, lambda: live_ok(p), 20)
                path = "{}/u6/{}.jsonl".format(VMDIR, name)
                vm.sh("rm -f " + path)
                subprocess.run(vm.cmd(P3, "sample", "--seconds", str(a.warm + a.res_s), "--interval", "5", "--out",
                                      path), check=True, capture_output=True, timeout=a.warm + a.res_s + 120)
                smp = samples_of(vm, path)
                with open(os.path.join(a.raw, "u6_%s.jsonl" % name), "w", encoding="utf-8") as f:
                    f.write("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in smp))
                s = res_step(smp, ratio, a.warm, len(ctxs), sum(len(p) for _, p in ctxs))
                s["live_all"] = all(live_ok(p) for _, ps in ctxs for p in ps)
                if name == "3tabs":
                    for i, p in enumerate(ctxs[0][1]):
                        p.screenshot(path=os.path.join(a.shots, "live_tab%d_of3.png" % (i + 1)))
                for ctx, _ in ctxs:
                    ctx.close()
                rep["steps"][name] = s
                time.sleep(3)
            rep["console_errors"] = br.errors[:10]
            br.close()
    finally:
        tun.down()
    rep["verdict"] = res_verdict(rep["steps"])
    return rep


def res_step(smp, ratio: float, warm: float, browsers: int, pages: int) -> dict:
    """Сводка шага U6 (p3lib.step_summary) и трафик в КБ/с — всего и на поток SSE (поток — один на браузер)."""
    s = p3lib.step_summary(smp, ratio, skip_s=warm)
    s.update(pages=pages, browsers=browsers)
    rate = s["traffic"]["rate_Bps"]
    s["traffic_KBps"] = round(rate / 1000.0, 2) if rate is not None else None
    s["traffic_KBps_per_stream"] = round(rate / 1000.0 / browsers, 2) if rate is not None and browsers else None
    return s


def res_verdict(steps: dict) -> dict:
    pss = max((s["pss_mib"]["max"] or 0) for s in steps.values())
    return {"pss_le_80mib_all_steps": pss <= LIMITS["pss_mib"],
            "cpu_a57_le_5pct_1tab": p3lib.verdict(steps["1"]["cpu_pct_a57"], LIMITS["cpu_a57_pct"]),
            "cpu_a57_le_5pct_3tabs": p3lib.verdict(steps["3tabs"]["cpu_pct_a57"], LIMITS["cpu_a57_pct"]),
            "traffic_le_15KBps_per_stream": p3lib.verdict(steps["1"]["traffic_KBps"], LIMITS["traffic_KBps"]),
            "one_stream_for_3tabs": steps["3tabs"]["clients"] == [1]}


def res_resummarize(a) -> dict:
    """Пересчёт сводок U6 из сырых выборок raw/u6_*.jsonl (тот же прогрев и отношение A57 из cpucal шага): сводка
    чинится без повторного замера, сырые данные — те же."""
    with open(os.path.join(a.out, "p3_res.json"), encoding="utf-8") as f:
        rep = json.load(f)
    for name, old in rep["steps"].items():
        with open(os.path.join(a.raw, "u6_%s.jsonl" % name), encoding="utf-8") as f:
            smp = [json.loads(x) for x in f if x.strip()]
        s = res_step(smp, rep["cpucal"]["ratio"], a.warm, old["browsers"], old["pages"])
        s["live_all"] = old.get("live_all")
        rep["steps"][name] = s
    rep["verdict"] = res_verdict(rep["steps"])
    rep["resummarized"] = round(time.time(), 1)
    return rep


# ---------------------------------------------------------------------------------------------------------- U7, U9

def k1_run(a, vm: Vm, label: str, poll=None) -> dict:
    out_vm = "{}/u7/{}".format(VMDIR, label)
    vm.sh("rm -rf {0} && mkdir -p {1}/u7".format(out_vm, VMDIR))
    with open(os.path.join(a.raw, "u7_%s.log" % label), "w") as err:
        proc = subprocess.Popen(vm.cmd(P3, "k1", "--label", label, "--out", out_vm, "--gestures", str(a.gestures),
                                       "--negatives", str(a.negatives), "--fit"),
                                stdout=subprocess.PIPE, stderr=err, text=True)
        while proc.poll() is None:
            if poll:
                poll()
            time.sleep(2.0)
        o = proc.stdout.read()
    vm.fetch_dir(out_vm, os.path.join(a.raw, "u7", label))
    try:
        return json.loads(o.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError("k1 {}: нет итога (код {}), см. raw/u7_{}.log".format(label, proc.returncode, label))


def step_k1soak(a, vm: Vm) -> dict:
    """Порядок «без, с, с, без»: дрейф стенда за час одинаково ложится на оба условия. Что бы ни случилось — пульт в
    конце снова запущен."""
    try:
        return _k1soak(a, vm)
    finally:
        if vm.sh("systemctl is-active neurobot-ui", check=False).strip() != "active":
            vm.ui("start")


def _k1soak(a, vm: Vm) -> dict:
    from playwright.sync_api import sync_playwright
    rep = {"step": "U7 K1 с пультом и без; U9 30 мин с открытым пультом", "runs": []}
    vm.ui("stop")
    rep["runs"].append(k1_run(a, vm, "off1"))
    vm.ui("start")
    port = free_port()
    tun = Tunnel(a.vm, port)
    tun.up()
    base = "http://127.0.0.1:%d" % port
    soak = {"nolink": 0, "nolink_s": [], "screens": []}
    vm.sh("mkdir -p {0}/u9 && rm -f {0}/u9/samples.jsonl".format(VMDIR))
    sampler = subprocess.Popen(vm.cmd(P3, "sample", "--seconds", str(a.soak_s + 60), "--interval", "30", "--out",
                                      VMDIR + "/u9/samples.jsonl"), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        with sync_playwright() as pw:
            br = Browser(pw, base, vm)
            ctx, page = br.context()
            screens = ["overview", "safety", "trends"]
            page.goto(base + "/#/overview")
            wait(page, lambda: live_ok(page), 20)
            t0 = time.monotonic()
            state = {"nolink_since": None, "next_switch": t0 + 300.0, "i": 0}

            def poll():
                now = time.monotonic()
                bad = page.query_selector("[data-testid=nolink]") is not None
                if bad and state["nolink_since"] is None:
                    state["nolink_since"] = now
                    soak["nolink"] += 1
                elif not bad and state["nolink_since"] is not None:
                    soak["nolink_s"].append(round(now - state["nolink_since"], 1))
                    state["nolink_since"] = None
                if now >= state["next_switch"]:
                    state["i"] += 1
                    scr = screens[state["i"] % 3]
                    page.goto("{}/#/{}".format(base, scr))
                    soak["screens"].append([round(now - t0), scr])
                    state["next_switch"] = now + 300.0
            page.wait_for_timeout(int(max(0.0, a.soak_lead_s) * 1000))
            rep["runs"].append(k1_run(a, vm, "on1", poll))
            rep["runs"].append(k1_run(a, vm, "on2", poll))
            while time.monotonic() - t0 < a.soak_s:
                poll()
                page.wait_for_timeout(2000)
            soak["duration_s"] = round(time.monotonic() - t0, 1)
            soak["p3"] = {k: v for k, v in page.evaluate("window.__p3").items() if k not in ("st",)}
            soak["console_errors"] = list(br.errors[:20])
            soak["live_at_end"] = live_ok(page)
            for theme in ("light", "dark"):
                page.emulate_media(color_scheme=theme)
                for scr in screens:
                    page.goto("{}/?theme={}#/{}".format(base, theme, scr))
                    page.wait_for_selector("[data-testid=source]", timeout=15000)
                    wait(page, lambda: live_ok(page), 10)
                    page.wait_for_timeout(1500)
                    page.screenshot(path=os.path.join(a.shots, "live_%s_%s_1920x1080_after_soak.png" % (scr, theme)))
            br.close()
    finally:
        tun.down()
        sampler.wait(a.soak_s + 300)
    smp = samples_of(vm, VMDIR + "/u9/samples.jsonl")
    with open(os.path.join(a.raw, "u9_samples.jsonl"), "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in smp))
    w = p3lib.window(smp, 120.0)
    soak["pss_mib"] = p3lib.mem_stats(w, "pss_kib")
    soak["anon_mib"] = p3lib.mem_stats(w, "anon_kib")
    soak["pss_slope_mib_h"] = p3lib.slope_per_h([(s["t"] - w[0]["t"], s["pss_kib"] / 1024.0) for s in w
                                                 if s.get("pss_kib") is not None])
    soak["anon_slope_mib_h"] = p3lib.slope_per_h([(s["t"] - w[0]["t"], s["anon_kib"] / 1024.0) for s in w
                                                  if s.get("anon_kib") is not None])
    soak["restarts"] = (smp[-1].get("nrestarts", 0) - smp[0].get("nrestarts", 0)) if smp else None
    soak["clients"] = sorted({s.get("clients") for s in smp if s.get("clients") is not None})
    rep["soak"] = soak
    vm.ui("stop")
    rep["runs"].append(k1_run(a, vm, "off2"))
    vm.ui("start")
    return k1_verdict(a, rep)


def per_gesture(raw: str, label: str) -> dict:
    """K1 по жестам прогона: номер сегмента → t0 → первая ненулевая /cmd_vel, с (без ранних, RTF ≥ 0,95 — как
    metrics.latency)."""
    out = {}
    with open(os.path.join(raw, "u7", label, "gestures.jsonl"), encoding="utf-8") as f:
        for line in f:
            g = json.loads(line) if line.strip() else {}
            if g.get("kind") == "gesture" and "cmd" in (g.get("lat") or {}) and not g.get("early") \
                    and (g.get("rtf") is None or g["rtf"] >= 0.95):
                out[g["seg"]] = g["lat"]["cmd"]
    return out


def k1_verdict(a, rep: dict) -> dict:
    """Сравнение K1 с пультом и без: p50 и p95 прогонов против их размаха внутри условия (правило, заданное до
    замера) и по одним и тем же жестам (разность — ранговый ДИ медианы); итог шага U7 + U9."""
    rep["k1"] = p3lib.compare_k1(rep["runs"])
    runs = {r["label"]: per_gesture(a.raw, r["label"]) for r in rep["runs"]}
    rep["k1_paired"] = p3lib.paired_k1(runs, [r["label"] for r in rep["runs"] if r["ui"] == "on"],
                                       [r["label"] for r in rep["runs"] if r["ui"] == "off"])
    soak = rep["soak"]
    p = soak["p3"]
    rep["verdict"] = {"k1_within_spread": rep["k1"].get("within_spread"),
                      "k1_paired_no_effect": rep["k1_paired"].get("no_effect_seen"),
                      "sse_errors_0": p.get("error") == 0 and p.get("hello") == 1 and soak["nolink"] == 0,
                      "soak_30min": soak["duration_s"] >= 1800 - 5,
                      "pss_slope_computed": soak["pss_slope_mib_h"] is not None}
    return rep


def k1_resummarize(a) -> dict:
    """Пересчёт сравнения K1 из p3_k1soak.json и raw/u7/*/gestures.jsonl (без повторных прогонов)."""
    with open(os.path.join(a.out, "p3_k1soak.json"), encoding="utf-8") as f:
        rep = json.load(f)
    rep = k1_verdict(a, rep)
    rep["resummarized"] = round(time.time(), 1)
    return rep


STEPS = {"stop": step_stop, "estop": step_estop, "res": step_res, "k1soak": step_k1soak}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("steps", nargs="+", choices=list(STEPS) + ["all", "res-summary", "k1-summary"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--shots", default="")
    ap.add_argument("--vm", default="sim")
    ap.add_argument("--runs", type=int, default=30, help="U2: стопов")
    ap.add_argument("--estops", type=int, default=10, help="U3: защёлок")
    ap.add_argument("--warm", type=float, default=30.0, help="U6: прогрев шага, с")
    ap.add_argument("--res-s", type=float, default=300.0, help="U6: замер шага, с")
    ap.add_argument("--gestures", type=int, default=32, help="U7: жестов в прогоне")
    ap.add_argument("--negatives", type=int, default=4, help="U7: отрицательных сегментов")
    ap.add_argument("--soak-s", type=float, default=1800.0, help="U9: страница открыта, с")
    ap.add_argument("--soak-lead-s", type=float, default=30.0, help="U9: до первого прогона с пультом, с")
    a = ap.parse_args()
    a.raw = os.path.join(a.out, "raw")
    a.shots = a.shots or os.path.join(a.out, "shots")
    for d in (a.out, a.raw, a.shots):
        os.makedirs(d, exist_ok=True)
    vm = Vm(a.vm)
    steps = list(STEPS) if "all" in a.steps else a.steps
    code = 0
    if steps == ["res-summary"]:
        print("П3 res: сводка пересчитана → " + save(a.out, "p3_res.json", res_resummarize(a)))
        return 0
    if steps == ["k1-summary"]:
        print("П3 k1soak: сравнение пересчитано → " + save(a.out, "p3_k1soak.json", k1_resummarize(a)))
        return 0
    for name in steps:
        t0 = time.time()
        rep = STEPS[name](a, vm)
        rep.update(started=round(t0, 1), took_s=round(time.time() - t0, 1),
                   label="двойник в общей ВМ: ориентир, не результат робота")
        path = save(a.out, "p3_%s.json" % name, rep)
        bad = [k for k, v in rep.get("verdict", {}).items() if v is not True]
        print("П3 %s: %s → %s" % (name, "ОК" if not bad else "не выполнено: " + ", ".join(bad), path), flush=True)
        code = code or (1 if bad else 0)
    return code


if __name__ == "__main__":
    sys.exit(main())
