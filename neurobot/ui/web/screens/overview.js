// «Обзор»: пространственная обстановка, шесть живых показателей и журнал наблюдений.
// Все числа и отметки берутся из текущей временной линии; скан не подменяется иллюстрацией в записях.
import { useEffect, useRef } from 'preact/hooks';
import { html, Sign, V, Age, Info, fresh, inK1, palette, safetySegments, stateWord, OUTCOMES } from '../lib/ui.js';
import { Spark } from '../lib/spark.js';
import { drawLidar } from '../lib/lidar.js';
import { num, unit, int, dur, pct, plural, DASH } from '../lib/fmt.js';
import { GMark, Tape } from './gestures.js';

const WIN = 900;   // 15 мин
const STALE = { safety: 2.5, out: 2, mem: 25, units: 25, scan: 1 };
const SHARE = ['ok', 'slow', 'blocked', 'estop'];

function MetricIcon({ kind }) {
  const paths = {
    'tile-k1': 'M4 13h4l3-8 3 14 3-9h3',
    'tile-speed': 'M5 17a8 8 0 1 1 14 0M12 12l4-4M8 17h8',
    'tile-camera': 'M4 7h4l2-3h4l2 3h4v12H4ZM16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
    'tile-k4': 'M8 4H5v6H2l6 10v-6h4M13 4h6v6h3l-6 10v-6h-3',
    'tile-llm': 'M5 4h14v12h-8l-6 4ZM8 8h8M8 12h5',
    'tile-mem': 'M7 7h10v10H7ZM4 8h3M4 12h3M4 16h3M17 8h3M17 12h3M17 16h3M8 4v3M12 4v3M16 4v3M8 17v3M12 17v3M16 17v3',
    'tile-units': 'M4 4h6v6H4ZM14 4h6v6h-6ZM4 14h6v6H4ZM14 14h6v6h-6Z',
    'tile-touch': 'M12 3v4M3 12h4M17 12h4M12 17v4M6 6l2 2M16 16l2 2M6 18l2-2M16 8l2-2M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  };
  return html`<svg class="ov-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d=${paths[kind] || paths['tile-units']} /></svg>`;
}

function Tile({ title, info, level = 0, ageSec, stale, children, testid, disconnected = false }) {
  return html`<section class=${'panel tile' + (level ? ' a' + level : '') + (stale ? ' stale' : '')} data-testid=${testid}>
    <h2><span class="ov-iconbox"><${MetricIcon} kind=${testid} /></span><span class="ov-tile-title">${title}</span>${level ? html`<${Sign} prio=${level} />` : ''}${info ? html`<${Info} text=${info} />` : ''}</h2>
    ${children}
    <div class="ov-tile-foot"><span>${disconnected ? 'Источник не подключён' : 'Последние 15 минут'}</span><span>${ageSec != null ? html`<${Age} sec=${ageSec} stale=${stale} />` : disconnected ? '' : 'телеметрия'}</span></div>
  </section>`;
}

function Lidar({ geo, safety, out, scan, hasSafety, theme }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current) return undefined;
    const draw = () => drawLidar(ref.current, { geo, safety, out, scan, pal: palette(), hasSafety, level: safety ? safety.level : 0 });
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(ref.current);
    return () => observer.disconnect();
  });
  return html`<canvas class="lidar" ref=${ref} role="img" data-testid="lidar" data-scan=${scan ? scan.r.filter((r) => r != null).length : 0}
    aria-label="мини-лидар: корпус, коридор, слепые секторы, зазор и тормозной путь"></canvas>`;
}

const SAFETY_TAIL = ' Корпус Ø0,39 м, коридор 0,49 м, поле лидара ±119,9° (сзади датчика нет), маска корпуса камеры, клин '
  + 'глубины 87°. Зазор — до ближайшей опасности в любую сторону; d_stop — тормозной путь safety_gate при скорости '
  + '/cmd_vel (умолчания safety_gate, допущение до замера на роботе). Серое — норма: slow с lidar_blind на роботе '
  + 'штатно, перед видит только глубина.';
const SAFETY_INFO = 'Мини-лидар по safety_state: скана в записи нет.' + SAFETY_TAIL;
const SAFETY_INFO_LIVE = 'Мини-лидар: точки — /scan шлюза (ближайшее попадание в каждом градусе пеленга, в базе робота '
  + 'по /tf_static, 5 Гц; не старше 1 с), поверх — сводка safety_state.' + SAFETY_TAIL;

function SafetyPanel({ run, tl, T, t0, geo, theme, dict, stats }) {
  const safety = tl.at('safety', T);
  const sAge = safety ? T - safety.t : null;
  const stale = sAge != null && sAge > STALE.safety;
  const out = fresh(tl.atWhere('cmd', T, (m) => m.topic === 'out'), T, STALE.out);
  const scan = fresh(tl.at('scan', T), T, STALE.scan);
  const level = safety ? safety.level : 0;
  const x0 = T - t0 - WIN, x1 = T - t0;
  const segs = safetySegments(tl.range('safety', T - WIN - 3, T), T, x0, x1, t0);
  const first = safety && safety.reasons.length ? safety.reasons[0] : null;
  const code = first ? String(first).split(':')[0].trim() : null;
  const share = stats && stats.safety_share;
  return html`<section class=${'panel safetybox' + (level ? ' a' + level : '') + (stale ? ' stale' : '')} data-testid="tile-safety">
    <h2><span class="ov-panel-dot"></span><span>Безопасность</span><${Info} text=${run.live ? SAFETY_INFO_LIVE : SAFETY_INFO} /><span class="r">${sAge != null ? html`<${Age} sec=${sAge} stale=${stale} />` : ''}</span></h2>
    ${run.has_safety ? html`
      <div class="ov-state"><div class="big" data-testid="state-word">${safety ? stateWord(safety.state) : DASH}</div>
        <span class=${'ov-state-pill' + (level ? ' priority' : '')}>${safety ? (stale ? 'Данные устарели' : level ? 'Тревога P' + level : 'Штатный режим') : 'Нет данных'}</span></div>
      <div class="reason">${code ? html`<span class="ov-reason-code">${code}</span><span>${(dict[code] || {}).text || String(first).split(':').slice(1).join(':').trim()}${safety.reasons.length > 1 ? html` <span class="muted">· ещё ${safety.reasons.length - 1}</span>` : ''}</span>` : html`<span class="muted">${safety ? 'Ограничений нет' : 'Ожидание safety_state'}</span>`}</div>`
      : html`<div class="ov-state"><div class="big muted">${DASH}</div><span class="ov-state-pill">Без safety_gate</span></div><div class="reason muted">Стек лаборатории пишет /cmd_vel прямо в twist_mux</div>`}
    <div class="ov-lidar-frame">
      <div class="ov-scope-meta"><span>Окружение · вид сверху</span><span class=${scan ? 'ov-scan-state active' : 'ov-scan-state'}>${run.live ? (scan ? 'СКАН / LIVE' : 'НЕТ СКАНА') : 'СВОДКА ЗАПИСИ'}</span></div>
      <${Lidar} geo=${geo} safety=${safety} out=${out} scan=${scan} hasSafety=${run.has_safety} theme=${theme} />
      <div class="ov-scope-caption"><span>Корпус Ø${num(2 * geo.robot_radius, 2)} м</span><span>${run.live ? (scan ? `${int(scan.r.filter((r) => r != null).length)} точек` : 'ожидание /scan') : 'скан в записи отсутствует'}</span></div>
    </div>
    ${run.has_safety ? html`<div class="ov-clearances"><div><span>Зазор до опасности</span><strong>${unit(safety ? safety.min_clearance_m : null, 2, 'м')}</strong></div><div><span>Тормозной путь <span class="num">d_stop</span></span><strong>${unit(out ? out.d_stop_m : null, 2, 'м')}</strong></div></div>
      <div class="legend ov-scope-legend"><span><i class="ov-key-gap"></i>зазор</span><span><i class="ov-key-stop"></i>d_stop</span><span><i class="ov-key-blind"></i>нет датчика</span></div>
      <div class="bandcap"><span>История безопасности</span><span>15 мин</span></div>
      <${Spark} kind="band" segs=${segs} x0=${x0} x1=${x1} theme=${theme} title="состояния safety_gate за 15 мин" />
      <div class="sub items ov-safety-share" data-testid="safety-share"><span class="ov-share-label">${run.live ? 'С запуска шлюза' : 'За прогон'}</span>${share ? SHARE.map((k) => html`<span>${k} <b>${pct(share[k] || 0)}</b></span>`) : DASH}</div>` : ''}
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

function Events({ tl, T, t0, live }) {
  const events = [...tl.until('fault', T).map((m) => ({ ...m, text: `отказ стенда: ${m.fault}`, tail: m.ttr_s != null ? `поднялся за ${unit(m.ttr_s, 1, 'с')}` : 'не поднялся' })),
    ...tl.until('operator', T).map((m) => ({ ...m, text: `человек: ${m.action}${m.what ? ' · ' + m.what : ''}`, tail: m.why || '' })),
    ...tl.until('touch', T).map((m) => ({ ...m, text: 'касание (истина стенда)', tail: (m.models || []).join(', ') }))]
    .sort((a, b) => b.t - a.t).slice(0, 30);
  return html`<section class="panel events" data-testid="events">
    <h2><span>${live ? 'Действия оператора' : 'Журнал событий'}</span><span class="r ov-count num">${int(events.length)}</span></h2>
    <div class="ov-panel-caption">${live ? 'События текущего подключения' : 'Стенд и действия человека'}</div>
    <div class="scroll">${events.length === 0 ? html`<div class="empty ov-empty"><svg viewBox="0 0 40 40" fill="none" stroke="currentColor" aria-hidden="true"><rect x="10" y="7" width="20" height="26" rx="4"/><path d="M15 15h10M15 20h10M15 25h6"/></svg><span>Событий пока нет</span><small>Здесь появятся действия и события стенда</small></div>` : events.map((e) => html`
      <div class="ev"><span class="ov-event-dot"></span><span class="muted num">T${dur(e.t - t0, true)}</span><span>${e.text}${e.tail ? html`<span class="ov-event-tail">${e.tail}</span>` : ''}</span></div>`)}</div>
  </section>`;
}

function LastGestures({ gest, t0, live }) {
  const last = gest.slice(-14).reverse();
  return html`<section class="panel glist" data-testid="last-gestures">
    <h2><span>Последние жесты</span><${Info} text=${live ? 'Δ — от конца жеста по perception (t_end) до первой ненулевой /cmd_vel; меряется, только если в t_end робот стоял. «защита» — исход оборвал safety_gate.' : 'Δ — задержка t0 → первая ненулевая /cmd_vel; «защита» — исход оборвал safety_gate.'} /><span class="r ov-count num">${int(gest.length)}</span></h2>
    <div class="ov-panel-caption">Распознавание и результат исполнения</div>
    <div class="scroll">${last.length === 0 ? html`<div class="empty ov-empty"><svg viewBox="0 0 40 40" fill="none" stroke="currentColor" aria-hidden="true"><path d="M13 25V13a3 3 0 0 1 6 0v8-13a3 3 0 0 1 6 0v13-8a3 3 0 0 1 6 0v12c0 7-4 11-11 11-5 0-8-3-11-7l-5-7a3 3 0 0 1 5-3l4 4"/></svg><span>Жестов ещё не было</span><small>Новые распознавания появятся здесь</small></div>` : html`<table class="gtab">
      <thead><tr><th>T</th><th></th><th>жест</th><th class="n">conf</th><th>исход</th><th class="n">Δ, с</th></tr></thead>
      <tbody>${last.map((g) => html`<tr>
        <td class="num muted">T${dur(g.first - t0, true)}</td><td><${GMark} outcome=${g.outcome} /></td>
        <td>${g.kind === 'negative' ? html`<span class="muted">без жеста: ${g.neg || ''}</span>` : (g.name || g.maneuver || DASH)}</td>
        <td class="n num">${num(g.conf, 2)}</td>
        <td><span class=${'ov-outcome outcome-' + (g.outcome || 'pending')}>${g.outcome ? OUTCOMES[g.outcome].text : 'ждём'}</span>${g.stopped_by ? html`<span class="muted"> · защита</span>` : ''}</td>
        <td class="n num">${num(g.d_cmd_s, 2)}</td></tr>`)}</tbody></table>`}</div>
  </section>`;
}

export function Overview({ run, tl, T, t0, geo, theme, bundle }) {
  const x0 = T - t0 - WIN, x1 = T - t0;
  const live = !!run.live;
  const k1 = tl.at('k1', T);
  const stats = tl.at('stats', T);
  const mem = tl.at('mem', T);
  const units = tl.at('units', T);
  const gest = tl.gestures(T).filter((g) => g.kind === 'gesture' || g.outcome === 'false_action');
  const llmHour = tl.range('llm', T - 3600, T);
  const tokHour = llmHour.reduce((s, m) => s + (m.prompt_tokens || 0) + (m.completion_tokens || 0), 0);
  const budget = geo.budget_mib;
  const rss = mem && mem.metric === 'rss';

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
  // у живого шлюза «с запуска» — по последним run_window жестам (кольцо): дальше подпись говорит, сколько их
  const runWord = !live ? 'прогон' : k1 && k1.run_window && k1.run.n >= k1.run_window ? `последних ${int(k1.run_window)}` : 'с запуска';

  const safety = html`<${SafetyPanel} run=${run} tl=${tl} T=${T} t0=${t0} geo=${geo} theme=${theme} dict=${bundle.alarms} stats=${stats} />`;
  const glist = html`<${LastGestures} gest=${gest} t0=${t0} live=${live} />`;
  const events = html`<${Events} tl=${tl} T=${T} t0=${t0} live=${live} />`;
  const k1Info = live
    ? 'p50 / p95 задержки «конец жеста по perception (t_end) → первая ненулевая /cmd_vel» по 20 последним жестам: t0, истины генератора жестов, на роботе нет, поэтому это не K1 двойника. Меряется, только если в t_end робот стоял. Точки — жесты за 15 мин, пунктир — 0,5 с (цель K1 стенда, здесь — ориентир).'
    : 'p50 / p95 задержки t0 → первая ненулевая /cmd_vel по 20 последним жестам; t0 — истина генератора жестов двойника (на роботе будет конец жеста по perception). Без ранних (реакция раньше t0) и без жестов при RTF < 0,95 — как K1 в metrics.py. Точки — жесты за 15 мин, пунктир — цель K1 p95 ≤ 0,5 с (стенд).';
  const k1Tile = html`<${Tile} title="Жест → команда" testid="tile-k1" info=${k1Info}>
        <div class="big"><${V} v=${k1 && k1.last.n ? `${num(k1.last.p50, 2)} / ${num(k1.last.p95, 2)}` : DASH} u="с" /></div>
        <div class="sub">${k1 ? `p50 / p95 · последних ${k1.last.n} · ${runWord} ${num(k1.run.p50, 2)} / ${num(k1.run.p95, 2)}` : 'команд на жесты ещё не было'}</div>
        <${Spark} kind="points" xs=${gWin.map((g) => g.first - t0)} ys=${gWin.map((g) => g.d_cmd_s)} x0=${x0} x1=${x1} target=${0.5} theme=${theme} title="задержка каждого жеста за 15 мин" />
      <//>`;
  const memTile = html`<${Tile} title=${rss ? 'Память стека · RSS' : 'Память стека'} testid="tile-mem" ageSec=${mem ? T - mem.t : null} stale=${mem && T - mem.t > STALE.mem} info=${`${rss ? 'RSS процессов юнитов стека из /proc (шлюз; PSS чужого пользователя без прав не читается — RSS считает общие библиотеки в каждом процессе, это верхняя оценка)' : 'PSS компонентов стека без драйверов двойника'} против бюджета стека с GNOME: ${num(budget.gnome, 0)} из ${num(budget['физическая'], 0)} МиБ (headless — ${num(budget.headless, 0)}), neurobot/sim/budget. CPU — доля одного ядра.`}>
        <div class="big"><${V} v=${mem ? num(mem.total_mib, 0) : DASH} u="МиБ" /></div>
        <div class="bar" title="против бюджета стека с GNOME"><i style=${{ width: `${mem ? Math.min(100, (100 * mem.total_mib) / budget.gnome) : 0}%` }}></i></div>
        <div class="sub">${mem ? pct(mem.total_mib / budget.gnome) : DASH} из ${num(budget.gnome, 0)} МиБ · CPU ${mem && mem.cpu_pct != null ? unit(mem.cpu_pct, 0, '%') : DASH}</div>
        <${Spark} kind="line" xs=${memWin.map((m) => m.t - t0)} ys=${memWin.map((m) => m.total_mib)} x0=${x0} x1=${x1} y0=${mem ? Math.max(0, mem.total_mib - 100) : 0} y1=${mem ? mem.total_mib + 50 : 1} theme=${theme} title="память стека за 15 мин" />
      <//>`;
  const unitsTile = html`<${Tile} title=${run.system === 'a0' ? 'Процессы' : 'Системные юниты'} level=${unitLevel} ageSec=${units ? T - units.t : null} stale=${units && T - units.t > STALE.units} testid="tile-units" info=${run.system === 'a0' ? 'Процессы стека лаборатории (демо, PicoClaw, драйверы двойника). Супервизии нет: упавшее поднимает человек.' : live ? 'Юниты systemd neurobot-* (systemctl show раз в 5 с), без самого пульта. Рестарты — NRestarts с запуска юнита.' : 'Юниты systemd neurobot-*, драйверы двойника — в счёте. Отказы — по расписанию стенда.'}>
        <div class="big"><${V} v=${units ? `${units.up} из ${units.total}` : DASH} /></div>
        <div class="sub">${!units ? 'нет данных' : units.down && units.down.length ? 'не работает: ' + units.down.join(', ') : 'все в работе'}${live ? (units && units.restarts ? ` · рестартов ${int(Object.values(units.restarts).reduce((a, b) => a + b, 0))}` : '') : ` · отказов стенда ${int(faults)}`}</div>
        <${Spark} kind="step" xs=${unitsWin.map((m) => m.t - t0)} ys=${unitsWin.map((m) => m.up)} x0=${x0} x1=${x1} y0=${0} y1=${units ? units.total : 1} theme=${theme} title="в работе за 15 мин" />
      <//>`;
  let tiles;
  if (live) {
    const outM = fresh(tl.atWhere('cmd', T, (m) => m.topic === 'out'), T, STALE.out);
    const reqM = fresh(tl.atWhere('cmd', T, (m) => m.topic === 'req'), T, STALE.out);
    const s = tl.at('safety', T);
    const lim = s && s.limits ? s.limits.v_max : null;
    const outWin = tl.range('cmd', T - WIN, T).filter((m) => m.topic === 'out');
    const health = tl.at('health', T);
    const healthWin = tl.range('health', T - WIN, T);
    const hLevel = health && (health.state === 'recovering' || (health.frame_age_s || 0) > 1) ? 2 : 0;
    tiles = html`<div class="tiles">
      ${k1Tile}
      <${Tile} title="Скорость · выдано" testid="tile-speed" ageSec=${outM ? T - outM.t : null} info="/cmd_vel — выход safety_gate (единственный писатель) против его предела вперёд (limits.v_max); req — запрос исполнителя /neurobot/cmd_vel_req. Доля времени в движении — с запуска шлюза. Линия — выдано за 15 мин.">
        <div class="big" data-testid="speed-out"><${V} v=${outM ? (lim != null ? `${num(outM.v, 2)} / ${num(lim, 2)}` : num(outM.v, 2)) : DASH} u="м/с" /></div>
        <div class="sub">запрос ${unit(reqM ? reqM.v : null, 2, 'м/с')} · в движении ${stats ? `${unit(stats.moving_s, 0, 'с')} (${pct(stats.moving_share)})` : DASH}</div>
        <${Spark} kind="step" xs=${outWin.map((m) => m.t - t0)} ys=${outWin.map((m) => Math.max(0, m.v))} x0=${x0} x1=${x1} y1=${0.25} theme=${theme} title="выдано вперёд за 15 мин" />
      <//>
      <${Tile} title="Камера и распознавание" testid="tile-camera" level=${hLevel} ageSec=${health ? T - health.t : null} info="/neurobot/perception_health: кадры в секунду, возраст кадра, восстановления камеры; состояние ok — распознаёт, paused — пауза в движении, recovering — камера восстанавливается (тревога P2). Линия — кадры/с за 15 мин.">
        <div class="big"><${V} v=${health && health.fps != null ? num(health.fps, 1) : DASH} u="к/с" /></div>
        <div class="sub">${health ? `${health.state} · кадр ${unit(health.frame_age_s, 2, 'с')} · восстановлений ${int(health.recoveries)}` : 'нет данных'}</div>
        <${Spark} kind="line" xs=${healthWin.map((m) => m.t - t0)} ys=${healthWin.map((m) => m.fps || 0)} x0=${x0} x1=${x1} y1=${15} theme=${theme} title="кадры в секунду за 15 мин" />
      <//>
      <${Tile} title="LLM за час" testid="tile-llm" disconnected=${true} info="В живом режиме вызовы LLM пульт не читает: EdgeCtx к роботу ещё не подключён (Э3), у PicoClaw своего счёта нет — токены считает учётный прокси mockllm на стенде. Числа ВКР по LLM — в «Записи».">
        <div class="big"><${V} v=${DASH} /></div>
        <div class="sub">не подключено: EdgeCtx — после Э3</div>
        <div class="ov-disconnected"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><path d="M8 3v4M16 3v4M6 7h12v4a6 6 0 0 1-12 0ZM12 17v4M3 3l18 18" /></svg><span>Данные будут доступны после подключения</span></div>
      <//>
      ${memTile}
      ${unitsTile}
    </div>`;
  } else {
    tiles = html`<div class="tiles">
      ${k1Tile}
      <${Tile} title="LLM на жест" testid="tile-k4" info="K4: вызовы LLM вне реплик человека (не на реплику и не служебные вызовы хода) за прогон. Рефлекс жестов работает без LLM, цель — 0. Линия — такие вызовы по минутам, черточки внизу — жесты.">
        <div class="big"><${V} v=${stats && stats.llm ? int(stats.llm.gesture) : DASH} /></div>
        <div class="sub">вызовов вне реплик за прогон · цель 0</div>
        <${Spark} kind="step" xs=${gx} ys=${gy} ticks=${gTicks} x0=${x0} x1=${x1} y1=${1} theme=${theme} title="вызовы LLM на жест по минутам и жесты за 15 мин" />
      <//>

      <${Tile} title="LLM за час" testid="tile-llm" info=${`Вызовы LLM этой системы за последний час записи: на реплики человека, служебные и на жест. ₽ факт — по счёту провайдера (в записи mockllm — 0 ₽), ₽ оценка — по замороженному прайсу RouterAI 06.10.2026 для ${run.model}. Столбцы — вызовы по минутам.`}>
        <div class="big"><${V} v=${int(nCalls)} u=${plural(nCalls, 'вызов', 'вызова', 'вызовов')} /></div>
        <div class="sub">${int(tokHour)} ток. · ₽ ${num(sum(llmHour, 'rub_fact'), 2)} факт · ${num(sum(llmHour, 'rub_est'), 2)} оценка</div>
        <${Spark} kind="bars" xs=${mx} ys=${my} x0=${x0} x1=${x1} theme=${theme} title="вызовы LLM по минутам за 15 мин" />
      <//>
      ${memTile}
      ${unitsTile}
      <${Tile} title="Касания · истина стенда" testid="tile-touch" info="Эпизоды касания по истине Gazebo (на роботе такой истины нет): контакты с перерывом короче 1 с — один эпизод. Пробег и время в движении — по истине, переносы робота не в счёт (metrics.travel). Линия — касания накопительно.">
        <div class="big"><${V} v=${stats ? int(stats.touches) : DASH} /></div>
        <div class="sub">пробег ${unit(stats ? stats.distance_m : null, 1, 'м')} · в движении ${stats ? `${unit(stats.moving_s, 0, 'с')} (${pct(stats.moving_share)})` : DASH}</div>
        <${Spark} kind="step" xs=${tx} ys=${ty} x0=${x0} x1=${x1} y1=${1} theme=${theme} title="касания накопительно за 15 мин" />
      <//>
    </div>`;
  }
  const tape = html`<section class="panel tapebox" data-testid="tape-15">
      <h2><span>Лента жестов</span><span class="ov-tape-period">15 минут</span><span class="r legend">${['done', 'wrong', 'none', 'cut', 'false_action'].map((o) => html`<span><${GMark} outcome=${o} /> ${OUTCOMES[o].text}</span>`)}</span></h2>
      <${Tape} gestures=${gest} t0=${t0} x0=${x0} x1=${x1} labels=${false} />
    </section>`;
  return html`<div class="ov ov-dashboard"><div class="ov-hero">${safety}<div class="ov-telemetry"><div class="ov-section-label"><span>Показатели системы</span><span>Окно трендов · 15 мин</span></div>${tiles}</div></div>${tape}<div class="ov-activity">${glist}${events}</div></div>`;
}
