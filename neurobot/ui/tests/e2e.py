"""E2E пульта в браузере (Playwright, Chromium headless): все экраны открываются на бандле «Записи», ошибок консоли
и страницы 0, отклонённых запросов 0 (включая нарушения CSP), источник «запись» на каждом экране; каркас ровно в окне
(страница не прокручивается, «Обзор» целиком в окне и при 1366×768), заголовки панелей не вылезают; все знаки текста —
из своего шрифта; в конце записи пульт показывает числа compare.md; пробел ставит паузу и после щелчка по ×60;
перемотка — по месту щелчка; скриншоты 1920×1080 светлой и тёмной темы (и 1366×768 — минимум) в каталог --out;
форматирование чисел fmt.js в браузере — те же векторы, что у fmt.py.

  .venv-ui/bin/python neurobot/ui/tests/e2e.py --bundle FILE --out DIR [--run ID] [--at СЕК]

Нужен playwright (pip) и chromium (`python -m playwright install --only-shell chromium`); шлюз поднимается здесь же
на свободном порту 127.0.0.1. Код выхода 0 — всё прошло; 1 — есть ошибки (список в DIR/e2e.json).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)

from neurobot.ui import fmt, server  # noqa: E402

SCREENS = ("overview", "safety", "trends")
# дополнительные кадры 1920×1080: защёлка shock после смерти базы (карточка «снимает только человек у робота») и A0
EXTRA = (("safety", "a1v2", 530.0, "safety_estop"), ("overview", "a0", 1095.0, "overview_a0"),
         ("trends", "a0", 2040.0, "trends_a0_end"))
# векторы форматирования: те же, что в test_ui_fmt.py
FMT_CASES = [[1.8098, 2], [0.125, 2], [0.375, 2], [2.5, 0], [3.5, 0], [-0.001, 2], [-84.555, 1], [12345.678, 1],
             [2406, 0], [1234567, 0], [1.805, 2], [99.995, 2], [None, 2], [0.6846, 2], [-0.5, 0]]
# конец записи — числа compare.md (столбцы A0 и A1 18.10, neurobot_20261018_a1v2/compare.md)
END_NUMBERS = {"a0": {"k1": "1,81 / 2,11 с (n=69)", "touch": "23", "k4": "0"},
               "a1v2": {"k1": "0,68 / 0,97 с (n=57)", "touch": "0", "k4": "0"}}
KPIS = ("2,11 → 0,97", "23 → 0", "0 → 0", "12 → 5")

# раскладка: страница не прокручивается, заголовки и панели не вылезают, «Загрузки…» нет
LAYOUT_JS = """() => {
  const d = document.documentElement, m = document.querySelector('main');
  const over = [...document.querySelectorAll('.panel > h2, .panel, .status .cell, .alarm')]
    .filter((e) => e.scrollWidth > e.clientWidth + 1)
    .map((e) => { const t = e.closest('[data-testid]');
      const name = e.tagName.toLowerCase() + '.' + [...e.classList].join('.');
      return `${name} ${t ? t.dataset.testid : ''} ${e.scrollWidth}>${e.clientWidth}`; });
  return { page: [d.scrollWidth, d.scrollHeight, innerWidth, innerHeight], main: [m.scrollHeight, m.clientHeight],
           over, loading: !!document.querySelector('.loading') };
}"""
# знаки текста, которых нет в JetBrains Mono: у неё все знаки шириной 0,6 em — иная ширина значит подмену шрифта
GLYPHS_JS = """async () => {
  await document.fonts.ready;
  const text = document.body.textContent + [...document.querySelectorAll('[title]')].map((e) => e.title).join('');
  const chars = [...new Set(text)].filter((c) => c.trim() || c === '\\u00a0' || c === '\\u202f');
  const ctx = document.createElement('canvas').getContext('2d');
  ctx.font = "100px 'JetBrains Mono', serif";
  return chars.filter((c) => Math.abs(ctx.measureText(c).width - 60) > 0.5)
    .map((c) => c + ' U+' + c.codePointAt(0).toString(16));
}"""


def text(page, sel):
    return " ".join(page.inner_text(sel).replace("\u00a0", " ").split())


def run(bundle: str, out: str, run_id: str, at: float, shots: bool = True) -> dict:
    from playwright.sync_api import sync_playwright
    os.makedirs(out, exist_ok=True)
    srv = server.make_server(bundle, 0)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = "http://127.0.0.1:%d/" % port
    report = {"bundle": bundle, "run": run_id, "at_s": at, "pages": [], "errors": [], "checks": {}}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for theme, size in (("light", (1920, 1080)), ("dark", (1920, 1080)), ("light", (1366, 768))):
                ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1,
                                          reduced_motion="reduce", color_scheme=theme, locale="ru-RU")
                page = ctx.new_page()
                errs = []
                page.on("console", lambda m, errs=errs: errs.append("console %s: %s" % (m.type, m.text))
                        if m.type in ("error", "warning") else None)
                page.on("pageerror", lambda e, errs=errs: errs.append("pageerror: %s" % e))
                page.on("requestfailed", lambda r, errs=errs: errs.append("requestfailed: %s" % r.url))
                page.on("response", lambda r, errs=errs: errs.append("HTTP %d: %s" % (r.status, r.url))
                        if r.status >= 400 else None)
                glyphs = set()
                for screen in SCREENS:
                    url = "%s?run=%s&t=%s&theme=%s#/%s" % (base, run_id, at, theme, screen)
                    page.goto(url)
                    page.wait_for_selector(".frame", timeout=15000)
                    page.wait_for_timeout(700)
                    src = text(page, "[data-testid=source]")
                    nav = text(page, "nav a[aria-current=page]")
                    lay = page.evaluate(LAYOUT_JS)
                    glyphs |= set(page.evaluate(GLYPHS_JS))
                    rec = {"screen": screen, "theme": theme, "size": list(size), "source": src, "nav": nav,
                           "layout": lay}
                    want = {"overview": "Обзор", "safety": "Безопасность", "trends": "Агент"}[screen]
                    if want not in nav:
                        errs.append("%s: открыт не тот экран: %r" % (screen, nav))
                    if "ЗАПИСЬ" not in src or "источник: запись" not in src:
                        errs.append("%s: нет подписи источника «запись»: %r" % (screen, src))
                    pw, ph, iw, ih = lay["page"]
                    if pw > iw or ph > ih:
                        errs.append("%s: страница больше окна (%dx%d при %dx%d) — строка состояния уйдёт"
                                    % (screen, pw, ph, iw, ih))
                    if lay["loading"]:
                        errs.append("%s: «Загрузка пульта…» осталась в странице" % screen)
                    if lay["over"]:
                        errs.append("%s: вылезает за панель: %s" % (screen, "; ".join(lay["over"][:5])))
                    if screen == "overview" and lay["main"][0] > lay["main"][1]:
                        errs.append("overview: «Обзор» не помещается в окно (%d > %d)" % tuple(lay["main"]))
                    if shots:
                        name = "%s_%s_%dx%d.png" % (screen, theme, size[0], size[1])
                        page.screenshot(path=os.path.join(out, name))
                        rec["png"] = name
                    report["pages"].append(rec)
                if glyphs:
                    errs.append("знаки не из JetBrains Mono (подмена шрифта): %s" % ", ".join(sorted(glyphs)))
                if size == (1920, 1080) and theme == "light":
                    controls(page, base, run_id, at, theme, errs, report)
                if size == (1920, 1080):
                    for screen, rid, t, name in EXTRA:
                        page.goto("%s?run=%s&t=%s&theme=%s#/%s" % (base, rid, t, theme, screen))
                        page.wait_for_selector(".frame", timeout=15000)
                        page.wait_for_timeout(700)
                        if screen == "safety" and not page.query_selector("[data-testid=latch]"):
                            errs.append("%s: нет карточки защёлки" % name)
                        if shots:
                            page.screenshot(path=os.path.join(out, "%s_%s_1920x1080.png" % (name, theme)))
                report["errors"] += ["%s %dx%d: %s" % (theme, size[0], size[1], e) for e in errs]
                ctx.close()
            browser.close()
    finally:
        srv.shutdown()
        srv.server_close()
    with open(os.path.join(out, "e2e.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return report


def controls(page, base, run_id, at, theme, errs, report):
    """Числа, проигрыватель, клавиши, тревоги — один раз, на светлой теме 1920×1080."""
    checks = report["checks"]
    got = page.evaluate("async (cases) => { const f = await import('./lib/fmt.js');"
                        " return cases.map(([v, nd]) => f.num(v, nd)); }", FMT_CASES)
    want = [fmt.num(v, nd) for v, nd in FMT_CASES]
    if got != want:
        errs.append("fmt.js ≠ fmt.py: %r против %r" % (got, want))
    checks["fmt_js"] = got == want

    # конец записи: те же числа, что compare.md (K1 за прогон, касания, LLM на жест; «до → после»)
    shown = {}
    for rid, nums in END_NUMBERS.items():
        page.goto("%s?run=%s&t=99999&theme=%s#/trends" % (base, rid, theme))
        page.wait_for_selector("[data-testid=chart-k1]")
        page.wait_for_timeout(300)
        k1 = text(page, "[data-testid=chart-k1] h2")
        kpis = text(page, "[data-testid=kpis]")
        page.keyboard.press("1")
        page.wait_for_selector("[data-testid=tile-touch]")
        touch = text(page, "[data-testid=tile-touch] .big")
        k4 = text(page, "[data-testid=tile-k4] .big")
        shown[rid] = {"k1": k1, "touch": touch, "k4": k4}
        if nums["k1"] not in k1:
            errs.append("%s: K1 за прогон не как в compare.md: %r (ждали %r)" % (rid, k1, nums["k1"]))
        if (touch, k4) != (nums["touch"], nums["k4"]):
            errs.append("%s: касания / LLM на жест %r / %r, в compare.md %r / %r" % (rid, touch, k4, nums["touch"],
                                                                                   nums["k4"]))
        for kp in KPIS:
            if kp not in kpis:
                errs.append("%s: «до → после» без %r: %r" % (rid, kp, kpis))
    checks["end_numbers"] = shown

    # без ?t= — конец записи: итоговые числа
    page.goto("%s?run=%s&theme=%s#/overview" % (base, run_id, theme))
    page.wait_for_selector(".frame")
    t_now, _, t_end = text(page, ".player .time").partition(" / ")
    checks["default_time"] = [t_now, t_end]
    if t_now != "T+" + t_end:
        errs.append("без ?t= пульт открылся не в конце записи: %s / %s" % (t_now, t_end))

    # проигрыватель: пуск ×60 — время идёт; щелчок по ×60 и пробел — пауза (фокус не остаётся на кнопке)
    page.goto("%s?run=%s&t=%s&theme=%s&play=1&speed=10#/overview" % (base, run_id, at, theme))
    page.wait_for_selector(".frame")
    page.click(".player .seg button:has-text('×60')")
    t1 = text(page, ".player .time")
    page.wait_for_timeout(1200)
    t2 = text(page, ".player .time")
    page.keyboard.press(" ")
    page.wait_for_timeout(200)
    t3 = text(page, ".player .time")
    page.wait_for_timeout(1200)
    t4 = text(page, ".player .time")
    page.keyboard.press(" ")
    page.wait_for_timeout(1200)
    t5 = text(page, ".player .time")
    checks["player"] = [t1, t2, t3, t4, t5]
    if t1 == t2:
        errs.append("проигрыватель: время не идёт (%s)" % t1)
    if t3 != t4:
        errs.append("пробел после щелчка по ×60 не поставил паузу: %s → %s" % (t3, t4))
    if t4 == t5:
        errs.append("пробел не пустил проигрывание снова (%s)" % t4)

    # перемотка — по месту щелчка на полосе
    page.goto("%s?run=%s&t=%s&theme=%s#/overview" % (base, run_id, at, theme))
    page.wait_for_selector(".scrub")
    box = page.locator(".scrub").bounding_box()
    hits = []
    for frac in (0.25, 0.5, 0.75):
        page.mouse.click(box["x"] + box["width"] * frac, box["y"] + box["height"] / 2)
        page.wait_for_timeout(100)
        now, top = (int(page.get_attribute(".scrub", a)) for a in ("aria-valuenow", "aria-valuemax"))
        hits.append([frac, now, round(top * frac)])
        if abs(now - top * frac) > max(2.0, top / box["width"] * 2):
            errs.append("перемотка мимо: щелчок на %.0f %% дал %d с из %d" % (100 * frac, now, top))
    checks["scrub"] = hits

    # «Стоп движения» в записи неактивен и подписан
    if not page.is_disabled(".stopbtn") or "только в живом режиме" not in text(page, ".stopbtn"):
        errs.append("«Стоп движения» в записи активен или без подписи «только в живом режиме»")

    # тревоги: квитирование гасит мигание стоящей; у снятых знак — контур без цвета
    page.goto("%s?run=a1v2&t=1095&theme=%s#/overview" % (base, theme))
    page.wait_for_selector("[data-testid=alarms]")
    blink0 = page.locator(".alarm:not(.off) .sign.blink").count()
    if blink0:
        page.locator(".alarm:not(.off)").first.click()
        page.wait_for_timeout(100)
    blink1 = page.locator(".alarm:not(.off) .sign.blink").count()
    fills = page.evaluate("() => [...document.querySelectorAll('.alarm.off .s1, .alarm.off .s2')]"
                          ".map((e) => getComputedStyle(e).fill)")
    checks["alarms"] = {"blink": [blink0, blink1], "cleared_fills": sorted(set(fills))}
    if not blink0 or blink1 != blink0 - 1:
        errs.append("квитирование: мигающих %d → %d" % (blink0, blink1))
    if any(f != "none" for f in fills):
        errs.append("снятые тревоги закрашены цветом: %s" % sorted(set(fills)))
    count = text(page, "[data-testid=alarm-count]")
    rows = page.locator("[data-testid=alarms] .alarm").count()
    more = page.locator("[data-testid=alarms] .more")
    hidden = int(text(page, "[data-testid=alarms] .more").split()[-1]) if more.count() else 0
    if "за 10 мин %d" % (rows + hidden) not in count:
        errs.append("число тревог в строке состояния %r не равно строкам ленты (%d)" % (count, rows + hidden))

    # управление: окна трендов, смена прогона, клавиши экранов, перемотка клавишами
    page.goto("%s?run=%s&t=%s&theme=%s#/trends" % (base, run_id, at, theme))
    page.wait_for_selector("[data-testid=chart-k1]")
    for title in ("1 ч", "прогон", "15 мин"):
        page.click("button.segbtn:has-text('%s')" % title)
        page.wait_for_timeout(150)
    runs = page.eval_on_selector_all("#runsel option", "els => els.map((e) => e.value)")
    for v in runs:
        page.select_option("#runsel", v)
        page.wait_for_timeout(250)
    for key, want in (("1", "Обзор"), ("2", "Безопасность"), ("3", "Агент"), ("Escape", "Обзор")):
        page.keyboard.press(key)
        page.wait_for_timeout(150)
        if want not in page.inner_text("nav a[aria-current=page]"):
            errs.append("клавиша %s не открыла «%s»" % (key, want))
    before = page.inner_text(".player .time")
    page.keyboard.press("Shift+ArrowLeft")
    page.wait_for_timeout(150)
    if page.inner_text(".player .time") == before:
        errs.append("перемотка Shift+← не сработала")
    checks["controls"] = {"runs": len(runs)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--bundle", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--run", default="a1v2", help="прогон для скриншотов (id в бандле)")
    p.add_argument("--at", type=float, default=1095.0, help="момент от начала расписания, с")
    p.add_argument("--no-shots", action="store_true")
    a = p.parse_args(argv)
    t = time.time()
    rep = run(a.bundle, a.out, a.run, a.at, not a.no_shots)
    print("e2e: страниц %d, ошибок %d, %.1f с" % (len(rep["pages"]), len(rep["errors"]), time.time() - t))
    for e in rep["errors"]:
        print("  " + e)
    return 1 if rep["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
