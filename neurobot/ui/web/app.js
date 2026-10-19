// Пульт оператора NeuroChip — режим «Запись»: проигрыватель прогонов на двойнике (docs/ui/PLAN.md §0а, §4, П1).
// Каркас: строка состояния (источник на каждом экране), навигация 1–3 и лента тревог, рабочая область, проигрыватель.
// Пульт — «окно, а не руль»: в «Записи» ни одной команды; «Стоп движения» виден, но неактивен.
import { render } from 'preact';
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { html, Sign, palette, safetySegments, drawBand, stateWord } from './lib/ui.js';
import { Timeline } from './lib/timeline.js';
import { dur, clock, num, unit, age as fmtAge, int, DASH } from './lib/fmt.js';
import { Overview } from './screens/overview.js';
import { SafetyScreen } from './screens/safety.js';
import { TrendsScreen } from './screens/trends.js';

const SCREENS = [
  { id: 'overview', key: '1', title: 'Обзор', lvl: 'L1', C: Overview },
  { id: 'safety', key: '2', title: 'Безопасность и жесты', lvl: 'L2', C: SafetyScreen },
  { id: 'trends', key: '3', title: 'Агент и тренды', lvl: 'L4', C: TrendsScreen },
];
const SPEEDS = [1, 10, 60];
const TICK_MS = 100;
const NO_DATA_S = 2;        // данных нет дольше — рабочая область в штриховке
const ALARM_WIN_S = 600;    // лента тревог: стоящие и снятые за 10 мин
const CLEARED_SHOWN = 5;    // снятых показываем столько, остальные — «ещё N»

function params() {
  const q = new URLSearchParams(location.search);
  const hash = location.hash.replace(/^#\/?/, '');
  return {
    run: q.get('run'), t: q.has('t') ? Number(q.get('t')) : null, speed: Number(q.get('speed')) || 10,
    play: q.get('play') === '1', screen: SCREENS.some((s) => s.id === hash) ? hash : (q.get('screen') || 'overview'),
    theme: q.get('theme'),
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

// без ?t= — конец записи: итоговые числа, те же, что compare.md
function startT(run, p) {
  return clampT(run, p.t != null && Number.isFinite(p.t) ? run.anchor_t + p.t : run.t_end);
}

function App({ bundle }) {
  const p0 = useMemo(params, []);
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
  const [screen, setScreen] = useState(p0.screen);
  const [themeMode, setThemeMode] = useState(() => p0.theme || storedTheme() || 'auto');
  const [themeKey, setThemeKey] = useState(0);
  // квитирование — по случаю тревоги (ключ и момент, когда встала): вставшая снова мигает заново; у прогона — своё
  const [acked, setAcked] = useState(() => new Set());
  useEffect(() => setAcked(new Set()), [runIdx]);

  useEffect(() => { applyTheme(themeMode === 'auto' ? null : themeMode); setThemeKey((k) => k + 1); }, [themeMode]);
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const on = () => setThemeKey((k) => k + 1);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);

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

  const go = (id) => setScreen(id);
  useEffect(() => { if (location.hash !== '#/' + screen) history.replaceState(null, '', location.pathname + location.search + '#/' + screen); }, [screen]);
  useEffect(() => {
    const onHash = () => { const id = location.hash.replace(/^#\/?/, ''); if (SCREENS.some((s) => s.id === id)) setScreen(id); };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const togglePlay = () => {
    setT((t) => (t >= run.t_end ? run.t_start : t));
    setPlaying((x) => !x);
  };
  useEffect(() => {
    // кнопка, нажатая мышью, не держит фокус: иначе пробел снова нажмёт её (×60), а не поставит паузу;
    // с клавиатуры (Tab) фокус остаётся и пробел нажимает кнопку, как положено
    const onClick = (e) => { if (e.detail > 0) { const b = e.target.closest && e.target.closest('button'); if (b) b.blur(); } };
    const onKey = (e) => {
      const tag = e.target && e.target.tagName;
      if (tag === 'SELECT' || tag === 'TEXTAREA' || (tag === 'INPUT' && e.target.type !== 'range')) return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const s = SCREENS.find((x) => x.key === e.key);
      if (s) { go(s.id); e.preventDefault(); return; }
      if (e.key === 'Escape') { go('overview'); return; }
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
    document.addEventListener('click', onClick, true);
    return () => { window.removeEventListener('keydown', onKey); document.removeEventListener('click', onClick, true); };
  }, [run]);

  const pickRun = (i) => {
    const rel = T - t0;
    setRunIdx(i);
    setT(clampT(runs[i], runs[i].anchor_t + rel));
  };

  const ctx = { bundle, run, runs, tl, T, t0, geo: bundle.geometry, theme: themeKey };
  const cur = SCREENS.find((s) => s.id === screen) || SCREENS[0];
  // данных нет дольше 2 с — рабочая область в штриховке: показанное больше не правда (PLAN §4)
  const silence = T - lastData(tl, T);
  return html`<div class="frame">
    <${StatusBar} ...${ctx} />
    <${Nav} ...${ctx} screen=${screen} go=${go} acked=${acked} setAcked=${setAcked} themeMode=${themeMode} setThemeMode=${setThemeMode} />
    <main class=${'main' + (silence > NO_DATA_S ? ' nolink' : '')} id="main" aria-label=${cur.title} data-screen=${cur.id}>
      ${silence > NO_DATA_S ? html`<div class="nolinkbar" role="status">нет данных ${Number.isFinite(silence) ? fmtAge(silence) : ''} — показанное больше не правда</div>` : ''}
      <${cur.C} ...${ctx} />
    </main>
    <${Player} ...${ctx} runIdx=${runIdx} pickRun=${pickRun} playing=${playing} togglePlay=${togglePlay}
      speed=${speed} setSpeed=${setSpeed} setT=${(t) => setT(clampT(run, t))} />
  </div>`;
}

function clampT(run, t) { return Math.min(run.t_end, Math.max(run.t_start, t)); }

function lastData(tl, T) {
  return Math.max(...['safety', 'cmd', 'mem', 'health'].map((k) => { const m = tl.at(k, T); return m ? m.t : -Infinity; }));
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

function StatusBar({ run, tl, T, t0 }) {
  const s = tl.at('safety', T);
  const outM = tl.atWhere('cmd', T, (m) => m.topic === 'out');
  const out = outM && T - outM.t <= NO_DATA_S ? outM : null;
  const { on, off } = alarmRows(tl, T);
  const llmHour = tl.range('llm', T - 3600, T).length;
  const lastAny = lastData(tl, T);
  const level = s ? s.level : 0;
  const limit = s && s.limits ? s.limits.v_max : null;
  const world = WORLDS[run.world] || run.world;
  return html`<header class="status" role="banner">
    <div class="cell opt"><span class="k">NeuroChip</span><span class="v brand">ПУЛЬТ</span></div>
    <div class="cell" title="состояние safety_gate и первая причина">
      <span class="k">безопасность</span>
      <span class="v">${run.has_safety ? html`${level ? html`<${Sign} prio=${level} /> ` : ''}<b>${s ? stateWord(s.state) : DASH}</b>
        <span class="muted"> ${firstNotable(s) || ''}</span>` : html`<span class="muted">safety_gate нет</span>`}</span>
    </div>
    <div class="cell" title=${limit == null ? 'предел скорости (limits) в записи не сохранялся — только в живом режиме' : 'выдано / предел'}>
      <span class="k">v out${limit != null ? ' / предел' : ''}</span>
      <span class="v num">${out ? (limit != null ? `${num(out.v, 2)} / ${unit(limit, 2, 'м/с')}` : unit(out.v, 2, 'м/с')) : DASH}</span></div>
    <div class="cell" data-testid="alarm-count" title="стоящие тревоги и все, что вставали за 10 мин (строк в ленте)"><span class="k">тревоги</span>
      <span class="v num">активных ${int(on.length)} · за 10 мин ${int(on.length + off.length)}</span></div>
    <div class="cell" title="возраст последних данных записи на момент проигрывания"><span class="k">данные</span>
      <span class="v num">${Number.isFinite(lastAny) ? fmtAge(T - lastAny) : DASH}</span></div>
    <div class="cell opt"><span class="k">LLM за час</span><span class="v num">${int(llmHour)}</span></div>
    <div class="cell"><span class="k">время записи</span>
      <span class="v num">${clock(T)} <span class="muted">T${dur(T - t0, true)}</span></span></div>
    <div class="cell opt"><span class="k">режим</span><span class="v">НАБЛЮДЕНИЕ</span></div>
    <div class="cell grow src" data-testid="source" title="источник данных: запись прогона на цифровом двойнике — не робот">
      <span class="badge">ЗАПИСЬ</span><span class="srcrun">источник: запись · ${run.system.toUpperCase()} ${run.run} · ${run.date} <span class="muted">(${run.origin}, ${world})</span></span></div>
    <button class="stopbtn" disabled aria-disabled="true" aria-describedby="stophint"
      title="«Стоп движения» — не аварийная кнопка: при обрыве связи не сработает">СТОП движения<span id="stophint" class="hint">только в живом режиме</span></button>
  </header>`;
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

function Nav({ tl, T, t0, screen, go, acked, setAcked, themeMode, setThemeMode }) {
  const { on, off } = alarmRows(tl, T);
  const [more, setMore] = useState(false);
  const ack = (k) => setAcked((s) => new Set(s).add(k));
  const nextTheme = { auto: 'light', light: 'dark', dark: 'auto' };
  const themeName = { auto: 'как в системе', light: 'светлая', dark: 'тёмная' };
  const shownOff = more ? off : off.slice(0, CLEARED_SHOWN);
  return html`<nav class="nav" aria-label="экраны и тревоги">
    ${SCREENS.map((s) => html`<a href=${'#/' + s.id} aria-current=${screen === s.id ? 'page' : undefined}
        onClick=${(e) => { e.preventDefault(); go(s.id); }}>
      <span class="key">${s.key}</span><span>${s.title}</span><span class="lvl">${s.lvl}</span></a>`)}
    <div class="alarmlist" data-testid="alarms">
      <h3>Тревоги · активные <span class="num">${int(on.length)}</span></h3>
      ${on.length === 0 ? html`<div class="empty">нет</div>` : on.map((a) => html`<${AlarmRow} a=${a} t0=${t0} T=${T} acked=${acked} ack=${ack} />`)}
      ${off.length ? html`<h3 class="sep">Снятые за 10 мин <span class="num">${int(off.length)}</span></h3>
        ${shownOff.map((a) => html`<${AlarmRow} a=${a} t0=${t0} T=${T} acked=${acked} ack=${ack} />`)}
        ${off.length > CLEARED_SHOWN ? html`<button class="more" onClick=${() => setMore(!more)}>${more ? 'свернуть' : `ещё ${off.length - CLEARED_SHOWN}`}</button>` : ''}` : ''}
    </div>
    <div class="navfoot">тема: <button onClick=${() => setThemeMode(nextTheme[themeMode] || 'auto')}>${themeName[themeMode] || themeName.auto}</button></div>
  </nav>`;
}

function Player({ run, runs, runIdx, pickRun, tl, T, t0, playing, togglePlay, speed, setSpeed, setT, theme }) {
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
  try {
    const res = await fetch('api/bundle', { cache: 'no-store' });
    if (!res.ok) throw new Error('бандл: HTTP ' + res.status);
    const bundle = await res.json();
    if (bundle.format !== 'neurobot-ui-bundle/1') throw new Error('не бандл пульта: ' + bundle.format);
    const p = params();
    if (p.theme) applyTheme(p.theme === 'auto' ? null : p.theme);
    else applyTheme(storedTheme());
    root.replaceChildren();          // «Загрузка пульта…» не остаётся под каркасом
    render(html`<${App} bundle=${bundle} />`, root);
  } catch (e) {
    root.textContent = 'Пульт не загрузился: ' + e.message;
  }
}

main();
