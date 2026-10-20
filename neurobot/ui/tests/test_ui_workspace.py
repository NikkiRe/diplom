"""Workspace HTTP boundaries and durable private state; no paid provider calls."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from neurobot.ui import gateway, live, server
from neurobot.ui.tests.test_ui_assistant import LocalAPI
from neurobot.ui.tests.test_ui_gateway import StubNode, write_code


class WorkspaceHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='QAtools-workspace-http-')
        self.addCleanup(self.temp.cleanup)
        self.srv = None
        self.worker = None
        self.start()
        self.addCleanup(self.stop)

    def start(self):
        try:
            self.srv = server.make_server(None, 0, state_dir=self.temp.name)
        except PermissionError:
            self.skipTest('loopback sockets unavailable in this sandbox')
        self.worker = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.worker.start()
        self.port = self.srv.server_port
        self.base = 'http://127.0.0.1:' + str(self.port)

    def stop(self):
        if self.srv is not None:
            self.srv.shutdown()
            self.srv.server_close()
            self.worker.join(timeout=2)
            self.srv = None

    def request(self, path, body=None, headers=None, method=None):
        header = {'Origin': self.base, 'Content-Type': 'application/json'}
        header.update(headers or {})
        header = {k: v for k, v in header.items() if v is not None}
        raw = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        connection.request(method or ('POST' if body is not None else 'GET'), path, body=raw, headers=header)
        response = connection.getresponse()
        output = response.read()
        result = response.status, output, dict(response.getheaders())
        connection.close()
        return result

    def test_origin_host_content_type_and_body_limits_precede_mutation(self):
        path = '/api/assistant/provider'
        body = {'base_url': 'https://provider.invalid/v1', 'model': 'QAtools-model', 'api_key': 'QAtools-private'}
        for headers, expected in [({'Origin': 'https://evil.invalid'}, 403), ({'Origin': None}, 403),
                                  ({'Host': 'evil.invalid', 'Origin': 'http://evil.invalid'}, 403),
                                  ({'Content-Type': 'text/plain'}, 415),
                                  ({'Content-Length': str(128 * 1024 + 1)}, 413)]:
            status, _, _ = self.request(path, body, headers)
            self.assertEqual(status, expected)
        self.assertFalse((Path(self.temp.name) / 'assistant' / 'provider.json').exists())
        self.assertEqual(self.request(path, b'{broken')[0], 400)
        self.assertEqual(self.request(path, [body])[0], 400)
        self.assertEqual(self.request('/api/assistant/missing', {})[0], 404)
        self.assertEqual(self.request('/api/assistant', headers={'Host': 'evil.invalid'})[0], 403)
        self.assertEqual(self.request('/api/assistant', headers={'Origin': 'https://evil.invalid'})[0], 403)

    def test_private_provider_and_chat_export_survive_restart_without_secret(self):
        key = 'QAtools-HTTP-PRIVATE-KEY'
        self.assertEqual(self.request('/api/assistant/provider', {'base_url': 'https://provider.invalid/v1', 'model': 'QAtools-model', 'api_key': key})[0], 200)
        status, raw, _ = self.request('/api/assistant/sessions', {'title': 'QAtools session ' + key})
        self.assertEqual(status, 201)
        identity = json.loads(raw)['session']['id']
        for path in ['/api/assistant', '/api/assistant/sessions/' + identity, '/api/assistant/export/' + identity]:
            status, raw, headers = self.request(path)
            self.assertEqual(status, 200)
            self.assertNotIn(key.encode(), raw)
            self.assertEqual(headers.get('Cache-Control'), 'no-store')
        self.assertEqual(self.request('/api/assistant/export/%2e%2e%2fprovider.json')[0], 404)
        self.stop()
        self.start()
        status, raw, _ = self.request('/api/assistant')
        snapshot = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(snapshot['provider']['has_key'])
        self.assertTrue(snapshot['state']['durable'])
        self.assertEqual(snapshot['sessions'][0]['id'], identity)
        self.assertNotIn(key.encode(), raw)
        self.assertEqual(self.request('/api/assistant/export/' + identity)[0], 200)

    def test_models_check_is_explicit_harmless_and_no_runtime_on_setup(self):
        fixture = LocalAPI()
        self.addCleanup(fixture.close)
        body = {'base_url': fixture.url + '/v1', 'model': 'QAtools-model', 'api_key': 'QAtools-fixture-key'}
        self.assertEqual(self.request('/api/assistant/provider', body)[0], 200)
        self.assertEqual(fixture.calls, [])
        status, raw, _ = self.request('/api/assistant/provider/test', {})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)['completion_requested'])
        self.assertEqual([(r[0], r[1]) for r in fixture.calls], [('GET', '/v1/models')])
        identity = json.loads(self.request('/api/assistant/sessions', {})[1])['session']['id']
        self.assertEqual(self.request('/api/assistant/chat', {'session_id': identity, 'text': 'QAtools no attached agent'})[0], 409)
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(self.request('/api/boards/probe.py')[0], 200)
        self.assertEqual(self.request('/api/boards/%2e%2e%2fprovider.json')[0], 404)


class GatewayWorkspaceAuthTest(unittest.TestCase):
    def test_workspace_requires_session_csrf_origin_and_runtime_stays_disabled(self):
        with tempfile.TemporaryDirectory(prefix='QAtools-gateway-workspace-') as directory:
            auth = gateway.Auth(directory)
            hub = live.Hub()
            observed = live.Live(hub)
            node = StubNode(observed)
            app = gateway.Gateway(hub, observed, node, auth, 'двойник', 'QAtools explicit fake gateway')
            try:
                srv = gateway.make_server(app, 0)
            except PermissionError:
                app.assistant.close()
                app.catalog.close()
                app.lab.close()
                self.skipTest('loopback sockets unavailable in this sandbox')
            worker = threading.Thread(target=srv.serve_forever, daemon=True)
            worker.start()
            port = srv.server_port
            base = 'http://127.0.0.1:' + str(port)
            def request(path, body=None, headers=None):
                current = {'Origin': base, 'Content-Type': 'application/json'}
                current.update(headers or {})
                current = {k: v for k, v in current.items() if v is not None}
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('POST' if body is not None else 'GET', path,
                                   json.dumps(body) if body is not None else None, current)
                response = connection.getresponse()
                value = response.status, response.read(), dict(response.getheaders())
                connection.close()
                return value
            try:
                self.assertEqual(request('/api/assistant')[0], 401)
                self.assertEqual(request('/api/boards')[0], 401)
                write_code(directory, 'QAtools-login')
                status, _, headers = request('/login?code=QAtools-login')
                self.assertEqual(status, 303)
                cookie = headers['Set-Cookie'].split(';', 1)[0]
                _, raw, _ = request('/api/meta', headers={'Cookie': cookie})
                csrf = json.loads(raw)['csrf']
                signed = {'Cookie': cookie, gateway.CSRF_HEADER: csrf}
                self.assertEqual(request('/api/assistant/sessions', {}, {'Cookie': cookie})[0], 403)
                self.assertEqual(request('/api/assistant/sessions', {}, {**signed, 'Origin': 'https://evil.invalid'})[0], 403)
                self.assertEqual(request('/api/assistant/sessions', {}, signed)[0], 201)
                self.assertEqual(request('/api/assistant/agent/start', {}, signed)[0], 403)
                self.assertEqual(request('/api/assistant/agent/connect', {'url': 'http://127.0.0.1:18080'}, signed)[0], 403)
                snapshot = json.loads(request('/api/assistant', headers={'Cookie': cookie})[1])
                self.assertFalse(snapshot['state']['runtime_enabled'])
                self.assertEqual(node.sent, [])
            finally:
                srv.shutdown()
                srv.server_close()
                worker.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
