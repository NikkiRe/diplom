"""Supervised local EdgeCtx process. Configuration contains no provider secret."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


class RuntimeError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ProviderBridge:
    """A narrow credential boundary; it never originates a completion itself.

    The actual Go client sees the actual provider response with its configured
    bearer token removed if a server accidentally echoes it. It cannot follow
    redirects or turn into a general HTTP proxy. No raw request/response is logged.
    """
    def __init__(self, base_url, secret):
        bridge = self
        self.base_url, self.secret = base_url.rstrip('/'), secret
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, value):
                raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def forward(self):
                if self.headers.get('Origin') or self.headers.get('Host') != '127.0.0.1:' + str(bridge.server.server_port):
                    self.respond(403, {'error': {'message': 'local agent channel only'}})
                    return
                route = {('GET', '/v1/models'): '/models',
                         ('POST', '/v1/chat/completions'): '/chat/completions'}.get((self.command, self.path))
                if not route:
                    self.respond(404, {'error': {'message': 'unsupported provider operation'}})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    length = -1
                if length < 0 or length > 8 * 1024 * 1024:
                    self.respond(413, {'error': {'message': 'request limit'}})
                    return
                if self.command == 'POST' and self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                    self.respond(415, {'error': {'message': 'JSON required'}})
                    return
                body = self.rfile.read(length) if self.command == 'POST' else None
                headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'EdgeLab-credential-bridge/1'}
                if bridge.secret:
                    headers['Authorization'] = 'Bearer ' + bridge.secret
                request = urllib.request.Request(bridge.base_url + route, data=body, headers=headers, method=self.command)
                try:
                    try:
                        response = bridge._opener.open(request, timeout=65)
                    except urllib.error.HTTPError as exc:
                        response = exc
                    with response:
                        if 300 <= response.code < 400:
                            self.respond(502, {'error': {'message': 'Provider redirect refused'}})
                            return
                        raw = response.read(8 * 1024 * 1024 + 1)
                        if len(raw) > 8 * 1024 * 1024:
                            self.respond(502, {'error': {'message': 'Provider response limit'}})
                            return
                        value = json.loads(raw)
                        self.respond(response.code, bridge.redact(value))
                except Exception:
                    self.respond(502, {'error': {'message': 'Provider request failed; response unavailable'}})

            do_GET = do_POST = forward  # noqa: N815
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name='edgelab-provider-bridge', daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port) + '/v1'

    def redact(self, value):
        if isinstance(value, str):
            return value.replace(self.secret, '[REDACTED]') if self.secret else value
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, dict):
            return {k: self.redact(v) for k, v in value.items()}
        return value

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def private_write(path, raw):
    """Atomic owner-only file creation, never a permissive intermediate file."""
    import uuid
    path = Path(path)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(str(temporary), str(path))
        os.chmod(str(path), 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


class AgentRuntime:
    def __init__(self, state_dir, binary=None):
        self.state_dir = Path(state_dir)
        self.binary = str(binary) if binary else None
        self.process = None
        self.trace_path = None
        self.url = None
        self._reader = None
        self._ready = threading.Event()
        self._closed = False
        self.bridge = None

    def executable(self):
        if self.binary:
            candidate = Path(self.binary).expanduser().resolve()
            if not candidate.is_file() or not os.access(str(candidate), os.X_OK):
                raise RuntimeError('Исполняемый EdgeCtx не найден')
            return str(candidate)
        go = shutil.which('go')
        if not go:
            raise RuntimeError('Для сборки EdgeCtx нужен установленный Go')
        digest = hashlib.sha256()
        for location in sorted((ROOT / 'agent').rglob('*.go')) + [ROOT / 'agent' / 'go.mod', ROOT / 'agent' / 'go.sum']:
            digest.update(str(location.relative_to(ROOT)).encode())
            digest.update(location.read_bytes())
        cache = self.state_dir / 'bin'
        cache.mkdir(mode=0o700, exist_ok=True)
        target = cache / ('edgectx-' + digest.hexdigest()[:20])
        if not target.is_file():
            temporary = target.with_suffix('.building')
            try:
                result = subprocess.run([go, 'build', '-trimpath', '-o', str(temporary), './cmd/edgectx'],
                                        cwd=str(ROOT / 'agent'), stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=150, check=False)
                if result.returncode:
                    raise RuntimeError('Сборка EdgeCtx не удалась; проверьте локальный Go и зависимости')
                os.chmod(str(temporary), 0o700)
                os.replace(str(temporary), str(target))
            except subprocess.TimeoutExpired:
                raise RuntimeError('Превышено время локальной сборки EdgeCtx')
            finally:
                if temporary.exists():
                    temporary.unlink()
        return str(target)

    def start(self, provider, secret, readonly_mcp_url=None, policy=None):
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError('Управляемый EdgeCtx уже запущен')
        if self._closed:
            raise RuntimeError('Запуск EdgeCtx отменён')
        executable = self.executable()
        if self._closed:
            raise RuntimeError('Запуск EdgeCtx отменён')
        now = str(time.time_ns())
        self.trace_path = self.state_dir / ('agent-' + now + '.jsonl')
        private_write(self.trace_path, b'')
        self.bridge = ProviderBridge(provider['base_url'], secret)
        history = {'history': 'window', 'window_turns': 12, 'mask_keep_turns': 2}
        history.update(policy or {})
        config = {
            'clock': 'real', 'listen': '127.0.0.1:0', 'run_id': 'ui-' + now,
            'llm': {'base_url': self.bridge.url, 'model': provider['model'],
                    'api_key_env': '', 'retries': 0,
                    'timeout_s': 60, 'connect_timeout_s': 10, 'max_tokens': 2048},
            'policy': {'wake': 'gate', 'gate': {'default': 'ignore', 'rules_enabled': False},
                       'max_steps': 4, 'history': history['history'],
                       'window_turns': history['window_turns'], 'mask_keep_turns': history['mask_keep_turns'],
                       'history_batch_turns': 1, 'profile_consolidate_s': 0,
                       'local_tools': ['update_profile', 'recent_events']},
            'userctx': {'db': str(self.state_dir / 'agent-context.sqlite3'), 'reset': False,
                        'journal_mode': 'wal', 'synchronous': 'normal'},
            'trace': str(self.trace_path), 'trace_flush_s': 0,
            'trace_context': True, 'trace_context_max_bytes': 131072,
            'mcp': ([{'name': 'workspace', 'transport': 'http', 'url': readonly_mcp_url,
                      'inventory_uri': 'hw://inventory', 'subscribe': [], 'timeout_s': 10}]
                    if readonly_mcp_url else []),
        }
        location = self.state_dir / 'agent-config.json'
        private_write(location, json.dumps(config, ensure_ascii=False).encode())
        # The state directory is private before SQLite and its WAL files exist.
        environment = os.environ.copy()
        environment.pop('EDGELAB_ASSISTANT_API_KEY', None)
        self._ready.clear()
        self.url = None
        self.process = subprocess.Popen([executable, '-config', str(location)], cwd=str(self.state_dir),
                                        env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, start_new_session=True)
        # stop() can arrive while Popen is creating the child, before process is
        # assigned. Honor that cancellation before reporting a ready runtime.
        if self._closed:
            self.stop()
            raise RuntimeError('Запуск EdgeCtx отменён')
        self._reader = threading.Thread(target=self._read_startup, name='edgelab-assistant-stderr', daemon=True)
        self._reader.start()
        if not self._ready.wait(25) or not self.url or self.process.poll() is not None:
            self.stop()
            raise RuntimeError('EdgeCtx не открыл локальный канал; проверьте совместимость модели и конфигурацию')
        for path in self.state_dir.glob('agent-context.sqlite3*'):
            os.chmod(str(path), 0o600)
        return self.url

    def _read_startup(self):
        # Never retain or expose raw process output; a provider can echo secrets.
        try:
            for line in iter(self.process.stderr.readline, b''):
                match = re.search(rb'listening on http://(127\.0\.0\.1:\d+) ', line[:4096])
                if match:
                    self.url = 'http://' + match.group(1).decode('ascii')
                    self._ready.set()
        finally:
            self._ready.set()

    def stop(self):
        self._closed = True
        proc = self.process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)
        if proc is not None and proc.stderr is not None:
            proc.stderr.close()
        self.process = None
        self.url = None
        if self.bridge is not None:
            self.bridge.close()
            self.bridge = None

    def recorded_turn(self, input_id, wake_id=None):
        unavailable = {'available': False, 'reason': 'Локальная трасса этого ответа недоступна'}
        if self.trace_path is None or not self.trace_path.exists():
            return unavailable
        # Traces are bounded for this view; the original remains available to EdgeLab.
        if self.trace_path.stat().st_size > 32 * 1024 * 1024:
            return {'available': False, 'reason': 'Трасса больше 32 МиБ; откройте её отдельно в лаборатории'}
        rows = []
        for line in self.trace_path.read_bytes().splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
            except (ValueError, UnicodeError):
                continue
        wakes = {str(wake_id)} if wake_id else set()
        wakes.update(str(r.get('wake_id')) for r in rows
                     if input_id in (r.get('input_ids') or []) or r.get('input_id') == input_id)
        requests = [r for r in rows if r.get('type') == 'llm_request' and str(r.get('wake_id')) in wakes]
        request_ids = {r.get('req_id') for r in requests}
        responses = [r for r in rows if r.get('type') == 'llm_response' and r.get('req_id') in request_ids]
        return {'available': bool(requests), 'origin': 'managed EdgeCtx JSONL', 'input_id': input_id,
                'requests': requests[-4:], 'responses': responses[-4:],
                'reason': None if requests else 'В трассе нет отправленного запроса для этого input_id'}
