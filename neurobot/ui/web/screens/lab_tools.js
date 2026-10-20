import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { html } from '../lib/ui.js';
import { Icon } from '../lib/icons.js';
import { num, dur } from '../lib/fmt.js';
import { applyPipeline, classifyValue, differenceCompatibility, discoverSignals, exportRecipe, importRecipe,
  canonical, makeEvidence, MAX_CARDS, MAX_NOTEBOOK_BYTES, MAX_OPERATORS, mergeNotebook, notebookDocument,
  notebookHTML, notebookMarkdown, recordedMediaSource, sampleAt, sourceFingerprint, validateNotebook } from '../lib/lab_analysis.js';

const STORE = 'edgelab.evidence.v1';
const OPERATORS = {
  raw: ['Без изменений', 'Сохранить исходные значения'],
  average: ['Скользящее среднее', 'Среднее наблюдений в предыдущем окне'],
  derivative: ['Производная', 'Изменение между соседними отсчётами / время'],
  threshold: ['Порог и выдержка', 'Условие по последовательности наблюдений'],
  difference: ['Разность сигналов', 'Последний совместимый отсчёт второго сигнала'],
};
const TYPES = { scalar: 'Число', boolean: 'Состояние', image: 'Изображение', audio: 'Аудио', depth: 'Глубина',
  pose: 'Поза', cloud: 'Облако точек', string: 'Текст', raw: 'JSON', null: 'Нет значения', mixed: 'Смешанный тип' };
const finite = v => typeof v === 'number' && Number.isFinite(v);
const unitLabel = unit => ({ Cel: '°C', 'Cel/s': '°C/с', ms: 'мс', s: 'с', 'ед./s': 'ед./с', tokens: 'ток.', bytes: 'байт', count: 'шт.' })[unit] || (unit || '');
function valueLabel(value) {
  if (value == null) return '—';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (finite(value)) return num(value, 4).replace(/(,\d*?[1-9])0+$/, '$1').replace(/,0+$/, '');
  if (typeof value === 'object') return Array.isArray(value) ? 'Массив · ' + value.length : 'Объект JSON';
  return String(value);
}
function timeLabel(t, run) { return finite(t) ? 'T' + dur(t - (finite(run?.t_start) ? run.t_start : 0), true) : '—'; }
function download(body, name, type) {
  const url = URL.createObjectURL(new Blob([body], { type })), anchor = document.createElement('a');
  anchor.href = url; anchor.download = name; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function readNotebook() {
  try { const raw = localStorage.getItem(STORE); return { cards: raw ? validateNotebook(raw) : [], error: '' }; }
  catch (e) { return { cards: [], error: 'Сохранённый блокнот не прочитан: ' + e.message + '. Исходная запись в хранилище не изменена.' }; }
}
function useNotebook() {
  const [initial] = useState(readNotebook), [cards, setCards] = useState(initial.cards), [error, setError] = useState(initial.error);
  const [dirty, setDirty] = useState(false), [saved, setSaved] = useState(false);
  // A saved result must survive immediate navigation to the journal. Storage is
  // bounded to 2 MiB; commit each edit before publishing the new card state.
  const update = next => {
    setCards(next); setSaved(false); setDirty(true);
    try {
      const raw = JSON.stringify(notebookDocument(next));
      validateNotebook(raw);
      localStorage.setItem(STORE, raw); setError(''); setDirty(false); setSaved(true);
    } catch (e) { setError('Блокнот остаётся в этой вкладке: ' + e.message + '. Экспортируйте JSON, чтобы сохранить его.'); }
  };
  return { cards, update, error, saved, dirty };
}

function SeriesChart({ samples, type, title, unit, run, cursor, onSeek, derived = false }) {
  const [hover, setHover] = useState(null);
  const points = samples.slice(-4000), values = points.map(p => typeof p.value === 'boolean' ? Number(p.value) : p.value).filter(finite);
  const W = 800, H = 200, L = 44, R = 14, B = 28, TOP = 16;
  const start = points[0]?.t ?? 0, end = points[points.length - 1]?.t ?? start + 1;
  const low = values.length ? Math.min(...values) : 0, high = values.length ? Math.max(...values) : 1;
  const padding = low === high ? Math.max(1, Math.abs(low) * .03) : (high - low) * .12;
  const min = type === 'boolean' ? -.1 : low - padding, max = type === 'boolean' ? 1.1 : high + padding;
  const X = t => L + (t - start) / Math.max(.00001, end - start) * (W - L - R);
  const Y = v => H - B - (v - min) / (max - min) * (H - B - TOP);
  let pen = false;
  const path = points.map(p => {
    const v = typeof p.value === 'boolean' ? Number(p.value) : p.value;
    if (!finite(v)) { pen = false; return ''; }
    const next = !pen ? `M${X(p.t)},${Y(v)}` : type === 'boolean' ? `H${X(p.t)} V${Y(v)}` : `L${X(p.t)},${Y(v)}`;
    pen = true; return next;
  }).join(' ');
  const indexAt = event => {
    if (!points.length) return null;
    const box = event.currentTarget.getBoundingClientRect(), target = start + Math.max(0, Math.min(1, ((event.clientX - box.left) / box.width * W - L) / (W - L - R))) * (end - start);
    let best = 0;
    for (let i = 1; i < points.length; i++) if (Math.abs(points[i].t - target) < Math.abs(points[best].t - target)) best = i;
    return best;
  };
  const inspected = points[hover] || sampleAt(points, cursor), currentX = finite(cursor) && cursor >= start && cursor <= end ? X(cursor) : null;
  return html`<section class=${'lab-series' + (derived ? ' is-derived' : '')} data-testid=${derived ? 'lab-derived-chart' : 'lab-source-chart'}>
    <div class="lab-series-heading"><span>${derived ? '02 / РЕЗУЛЬТАТ' : '01 / ИСТОЧНИК'}</span><h3>${title}</h3><small>${unitLabel(unit) || 'Единица не объявлена'}</small></div>
    <div class="lab-chart-readout"><b>${valueLabel(inspected?.value)}</b><span>${unitLabel(unit)}</span><small>${inspected ? timeLabel(inspected.t, run) : 'Нет отсчёта в выбранный момент'}</small></div>
    <svg viewBox=${`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" tabindex="0" aria-label=${title + '. Стрелки — выбрать отсчёт, Enter — перейти к событию'}
      onPointerMove=${e => setHover(indexAt(e))} onPointerLeave=${() => setHover(null)} onClick=${e => { const i = indexAt(e); if (i != null) onSeek?.(points[i].t); }}
      onKeyDown=${e => { if (!points.length) return; if (['ArrowLeft', 'ArrowRight'].includes(e.key)) { e.preventDefault(); e.stopPropagation(); setHover(Math.max(0, Math.min(points.length - 1, (hover ?? points.length - 1) + (e.key === 'ArrowLeft' ? -1 : 1)))); } if (e.key === 'Enter' && inspected) { e.preventDefault(); e.stopPropagation(); onSeek?.(inspected.t); } }}>
      ${[0, .5, 1].map(q => html`<g><line x1=${L} x2=${W - R} y1=${TOP + (H - B - TOP) * q} y2=${TOP + (H - B - TOP) * q} class="lab-chart-grid" /><text x=${L - 7} y=${TOP + (H - B - TOP) * q + 3} text-anchor="end">${valueLabel(max - (max - min) * q)}</text></g>`)}
      ${path ? html`<path class="lab-chart-line" d=${path} />` : ''}
      ${currentX != null ? html`<line class="lab-chart-cursor" x1=${currentX} x2=${currentX} y1=${TOP} y2=${H - B} />` : ''}
      ${inspected && (finite(inspected.value) || typeof inspected.value === 'boolean') ? html`<circle class="lab-chart-dot" cx=${X(inspected.t)} cy=${Y(Number(inspected.value))} r="4" />` : ''}
      <text x=${L} y=${H - 6}>${timeLabel(start, run)}</text><text x=${W - R} y=${H - 6} text-anchor="end">${timeLabel(end, run)}</text>
    </svg>
    ${!values.length ? html`<div class="lab-chart-empty">${samples.length ? 'В этом участке нет числовых измерений' : 'Журнал не содержит отсчётов этого сигнала'}</div>` : ''}
    <div class="lab-chart-caption"><span>${points.length < samples.length ? `Последние ${points.length} из ${samples.length}` : samples.length + ' отсчётов'} · разрывы сохранены</span><span>Нажмите на график → событие</span></div>
  </section>`;
}

function PayloadView({ signal, sample }) {
  const value = sample?.value, kind = classifyValue(value, signal?.metadata || {}), source = recordedMediaSource(value, signal?.metadata || {}, kind.type);
  return html`<section class="lab-payload" data-testid="lab-typed-payload"><div class="lab-payload-head"><span class="lab-type-badge">${TYPES[kind.type] || kind.type}</span><small>${kind.evidence}</small></div>
    ${kind.type === 'image' && source ? html`<figure><img src=${source} alt=${'Записанный кадр: ' + signal.label} loading="lazy" /><figcaption>Кадр из записи · время ${sample?.t} · ${kind.mime}</figcaption></figure>` :
      kind.type === 'audio' && source ? html`<div class="lab-recorded-audio"><audio controls preload="none" src=${source}></audio><p>Записанный фрагмент · ${kind.mime}</p></div>` :
      html`<div class="lab-payload-note">${['image', 'audio'].includes(kind.type) ? 'В записи нет поддерживаемых локальных медиаданных. Доступно исходное значение.' : ['depth', 'pose', 'cloud'].includes(kind.type) ? 'Пространственный тип объявлен источником. Системы координат и калибровка автоматически не выведены.' : 'Значение показано без преобразования физического смысла.'}</div>`}
    <details><summary>Исходное значение и схема</summary><pre>${JSON.stringify({ value, metadata: signal?.metadata || {} }, null, 2)}</pre></details></section>`;
}

function NumberField({ label, value, min, max, step = 'any', onChange }) {
  return html`<label class="lab-number-field"><span>${label}</span><input type="number" value=${value} min=${min} max=${max} step=${step} onInput=${e => onChange(Number(e.target.value))} /></label>`;
}
function Operator({ operator, node, index, count, update, remove, move, signals, source }) {
  const title = OPERATORS[operator.op] || OPERATORS.raw;
  const change = patch => update({ ...operator, ...patch });
  return html`<article class=${'lab-operator' + (node?.error ? ' has-error' : '')} data-testid="lab-operator" data-op=${operator.op}>
    <div class="lab-operator-top"><span class="lab-node-number">${String(index + 1).padStart(2, '0')}</span><div><h3>${title[0]}</h3><p>${title[1]}</p></div><div class="lab-node-actions">
      <button disabled=${index === 0} onClick=${() => move(-1)} aria-label=${'Поднять оператор ' + (index + 1)}>↑</button><button disabled=${index === count - 1} onClick=${() => move(1)} aria-label=${'Опустить оператор ' + (index + 1)}>↓</button><button onClick=${remove} aria-label=${'Удалить оператор ' + (index + 1)}>×</button></div></div>
    <div class="lab-operator-params">
      ${operator.op === 'average' ? html`<${NumberField} label="Окно, с" value=${operator.windowS ?? 10} min=${.001} max=${86400} onChange=${v => change({ windowS: v })} />` : ''}
      ${['average', 'derivative', 'threshold'].includes(operator.op) ? html`<${NumberField} label="Макс. разрыв, с" value=${operator.maxGapS ?? 60} min=${.001} max=${86400} onChange=${v => change({ maxGapS: v })} />` : ''}
      ${operator.op === 'threshold' ? html`<label class="lab-number-field"><span>Условие</span><select value=${operator.compare || 'gt'} onChange=${e => change({ compare: e.target.value })}><option value="gt">Больше &gt;</option><option value="gte">Не меньше ≥</option><option value="lt">Меньше &lt;</option><option value="lte">Не больше ≤</option></select></label><${NumberField} label="Значение порога" value=${operator.value ?? 0} onChange=${v => change({ value: v })} /><${NumberField} label="Выдержка, с" value=${operator.holdS ?? 0} min=${0} max=${86400} onChange=${v => change({ holdS: v })} />` : ''}
      ${operator.op === 'difference' ? html`<label class="lab-reference-field"><span>Вычесть сигнал</span><select value=${operator.signalId || ''} onChange=${e => change({ signalId: e.target.value })}><option value="">Выберите совместимый сигнал</option>${signals.filter(s => s.id !== source?.id && s.type === 'scalar').map(s => html`<option value=${s.id}>${s.label} ${s.unit ? '[' + unitLabel(s.unit) + ']' : '[единица неизвестна]'}</option>`)}</select></label><${NumberField} label="Возраст второго отсчёта, с" value=${operator.maxAgeS ?? 2} min=${0} max=${86400} onChange=${v => change({ maxAgeS: v })} />` : ''}
      ${operator.op === 'raw' ? html`<span class="lab-identity-note">Значения и временные метки передаются без изменений.</span>` : ''}
    </div>
    ${node?.error ? html`<div class="lab-inline-error" role="status">${node.error}</div>` : node ? html`<div class="lab-node-lineage"><span>${TYPES[node.inputType] || node.inputType} ${unitLabel(node.inputUnit)}</span><span>→</span><b>${TYPES[node.outputType] || node.outputType} ${unitLabel(node.outputUnit)}</b><small>${node.summary.samples} отсчётов · ${node.summary.missing} пропусков</small></div>` : html`<div class="lab-node-lineage">Ожидает корректного результата предыдущего шага</div>`}
  </article>`;
}

function EvidenceCard({ card, run, onSeek, update, remove }) {
  const sameSource = sourceFingerprint(run).value === card.run.source.value && sourceFingerprint(run).algorithm === card.run.source.algorithm;
  const liveEvent = run.kind === 'live' && String(run.id) === card.run.id && card.eventId && canonical(run.events.find(e => String(e.id) === card.eventId)) === canonical(card.event);
  const matching = (sameSource || liveEvent) && String(run.clock || '') === String(card.run.clock || '');
  return html`<article class="lab-evidence-card" data-testid="lab-evidence-card"><div class="lab-evidence-top"><span class="lab-type-badge">${card.result ? 'Результат' : 'Событие'}</span><time>${finite(card.run.t_start) ? timeLabel(card.cursor, card.run) : 't=' + card.cursor}</time><button class="lab-icon-button" onClick=${remove} aria-label="Удалить карточку">×</button></div>
    <h3>${card.result?.label || card.event?.type || 'Выбранный момент'}</h3><p class="lab-evidence-origin">${card.run.label}<span>${card.run.origin || 'Источник не указан'}</span></p>
    ${card.result ? html`<div class="lab-evidence-value"><b>${valueLabel(card.result.value)}</b><span>${unitLabel(card.result.unit)}</span><small>${card.result.sampleT == null ? 'Нет отсчёта к этому моменту' : 'Отсчёт: ' + card.result.sampleT}</small></div>` : ''}
    <label class="lab-annotation"><span>Наблюдение / вывод</span><textarea placeholder="Что подтверждает это событие? Какие ограничения у вывода?" value=${card.annotation} maxlength="4000" onInput=${e => update({ ...card, annotation: e.target.value })}></textarea><small>${card.annotation.length} / 4000</small></label>
    <div class="lab-evidence-proof"><span>${card.eventId ? 'Событие ' + card.eventId : 'Позиция в журнале'}</span><code title=${card.run.source.scope}>${card.run.source.algorithm}: ${card.run.source.value}</code></div>
    <details><summary>Происхождение и воспроизводимость</summary><dl><dt>Время журнала</dt><dd>${card.cursor}</dd><dt>Область часов</dt><dd>${card.run.clock || 'Не указана'}</dd><dt>Зафиксировано</dt><dd>${card.createdAt}</dd></dl><p>${card.run.source.scope}</p><pre>${JSON.stringify({ event: card.event, recipe: card.recipe, result: card.result, config: card.config }, null, 2)}</pre></details>
    <button class="lab-text-button" disabled=${!matching} onClick=${() => onSeek?.(card.cursor)}>${matching ? 'Перейти к источнику →' : 'Откройте соответствующий прогон для перехода'}</button></article>`;
}

export function LabTools({ run, cursor, event, onSeek, mode }) {
  const discovery = useMemo(() => discoverSignals(run), [run]);
  const signals = discovery.signals, source = useMemo(() => sourceFingerprint(run), [run]);
  const recipeKey = 'edgelab.recipe.' + (run.kind === 'live' ? run.id : source.value);
  const [tab, setTab] = useState('analysis'), [signalId, setSignalId] = useState(''), [operators, setOperators] = useState([]);
  const [untilCursor, setUntilCursor] = useState(false), [newOperator, setNewOperator] = useState('average'), [notice, setNotice] = useState('');
  const [filterCurrent, setFilterCurrent] = useState(false), [deleted, setDeleted] = useState(null);
  const recipeFile = useRef(null), notebookFile = useRef(null), notebook = useNotebook();
  const activeTab = mode || tab;
  useEffect(() => {
    let found = null;
    try { const saved = localStorage.getItem(recipeKey); if (saved) found = importRecipe(saved, signals); } catch (e) { setNotice('Сохранённый рецепт не применён: ' + e.message); }
    setSignalId(found?.signalId || signals.find(s => s.type === 'scalar')?.id || signals[0]?.id || '');
    setOperators(found?.operators || []); setUntilCursor(finite(found?.until));
    if (finite(found?.until)) onSeek?.(found.until);
  }, [recipeKey]);
  const signal = signals.find(s => s.id === signalId) || signals[0];
  const until = untilCursor && finite(cursor) ? cursor : null;
  const analysis = useMemo(() => applyPipeline(signal, operators, signals, { until }), [signal, operators, signals, until]);
  const sourceSamples = signal?.samples.filter(p => until == null || p.t <= until) || [];
  const point = sampleAt(sourceSamples, cursor);
  const persistRecipe = (id, ops, boundary = until) => {
    try { localStorage.setItem(recipeKey, JSON.stringify(exportRecipe(run, signals.find(s => s.id === id), ops, boundary))); }
    catch { setNotice('Рецепт пока хранится в этой вкладке. Его можно экспортировать в JSON.'); }
  };
  const changeOperators = next => { setOperators(next); persistRecipe(signal?.id || '', next); };
  const capture = result => {
    try {
      const card = makeEvidence({ run, cursor, event, signal: result ? signal : null, analysis, operators, until });
      if (notebook.cards.some(c => c.id === card.id)) { setNotice('Это наблюдение уже есть в блокноте.'); return; }
      notebook.update(mergeNotebook(notebook.cards, [card])); setNotice('Наблюдение сохранено в блокнот вместе с источником и рецептом.');
    } catch (e) { setNotice(e.message); }
  };
  const importFile = async (file, kind) => {
    if (!file) return;
    try {
      if (file.size > (kind === 'recipe' ? 128000 : MAX_NOTEBOOK_BYTES / 2)) throw Error('Файл превышает допустимый размер');
      const raw = await file.text();
      if (kind === 'recipe') {
        const recipe = importRecipe(raw, signals); setSignalId(recipe.signalId); setOperators(recipe.operators); setUntilCursor(finite(recipe.until));
        if (finite(recipe.until)) onSeek?.(recipe.until);
        persistRecipe(recipe.signalId, recipe.operators, recipe.until);
        setNotice(recipe.source?.value && recipe.source.value !== source.value ? 'Рецепт импортирован из другого источника. Совместимость операторов проверяется заново.' : 'Рецепт импортирован. Результат пересчитан по текущей записи.');
      } else { notebook.update(mergeNotebook(notebook.cards, validateNotebook(raw))); setNotice('Карточки добавлены. Существующие заметки сохранены.'); }
    } catch (e) { setNotice('Импорт не выполнен: ' + e.message); }
  };
  const visibleCards = filterCurrent ? notebook.cards.filter(c => c.run.id === String(run.id)) : notebook.cards;
  const exportBook = format => {
    if (format === 'json') download(JSON.stringify(notebookDocument(visibleCards), null, 2), 'edgelab-evidence.json', 'application/json');
    else if (format === 'html') download(notebookHTML(visibleCards), 'edgelab-evidence.html', 'text/html;charset=utf-8');
    else download(notebookMarkdown(visibleCards), 'edgelab-evidence.md', 'text/markdown;charset=utf-8');
  };
  return html`<div class="lab-tools" data-testid="lab-tools" data-mode=${activeTab}>
    ${!mode ? html`<div class="lab-tool-tabs"><button aria-pressed=${activeTab === 'analysis'} onClick=${() => setTab('analysis')}>Обработка сигналов</button><button aria-pressed=${activeTab === 'notebook'} onClick=${() => setTab('notebook')}>Блокнот <span>${notebook.cards.length}</span></button></div>` : ''}
    ${notice ? html`<div class="lab-tools-notice" role="status"><span>${notice}</span><button onClick=${() => setNotice('')} aria-label="Скрыть сообщение">×</button></div>` : ''}
    ${notebook.error ? html`<div class="lab-inline-error" role="status">${notebook.error}</div>` : ''}
    ${activeTab === 'analysis' ? html`<header class="lab-tool-header"><div><span class="lab-tool-kicker">ИЗМЕРЕНИЕ / ПРЕОБРАЗОВАНИЕ / ДОКАЗАТЕЛЬСТВО</span><h2>Инструменты исследования</h2><p>Обработайте реальные отсчёты. Сохраните рецепт и наблюдение, к которым можно вернуться.</p></div><span class="lab-type-badge">${signals.length} сигналов из записи</span></header>
      ${signals.length ? html`<section class="lab-source-select"><label><span>Исходный сигнал</span><select data-testid="lab-signal-select" value=${signal?.id || ''} onChange=${e => { setSignalId(e.target.value); persistRecipe(e.target.value, operators); }}>${signals.map(s => html`<option value=${s.id}>${s.label} · ${TYPES[s.type]}${s.unit ? ' · ' + unitLabel(s.unit) : ''} · ${s.samples.length} отсч.</option>`)}</select></label><label class="lab-range-select"><span>Диапазон обработки</span><select value=${untilCursor ? 'cursor' : 'all'} onChange=${e => { const selected = e.target.value === 'cursor'; setUntilCursor(selected); persistRecipe(signal?.id || '', operators, selected ? cursor : null); }}><option value="all">Весь записанный прогон</option><option value="cursor">До выбранного момента</option></select></label></section>
      <div class="lab-signal-contract"><span class="lab-type-badge">${TYPES[signal?.type] || signal?.type}</span><span>${signal?.unitKnown ? 'Единица: ' + unitLabel(signal.unit) : 'Единица не объявлена'}</span><span>Часы: ${signal?.clock || 'не указаны'}</span><span>${signal?.unitEvidence}</span>${signal?.unitConflict ? html`<b class="lab-inline-warning">В источнике меняется единица; разность запрещена</b>` : ''}</div>
      ${discovery.warnings.map(w => html`<div class="lab-inline-warning">${w}</div>`)}
      ${['scalar', 'boolean', 'null'].includes(signal?.type) ? html`<div class="lab-analysis-charts"><${SeriesChart} samples=${sourceSamples} type=${signal?.type} title=${signal?.label} unit=${signal?.unit} run=${run} cursor=${cursor} onSeek=${onSeek} /><${SeriesChart} samples=${analysis.samples} type=${analysis.signal?.type} title=${operators.length ? 'После цепочки обработки' : 'Без преобразований'} unit=${analysis.signal?.unit} run=${run} cursor=${cursor} onSeek=${onSeek} derived=${true} /></div>` : html`<${PayloadView} signal=${signal} sample=${point} />`}
      <div class="lab-analysis-layout"><section class="lab-pipeline"><div class="lab-section-head"><div><span class="lab-tool-kicker">ЦЕПОЧКА ОБРАБОТКИ</span><h3>Каждый шаг виден</h3></div><span>${operators.length} / ${MAX_OPERATORS}</span></div><div class="lab-pipeline-source"><${Icon} name="pulse" size=${18} /><div><b>${signal?.label}</b><small>Исходные события · временные метки сохранены</small></div></div>
        ${operators.map((op, index) => html`<${Operator} key=${op.id} operator=${op} node=${analysis.nodes[index]} index=${index} count=${operators.length} source=${signal} signals=${signals} update=${next => changeOperators(operators.map((o, i) => i === index ? next : o))} remove=${() => changeOperators(operators.filter((_, i) => i !== index))} move=${step => { const next = [...operators]; [next[index], next[index + step]] = [next[index + step], next[index]]; changeOperators(next); }} />`)}
        <div class="lab-add-operator"><select aria-label="Добавить оператор" value=${newOperator} onChange=${e => setNewOperator(e.target.value)}>${Object.entries(OPERATORS).map(([id, title]) => html`<option value=${id}>${title[0]}</option>`)}</select><button class="lab-tool-primary" data-testid="lab-add-operator" disabled=${operators.length >= MAX_OPERATORS} onClick=${() => { const match = signals.find(s => s.id !== signal.id && !differenceCompatibility(analysis.signal || signal, s)); changeOperators([...operators, { id: 'op-' + Date.now().toString(36) + '-' + operators.length, op: newOperator, windowS: 10, maxGapS: 60, value: 0, holdS: 0, compare: 'gt', signalId: match?.id || '', maxAgeS: 2 }]); }}>+ Добавить</button></div>
        <p class="lab-method-note">Среднее считается по наблюдаемым отсчётам, без взвешивания по времени. Производная требует двух разных времён. Выдержка опирается на последовательные наблюдения; пропуск сбрасывает её. Разность не интерполирует второй сигнал.</p>
      </section><aside class="lab-result"><div class="lab-section-head"><div><span class="lab-tool-kicker">РЕЗУЛЬТАТ</span><h3>К выбранному моменту</h3></div><span>${timeLabel(cursor, run)}</span></div>
        ${analysis.errors.length ? html`<div class="lab-inline-error">${analysis.errors.join('; ')}</div>` : ''}<div class="lab-result-value" data-testid="lab-result-value"><b>${valueLabel(sampleAt(analysis.samples, cursor)?.value)}</b><span>${unitLabel(analysis.signal?.unit)}</span></div>
        <dl class="lab-result-stats"><dt>Исходных / полученных отсчётов</dt><dd>${sourceSamples.length} / ${analysis.summary.samples}</dd><dt>Пропусков в результате</dt><dd>${analysis.summary.missing}</dd><dt>Минимум / максимум</dt><dd>${valueLabel(analysis.summary.min)} / ${valueLabel(analysis.summary.max)}</dd><dt>Среднее наблюдений</dt><dd>${valueLabel(analysis.summary.mean)}</dd>${analysis.signal?.type === 'boolean' ? html`<dt>true / false</dt><dd>${analysis.summary.trueCount} / ${analysis.summary.falseCount}</dd>` : ''}</dl>
        <small class="lab-stats-scope">Статистика относится к ${untilCursor ? 'участку до курсора' : 'всему записанному прогону'}. Пропуски не превращаются в нули.</small>
        <button class="lab-tool-primary lab-capture" data-testid="lab-capture-result" disabled=${!!analysis.errors.length || !analysis.samples.length} onClick=${() => capture(true)}><${Icon} name="overview" size=${15} /> Зафиксировать результат</button><div class="lab-recipe-actions"><button class="lab-text-button" onClick=${() => download(JSON.stringify(exportRecipe(run, signal, operators, until), null, 2), 'edgelab-recipe.json', 'application/json')}>Экспорт рецепта ↗</button><button class="lab-text-button" onClick=${() => recipeFile.current?.click()}>Импорт JSON</button></div><input ref=${recipeFile} class="lab-file-input" type="file" accept=".json,application/json" onChange=${e => { importFile(e.target.files?.[0], 'recipe'); e.target.value = ''; }} />
        <div class="lab-result-provenance"><span>ИСТОЧНИК</span><code title=${source.scope}>${source.algorithm}: ${source.value}</code><small>Результат хранится вместе с конфигурацией, исходным событием и параметрами цепочки.</small></div></aside></div>` : html`<div class="lab-tools-empty"><${Icon} name="pulse" size=${32} /><h3>В этой записи нет значений для обработки</h3><p>Нужны события sensor с полем value или числовые поля LLM/tool-журнала. Само выбранное событие можно зафиксировать в блокноте.</p><button class="lab-tool-primary" disabled=${!event && !finite(cursor)} onClick=${() => capture(false)}>Сохранить выбранное событие</button></div>`}
      <div class="lab-tools-footer"><span>Обработка выполняется локально по журналу · команды устройствам не отправляются</span><span>В блокноте ${notebook.cards.length} наблюдений</span></div>` :
      html`<header class="lab-tool-header"><div><span class="lab-tool-kicker">ПОЛЕВОЙ БЛОКНОТ / ВОСПРОИЗВОДИМЫЕ НАБЛЮДЕНИЯ</span><h2>Доказательства эксперимента</h2><p>Фиксируйте события, результаты обработки и собственные выводы со ссылкой на источник.</p></div><button class="lab-tool-primary" data-testid="lab-capture-event" disabled=${!event && !finite(cursor)} onClick=${() => capture(false)}>+ Текущее событие</button></header>
      <div class="lab-notebook-toolbar"><div class="lab-book-filters"><button aria-pressed=${!filterCurrent} onClick=${() => setFilterCurrent(false)}>Все прогоны <span>${notebook.cards.length}</span></button><button aria-pressed=${filterCurrent} onClick=${() => setFilterCurrent(true)}>Текущий прогон</button></div><div class="lab-book-exports"><span>Экспорт ${visibleCards.length}:</span><button disabled=${!visibleCards.length} onClick=${() => exportBook('json')}>JSON</button><button disabled=${!visibleCards.length} onClick=${() => exportBook('html')}>HTML</button><button disabled=${!visibleCards.length} onClick=${() => exportBook('markdown')}>Markdown</button><button onClick=${() => notebookFile.current?.click()}>Импорт</button></div><input ref=${notebookFile} class="lab-file-input" type="file" accept=".json,application/json" data-testid="lab-notebook-import" onChange=${e => { importFile(e.target.files?.[0], 'notebook'); e.target.value = ''; }} /></div>
      <div class="lab-book-status"><span>${notebook.dirty ? 'Сохраняется локально…' : notebook.saved ? 'Сохранено в этом браузере' : 'Локальный блокнот браузера'}</span><span>${notebook.cards.length} / ${MAX_CARDS} карточек · до 2 МиБ · экспорт для переноса</span></div>
      ${deleted ? html`<div class="lab-tools-notice" role="status"><span>Карточка удалена из блокнота.</span><button onClick=${() => { try { notebook.update(mergeNotebook(notebook.cards, [deleted])); setDeleted(null); } catch (e) { setNotice(e.message); } }}>Отменить</button></div>` : ''}
      ${visibleCards.length ? html`<div class="lab-evidence-grid">${visibleCards.map(card => html`<${EvidenceCard} key=${card.id} card=${card} run=${run} onSeek=${onSeek} update=${next => notebook.update(notebook.cards.map(c => c.id === card.id ? next : c))} remove=${() => { setDeleted(card); notebook.update(notebook.cards.filter(c => c.id !== card.id)); }} />`)}</div>` : html`<div class="lab-tools-empty"><${Icon} name="overview" size=${32} /><h3>Первое наблюдение начинает историю</h3><p>Выберите событие на временной шкале и сохраните его сюда. Результат цепочки обработки фиксируется из раздела «Инструменты».</p></div>`}
      <div class="lab-tools-footer"><span>HTML и Markdown автономны: исходные данные, конфигурация, рецепт и заметка в одном документе</span><span>Импорт не исполняет код и не заменяет существующие заметки</span></div>`}
  </div>`;
}
