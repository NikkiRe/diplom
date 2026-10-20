"""Durable assistant journal, credential boundaries and actual Go/mock chat.

Every model endpoint in these tests is an owned loopback fixture. No paid model
or physical device is contacted. Integration builds only the local Go agent.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
import unittest

from neurobot.ui import assistant


class LocalAPI:
    """Explicit test provider + attachable channel, never an actual model."""
    def __init__(self):
        self.calls = []
        self.facts = []
        self.redirect = False
        self.fail_chat = False
        self.tool_once = False
        self.delay = 0
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, value):
                raw = json.dumps(value).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):  # noqa: N802
                fixture.calls.append(('GET', self.path, None, self.headers.get('Authorization')))
                if self.path == '/v1/models':
                    if fixture.redirect:
                        self.send_response(302)
                        self.send_header('Location', fixture.url + '/v1/chat/completions')
                        self.end_headers()
                    else:
                        self.reply(200, {'data': [{'id': 'QAtools-model'}]})
                elif self.path == '/v1/health':
                    self.reply(200, {'ok': True, 'run_id': 'QAtools-external-agent', 'idle': True, 'clock': 'real'})
                elif self.path == '/v1/context':
                    self.reply(200, {'facts': fixture.facts, 'summary': '', 'history_turns': 0,
                                     'turns': [], 'visible_history': [], 'profile_text': '',
                                     'policy': {'history': 'window', 'window_turns': 12}, 'hardware': {'devices': []}})
                else:
                    self.reply(404, {'error': 'not found'})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                fixture.calls.append(('POST', self.path, body, self.headers.get('Authorization')))
                if fixture.delay:
                    time.sleep(fixture.delay)
                if self.path == '/v1/chat/completions':
                    if fixture.fail_chat:
                        self.reply(503, {'error': {'message': 'QAtools echoed header ' + str(self.headers.get('Authorization'))}})
                    elif fixture.tool_once:
                        fixture.tool_once = False
                        name = next(t['function']['name'] for t in body['tools'] if t['function']['name'].endswith('read_property'))
                        self.reply(200, {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': None,
                            'tool_calls': [{'id': 'QAtools-read', 'type': 'function', 'function': {
                                'name': name, 'arguments': json.dumps({'device': 's1/test', 'property': 'off'})}}]},
                            'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}})
                    else:
                        count = sum(c[1] == '/v1/chat/completions' for c in fixture.calls)
                        self.reply(200, {'id': 'QAtools-response-' + str(count), 'object': 'chat.completion',
                                         'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'QAtools ответ ' + str(count)}, 'finish_reason': 'stop'}],
                                         'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}})
                elif self.path == '/v1/user':
                    self.reply(200, {'input_id': body['id'], 'wake_id': 'QAtools-wake', 'text': 'Ответ внешнего тестового канала',
                                     'ok': True, 'llm_calls': 1, 'tool_calls': 0, 'latency_ms': 1})
                elif self.path == '/v1/context/facts':
                    op, identity = body['operation'], body.get('id') or 'f1'
                    if op == 'add':
                        fixture.facts.append({'id': identity, 'text': body['text']})
                    elif op == 'update':
                        for fact in fixture.facts:
                            if fact['id'] == identity:
                                fact['text'] = body['text']
                    else:
                        fixture.facts[:] = [f for f in fixture.facts if f['id'] != identity]
                    self.reply(200, {'ok': True})
                elif self.path == '/v1/context/history/clear':
                    self.reply(200, {'ok': True})
                else:
                    self.reply(404, {'error': 'not found'})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def wait_job(store, identity, timeout=15):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        job = next(j for j in store.snapshot()['jobs'] if j['id'] == identity)
        if job['status'] not in ('queued', 'running'):
            return job
        time.sleep(.05)
    raise AssertionError('assistant job timed out: ' + identity)


class AssistantStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='QAtools-assistant-test-')
        self.addCleanup(self.temp.cleanup)
        self.store = assistant.AssistantStore(state_dir=self.temp.name)
        self.addCleanup(self.store.close)

    def call(self, route, body):
        return assistant.mutate(self.store, '/api/assistant' + route, body)

    def local(self):
        try:
            fixture = LocalAPI()
        except PermissionError:
            self.skipTest('loopback sockets unavailable in this sandbox')
        self.addCleanup(fixture.close)
        return fixture

    def test_private_durable_configuration_masks_secret_and_preserves_on_omission(self):
        key = 'QAtools-PRIVATE-NOT-A-REAL-PROVIDER-KEY'
        code, _ = self.call('/provider', {'base_url': 'https://provider.invalid/v1', 'model': 'some-model', 'api_key': key})
        self.assertEqual(code, 200)
        self.assertNotIn(key, json.dumps(self.store.snapshot()))
        self.assertTrue(self.store.snapshot()['provider']['has_key'])
        path = Path(self.temp.name) / 'provider.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(self.temp.name).stat().st_mode), 0o700)
        self.call('/provider', {'base_url': 'https://provider.invalid/v1', 'model': 'other-model'})
        self.assertEqual(json.loads(path.read_text())['api_key'], key)
        self.store.close()
        self.store = assistant.AssistantStore(state_dir=self.temp.name)
        self.addCleanup(self.store.close)
        self.assertTrue(self.store.snapshot()['provider']['has_key'])
        self.assertEqual(self.store.snapshot()['provider']['model'], 'other-model')
        self.assertNotIn(key, json.dumps(self.store.snapshot()))

    def test_provider_test_only_get_models_no_redirect_no_completion(self):
        fixture = self.local()
        self.call('/provider', {'base_url': fixture.url + '/v1', 'model': 'QAtools-model', 'api_key': 'QAtools-key'})
        code, result = self.call('/provider/test', {})
        self.assertEqual((code, result['models']), (200, ['QAtools-model']))
        self.assertFalse(result['completion_requested'])
        self.assertEqual([(r[0], r[1]) for r in fixture.calls], [('GET', '/v1/models')])
        fixture.redirect = True
        self.assertEqual(self.call('/provider/test', {})[0], 502)
        self.assertEqual(len(fixture.calls), 2)

    def test_loopback_attach_actual_context_mutation_and_external_chat(self):
        fixture = self.local()
        self.assertEqual(self.call('/agent/connect', {'url': fixture.url})[0], 200)
        self.assertTrue(self.store.snapshot()['memory']['available'])
        self.assertEqual(self.call('/memory', {'op': 'ADD', 'text': 'actual fixture fact'})[0], 200)
        self.assertEqual(self.store.snapshot()['memory']['facts'][0]['text'], 'actual fixture fact')
        self.assertEqual(self.call('/memory', {'op': 'UPDATE', 'fact_id': 'f1', 'text': 'changed'})[0], 200)
        self.assertEqual(fixture.facts[0]['text'], 'changed')
        session = self.call('/sessions', {'title': 'QAtools external'})[1]['session']
        code, response = self.call('/chat', {'session_id': session['id'], 'text': 'Привет тестовому каналу'})
        self.assertEqual(code, 202)
        self.assertEqual(wait_job(self.store, response['job']['id'])['status'], 'completed')
        messages = self.store.session(session['id'])['session']['messages']
        self.assertEqual(messages[-1]['text'], 'Ответ внешнего тестового канала')
        self.assertFalse(messages[-1]['trace']['available'])
        self.assertEqual(sum(c[1] == '/v1/user' for c in fixture.calls), 1)
        self.assertEqual(self.call('/memory', {'op': 'DELETE', 'fact_id': 'f1'})[0], 200)
        self.assertEqual(fixture.facts, [])
        self.assertEqual(self.call('/history', {'op': 'clear'})[0], 400)
        self.assertEqual(self.call('/history', {'op': 'clear', 'confirm': True})[0], 200)
        self.call('/agent/stop', {})
        self.assertNotIn('/v1/shutdown', [c[1] for c in fixture.calls])

    def test_durable_journal_restart_export_and_delete_does_not_clear_agent(self):
        session = self.call('/sessions', {'title': 'QAtools persisted'})[1]['session']
        identity = session['id']
        exported = self.store.export(identity)
        self.assertEqual(exported['format'], 'edgelab-chat/1')
        self.store.close()
        self.store = assistant.AssistantStore(state_dir=self.temp.name)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.session(identity)['session']['title'], 'QAtools persisted')
        code, result = self.call('/sessions/delete', {'session_id': identity})
        self.assertEqual(code, 200)
        self.assertFalse(result['agent_history_changed'])
        self.assertEqual(self.store.snapshot()['sessions'], [])

    def test_invalid_configuration_runtime_disabled_and_storage_bounds(self):
        for body in [{'base_url': 'file:///etc/passwd', 'model': 'm'},
                     {'base_url': 'https://key:secret@example.com', 'model': 'm'},
                     {'base_url': 'http://example.com/v1', 'model': 'm'},
                     {'base_url': 'https://example.com', 'model': ''}]:
            self.assertEqual(self.call('/provider', body)[0], 400)
        self.assertEqual(self.call('/agent/connect', {'url': 'http://192.168.1.20:18080'})[0], 400)
        self.assertEqual(self.call('/chat', {'session_id': 'missing', 'text': ''})[0], 400)
        self.assertEqual(self.call('/memory', {'op': 'EXECUTE', 'text': 'x'})[0], 400)
        self.assertEqual(self.call('/history', {'op': 'policy', 'policy': 'summary_system'})[0], 400)
        for _ in range(assistant.MAX_SESSIONS):
            self.assertEqual(self.call('/sessions', {})[0], 201)
        self.assertEqual(self.call('/sessions', {})[0], 413)
        disabled = assistant.AssistantStore(runtime_enabled=False)
        self.addCleanup(disabled.close)
        self.assertEqual(assistant.mutate(disabled, '/api/assistant/agent/start', {})[0], 403)
        self.assertEqual(assistant.mutate(disabled, '/api/assistant/agent/connect', {'url': 'http://127.0.0.1:18080'})[0], 403)

    def test_no_plaintext_key_in_git_worktree(self):
        location = Path(self.temp.name) / 'repo'
        location.mkdir()
        (location / '.git').mkdir()
        state = assistant.AssistantStore(state_dir=location / 'private')
        self.addCleanup(state.close)
        code, result = assistant.mutate(state, '/api/assistant/provider', {'base_url': 'https://provider.invalid/v1', 'model': 'm', 'api_key': 'QAtools-secret'})
        self.assertEqual(code, 400)
        self.assertNotIn('QAtools-secret', json.dumps(result))
        self.assertFalse((location / 'private' / 'provider.json').exists())


@unittest.skipUnless(shutil.which('go'), 'local Go toolchain unavailable')
class ActualGoAssistantTest(unittest.TestCase):
    def test_actual_go_chat_memory_next_prompt_restart_and_no_background_completions(self):
        try:
            provider = LocalAPI()
        except PermissionError:
            self.skipTest('loopback sockets unavailable in this sandbox')
        self.addCleanup(provider.close)
        with tempfile.TemporaryDirectory(prefix='QAtools-real-go-') as directory:
            binary = os.environ.get('EDGELAB_TEST_BINARY')
            store = assistant.AssistantStore(state_dir=directory, binary=binary)
            self.addCleanup(store.close)
            def call(route, body):
                code, value = assistant.mutate(store, '/api/assistant' + route, body)
                self.assertLess(code, 300, value)
                return value
            call('/provider', {'base_url': provider.url + '/v1', 'model': 'QAtools-model', 'api_key': 'QAtools-secret-local-only'})
            call('/provider/test', {})
            started = call('/agent/start', {})
            job = wait_job(store, started['job']['id'], timeout=180)
            self.assertEqual(job['status'], 'completed', job)
            snapshot = store.snapshot()
            self.assertTrue(snapshot['memory']['available'], snapshot)
            self.assertEqual(snapshot['agent']['health']['clock'], 'real')
            self.assertEqual(snapshot['agent']['health']['wake'], 'gate')
            time.sleep(.25)
            self.assertFalse(any(c[1] == '/v1/chat/completions' for c in provider.calls))
            call('/memory', {'op': 'ADD', 'fact_id': 'ui-board', 'text': 'QAtools: моя плата Orange Pi, проверенный факт пользователя'})
            session = call('/sessions', {'title': 'QAtools actual Go'})['session']
            response = call('/chat', {'session_id': session['id'], 'text': 'Прочитай контекст, ничего не включай'})
            job = wait_job(store, response['job']['id'])
            self.assertEqual(job['status'], 'completed', job)
            messages = store.session(session['id'])['session']['messages']
            answer = messages[-1]
            self.assertEqual(answer['text'], 'QAtools ответ 1')
            self.assertTrue(answer['trace']['available'])
            prompt = answer['trace']['requests'][0]['context_snapshot']
            self.assertTrue(prompt['complete'])
            self.assertIn('Orange Pi', json.dumps(prompt['messages'], ensure_ascii=False))
            self.assertEqual(answer['reply']['input_id'], answer['input_id'])
            self.assertEqual(answer['reply']['tool_calls'], 0)
            self.assertEqual(store.snapshot()['memory']['turn_count'], 1)
            config = json.loads((Path(directory) / 'agent-config.json').read_text())
            self.assertEqual(config['llm']['retries'], 0)
            self.assertEqual(config['policy']['gate']['default'], 'ignore')
            self.assertEqual(config['mcp'], [])
            self.assertNotIn('QAtools-secret-local-only', json.dumps(config))
            call('/memory', {'op': 'UPDATE', 'fact_id': 'ui-board', 'text': 'QAtools: теперь указан Raspberry Pi'})
            response = call('/chat', {'session_id': session['id'], 'text': 'Что записано о плате?'})
            self.assertEqual(wait_job(store, response['job']['id'])['status'], 'completed')
            prompt = store.session(session['id'])['session']['messages'][-1]['trace']['requests'][0]['context_snapshot']
            self.assertIn('Raspberry Pi', json.dumps(prompt['messages'], ensure_ascii=False))
            self.assertEqual(sum(c[1] == '/v1/chat/completions' for c in provider.calls), 2)
            call('/agent/stop', {})
            from neurobot.ui import read_proxy
            from neurobot.ui.tests.test_ui_read_proxy import Registry
            class ObservedProxy(read_proxy.ReadProxy):
                def __init__(self, *args):
                    super().__init__(*args)
                    self.calls = []
                def dispatch(self, method, params):
                    self.calls.append((method, params))
                    return super().dispatch(method, params)
            registry = Registry()
            class CollectedBoards:
                doc = {'boards': []}
                def snapshot(self):
                    return self.doc
            passports = CollectedBoards()
            projection = ObservedProxy(registry, passports)
            self.addCleanup(projection.close)
            store.configure_mcp(projection.start())
            started = call('/agent/start', {})
            self.assertEqual(wait_job(store, started['job']['id'])['status'], 'completed')
            self.assertEqual(store.snapshot()['memory']['turn_count'], 2)
            self.assertEqual(store.snapshot()['memory']['facts'][0]['text'], 'QAtools: теперь указан Raspberry Pi')
            self.assertEqual(store.snapshot()['memory']['hardware']['devices'], 1)
            provider.tool_once = True
            response = call('/chat', {'session_id': session['id'], 'text': 'Прочитай наблюдаемое off, не включай оборудование'})
            self.assertEqual(wait_job(store, response['job']['id'])['status'], 'completed')
            latest = [c[2] for c in provider.calls if c[1] == '/v1/chat/completions'][-1]
            actual_tool = next(m for m in latest['messages'] if m['role'] == 'tool')
            self.assertIs(json.loads(actual_tool['content'])['value'], False)
            sent = json.dumps(latest['messages'], ensure_ascii=False)
            self.assertIn('zero=0', sent)
            self.assertIn('off=false', sent)
            tools_called = [params['name'] for method, params in projection.calls if method == 'tools/call']
            self.assertEqual(tools_called, ['read_property'])
            registry.doc['devices'][0]['properties'][0]['value'] = 42
            registry.doc['devices'][0]['properties'][1]['value'] = True
            passports.doc = {'boards': [{'id': 'QAtools-board', 'label': 'QAtools added passport', 'origin': 'explicit test fixture',
                                          'report': {'model': 'QAtools-new-board', 'collected_at': 101}}]}
            before = sum(c[1] == '/v1/chat/completions' for c in provider.calls)
            time.sleep(.15)
            self.assertEqual(sum(c[1] == '/v1/chat/completions' for c in provider.calls), before)
            response = call('/chat', {'session_id': session['id'], 'text': 'QAtools обнови фактический контекст'})
            self.assertEqual(wait_job(store, response['job']['id'])['status'], 'completed')
            last = [c[2] for c in provider.calls if c[1] == '/v1/chat/completions'][-1]
            current = json.dumps(last['messages'], ensure_ascii=False)
            self.assertIn('zero: 0→42', current)
            self.assertIn('off: false→true', current)
            self.assertIn('QAtools-new-board', current)
            self.assertEqual(store.snapshot()['memory']['hardware']['devices'], 2)
            call('/history', {'op': 'clear', 'confirm': True})
            self.assertEqual(store.snapshot()['memory']['turn_count'], 0)
            self.assertEqual(len(store.snapshot()['memory']['facts']), 1)
            call('/memory', {'op': 'DELETE', 'fact_id': 'ui-board'})
            self.assertEqual(store.snapshot()['memory']['facts'], [])
            provider.fail_chat = True
            response = call('/chat', {'session_id': session['id'], 'text': 'QAtools failure must not retry'})
            failed = wait_job(store, response['job']['id'])
            self.assertEqual(failed['status'], 'deferred', failed)
            self.assertEqual(store.snapshot()['agent']['status'], 'stopped')
            count = sum(c[1] == '/v1/chat/completions' for c in provider.calls)
            time.sleep(.3)
            self.assertEqual(sum(c[1] == '/v1/chat/completions' for c in provider.calls), count)
            self.assertEqual(count, 6)
            for path in Path(directory).glob('agent-*'):
                if path.is_file():
                    self.assertNotIn(b'QAtools-secret-local-only', path.read_bytes(), path.name)
            store.close()


if __name__ == '__main__':
    unittest.main()
