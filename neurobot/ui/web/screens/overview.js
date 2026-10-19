// «Обзор» (L1): всё ли в порядке и куда смотреть. Слева — безопасность: состояние и первая причина, мини-лидар по
// safety_state, полоса 15 мин; под ней — события стенда и человека; справа — плитки 3×2 (заголовок, значение с
// единицей, одна строка контекста, спарклайн 15 мин), лента и список последних жестов. На 1366×768 события — рядом
// с жестами, и экран целиком в окне. Цвет — только у панели с активной тревогой (рамка и знак). Пояснения — в
// подсказках «i», а не текстом на экране.
import { useEffect, useRef } from 'preact/hooks';
import { html, Sign, V, Age, Info, fresh, inK1, palette, safetySegments, stateWord, useMedia, OUTCOMES } from '../lib/ui.js';
import { Spark } from '../lib/spark.js';
import { drawLidar } from '../lib/lidar.js';
import { num, unit, int, dur, pct, plural, DASH } from '../lib/fmt.js';
import { GMark, Tape } from './gestures.js';

const WIN = 900;   // 15 мин
const STALE = { safety: 2.5, out: 2, mem: 25, units: 25 };
const SHARE = ['ok', 'slow', 'blocked', 'estop'];

function Tile({ title, info, level = 0, ageSec, stale, children, testid }) {
  return html`<section class=${'panel tile' + (level ? ' a' + level : '') + (stale ? ' stale' : '')} data-testid=${testid}>
    <h2>${level ? html`<${Sign} prio=${level} />` : ''}<span>${title}</span>${info ? html`<${Info} text=${info} />` : ''}<span class="r">${ageSec != null ? html`<${Age} sec=${ageSec} stale=${stale} />` : ''}</span></h2>
    ${children}
  </section>`;
}

function Lidar({ geo, safety, out, hasSafety, theme }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) drawLidar(ref.current, { geo, safety, out, pal: palette(), hasSafety, level: safety ? safety.level : 0 });
  });
  return html`<canvas class="lidar" ref=${ref} role="img" aria-label="мини-лидар: корпус, коридор, слепые секторы, зазор и тормозной путь"></canvas>`;
}

const SAFETY_INFO = 'Мини-лидар по safety_state: скана в записи нет. Корпус Ø0,39 м, коридор 0,49 м, поле лидара ±119,9° '
  + '(сзади датчика нет), маска корпуса камеры, клин глубины 87°. Зазор — до ближайшей опасности в любую сторону; '
  + 'd_stop — тормозной путь safety_gate при скорости /cmd_vel (умолчания safety_gate, допущение до замера на роботе). '
  + 'Серое — норма: slow с lidar_blind на роботе штатно, перед видит только глубина.';

function SafetyPanel({ run, tl, T, t0, geo, theme, dict, stats }) {
  const safety = tl.at('safety', T);
  const sAge = safety ? T - safety.t : null;
  const stale = sAge != null && sAge > STALE.safety;
  const out = fresh(tl.atWhere('cmd', T, (m) => m.topic === 'out'), T, STALE.out);
  const level = safety ? safety.level : 0;
  const x0 = T - t0 - WIN, x1 = T - t0;
  const segs = safetySegments(tl.range('safety', T - WIN - 3, T), T, x0, x1, t0);
  const first = safety && safety.reasons.length ? safety.reasons[0] : null;
  const code = first ? String(first).split(':')[0].trim() : null;
  const share = stats && stats.safety_share;
  return html`<section class=${'panel safetybox' + (level ? ' a' + level : '') + (stale ? ' stale' : '')} data-testid="tile-safety">
    <h2>${level ? html`<${Sign} prio=${level} />` : ''}<span>Безопасность</span><${Info} text=${SAFETY_INFO} /><span class="r">${sAge != null ? html`<${Age} sec=${sAge} stale=${stale} />` : ''}</span></h2>
    ${run.has_safety ? html`
      <div class="row"><div class="big" data-testid="state-word">${safety ? stateWord(safety.state) : DASH}</div>
        <div class="sub">${safety ? (level ? 'тревога P' + level : 'норма') : ''}</div></div>
      <div class="reason">${code ? html`<b>${code}</b> — ${(dict[code] || {}).text || ''}${safety.reasons.length > 1 ? html` <span class="muted">и ещё ${safety.reasons.length - 1}</span>` : ''}` : html`<span class="muted">причин нет</span>`}</div>
      <div class="sub">зазор ${unit(safety ? safety.min_clearance_m : null, 2, 'м')} · d_stop ${unit(out ? out.d_stop_m : null, 2, 'м')}${out ? ` при ${unit(Math.max(0, out.v), 2, 'м/с')}` : ''}</div>`
      : html`<div class="big muted">${DASH}</div><div class="reason muted">стек лаборатории пишет /cmd_vel прямо в twist_mux</div>`}
    <${Lidar} geo=${geo} safety=${safety} out=${out} hasSafety=${run.has_safety} theme=${theme} />
    ${run.has_safety ? html`<div class="legend items"><span>штриховка — нет датчика</span><span>пунктир — зазор</span><span>черта — d_stop</span></div>` : ''}
    ${run.has_safety ? html`<div class="bandcap">состояния за 15 мин</div>
      <${Spark} kind="band" segs=${segs} x0=${x0} x1=${x1} theme=${theme} title="состояния safety_gate за 15 мин" />` : ''}
    ${run.has_safety ? html`<div class="sub items" data-testid="safety-share">за прогон: ${share ? SHARE.map((k, i) => html`<span>${k} ${pct(share[k] || 0)}${i < SHARE.length - 1 ? ' · ' : ''}</span> `) : DASH}</div>` : ''}
  </section>`;
}

// вызовы по минутам окна: [x — середина минуты], [число]
function perMinute(msgs, t0, x0, x1) {
  const xs = [], ys = [];
  for (let b = Math.floor(x0 / 60) * 60; b < x1; b += 60) { xs.push(b + 30); ys.push(0); }
  for (const m of msgs) {
    const i = Math.floor((m.t - t0 - xs[0] + 30) / 60);
    if (i >= 0 && i < ys.length) ys[i]++;
  }
  return [xs, ys];
}

function sum(list, f) {
  const has = list.filter((m) => m[f] != null);
  return has.length ? has.reduce((s, m) => s + m[f], 0) : (list.length ? null : 0);
}

function Events({ tl, T, t0 }) {
  const events = [...tl.until('fault', T).map((m) => ({ ...m, text: `отказ стенда: ${m.fault}`, tail: m.ttr_s != null ? `поднялся за ${unit(m.ttr_s, 1, 'с')}` : 'не поднялся' })),
    ...tl.until('operator', T).map((m) => ({ ...m, text: `человек: ${m.action}${m.what ? ' ' + m.what : ''}`, tail: m.why || '' })),
    ...tl.until('touch', T).map((m) => ({ ...m, text: 'касание (истина стенда)', tail: (m.models || []).join(', ') }))]
    .sort((a, b) => b.t - a.t).slice(0, 30);
  return html`<section class="panel events" data-testid="events">
    <h2><span>События стенда и человека</span><span class="r muted num">${int(events.length)}</span></h2>
    <div class="scroll">${events.length === 0 ? html`<div class="empty">событий нет</div>` : events.map((e) => html`
      <div class="ev"><span class="muted num">T${dur(e.t - t0, true)}</span><span>${e.text}${e.tail ? html`<span class="muted"> · ${e.tail}</span>` : ''}</span></div>`)}</div>
  </section>`;
}

function LastGestures({ gest, t0 }) {
  const last = gest.slice(-14).reverse();
  return html`<section class="panel glist" data-testid="last-gestures">
    <h2><span>Жесты · последние</span><${Info} text="Δ — задержка t0 → первая ненулевая /cmd_vel; «защита» — исход оборвал safety_gate." /></h2>
    <div class="scroll">${last.length === 0 ? html`<div class="empty">жестов ещё не было</div>` : html`<table class="gtab">
      <thead><tr><th>T</th><th></th><th>жест</th><th class="n">conf</th><th>исход</th><th class="n">Δ, с</th></tr></thead>
      <tbody>${last.map((g) => html`<tr>
        <td class="num muted">T${dur(g.first - t0, true)}</td><td><${GMark} outcome=${g.outcome} /></td>
        <td>${g.kind === 'negative' ? html`<span class="muted">без жеста: ${g.neg || ''}</span>` : (g.name || g.maneuver || DASH)}</td>
        <td class="n num">${num(g.conf, 2)}</td>
        <td>${g.outcome ? OUTCOMES[g.outcome].text : html`<span class="muted">ждём</span>`}${g.stopped_by ? html`<span class="muted"> · защита</span>` : ''}</td>
        <td class="n num">${num(g.d_cmd_s, 2)}</td></tr>`)}</tbody></table>`}</div>
  </section>`;
}

export function Overview({ run, tl, T, t0, geo, theme, bundle }) {
  const x0 = T - t0 - WIN, x1 = T - t0;
  const k1 = tl.at('k1', T);
  const stats = tl.at('stats', T);
  const mem = tl.at('mem', T);
  const units = tl.at('units', T);
  const gest = tl.gestures(T).filter((g) => g.kind === 'gesture' || g.outcome === 'false_action');
  const llmHour = tl.range('llm', T - 3600, T);
  const tokHour = llmHour.reduce((s, m) => s + (m.prompt_tokens || 0) + (m.completion_tokens || 0), 0);
  const budget = geo.budget_mib;

  const gWin = gest.filter((g) => inK1(g) && g.first - t0 >= x0);
  const memWin = tl.range('mem', T - WIN, T);
  const unitsWin = tl.range('units', T - WIN, T);
  const llmWin = tl.range('llm', T - WIN, T);
  const [mx, my] = perMinute(llmWin, t0, x0, x1);
  const [gx, gy] = perMinute(llmWin.filter((m) => m.kind === 'gesture'), t0, x0, x1);
  const gTicks = gest.filter((g) => g.kind === 'gesture' && g.first - t0 >= x0).map((g) => g.first - t0);
  const touches = tl.range('touch', T - WIN, T);
  const before = tl.at('touch', T - WIN);
  const tx = [x0, ...touches.map((m) => m.t - t0), x1];
  const ty = [before ? before.episodes : 0, ...touches.map((m) => m.episodes), touches.length ? touches[touches.length - 1].episodes : (before ? before.episodes : 0)];
  const unitLevel = units && units.down && units.down.length ? 2 : 0;
  const nCalls = llmHour.length;
  const faults = tl.until('fault', T).length;
  const narrow = useMedia('(max-width: 1600px)');

  const safety = html`<${SafetyPanel} run=${run} tl=${tl} T=${T} t0=${t0} geo=${geo} theme=${theme} dict=${bundle.alarms} stats=${stats} />`;
  const glist = html`<${LastGestures} gest=${gest} t0=${t0} />`;
  const events = html`<${Events} tl=${tl} T=${T} t0=${t0} />`;
  const tiles = html`<div class="tiles">
      <${Tile} title="Жест → команда" testid="tile-k1" info=${'p50 / p95 задержки t0 → первая ненулевая /cmd_vel по 20 последним жестам; t0 — истина генератора жестов двойника (на роботе будет конец жеста по perception). Без ранних (реакция раньше t0) и без жестов при RTF < 0,95 — как K1 в metrics.py. Точки — жесты за 15 мин, пунктир — цель K1 p95 ≤ 0,5 с (стенд).'}>
        <div class="big"><${V} v=${k1 && k1.last.n ? `${num(k1.last.p50, 2)} / ${num(k1.last.p95, 2)}` : DASH} u="с" /></div>
        <div class="sub">${k1 ? `p50 / p95 · последних ${k1.last.n} · прогон ${num(k1.run.p50, 2)} / ${num(k1.run.p95, 2)}` : 'команд на жесты ещё не было'}</div>
        <${Spark} kind="points" xs=${gWin.map((g) => g.first - t0)} ys=${gWin.map((g) => g.d_cmd_s)} x0=${x0} x1=${x1} target=${0.5} theme=${theme} title="задержка каждого жеста за 15 мин" />
      <//>

      <${Tile} title="LLM на жест" testid="tile-k4" info="K4: вызовы LLM вне реплик человека (не на реплику и не служебные вызовы хода) за прогон. Рефлекс жестов работает без LLM, цель — 0. Линия — такие вызовы по минутам, черточки внизу — жесты.">
        <div class="big"><${V} v=${stats ? int(stats.llm.gesture) : DASH} /></div>
        <div class="sub">вызовов вне реплик за прогон · цель 0</div>
        <${Spark} kind="step" xs=${gx} ys=${gy} ticks=${gTicks} x0=${x0} x1=${x1} y1=${1} theme=${theme} title="вызовы LLM на жест по минутам и жесты за 15 мин" />
      <//>

      <${Tile} title="LLM за час" testid="tile-llm" info=${`Вызовы LLM этой системы за последний час записи: на реплики человека, служебные и на жест. ₽ факт — по счёту провайдера (в записи mockllm — 0 ₽), ₽ оценка — по замороженному прайсу RouterAI 06.10.2026 для ${run.model}. Столбцы — вызовы по минутам.`}>
        <div class="big"><${V} v=${int(nCalls)} u=${plural(nCalls, 'вызов', 'вызова', 'вызовов')} /></div>
        <div class="sub">${int(tokHour)} ток. · ₽ ${num(sum(llmHour, 'rub_fact'), 2)} факт · ${num(sum(llmHour, 'rub_est'), 2)} оценка</div>
        <${Spark} kind="bars" xs=${mx} ys=${my} x0=${x0} x1=${x1} theme=${theme} title="вызовы LLM по минутам за 15 мин" />
      <//>

      <${Tile} title="Память стека" testid="tile-mem" ageSec=${mem ? T - mem.t : null} stale=${mem && T - mem.t > STALE.mem} info=${`PSS компонентов стека без драйверов двойника против бюджета стека с GNOME: ${num(budget.gnome, 0)} из ${num(budget['физическая'], 0)} МиБ (headless — ${num(budget.headless, 0)}), neurobot/sim/budget. CPU — доля одного ядра.`}>
        <div class="big"><${V} v=${mem ? num(mem.total_mib, 0) : DASH} u="МиБ" /></div>
        <div class="bar" title="PSS против бюджета стека с GNOME"><i style=${{ width: `${mem ? Math.min(100, (100 * mem.total_mib) / budget.gnome) : 0}%` }}></i></div>
        <div class="sub">${mem ? pct(mem.total_mib / budget.gnome) : DASH} из ${num(budget.gnome, 0)} МиБ · CPU ${mem && mem.cpu_pct != null ? unit(mem.cpu_pct, 0, '%') : DASH}</div>
        <${Spark} kind="line" xs=${memWin.map((m) => m.t - t0)} ys=${memWin.map((m) => m.total_mib)} x0=${x0} x1=${x1} y0=${mem ? Math.max(0, mem.total_mib - 100) : 0} y1=${mem ? mem.total_mib + 50 : 1} theme=${theme} title="PSS стека за 15 мин" />
      <//>

      <${Tile} title=${run.system === 'a0' ? 'Процессы' : 'Юниты'} level=${unitLevel} ageSec=${units ? T - units.t : null} testid="tile-units" info=${run.system === 'a0' ? 'Процессы стека лаборатории (демо, PicoClaw, драйверы двойника). Супервизии нет: упавшее поднимает человек.' : 'Юниты systemd neurobot-*, драйверы двойника — в счёте. Отказы — по расписанию стенда.'}>
        <div class="big"><${V} v=${units ? `${units.up} из ${units.total}` : DASH} /></div>
        <div class="sub">${!units ? 'нет данных' : units.down && units.down.length ? 'не работает: ' + units.down.join(', ') : 'все в работе'} · отказов стенда ${int(faults)}</div>
        <${Spark} kind="step" xs=${unitsWin.map((m) => m.t - t0)} ys=${unitsWin.map((m) => m.up)} x0=${x0} x1=${x1} y0=${0} y1=${units ? units.total : 1} theme=${theme} title="в работе за 15 мин" />
      <//>

      <${Tile} title="Касания · истина стенда" testid="tile-touch" info="Эпизоды касания по истине Gazebo (на роботе такой истины нет): контакты с перерывом короче 1 с — один эпизод. Пробег и время в движении — по истине, переносы робота не в счёт (metrics.travel). Линия — касания накопительно.">
        <div class="big"><${V} v=${stats ? int(stats.touches) : DASH} /></div>
        <div class="sub">пробег ${unit(stats ? stats.distance_m : null, 1, 'м')} · в движении ${stats ? `${unit(stats.moving_s, 0, 'с')} (${pct(stats.moving_share)})` : DASH}</div>
        <${Spark} kind="step" xs=${tx} ys=${ty} x0=${x0} x1=${x1} y1=${1} theme=${theme} title="касания накопительно за 15 мин" />
      <//>
    </div>`;
  const tape = html`<section class="panel tapebox" data-testid="tape-15">
      <h2><span>Жесты · 15 мин</span><span class="r legend">${['done', 'wrong', 'none', 'cut', 'false_action'].map((o) => html`<span><${GMark} outcome=${o} /> ${OUTCOMES[o].text}</span>`)}</span></h2>
      <${Tape} gestures=${gest} t0=${t0} x0=${x0} x1=${x1} labels=${false} />
    </section>`;
  return narrow
    ? html`<div class="ov narrow"><div class="col">${safety}</div><div class="col">${tiles}${tape}<div class="pair2">${glist}${events}</div></div></div>`
    : html`<div class="ov"><div class="col">${safety}${events}</div><div class="col">${tiles}${tape}${glist}</div></div>`;
}
