// Pure, deterministic analysis of recorded observations. No device commands or generated measurements.
export const RECIPE_FORMAT = 'edgelab-recipe/1';
export const NOTEBOOK_FORMAT = 'edgelab-notebook/1';
export const MAX_CARDS = 40;
export const MAX_NOTEBOOK_BYTES = 2 * 1024 * 1024;
export const MAX_OPERATORS = 12;
const MAX_SIGNALS = 256, MAX_POINTS = 20000;
const finite = v => typeof v === 'number' && Number.isFinite(v);
const plain = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const text = (v, max = 500) => typeof v === 'string' ? v.slice(0, max) : '';
const clockText = v => typeof v === 'string' ? v : plain(v) ? canonical(v) : '';

export function canonical(value) {
  const seen = new WeakSet();
  const visit = (v, depth) => {
    if (depth > 48) return '[depth limit]';
    if (typeof v === 'number') return Number.isFinite(v) ? v : null;
    if (v === null || typeof v !== 'object') return v;
    if (seen.has(v)) return '[circular]';
    seen.add(v);
    const result = Array.isArray(v) ? v.map(x => visit(x, depth + 1)) : Object.fromEntries(
      Object.keys(v).sort().map(k => [k, visit(v[k], depth + 1)]));
    seen.delete(v);
    return result;
  };
  return JSON.stringify(visit(value, 0));
}

export function fingerprint(value) {
  const source = typeof value === 'string' ? value : canonical(value);
  let hash = 2166136261;
  for (let i = 0; i < source.length; i++) hash = Math.imul(hash ^ source.charCodeAt(i), 16777619);
  return (hash >>> 0).toString(16).padStart(8, '0');
}

export function sourceFingerprint(run) {
  const sha = run?.provenance?.raw_trace_sha256 || run?.provenance?.sha256;
  const declared = run?.fingerprint || run?.source_hash || run?.sha256 || run?.source?.sha256 || sha;
  if (declared) return { algorithm: declared === sha ? 'sha256' : typeof declared === 'object' ? declared.algorithm || 'source' : 'source',
    value: typeof declared === 'object' ? String(declared.value || declared.sha256 || canonical(declared)) : String(declared),
    scope: declared === sha ? 'SHA-256 исходного журнала, переданный загрузчиком' : 'Идентификатор передан загрузчиком источника' };
  return { algorithm: 'fnv1a32', value: fingerprint({ id: run?.id, clock: run?.clock, config: run?.config, events: run?.events || [] }),
    scope: 'Нормализованный журнал и конфигурация; локальный некриптографический отпечаток' };
}

export function classifyValue(value, metadata = {}) {
  const mime = String(metadata.contentMediaType || metadata.mimeType || metadata.mime_type || metadata.content_type || (plain(value) && value.mimeType) || '');
  const declared = metadata.semantic_type || metadata.semanticType || metadata.format || '';
  if (mime.startsWith('image/')) return { type: 'image', evidence: 'Явный MIME источника', mime };
  if (mime.startsWith('audio/')) return { type: 'audio', evidence: 'Явный MIME источника', mime };
  if (['depth', 'pose', 'pointcloud', 'cloud', 'image', 'audio'].includes(declared)) return {
    type: declared === 'pointcloud' ? 'cloud' : declared, evidence: 'Явный тип источника', mime };
  const type = value === null || value === undefined ? 'null' : finite(value) ? 'scalar' :
    typeof value === 'boolean' ? 'boolean' : typeof value === 'string' ? 'string' : 'raw';
  return { type, evidence: 'Тип по значению JSON; физический смысл не выведен', mime };
}

function leaves(value, path = [], out = [], depth = 0) {
  if (out.length >= 64 || depth > 6) return out;
  if (Array.isArray(value)) {
    // Large arrays remain a typed/raw payload rather than thousands of invented scalar instruments.
    if (value.length > 16) return out;
    value.forEach((v, i) => leaves(v, [...path, i], out, depth + 1));
  } else if (plain(value)) {
    for (const [k, v] of Object.entries(value)) leaves(v, [...path, k], out, depth + 1);
  } else if (finite(value) || typeof value === 'boolean' || value == null) out.push({ path, value });
  return out;
}
export function pathLabel(path) {
  return path.reduce((s, p) => typeof p === 'number' ? s + '[' + p + ']' : s ? s + '.' + p : p, '');
}
function traceUnit(type, path) {
  const key = path.join('.');
  if ((type === 'llm_response' && key === 'latency_ms') || (type === 'tool_result' && key === 'ms')) return 'ms';
  if ((type === 'llm_response' && key.startsWith('usage.') && key.endsWith('tokens')) ||
      (type === 'llm_request' && (key === 'prompt_tokens_est' || key.startsWith('segments_tokens_est.')))) return 'tokens';
  if (['bytes', 'image_bytes'].includes(key)) return 'bytes';
  if (['n_tools', 'n_messages', 'n_tool_calls', 'attempts', 'step', 'chars'].includes(key)) return 'count';
  return '';
}

export function discoverSignals(run) {
  const map = new Map(), warnings = new Set();
  const runClock = clockText(run?.clock), runId = String(run?.id || 'run');
  const events = (Array.isArray(run?.events) ? run.events : []).map((e, index) => ({ e, index }))
    .filter(({ e }) => e && finite(e.t)).sort((a, b) => a.e.t - b.e.t || a.index - b.index);
  const append = (key, label, value, event, index, metadata = {}, declaredUnit = '', unitEvidence = '') => {
    if (!map.has(key)) {
      if (map.size >= MAX_SIGNALS) { warnings.add('Показаны первые ' + MAX_SIGNALS + ' сигналов; исходный журнал сохранён.'); return; }
      map.set(key, { id: key, label, samples: [], metadata, types: new Set(), units: new Set(), clocks: new Set(),
        unitEvidence, device: event.device || '', property: event.property || '', eventType: event.type || '', omitted: 0 });
    }
    const signal = map.get(key), classification = classifyValue(value, metadata);
    if (classification.type !== 'null') signal.types.add(classification.type);
    if (declaredUnit) signal.units.add(String(declaredUnit));
    const clock = clockText(event.clock) || runClock;
    signal.clocks.add(clock);
    if (signal.samples.length >= MAX_POINTS) { signal.omitted++; return; }
    signal.samples.push({ t: event.t, value: typeof value === 'number' && !Number.isFinite(value) ? null : value,
      eventId: String(event.id ?? event.seq ?? index), eventIndex: index, clock, unit: declaredUnit || null });
  };
  for (const { e, index } of events) {
    if (e.type === 'input' && ['sensor', 'image', 'audio', 'media', 'depth', 'pose', 'cloud'].includes(e.kind) && Object.prototype.hasOwnProperty.call(e, 'value')) {
      const declaredDevices = run?.scenario_truth?.header?.devices || [];
      const deviceRows = Array.isArray(declaredDevices) ? declaredDevices : Object.entries(declaredDevices).map(([id, d]) => ({ id, ...d }));
      const declaredDevice = deviceRows.find(d => d.id === e.device);
      const propertySchema = declaredDevice?.properties?.[e.property];
      const metadata = { ...(plain(propertySchema) ? propertySchema : {}), ...(plain(e.schema) ? e.schema : {}), ...(plain(e.metadata) ? e.metadata : {}) };
      for (const name of ['contentMediaType', 'mimeType', 'contentEncoding', 'semantic_type', 'frame_id']) if (e[name] != null) metadata[name] = e[name];
      if (plain(e.value) && typeof e.value.mimeType === 'string') metadata.mimeType = e.value.mimeType;
      if (plain(e.value) && typeof e.value.data === 'string' && metadata.mimeType) metadata.contentEncoding = e.value.contentEncoding || 'base64';
      const device = String(e.device || 'sensor'), property = String(e.property || 'value');
      const key = 'sensor:' + JSON.stringify([e.server || '', device, property]);
      const unit = typeof e.unit === 'string' ? e.unit : typeof metadata.unit === 'string' ? metadata.unit : '';
      append(key, device + ' / ' + property, e.value, e, index, metadata, unit, unit ? (e.unit ? 'Единица явно передана событием' : plain(propertySchema) && propertySchema.unit ? 'Единица из схемы устройства в сценарии' : 'Единица явно передана источником') : 'Единица не указана');
      if (classifyValue(e.value, metadata).type === 'raw') for (const leaf of leaves(e.value)) {
        const field = (metadata.fields || {})[pathLabel(leaf.path)] || {};
        append(key + ':' + JSON.stringify(leaf.path), device + ' / ' + property + '.' + pathLabel(leaf.path),
          leaf.value, e, index, field, typeof field.unit === 'string' ? field.unit : '', 'Единица вложенного поля не выведена');
      }
      continue;
    }
    if (!['llm_request', 'llm_response', 'tool_result', 'tool_call', 'budget', 'run_end'].includes(e.type)) continue;
    const payload = Object.fromEntries(Object.entries(e).filter(([k]) => !['t', 'ts_clock', 'ts_wall', 'seq', 'id', 'run_id', 'clock', 'args', 'context_snapshot', 'prompt', 'messages', 'tools'].includes(k)));
    for (const leaf of leaves(payload)) {
      if (!finite(leaf.value) && typeof leaf.value !== 'boolean') continue;
      const unit = traceUnit(e.type, leaf.path);
      append('trace:' + JSON.stringify([e.type, leaf.path]), e.type + ' / ' + pathLabel(leaf.path), leaf.value,
        e, index, {}, unit, unit ? 'Единица контракта журнала' : 'Единица не указана');
    }
  }
  const signals = [...map.values()].map(s => {
    const types = [...s.types], clocks = [...s.clocks], units = [...s.units];
    if (s.omitted) warnings.add(s.label + ': обработаны первые ' + MAX_POINTS + ' отсчётов из ' + (MAX_POINTS + s.omitted));
    return { ...s, types, clocks, units, type: types.length === 1 ? types[0] : types.length ? 'mixed' : 'null',
      unit: units.length === 1 ? units[0] : '', unitKnown: units.length === 1, unitConflict: units.length > 1,
      clock: clocks.length === 1 ? clocks[0] : '', clockKnown: clocks.length === 1 && !!clocks[0],
      clockKey: runId + ':' + (clocks.length === 1 ? clocks[0] : 'mixed'), runId };
  });
  return { signals, warnings: [...warnings], eventCount: events.length };
}

export function summarize(samples) {
  const values = samples.map(p => p.value).filter(finite), valid = values.length;
  return { samples: samples.length, valid, missing: samples.filter(p => p.value == null).length,
    trueCount: samples.filter(p => p.value === true).length, falseCount: samples.filter(p => p.value === false).length,
    min: valid ? Math.min(...values) : null, max: valid ? Math.max(...values) : null,
    mean: valid ? values.reduce((a, b) => a + b, 0) / valid : null,
    firstT: samples.length ? samples[0].t : null, lastT: samples.length ? samples[samples.length - 1].t : null };
}
const bounded = (value, fallback, lo, hi) => {
  if (value === undefined) return fallback;
  if (!finite(value) || value < lo || value > hi) throw Error('Параметр оператора должен быть числом от ' + lo + ' до ' + hi);
  return value;
};
export function normalizeOperator(op, index = 0) {
  const kind = op?.op;
  if (!['raw', 'average', 'derivative', 'threshold', 'difference'].includes(kind)) throw Error('Неизвестный оператор');
  const base = { id: text(op.id, 80) || 'op-' + index, op: kind };
  if (kind === 'average') return { ...base, windowS: bounded(op.windowS, 10, .001, 86400), maxGapS: bounded(op.maxGapS, 60, .001, 86400) };
  if (kind === 'derivative') return { ...base, maxGapS: bounded(op.maxGapS, 60, .001, 86400) };
  if (kind === 'threshold') return { ...base, compare: ['gt', 'gte', 'lt', 'lte'].includes(op.compare) ? op.compare : 'gt',
    value: bounded(op.value, 0, -1e15, 1e15), holdS: bounded(op.holdS, 0, 0, 86400), maxGapS: bounded(op.maxGapS, 60, .001, 86400) };
  if (kind === 'difference') return { ...base, signalId: text(op.signalId, 1500), maxAgeS: bounded(op.maxAgeS, 2, 0, 86400) };
  return base;
}

export function differenceCompatibility(left, right) {
  if (!right) return 'Выберите второй сигнал';
  if (left.type !== 'scalar' || right.type !== 'scalar') return 'Разность определена только для числовых сигналов';
  if (!left.clockKnown || !right.clockKnown || left.clockKey !== right.clockKey) return 'Области часов не совпадают или не объявлены';
  if (!left.unitKnown || !right.unitKnown) return 'Для разности нужны явно объявленные единицы обоих сигналов';
  if (left.unit !== right.unit) return 'Единицы различаются: ' + left.unit + ' и ' + right.unit + '. Автоконвертации нет';
  return '';
}

export function applyPipeline(signal, operators = [], signals = [], options = {}) {
  if (!signal) return { samples: [], nodes: [], errors: [], signal: null, summary: summarize([]) };
  let current = { ...signal, samples: signal.samples.filter(p => !finite(options.until) || p.t <= options.until) };
  const nodes = [], errors = [];
  for (const [index, input] of operators.slice(0, MAX_OPERATORS).entries()) {
    let op;
    try { op = normalizeOperator(input, index); } catch (e) { errors.push(e.message); break; }
    const before = current;
    let error = '';
    if (op.op !== 'raw' && current.type !== 'scalar') error = 'Оператор требует числовой сигнал; текущий тип: ' + current.type;
    if (op.op !== 'raw' && current.unitConflict) error = 'В одном сигнале смешаны единицы измерения. Сначала уточните схему источника';
    if (op.op !== 'raw' && (!current.clockKnown || current.clocks?.length > 1)) error = 'Для обработки по времени нужна одна явно объявленная область часов';
    const ref = op.op === 'difference' ? signals.find(s => s.id === op.signalId) : null;
    if (!error && op.op === 'difference') error = differenceCompatibility(current, ref);
    if (error) {
      nodes.push({ ...op, error, inputType: current.type, outputType: current.type, inputUnit: current.unit, outputUnit: current.unit, summary: summarize([]) });
      errors.push(error); break;
    }
    let samples = current.samples, type = current.type, unit = current.unit, unitKnown = current.unitKnown;
    if (op.op === 'average') {
      let queue = [], left = 0, sum = 0, previous = null;
      samples = samples.map(p => {
        if (!finite(p.value)) { queue = []; left = sum = 0; previous = null; return { ...p, value: null }; }
        if (previous != null && p.t - previous > op.maxGapS) { queue = []; left = sum = 0; }
        while (left < queue.length && queue[left].t < p.t - op.windowS) sum -= queue[left++].value;
        queue.push(p); sum += p.value; previous = p.t;
        return { ...p, value: sum / (queue.length - left) };
      });
    } else if (op.op === 'derivative') {
      let previous = null;
      samples = samples.map(p => {
        const dt = previous ? p.t - previous.t : 0;
        const value = finite(p.value) && previous && dt > 0 && dt <= op.maxGapS ? (p.value - previous.value) / dt : null;
        previous = finite(p.value) ? p : null;
        return { ...p, value: finite(value) ? value : null };
      });
      unit = unitKnown ? unit + '/s' : 'ед./s';
    } else if (op.op === 'threshold') {
      let since = null, previous = null;
      const compare = v => ({ gt: v > op.value, gte: v >= op.value, lt: v < op.value, lte: v <= op.value })[op.compare];
      samples = samples.map(p => {
        if (!finite(p.value)) { since = previous = null; return { ...p, value: null }; }
        if (previous != null && p.t - previous > op.maxGapS) since = null;
        previous = p.t;
        if (!compare(p.value)) { since = null; return { ...p, value: false }; }
        if (since == null) since = p.t;
        return { ...p, value: p.t - since >= op.holdS };
      });
      type = 'boolean'; unit = ''; unitKnown = true;
    } else if (op.op === 'difference') {
      let j = -1;
      samples = samples.map(p => {
        while (j + 1 < ref.samples.length && ref.samples[j + 1].t <= p.t) j++;
        const other = ref.samples[j], usable = other && p.t - other.t <= op.maxAgeS && finite(other.value) && finite(p.value);
        return { ...p, value: usable ? p.value - other.value : null, referenceEventId: other?.eventId || null };
      });
    }
    current = { ...current, samples, type, unit, unitKnown };
    nodes.push({ ...op, inputType: before.type, outputType: type, inputUnit: before.unit,
      outputUnit: unit, summary: summarize(samples), error: '' });
  }
  return { samples: current.samples, nodes, errors, signal: current, summary: summarize(current.samples) };
}

export function sampleAt(samples, cursor) {
  if (!samples?.length) return null;
  const t = finite(cursor) ? cursor : samples[samples.length - 1].t;
  let lo = 0, hi = samples.length - 1, found = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (samples[mid].t <= t) { found = mid; lo = mid + 1; } else hi = mid - 1; }
  return found < 0 ? null : samples[found];
}

export function exportRecipe(run, signal, operators, until = null) {
  return { format: RECIPE_FORMAT, source: sourceFingerprint(run), runId: String(run.id), signalId: signal?.id || '',
    label: signal?.label || '', clock: signal?.clock || '', unit: signal?.unit || null, until: finite(until) ? until : null,
    operators: operators.map(normalizeOperator) };
}
export function importRecipe(input, signals) {
  const value = typeof input === 'string' ? JSON.parse(input) : input;
  if (!plain(value) || value.format !== RECIPE_FORMAT || typeof value.signalId !== 'string' || !Array.isArray(value.operators)) throw Error('Это не рецепт EdgeLab');
  if (value.operators.length > MAX_OPERATORS) throw Error('В рецепте больше ' + MAX_OPERATORS + ' операторов');
  if (!signals.some(s => s.id === value.signalId)) throw Error('Исходного сигнала рецепта нет в этом прогоне');
  return { signalId: value.signalId, operators: value.operators.map(normalizeOperator), source: value.source || null,
    until: finite(value.until) ? value.until : null };
}

function boundedJSON(value, max = 24000) {
  const encoded = canonical(value ?? null);
  return encoded.length <= max ? JSON.parse(encoded) : { omitted: true, characters: encoded.length,
    fingerprint: { algorithm: 'fnv1a32', value: fingerprint(encoded) }, preview: encoded.slice(0, 2000) };
}
export function makeEvidence({ run, cursor, event, signal, analysis, operators = [], until = null, annotation = '' }) {
  const source = sourceFingerprint(run), point = sampleAt(analysis?.samples || [], cursor);
  const recipe = signal ? exportRecipe(run, signal, operators, until) : null;
  const focus = event || (run.events || []).find(e => String(e.id) === point?.eventId) || null;
  const identity = { source, t: finite(cursor) ? cursor : focus?.t ?? point?.t ?? null, eventId: focus?.id ?? point?.eventId ?? null, recipe };
  if (!finite(identity.t)) throw Error('Выберите момент или событие для фиксации');
  return { id: 'evidence-' + fingerprint(identity), createdAt: new Date().toISOString(),
    run: { id: String(run.id), label: text(run.label || run.id, 300), origin: text(run.origin, 300), clock: clockText(run.clock), t_start: finite(run.t_start) ? run.t_start : null, source },
    cursor: identity.t, eventId: identity.eventId == null ? null : String(identity.eventId), event: boundedJSON(focus),
    config: boundedJSON(run.config), recipe, result: signal ? { label: signal.label, type: analysis.signal?.type || signal.type,
      unit: analysis.signal?.unit || null, value: point?.value ?? null, sampleT: point?.t ?? null, sourceEventId: point?.eventId || null,
      referenceEventId: point?.referenceEventId || null, summary: analysis.summary, errors: analysis.errors || [], lineage: analysis.nodes || [] } : null,
    annotation: text(annotation, 4000) };
}

export function validateNotebook(input) {
  const encoded = typeof input === 'string' ? input : JSON.stringify(input);
  if (encoded.length > MAX_NOTEBOOK_BYTES / 2) throw Error('Блокнот превышает безопасный размер 2 МиБ');
  const value = typeof input === 'string' ? JSON.parse(input) : input;
  if (!plain(value) || value.format !== NOTEBOOK_FORMAT || !Array.isArray(value.cards)) throw Error('Это не блокнот EdgeLab');
  if (value.cards.length > MAX_CARDS) throw Error('В блокноте больше ' + MAX_CARDS + ' карточек');
  const ids = new Set();
  return value.cards.map(card => {
    if (!plain(card) || typeof card.id !== 'string' || !plain(card.run) || !finite(card.cursor) || !plain(card.run.source)) throw Error('Карточка не содержит связи с источником и временем');
    if (ids.has(card.id)) throw Error('В блокноте повторяются идентификаторы карточек');
    ids.add(card.id);
    return { id: text(card.id, 120), createdAt: text(card.createdAt, 80),
      run: { id: text(card.run.id, 300), label: text(card.run.label, 300), origin: text(card.run.origin, 300), clock: text(card.run.clock, 600), t_start: finite(card.run.t_start) ? card.run.t_start : null,
        source: { algorithm: text(card.run.source.algorithm, 100), value: text(card.run.source.value, 1000), scope: text(card.run.source.scope, 1000) } },
      cursor: card.cursor, eventId: card.eventId == null ? null : text(String(card.eventId), 300), event: boundedJSON(card.event),
      config: boundedJSON(card.config), recipe: boundedJSON(card.recipe, 20000), result: boundedJSON(card.result, 40000), annotation: text(card.annotation, 4000) };
  });
}
export function notebookDocument(cards) { return { format: NOTEBOOK_FORMAT, exportedAt: new Date().toISOString(), cards }; }
export function mergeNotebook(current, incoming) {
  const merged = new Map(current.map(c => [c.id, c]));
  for (const card of incoming) if (!merged.has(card.id)) merged.set(card.id, card);
  if (merged.size > MAX_CARDS) throw Error('Блокнот вмещает ' + MAX_CARDS + ' карточек. Экспортируйте и удалите лишние');
  const cards = [...merged.values()];
  validateNotebook(notebookDocument(cards));
  return cards;
}
export function escapeHTML(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]); }
function readableJSON(value) { return JSON.stringify(value ?? null, null, 2); }
export function notebookHTML(cards) {
  const esc = escapeHTML;
  return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>EdgeLab · Блокнот доказательств</title>' +
    '<style>body{max-width:960px;margin:48px auto;padding:0 24px;font:16px/1.6 system-ui;color:#172b28;background:#fbfcf9}h1{font-size:32px}article{margin:32px 0;border:1px solid #ccd6cf;border-radius:12px;padding:24px}small{color:#52615a}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef2ed;padding:16px;font:12px/1.5 monospace}blockquote{white-space:pre-wrap;border-left:3px solid #829d35;padding-left:16px;margin-left:0}dl{display:grid;grid-template-columns:140px 1fr;gap:8px}dd{margin:0;overflow-wrap:anywhere}@media print{article{break-inside:avoid}body{margin:0}}</style>' +
    '<h1>Блокнот эксперимента</h1><p>Зафиксированные события, вычисления и заметки. Значения получены из указанных источников; операторы обработки сохранены вместе с результатом.</p>' +
    cards.map((c, i) => '<article><small>НАБЛЮДЕНИЕ ' + (i + 1) + '</small><h2>' + esc(c.result?.label || c.event?.type || 'Событие') + '</h2>' +
      '<dl><dt>Прогон</dt><dd>' + esc(c.run.label) + ' · ' + esc(c.run.id) + '</dd><dt>Источник</dt><dd>' + esc(c.run.origin) + '</dd><dt>Время журнала</dt><dd>' + esc(c.cursor) + '</dd><dt>Событие</dt><dd>' + esc(c.eventId || '—') + '</dd><dt>Область часов</dt><dd>' + esc(c.run.clock || 'не указана') + '</dd><dt>Отпечаток</dt><dd>' + esc(c.run.source.algorithm + ': ' + c.run.source.value) + '</dd></dl>' +
      '<p><small>' + esc(c.run.source.scope) + '</small></p><blockquote>' + esc(c.annotation || 'Заметка не добавлена') + '</blockquote>' +
      '<h3>Результат и происхождение</h3><pre>' + esc(readableJSON(c.result)) + '</pre><h3>Рецепт</h3><pre>' + esc(readableJSON(c.recipe)) +
      '</pre><details><summary>Исходное событие и конфигурация</summary><pre>' + esc(readableJSON({ event: c.event, config: c.config })) + '</pre></details></article>').join('') + '</html>';
}
function fenced(value) {
  const s = typeof value === 'string' ? value : readableJSON(value), longest = Math.max(2, ...(s.match(/`+/g) || []).map(x => x.length));
  const fence = '`'.repeat(longest + 1); return fence + '\n' + s + '\n' + fence;
}
export function notebookMarkdown(cards) {
  return '# Блокнот эксперимента · EdgeLab\n\n' + cards.map((c, i) => '## Наблюдение ' + (i + 1) + '\n\n' +
    fenced({ run: c.run, cursor: c.cursor, eventId: c.eventId }) + '\n\n### Заметка\n\n' + fenced(c.annotation || 'Заметка не добавлена') +
    '\n\n### Результат\n\n' + fenced(c.result) + '\n\n### Рецепт\n\n' + fenced(c.recipe) + '\n\n### Источник\n\n' + fenced({ event: c.event, config: c.config })).join('\n\n');
}

export function recordedMediaSource(value, metadata = {}, family = 'image') {
  const candidate = typeof value === 'string' ? value : plain(value) ? value.data || value.url || value.uri : '';
  if (typeof candidate !== 'string' || candidate.length > 1500000) return '';
  const allowed = family === 'image' ? /^data:image\/(png|jpeg|webp);base64,[a-zA-Z0-9+/=\s]+$/ : /^data:audio\/(wav|ogg|mpeg);base64,[a-zA-Z0-9+/=\s]+$/;
  if (allowed.test(candidate)) return candidate;
  if (/^\/?api\/lab\/media\/[a-zA-Z0-9_./?=&%-]+$/.test(candidate) && !candidate.includes('..')) return candidate;
  const mime = metadata.contentMediaType || metadata.mimeType || (plain(value) && value.mimeType);
  const mimeAllowed = family === 'image' ? ['image/png', 'image/jpeg', 'image/webp'] : ['audio/wav', 'audio/ogg', 'audio/mpeg'];
  if ((metadata.contentEncoding === 'base64' || (plain(value) && value.mimeType && typeof value.data === 'string')) && mimeAllowed.includes(mime) && /^[a-zA-Z0-9+/=\s]+$/.test(candidate)) return 'data:' + mime + ';base64,' + candidate;
  return '';
}
