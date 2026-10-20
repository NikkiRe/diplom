"""Self-contained browser acceptance for trusted local live JSONL following.

Starts an ephemeral UI server and owns a temporary, explicitly synthetic QAtrace.
No LLM, MCP source, robot, VM, external network or agent process is used.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import traceback

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from neurobot.ui import server  # noqa: E402


def encoded(record):
    return (json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


def sensor(t, value, record_id=None):
    return {'type': 'input', 'kind': 'sensor', 'device': 'QAtrace-thermometer',
            'property': 'temperature', 'unit': 'Cel', 'value': value, 'ts_clock': t,
            'id': record_id or 'QAtrace-input-' + str(t), 'vendor_unknown': {'kept': True}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report = {'checks': [], 'errors': [], 'browser_events': [], 'screenshots': []}

    def check(name, ok, detail=None):
        report['checks'].append({'name': name, 'ok': bool(ok), 'detail': detail})
        if not ok:
            report['errors'].append(name)

    def eventually(page, condition, seconds=10):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            result = condition()
            if result:
                return result
            page.wait_for_timeout(100)
        return None

    directory = tempfile.TemporaryDirectory(prefix='QAtrace-live-')
    location = Path(directory.name) / 'QAtrace-agent.jsonl'
    prefix = encoded({'type': 'run_start', 'run_id': 'QAtrace-original', 'clock': 'virtual',
                      'ts_clock': 0, 'fixture': 'synthetic browser acceptance, no actual agent'}) + encoded(sensor(1, 10))
    location.write_bytes(prefix)
    srv = server.make_server(None, 0)
    watcher = srv.lab.watch_trace(location, label='QAtrace · local file observer')
    worker = threading.Thread(target=srv.serve_forever, name='QAtrace-ui-server', daemon=True)
    worker.start()
    base = 'http://127.0.0.1:' + str(srv.server_port)
    report['server'] = base
    page = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                                          locale='ru-RU', reduced_motion='reduce', accept_downloads=True)
            page = context.new_page()
            page.on('pageerror', lambda e: report['errors'].append('pageerror: ' + str(e)))
            page.on('console', lambda m: report['errors'].append('console: ' + m.text) if m.type == 'error' else None)
            page.on('requestfailed', lambda r: report['errors'].append('requestfailed: ' + r.url)
                    if r.failure != 'net::ERR_ABORTED' else None)
            page.on('response', lambda r: report['browser_events'].append({'status': r.status, 'url': r.url})
                    if r.status >= 400 else None)

            def doc():
                return context.request.get(base + '/api/lab').json()

            def watched():
                return next(w for w in doc()['watchers'] if w['id'] == watcher['id'])

            def read_run(run_id):
                return context.request.get(base + '/api/lab/runs/' + run_id).json()

            def cursor():
                return float(page.get_by_label('Момент эксперимента').input_value())

            def event_count():
                return int(page.locator('.lab-run-counts b').first.inner_text().replace('\u00a0', '').replace(' ', ''))

            def append(value):
                with location.open('ab') as output:
                    output.write(value)

            def shot(name):
                page.screenshot(path=str(args.out / (name + '.png')), animations='disabled')
                report['screenshots'].append(name + '.png')

            check('watcher_initial_run_available', bool(eventually(page, lambda: watched().get('run_id'))))
            original_id = watched()['run_id']
            page.goto(base + '/#/lab', wait_until='networkidle')
            page.get_by_test_id('lab-run-select').select_option(original_id)
            page.get_by_test_id('lab-live-source').wait_for()
            follow = page.get_by_test_id('lab-follow')
            follow.wait_for()
            check('live_source_label_explicit', 'ЖИВАЯ ТРАССА' in page.get_by_test_id('lab-live-source').inner_text() and 'QAtrace-agent.jsonl' in page.locator('.lab-run-provenance').inner_text() and 'QAtrace' in page.get_by_test_id('lab-run-select').locator('option:checked').inner_text())
            check('live_kind_and_provenance', read_run(original_id)['kind'] == 'live' and read_run(original_id)['provenance']['live'])
            check('follow_initially_enabled', follow.get_attribute('aria-pressed') == 'true')
            check('initial_latest_cursor', eventually(page, lambda: cursor() == 1))
            page.locator('.lab-tabs').get_by_role('button', name=re.compile('Инструменты$')).click()
            page.get_by_test_id('lab-signal-select').wait_for()
            check('initial_recorded_value', page.get_by_test_id('lab-result-value').locator('b').inner_text() == '10')

            prefix += encoded(sensor(2, 20))
            append(encoded(sensor(2, 20)))
            check('appended_count_without_reload', eventually(page, lambda: event_count() == 3))
            check('follow_appends_latest_cursor_and_value', eventually(page, lambda: cursor() == 2 and page.get_by_test_id('lab-result-value').locator('b').inner_text() == '20'))
            check('live_revision_advances', read_run(original_id)['revision'] >= 2)
            shot('live_follow')
            page.set_viewport_size({'width': 390, 'height': 844})
            shape = page.locator('main').evaluate('(e)=>({width:e.clientWidth,scroll:e.scrollWidth})')
            check('live_mobile_no_horizontal_overflow', shape['scroll'] <= shape['width'] + 1, shape)
            page.get_by_test_id('lab-live-source').evaluate('(e)=>e.scrollIntoView({block:"start"})')
            shot('live_follow_390')
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.get_by_label('Добавить оператор', exact=True).select_option('raw')
            page.get_by_test_id('lab-add-operator').click()
            check('live_pipeline_selected', page.get_by_test_id('lab-operator').count() == 1)

            # A complete JSON object still waits for its newline commit boundary.
            partial = encoded(sensor(3, 30))[:-1]
            append(partial)
            page.wait_for_timeout(2300)
            check('partial_last_line_withheld', event_count() == 3 and cursor() == 2 and len(read_run(original_id)['events']) == 3)
            export = context.request.get(base + '/api/lab/export/' + original_id)
            check('partial_bytes_not_exported_as_record', export.body() == prefix)
            append(b'\n')
            prefix += partial + b'\n'
            check('newline_commits_held_record', eventually(page, lambda: event_count() == 4 and cursor() == 3 and page.get_by_test_id('lab-result-value').locator('b').inner_text() == '30'))
            check('live_pipeline_preserved_across_revision', page.get_by_test_id('lab-operator').count() == 1)
            page.get_by_test_id('lab-operator').get_by_role('button', name='Удалить оператор 1', exact=True).click()

            page.get_by_label('Момент эксперимента').evaluate('(e)=>{e.value="1";e.dispatchEvent(new Event("input",{bubbles:true}));}')
            check('manual_seek_disables_follow', eventually(page, lambda: follow.get_attribute('aria-pressed') == 'false'))
            past_cursor = cursor()
            prefix += encoded(sensor(4, 40))
            append(encoded(sensor(4, 40)))
            check('inspection_receives_new_records', eventually(page, lambda: event_count() == 5))
            check('inspection_preserves_past_cursor_and_value', cursor() == past_cursor and page.get_by_test_id('lab-result-value').locator('b').inner_text() == '10')
            follow.click()
            check('follow_resumes_latest_record', eventually(page, lambda: follow.get_attribute('aria-pressed') == 'true' and cursor() == 4 and page.get_by_test_id('lab-result-value').locator('b').inner_text() == '40'))
            page.get_by_test_id('lab-capture-result').click()
            page.locator('.lab-tabs').get_by_role('button', name=re.compile('Журнал$')).click()
            card = page.get_by_test_id('lab-evidence-card')
            card.wait_for()
            check('live_evidence_initial_source_link', card.get_by_role('button', name='Перейти к источнику →').is_enabled())
            page.locator('.lab-tabs').get_by_role('button', name=re.compile('Инструменты$')).click()

            with page.expect_download() as info:
                page.locator('.lab-export').click()
            info.value.save_as(str(args.out / 'QAtrace-original.jsonl'))
            check('original_export_byte_exact_ids_unknown_fields', (args.out / 'QAtrace-original.jsonl').read_bytes() == prefix)

            append(b'{"type":"input",BROKEN_QAtrace}\n')
            check('malformed_complete_line_is_watcher_error', eventually(page, lambda: watched()['status'] == 'error'))
            check('watcher_error_visible_without_reload', eventually(page, lambda: bool(re.search('ошиб|устар|stale|JSON', page.get_by_test_id('lab-live-source').inner_text(), re.I))))
            check('malformed_input_keeps_last_good_values', cursor() == 4 and event_count() == 5 and page.get_by_test_id('lab-result-value').locator('b').inner_text() == '40')
            check('backend_stale_and_original_export_retained', read_run(original_id).get('stale') is True and context.request.get(base + '/api/lab/export/' + original_id).body() == prefix)
            shot('live_source_error')

            # Repair only the failed tail, keeping the previously accepted prefix.
            prefix += encoded(sensor(5, 50))
            location.write_bytes(prefix)
            check('valid_repair_recovers_same_run', eventually(page, lambda: watched()['status'] == 'watching' and watched()['run_id'] == original_id))
            check('recovery_updates_browser_and_clears_stale', eventually(page, lambda: cursor() == 5 and event_count() == 6 and page.get_by_test_id('lab-result-value').locator('b').inner_text() == '50') and not read_run(original_id).get('stale'))
            page.locator('.lab-tabs').get_by_role('button', name=re.compile('Журнал$')).click()
            check('live_evidence_lineage_survives_append_sha_change', page.get_by_test_id('lab-evidence-card').get_by_role('button', name='Перейти к источнику →').is_enabled())
            page.get_by_test_id('lab-evidence-card').get_by_role('button', name='Перейти к источнику →').click()
            check('live_evidence_seeks_original_moment', cursor() == 4 and follow.get_attribute('aria-pressed') == 'false')
            follow.click()
            page.locator('.lab-tabs').get_by_role('button', name=re.compile('Инструменты$')).click()

            # A new inode represents a new trace. Its timestamps and IDs deliberately
            # overlap the previous run so an accidental merge cannot pass.
            rotated = encoded({'type': 'run_start', 'run_id': 'QAtrace-rotated', 'clock': 'virtual', 'ts_clock': 0}) + encoded(sensor(1, 900, 'QAtrace-rotated-input'))
            replacement = location.with_suffix('.next')
            replacement.write_bytes(rotated)
            replacement.replace(location)
            check('rotation_creates_distinct_run', eventually(page, lambda: watched().get('run_id') and watched()['run_id'] != original_id))
            rotated_id = watched()['run_id']
            runs = doc()['runs']
            check('rotation_preserves_two_distinct_histories', {original_id, rotated_id}.issubset({r['id'] for r in runs}) and len(read_run(original_id)['events']) == 6 and len(read_run(rotated_id)['events']) == 2)
            check('old_run_export_is_frozen', context.request.get(base + '/api/lab/export/' + original_id).body() == prefix)
            check('rotated_run_picker_discovered', eventually(page, lambda: page.get_by_test_id('lab-run-select').locator('option[value="' + rotated_id + '"]').count() == 1))
            page.get_by_test_id('lab-run-select').select_option(rotated_id)
            check('rotated_values_not_merged', eventually(page, lambda: event_count() == 2 and page.get_by_test_id('lab-result-value').locator('b').inner_text() == '900'))
            check('rotated_export_exact', context.request.get(base + '/api/lab/export/' + rotated_id).body() == rotated)
            shot('live_rotated_run')
            context.close()
            browser.close()
    except Exception as exc:
        report['errors'].append(str(exc))
        report['traceback'] = traceback.format_exc()
        if page is not None:
            try:
                page.screenshot(path=str(args.out / 'failure.png'))
            except Exception:
                pass
    finally:
        srv.shutdown()
        srv.server_close()
        worker.join(timeout=3)
        directory.cleanup()
    check('server_and_watcher_threads_stopped', not worker.is_alive() and not any(t.name.startswith('edgelab-trace-' + watcher['id']) and t.is_alive() for t in threading.enumerate()))
    check('temporary_trace_removed', not location.exists())
    check('server_socket_closed', srv.fileno() == -1)
    (args.out / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'checks': len(report['checks']), 'errors': report['errors']}, ensure_ascii=False))
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
