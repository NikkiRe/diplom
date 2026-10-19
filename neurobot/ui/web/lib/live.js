// Живой источник пульта: поток neurobot-ui/1 по SSE (/api/stream) — одно соединение на браузер. Вкладки делят его через
// BroadcastChannel, ведущую выбирает Web Locks: иначе каждая вкладка держала бы своё соединение, и при лимите HTTP/1.1
// в 6 соединений на хост POST «СТОП» ждал бы в очереди (PLAN §0а п. 6; MDN EventSource). Ведущая открывает
// EventSource и пересылает сообщения остальным с номером; новая вкладка просит у ведущей накопленное и досыпает хвост
// по номерам. Закрылась ведущая — замок переходит к другой вкладке, та открывает свой поток: новый hello — заново со
// снимка шлюза. Время робота — по hb (раз в 0,5 с): T = часы браузера + сдвиг; связь — возраст последнего сообщения.
// Свой EventSource после CLOSED (Chrome закрывает его на «connection refused» — туннель упал) и поток новой ведущей
// продолжают с номера последнего события (?last=, как Last-Event-ID): иначе каждый обрыв туннеля — снимок и буфер
// заново, а история браузера — с начала буфера шлюза.
// Хранится не всё подряд: у каждого вида свой срок (KEEP) — пульт с открытой вкладкой живёт днями.
import { Timeline } from './timeline.js';

// срок хранения в браузере, с: подробные виды — час (окна экранов — 10 и 15 мин, тренды — 1 ч), агрегаты — 8 ч;
// тревоги и жесты — час плюс то, что ещё стоит или не закрыто (keepOpen); последнее значение вида не выбрасывается
const KEEP = {
  safety: 3600, cmd: 3600, health: 3600, units: 3600, operator: 3600, k1: 3600, alarm: 3600, gesture: 3600,
  stats: 8 * 3600, mem: 8 * 3600,
};
const TRIM_EVERY_MS = 60000;
const LOCK = 'nb-ui-stream-leader';
const CHANNEL = 'nb-ui-stream';
const SYNC_WAIT_MS = 1500;
const REOPEN_MS = 3000;                     // шлюз ответил не 200: EventSource сам не повторит
const OFFSET_SAMPLES = 10;                  // сдвиг часов — максимум по последним hb: задержка только уменьшает его
export const COMMAND_TIMEOUT_MS = 2000;     // СТОП и защёлка: нет ответа за столько — «не дошёл», а не «отправляю…»

let clockSource = null;                     // живой источник вкладки: его часы робота точнее
let metaOffset = null;                      // часы робота − часы браузера по /api/meta (пока живого потока нет)

// часы робота: по hb живого потока, иначе по /api/meta (режим «Запись» при живом шлюзе)
export function robotNow() {
  if (clockSource && clockSource.hello) return clockSource.now();
  return metaOffset == null ? null : Date.now() / 1000 + metaOffset;
}

export function setMetaClock(t, fetchedAt) {
  metaOffset = t - fetchedAt / 1000;
}

export class LiveSource {
  constructor({ url = 'api/stream', onChange = null } = {}) {
    this.url = url;
    this.tl = new Timeline();
    this.hello = null;
    this.offset = 0;                        // часы робота − часы браузера, с
    this.offsets = [];
    this.lastAt = null;                     // performance.now() последнего сообщения
    this.n = 0;                             // номер сообщения у ведущей (для досылки ведомым)
    this.lastId = '';                       // id последнего события шлюза («запуск.номер») — для продолжения потока
    this.role = 'starting';
    this.state = 'connecting';              // connecting | open | closed
    this.denied = false;                    // 401: вход истёк
    this.restart = null;                    // {t, history, why}: поток начат заново — история браузера с этого момента
    this.onChange = onChange;
    this.pending = null;                    // ведомая до синхронизации: хвост, пришедший раньше снимка
    this.timer = null;
    this.bc = typeof BroadcastChannel === 'function' ? new BroadcastChannel(CHANNEL) : null;
  }

  start() {
    clockSource = this;
    if (this.bc && navigator.locks) {
      this.bc.onmessage = (e) => this.onBus(e.data);
      this.role = 'follower';
      this.pending = [];
      this.bc.postMessage({ type: 'sync?' });
      setTimeout(() => { if (this.role === 'follower' && this.pending) this.pending = null; }, SYNC_WAIT_MS);
      // замок держится, пока жива вкладка: обещание не разрешается
      navigator.locks.request(LOCK, () => new Promise(() => this.lead()));
    } else {
      this.lead();
    }
    setInterval(() => this.trim(), TRIM_EVERY_MS);
    return this;
  }

  lead() {
    this.role = 'leader';
    this.pending = null;
    this.open();
  }

  open() {
    const es = new EventSource(this.lastId ? `${this.url}?last=${encodeURIComponent(this.lastId)}` : this.url);
    this.es = es;
    es.onopen = () => { this.state = 'open'; this.changed(); };
    es.onmessage = (e) => {
      const m = JSON.parse(e.data);
      this.n += 1;
      if (e.lastEventId) this.lastId = e.lastEventId;
      this.ingest(m);
      if (this.bc) this.bc.postMessage({ type: 'msg', n: this.n, m, id: this.lastId });
    };
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) {
        this.state = 'closed';
        fetch('api/meta', { cache: 'no-store' }).then((r) => { this.denied = r.status === 401; this.changed(); }, () => {});
        setTimeout(() => { if (this.es === es) this.open(); }, REOPEN_MS);
      } else {
        this.state = 'connecting';
      }
      this.changed();
    };
  }

  onBus(d) {
    if (this.role === 'leader') {
      if (d.type === 'sync?') {
        this.trim();
        this.bc.postMessage({ type: 'sync', n: this.n, hello: this.hello, offset: this.offset, restart: this.restart, id: this.lastId, msgs: this.all() });
      }
      return;
    }
    if (d.type === 'sync' && this.pending) {
      this.tl = new Timeline(d.msgs);
      this.hello = d.hello;
      this.offset = d.offset;
      this.restart = d.restart;
      this.lastId = d.id || '';
      this.n = d.n;
      for (const p of this.pending) if (p.n > d.n) this.apply(p);
      this.pending = null;
      this.lastAt = performance.now();
      this.changed();
    } else if (d.type === 'msg') {
      if (this.pending) this.pending.push(d);
      else this.apply(d);
    }
  }

  apply(d) {
    this.n = d.n;
    if (d.id) this.lastId = d.id;
    this.ingest(d.m);
  }

  clock(t) {
    this.offsets.push(t - Date.now() / 1000);
    if (this.offsets.length > OFFSET_SAMPLES) this.offsets.shift();
    this.offset = Math.max(...this.offsets);
  }

  ingest(m) {
    this.lastAt = performance.now();
    if (m.k === 'hello') {
      if (!m.resume) {
        // новое подключение — снимок и буфер заново: история браузера до него выброшена (иначе тревоги прежнего
        // запуска шлюза висели бы вечно); экран говорит об этом, а не молча теряет час
        if (this.hello) {
          this.restart = { t: m.t, why: m.boot !== this.hello.boot ? 'шлюз перезапущен' : 'поток начат заново', boot: m.boot };
        }
        this.tl = new Timeline();
        this.offsets = [];
      }
      this.hello = m;
      this.clock(m.t);
      this.state = 'open';
    } else if (m.k === 'hb') {
      this.clock(m.t);
    } else if (m.k === 'scan') {
      this.tl.by.scan = [m];                       // скан — только последний
    } else {
      this.tl.push(m);
    }
    this.changed();
  }

  // первое сообщение в браузере после restart: «история с …»
  historyFrom() {
    let first = Infinity;
    for (const a of Object.values(this.tl.by)) if (a.length && a[0].t < first) first = a[0].t;
    return Number.isFinite(first) ? first : null;
  }

  // всё накопленное — для новой вкладки (по видам порядок сохраняется: сортировка устойчивая); после trim — в пределах KEEP
  all() {
    return Object.values(this.tl.by).flat().sort((a, b) => a.t - b.t);
  }

  trim() {
    const now = this.now();
    for (const [k, keep] of Object.entries(KEEP)) {
      const a = this.tl.list(k);
      if (!a.length) continue;
      const before = now - keep;
      let i = this.tl.idx(k, before);              // всё до i включительно — старше срока
      if (i < 0) continue;
      if (i >= a.length - 1) i = a.length - 2;     // последнее значение вида оставляем
      if (i < 0) continue;
      const old = a.slice(0, i + 1);
      const kept = keepOpen(k, old);
      this.tl.by[k] = kept.concat(a.slice(i + 1));
    }
  }

  now() { return Date.now() / 1000 + this.offset; }

  linkAge() { return this.lastAt == null ? Infinity : (performance.now() - this.lastAt) / 1000; }

  changed() {
    if (!this.onChange || this.timer) return;
    this.timer = setTimeout(() => { this.timer = null; if (this.onChange) this.onChange(); }, 100);
  }
}

// из старых сообщений тревог и жестов остаётся то, что ещё действует: стоящая тревога (последнее сообщение ключа —
// on) и незакрытый жест (без outcome) — все их сообщения, иначе свёртка потеряет since и поля жеста
function keepOpen(kind, old) {
  if (kind !== 'alarm' && kind !== 'gesture') return [];
  const field = kind === 'alarm' ? 'key' : 'id';
  const open = new Map();
  for (const m of old) {
    const id = m[field];
    if (kind === 'alarm') open.set(id, m.on ? (open.get(id) || []).concat(m) : null);
    else open.set(id, m.outcome ? null : (open.get(id) || []).concat(m));
  }
  return [...open.values()].filter(Boolean).flat().sort((a, b) => a.t - b.t);
}

// команда пульта: POST с заголовком CSRF (шлюз сверяет его и Origin) и временем отправки по часам робота (issued_at:
// запоздавшую команду шлюз не исполнит); ответа нет за timeoutMs — запрос брошен, итог честный: «нет ответа».
// Ответ — JSON шлюза и код HTTP (0 — нет связи или нет ответа)
export async function command(path, csrf, body = {}, { timeoutMs = COMMAND_TIMEOUT_MS, stamp = true } = {}) {
  const ctl = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctl && timeoutMs ? setTimeout(() => ctl.abort(), timeoutMs) : null;
  const issued = stamp ? robotNow() : null;
  try {
    const res = await fetch(path, {
      method: 'POST', cache: 'no-store', signal: ctl ? ctl.signal : undefined,
      body: JSON.stringify(stamp && issued != null ? { ...body, issued_at: Math.round(issued * 1000) / 1000 } : body),
      headers: { 'Content-Type': 'application/json', 'X-Neurobot-CSRF': csrf },
    });
    let doc = {};
    try { doc = await res.json(); } catch (e) { doc = { error: 'HTTP ' + res.status }; }
    return { status: res.status, ...doc };
  } catch (e) {
    if (e && e.name === 'AbortError') {
      return { status: 0, ok: false, timeout: true, error: `нет ответа за ${timeoutMs / 1000} с: дошла ли команда — неизвестно` };
    }
    return { status: 0, ok: false, error: 'нет связи со шлюзом: ' + e.message };
  } finally {
    if (timer) clearTimeout(timer);
  }
}
