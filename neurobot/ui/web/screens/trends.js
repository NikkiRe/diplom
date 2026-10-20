// «Агент и тренды» (L4) — ключевая метрика ВКР во времени: вызовы и токены LLM, задержка «жест → команда» с
// целевыми линиями, память PSS и CPU, полоса safety_state, A0 и A1 на одной оси; справа — «до/после» A0 против A1
// по compare.md и реплики агенту. Курсоры графиков синхронны, сглаживания нет. Пульт числа ВКР не производит: это
// запись стенда. Цвет: акцент — только выбор и A1 («наш стек»), цвета тревог для данных не используются.
// В живом режиме справа — юниты (systemctl show шлюза) и doctor по кнопке: «до/после» и реплик там нет.
import { useEffect, useMemo, useState } from 'preact/hooks';
import { html, Info, Sign, inK1, safetySegments } from '../lib/ui.js';
import { command } from '../lib/live.js';
import { Chart, Key } from '../lib/charts.js';
import { Timeline } from '../lib/timeline.js';
import { num, unit, int, dur, nbsp, DASH } from '../lib/fmt.js';

const WINDOWS = [{ id: '15', s: 900, title: '15 мин' }, { id: '60', s: 3600, title: '1 ч' }, { id: 'run', s: null, title: 'прогон' }];

function lastBefore(list, t) {
  let lo = 0, hi = list.length - 1, ans = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (list[m].t <= t) { ans = m; lo = m + 1; } else hi = m - 1; }
  return ans;
}

// цель K1 — на p95 (цвет p95), цель Э2 — на p50 (цвет p50); подпись справа — квантиль и порог
const K1_TARGETS = () => [
  { y: 0.5, label: 'p95 0,5', color: (p) => p.accent },
  { y: 0.7, label: 'p50 0,7', color: (p) => p.base },
];

export function TrendsScreen({ run, runs, tl, T, t0, geo, theme, bundle, meta }) {
  const [win, setWin] = useState('15');
  const live = !!run.live;
  const w = WINDOWS.find((x) => x.id === win);
  const xb = w.s ? T - t0 : run.t_end - t0;
  const xa = w.s ? Math.max(run.t_start - t0, xb - w.s) : run.t_start - t0;
  const xRange = () => [xa, Math.max(xa + 60, xb)];
  const sec = Math.floor(T);
  const key = run.id + ':' + win;
  const rss = (tl.at('mem', T) || {}).metric === 'rss';

  // LLM: вызовы по минутам (столбцы) и токены накопительно; вызовы «на жест» (K4) — отдельным рядом
  const llmData = useMemo(() => {
    const all = tl.until('llm', T);
    const b0 = Math.floor(xa / 60) * 60;
    const xs = [], calls = [], gest = [], tok = [];
    let i = 0, cum = 0;
    for (let b = b0; b <= Math.min(xb, T - t0); b += 60) {
      let c = 0, g = 0;
      while (i < all.length && all[i].t - t0 < b + 60) {
        const m = all[i++];
        cum += (m.prompt_tokens || 0) + (m.completion_tokens || 0);
        if (m.t - t0 >= b) { c++; if (m.kind === 'gesture') g++; }
      }
      xs.push(b + 30); calls.push(c || null); gest.push(g || null); tok.push(cum);
    }
    return [xs, calls, gest, tok];
  }, [key, sec]);
  const llmSpec = useMemo(() => ({
    key: 'llm-' + key, height: 150, yLabel: 'вызовов/мин', yFmt: (v) => num(v, 0),
    y2: { label: 'токенов', fmt: (v) => (v >= 1000 ? num(v / 1000, 0) + 'k' : num(v, 0)) },
    series: [
      { label: 'вызовов в минуту', color: (p) => p.base, paths: 'bars', fill: (p) => p.base, fmt: (v) => num(v, 0) },
      { label: 'на жест (K4)', color: (p) => p.text, paths: 'none', points: 8, hollow: true, fmt: (v) => num(v, 0) },
      { label: 'токенов накопительно', color: (p) => p.accent, width: 2, scale: 'y2', fmt: (v) => num(v, 0) },
    ],
  }), [key]);

  // задержка t0 → /cmd_vel: точки жестов, входящих в K1 (без ранних и без RTF < 0,95 — флаги жеста), скользящие
  // p50 / p95 последних 20 (сообщения k1 сборщика: metrics.latency), цели K1 и Э2
  const k1Data = useMemo(() => {
    const all = tl.gestures(T).filter((g) => g.kind === 'gesture' && g.d_cmd_s != null && g.t_cmd - t0 >= xa - 1);
    const g = all.filter(inK1).sort((a, b) => a.t_cmd - b.t_cmd);
    const k = tl.list('k1');
    const xs = [], on = [], p50 = [], p95 = [];
    for (const m of g) {
      xs.push(m.t_cmd - t0);
      on.push(m.d_cmd_s);
      const j = lastBefore(k, m.t_cmd);
      p50.push(j >= 0 ? k[j].last.p50 ?? null : null);
      p95.push(j >= 0 ? k[j].last.p95 ?? null : null);
    }
    const out = [xs, on, p50, p95];
    out.excluded = all.length - g.length;
    return out;
  }, [key, sec]);
  const k1Spec = useMemo(() => ({
    key: 'k1-' + key, height: 190, yLabel: 'с', yRange: (u, lo, hi) => [0, Math.min(3, Math.max(1.2, (hi || 0) * 1.1))],
    series: [
      { label: 'жест', color: (p) => p.muted, paths: 'none', points: 5, fmt: (v) => unit(v, 2, 'с') },
      { label: 'p50', color: (p) => p.base, paths: 'step', width: 2, fmt: (v) => unit(v, 2, 'с') },
      { label: 'p95', color: (p) => p.accent, paths: 'step', width: 2, fmt: (v) => unit(v, 2, 'с') },
    ],
    targets: K1_TARGETS,
  }), [key]);

  // память PSS (без драйверов двойника) и отдельной полосой — CPU
  const memData = useMemo(() => {
    const ms = tl.until('mem', T).filter((m) => m.t - t0 >= xa - 10);
    return [ms.map((m) => m.t - t0), ms.map((m) => m.total_mib)];
  }, [key, sec]);
  const cpuData = useMemo(() => {
    const ms = tl.until('mem', T).filter((m) => m.t - t0 >= xa - 10);
    return [ms.map((m) => m.t - t0), ms.map((m) => (m.cpu_pct != null ? m.cpu_pct : null))];
  }, [key, sec]);
  const memSpec = useMemo(() => ({
    key: 'mem-' + key, height: 116, yLabel: 'МиБ', yFmt: (v) => num(v, 0), xAxis: false, ySpace: 24,
    yRange: (u, lo, hi) => [Math.max(0, Math.floor(((lo || 0) * 0.9) / 50) * 50), Math.ceil(((hi || 100) * 1.08) / 50) * 50],
    series: [{ label: rss ? 'RSS' : 'PSS', color: (p) => p.accent, width: 2, fmt: (v) => unit(v, 0, 'МиБ') }],
  }), [key, rss]);
  const cpuSpec = useMemo(() => ({
    key: 'cpu-' + key, height: 92, yLabel: '%', yFmt: (v) => num(v, 0), ySpace: 16,
    yRange: (u, lo, hi) => [0, Math.max(40, Math.ceil(((hi || 0) * 1.1) / 20) * 20)],
    series: [{ label: 'CPU', color: (p) => p.base, fmt: (v) => unit(v, 0, '%') }],
  }), [key]);

  // полоса состояний — отдельной строкой под графиками
  const bandData = useMemo(() => [[xa, xb], [null, null]], [key, sec]);
  const bandSpec = useMemo(() => ({
    key: 'band-' + key, height: 26, axes: false, bandFull: true, padding: [3, 0, 3, 0],
    series: [{ label: 'safety_state', color: (p) => p.line }],
    band: (a, b) => (run.has_safety ? safetySegments(tl.list('safety'), T, a, b, t0) : []),
  }), [key, sec]);

  const stats = tl.at('stats', T);
  const k1Run = tl.at('k1', T);
  const llm = stats ? stats.llm : null;
  const metrics = (run.summary && run.summary.metrics) || {};
  const mem = (metrics.K8 && metrics.K8.memory && metrics.K8.memory._total) || null;
  const winTitle = (x) => (live && x.id === 'run' ? 'с запуска' : x.title);
  return html`<div class="grid trends-screen">
    <div class="grid tr">
    <div class="grid trl">
      <div class="toolbar" data-testid="trend-controls"><span class="toolbar-label">Период анализа</span>
        <div class="seg">${WINDOWS.map((x) => html`<button class="segbtn" aria-pressed=${win === x.id ? 'true' : 'false'} onClick=${() => setWin(x.id)}>${winTitle(x)}</button>`)}</div>
        <${Info} text=${live ? 'Ось — время от запуска шлюза (T). Подробные данные браузер держит час, буфер шлюза — 15 мин: после переподключения окно длиннее 15 мин начинается заново. Наведите курсор на график — значения в подсказке.' : 'Ось — время от начала расписания (T). У A0 и A1 одно расписание жестов и отказов, поэтому их можно сравнивать по T. Наведите курсор на график — значения в подсказке.'} /></div>
      ${live ? html`<section class="panel" data-testid="chart-llm"><h2><span>Работа языковой модели</span><${Info} text="В живом режиме вызовы LLM пульт не читает: EdgeCtx к роботу ещё не подключён (Э3), у PicoClaw своего счёта нет. Числа ВКР по LLM — в «Записи»." /></h2>
        <div class="detail-empty llm-unavailable"><span class="empty-code">LLM / ИНТЕГРАЦИЯ</span><strong>Данные агента не подключены</strong><span>Вызовы модели и токены доступны в режиме «Запись». В живом потоке эти показатели пока не собираются.</span></div></section>` : html`<section class="panel" data-testid="chart-llm">
        <h2><span>Работа языковой модели</span><${Info} text="Вызовы LLM этой системы: на реплики человека, служебные вызовы хода и вне реплик — «на жест» (K4, цель 0). Токены — prompt + completion накопительно с начала прогона." />
          <span class="r num">за прогон: реплики ${int(llm ? llm.request : null)} · служебные ${int(llm ? llm.service : null)} · на жест ${int(llm ? llm.gesture : null)} · ₽ оценка ${num(llm ? llm.rub_est : null, 2)}</span></h2>
        <div class="legend"><${Key} kind="bar" color="base">вызовов в минуту<//><${Key} kind="sq">на жест (K4)<//><${Key} color="accent">токенов накопительно (правая ось)<//></div>
        <${Chart} spec=${llmSpec} data=${llmData} theme=${theme} xRange=${xRange} /></section>`}
      <section class="panel" data-testid="chart-k1">
        <h2><span>Задержка «жест → команда»</span><${Info} text=${live ? 'Конец жеста по perception (t_end) → первая ненулевая /cmd_vel: истины t0 на роботе нет, это не K1 двойника, а его ориентир; меряется, только если в t_end робот стоял. Ступени — p50 и p95 последних 20 жестов. Линии — цели стенда K1 p95 ≤ 0,5 с и Э2 p50 ≤ 0,7 с. Шкала до 3 с.' : 't0 → первая ненулевая /cmd_vel; t0 — истина генератора жестов двойника. Точки — жесты, входящие в K1: без ранних (реакция раньше t0) и без жестов при RTF < 0,95, как metrics.latency. Ступени — p50 и p95 последних 20 жестов. Цели: K1 p95 ≤ 0,5 с (стенд), Э2 p50 ≤ 0,7 с. Шкала до 3 с.'} />
          <span class="r num">${!live ? 'за прогон' : k1Run && k1Run.run_window && stats && stats.k1 && stats.k1.n >= k1Run.run_window ? `последних ${int(k1Run.run_window)}` : 'с запуска'} p50 / p95: ${stats && stats.k1 && stats.k1.n ? `${num(stats.k1.p50, 2)} / ${num(stats.k1.p95, 2)} с (n=${stats.k1.n})` : DASH}</span></h2>
        <div class="legend"><${Key} kind="dot" color="muted">жест<//><${Key} color="base">p50, 20 жестов<//><${Key} color="accent">p95, 20 жестов<//><${Key} kind="dash" color="accent">цель K1: p95 ≤ 0,5<//><${Key} kind="dash" color="base">цель Э2: p50 ≤ 0,7<//>
          <span class="muted num">не в K1 в окне: ${int(k1Data.excluded)}</span></div>
        <${Chart} spec=${k1Spec} data=${k1Data} theme=${theme} xRange=${xRange} /></section>
      <section class="panel" data-testid="chart-mem">
        <h2><span>Память и CPU стека</span><${Info} text=${`${rss ? 'RSS процессов юнитов стека из /proc шлюза, раз в 10 с (PSS чужого пользователя без прав не читается; RSS считает общие библиотеки в каждом процессе — верхняя оценка)' : 'PSS компонентов стека без драйверов двойника (сэмплер раз в 10 с)'}. Бюджет стека с GNOME — ${num(geo.budget_mib.gnome, 0)} МиБ, headless — ${num(geo.budget_mib.headless, 0)} МиБ. CPU — сумма по процессам стека, % одного ядра.${live ? '' : ' Наклон за прогон — metrics.memory, первые 2 мин не в счёте.'}`} />
          ${live ? '' : html`<span class="r num">за прогон ${mem ? `${num(mem.pss_mib_start, 0)} → ${unit(mem.pss_mib_end, 0, 'МиБ')}, ${unit(mem.slope_mib_h, 1, 'МиБ/ч')}` : DASH}</span>`}</h2>
        <div class="legend"><${Key} color="accent">${rss ? 'RSS' : 'PSS'}, МиБ<//><${Key} color="base">CPU, % ядра<//></div>
        <${Chart} spec=${memSpec} data=${memData} theme=${theme} xRange=${xRange} />
        <${Chart} spec=${cpuSpec} data=${cpuData} theme=${theme} xRange=${xRange} /></section>
      <section class="panel" data-testid="chart-band">
        <h2><span>История состояний защиты</span><${Info} text="Серое — норма (slow с lidar_blind на роботе штатно), цвет и высота — уровень тревоги (P1 — во всю высоту, P2 — на две трети), штриховка — нет данных." />${run.has_safety ? '' : html`<span class="r muted">у A0 safety_gate нет</span>`}</h2>
        <${Chart} spec=${bandSpec} data=${bandData} theme=${theme} xRange=${xRange} /></section>
      ${live ? '' : html`<${Overlay} runs=${runs} run=${run} T=${T} t0=${t0} theme=${theme} />`}
    </div>
    <div class="grid trr">
      ${live ? html`<${Units} tl=${tl} T=${T} /><${Doctor} meta=${meta} />` : html`<${Compare} bundle=${bundle} run=${run} runs=${runs} />
      <${Agent} tl=${tl} T=${T} t0=${t0} metrics=${metrics} />`}
    </div>
    </div>
  </div>`;
}

const ACTIVE = { active: 'работает', activating: 'запускается', deactivating: 'останавливается', failed: 'отказ', inactive: 'не запущен' };

// юниты стека по systemctl show шлюза (без root): состояние и рестарты; не работает — знак P2
function Units({ tl, T }) {
  const u = tl.at('units', T);
  const list = (u && u.list) || [];
  return html`<section class="panel" data-testid="units">
    <h2><span>Компоненты системы</span><${Info} text="systemctl show neurobot-* раз в 5 с (шлюз, без root). Рестарты — NRestarts с запуска юнита. Сам пульт в списке не числится: робот работает и без него." />
      <span class="r num">${u ? `${int(u.up)} из ${int(u.total)} · ${dur(T - u.t)} назад` : DASH}</span></h2>
    <div class="table-wrap" role="region" aria-label="Таблица показателей" tabindex="0"><table><thead><tr><th></th><th>компонент</th><th>состояние</th><th class="n">рестартов</th></tr></thead>
      <tbody>${list.map((r) => html`<tr><td>${r.active === 'active' ? '' : html`<${Sign} prio=${2} />`}</td><td>${r.comp}</td>
        <td class=${r.active === 'active' ? 'muted' : ''}>${ACTIVE[r.active] || r.active}${r.sub && r.sub !== 'running' ? ` · ${r.sub}` : ''}</td>
        <td class="n num">${int(r.restarts)}</td></tr>`)}</tbody></table></div>
    ${list.length === 0 ? html`<div class="empty">нет данных</div>` : ''}
  </section>`;
}

const LEVEL = { ok: 'ОК', warn: 'ВНИМАНИЕ', fail: 'ОШИБКА', starting: 'ЗАПУСК' };

// doctor — по кнопке и не чаще раза в N с (шлюз хранит последний итог с возрастом); сам пульт его не запускает
function Doctor({ meta }) {
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { fetch('api/doctor', { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null)).then(setRes, () => {}); }, []);
  const off = !meta || !meta.doctor_s;
  const run = async () => {
    setBusy(true);
    setRes(await command('api/doctor', meta.csrf, {}, { timeoutMs: 0, stamp: false }));   // doctor — до минуты
    setBusy(false);
  };
  const rep = res && res.report;
  const shown = rep ? rep.findings.filter((f) => f.level !== 'ok') : [];
  return html`<section class="panel" data-testid="doctor">
    <h2><span>Диагностика системы</span><${Info} text=${`neurobot doctor --json на роботе — по кнопке, не чаще раза в ${int(meta ? meta.doctor_s : 0)} с; пульт сам его не запускает (во время замеров выключен: NEUROBOT_UI_DOCTOR_S=0). Без группы systemd-journal журнал PicoClaw doctor не видит — это предупреждение, не отказ.`} />
      <span class="r"><button class="segbtn" data-testid="doctor-run" disabled=${off || busy} onClick=${run}>${busy ? 'Проверяем…' : 'Запустить проверку'}</button></span></h2>
    ${off ? html`<div class="detail-empty compact"><strong>Диагностика выключена</strong><span>Проверки приостановлены на время замеров.</span></div>`
      : !res || !res.t ? html`<div class="detail-empty compact"><strong>Готова к проверке</strong><span>Проверьте компоненты и получите описание найденных проблем.</span></div>`
        : html`<div class="sub num">${res.cached ? 'прежний итог: ' : ''}${dur(res.age_s)} назад · шёл ${unit(res.took_s, 1, 'с')}${res.next_in_s ? ` · снова через ${dur(res.next_in_s)}` : ''}</div>
          ${rep ? html`<div class=${'mid verdict-' + (rep.root ? rep.root.level : 'ok')} data-testid="doctor-verdict">${rep.root ? `${LEVEL[rep.root.level] || rep.root.level} — ${rep.root.component}` : 'Все проверки пройдены'}</div>
            ${rep.root ? html`<div class="sub">${rep.root.text}</div><div class="sub">${rep.root.hint || ''}</div>` : ''}
            ${shown.length ? html`<div class="table-wrap" role="region" aria-label="Результаты диагностики" tabindex="0"><table><tbody>${shown.map((f) => html`<tr><td>${f.component}</td><td>${LEVEL[f.level] || f.level}</td><td class="muted">${f.text}</td></tr>`)}</tbody></table></div>` : ''}`
            : html`<div class="sub bad">${res.error || 'doctor не ответил'}</div>`}`}
  </section>`;
}

function Agent({ tl, T, t0, metrics }) {
  const reqs = tl.until('agent', T).slice(-8).reverse();
  const a = (metrics.K4 && metrics.K4.agent) || {};
  const kind = { status: 'состояние', move: 'вперёд' };
  const outcome = { moved: 'поехал', refused: 'сообщил о запрете', false_ok: '«готово» без движения', unconfirmed: 'не подтверждено', no_answer: 'без ответа' };
  return html`<section class="panel" data-testid="agent">
    <h2><span>Диалог с агентом</span><${Info} text="PicoClaw — канал pico, LLM — mockllm (оракул, 0 ₽). Пробуждения по событиям PicoClaw не пишет. «LLM на жест» — вызовы вне реплик (K4)." />
      <span class="r num">за прогон: ${a.ok ?? DASH} из ${a.requests ?? DASH} · LLM на реплику ${num((a.llm_calls_per_request || {}).p50, 0)}</span></h2>
    <div class="table-wrap" role="region" aria-label="Таблица показателей" tabindex="0"><table><thead><tr><th>T</th><th>реплика</th><th>ответ</th><th class="n">с</th><th class="n">LLM</th><th>итог</th></tr></thead>
      <tbody>${reqs.map((m) => html`<tr><td class="num muted">T${dur(m.t - t0, true)}</td><td>#${m.n} ${kind[m.request] || m.request}</td>
        <td>${m.ok ? 'да' : html`<b>нет</b>`}</td><td class="n num">${num(m.latency_s, 2)}</td><td class="n num">${int(m.llm_calls)}</td>
        <td class="muted">${m.outcome ? outcome[m.outcome] || m.outcome : ''}</td></tr>`)}</tbody></table></div>
    ${reqs.length === 0 ? html`<div class="detail-empty compact"><strong>Вопросов агенту пока нет</strong><span>Реплики, задержка ответа и результат появятся здесь.</span></div>` : ''}
  </section>`;
}

function shortLabel(r) { return `${r.system.toUpperCase()} ${r.run} · ${r.date}`; }

// одно расписание жестов и отказов — сравнение по оси T имеет смысл
function sameSchedule(a, b) { return !!(a && b && a.schedule_sha && a.schedule_sha === b.schedule_sha); }

function pick(r) {
  const m = (r.summary && r.summary.metrics) || {};
  return {
    k1: num((m.K1_t0_to_cmd_s || {}).p95, 2), touch: int((m.touches || {}).episodes),
    k4: int((m.K4 || {}).llm_calls_outside_requests), k7: int((m.K7 || {}).total),
  };
}

// A0 и выбранный прогон A1 (на A0 — первый A1 бандла): главные числа «до → после» и ячейки compare.md
function Compare({ bundle, run, runs }) {
  const a0 = runs.find((r) => r.system === 'a0');
  const a1 = run.system === 'a1' ? run : runs.find((r) => r.system === 'a1');
  const cols = [a0, a1].filter(Boolean);
  const kp = a0 && a1 ? [pick(a0), pick(a1)] : null;
  const KPI = [['k1', 'K1 p95', 'с'], ['touch', 'касания', ''], ['k4', 'LLM на жест', ''], ['k7', 'нежелат. действия', '']];
  return html`<section class="panel" data-testid="compare">
    <h2><span>Сравнение стеков</span><span class="panel-period">A0 → A1</span><${Info} text="Ячейки — как в compare.md: те же функции metrics.py и форматтеры compare.py. Полный текст ячейки — в подсказке. Стенд в общей ВМ: ориентир, не результат робота; результат — счётчики (касания, вмешательства человека, вызовы LLM)." /></h2>
    ${kp ? html`<div class="kpis" data-testid="kpis">${KPI.map(([k, title, u]) => html`<div class="kpi"><div class="sub">${title}${u ? ', ' + u : ''}</div>
      <div class="mid num"><span class="muted">${kp[0][k]}</span><span class="kpi-arrow">→</span><b>${kp[1][k]}</b></div></div>`)}</div>` : ''}
    <div class="table-wrap" role="region" aria-label="Сравнение стеков A0 и A1" tabindex="0"><table class="cmp"><thead><tr><th>метрика</th>${cols.map((r) => html`<th class=${r.system === 'a0' ? 'a0' : 'cur'}>${shortLabel(r)}</th>`)}<th>цель</th></tr></thead>
      <tbody>${(bundle.compare_rows || []).map((row, i) => html`<tr><td class="muted" title=${row.name}>${row.short || row.name}</td>
        ${cols.map((r) => {
          const value = r.summary?.compare?.[i] ?? DASH;
          return html`<td class=${r.system === 'a0' ? 'a0' : 'cur'} title=${value}><span class="clip">${nbsp(value)}</span></td>`;
        })}
        <td class="muted" title=${row.goal}><span class="clip">${nbsp(row.goal)}</span></td></tr>`)}</tbody></table></div>
    ${!a0 || !a1 ? html`<div class="detail-empty compact"><strong>Нет пары для сравнения</strong><span>Нужны записи базового стека A0 и нового стека A1.</span></div>` : sameSchedule(a0, a1) ? '' : html`<div class="warn">Расписания разные (${a0.schedule || DASH} против ${a1.schedule || DASH}): сравнение по строкам — только ориентир, не мера улучшения.</div>`}
    ${cols.some((r) => r.world === 'lab') ? html`<div class="warn">Мир lab (комната 5 × 5 м) с залом сравнивается только как ориентир.</div>` : ''}
  </section>`;
}

// A0 и A1 на одной оси расписания: задержка каждого жеста до текущего T
function Overlay({ runs, run, T, t0, theme }) {
  const a0 = runs.find((r) => r.system === 'a0');
  const a1 = run.system === 'a1' ? run : runs.find((r) => r.system === 'a1');
  const tls = useMemo(() => (a0 && a1 ? [new Timeline(a0.stream), new Timeline(a1.stream)] : null), [a0, a1]);
  const rel = T - t0;
  const sec = Math.floor(T);
  const data = useMemo(() => {
    if (!tls) return [[], [], []];
    const pts = [];
    tls.forEach((tl, i) => {
      const r = i === 0 ? a0 : a1;
      for (const g of tl.gestures(r.t_end)) {
        if (!inK1(g) || g.t_cmd - r.anchor_t > rel) continue;
        pts.push([g.t_cmd - r.anchor_t, i, g.d_cmd_s]);
      }
    });
    pts.sort((a, b) => a[0] - b[0]);
    return [pts.map((p) => p[0]), pts.map((p) => (p[1] === 0 ? p[2] : null)), pts.map((p) => (p[1] === 1 ? p[2] : null))];
  }, [tls, sec]);
  const spec = useMemo(() => ({
    key: 'ov-' + (a1 ? a1.id : ''), height: 176, yLabel: 'с',
    yRange: (u, lo, hi) => [0, Math.min(3, Math.max(1.2, (hi || 0) * 1.05))],
    series: [
      { label: a0 ? `A0 ${a0.run}` : 'A0', color: (p) => p.base, paths: 'none', points: 6, fmt: (v) => unit(v, 2, 'с') },
      { label: a1 ? `A1 ${a1.run}` : 'A1', color: (p) => p.accent, paths: 'none', points: 6, fmt: (v) => unit(v, 2, 'с') },
    ],
    targets: () => [{ y: 0.5, label: 'K1 0,5', color: (p) => p.text }],
  }), [a0, a1]);
  if (!a0 || !a1) return html`<section class="panel"><h2><span>A0 и A1 на одной оси</span></h2><div class="detail-empty compact"><strong>Нет пары для сравнения</strong><span>Нужны записи базового стека A0 и нового стека A1.</span></div></section>`;
  const end = Math.max(a0.t_end - a0.anchor_t, a1.t_end - a1.anchor_t);
  return html`<section class="panel" data-testid="overlay">
    <h2><span>Сравнение времени реакции</span><span class="panel-period">A0 / A1</span><${Info} text="t0 → первая ненулевая /cmd_vel, только жесты, входящие в K1. Ось — время от начала расписания, общее у A0 и A1." /></h2>
    <div class="legend"><${Key} kind="dot" color="base">A0 ${a0.run}<//><${Key} kind="dot" color="accent">A1 ${a1.run}<//><${Key} kind="dash">цель K1<//></div>
    ${sameSchedule(a0, a1) ? '' : html`<div class="warn">Расписания разные (${a0.schedule} и ${a1.schedule}): точки на одной оси не соответствуют одним жестам.</div>`}
    <${Chart} spec=${spec} data=${data} theme=${theme} sync="ov" xRange=${() => [0, end]} />
  </section>`;
}
