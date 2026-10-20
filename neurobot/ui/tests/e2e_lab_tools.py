"""Focused, real-browser EdgeLab instruments and evidence acceptance.

Uses the existing local demo and imports one explicitly named QAtools trace.
No hardware commands, models, providers, VMs or experiment runners are invoked.
"""
import argparse
import json
from pathlib import Path
import re
import traceback

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:18127')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report = {'checks': [], 'pages': [], 'errors': [], 'downloads': []}
    base = args.base.rstrip('/')

    def check(name, passed, detail=None):
        report['checks'].append({'name': name, 'ok': bool(passed), 'detail': detail})
        if not passed:
            report['errors'].append(name)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                                      locale='ru-RU', reduced_motion='reduce', accept_downloads=True)
        page = context.new_page()
        page.on('pageerror', lambda e: report['errors'].append('pageerror: ' + str(e)))
        page.on('console', lambda m: report['errors'].append('console: ' + m.text) if m.type == 'error' else None)
        page.on('requestfailed', lambda r: report['errors'].append('requestfailed: ' + r.url)
                if r.failure != 'net::ERR_ABORTED' else None)

        def tab(name):
            page.locator('.lab-tabs').get_by_role('button', name=re.compile(name + '$')).click()
            page.locator('[data-testid="lab-tools"]').wait_for()

        def seek(t):
            page.get_by_label('Момент эксперимента').evaluate(
                '(el,t)=>{el.value=t;el.dispatchEvent(new Event("input",{bubbles:true}));}', str(t))

        def add(op):
            page.get_by_label('Добавить оператор', exact=True).select_option(op)
            page.get_by_test_id('lab-add-operator').click()

        def clear_ops():
            while page.get_by_test_id('lab-operator').count():
                page.get_by_test_id('lab-operator').first.get_by_role('button', name=re.compile('Удалить оператор')).click()

        def download(button, file):
            with page.expect_download() as info:
                button.click()
            target = args.out / file
            info.value.save_as(str(target))
            report['downloads'].append(file)
            return target

        try:
            index = context.request.get(base + '/api/lab').json()
            gate = next(r for r in index['runs'] if 'gate' in r['label'] and r['kind'] == 'experiment')
            heartbeat = next(r for r in index['runs'] if 'heartbeat' in r['label'] and r['kind'] == 'experiment')
            run = context.request.get(base + '/api/lab/runs/' + gate['id']).json()
            check('real_actual_edge_ctx_run', run['provenance']['execution'] == 'actual_go_agent_local_oracle')
            page.goto(base + '/?demo=1#/lab', wait_until='networkidle')
            page.get_by_test_id('lab-run-select').select_option(gate['id'])
            tab('Инструменты')
            options = page.get_by_test_id('lab-signal-select').locator('option')
            temp = next(o.get_attribute('value') for o in options.all() if 'thermo_lab / temperature' in o.inner_text())
            page.get_by_test_id('lab-signal-select').select_option(temp)
            check('physical_unit_from_scenario_schema', '°C' in page.locator('.lab-signal-contract').inner_text())
            check('prompt_schema_not_discovered_as_signals', not any('context_snapshot' in o.inner_text() for o in options.all()))
            seek(170)
            add('average')
            add('derivative')
            add('threshold')
            check('ordered_three_step_pipeline', page.get_by_test_id('lab-operator').evaluate_all('(es)=>es.map(e=>e.dataset.op)') == ['average', 'derivative', 'threshold'])
            check('typed_derivative_unit', '°C/с' in page.get_by_test_id('lab-operator').nth(1).inner_text())
            page.get_by_role('button', name='Поднять оператор 3', exact=True).click()
            check('reorder_recomputes_invalid_boolean_derivative', page.get_by_test_id('lab-capture-result').is_disabled() and 'числовой' in page.locator('.lab-result').inner_text().lower())
            page.get_by_role('button', name='Опустить оператор 2', exact=True).click()
            check('reorder_restores_valid_pipeline', page.get_by_test_id('lab-capture-result').is_enabled())
            page.get_by_role('button', name='Удалить оператор 3', exact=True).click()
            check('remove_operator', page.get_by_test_id('lab-operator').count() == 2)
            avg = page.get_by_test_id('lab-operator').first
            avg.get_by_label('Окно, с', exact=True).fill('-1')
            check('invalid_parameter_blocks_capture', page.get_by_test_id('lab-capture-result').is_disabled())
            avg.get_by_label('Окно, с', exact=True).fill('10')
            check('parameter_error_recovers', page.get_by_test_id('lab-capture-result').is_enabled())
            recipe_path = download(page.get_by_role('button', name='Экспорт рецепта ↗'), 'recipe.json')
            recipe = json.loads(recipe_path.read_text())
            check('recipe_links_actual_source_sha', recipe['source']['value'] == run['provenance']['raw_trace_sha256'])
            clear_ops()
            page.locator('.lab-result input[type=file]').set_input_files(str(recipe_path))
            page.get_by_test_id('lab-operator').nth(1).wait_for()
            check('recipe_roundtrip', page.get_by_test_id('lab-operator').evaluate_all('(es)=>es.map(e=>e.dataset.op)') == ['average', 'derivative'])
            page.get_by_label('Диапазон обработки').select_option('cursor')
            partial_path = download(page.get_by_role('button', name='Экспорт рецепта ↗'), 'recipe-partial.json')
            partial = json.loads(partial_path.read_text())
            page.get_by_label('Диапазон обработки').select_option('all')
            seek(0)
            page.locator('.lab-result input[type=file]').set_input_files(str(partial_path))
            page.wait_for_timeout(100)
            check('recipe_restores_processing_boundary', page.get_by_label('Диапазон обработки').input_value() == 'cursor' and abs(float(page.get_by_label('Момент эксперимента').input_value()) - partial['until']) < .05)
            page.get_by_test_id('lab-capture-result').click()
            # Deliberately navigate immediately: evidence cannot wait for a debounce.
            tab('Журнал')
            card = page.get_by_test_id('lab-evidence-card').first
            card.wait_for()
            check('capture_survives_immediate_journal_navigation', page.get_by_test_id('lab-evidence-card').count() == 1)
            note = 'QAtools: подтверждено исходной трассой. <img src=x onerror="window.QAtoolsInjected=1">\n``` source ```'
            card.get_by_role('textbox').fill(note)
            page.reload(wait_until='networkidle')
            page.get_by_test_id('lab-run-select').select_option(gate['id'])
            tab('Журнал')
            card = page.get_by_test_id('lab-evidence-card').first
            check('annotation_persists_immediate_reload', card.get_by_role('textbox').input_value() == note)
            stored = page.evaluate('JSON.parse(localStorage.getItem("edgelab.evidence.v1"))')
            evidence = stored['cards'][0]
            check('evidence_has_source_run_hash_config_lineage', evidence['run']['id'] == gate['id'] and evidence['run']['source']['value'] == run['provenance']['raw_trace_sha256'] and bool(evidence['config']) and len(evidence['result']['lineage']) == 2, evidence['result'])
            source_sample = next(e for e in run['events'] if e['id'] == evidence['result']['sourceEventId'])
            check('result_links_real_nonfuture_sensor_event', source_sample['type'] == 'input' and source_sample['t'] <= evidence['cursor'])
            seek(0)
            card.get_by_role('button', name='Перейти к источнику →').click()
            check('source_link_seeks_saved_moment', page.get_by_label('Момент эксперимента').input_value() == str(evidence['cursor']))
            notebook_path = download(page.locator('.lab-book-exports').get_by_role('button', name='JSON', exact=True), 'evidence.json')
            html_path = download(page.locator('.lab-book-exports').get_by_role('button', name='HTML', exact=True), 'evidence.html')
            md_path = download(page.locator('.lab-book-exports').get_by_role('button', name='Markdown', exact=True), 'evidence.md')
            check('html_export_escapes_user_text', '&lt;img src=x onerror=' in html_path.read_text() and '<img src=x' not in html_path.read_text())
            check('markdown_fence_preserves_untrusted_text', '````\n' + note in md_path.read_text())
            preview = context.new_page()
            preview.goto(html_path.as_uri(), wait_until='load')
            check('selfcontained_html_has_no_active_payload', preview.locator('article').count() == 1 and preview.locator('script,img').count() == 0 and preview.evaluate('window.QAtoolsInjected') is None)
            preview.close()
            card.get_by_role('button', name='Удалить карточку', exact=True).click()
            check('card_delete', page.get_by_test_id('lab-evidence-card').count() == 0)
            page.get_by_test_id('lab-notebook-import').set_input_files(str(notebook_path))
            page.get_by_test_id('lab-evidence-card').wait_for()
            check('json_notebook_roundtrip_annotation', page.get_by_test_id('lab-evidence-card').get_by_role('textbox').input_value() == note)
            page.get_by_test_id('lab-notebook-import').set_input_files(str(notebook_path))
            page.wait_for_timeout(100)
            check('notebook_merge_deduplicates', page.get_by_test_id('lab-evidence-card').count() == 1)
            page.get_by_test_id('lab-run-select').select_option(heartbeat['id'])
            check('cross_run_source_seek_disabled', page.get_by_test_id('lab-evidence-card').get_by_role('button', name='Откройте соответствующий прогон для перехода').is_disabled())
            page.get_by_test_id('lab-run-select').select_option(gate['id'])
            page.get_by_test_id('lab-evidence-card').get_by_role('button', name='Перейти к источнику →').wait_for()
            # Explicit imported observation fixture tests gaps, zero, boolean and declared units.
            records = [{'type': 'run_start', 'clock': 'virtual', 'ts_clock': 0, 'run_id': 'QAtools-gaps'}]
            records += [{'type': 'input', 'kind': 'sensor', 'device': 'QAtools-device', 'property': 'temperature',
                         'unit': 'Cel', 'value': value, 'ts_clock': t}
                        for t, value in [(0, 0), (1, 2), (2, None), (3, 6), (4, 8), (5, None), (6, 10), (100, 12)]]
            records += [{'type': 'input', 'kind': 'sensor', 'device': 'QAtools-device', 'property': 'ready', 'value': False, 'ts_clock': 100},
                        {'type': 'run_end', 'ts_clock': 100}]
            response = context.request.post(base + '/api/lab/import', headers={'Origin': base}, data={'name': 'QAtools · gaps and false', 'events': records})
            check('bounded_gap_fixture_imported', response.status == 201, response.text()[:300])
            fixture = response.json()['run']
            page.reload(wait_until='networkidle')
            page.get_by_test_id('lab-run-select').select_option(fixture['id'])
            tab('Инструменты')
            seek(100)
            selector = page.get_by_test_id('lab-signal-select')
            scalar = next(o.get_attribute('value') for o in selector.locator('option').all() if 'temperature' in o.inner_text())
            selector.select_option(scalar)
            seek(0)
            check('zero_value_is_not_missing', page.get_by_test_id('lab-result-value').locator('b').inner_text() == '0')
            seek(2)
            check('explicit_null_is_gap', page.get_by_test_id('lab-result-value').locator('b').inner_text() == '—')
            path = page.get_by_test_id('lab-source-chart').locator('path').get_attribute('d')
            check('chart_preserves_null_gaps', path.count('M') == 3, path)
            add('average')
            seek(1)
            check('causal_average_actual_values', page.get_by_test_id('lab-result-value').locator('b').inner_text() == '1')
            seek(3)
            check('average_resets_at_gap', page.get_by_test_id('lab-result-value').locator('b').inner_text() == '6')
            clear_ops()
            add('derivative')
            seek(4)
            check('derivative_actual_values_and_units', page.get_by_test_id('lab-result-value').inner_text().replace('\n', '').startswith('2') and '°C/с' in page.get_by_test_id('lab-result-value').inner_text())
            seek(100)
            check('derivative_gap_is_not_interpolated', page.get_by_test_id('lab-result-value').locator('b').inner_text() == '—')
            clear_ops()
            add('threshold')
            op = page.get_by_test_id('lab-operator')
            op.get_by_label('Значение порога', exact=True).fill('5')
            op.get_by_label('Выдержка, с', exact=True).fill('1')
            seek(3)
            check('threshold_waits_actual_hold', page.get_by_test_id('lab-result-value').locator('b').inner_text() == 'false')
            seek(4)
            check('threshold_after_hold', page.get_by_test_id('lab-result-value').locator('b').inner_text() == 'true')
            seek(6)
            check('threshold_gap_resets_hold', page.get_by_test_id('lab-result-value').locator('b').inner_text() == 'false')
            clear_ops()
            ready = next(o.get_attribute('value') for o in selector.locator('option').all() if 'ready' in o.inner_text())
            selector.select_option(ready)
            seek(100)
            check('boolean_false_not_missing', page.get_by_test_id('lab-result-value').locator('b').inner_text() == 'false')
            add('derivative')
            check('boolean_derivative_explicit_error', page.get_by_test_id('lab-capture-result').is_disabled())
            clear_ops()
            selector.select_option(scalar)
            add('average')
            add('derivative')
            for theme in ['dark', 'light']:
                for width, height in [(1440, 1000), (390, 844)]:
                    page.set_viewport_size({'width': width, 'height': height})
                    page.evaluate('(theme)=>{document.documentElement.dataset.theme=theme;localStorage.setItem("nb-theme",theme);}', theme)
                    for name in ['Инструменты', 'Журнал']:
                        tab(name)
                        shape = page.locator('main').evaluate('(e)=>({width:e.clientWidth,scroll:e.scrollWidth,body:document.body.scrollWidth,viewport:innerWidth})')
                        check('no_overflow_%s_%s_%s' % (name, width, theme), shape['scroll'] <= shape['width'] + 1 and shape['body'] <= shape['viewport'] + 1, shape)
                        label = 'analysis' if name == 'Инструменты' else 'notebook'
                        page.locator('[data-testid=lab-tools]').evaluate('(e)=>e.scrollIntoView({block:"start"})')
                        page.screenshot(path=str(args.out / ('%s_%s_%s.png' % (label, width, theme))), animations='disabled')
                        report['pages'].append({'theme': theme, 'width': width, 'tab': label})
            # Final review images use the actual executed experiment, with its
            # source-matching notebook rather than the explicit gap fixture.
            page.get_by_test_id('lab-run-select').select_option(gate['id'])
            tab('Инструменты')
            clear_ops()
            page.get_by_test_id('lab-signal-select').select_option(temp)
            page.get_by_label('Диапазон обработки').select_option('all')
            add('average')
            add('derivative')
            seek(80)
            for width, height in [(1440, 1000), (390, 844)]:
                page.set_viewport_size({'width': width, 'height': height})
                page.evaluate('document.documentElement.dataset.theme="dark"')
                tab('Инструменты')
                page.get_by_test_id('lab-tools').evaluate('(e)=>e.scrollIntoView({block:"start"})')
                page.screenshot(path=str(args.out / ('actual_analysis_%s_dark.png' % width)), animations='disabled')
                tab('Журнал')
                page.get_by_test_id('lab-evidence-card').get_by_role('textbox').fill('QAtools · Среднее по записанным наблюдениям и его производная. Разрывы не заменены нулями; исходная трасса, конфигурация и рецепт сохранены вместе с результатом.')
                page.get_by_test_id('lab-tools').evaluate('(e)=>e.scrollIntoView({block:"start"})')
                page.screenshot(path=str(args.out / ('actual_notebook_%s_dark.png' % width)), animations='disabled')
        except Exception as error:
            report['errors'].append(str(error))
            report['traceback'] = traceback.format_exc()
            page.screenshot(path=str(args.out / 'failure.png'))
        finally:
            browser.close()
    (args.out / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'checks': len(report['checks']), 'pages': len(report['pages']), 'errors': report['errors']}, ensure_ascii=False))
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
