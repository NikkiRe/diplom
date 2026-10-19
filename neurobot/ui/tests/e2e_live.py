"""E2E живого режима пульта на цифровом двойнике (Playwright, Chromium headless; ВМ sim с пакетом и пультом, README).

Браузер на Mac ходит к шлюзу в ВМ тем же путём, что к роботу: SSH-туннель (`ssh -F ~/.lima/sim/ssh.config -L`) на
свободный локальный порт — не 18120, проверка «любой порт туннеля» — и одноразовая ссылка `sudo neurobot ui-url`.
Проверяется по шагам (итоги и числа — в DIR/e2e_live.json, кадры — в DIR):
  вход — ссылка одноразовая, без cookie /api/* — 401, чужой Host — 403, POST без CSRF — 403;
  экраны — источник «двойник» и знак ЖИВОЙ на каждом, каркас в окне, все знаки из своего шрифта, ошибок 0;
  живой поток — связь < 2 с, состояние safety_gate, точки скана, юниты, память;
  СТОП — робот едет заявкой агента, кнопка → 5 копий с одним id в /neurobot/stop_now, ноль в /cmd_vel, после стопа
  не поехал (заявка шла дальше), на экране — 0, итог «принят safety_gate» (его счётчик стопов вырос);
  защёлка — «Поставить» с подтверждением → ESTOP на экране, карточка с `sudo neurobot estop release`, кнопки
  «снять» нет, POST /api/release — 404; снятие человеком (pty в ВМ) → на экране не ESTOP;
  жесты — новый таймлайн gesturegen: строки жестов с Δ «конец жеста → команда» (ref t_end);
  режимы — «Запись» и обратно; одна SSE на браузер — две вкладки, у шлюза 1 поток; doctor по кнопке; ресурсы юнита;
  нет связи — туннель оборван 10 раз: штриховка «нет связи» ≤ 2 с каждый раз (PLAN §7 U8), туннель снова — связь
  вернулась; СТОП при застрявшем туннеле (ssh на паузе, TCP жив): за ≤ 2,5 с — «нет ответа», кнопка снова доступна,
  после паузы запоздавший стоп шлюз не исполняет (в аудите — только отказ по возрасту); kill -9 шлюза — «нет связи»,
  затем «шлюз перезапущен в … · история с …».

  .venv-ui/bin/python neurobot/ui/tests/e2e_live.py --out DIR [--vm sim] [--skip-gestures]
Код выхода 0 — всё прошло; 1 — есть ошибки (список в DIR/e2e_live.json). Числа — стенд в общей ВМ, ориентир.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from e2e import GLYPHS_JS, LAYOUT_JS, text  # noqa: E402

HELPER = os.path.join(ROOT, "neurobot", "ui", "tests", "twin_helper.py")
SCREENS = ("overview", "safety", "trends")
REMOTE_PORT = 18120


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Twin:
    def __init__(self, vm: str):
        self.vm = vm

    def helper_cmd(self, *args: str):
        inner = "source /opt/ros/jazzy/setup.bash >/dev/null; python3 {} {}".format(
            shlex.quote(HELPER), " ".join(shlex.quote(a) for a in args))
        return ["limactl", "shell", "--workdir", "/", self.vm, "--", "sudo", "bash", "-c", inner]

    def helper(self, *args: str, timeout: float = 300.0) -> str:
        r = subprocess.run(self.helper_cmd(*args), capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError("twin_helper {}: {}".format(" ".join(args), (r.stderr or r.stdout)[-400:]))
        return r.stdout

    def helper_json(self, *args: str, timeout: float = 300.0) -> dict:
        return json.loads(self.helper(*args, timeout=timeout).strip().splitlines()[-1])


class Tunnel:
    def __init__(self, vm: str, port: int):
        self.vm, self.port, self.proc = vm, port, None

    def up(self) -> None:
        cfg = os.path.expanduser("~/.lima/{}/ssh.config".format(self.vm))
        # своё соединение, а не мультиплексор Lima (ControlMaster): иначе проброс живёт в нём и после нашего выхода
        self.proc = subprocess.Popen(["ssh", "-F", cfg, "-o", "ExitOnForwardFailure=yes", "-o", "ControlMaster=no",
                                      "-o", "ControlPath=none", "-N", "-L",
                                      "{}:127.0.0.1:{}".format(self.port, REMOTE_PORT), "lima-" + self.vm],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.monotonic() + 15
        while time.monotonic() < end:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("туннель к ВМ не поднялся")

    def down(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(5)
            self.proc = None


def req(port: int, method: str, path: str, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": "127.0.0.1:%d" % port}
    h.update(headers or {})
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def num(s: str):
    m = re.search(r"−?\d+(?:,\d+)?", s.replace(" ", " "))
    return float(m.group(0).replace(",", ".").replace("−", "-")) if m else None


def wait(page, fn, timeout: float, step: float = 0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        page.wait_for_timeout(int(step * 1000))
    return None


def run(out: str, vm: str, gestures: bool) -> dict:
    from playwright.sync_api import sync_playwright
    os.makedirs(out, exist_ok=True)
    twin = Twin(vm)
    port = free_port()
    tunnel = Tunnel(vm, port)
    rep = {"vm": vm, "tunnel_port": port, "checks": {}, "errors": [], "pages": [], "numbers": {}}
    errs = rep["errors"]
    ok = rep["checks"]

    def check(name, cond, detail=""):
        ok[name] = bool(cond)
        if not cond:
            errs.append("{}: {}".format(name, detail))

    tunnel.up()
    base = "http://127.0.0.1:%d" % port
    try:
        # -- вход через туннель на другой локальный порт
        link = twin.helper("url").strip().splitlines()[-1]
        code = link.split("code=", 1)[1]
        status, _ = req(port, "GET", "/api/meta")
        check("no_cookie_401", status == 401, status)
        status, _ = req(port, "GET", "/api/meta", {"Host": "evil.example:%d" % port})
        check("foreign_host_403", status == 403, status)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1,
                                      reduced_motion="reduce", color_scheme="light", locale="ru-RU")
            page = ctx.new_page()
            console = []
            phase = {"name": "start"}
            page.on("console", lambda m: console.append((phase["name"], "console %s: %s" % (m.type, m.text)))
                    if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: console.append((phase["name"], "pageerror: %s" % e)))
            page.goto(base + "/login?code=" + code)
            page.wait_for_selector("[data-testid=source]", timeout=20000)
            check("login_redirect_to_root", page.url.rstrip("/").endswith(str(port)) or "/#/" in page.url
                  or page.url.endswith("/"), page.url)
            cookie = next(c for c in ctx.cookies() if c["name"].startswith("nbui_"))
            check("cookie_httponly_strict", cookie["httpOnly"] and cookie["sameSite"] == "Strict", cookie)
            status, _ = req(port, "GET", "/login?code=" + code)
            check("link_one_time", status == 401, status)
            csrf = page.evaluate("fetch('api/meta').then((r) => r.json()).then((m) => m.csrf)")
            status, _ = req(port, "POST", "/api/stop", {"Cookie": "%s=%s" % (cookie["name"], cookie["value"]),
                                                         "Origin": base, "Content-Type": "application/json"}, b"{}")
            check("post_without_csrf_403", status == 403, status)
            check("csrf_in_meta", bool(csrf), csrf)

            # -- живой поток
            phase["name"] = "live"

            def live_ready():
                link_age = num(text(page, "[data-testid=link]").split("·")[-1]) if page.query_selector(
                    "[data-testid=link]") else None
                scan = int(page.get_attribute("[data-testid=lidar]", "data-scan") or 0)
                units = text(page, "[data-testid=tile-units]")
                state = text(page, "[data-testid=status-safety]")
                if link_age is not None and link_age < 2 and scan > 10 and " из " in units and state.strip("— "):
                    return {"link_s": link_age, "scan_points": scan, "units": units.split("ЮНИТЫ")[-1][:40],
                            "state": state}
                return None
            got = wait(page, live_ready, 25)
            check("live_stream", got, "нет связи, скана или состояния за 25 с")
            rep["numbers"]["live"] = got
            mem = text(page, "[data-testid=tile-mem]")
            rep["numbers"]["stack_rss_tile"] = mem[:80]

            # -- экраны: источник, знак ЖИВОЙ, каркас, шрифт
            phase["name"] = "screens"
            for theme, size in (("light", (1920, 1080)), ("dark", (1920, 1080)), ("light", (1366, 768))):
                page.set_viewport_size({"width": size[0], "height": size[1]})
                page.emulate_media(color_scheme=theme)
                glyphs = set()
                for screen in SCREENS:
                    page.goto("%s/?theme=%s#/%s" % (base, theme, screen))
                    page.wait_for_selector("[data-testid=source]", timeout=15000)
                    wait(page, lambda: "ЖИВОЙ" in text(page, "[data-testid=source]") and page.query_selector(
                        "[data-testid=link]"), 10)
                    page.wait_for_timeout(1500)
                    src = text(page, "[data-testid=source]")
                    lay = page.evaluate(LAYOUT_JS)
                    glyphs |= set(page.evaluate(GLYPHS_JS))
                    rec = {"screen": screen, "theme": theme, "size": list(size), "source": src, "layout": lay}
                    if "ЖИВОЙ" not in src or "двойник" not in src:
                        errs.append("%s/%s: нет подписи источника «двойник»: %r" % (screen, theme, src))
                    pw, ph, iw, ih = lay["page"]
                    if pw > iw or ph > ih:
                        errs.append("%s/%s %dx%d: страница больше окна" % (screen, theme, size[0], size[1]))
                    if lay["over"]:
                        errs.append("%s/%s: вылезает за панель: %s" % (screen, theme, "; ".join(lay["over"][:4])))
                    if screen == "overview" and lay["main"][0] > lay["main"][1]:
                        errs.append("overview %dx%d: «Обзор» не в окне" % size)
                    name = "live_%s_%s_%dx%d.png" % (screen, theme, size[0], size[1])
                    page.screenshot(path=os.path.join(out, name))
                    rec["png"] = name
                    rep["pages"].append(rec)
                if glyphs:
                    errs.append("%s %dx%d: знаки не из JetBrains Mono: %s" % (theme, size[0], size[1],
                                                                            " ".join(sorted(glyphs))))
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.emulate_media(color_scheme="light")

            # -- СТОП движения: робот едет заявкой агента, оператор жмёт кнопку
            phase["name"] = "stop"
            page.goto(base + "/#/overview")
            page.wait_for_selector("[data-testid=stop]")
            drive = subprocess.Popen(twin.helper_cmd("drive", "--seconds", "9", "--speed", "0.12", "--watch", "1.5"),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            moving = wait(page, lambda: (num(text(page, "[data-testid=speed-out]")) or 0) > 0.05, 12, 0.1)
            check("robot_moving_before_stop", moving, "робот не поехал по заявке агента")
            page.click("[data-testid=stop]")
            t_click = time.monotonic()
            res = wait(page, lambda: (lambda s: s if s and "отправляю" not in s and "не аварийная" not in s else None)(
                text(page, "[data-testid=stop-result]")), 5, 0.05)
            zero_ui = wait(page, lambda: num(text(page, "[data-testid=speed-out]")) == 0.0, 5, 0.05)
            rep["numbers"]["stop_ui"] = {"result": res, "zero_on_screen_s": round(time.monotonic() - t_click, 2)
                                         if zero_ui else None}
            page.screenshot(path=os.path.join(out, "live_stop.png"))
            o, e = drive.communicate(timeout=60)
            try:
                d = json.loads(o.strip().splitlines()[-1])
            except (ValueError, IndexError):
                d = {"stops": [], "error": e[-300:]}
            rep["numbers"]["stop_graph"] = d
            ui_stops = [s for s in d.get("stops", []) if s.get("by") == "ui:operator"]
            check("stop_result_confirmed", res and res.startswith("принят safety_gate"), res)
            check("stop_reached_graph_5_copies_one_id", len(ui_stops) == 1 and ui_stops[0]["copies"] == 5, d)
            check("stop_zero_cmd_vel_100ms", ui_stops and ui_stops[0]["zero_after_s"] is not None
                  and ui_stops[0]["zero_after_s"] <= 0.1, ui_stops)
            check("not_moving_after_stop", ui_stops and not ui_stops[0]["moved_after_stop"], ui_stops)
            check("zero_on_screen", zero_ui, "на экране не 0 после стопа")

            # -- защёлка: поставить с подтверждением; снять из пульта нечем
            phase["name"] = "estop"
            page.wait_for_timeout(2500)
            page.goto(base + "/#/safety")
            page.wait_for_selector("[data-testid=estop]", timeout=10000)
            page.click("[data-testid=estop]")
            page.wait_for_selector("[data-testid=estop-confirm]")
            page.click("[data-testid=estop-confirm]")
            t_click = time.monotonic()
            est = wait(page, lambda: "ESTOP" in text(page, "[data-testid=status-safety]"), 5, 0.05)
            rep["numbers"]["estop_on_screen_s"] = round(time.monotonic() - t_click, 2) if est else None
            check("estop_on_screen", est, "ESTOP не показан за 5 с")
            page.wait_for_selector("[data-testid=latch]", timeout=5000)
            latch = text(page, "[data-testid=latch]")
            rep["numbers"]["estop_result"] = text(page, "[data-testid=estop-box]")
            check("latch_card_has_release_command", "sudo neurobot estop release" in latch, latch)
            buttons = [b.inner_text() for b in page.query_selector_all("button")]
            check("no_release_button", not any(re.search(r"сн[яи]т", b, re.I) for b in buttons), buttons)
            phase["name"] = "release404"        # ожидаемый 404 браузер пишет в консоль — не ошибка страницы
            st = page.evaluate("""(c) => fetch('api/release', {method: 'POST', headers: {'Content-Type':
                'application/json', 'X-Neurobot-CSRF': c}, body: '{}'}).then((r) => r.status)""", csrf)
            check("release_route_404", st == 404, st)
            phase["name"] = "estop"
            page.screenshot(path=os.path.join(out, "live_estop_card.png"))
            rel = twin.helper_json("release", timeout=60)
            t_rel = time.monotonic()
            back = wait(page, lambda: "ESTOP" not in text(page, "[data-testid=status-safety]"), 8, 0.1)
            rep["numbers"]["release"] = {"cli_rc": rel["rc"], "on_screen_s": round(time.monotonic() - t_rel, 2)
                                         if back else None, "state": text(page, "[data-testid=status-safety]")}
            check("release_by_human_shown", rel["rc"] == 0 and back, rel)

            # -- жесты: новый таймлайн, на экране — строки с Δ от t_end
            if gestures:
                phase["name"] = "gestures"
                page.goto(base + "/#/overview")
                ready = twin.helper_json("gestures", "--n", "5", timeout=300)
                rep["numbers"]["perception_ready_s"] = ready.get("perception_ready_s")

                def rows():
                    tr = page.query_selector_all("[data-testid=last-gestures] tbody tr")
                    vals = [r.inner_text().replace(" ", " ").split("\t") for r in tr]
                    return [v for v in vals if v and num(v[-1]) is not None]
                done = wait(page, lambda: len(rows()) >= 2 and rows(), 150, 1.0)
                rep["numbers"]["gestures"] = done
                check("gestures_with_latency", done, "за 150 с нет двух жестов с Δ")
                rep["numbers"]["k1_tile"] = text(page, "[data-testid=tile-k1]")[:120]
                page.screenshot(path=os.path.join(out, "live_gestures.png"))
                page.goto(base + "/#/safety")
                page.wait_for_timeout(1500)
                page.screenshot(path=os.path.join(out, "live_safety_gestures.png"))

            # -- «Запись» рядом и обратно; СТОП работает и там
            phase["name"] = "modes"
            page.goto(base + "/#/overview")
            page.wait_for_selector("[data-testid=mode-switch]")
            page.click("[data-testid=mode-switch] >> text=Запись")
            rec = wait(page, lambda: "ЗАПИСЬ" in text(page, "[data-testid=source]"), 20)
            check("mode_replay", rec, "не переключился на «Запись»")
            check("stop_enabled_in_replay", page.is_enabled("[data-testid=stop]"), "СТОП неактивен в «Записи»")
            page.screenshot(path=os.path.join(out, "live_mode_replay.png"))
            page.click("[data-testid=mode-switch] >> text=Живой")
            check("mode_live_again", wait(page, lambda: "ЖИВОЙ" in text(page, "[data-testid=source]"), 20), "")

            # -- одна SSE на браузер: вторая вкладка — без своего потока
            phase["name"] = "tabs"
            page.wait_for_timeout(2000)
            p2 = ctx.new_page()
            p2.on("pageerror", lambda e: console.append(("tabs", "pageerror 2: %s" % e)))
            p2.goto(base + "/#/safety")
            got2 = wait(p2, lambda: p2.query_selector("[data-testid=link]") and "ЖИВОЙ" in text(
                p2, "[data-testid=source]") and (num(text(p2, "[data-testid=link]").split("·")[-1]) or 9) < 2, 15)
            clients = json.loads(req(port, "GET", "/healthz")[1])["clients"]
            rep["numbers"]["clients_two_tabs"] = clients
            check("second_tab_live", got2, "вторая вкладка без живых данных")
            check("one_stream_per_browser", clients == 1, clients)
            rep["numbers"]["ui_unit"] = twin.helper_json("stats")

            # -- doctor по кнопке
            phase["name"] = "doctor"
            page.goto(base + "/#/trends")
            page.wait_for_selector("[data-testid=doctor-run]")
            page.click("[data-testid=doctor-run]")
            verdict = wait(page, lambda: page.query_selector("[data-testid=doctor-verdict]") and text(
                page, "[data-testid=doctor-verdict]"), 120, 1.0)
            rep["numbers"]["doctor"] = {"verdict": verdict, "panel": text(page, "[data-testid=doctor]")[:300]}
            check("doctor_by_button", verdict, "doctor не ответил за 120 с")
            page.screenshot(path=os.path.join(out, "live_trends_doctor.png"))
            rep["numbers"]["ui_unit_after_doctor"] = twin.helper_json("stats")
            p2.close()

            # -- нет связи: туннель оборван → штриховка ≤ 2 с (U8), 10 раз; туннель снова → связь вернулась
            phase["name"] = "nolink"
            page.goto(base + "/#/overview")
            page.wait_for_selector("[data-testid=link]")
            page.wait_for_timeout(1500)
            shown, back_s = [], []
            for i in range(10):
                wait(page, lambda: not page.query_selector("[data-testid=nolink]"), 20, 0.1)
                page.wait_for_timeout(1200)
                tunnel.down()
                t_cut = time.monotonic()
                gone = wait(page, lambda: page.query_selector("[data-testid=nolink]"), 8, 0.02)
                shown.append(round(time.monotonic() - t_cut, 2) if gone else None)
                if i == 0:
                    page.wait_for_timeout(800)
                    page.screenshot(path=os.path.join(out, "live_nolink.png"))
                tunnel.up()
                t_up = time.monotonic()
                backl = wait(page, lambda: not page.query_selector("[data-testid=nolink]"), 20, 0.1)
                back_s.append(round(time.monotonic() - t_up, 2) if backl else None)
            rep["numbers"]["nolink_shown_s"] = shown
            rep["numbers"]["link_back_s"] = back_s
            check("nolink_overlay_2s_x10", all(x is not None and x <= 2.0 for x in shown), shown)
            check("link_back", all(x is not None for x in back_s), back_s)

            # -- СТОП при застрявшем туннеле: ssh на паузе (TCP жив, данные не идут) — как уснувший Wi-Fi
            phase["name"] = "stuck"
            page.wait_for_timeout(1500)
            lines0 = twin.helper_json("audit")["lines"]
            os.kill(tunnel.proc.pid, signal.SIGSTOP)
            t_stop = time.monotonic()
            try:
                page.click("[data-testid=stop]")
                t_click = time.monotonic()
                res = wait(page, lambda: (lambda x: x if x and "отправляю" not in x and "не аварийная" not in x
                                          else None)(text(page, "[data-testid=stop-result]")), 6, 0.05)
                took = round(time.monotonic() - t_click, 2)
                nolink = page.query_selector("[data-testid=nolink]") is not None
                enabled = page.is_enabled("[data-testid=stop]")
                page.screenshot(path=os.path.join(out, "live_stop_stuck.png"))
                page.wait_for_timeout(max(0, int((5.0 - (time.monotonic() - t_stop)) * 1000)))
            finally:
                os.kill(tunnel.proc.pid, signal.SIGCONT)
            page.wait_for_timeout(5000)            # запоздавший запрос, если был в буфере ssh, дошёл до шлюза
            late = twin.helper_json("audit", "--after", str(lines0))["records"]
            stops = [r for r in late if r.get("action") == "stop"]
            rep["numbers"]["stop_stuck"] = {"result": res, "result_s": took, "nolink_shown": nolink,
                                            "button_enabled": enabled, "paused_s": 5.0, "audit": stops}
            check("stuck_stop_says_no_answer", res and res.startswith("нет ответа") and took <= 2.5, (res, took))
            check("stuck_stop_button_back", enabled, "кнопка СТОП осталась заблокированной")
            check("stuck_stop_not_executed_late", all(not r["ok"] and not r["sent"] and (r.get("age_s") or 0) > 1
                                                      for r in stops), stops)
            check("stuck_nolink_shown", nolink, "при застрявшем туннеле нет штриховки «нет связи»")
            backl = wait(page, lambda: not page.query_selector("[data-testid=nolink]"), 20, 0.1)
            check("stuck_link_back", backl, "после паузы туннеля связь не вернулась")

            # обрывы туннеля и пауза ssh — тот же запуск шлюза: поток продолжается с номера, история браузера цела
            before = page.query_selector("[data-testid=restart]")
            rep["numbers"]["restart_notice_before_kill"] = text(page, "[data-testid=restart]") if before else None
            check("tunnel_drops_resume_stream", not before, rep["numbers"]["restart_notice_before_kill"])

            # -- kill -9 шлюза: «нет связи», затем отметка «шлюз перезапущен», история — с нового потока
            phase["name"] = "restart"
            page.wait_for_timeout(1500)
            killed = twin.helper_json("kill-ui", timeout=90)
            seen = wait(page, lambda: page.query_selector("[data-testid=restart]") and "шлюз перезапущен" in text(
                page, "[data-testid=restart]") and text(page, "[data-testid=restart]"), 30, 0.2)
            rep["numbers"]["gateway_restart"] = {"back_s": killed.get("back_s"), "notice": seen}
            check("restart_notice", seen and "шлюз перезапущен" in seen, seen)
            page.screenshot(path=os.path.join(out, "live_restart_notice.png"))
            phase["name"] = "end"
            page.wait_for_timeout(1500)
            bad = [m for ph, m in console if ph not in ("nolink", "release404", "stuck", "restart")]
            rep["console_nolink"] = [m for ph, m in console if ph in ("nolink", "stuck", "restart")]
            check("console_errors_0", not bad, bad[:6])
            browser.close()
    finally:
        tunnel.down()
    rep["ok"] = not errs
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--vm", default="sim")
    ap.add_argument("--skip-gestures", action="store_true")
    a = ap.parse_args()
    rep = run(a.out, a.vm, not a.skip_gestures)
    path = os.path.join(a.out, "e2e_live.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print("e2e живого режима: %s, проверок %d, ошибок %d → %s" % ("ОК" if rep["ok"] else "ЕСТЬ ОШИБКИ",
                                                                   len(rep["checks"]), len(rep["errors"]), path))
    for e in rep["errors"]:
        print("  - " + e)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
