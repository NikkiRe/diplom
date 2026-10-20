"""Typed instrument browser acceptance against a running local EdgeLab.

  .venv-ui/bin/python neurobot/ui/tests/e2e_instruments.py --base http://127.0.0.1:18127 --out /tmp/instruments-qa

The fixture is an explicitly synthetic MCP peer with an unpredictable device id.
It audits every RPC, exposes resources, and never executes hardware actions.
--require-camera also requires an already connected cam-mcp source; the test only
requests its reviewed read-only snapshot and imports that exact frame into Lab.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path
import re
import sys
import time
import traceback
from urllib.parse import urlsplit
import wave

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from neurobot.ui.tests.e2e_equipment import UnknownMCP  # noqa: E402


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="


def wav_fixture():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\x00\x00" * 800)
    return base64.b64encode(output.getvalue()).decode("ascii")


class InstrumentMCP(UnknownMCP):
    def __init__(self):
        super().__init__()
        self.values = {
            "scalar_zero": 0,
            "boolean_false": False,
            "vector_with_gap": [0, 1, None, -1, 0],
            "xy_points": [[0, 0], [2, 3], [-2, 1]],
            "manual_pose": {"position": {"x": 0, "y": -2, "z": 4}},
            "png_frame": {"mimeType": "image/png", "data": PNG},
            "wav_audio": {"mimeType": "audio/wav", "data": wav_fixture()},
            "unsafe_svg": {"mimeType": "image/svg+xml", "data": base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode()},
            "external_image": "https://example.invalid/qa-never-fetch.png",
            "missing_value": None,
            "vendor.signal/zero": 0,
        }
        self.properties = {name: {"type": "object" if isinstance(value, dict) else "array" if isinstance(value, list)
                                 else "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float))
                                 else "string" if isinstance(value, str) else "number", "writable": False}
                           for name, value in self.values.items()}
        self.properties["scalar_zero"]["unit"] = "qa-unit"

    def descriptor(self):
        return dict(super().descriptor(), name="QA · typed instrument fixture")

    def state(self):
        self.reads += 1
        now = time.time()
        return {"device": self.device_id, "now": now, "properties": {
            name: {"value": value, "ts": now, "age_s": 0,
                   **({"unit": self.properties[name]["unit"]} if self.properties[name].get("unit") else {}),
                   **({"error": "qa_missing"} if value is None else {})}
            for name, value in self.values.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:18127")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--require-camera", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")
    if urlsplit(base).hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("Acceptance only contacts an explicitly local EdgeLab server")
    report = {"checks": [], "errors": [], "browser_events": [], "camera": "not checked"}

    def check(name, passed, detail=None):
        report["checks"].append({"name": name, "ok": bool(passed), "detail": detail})
        if not passed:
            report["errors"].append(name)

    def attempt(name, action):
        try:
            action()
        except Exception:
            check(name, False, traceback.format_exc())

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ru-RU", reduced_motion="reduce")
        meta_response = context.request.get(base + "/api/meta")
        if not meta_response.ok:
            raise RuntimeError("Local server metadata HTTP " + str(meta_response.status))
        meta = meta_response.json()
        headers = {"Origin": base, **({"X-Neurobot-CSRF": meta["csrf"]} if meta.get("csrf") else {})}
        page = context.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror", lambda error: report["browser_events"].append({"kind": "pageerror", "detail": str(error)}))
        page.on("console", lambda message: report["browser_events"].append({"kind": "console.error", "detail": message.text}) if message.type == "error" else None)
        unexpected_requests = []

        def reject_external(route):
            unexpected_requests.append(route.request.url)
            route.abort()

        page.route("**/qa-never-fetch.png", reject_external)

        def choose_device(device_id):
            page.goto(base + "/?theme=dark#/equipment", wait_until="domcontentloaded")
            page.locator('[data-testid="equipment-device"][data-device-id=' + json.dumps(device_id) + ']').click()
            page.get_by_role("button", name="Прибор устройства", exact=True).click()
            page.locator('[data-testid="capability-instrument"]').wait_for()

        def field(name, kind="auto"):
            page.get_by_label("Поле прибора", exact=True).select_option(json.dumps(["properties", name], separators=(",", ":")))
            page.get_by_label("Тип прибора", exact=True).select_option(kind)

        with InstrumentMCP() as fixture:
            source_id = None
            try:
                response = context.request.post(base + "/api/catalog/sources", headers=headers,
                                                data={"url": fixture.url, "label": "QA · typed instruments"})
                check("source_connected", response.ok, response.status)
                doc = response.json()
                source_id = doc.get("source_id")
                if not source_id:
                    raise RuntimeError("Source registration failed: " + json.dumps(doc, ensure_ascii=False))
                device_id = source_id + "/" + fixture.device_id
                deadline = time.monotonic() + 15
                device = None
                while time.monotonic() < deadline:
                    snapshot = context.request.get(base + "/api/catalog").json()
                    device = next((d for d in snapshot.get("devices", []) if d["id"] == device_id), None)
                    if device:
                        break
                    page.wait_for_timeout(100)
                check("resource_device_discovered", device is not None)
                if not device:
                    raise RuntimeError("Fixture did not appear in catalog")
                props = {p["name"]: p["value"] for p in device["properties"]}
                check("endpoint_preserves_zero_false_gap", props["scalar_zero"] == 0 and props["boolean_false"] is False
                      and props["vector_with_gap"][2] is None, props["vector_with_gap"])
                choose_device(device_id)

                def scalar():
                    field("scalar_zero")
                    value = page.locator('[data-testid="instrument-value"]').inner_text()
                    check("scalar_zero_rendered", value.startswith("0") and "qa-unit" in value, value)
                    field("vendor.signal/zero", "scalar")
                    check("literal_dot_slash_field", page.locator('[data-testid="instrument-value"]').inner_text() == "0")
                    field("missing_value", "scalar")
                    check("missing_is_not_zero", page.locator('[data-testid="instrument-value"]').inner_text() == "—")
                attempt("scalar_cases", scalar)

                def boolean():
                    field("boolean_false")
                    check("boolean_false_rendered", page.locator(".ci-boolean strong").inner_text() == "FALSE")
                    check("false_not_active", "on" not in (page.locator(".ci-boolean").get_attribute("class") or "").split())
                attempt("boolean_case", boolean)

                def vector():
                    field("vector_with_gap")
                    path = page.locator(".ci-line").get_attribute("d")
                    check("vector_null_breaks_path", path.count("M") == 2 and path.count("L") == 2, path)
                    check("vector_has_no_nan", "NaN" not in path)
                    page.locator('[data-testid="capability-instrument"]').screenshot(path=str(args.out / "vector-gap.png"), animations="disabled")
                attempt("vector_case", vector)

                def points():
                    field("xy_points")
                    check("xy_all_actual_points", page.locator(".ci-point").count() == 3)
                    coords = page.locator(".ci-point").evaluate_all('(els)=>els.map(e=>[+e.getAttribute("cx"),+e.getAttribute("cy")])')
                    check("xy_points_within_plot", all(40 <= x <= 560 and 30 <= y <= 200 for x, y in coords), coords)
                attempt("xy_case", points)

                def pose():
                    field("manual_pose", "pose")
                    check("pose_manual_projection", page.locator(".ci-point").count() == 1 and "без ориентации" in page.locator(".ci-plot").inner_text())
                    page.locator(".ci-binding summary").click()
                    page.get_by_label("Единица прибора", exact=True).fill("m")
                    page.get_by_label("Система координат прибора", exact=True).fill("qa-frame")
                    choose_device(device_id)
                    check("manual_binding_survives_reload", page.get_by_label("Тип прибора", exact=True).input_value() == "pose"
                          and page.get_by_label("Поле прибора", exact=True).input_value() == '["properties","manual_pose"]')
                    page.locator(".ci-binding summary").click()
                    check("semantic_binding_preserved", page.get_by_label("Единица прибора", exact=True).input_value() == "m"
                          and page.get_by_label("Система координат прибора", exact=True).input_value() == "qa-frame")
                attempt("pose_case", pose)

                def image():
                    field("png_frame")
                    image = page.locator(".ci-media img")
                    image.wait_for()
                    page.wait_for_function('()=>{const e=document.querySelector(".ci-media img");return e&&e.complete&&e.naturalWidth===1;}')
                    check("png_bytes_decoded", image.get_attribute("src") == "data:image/png;base64," + PNG)
                attempt("image_case", image)

                def audio():
                    field("wav_audio")
                    audio = page.locator(".ci-audio audio")
                    audio.wait_for()
                    check("audio_is_explicit_control", audio.get_attribute("controls") is not None and audio.get_attribute("autoplay") is None
                          and audio.get_attribute("preload") == "none")
                    audio.evaluate("e=>e.load()")
                    page.wait_for_function('()=>{const e=document.querySelector(".ci-audio audio");return e&&e.readyState>=1;}')
                    duration = audio.evaluate("e=>e.duration")
                    check("wav_bytes_decoded", 0.099 <= duration <= 0.101, duration)
                attempt("audio_case", audio)

                def unsafe_media():
                    for name in ("unsafe_svg", "external_image"):
                        field(name, "image")
                        check(name + "_rejected", page.locator(".ci-unsupported").count() == 1 and page.locator(".ci-media img").count() == 0)
                    check("external_url_never_requested", not unexpected_requests, unexpected_requests)
                attempt("unsafe_media_cases", unsafe_media)

                def responsive():
                    field("vector_with_gap", "vector")
                    for theme in ("dark", "light"):
                        for width, height in ((390, 844), (768, 1024), (1440, 1000)):
                            page.set_viewport_size({"width": width, "height": height})
                            page.evaluate('(theme)=>{document.documentElement.dataset.theme=theme;}', theme)
                            layout = page.locator("main").evaluate('''e=>({width:e.clientWidth,scroll:e.scrollWidth,
                                offenders:[...e.querySelectorAll('*')].filter(x=>{const r=x.getBoundingClientRect();return r.width>0&&r.right>innerWidth+1;})
                                .slice(0,12).map(x=>({tag:x.tagName,class:x.className,width:x.clientWidth,scroll:x.scrollWidth,text:x.textContent.slice(0,70)}))})''')
                            check("layout_%s_%s" % (theme, width), layout["scroll"] <= layout["width"] + 1, layout)
                            page.screenshot(path=str(args.out / ("instrument-%s-%s.png" % (width, theme))), animations="disabled")
                attempt("responsive_cases", responsive)
                check("resource_reads_used", any(c.get("method") == "resources/read" for c in fixture.calls))
                check("no_actuator_calls", not fixture.actuator_calls, fixture.actuator_calls)
                report["rpc_audit"] = {"requests": len(fixture.calls), "state_reads": fixture.reads,
                                       "actuator_calls": fixture.actuator_calls,
                                       "methods": sorted({c["method"] for c in fixture.calls})}
            except Exception:
                check("fixture_suite_completed", False, traceback.format_exc())
            finally:
                if source_id:
                    response = context.request.post(base + "/api/catalog/disconnect", headers=headers, data={"id": source_id})
                    check("fixture_source_disconnected", response.ok, response.status)
                    catalog = context.request.get(base + "/api/catalog").json()
                    check("fixture_removed_from_catalog", all(s["id"] != source_id for s in catalog.get("sources", [])))

        def camera():
            snapshot = context.request.get(base + "/api/catalog").json()
            camera_sources = [s["id"] for s in snapshot.get("sources", []) if s.get("server_info", {}).get("name") == "cam-mcp"]
            device = next((d for d in snapshot.get("devices", []) if d.get("source") in camera_sources), None)
            if not device:
                report["camera"] = "skipped: no reviewed cam-mcp source connected"
                if args.require_camera:
                    check("camera_source_present", False)
                return
            report["camera"] = "reviewed cam-mcp snapshot"
            page.set_viewport_size({"width": 1440, "height": 1000})
            choose_device(device["id"])
            page.locator('[data-testid="camera-instrument"]').wait_for()
            check("camera_initially_explicit", page.locator('[data-testid="camera-frame"]').count() == 0
                  and page.locator('[data-testid="camera-save-episode"]').is_disabled())
            with page.expect_response(lambda r: urlsplit(r.url).path == "/api/catalog/frame" and r.request.method == "POST") as observed:
                page.locator('[data-testid="camera-snapshot"]').click()
            response = observed.value
            check("camera_snapshot_read_succeeds", response.ok, response.status)
            frame = response.json()
            page.locator('[data-testid="camera-frame"]').wait_for()
            page.wait_for_function('()=>{const e=document.querySelector("[data-testid=camera-frame]");return e&&e.complete&&e.naturalWidth>0;}')
            check("camera_exact_bytes_displayed", page.locator('[data-testid="camera-frame"]').get_attribute("src")
                  == "data:" + frame["mimeType"] + ";base64," + frame["data"])
            page.screenshot(path=str(args.out / "camera-snapshot.png"), animations="disabled")
            page.set_viewport_size({"width": 390, "height": 844})
            layout = page.locator("main").evaluate("e=>({width:e.clientWidth,scroll:e.scrollWidth})")
            check("camera_mobile_layout", layout["scroll"] <= layout["width"] + 1, layout)
            page.locator('[data-testid="camera-frame"]').scroll_into_view_if_needed()
            page.screenshot(path=str(args.out / "camera-snapshot-mobile.png"), animations="disabled")
            page.set_viewport_size({"width": 1440, "height": 1000})
            with page.expect_response(lambda r: urlsplit(r.url).path == "/api/lab/import" and r.request.method == "POST") as saved:
                page.locator('[data-testid="camera-save-episode"]').click()
            response = saved.value
            check("camera_import_succeeds", response.ok, response.status)
            run_id = response.json()["run"]["id"]
            page.locator('[data-testid="lab-screen"]').wait_for()
            page.wait_for_function('(id)=>document.querySelector("[data-testid=lab-run-select]")?.value===id', arg=run_id)
            run = context.request.get(base + "/api/lab/runs/" + run_id).json()
            images = [e for e in run["events"] if e.get("type") == "input" and e.get("kind") == "image"]
            check("camera_capture_exact_recorded_payload", len(images) == 1 and images[0].get("value") == {"mimeType": frame["mimeType"], "data": frame["data"]})
            check("camera_capture_actual_timestamp", len(images) == 1 and images[0].get("ts_wall") == frame["observed_at"])
            check("camera_capture_no_fabricated_agent", not any(e["type"].startswith("llm_") for e in run["events"]) and not run.get("contexts"))
            page.locator(".lab-tabs").get_by_role("button", name=re.compile("Инструменты$")).click()
            signal_select = page.locator('[data-testid="lab-signal-select"]')
            image_option = next(o.get_attribute("value") for o in signal_select.locator("option").all() if "Изображение" in o.inner_text())
            signal_select.select_option(image_option)
            image = page.locator('[data-testid="lab-typed-payload"] img')
            image.wait_for()
            image.scroll_into_view_if_needed()
            page.wait_for_function('()=>{const e=document.querySelector("[data-testid=lab-typed-payload] img");return e&&e.complete&&e.naturalWidth>0;}')
            check("captured_media_available_to_tools", image.get_attribute("src") == "data:" + frame["mimeType"] + ";base64," + frame["data"])
            page.screenshot(path=str(args.out / "camera-recorded-lab.png"), animations="disabled")
            report["camera_run"] = run_id
        attempt("camera_case", camera)
        check("no_browser_errors", not report["browser_events"], report["browser_events"])
        browser.close()
    (args.out / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": len(report["checks"]), "errors": report["errors"], "camera": report["camera"]}, ensure_ascii=False))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
