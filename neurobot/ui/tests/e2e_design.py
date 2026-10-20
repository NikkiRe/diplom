"""Browser acceptance for the responsive console, using only local replay and a synthetic live gateway.

  .venv-ui/bin/python neurobot/ui/tests/e2e_design.py --out /tmp/neurobot-ui-qa

No ROS, VM or robot is contacted. The replay server runs in this process; fake_live.py is a child process.
Screenshots and the machine-readable acceptance report are saved in --out. Vertical scrolling is allowed on
small screens; horizontal page overflow is a failure. --bundle can select a full recording instead of the
committed public sample. Existing e2e_live.py remains the separate hardware/VM acceptance suite.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from neurobot.ui import fmt, server, stream  # noqa: E402

SCREENS = ("overview", "safety", "trends")
SIZES = ((1440, 1000), (1366, 768), (768, 1024), (390, 844))
SAMPLE = ROOT / "neurobot/ui/samples/demo_hall.json.gz"
LAYOUT = """() => {
  const d = document.documentElement;
  const bad = [...document.querySelectorAll('body *')].filter(e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return s.display !== 'none' && s.position !== 'fixed' && r.width > 0
      && (r.right > innerWidth + 1 || r.left < -1) && s.visibility !== 'hidden';
  }).slice(0, 12).map(e => ({tag:e.tagName, cls:e.className, testid:e.dataset.testid,
    width:Math.round(e.getBoundingClientRect().width), text:e.textContent.trim().slice(0,80)}));
  return {width:d.scrollWidth, viewport:innerWidth, height:d.scrollHeight, offenders:bad,
    loading:!!document.querySelector('.loading'), screen:document.querySelector('main')?.dataset.screen};
}"""


def txt(page, selector):
    return " ".join(page.locator(selector).inner_text().replace("\u00a0", " ").split())


def number(value):
    match = re.search(r"-?\d+(?:[,.]\d+)?", value.replace("\u00a0", ""))
    return float(match.group().replace(",", ".")) if match else None


def wait_for(page, condition, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        page.wait_for_timeout(100)
    return None


class Acceptance:
    def __init__(self, out, shots=True):
        self.out = Path(out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.shots = shots
        self.report = {"pages": [], "checks": [], "errors": [], "browser_events": []}
        self.phase = "start"

    def check(self, name, ok, detail=None):
        self.report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            self.report["errors"].append({"phase": self.phase, "check": name, "detail": detail})

    def attempt(self, name, action):
        self.phase = name
        try:
            action()
        except Exception as exc:
            self.check(name + "_completed", False, str(exc))
            self.report.setdefault("exceptions", []).append(traceback.format_exc())

    def observe(self, page):
        def event(kind, value):
            self.report["browser_events"].append({"phase": self.phase, "kind": kind, "detail": value})
        page.on("console", lambda message: event("console." + message.type, message.text)
                if message.type in ("warning", "error") else None)
        page.on("pageerror", lambda error: event("pageerror", str(error)))
        # Navigation intentionally closes EventSource and a component's pending catalog fetch.
        page.on("requestfailed", lambda request: event("requestfailed", request.url + " " + str(request.failure))
                if not (urlsplit(request.url).path in ("/api/stream", "/api/catalog") and
                        "ERR_ABORTED" in str(request.failure)) else None)
        page.on("response", lambda response: event("http", str(response.status) + " " + response.url)
                if response.status >= 400 else None)

    def ready(self, page):
        page.locator("[data-testid=source]").wait_for(timeout=15000)
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(250)

    def screenshot(self, page, name):
        if self.shots:
            page.screenshot(path=str(self.out / (name + ".png")), full_page=True, animations="disabled")

    def matrix(self, browser, base, live=False, context=None, run_id="a1v2", at=1095):
        mode = "live" if live else "replay"
        for theme in ("dark", "light"):
            for width, height in SIZES:
                ctx = context or browser.new_context(viewport={"width": width, "height": height},
                    color_scheme=theme, reduced_motion="reduce", locale="ru-RU")
                page = ctx.new_page()
                self.observe(page)
                page.set_viewport_size({"width": width, "height": height})
                page.emulate_media(color_scheme=theme, reduced_motion="reduce")
                try:
                    for screen in SCREENS:
                        self.phase = f"{mode}/{screen}/{theme}/{width}x{height}"
                        query = f"?theme={theme}" + ("" if live else f"&run={run_id}&t={at}")
                        page.goto(base + "/" + query + "#/" + screen)
                        self.ready(page)
                        if live:
                            wait_for(page, lambda: page.locator("[data-testid=link]").count(), 10)
                        source = txt(page, "[data-testid=source]")
                        current = page.locator("nav a[aria-current=page]")
                        layout = page.evaluate(LAYOUT)
                        name = f"{mode}_{screen}_{theme}_{width}x{height}"
                        self.check(name + "_source", ("ЖИВОЙ" in source and "двойник" in source) if live
                                   else ("ЗАПИСЬ" in source and "источник: запись" in source), source)
                        self.check(name + "_navigation", current.count() == 1 and
                                   current.get_attribute("href") == "#/" + screen, layout["screen"])
                        self.check(name + "_width", layout["width"] <= layout["viewport"] + 1, layout)
                        self.check(name + "_loaded", not layout["loading"], layout)
                        self.check(name + "_theme", page.locator("html").get_attribute("data-theme") == theme)
                        self.screenshot(page, name)
                        self.report["pages"].append({"mode": mode, "screen": screen, "theme": theme,
                            "size": [width, height], "source": source, "layout": layout,
                            "screenshot": name + ".png" if self.shots else None})
                finally:
                    page.close()
                    if context is None:
                        ctx.close()

    def replay_controls(self, browser, base, bundle):
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ru-RU",
                                  reduced_motion="reduce")
        page = ctx.new_page()
        self.observe(page)
        try:
            # Values come from the recording, and must equal the independently produced metrics summary.
            for run in bundle["runs"]:
                page.goto(base + f"/?run={run['id']}&t=99999&theme=dark#/trends")
                self.ready(page)
                metrics = run["summary"]["metrics"]
                k1 = metrics.get("K1_t0_to_cmd_s", {})
                expected = f"{fmt.num(k1.get('p50'), 2)} / {fmt.num(k1.get('p95'), 2)} с (n={k1.get('n', 0)})"
                shown = txt(page, "[data-testid=chart-k1] h2")
                self.check("end_" + run["id"] + "_k1", expected in shown, {"want": expected, "got": shown})
                cells = page.locator("[data-testid=compare] td[title]").evaluate_all("els => els.map(e => e.title)")
                self.check("end_" + run["id"] + "_compare", all(value in cells for value in run["summary"]["compare"]), cells)
                page.keyboard.press("1")
                page.locator("[data-testid=tile-touch]").wait_for()
                for testid, value in (("tile-touch", metrics["touches"]["episodes"]),
                                     ("tile-k4", metrics["K4"]["llm_calls_outside_requests"])):
                    shown = txt(page, f"[data-testid={testid}] .big")
                    self.check("end_" + run["id"] + "_" + testid, shown == fmt.num(value, 0), shown)

            rid = next((r["id"] for r in bundle["runs"] if r["system"] == "a1"), bundle["runs"][0]["id"])
            page.goto(base + f"/?run={rid}&theme=dark#/overview")
            self.ready(page)
            now, _, end = txt(page, ".player .time").partition(" / ")
            self.check("replay_default_at_end", now == "T+" + end, [now, end])
            self.check("replay_stop_disabled", page.locator("[data-testid=stop]").is_disabled()
                       and "только в живом режиме" in txt(page, "[data-testid=stop]"))

            page.goto(base + f"/?run={rid}&t=120&theme=dark&play=1&speed=10#/overview")
            self.ready(page)
            page.get_by_role("button", name="×60", exact=True).click()
            before = txt(page, ".player .time")
            page.wait_for_timeout(1100)
            after = txt(page, ".player .time")
            self.check("replay_60x_advances", before != after, [before, after])
            page.keyboard.press("Space")
            page.wait_for_timeout(200)
            paused = txt(page, ".player .time")
            page.wait_for_timeout(1100)
            self.check("space_after_speed_pauses", paused == txt(page, ".player .time"), paused)
            page.keyboard.press("Space")
            page.wait_for_timeout(1100)
            self.check("space_resumes", paused != txt(page, ".player .time"))
            page.keyboard.press("Space")
            slider = page.get_by_role("slider")
            for fraction in (0.25, 0.5, 0.75):
                # The time label gains a digit after ten minutes, so the flexible track can resize.
                box = slider.bounding_box()
                page.mouse.click(box["x"] + box["width"] * fraction, box["y"] + box["height"] / 2)
                page.wait_for_timeout(150)
                value = int(slider.get_attribute("aria-valuenow"))
                maximum = int(slider.get_attribute("aria-valuemax"))
                self.check("scrub_" + str(fraction), abs(value - maximum * fraction) <= max(2, maximum / box["width"] * 2), value)
            page.keyboard.press("Shift+ArrowLeft")
            page.wait_for_timeout(150)
            self.check("keyboard_seek_minute", abs(int(slider.get_attribute("aria-valuenow")) - (value - 60)) <= 1)
            for key, screen in (("1", "overview"), ("2", "safety"), ("3", "trends"), ("Escape", "overview")):
                page.keyboard.press(key)
                page.wait_for_timeout(150)
                self.check("keyboard_nav_" + key, page.locator("main").get_attribute("data-screen") == screen)
            for screen in SCREENS:
                page.locator(f"nav a[href='#/{screen}']").click()
                page.wait_for_timeout(150)
                self.check("click_nav_" + screen, page.locator("main").get_attribute("data-screen") == screen)

            # Theme control must remain usable by keyboard and persist over a reload.
            theme_button = page.get_by_role("button", name="Переключить тему", exact=True)
            theme_button.focus()
            page.keyboard.press("Enter")
            page.wait_for_timeout(150)
            self.check("theme_toggle_light", page.locator("html").get_attribute("data-theme") == "light")
            page.keyboard.press("Enter")
            page.wait_for_timeout(150)
            self.check("theme_toggle_dark", page.locator("html").get_attribute("data-theme") == "dark")
            page.goto(base + "/#/overview")
            self.ready(page)
            self.check("theme_persists", page.locator("html").get_attribute("data-theme") == "dark")
            page.get_by_role("button", name="Горячие клавиши", exact=True).click()
            page.get_by_role("dialog").wait_for()
            self.check("shortcuts_dialog_visible", page.get_by_role("dialog").is_visible())
            self.screenshot(page, "shortcuts_dialog")
            page.keyboard.press("Escape")
            page.get_by_role("dialog").wait_for(state="hidden")
            self.check("shortcuts_escape_closes", not page.get_by_role("dialog").count())
            options = page.locator("#runsel option").evaluate_all("els => els.map(e => e.value)")
            for option in options:
                page.locator("#runsel").select_option(option)
                page.wait_for_timeout(150)
                self.check("run_select_" + option, page.locator("#runsel").input_value() == option)
        finally:
            ctx.close()

    def empty_data(self, browser, original, temp):
        doc = copy.deepcopy(original)
        run = doc["runs"][1] if len(doc["runs"]) > 1 else doc["runs"][0]
        run.update(id="empty", summary={"compare": []}, t_start=100.0, t_end=101.0, anchor_t=100.0,
                   stream=[{"k": "hb", "t": 100.5}])
        doc["runs"], doc["compare_rows"] = [run], []
        path = str(Path(temp) / "empty.json.gz")
        stream.write_bundle(path, doc)
        srv = server.make_server(path, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        ctx = browser.new_context(viewport={"width": 390, "height": 844})
        page = ctx.new_page()
        self.observe(page)
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            for screen in SCREENS:
                page.goto(base + "/?t=0&theme=dark#/" + screen)
                self.ready(page)
                body = txt(page, "body")
                self.check("empty_" + screen + "_valid_text", not re.search(r"NaN|undefined|Infinity|\[object Object\]", body), body)
                if screen == "overview":
                    values = {key: txt(page, f"[data-testid={key}] .big") for key in
                              ("tile-k1", "tile-k4", "tile-mem", "tile-units", "tile-touch")}
                    self.check("empty_metrics_are_unknown", all("—" in v for v in values.values()), values)
                    self.check("empty_safety_unknown", "—" in txt(page, "[data-testid=state-word]"))
                if screen == "safety":
                    self.check("empty_clearance_unknown", "—" in txt(page, "[data-testid=clearance]"))
                if screen == "trends":
                    self.check("empty_latency_unknown", "—" in txt(page, "[data-testid=chart-k1] h2"))
                    self.check("empty_comparison_has_no_fabricated_pair", not page.locator("[data-testid=kpis]").count())
                self.screenshot(page, "empty_" + screen)
        finally:
            ctx.close()
            srv.shutdown()
            srv.server_close()

    def live(self, browser, bundle_path, temp):
        state = Path(temp) / "fake-state"
        log = (self.out / "fake_live.stderr.log").open("w")
        child = subprocess.Popen([sys.executable, str(ROOT / "neurobot/ui/tests/fake_live.py"), "--port", "0",
            "--state", str(state), "--bundle", str(bundle_path)], stdout=subprocess.PIPE, stderr=log, text=True)
        lines = queue.Queue()
        threading.Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True).start()
        ctx = None
        try:
            login = lines.get(timeout=15).strip()
            if not login.startswith("http://127.0.0.1:"):
                raise RuntimeError("Synthetic gateway did not start: " + login)
            base = login.split("/login?", 1)[0]
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ru-RU",
                                      reduced_motion="reduce")
            page = ctx.new_page()
            self.observe(page)
            page.goto(login)
            self.ready(page)
            self.check("live_login_one_time", ctx.request.get(login).status == 401)
            cookies = [cookie for cookie in ctx.cookies() if cookie["name"].startswith("nbui_")]
            self.check("live_session_cookie", len(cookies) == 1 and cookies[0]["httpOnly"] and
                       cookies[0]["sameSite"] == "Strict")
            # Equipment is the workspace default; these checks exercise the dedicated robot overview.
            page.goto(base + "/?theme=dark#/overview")
            self.ready(page)
            self.check("live_stream_values", wait_for(page, lambda: page.locator("[data-testid=lidar]").count()
                and int(page.locator("[data-testid=lidar]").get_attribute("data-scan") or 0) > 10
                and "из" in txt(page, "[data-testid=tile-units]") and
                "SLOW" in txt(page, "[data-testid=status-safety]"), 15), txt(page, "[data-testid=source]"))
            self.check("live_llm_unknown", "—" in txt(page, "[data-testid=tile-llm] .big"))
            self.check("live_camera_value", "14,8" in txt(page, "[data-testid=tile-camera]"))
            page.goto(base + "/?theme=dark#/overview")
            self.ready(page)
            moving = wait_for(page, lambda: (number(txt(page, "[data-testid=speed-out]")) or 0) > 0.05, 35)
            self.check("live_moving_before_stop", moving)
            page.locator("[data-testid=stop]").click()
            accepted = wait_for(page, lambda: "принят safety_gate" in txt(page, "[data-testid=stop-result]"), 5)
            self.check("stop_confirmed_by_safety_gate", accepted, txt(page, "[data-testid=stop-result]"))
            self.check("stop_zero_visible", wait_for(page, lambda: number(txt(page, "[data-testid=speed-out]")) == 0, 3))
            self.screenshot(page, "live_stop_confirmed")
            page.locator("nav a[href='#/safety']").click()
            page.locator("[data-testid=estop]").click()
            confirm = page.locator("[data-testid=estop-confirm]")
            confirm.wait_for()
            self.check("latch_requires_confirmation", "ESTOP" not in txt(page, "[data-testid=status-safety]"))
            self.screenshot(page, "live_latch_confirmation")
            confirm.click()
            self.check("latch_estop_visible", wait_for(page, lambda: "ESTOP" in txt(page, "[data-testid=status-safety]"), 5))
            page.locator("[data-testid=latch]").wait_for()
            self.check("latch_release_is_local_command", "sudo neurobot estop release" in txt(page, "[data-testid=latch]"))
            labels = page.get_by_role("button").all_inner_texts()
            self.check("no_release_button", not any(re.search(r"сн[яи]ть|release", label, re.I) for label in labels), labels)
            meta = ctx.request.get(base + "/api/meta").json()
            response = ctx.request.post(base + "/api/release", headers={"Origin": base, "X-Neurobot-CSRF": meta["csrf"]}, data={})
            self.check("release_route_absent", response.status == 404)
            self.screenshot(page, "live_estop_latched")
            # Closed first page avoids its leader stream being counted as a duplicate during the matrix.
            page.close()
            self.matrix(browser, base, live=True, context=ctx)
            audit = [json.loads(line) for line in (state / "audit.jsonl").read_text().splitlines()]
            actions = [entry for entry in audit if entry.get("action") in ("stop", "estop_engage")]
            self.check("live_actions_audited", len(actions) == 2 and all(entry.get("ok") for entry in actions), actions)
        finally:
            if ctx:
                ctx.close()
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            log.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--bundle", default=str(SAMPLE))
    parser.add_argument("--run", default="a1v2")
    parser.add_argument("--at", type=float, default=1095)
    parser.add_argument("--replay-only", action="store_true")
    parser.add_argument("--no-shots", action="store_true")
    args = parser.parse_args(argv)
    from playwright.sync_api import sync_playwright
    acceptance = Acceptance(args.out, not args.no_shots)
    started = time.monotonic()
    bundle = stream.read_bundle(args.bundle)
    srv = server.make_server(args.bundle, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with tempfile.TemporaryDirectory(prefix="neurobot-design-") as temp, sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            acceptance.attempt("replay_matrix", lambda: acceptance.matrix(browser, base, run_id=args.run, at=args.at))
            acceptance.attempt("replay_controls", lambda: acceptance.replay_controls(browser, base, bundle))
            acceptance.attempt("empty_data", lambda: acceptance.empty_data(browser, bundle, temp))
            if not args.replay_only:
                acceptance.attempt("synthetic_live", lambda: acceptance.live(browser, args.bundle, temp))
            browser.close()
    except Exception as exc:
        acceptance.check("browser_session_completed", False, str(exc))
        acceptance.report.setdefault("exceptions", []).append(traceback.format_exc())
    finally:
        srv.shutdown()
        srv.server_close()
        acceptance.phase = "browser_events"
        acceptance.check("browser_console_page_requests_clean", not acceptance.report["browser_events"],
                         acceptance.report["browser_events"])
        acceptance.report["seconds"] = round(time.monotonic() - started, 2)
        acceptance.report["ok"] = not acceptance.report["errors"]
        path = acceptance.out / "e2e_design.json"
        path.write_text(json.dumps(acceptance.report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"design e2e: {len(acceptance.report['pages'])} pages, {len(acceptance.report['checks'])} checks, "
              f"{len(acceptance.report['errors'])} errors, {acceptance.report['seconds']} s; {path}")
        for error in acceptance.report["errors"]:
            print("  " + str(error))
    return 0 if acceptance.report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
