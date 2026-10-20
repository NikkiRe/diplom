"""Durable UI journal and a supervised, actual EdgeCtx assistant connection.

The agent's SQLite context is authoritative. This module never simulates facts,
answers or sent prompts. Setup uses health/context/models GETs, never completions.
Only explicit /chat requests can reach the model. There are no automatic retries.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from neurobot.ui.assistant_runtime import AgentRuntime, RuntimeError, private_write

MAX_TEXT = 16000
MAX_SESSIONS = 64
MAX_MESSAGES = 2000
MAX_JOURNAL_BYTES = 16 * 1024 * 1024


class AssistantError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint(value, loopback=False):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AssistantError('Нужен корректный URL')
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise AssistantError('Некорректный URL или порт')
    if parsed.scheme not in ('http', 'https') or not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AssistantError('URL должен использовать HTTP(S), без логина, пароля и параметров')
    try:
        local = host.lower() == 'localhost' or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host.lower() == 'localhost'
    if loopback and not local:
        raise AssistantError('Агент подключается только через loopback URL или локальный SSH-туннель')
    if not loopback and parsed.scheme == 'http' and not local:
        raise AssistantError('Для внешнего провайдера нужен HTTPS; HTTP разрешён для локального сервера')
    if port == 0 or (port is not None and not 1 <= port <= 65535):
        raise AssistantError('Некорректный порт')
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', ''))


def _plain(value):
    return isinstance(value, dict)


class AssistantStore:
    def __init__(self, state_dir=None, binary=None, python=None, readonly_mcp_url=None, runtime_enabled=True):
        self._temporary = tempfile.TemporaryDirectory(prefix='edgelab-assistant-') if state_dir is None else None
        self.state_dir = Path(self._temporary.name if self._temporary else state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.state_dir.stat()
        if hasattr(os, 'getuid') and info.st_uid != os.getuid():
            raise AssistantError('Каталог состояния должен принадлежать текущему пользователю')
        os.chmod(str(self.state_dir), 0o700)
        self.runtime_enabled = bool(runtime_enabled)
        self.python = python  # accepted for the common UI runtime constructor contract
        self._lock = threading.RLock()
        self._refreshing = False
        self._refreshed_at = 0
        self._closed = False
        self._threads = []
        self._runtime = AgentRuntime(self.state_dir, binary=binary)
        self.readonly_mcp_url = endpoint(readonly_mcp_url, loopback=True) if readonly_mcp_url else None
        self.trace_callback = None
        self._trace_watch = None
        self._provider = {'base_url': '', 'model': ''}
        self._secret = ''
        self._policy = {'history': 'window', 'window_turns': 12, 'mask_keep_turns': 2}
        self._provider_error = None
        location = self.state_dir / 'provider.json'
        if location.exists():
            try:
                data = json.loads(location.read_text(encoding='utf-8'))
                self._provider = {'base_url': endpoint(data['base_url']), 'model': str(data['model'])[:300]}
                self._secret = str(data.get('api_key', ''))
                self._policy.update(data.get('history_policy') or {})
                os.chmod(str(location), 0o600)
            except Exception:
                self._provider_error = 'Сохранённые настройки провайдера не прочитаны; исходный файл не изменён'
        self._agent = {'mode': 'disconnected' if self.runtime_enabled else 'disabled', 'status': 'stopped',
                       'url': None, 'managed': False, 'health': None, 'error': None}
        self._memory = self._empty_memory()
        db_path = self.state_dir / 'chat.sqlite3'
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        self._db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,title TEXT NOT NULL,created REAL,updated REAL);
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,role TEXT,text TEXT,
                status TEXT,created REAL,input_id TEXT,reply TEXT,trace TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,session_id TEXT,created REAL,
                finished REAL,error TEXT,result TEXT);
        ''')
        self._db.execute("UPDATE jobs SET status='interrupted',error='Процесс интерфейса завершился; запрос автоматически не повторялся' WHERE status IN ('queued','running')")
        self._db.execute("UPDATE messages SET status='interrupted',error='Ответ неизвестен после перезапуска; автоматического повтора нет' WHERE status='pending'")
        self._db.commit()
        for path in self.state_dir.glob('chat.sqlite3*'):
            os.chmod(str(path), 0o600)

    @staticmethod
    def _empty_memory(error='Подключите или запустите EdgeCtx для чтения его контекста'):
        return {'available': False, 'facts': [], 'summary': '', 'turn_count': 0,
                'history_policy': None, 'visible_history': [], 'turns': [], 'error': error}

    def _redact(self, value):
        secret = self._secret
        if isinstance(value, str):
            return value.replace(secret, '[REDACTED]') if secret else value
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()
                    if str(k).lower() not in ('api_key', 'authorization', 'api-key', 'apikey')}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        return value

    def _request(self, url, body=None, key=None, timeout=3, limit=4 * 1024 * 1024):
        headers = {'Accept': 'application/json', 'User-Agent': 'EdgeLab-local-assistant/1'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if key:
            headers['Authorization'] = 'Bearer ' + key
        raw = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
        request = urllib.request.Request(url, data=raw, headers=headers, method='POST' if raw is not None else 'GET')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        except Exception as exc:
            # Never include request headers or unbounded remote response bodies.
            raise AssistantError(self._redact('Соединение не установлено: ' + str(exc)[:400]), 502)
        with response:
            payload = response.read(limit + 1)
            if len(payload) > limit:
                raise AssistantError('Ответ сервера превышает допустимый размер', 502)
            try:
                data = json.loads(payload) if payload else {}
            except (ValueError, UnicodeError):
                raise AssistantError('Сервер вернул некорректный JSON', 502)
            if not 200 <= response.code < 300:
                detail = str(data.get('error', ''))[:400] if _plain(data) else ''
                raise AssistantError(self._redact('HTTP %d%s' % (response.code, ': ' + detail if detail else '')),
                                     response.code if response.code in (400, 404, 409) else 502)
            return response.code, self._redact(data)

    def configure_mcp(self, url):
        with self._lock:
            self.readonly_mcp_url = endpoint(url, loopback=True) if url else None

    @property
    def trace_path(self):
        return self._runtime.trace_path

    def _active(self):
        return self._db.execute("SELECT id FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone()

    def _require_idle_job(self):
        if self._active():
            raise AssistantError('Дождитесь текущего запроса или остановите управляемый агент', 409)

    def _refresh(self, force=False):
        with self._lock:
            url = self._agent['url']
            if self._closed or not url or self._refreshing or (not force and time.monotonic() - self._refreshed_at < 1):
                return
            self._refreshing = True
        try:
            _, health = self._request(url + '/v1/health', timeout=2)
            if not _plain(health) or health.get('ok') is not True or not isinstance(health.get('run_id'), str):
                raise AssistantError('По этому URL нет готового EdgeCtx', 502)
            try:
                _, context = self._request(url + '/v1/context', timeout=3)
                if not isinstance(context, dict):
                    raise AssistantError('Некорректный ответ контекста EdgeCtx', 502)
                memory = {**context, 'available': True, 'facts': context.get('facts') or [], 'summary': context.get('summary') or '',
                          'turn_count': context.get('history_turns', 0), 'history_policy': context.get('policy'),
                          'visible_history': context.get('visible_history') or context.get('history') or [],
                          'turns': context.get('turns') or [], 'profile_text': context.get('profile_text') or '',
                          'hardware': context.get('hardware'), 'error': None}
            except AssistantError as exc:
                memory = self._empty_memory('Контекст этого агента недоступен: ' + str(exc))
            with self._lock:
                if self._agent['url'] == url:
                    self._agent.update(status='ready', health=health, error=None)
                    self._memory = memory
        except AssistantError as exc:
            with self._lock:
                if self._agent['url'] == url:
                    self._agent.update(status='error', error=str(exc))
                    self._memory = self._empty_memory('Агент не ответил; сохранённые данные не заменены измерениями')
        finally:
            with self._lock:
                self._refreshing = False
                self._refreshed_at = time.monotonic()

    def _sessions(self):
        rows = self._db.execute('SELECT s.*,COUNT(m.id) AS message_count FROM sessions s LEFT JOIN messages m '
                                'ON m.session_id=s.id GROUP BY s.id ORDER BY s.updated DESC').fetchall()
        return [{'id': r['id'], 'title': r['title'], 'created_at': r['created'], 'updated_at': r['updated'],
                 'message_count': r['message_count']} for r in rows]

    def _jobs(self):
        return [{'id': r['id'], 'kind': r['kind'], 'status': r['status'], 'session_id': r['session_id'],
                 'created_at': r['created'], 'finished_at': r['finished'], 'error': r['error'],
                 'result': json.loads(r['result']) if r['result'] else None}
                for r in self._db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 30')]

    def snapshot(self):
        self._refresh()
        with self._lock:
            if self._closed:
                raise AssistantError('Ассистент остановлен', 503)
            agent = copy.deepcopy(self._agent)
            if self._trace_watch:
                agent['trace'] = copy.deepcopy(self._trace_watch)
            result = {'provider': {**self._provider, 'has_key': bool(self._secret),
                                   'key_masked': '••••••••' if self._secret else '', 'error': self._provider_error,
                                   'applies_to': 'managed_agent'},
                      'agent': agent, 'state': {'durable': self._temporary is None,
                                               'runtime_enabled': self.runtime_enabled,
                                               'credentials_saved': bool(self._secret),
                                               'context_scope': 'agent_shared', 'closed': self._closed},
                      'sessions': self._sessions(), 'memory': copy.deepcopy(self._memory),
                      'jobs': self._jobs(), 'history_policy': copy.deepcopy(self._policy),
                      'limits': {'sessions': MAX_SESSIONS, 'messages': MAX_MESSAGES, 'text': MAX_TEXT,
                                 'journal_bytes': MAX_JOURNAL_BYTES}}
            return self._redact(result)

    def session(self, session_id):
        with self._lock:
            if self._closed:
                raise AssistantError('Ассистент остановлен', 503)
            found = next((s for s in self._sessions() if s['id'] == session_id), None)
            if found is None:
                raise AssistantError('Диалог не найден', 404)
            found['context_scope'] = 'agent_shared'
            found['messages'] = [{'id': r['id'], 'role': r['role'], 'text': r['text'], 'status': r['status'],
                                  'created_at': r['created'], 'input_id': r['input_id'],
                                  'reply': json.loads(r['reply']) if r['reply'] else None,
                                  'trace': json.loads(r['trace']) if r['trace'] else None, 'error': r['error']}
                                 for r in self._db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY created,rowid', (session_id,))]
            return {'session': self._redact(found)}

    def export(self, session_id):
        return {'format': 'edgelab-chat/1', 'exported_at': time.time(), **self.session(session_id)}

    def _save_provider(self):
        if self._secret and any((parent / '.git').exists() for parent in [self.state_dir, *self.state_dir.parents]):
            raise AssistantError('Ключ нельзя сохранять внутри Git-репозитория; задайте отдельный приватный каталог состояния')
        private_write(self.state_dir / 'provider.json', json.dumps({**self._provider, 'api_key': self._secret,
                                                                  'history_policy': self._policy}, ensure_ascii=False).encode())

    def provider(self, body):
        allowed = {'base_url', 'model', 'api_key'}
        if set(body) - allowed:
            raise AssistantError('Неизвестное поле настройки провайдера')
        base = endpoint(body.get('base_url', self._provider['base_url']))
        model = body.get('model', self._provider['model'])
        if not isinstance(model, str) or not model.strip() or len(model) > 300 or any(ord(c) < 32 for c in model):
            raise AssistantError('Укажите идентификатор модели (до 300 символов)')
        key = body.get('api_key', self._secret)
        if not isinstance(key, str) or len(key) > 8192 or any(ord(c) <= 32 or ord(c) >= 127 for c in key):
            raise AssistantError('Некорректный API-ключ')
        with self._lock:
            self._require_idle_job()
            old = self._provider, self._secret
            self._provider, self._secret = {'base_url': base, 'model': model.strip()}, key
            try:
                self._save_provider()
            except Exception:
                self._provider, self._secret = old
                raise
            self._provider_error = None
        if self._agent['managed']:
            self.stop()
        return {'ok': True, 'provider': self.snapshot()['provider'], 'restart_required': False}

    def test_provider(self):
        if not self._provider['base_url']:
            raise AssistantError('Сначала сохраните URL и модель')
        _, data = self._request(self._provider['base_url'] + '/models', key=self._secret, timeout=8, limit=512 * 1024)
        models = [str(item['id'])[:300] for item in (data.get('data') or [])[:200]
                  if isinstance(item, dict) and isinstance(item.get('id'), str)] if isinstance(data, dict) else []
        return {'ok': True, 'models': self._redact(models), 'method': 'GET models', 'completion_requested': False}

    def create_session(self, body):
        if set(body) - {'title'}:
            raise AssistantError('Диалог принимает только title')
        title = body.get('title') or 'Новый диалог'
        if not isinstance(title, str) or len(title) > 160:
            raise AssistantError('Название диалога: до 160 символов')
        with self._lock:
            if self._db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] >= MAX_SESSIONS:
                raise AssistantError('Достигнут предел диалогов; экспортируйте и удалите ненужные', 413)
            now, identity = time.time(), 'session-' + uuid.uuid4().hex[:20]
            self._db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (identity, self._redact(title.strip()), now, now))
            self._db.commit()
            return self.session(identity)

    def delete_session(self, body):
        identity = body.get('session_id')
        with self._lock:
            self._require_idle_job()
            self.session(identity)
            self._db.execute('DELETE FROM messages WHERE session_id=?', (identity,))
            self._db.execute('DELETE FROM sessions WHERE id=?', (identity,))
            self._db.commit()
        return {'ok': True, 'agent_history_changed': False}

    def connect(self, body):
        if not self.runtime_enabled:
            raise AssistantError('Подключение агента отключено в этом режиме интерфейса', 403)
        if set(body) != {'url'}:
            raise AssistantError('Для подключения передайте только url')
        url = endpoint(body['url'], loopback=True)
        if urllib.parse.urlsplit(url).path:
            raise AssistantError('Нужен базовый URL HTTP-канала EdgeCtx без пути')
        with self._lock:
            self._require_idle_job()
        _, health = self._request(url + '/v1/health')
        if not isinstance(health, dict) or health.get('ok') is not True or not isinstance(health.get('run_id'), str):
            raise AssistantError('По этому URL нет готового EdgeCtx', 502)
        self._runtime.stop()
        with self._lock:
            self._agent = {'mode': 'external', 'status': 'ready', 'url': url, 'managed': False,
                           'health': health, 'error': None}
            self._trace_watch = None
        self._refresh(force=True)
        return {'ok': True, 'agent': self.snapshot()['agent']}

    def _new_job(self, kind, session_id=None):
        identity = 'job-' + uuid.uuid4().hex[:20]
        self._db.execute('INSERT INTO jobs(id,kind,status,session_id,created) VALUES(?,?,?,?,?)',
                         (identity, kind, 'running', session_id, time.time()))
        self._db.execute("DELETE FROM jobs WHERE id IN (SELECT id FROM jobs WHERE status NOT IN ('queued','running') ORDER BY created DESC LIMIT -1 OFFSET 50)")
        self._db.commit()
        return identity

    def _finish_job(self, job_id, status, error=None, result=None):
        with self._lock:
            if self._closed:
                return
            self._db.execute('UPDATE jobs SET status=?,finished=?,error=?,result=? WHERE id=?',
                             (status, time.time(), self._redact(error), json.dumps(self._redact(result)) if result else None, job_id))
            self._db.commit()

    def _spawn(self, name, action):
        thread = threading.Thread(target=action, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def start(self, body=None):
        if body:
            raise AssistantError('Запуск использует только сохранённую конфигурацию')
        if not self.runtime_enabled:
            raise AssistantError('Локальный запуск отключён в этом режиме интерфейса', 403)
        with self._lock:
            self._require_idle_job()
            if not self._provider['base_url'] or not self._provider['model']:
                raise AssistantError('Сначала сохраните настройки провайдера и модели')
            if self._agent['managed'] and self._agent['status'] == 'ready':
                raise AssistantError('Управляемый агент уже запущен', 409)
            job = self._new_job('agent_start')
            self._agent.update(mode='managed', status='starting', managed=True, url=None, error=None, health=None)
            self._memory = self._empty_memory('Локальный EdgeCtx запускается')
            self._trace_watch = None
            self._runtime._closed = False
            provider, secret, policy = dict(self._provider), self._secret, dict(self._policy)
        def launch():
            try:
                url = self._runtime.start(provider, secret, self.readonly_mcp_url, policy)
                with self._lock:
                    if self._closed:
                        self._runtime.stop()
                        return
                    self._agent.update(mode='managed', status='ready', managed=True, url=url, error=None)
                    if self.trace_callback:
                        try:
                            self._trace_watch = self.trace_callback(self._runtime.trace_path, 'EdgeCtx · локальный диалог')
                        except Exception:
                            self._trace_watch = {'error': 'Трасса записана, но не подключена к лаборатории'}
                self._refresh(force=True)
                self._finish_job(job, 'completed', result={'agent': 'ready'})
            except Exception as exc:
                self._runtime.stop()
                error = self._redact(str(exc)[:600])
                with self._lock:
                    if not self._closed:
                        self._agent.update(status='error', error=error, url=None)
                self._finish_job(job, 'error', error)
        self._spawn('edgelab-assistant-start', launch)
        return {'job': {'id': job, 'kind': 'agent_start', 'status': 'running'}}

    def stop(self, body=None):
        if body:
            raise AssistantError('Остановка не принимает дополнительных полей')
        self._runtime.stop()
        with self._lock:
            self._agent = {'mode': 'disconnected' if self.runtime_enabled else 'disabled', 'status': 'stopped',
                           'url': None, 'managed': False, 'health': None, 'error': None}
            self._memory = self._empty_memory()
            self._refreshed_at = 0
        return {'ok': True, 'external_agent_stopped': False}

    def chat(self, body):
        if set(body) != {'session_id', 'text'}:
            raise AssistantError('Чат принимает session_id и text')
        text = body['text']
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise AssistantError('Сообщение: от 1 до %d символов' % MAX_TEXT)
        self._refresh(force=True)
        with self._lock:
            self._require_idle_job()
            session_id = body['session_id']
            self.session(session_id)
            if self._agent['status'] != 'ready' or not self._agent['url']:
                raise AssistantError('Сначала подключите или запустите готовый EdgeCtx', 409)
            if self._agent['health'].get('idle') is False:
                raise AssistantError('EdgeCtx выполняет другой ход; дождитесь его окончания', 409)
            rows, size = self._db.execute('SELECT COUNT(*),COALESCE(SUM(LENGTH(text)+LENGTH(COALESCE(reply,\'\'))+LENGTH(COALESCE(trace,\'\'))),0) FROM messages').fetchone()
            if rows + 2 > MAX_MESSAGES or size + len(text.encode()) > MAX_JOURNAL_BYTES:
                raise AssistantError('Архив диалогов заполнен; экспортируйте и удалите старые сессии', 413)
            input_id = 'ui-' + uuid.uuid4().hex
            user_id, answer_id = 'message-' + uuid.uuid4().hex, 'message-' + uuid.uuid4().hex
            now = time.time()
            self._db.execute('INSERT INTO messages(id,session_id,role,text,status,created,input_id) VALUES(?,?,?,?,?,?,?)',
                             (user_id, session_id, 'user', self._redact(text.strip()), 'sent', now, input_id))
            self._db.execute('INSERT INTO messages(id,session_id,role,text,status,created,input_id) VALUES(?,?,?,?,?,?,?)',
                             (answer_id, session_id, 'assistant', '', 'pending', now + .000001, input_id))
            self._db.execute('UPDATE sessions SET updated=? WHERE id=?', (now, session_id))
            job = self._new_job('chat', session_id)
            url, managed = self._agent['url'], self._agent['managed']
        def send():
            try:
                if managed:
                    self._request(url + '/v1/context/hardware/refresh', {}, timeout=25)
                status, reply = self._request(url + '/v1/user', {'id': input_id, 'text': self._redact(text.strip())}, timeout=270)
                if not isinstance(reply, dict):
                    raise AssistantError('EdgeCtx вернул ответ неизвестного формата', 502)
                deferred = status == 202 or bool(reply.get('deferred'))
                trace = self._runtime.recorded_turn(input_id, reply.get('wake_id')) if managed else {
                    'available': False, 'reason': 'Внешний агент не предоставляет локальную запись отправленного промпта'}
                error = reply.get('error')
                answer_status = 'deferred' if deferred else 'completed' if reply.get('ok') else 'error'
                if deferred:
                    error = 'Агент отложил ход. Управляемый процесс остановлен, автоматического повтора нет' if managed else 'Внешний агент отложил ход; дальнейшие попытки определяет его собственная конфигурация'
                    if managed:
                        self.stop()
                with self._lock:
                    if not self._closed:
                        self._db.execute('UPDATE messages SET text=?,status=?,reply=?,trace=?,error=? WHERE id=?',
                                         (self._redact(str(reply.get('text') or '')), answer_status,
                                          json.dumps(self._redact(reply)), json.dumps(self._redact(trace)), self._redact(error), answer_id))
                        self._db.commit()
                self._finish_job(job, answer_status, error, {'input_id': input_id})
                self._refresh(force=True)
            except Exception as exc:
                error = self._redact(str(exc)[:600]) + '. Ответ может быть неизвестен; запрос автоматически не повторялся'
                if managed:
                    self.stop()
                with self._lock:
                    if not self._closed:
                        self._db.execute("UPDATE messages SET status='error',error=? WHERE id=?", (error, answer_id))
                        self._db.commit()
                self._finish_job(job, 'error', error)
        self._spawn('edgelab-assistant-chat', send)
        return {'job': {'id': job, 'kind': 'chat', 'status': 'running', 'session_id': session_id},
                **self.session(session_id)}

    def memory(self, body):
        if set(body) - {'op', 'fact_id', 'text'}:
            raise AssistantError('Неизвестное поле операции памяти')
        operation = str(body.get('op') or '').lower()
        if operation not in ('add', 'update', 'delete'):
            raise AssistantError('Операция памяти: ADD, UPDATE или DELETE')
        fact_id = body.get('fact_id') or ''
        if not isinstance(fact_id, str) or len(fact_id.encode('utf-8')) > 128:
            raise AssistantError('Некорректный идентификатор факта')
        text = body.get('text') or ''
        if operation != 'delete' and (not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 4096):
            raise AssistantError('Факт пуст или слишком длинный; сократите текст')
        self._refresh(force=True)
        with self._lock:
            self._require_idle_job()
            url = self._agent['url']
            if not url or self._agent['status'] != 'ready':
                raise AssistantError('Память редактируется в подключённом готовом EdgeCtx', 409)
        _, result = self._request(url + '/v1/context/facts', {'operation': operation, 'id': fact_id,
                                                           'text': self._redact(text)}, timeout=5)
        self._refresh(force=True)
        return {'ok': True, 'result': result, 'memory': self.snapshot()['memory']}

    def history(self, body):
        operation = body.get('op')
        if operation == 'clear':
            if body.get('confirm') is not True:
                raise AssistantError('Очистка истории требует confirm:true')
            self._refresh(force=True)
            with self._lock:
                self._require_idle_job()
                url = self._agent['url']
                if not url:
                    raise AssistantError('Подключите EdgeCtx для очистки его истории', 409)
            _, result = self._request(url + '/v1/context/history/clear', {'confirm': True}, timeout=5)
            self._refresh(force=True)
            return {'ok': True, 'result': result, 'memory': self.snapshot()['memory']}
        if operation == 'policy':
            if body.get('policy') not in ('full', 'window', 'mask'):
                raise AssistantError('Политика истории: full, window или mask')
            window, keep = body.get('window_turns', 12), body.get('mask_keep_turns', 2)
            if not isinstance(window, int) or isinstance(window, bool) or not 1 <= window <= 100 or not isinstance(keep, int) or isinstance(keep, bool) or not 0 <= keep <= 100:
                raise AssistantError('Окно истории: 1–100 ходов, маска: 0–100')
            with self._lock:
                self._require_idle_job()
                if self._agent['mode'] == 'external':
                    raise AssistantError('Политика внешнего агента задаётся в его конфигурации', 409)
                self._policy = {'history': body['policy'], 'window_turns': window, 'mask_keep_turns': keep}
                self._save_provider()
            if self._agent['managed']:
                self.stop()
            return {'ok': True, 'history_policy': dict(self._policy), 'restart_required': True}
        raise AssistantError('Операция истории: clear или policy')

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._runtime.stop()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=3)
        with self._lock:
            self._db.close()
        if self._temporary:
            self._temporary.cleanup()


def mutate(store, path, body):
    try:
        if not isinstance(body, dict):
            raise AssistantError('Ожидается JSON-объект')
        if store._closed:
            raise AssistantError('Ассистент остановлен', 503)
        route = path.removeprefix('/api/assistant') if hasattr(str, 'removeprefix') else path[len('/api/assistant'):]
        handlers = {'/provider': store.provider, '/provider/test': lambda unused: store.test_provider(),
                    '/sessions': store.create_session, '/sessions/delete': store.delete_session,
                    '/chat': store.chat, '/memory': store.memory, '/history': store.history,
                    '/agent/connect': store.connect, '/agent/start': store.start, '/agent/stop': store.stop}
        if route not in handlers:
            raise AssistantError('Неизвестный маршрут ассистента', 404)
        result = handlers[route](body)
        return (202 if route in ('/chat', '/agent/start') else 201 if route == '/sessions' else 200), result
    except AssistantError as exc:
        return exc.status, {'error': store._redact(str(exc))}
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        return 500, {'error': store._redact('Операция не выполнена: ' + str(exc)[:500])}
