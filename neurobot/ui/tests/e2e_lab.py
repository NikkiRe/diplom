"""Browser acceptance for EdgeLab on a running local demo.

  python neurobot/ui/tests/e2e_lab.py --base http://127.0.0.1:18127 --out /tmp/lab-qa [--exercise-runner]

Uses local simulator data only. --exercise-runner launches one bounded actual
EdgeCtx/mock experiment; never uses a provider, robot or virtual machine.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import time

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from neurobot.ui.tests.e2e_equipment import UnknownMCP  # noqa: E402

SIZES = [(390, 844), (768, 1024), (1366, 900), (1440, 1000)]
TABS = ['Наблюдение', 'Решения', 'Эксперименты', 'Инструменты', 'Журнал']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:18127')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--exercise-runner', action='store_true')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report = {'checks': [], 'pages': [], 'errors': []}
    def check(name, passed, detail=None):
        report['checks'].append({'name': name, 'ok': bool(passed), 'detail': detail})
        if not passed:
            report['errors'].append(name)
    base = args.base.rstrip('/')
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={'width': 1440, 'height': 1000}, locale='ru-RU', reduced_motion='reduce')
        index = ctx.request.get(base + '/api/lab').json()
        runs = index['runs']
        check('actual_seeded_pair_present', len(runs) >= 2)
        gate = next((r for r in runs if 'gate' in r['label']), runs[-1])
        heartbeat = next((r for r in runs if 'heartbeat' in r['label']), runs[0])
        run = ctx.request.get(base + '/api/lab/runs/' + gate['id']).json()
        check('actual_execution', run['provenance'].get('execution') == 'actual_go_agent_local_oracle')
        check('recorded_context', all(e['context_snapshot']['complete'] for e in run['events'] if e['type'] == 'llm_request'))
        page = ctx.new_page()
        page.on('pageerror', lambda error: report['errors'].append(str(error)))
        page.on('console', lambda message: report['errors'].append(message.text) if message.type == 'error' else None)
        for theme in ['dark', 'light']:
            for width, height in SIZES:
                page.set_viewport_size({'width': width, 'height': height})
                page.goto(base + '/?demo=1&theme=' + theme + '#/lab', wait_until='networkidle')
                page.locator('[data-testid="lab-run-select"]').select_option(gate['id'])
                page.locator('[data-testid="lab-timeline"]').wait_for() if page.locator('[data-testid="lab-timeline"]').count() else page.locator('.lab-timebench').wait_for()
                for tab in TABS:
                    page.locator('.lab-tabs').get_by_role('button', name=re.compile(tab + '$')).click()
                    page.wait_for_timeout(100)
                    layout = page.locator('main').evaluate('(e)=>({width:e.clientWidth,scroll:e.scrollWidth,height:e.clientHeight})')
                    check('width_%s_%s_%s' % (width, theme, tab), layout['scroll'] <= layout['width'] + 1, layout)
                    report['pages'].append({'width': width, 'theme': theme, 'tab': tab})
                    if width in (390, 1440):
                        name = {'Наблюдение': 'observe', 'Решения': 'decisions', 'Эксперименты': 'experiments', 'Инструменты': 'analysis', 'Журнал': 'journal'}[tab]
                        page.screenshot(path=str(args.out / ('%s_%s_%s.png' % (name, width, theme))), animations='disabled')
        # Common cursor moves actual scenario observations and recorded context together.
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.goto(base + '/?demo=1#/lab', wait_until='networkidle')
        page.locator('[data-testid="lab-run-select"]').select_option(gate['id'])
        page.locator('.lab-timebench').wait_for()
        page.locator('input[aria-label="Момент эксперимента"]').evaluate('(el)=>{el.value="67.8";el.dispatchEvent(new Event("input",{bubbles:true}));}')
        check('cursor_shared', '1:07' in page.locator('[data-testid="lab-cursor"]').inner_text())
        page.locator('.lab-tabs').get_by_role('button', name=re.compile('Решения$')).click()
        check('causal_microscope', page.locator('.lab-chain-stage').count() == 4)
        # Actual paired input identity and deadline results are visible alongside costs.
        page.locator('.lab-tabs').get_by_role('button', name=re.compile('Эксперименты$')).click()
        page.get_by_label('Прогон для сравнения').select_option(heartbeat['id'])
        page.wait_for_timeout(200)
        check('paired_quality_and_provenance', page.locator('.lab-comparison').inner_text().find('дедлайн') >= 0 or page.locator('.lab-comparison').inner_text().find('срок') >= 0)
        page.locator('.lab-comparison').screenshot(path=str(args.out / 'comparison.png'), animations='disabled')
        if args.exercise_runner:
            before = {r['id'] for r in ctx.request.get(base + '/api/lab').json()['runs']}
            page.locator('.lab-experiment-form').get_by_label('Название', exact=False).fill('QA · actual hybrid')
            page.locator('.lab-policy-picker').get_by_role('button', name=re.compile('Hybrid')).click()
            page.locator('[data-testid="lab-launch"]').click()
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                latest = ctx.request.get(base + '/api/lab').json()
                if any(r['id'] not in before for r in latest['runs']):
                    break
                page.wait_for_timeout(400)
            new = [r for r in latest['runs'] if r['id'] not in before]
            check('actual_ui_experiment_completed', bool(new), latest['jobs'][-1])
        # Unknown identity and arbitrary fields; no write/action RPC is ever invoked.
        mcp = UnknownMCP()
        mcp.__enter__()
        source_id = None
        try:
            response = ctx.request.post(base + '/api/catalog/sources', headers={'Origin': base}, data={'url': mcp.url, 'label': 'QA · unknown instrument'})
            check('unknown_source_connected', response.status == 200, response.status)
            source_id = response.json().get('source_id')
            page.goto(base + '/?demo=1#/equipment', wait_until='networkidle')
            page.locator('[data-device-id$="' + mcp.device_id + '"]').click()
            page.get_by_role('button', name='Прибор устройства', exact=True).click()
            page.locator('[data-testid="capability-instrument"]').wait_for()
            scalar = next(o.get_attribute('value') for o in page.get_by_label('Поле прибора').locator('option').all() if 'mystery_flux' in o.inner_text())
            page.get_by_label('Поле прибора').select_option(scalar)
            check('unknown_scalar_rendered', page.locator('[data-testid="instrument-value"]').inner_text() != '—')
            page.get_by_label('Тип прибора').select_option('boolean')
            options = page.get_by_label('Поле прибора').locator('option')
            ready = next(o.get_attribute('value') for o in options.all() if 'is_ready' in o.inner_text())
            page.get_by_label('Поле прибора').select_option(ready)
            check('false_not_missing', 'FALSE' in page.locator('.ci-display').inner_text())
            page.wait_for_timeout(2200)
            page.locator('[data-testid="capture-episode"]').click()
            page.locator('[data-testid="lab-screen"]').wait_for()
            page.wait_for_timeout(500)
            check('live_episode_opens_lab', 'Эпизод MCP' in page.locator('[data-testid="lab-run-select"]').locator('option:checked').inner_text())
            check('no_llm_fabricated', '0' in page.locator('.lab-run-counts').inner_text())
            write_calls = [r for r in mcp.calls if r['method'] == 'tools/call' and r.get('params', {}).get('name') != 'list_devices']
            check('no_hardware_tool_execution', not write_calls, write_calls)
        finally:
            if source_id:
                ctx.request.post(base + '/api/catalog/disconnect', headers={'Origin': base}, data={'id': source_id})
            mcp.__exit__()
        browser.close()
    (args.out / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'pages': len(report['pages']), 'checks': len(report['checks']), 'errors': report['errors']}, ensure_ascii=False))
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
