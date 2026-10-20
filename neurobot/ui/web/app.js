// Пульт оператора NeuroChip (docs/ui/PLAN.md §0а, §4): «Запись» — проигрыватель прогонов на двойнике (П1), «Живой» —
// поток шлюза робота или двойника (П2). Каркас один: строка состояния (источник на каждом экране), навигация 1–3 и
// лента тревог, рабочая область; внизу — проигрыватель записи или строка живого потока. Пульт — «окно, а не руль»:
// команд две — «СТОП движения» и «Поставить защёлку» (только у живого шлюза); снять защёлку из пульта нельзя.
import { render } from 'preact';
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { html, Sign, palette, safetySegments, drawBand, stateWord } from './lib/ui.js';
import { Timeline } from './lib/timeline.js';
import { LiveSource, command, setMetaClock } from './lib/live.js';
import { dur, clock, num, unit, age as fmtAge, int, DASH } from './lib/fmt.js';
import { Overview } from './screens/overview.js';
import { SafetyScreen } from './screens/safety.js';
import { TrendsScreen } from './screens/trends.js';
import { Icon } from './lib/icons.js';
import { Equipment } from './screens/equipment.js';
import { LabScreen } from './screens/lab.js';
import { ConnectScreen, GlassToggle } from './screens/connect.js';
import { AssistantScreen } from './screens/assistant.js';

const SCREENS = [
  { id: 'connect', key: '6', title: 'Подключения', desc: 'Устройство, его возможности и связь с рабочим пространством.', C: ConnectScreen },
  { id: 'assistant', key: '7', title: 'Ассистент', desc: 'Модель, диалог и общая память агента.', C: AssistantScreen },
  { id: 'lab', key: '5', title: 'Лаборатория агента', desc: 'Наблюдать. Понять решение. Проверить другую гипотезу.', C: LabScreen },
  { id: 'equipment', key: '4', title: 'Атлас сигналов', desc: 'Оборудование, контекст и каждый сигнал между ними.', C: Equipment },
  { id: 'overview', key: '1', title: 'Обзор', desc: 'Вся система. В одном поле зрения.', C: Overview },
  { id: 'safety', key: '2', title: 'Безопасность и жесты', desc: 'Контроль движения, защитные ограничения и распознавание.', C: SafetyScreen },
  { id: 'trends', key: '3', title: 'Агент и тренды', desc: 'Ресурсы, задержки и работа ИИ-агента во времени.', C: TrendsScreen },
];
const SPEEDS = [1, 10, 60];
const TICK_MS = 100;
const LIVE_TICK_MS = 250;
const LINK_CHECK_MS = 100;  // связь проверяется чаще, чем перерисовка: «нет связи» — не позже 1,6 с тишины
const NO_DATA_S = 2;        // запись: данных нет дольше — рабочая область в штриховке
const NO_LINK_S = 1.5;      // живой режим: hb раз в 0,5 с — тишина дольше, и «нет связи» (PLAN §7 U8: ≤ 2 с)
const META_CLOCK_MS = 30000; // часы робота по /api/meta, пока живого потока нет («Запись»): для issued_at команд
const ALARM_WIN_S = 600;    // лента тревог: стоящие и снятые за 10 мин
const CLEARED_SHOWN = 5;    // снятых показываем столько, остальные — «ещё N»
const RESULT_MS = 8000;     // итог команды под кнопкой

function params() {
  const q = new URLSearchParams(location.search);
  const hash = location.hash.replace(/^#\/?/, '');
  return {
    run: q.get('run'), t: q.has('t') ? Number(q.get('t')) : null, speed: Number(q.get('speed')) || 10,
    play: q.get('play') === '1', screen: SCREENS.some((s) => s.id === hash) ? hash : (q.get('screen') || 'connect'),
    theme: q.get('theme'), mode: q.get('mode'), demo: q.get('demo') === '1',
  };
}

function storedTheme() {
  try { return localStorage.getItem('nb-ui-theme'); } catch (e) { return null; }
}

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === 'light' || mode === 'dark') root.setAttribute('data-theme', mode); else root.removeAttribute('data-theme');
  try { mode ? localStorage.setItem('nb-ui-theme', mode) : localStorage.removeItem('nb-ui-theme'); } catch (e) { /* без хранилища — только на сеанс */ }
}

// тема: из ?theme=, сохранённой или системной; key — чтобы холсты перерисовались
function useTheme(p0) {
  const [themeMode, setThemeMode] = useState(() => p0.theme || storedTheme() || 'dark');
  const [themeKey, setThemeKey] = useState(0);
  useEffect(() => { applyTheme(themeMode === 'auto' ? null : themeMode); setThemeKey((k) => k + 1); }, [themeMode]);
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const on = () => setThemeKey((k) => k + 1);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);
  return { themeMode, setThemeMode, themeKey };
}

// экран — в адресе (#/safety), клавиши 1–3 и Esc
function useScreen(p0) {
  const [screen, setScreen] = useState(p0.screen);
  useEffect(() => { if (location.hash !== '#/' + screen) history.replaceState(null, '', location.pathname + location.search + '#/' + screen); }, [screen]);
  useEffect(() => { document.querySelector('.nav a[aria-current="page"]')?.scrollIntoView({block:'nearest',inline:'nearest'}); }, [screen]);
  useEffect(() => {
    const onHash = () => { const id = location.hash.replace(/^#\/?/, ''); if (SCREENS.some((s) => s.id === id)) setScreen(id); };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  return [screen, setScreen];
}

function typing(e) {
  const tag = e.target && e.target.tagName;
  return (e.target && e.target.closest && e.target.closest('dialog[open]')) || tag === 'SELECT' || tag === 'TEXTAREA' || (tag === 'INPUT' && e.target.type !== 'range') || e.ctrlKey || e.metaKey || e.altKey;
}

function screenKey(e, go) {
  const s = SCREENS.find((x) => x.key === e.key);
  if (s) { go(s.id); e.preventDefault(); return true; }
  if (e.key === 'Escape') { go('overview'); return true; }
  return false;
}

// кнопка, нажатая мышью, не держит фокус: иначе пробел снова нажмёт её, а не поставит паузу
function useBlurOnClick() {
  useEffect(() => {
    const onClick = (e) => { if (e.detail > 0) { const b = e.target.closest && e.target.closest('button'); if (b) b.blur(); } };
    document.addEventListener('click', onClick, true);
    return () => document.removeEventListener('click', onClick, true);
  }, []);
}

// ---- режим: «Живой» (шлюз) или «Запись» (бандл); переключатель — если шлюз раздаёт и бандл

function Root({ meta }) {
  const p0 = useMemo(params, []);
  const modes = meta.modes || [meta.mode];
  const [mode, setMode] = useState(modes.includes(p0.mode) ? p0.mode : modes[0]);
  const [bundle, setBundle] = useState(null);
  const [err, setErr] = useState(null);
  const theme = useTheme(p0);
  useEffect(() => {
    if (mode !== 'replay' || bundle) return;
    fetch('api/bundle', { cache: 'no-store' })
      .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then((b) => { if (b.format !== 'neurobot-ui-bundle/1') throw new Error('не бандл пульта: ' + b.format); setBundle(b); })
      .catch((e) => setErr(e.message));
  }, [mode]);
  const pick = (m) => {
    const q = new URLSearchParams(location.search);
    q.set('mode', m);
    history.replaceState(null, '', location.pathname + '?' + q.toString() + location.hash);
    setMode(m);
  };
  const common = { meta, modes, mode, setMode: pick, p0, theme };
  if (mode === 'catalog') return html`<${CatalogApp} ...${common} />`;
  if (mode === 'live') return html`<${LiveApp} ...${common} />`;
  if (!bundle) return html`<p class="loading">${err ? 'Запись не загрузилась: ' + err : 'Загрузка записи…'}</p>`;
  return html`<${ReplayApp} ...${common} bundle=${bundle} />`;
}

// Standalone MCP observatory: no simulated robot and no recording are required.
function CatalogApp({ meta, p0, theme }) {
  const tl = useMemo(() => new Timeline(), []);
  const [T, setT] = useState(() => Date.now() / 1000);
  const [acked, setAcked] = useState(() => new Set());
  const started = useRef(T);
  const [screen, go] = useScreen({ ...p0, screen: ['connect', 'assistant', 'lab', 'equipment'].includes(p0.screen) ? p0.screen : 'connect' });
  useBlurOnClick();
  useEffect(() => {
    const onKey = e => { if (!typing(e) && ['4', '5', '6', '7'].includes(e.key)) screenKey(e, go); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  useEffect(() => { const id = setInterval(() => setT(Date.now() / 1000), 1000); return () => clearInterval(id); }, []);
  const run = { catalog: true, demo: p0.demo, id: 'catalog', system: 'mcp', run: 'MCP', origin: 'MCP', has_safety: false, date: '' };
  const ctx = { meta, run, tl, T, t0: started.current, live: null, theme: theme.themeKey };
  return html`<${Frame} ctx=${ctx} screen=${screen} go=${go} acked=${acked} setAcked=${setAcked} theme=${theme} silence=${0}
    footer=${html`<footer class="player livebar"><span class="badge live">LOCAL</span><span>EdgeLab · рабочее пространство</span><span class="muted">Устройства · агент · контекст · эксперименты</span><span class="sp"></span><span class="num muted">${clock(T)}</span></footer>`} />`;
}

function ModeSwitch({ modes, mode, setMode }) {
  if (!modes || modes.length < 2) return '';
  const name = { live: 'Живой', replay: 'Запись' };
  return html`<div class="seg" role="group" aria-label="режим пульта" data-testid="mode-switch">
    ${modes.map((m) => html`<button class="segbtn" aria-pressed=${mode === m ? 'true' : 'false'} onClick=${() => setMode(m)}>${name[m] || m}</button>`)}
  </div>`;
}

// ---- «Запись»

function startT(run, p) {
  return clampT(run, p.t != null && Number.isFinite(p.t) ? run.anchor_t + p.t : run.t_end);
}

function ReplayApp({ bundle, meta, modes, mode, setMode, p0, theme }) {
  const runs = bundle.runs;
  const firstA1 = Math.max(0, runs.findIndex((r) => r.system === 'a1'));
  const [runIdx, setRunIdx] = useState(() => {
    const i = runs.findIndex((r) => r.id === p0.run);
    return i >= 0 ? i : firstA1;
  });
  const run = runs[runIdx];
  const tl = useMemo(() => new Timeline(run.stream), [run]);
  const t0 = run.anchor_t;
  const [T, setT] = useState(() => startT(run, p0));
  const [playing, setPlaying] = useState(p0.play);
  const [speed, setSpeed] = useState(SPEEDS.includes(p0.speed) ? p0.speed : 10);
  const [screen, go] = useScreen(p0);
  // квитирование — по случаю тревоги (ключ и момент, когда встала): вставшая снова мигает заново; у прогона — своё
  const [acked, setAcked] = useState(() => new Set());
  useEffect(() => setAcked(new Set()), [runIdx]);
  useBlurOnClick();

  // ход времени: шаг 100 мс × скорость; конец записи — пауза
  const live = useRef({ playing, speed });
  live.current = { playing, speed };
  useEffect(() => {
    let last = performance.now();
    const id = setInterval(() => {
      const now = performance.now();
      const dt = (now - last) / 1000;
      last = now;
      if (!live.current.playing) return;
      setT((t) => {
        const nt = t + dt * live.current.speed;
        if (nt >= run.t_end) { setPlaying(false); return run.t_end; }
        return nt;
      });
    }, TICK_MS);
    return () => clearInterval(id);
  }, [run]);

  const togglePlay = () => {
    setT((t) => (t >= run.t_end ? run.t_start : t));
    setPlaying((x) => !x);
  };
  useEffect(() => {
    const onKey = (e) => {
      if (typing(e) || screenKey(e, go)) return;
      if (document.querySelector('main[data-screen="lab"]') && [' ', 'ArrowLeft', 'ArrowRight'].includes(e.key)) return;
      const tag = e.target && e.target.tagName;
      if (e.key === ' ') {
        if (tag === 'BUTTON') return;            // кнопка в фокусе с клавиатуры — её собственное нажатие
        togglePlay(); e.preventDefault(); return;
      }
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        const step = (e.shiftKey ? 60 : 10) * (e.key === 'ArrowLeft' ? -1 : 1);
        setT((t) => clampT(run, t + step)); e.preventDefault();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [run]);

  const pickRun = (i) => {
    const rel = T - t0;
    setRunIdx(i);
    setT(clampT(runs[i], runs[i].anchor_t + rel));
  };

  const ctx = { bundle, run, runs, tl, T, t0, geo: bundle.geometry, theme: theme.themeKey, meta, live: null };
  // данных нет дольше 2 с — рабочая область в штриховке: показанное больше не правда (PLAN §4)
  const silence = T - lastData(tl, T);
  return html`<${Frame} ctx=${ctx} screen=${screen} go=${go} acked=${acked} setAcked=${setAcked} theme=${theme}
    silence=${silence} silenceText="нет данных"
    footer=${html`<${Player} ...${ctx} runIdx=${runIdx} pickRun=${pickRun} playing=${playing} togglePlay=${togglePlay}
      speed=${speed} setSpeed=${setSpeed} setT=${(t) => setT(clampT(run, t))} modes=${modes} mode=${mode} setMode=${setMode} />`} />`;
}

function clampT(run, t) { return Math.min(run.t_end, Math.max(run.t_start, t)); }

function lastData(tl, T) {
  return Math.max(...['safety', 'cmd', 'mem', 'health'].map((k) => { const m = tl.at(k, T); return m ? m.t : -Infinity; }));
}

// ---- «Живой»

let liveSource = null;      // один поток на вкладку (и на браузер — через ведущую вкладку)

function LiveApp({ meta, modes, mode, setMode, p0, theme }) {
  const src = useMemo(() => liveSource || (liveSource = new LiveSource().start()), []);
  const [, force] = useState(0);
  const [screen, go] = useScreen(p0);
  const [acked, setAcked] = useState(() => new Set());
  useBlurOnClick();
  useEffect(() => {
    src.onChange = () => force((x) => x + 1);
    const id = setInterval(() => force((x) => x + 1), LIVE_TICK_MS);   // возраст значений и связи идёт и без сообщений
    // «нет связи» — по смене признака, а не перерисовкой всего пульта 10 раз в секунду
    let stale = false;
    const link = setInterval(() => { const s = src.linkAge() > NO_LINK_S; if (s !== stale) { stale = s; force((x) => x + 1); } }, LINK_CHECK_MS);
    return () => { clearInterval(id); clearInterval(link); src.onChange = null; };
  }, [src]);
  useEffect(() => {
    const onKey = (e) => { if (!typing(e)) screenKey(e, go); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  const hello = src.hello;
  if (src.denied) return html`<${Login} />`;
  if (!hello) {
    return html`<div class="loading" data-testid="connecting">Подключение к ${meta.source === 'двойник' ? 'двойнику' : 'роботу'}…
      ${src.state === 'closed' ? ' шлюз не отдаёт поток, повторяю' : ''}</div>`;
  }
  const T = src.now();
  const t0 = hello.started || T;
  const run = {
    id: 'live', live: true, system: 'a1', run: hello.run || '', label: 'живой поток', origin: hello.source, world: '',
    date: new Date(T * 1000).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' }), has_safety: true,
    t_start: t0, t_end: T, anchor_t: t0, model: '', summary: null, units: [], ref: hello.ref || 't_end',
  };
  const bundle = { alarms: hello.alarms || {}, geometry: hello.geometry, compare_rows: [] };
  const ctx = { bundle, run, runs: [run], tl: src.tl, T, t0, geo: hello.geometry, theme: theme.themeKey, meta,
    live: { src, hello, age: src.linkAge(), state: src.state } };
  return html`<${Frame} ctx=${ctx} screen=${screen} go=${go} acked=${acked} setAcked=${setAcked} theme=${theme}
    silence=${src.linkAge()} limit=${NO_LINK_S} silenceText=${'нет связи с ' + (hello.source === 'двойник' ? 'двойником' : 'роботом')}
    footer=${html`<${LiveBar} ctx=${ctx} modes=${modes} mode=${mode} setMode=${setMode} />`} />`;
}

function LiveBar({ ctx, modes, mode, setMode }) {
  const { live, tl, T, t0 } = ctx;
  const age = live.age;
  const span = Math.max(0, T - Math.max(t0, T - 900));
  const link = !Number.isFinite(age) ? 'нет' : age > NO_LINK_S ? `нет ${fmtAge(age)}` : `есть · ${fmtAge(age)}`;
  // поток начат заново (шлюз перезапущен или браузер отстал от буфера): история браузера — с этого момента, не молча
  const rs = live.src.restart;
  const from = rs ? live.src.historyFrom() : null;
  return html`<footer class="player livebar" aria-label="живой поток" data-testid="livebar">
    <${ModeSwitch} modes=${modes} mode=${mode} setMode=${setMode} />
    <span class="badge live">ЖИВОЙ</span>
    <span class="num" data-testid="link">связь: ${link}</span>
    <span class="muted num">поток с ${clock(t0)} · буфер шлюза — 15 мин, в окне ${dur(span)}</span>
    ${rs ? html`<span class="num" data-testid="restart" title="прежняя история браузера выброшена: тревоги и жесты прежнего потока могли бы висеть вечно">${rs.why} в ${clock(rs.t)} · история с ${from != null ? clock(from) : DASH}</span>` : ''}
    <span class="sp"></span>
    <span class="muted num" title="время робота по его часам">${clock(T)}</span>
  </footer>`;
}

function Login() {
  return html`<div class="login" data-testid="login"><section class="panel">
    <h2><span>Вход в пульт</span></h2>
    <p>Пульт открывается одноразовой ссылкой. На роботе: <code>sudo neurobot ui-url</code>; с ноутбука — туннель
      <code>ssh -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -L 18120:127.0.0.1:18120 &lt;пользователь&gt;@&lt;робот&gt;</code>,
      затем открыть ссылку.</p>
    <p class="muted">Ссылка действует 2 минуты и один раз; вход — 12 часов (срок держит шлюз). Cookie видна и другим
      локальным портам этого браузера: на ноутбуке с чужими dev-серверами выходите из пульта (на роботе — удалить сессию).</p>
  </section></div>`;
}

// ---- каркас

function Frame({ ctx, screen, go, acked, setAcked, theme, silence, silenceText, footer, limit = NO_DATA_S }) {
  const cur = SCREENS.find((s) => s.id === screen) || SCREENS[0];
  const stale = silence > limit;
  const [help, setHelp] = useState(false);
  return html`<div class=${'frame'+(screen==='lab'?' lab-frame':'')+(ctx.run.catalog?' catalog-frame':'')}>
    <${StatusBar} ...${ctx} />
    <${Nav} ...${ctx} screen=${screen} go=${go} acked=${acked} setAcked=${setAcked} themeMode=${theme.themeMode} setThemeMode=${theme.setThemeMode} showHelp=${() => setHelp(true)} />
    <main class=${'main' + (stale ? ' nolink' : '')} id="main" aria-label=${cur.title} data-screen=${cur.id}>
      ${stale ? html`<div class="nolinkbar" role="status" data-testid="nolink">${silenceText} ${Number.isFinite(silence) ? fmtAge(silence) : ''} — показанное больше не правда</div>` : ''}
      ${!['lab','connect','assistant'].includes(screen)?html`<div class="pagehead"><div><div class="eyebrow">ЦЕНТР НАБЛЮДЕНИЯ <span>/ ${cur.key.padStart(2, '0')}</span></div>
        <h1>${cur.title}</h1><p>${cur.desc}</p></div>
        <div class="session-label"><span class=${'signal-dot' + (stale ? ' offline' : '')}></span>
          ${ctx.run.catalog ? 'Оборудование' : ctx.live ? 'Живой поток' : 'Просмотр записи'}<span class="session-detail">${ctx.run.catalog ? 'MCP' : ctx.live ? ctx.run.origin : ctx.run.system.toUpperCase() + ' · ' + ctx.run.date}</span></div></div>`:''}
      <${cur.C} ...${ctx} />
      <div class="main-footnote">${ctx.run.demo ? 'Симулятор sysfs · физическое оборудование не подключено' : ctx.run.catalog ? 'EdgeLab · локальные трассы агента и подключённое оборудование' : ctx.live ? (ctx.run.run.includes('подставной') ? 'Синтетический стенд · данные для проверки интерфейса' : 'Источник: ' + ctx.run.origin + ' · наблюдение в реальном времени') : 'Цифровой двойник · данные записи · ориентир стенда, не результат на роботе'}<span>EDGE LAB / RESEARCH</span></div>
    </main>
    ${footer}
    ${help ? html`<${Shortcuts} close=${() => setHelp(false)} />` : ''}
  </div>`;
}

function Shortcuts({ close }) {
  const ref = useRef(null);
  useEffect(() => { ref.current.showModal(); }, []);
  return html`<dialog class="shortcuts" ref=${ref} onCancel=${close} aria-labelledby="help-title">
    <div class="dialog-head"><div><div class="eyebrow">БЫСТРЫЙ ДОСТУП</div><h2 id="help-title">Горячие клавиши</h2></div>
      <button class="icon-button" onClick=${close} aria-label="Закрыть"><${Icon} name="close" /></button></div>
    <dl><dt><kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd> <kbd>4</kbd> <kbd>5</kbd> <kbd>6</kbd> <kbd>7</kbd></dt><dd>Переключить экран</dd>
      <dt><kbd>Esc</kbd></dt><dd>Вернуться к обзору</dd><dt><kbd>Пробел</kbd></dt><dd>Проиграть / пауза записи</dd>
      <dt><kbd>←</kbd> <kbd>→</kbd></dt><dd>Перемотать на 10 секунд</dd><dt><kbd>Shift</kbd> + <kbd>←</kbd> <kbd>→</kbd></dt><dd>Перемотать на минуту</dd></dl>
    <p class="muted">Клавиши проигрывателя действуют в режиме записи. Команды роботу доступны только через кнопки пульта.</p>
  </dialog>`;
}

const WORLDS = { hall: 'зал', lab: 'комната lab' };

function firstNotable(safety) {
  if (!safety) return null;
  const r = safety.reasons || [];
  return r[0] ? String(r[0]).split(':')[0] : null;
}

// тревоги ленты на момент T: стоящие и снятые за 10 мин (по одной строке на ключ)
function alarmRows(tl, T) {
  const all = tl.alarms(T).filter((a) => a.on || (a.off != null && T - a.off <= ALARM_WIN_S));
  const on = all.filter((a) => a.on).sort((a, b) => (a.prio - b.prio) || (b.since - a.since));
  const off = all.filter((a) => !a.on).sort((a, b) => b.off - a.off);
  return { on, off };
}

function StatusBar({ run, tl, T, t0, live, meta }) {
  if (run.catalog) return html`<header class="status" role="banner"><div class="status-top"><div class="workspace-crumb"><span class="mobile-brand"><${Icon} name="network" /></span><b>EdgeLab</b><span>/</span><span>Среда исследования агента</span></div><div class="source-block" data-testid="source"><span class="badge">${run.demo ? 'СИМУЛЯТОР' : 'MCP'}</span><span class="srcrun">${run.demo ? 'Живое sysfs через hw-mcp' : 'Подключаемые источники'}<small>${run.demo ? 'Тестовые датчики · без платы' : 'Каталог и живые сигналы'}</small></span></div></div><div class="status-telemetry"><div class="cell"><span class="k">РАБОЧЕЕ ПРОСТРАНСТВО</span><span class="v">Наблюдение → решение → эксперимент</span></div><span class="observation"><span class="signal-dot"></span> LOCAL WORKSPACE</span></div></header>`;
  const s = tl.at('safety', T);
  const outM = tl.atWhere('cmd', T, (m) => m.topic === 'out');
  const out = outM && T - outM.t <= NO_DATA_S ? outM : null;
  const { on, off } = alarmRows(tl, T);
  const level = s ? s.level : 0;
  const limit = s && s.limits ? s.limits.v_max : null;
  const world = WORLDS[run.world] || run.world;
  const dataAge = live ? live.age : T - lastData(tl, T);
  return html`<header class="status" role="banner">
    <div class="status-top">
      <div class="workspace-crumb"><span class="mobile-brand"><${Icon} name="chip" /></span><b>Пульт оператора</b><span class="crumb-divider">/</span><span>NeuroChip</span></div>
      <div class="source-block" data-testid="source" title=${live ? 'источник: живой поток шлюза · ' + run.origin + ' · ' + run.run : 'источник: запись прогона на цифровом двойнике'}>
        <span class=${'badge' + (live ? ' live' : '')}>${live ? 'ЖИВОЙ' : 'ЗАПИСЬ'}</span>
        <span class="srcrun">${live ? html`источник: ${run.origin}<small>${run.run}</small>` : html`источник: запись · ${run.system.toUpperCase()}<small>${run.origin} · ${world} · ${run.date}</small>`}</span>
      </div>
      <${StopButton} meta=${meta} live=${live} />
    </div>
    <div class="status-telemetry">
      <div class=${'cell state-cell level-' + level} title="состояние safety_gate и первая причина"><span class="k">Safety gate</span>
        <span class="v" data-testid="status-safety">${run.has_safety ? html`<${Sign} prio=${level} /><b>${s ? stateWord(s.state) : DASH}</b><span class="muted reason-code">${firstNotable(s) || ''}</span>` : html`<span class="muted">safety_gate нет</span>`}</span></div>
      <div class="cell speed-cell" title=${limit == null ? 'выданная скорость' : 'выданная скорость / предел'}><span class="k">Скорость ${limit != null ? '/ предел' : ''}</span>
        <span class="v num">${out ? (limit != null ? `${num(out.v, 2)} / ${unit(limit, 2, 'м/с')}` : unit(out.v, 2, 'м/с')) : DASH}</span></div>
      <div class="cell alarm-cell" data-testid="alarm-count"><span class="k">Тревоги</span><span class="v">активных ${int(on.length)} <span class="muted">· за 10 мин ${int(on.length + off.length)}</span></span></div>
      <div class="cell data-cell" title=${live ? 'возраст последнего сообщения шлюза' : 'возраст данных записи'}><span class="k">${live ? 'Связь' : 'Данные'}</span>
        <span class="v num"><i class=${'signal-dot' + (dataAge > NO_LINK_S ? ' offline' : '')}></i>${Number.isFinite(dataAge) ? fmtAge(dataAge) : DASH}</span></div>
      <div class="cell clock-cell"><span class="k">${live ? 'Время робота' : 'Время записи'}</span><span class="v num">${clock(T)} <span class="muted elapsed">T${dur(T - t0, true)}</span></span></div>
      <span class="observation"><span class="signal-dot"></span> НАБЛЮДЕНИЕ</span>
    </div>
  </header>`;
}

// итог «СТОП движения» коротко (полная причина шлюза — в подсказке): принят safety_gate (вырос его счётчик стопов),
// отправлен без подтверждения, нет ответа за 2 с (запрос брошен — дошёл ли, неизвестно), устарел в пути (шлюз не
// исполнил), никто не слушает, нет связи
function stopHint(res) {
  if (res.ok) return `принят safety_gate · ${clock(res.t)} · ${num(res.ms, 0)} мс`;
  if (res.status === 202) return 'отправлен, без подтверждения — смотрите на робот';
  if (res.timeout) return 'нет ответа за 2 с — останавливайте у робота';
  if (res.status === 409) return `не исполнен: шёл ${num(res.age_s, 1)} с — останавливайте у робота`;
  if (res.status === 503) return 'не дошёл: safety_gate не слушает — останавливайте у робота';
  if (res.status === 0) return 'нет связи со шлюзом — останавливайте у робота';
  return `не отправлен: ${res.error || 'HTTP ' + res.status}`;
}

// «СТОП движения» — /neurobot/stop_now (ноль сразу, без защёлки): видна всегда, работает только у живого шлюза —
// и когда на экране запись (команда уходит роботу, а не в запись). Не аварийная кнопка: при обрыве связи не сработает.
// Пока запрос идёт (не дольше 2 с), кнопка заблокирована: повторные нажатия не копятся в очереди и не придут разом.
function StopButton({ meta }) {
  const can = meta && meta.mode === 'live';
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (!res) return undefined; const id = setTimeout(() => setRes(null), RESULT_MS); return () => clearTimeout(id); }, [res]);
  const press = async () => {
    if (busy) return;
    setBusy(true);
    const t = performance.now();
    const r = await command('api/stop', meta.csrf, { reason: 'кнопка пульта' });
    setBusy(false);
    setRes({ ...r, ms: performance.now() - t });
  };
  const hint = !can ? 'только в живом режиме' : busy ? 'отправляю…' : res ? stopHint(res) : 'не аварийная кнопка';
  return html`<button class=${'stopbtn' + (can ? ' on' : '') + (res && !res.ok ? ' bad' : '')} disabled=${!can || busy} aria-disabled=${can && !busy ? 'false' : 'true'}
      aria-describedby="stophint" data-testid="stop" data-result=${res ? (res.ok ? 'ok' : res.timeout ? 'timeout' : String(res.status)) : ''} onClick=${can ? press : undefined}
      title="«Стоп движения» — не аварийная кнопка: при обрыве связи не сработает. Ноль в /cmd_vel сразу, без защёлки; дальше — с нулевого запроса">
    СТОП движения<span id="stophint" class="hint" data-testid="stop-result" title=${res && res.error ? res.error : ''}>${hint}</span></button>`;
}

function AlarmRow({ a, t0, T, acked, ack }) {
  const k = a.key + '@' + a.since;
  return html`<div class=${'alarm' + (a.on ? '' : ' off')} onClick=${a.on ? () => ack(k) : undefined}
      title=${a.on ? 'щелчок — квитировать (гасит мигание, не тревогу)' : 'снята'}>
    <${Sign} prio=${a.prio} off=${!a.on} blink=${a.on && !acked.has(k)} />
    <div><div>${a.text}${a.n > 1 ? html` <span class="x">×${a.n}</span>` : ''}</div>
      <div class="t num">T${dur(a.since - t0, true)} · ${a.on ? dur(T - a.since) : 'снята'}</div></div>
  </div>`;
}

function Nav({ tl, T, t0, run, screen, go, acked, setAcked, themeMode, setThemeMode, showHelp }) {
  const { on, off } = alarmRows(tl, T);
  const [more, setMore] = useState(false);
  const ack = (k) => setAcked((s) => new Set(s).add(k));
  const nextTheme = { auto: 'light', light: 'dark', dark: 'light' };
  const themeName = { auto: 'Системная тема', light: 'Светлая тема', dark: 'Тёмная тема' };
  const shownOff = more ? off : off.slice(0, CLEARED_SHOWN);
  return html`<nav class="nav" aria-label="экраны и тревоги">
    <div class="nav-brand"><div class="brand-mark"><${Icon} name="lab" size=${27} /></div><div><b>edge<span>/</span>lab</b><small>EXPERIMENTAL OBSERVATORY</small></div></div>
    <div class="nav-section-label">РАБОЧЕЕ ПРОСТРАНСТВО</div>
    <div class="nav-links">${SCREENS.filter(s => !run.catalog || ['connect','assistant','equipment','lab'].includes(s.id)).map((s) => html`<a href=${'#/' + s.id} aria-label=${s.title} title=${s.title} aria-current=${screen === s.id ? 'page' : undefined}
        onClick=${(e) => { e.preventDefault(); go(s.id); }}>
      <${Icon} name=${s.id} /><span>${s.title}</span><kbd>${s.key}</kbd></a>`)}</div>
    <div class="alarmlist" data-testid="alarms">
      <h3>ЦЕНТР СОБЫТИЙ <span class="alarm-total num">${int(on.length)}</span></h3>
      ${on.length === 0 ? html`<div class="alarm-empty"><${Icon} name=${run.catalog?'lab':'safety'} size=${22} /><span>${run.catalog?'Локальная лаборатория':'Всё спокойно'}<small>${run.catalog?'Трассы, эксперименты и источники':'Активных тревог нет'}</small></span></div>` : on.map((a) => html`<${AlarmRow} a=${a} t0=${t0} T=${T} acked=${acked} ack=${ack} />`)}
      ${off.length ? html`<h3 class="sep">Снятые за 10 мин <span class="num">${int(off.length)}</span></h3>
        ${shownOff.map((a) => html`<${AlarmRow} a=${a} t0=${t0} T=${T} acked=${acked} ack=${ack} />`)}
        ${off.length > CLEARED_SHOWN ? html`<button class="more" onClick=${() => setMore(!more)}>${more ? 'свернуть' : `ещё ${off.length - CLEARED_SHOWN}`}</button>` : ''}` : ''}
    </div>
    <div class="navfoot"><${GlassToggle}/><button class="theme-toggle" onClick=${() => setThemeMode(nextTheme[themeMode] || 'dark')} aria-label="Переключить тему"><${Icon} name=${themeMode === 'light' ? 'sun' : 'moon'} size=${17} /><span>${themeName[themeMode]}</span></button>
      <button class="help-toggle" onClick=${showHelp} aria-label="Горячие клавиши"><${Icon} name="keyboard" size=${18} /></button></div>
    <div class="nav-project"><span class="signal-dot"></span><div>Edge AI Research<small>ИТМО · Дипломный проект</small></div><span class="version">01</span></div>
  </nav>`;
}

function Player({ run, runs, runIdx, pickRun, tl, T, t0, playing, togglePlay, speed, setSpeed, setT, theme, modes, mode, setMode }) {
  const cv = useRef(null);
  const total = run.t_end - run.t_start;
  const [size, setSize] = useState(0);
  const drag = useRef(false);
  useEffect(() => {
    const on = () => setSize(window.innerWidth);
    window.addEventListener('resize', on);
    return () => window.removeEventListener('resize', on);
  }, []);
  useEffect(() => {
    const c = cv.current;
    if (!c) return;
    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth, h = c.clientHeight;
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
    const ctx = c.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const pal = palette();
    const x0 = run.t_start - t0, x1 = run.t_end - t0;
    const segs = safetySegments(tl.list('safety'), run.t_end, x0, x1, t0);
    drawBand(ctx, pal, segs, x0, x1, 0, w, 0, h);
    ctx.fillStyle = pal.text;
    for (const g of tl.list('gesture')) {
      if (g.kind !== 'gesture' || g.outcome) continue;
      const x = ((g.t - t0 - x0) / (x1 - x0)) * w;
      ctx.fillRect(x, 0, 1, 3);
    }
  }, [run, theme, size]);
  // перемотка — по месту указателя на полосе (а не по ползунку: у него свой отступ)
  const seek = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    setT(run.t_start + (Math.min(r.width, Math.max(0, e.clientX - r.left)) / r.width) * total);
  };
  const pos = ((T - run.t_start) / total) * 100;
  return html`<footer class="player" aria-label="проигрыватель записи">
    <${ModeSwitch} modes=${modes} mode=${mode} setMode=${setMode} />
    <label class="muted" for="runsel">прогон</label>
    <select id="runsel" value=${runIdx} onChange=${(e) => { pickRun(Number(e.target.value)); e.target.blur(); }} data-testid="run-select">
      ${runs.map((r, i) => html`<option value=${i}>${r.label}</option>`)}
    </select>
    <button class="play" onClick=${togglePlay} aria-label=${playing ? 'пауза' : 'проиграть'} title="пробел">
      <svg viewBox="0 0 12 12" aria-hidden="true">${playing ? html`<path d="M2 1h3v10H2zM7 1h3v10H7z" />` : html`<path d="M2 1l9 5-9 5z" />`}</svg></button>
    <div class="seg" role="group" aria-label="скорость">
      ${SPEEDS.map((s) => html`<button aria-pressed=${speed === s ? 'true' : 'false'} onClick=${() => setSpeed(s)}>×${s}</button>`)}
    </div>
    <div class="scrub" role="slider" tabindex="0" aria-label="перемотка записи: ← → по 10 с, Shift — по минуте"
      aria-valuemin=${0} aria-valuemax=${Math.round(total)} aria-valuenow=${Math.round(T - run.t_start)} aria-valuetext=${'T' + dur(T - t0, true)}
      onPointerDown=${(e) => { drag.current = true; e.currentTarget.setPointerCapture(e.pointerId); seek(e); }}
      onPointerMove=${(e) => { if (drag.current) seek(e); }}
      onPointerUp=${() => { drag.current = false; }} onPointerCancel=${() => { drag.current = false; }}>
      <canvas ref=${cv}></canvas>
      <div class="head" style=${{ left: `calc(${pos}% - 1px)` }}></div>
    </div>
    <span class="time num">T${dur(T - t0, true)} / ${dur(run.t_end - t0)}</span>
  </footer>`;
}

async function main() {
  const root = document.getElementById('app');
  const p = params();
  if (p.theme) applyTheme(p.theme === 'auto' ? null : p.theme);
  else applyTheme(storedTheme());
  try {
    const res = await fetch('api/meta', { cache: 'no-store' });
    if (res.status === 401) {
      root.replaceChildren();
      render(html`<${Login} />`, root);
      return;
    }
    if (!res.ok) throw new Error('api/meta: HTTP ' + res.status);
    const meta = await res.json();
    if (meta.mode === 'live' && Number.isFinite(meta.t)) {
      setMetaClock(meta.t, Date.now());
      setInterval(() => fetch('api/meta', { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null))
        .then((m) => { if (m && Number.isFinite(m.t)) setMetaClock(m.t, Date.now()); }, () => {}), META_CLOCK_MS);
    }
    root.replaceChildren();          // «Загрузка пульта…» не остаётся под каркасом
    render(html`<${Root} meta=${meta} />`, root);
  } catch (e) {
    root.textContent = 'Пульт не загрузился: ' + e.message;
  }
}

main();
