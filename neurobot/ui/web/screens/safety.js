// «Безопасность и жесты» (L2): что с безопасностью и почему, и что стало с жестами.
// Состояние и причины кодом и текстом; цепочка скорости req → out; зазор против тормозного пути; полоса
// состояний и лента жестов за 10 мин. Защёлку из пульта снять нельзя — только человек у робота (sudo, TTY).
import { useMemo } from 'preact/hooks';
import { html, Sign, V, Age, Info, fresh, safetySegments, stateWord, OUTCOMES } from '../lib/ui.js';
import { Chart, Key } from '../lib/charts.js';
import { num, unit, int, dur, clock, nbsp, DASH } from '../lib/fmt.js';
import { GMark, Tape } from './gestures.js';

const WIN = 600;       // 10 мин
const STALE_CMD = 2;   // скорость старше — устарела: «—» и на шкале, и в тормозном пути

function reasonPrio(dict, code, state) {
  const c = dict[code];
  if (!c) return 2;
  if (c.only_stopped && state !== 'blocked' && state !== 'estop') return 0;
  return c.prio;
}

function StatePanel({ run, safety, sAge, dict, T }) {
  if (!run.has_safety) {
    return html`<section class="panel" data-testid="state"><h2><span>Состояние safety_gate</span><${Info} text="У A0 (стек лаборатории) safety_gate нет: демо пишет /cmd_vel прямо в twist_mux. Касания этого прогона — по истине стенда (экраны «Обзор», «Агент и тренды»)." /></h2>
      <div class="big muted">${DASH}</div><div class="sub">safety_gate нет</div></section>`;
  }
  const level = safety ? safety.level : 0;
  const stale = sAge != null && sAge > 2.5;
  const est = safety && safety.estop;
  return html`<section class=${'panel' + (level ? ' a' + level : '') + (stale ? ' stale' : '')} data-testid="state">
    <h2>${level ? html`<${Sign} prio=${level} />` : ''}<span>Состояние safety_gate</span><span class="r">${sAge != null ? html`<${Age} sec=${sAge} stale=${stale} />` : ''}</span></h2>
    <div class="row"><div class="big">${safety ? stateWord(safety.state) : DASH}</div>
      <div class="sub">${safety ? (level ? 'тревога P' + level : 'норма') : ''}</div></div>
    <ul class="reasons">
      ${safety && safety.reasons.length ? safety.reasons.map((r) => {
        const code = String(r).split(':')[0].trim();
        const text = String(r).slice(code.length + 1).trim();
        const p = reasonPrio(dict, code, safety.state);
        return html`<li><${Sign} prio=${p} /><span><code>${code}</code> <span class=${p ? '' : 'muted'}>${nbsp(text || (dict[code] || {}).text || '')}</span></span></li>`;
      }) : html`<li class="muted">причин нет</li>`}
    </ul>
    ${safety && safety.warnings && safety.warnings.length ? html`<div class="sub muted">предупреждения: ${safety.warnings.join('; ')}</div>` : ''}
    ${est ? html`<div class="latch" data-testid="latch">
      <div><b>Защёлка</b> с ${clock(est.since)} · стоит ${dur(T - est.since)} · ${est.reasons.join(', ')}</div>
      <div class="sub">Снимает только человек у робота: <code>sudo neurobot estop release</code> <button onClick=${() => { try { navigator.clipboard.writeText('sudo neurobot estop release'); } catch (e) { /* нет буфера */ } }}>копировать</button></div>
    </div>` : ''}
  </section>`;
}

function Scale({ lo, hi, req, out, lim, unitName }) {
  const P = (v) => `${((Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo)) * 100}%`;
  return html`<div class="scale" title=${`шкала ${num(lo, 2)}…${num(hi, 2)} ${unitName}`}>
    <span class="z" style=${{ left: P(0) }}></span>
    ${lim != null ? html`<span class="lim" style=${{ left: P(lim) }}></span>` : ''}
    ${req != null ? html`<span class="m req" style=${{ left: P(req) }}></span>` : ''}
    ${out != null ? html`<span class="m out" style=${{ left: P(out) }}></span>` : ''}
  </div>`;
}

function ChainPanel({ run, safety, req, out, rAge, oAge, geo }) {
  const lim = safety && safety.limits ? safety.limits : null;
  const dv = req && out ? req.v - out.v : null;
  const info = `○ — запрошено (/neurobot/cmd_vel_req, исполнитель${run.system === 'a0' ? '; у A0 нет' : ''}), ■ — выдано (/cmd_vel, ${run.system === 'a0' ? 'демо пишет прямо' : 'safety_gate — единственный писатель'}), черта — предел (в живом режиме). `
    + `Пределы safety_gate: вперёд ≤ ${num(geo.max_speed, 2)}, в слепом секторе ≤ ${num(geo.blind_speed, 2)}, назад ≤ ${num(geo.reverse_speed, 2)} м/с. Скорость старше 2 с — «—».`;
  return html`<section class="panel" data-testid="chain">
    <h2><span>Скорость · запрошено → выдано</span><${Info} text=${info} /></h2>
    <div class="chain">
      <span class="muted">v</span>
      <${Scale} lo=${-0.1} hi=${0.3} req=${req ? req.v : null} out=${out ? out.v : null} lim=${lim ? lim.v_max : null} unitName="м/с" />
      <span class="num">${num(req ? req.v : null, 2)} → <b>${num(out ? out.v : null, 2)}</b> <span class="u">м/с</span></span>
      <span class="muted">w</span>
      <${Scale} lo=${-1} hi=${1} req=${req ? req.w : null} out=${out ? out.w : null} lim=${lim ? lim.w_max : null} unitName="рад/с" />
      <span class="num">${num(req ? req.w : null, 2)} → <b>${num(out ? out.w : null, 2)}</b> <span class="u">рад/с</span></span>
    </div>
    <div class="kv sub num">
      <span class="k">возраст</span><span>req ${rAge != null ? unit(rAge, 1, 'с') : DASH} · out ${oAge != null ? unit(oAge, 1, 'с') : DASH}</span>
      <span class="k">предел</span><span title="пределы limits в записи не сохранялись — только в живом режиме">${lim ? `v ≤ ${num(lim.v_max, 2)} · назад ≤ ${num(lim.v_rev, 2)} · w ≤ ${num(lim.w_max, 2)}` : DASH}</span>
      <span class="k">req − out</span><span>${dv != null ? unit(dv, 2, 'м/с') + (Math.abs(dv) > 0.005 ? ' — срез' : '') : DASH}</span>
    </div>
  </section>`;
}

function ClearancePanel({ safety, out, geo }) {
  const c = safety ? safety.min_clearance_m : null;
  const d = out ? out.d_stop_m : null;
  const MAX = 1.5;
  const P = (v) => `${(Math.min(MAX, Math.max(0, v)) / MAX) * 100}%`;
  const spare = c != null && d != null ? c - d : null;
  const info = `Зазор — до ближайшей опасности в любую сторону, от края корпуса (min_clearance_m). d_stop(v) = v·${num(geo.t_lat, 2)} + v²/(2·${num(geo.a_brake, 1)}) + ${num(geo.margin, 2)} — умолчания safety_gate, допущение до замера на роботе. Путь вперёд (front_m) в записи не сохранялся. Шкала 0…1,5 м.`;
  return html`<section class="panel" data-testid="clearance">
    <h2><span>Зазор против тормозного пути</span><${Info} text=${info} /></h2>
    <div class="pair">
      <div><div class="mid"><${V} v=${num(c, 2)} u="м" /></div><div class="sub">зазор</div></div>
      <div><div class="mid"><${V} v=${num(d, 2)} u="м" /></div><div class="sub">d_stop${out ? ` при ${unit(Math.max(0, out.v), 2, 'м/с')}` : ''}</div></div>
      <div><div class="mid"><${V} v=${num(spare, 2)} u="м" /></div><div class="sub">запас</div></div>
    </div>
    <div class="bar" title="0…1,5 м от края корпуса: полоса — зазор, черта — d_stop">
      ${c != null ? html`<i style=${{ width: P(c) }}></i>` : ''}
      ${d != null ? html`<b style=${{ left: P(d) }}></b>` : ''}
    </div>
  </section>`;
}

export function SafetyScreen({ run, tl, T, t0, geo, theme, bundle }) {
  const safety = tl.at('safety', T);
  const sAge = safety ? T - safety.t : null;
  const reqM = tl.atWhere('cmd', T, (m) => m.topic === 'req');
  const outM = tl.atWhere('cmd', T, (m) => m.topic === 'out');
  const req = fresh(reqM, T, STALE_CMD), out = fresh(outM, T, STALE_CMD);
  const x0 = T - t0 - WIN, x1 = T - t0;
  const xRange = () => [x0, x1];
  const sec = Math.floor(T);

  const chartData = useMemo(() => {
    const s = tl.range('safety', T - WIN - 3, T).filter((m) => m.min_clearance_m != null);
    const o = tl.range('cmd', T - WIN - 3, T).filter((m) => m.topic === 'out');
    const xs = [...new Set([...s.map((m) => m.t), ...o.map((m) => m.t)])].sort((a, b) => a - b);
    const cl = [], ds = [];
    let i = 0, j = 0, lc = null, ld = null;
    for (const t of xs) {
      while (i < s.length && s[i].t <= t) lc = s[i++].min_clearance_m;
      while (j < o.length && o[j].t <= t) ld = o[j++].d_stop_m;
      cl.push(lc); ds.push(ld);
    }
    return [xs.map((t) => t - t0), cl, ds];
  }, [run, sec]);
  const spec = useMemo(() => ({
    key: 'clear-' + run.id, height: 140, yLabel: 'м', yRange: (u, lo, hi) => [0, Math.max(0.5, Math.min(1.6, (hi || 0) * 1.1))],
    series: [
      { label: 'зазор', color: (p) => p.text, paths: 'step', fmt: (v) => unit(v, 2, 'м') },
      { label: 'd_stop', color: (p) => p.text, dash: [5, 4], paths: 'step', fmt: (v) => unit(v, 2, 'м') },
    ],
  }), [run]);
  const bandData = useMemo(() => [[x0, x1], [null, null]], [run, sec]);
  const bandSpec = useMemo(() => ({
    key: 'sband-' + run.id, height: 26, axes: false, bandFull: true, padding: [3, 0, 3, 0],
    series: [{ label: 'safety_state', color: (p) => p.line }],
    band: (a, b) => (run.has_safety ? safetySegments(tl.range('safety', t0 + a - 3, Math.min(T, t0 + b)), T, a, b, t0) : []),
  }), [run, sec]);

  const gestures = tl.gestures(T).filter((g) => g.kind === 'gesture' || g.outcome === 'false_action');
  const shown = gestures.slice(-8).reverse();
  const stats = tl.at('stats', T);
  const done = gestures.filter((g) => g.kind === 'gesture');
  const cnt = (o) => done.filter((g) => g.outcome === o).length;
  const bySafety = done.filter((g) => g.stopped_by).length;
  const recNone = done.filter((g) => g.outcome && (g.name === 'unknown' || g.name === 'none' || g.cls === -1)).length;
  return html`<div class="grid">
    <div class="grid sg">
      <${StatePanel} run=${run} safety=${safety} sAge=${sAge} dict=${bundle.alarms} T=${T} />
      <${ChainPanel} run=${run} safety=${safety} req=${req} out=${out} rAge=${reqM ? T - reqM.t : null} oAge=${outM ? T - outM.t : null} geo=${geo} />
      <${ClearancePanel} safety=${safety} out=${out} geo=${geo} />
    </div>
    <section class="panel" data-testid="band-chart">
      <h2><span>Зазор и тормозной путь · состояния safety_gate · 10 мин</span><${Info} text="Полоса: серое — норма, цвет и высота — уровень тревоги (P1 — во всю высоту, P2 — на две трети), штриховка — нет данных." />
        <span class="r legend"><${Key}>зазор<//><${Key} kind="dash">d_stop<//></span></h2>
      <${Chart} spec=${spec} data=${chartData} theme=${theme} xRange=${xRange} />
      <${Chart} spec=${bandSpec} data=${bandData} theme=${theme} xRange=${xRange} />
    </section>
    <section class="panel" data-testid="tape">
      <h2><span>Лента жестов · 10 мин</span><span class="r legend">
        ${['done', 'wrong', 'none', 'cut', 'false_action'].map((o) => html`<span><${GMark} outcome=${o} /> ${OUTCOMES[o].text}</span>`)}</span></h2>
      <${Tape} gestures=${gestures} t0=${t0} x0=${x0} x1=${x1} />
      <div class="counts num">
        <span>жестов <b>${int(done.length)}</b></span><span>выполнено <b>${int(cnt('done'))}</b></span><span>другой манёвр <b>${int(cnt('wrong'))}</b></span>
        <span>нет реакции <b>${int(cnt('none'))}</b></span><span>оборвано <b>${int(cnt('cut'))}</b></span><span>остановила защита <b>${int(bySafety)}</b></span>
        <span>«не распознано» <b>${int(recNone)}</b></span>
        <span>движений без жеста <b>${int(gestures.filter((g) => g.outcome === 'false_action').length)}</b></span>
        <span class="sp"></span><span>LLM на жест <b>${stats ? int(stats.llm.gesture) : DASH}</b></span>
      </div>
    </section>
    <section class="panel" data-testid="gesture-table">
      <h2><span>Жесты · последние 8</span><${Info} text="Δрасп = t_rec − t_end (perception: конец жеста → распознан). Δкоманда = t0 → первая ненулевая /cmd_vel (t0 — истина двойника). «ранний» — реакция раньше t0, в K1 не входит." /></h2>
      <table>
        <thead><tr><th>T</th><th></th><th>ожидался</th><th>распознан</th><th class="n">conf</th><th class="n">Δрасп, с</th><th class="n">Δкоманда, с</th><th>манёвр</th><th>исход</th></tr></thead>
        <tbody>${shown.map((g) => html`<tr>
          <td class="num muted">T${dur(g.first - t0, true)}</td><td><${GMark} outcome=${g.outcome} /></td>
          <td>${g.kind === 'negative' ? html`<span class="muted">без жеста: ${g.neg}</span>` : g.expected || DASH}</td>
          <td>${g.name || (run.system === 'a0' ? html`<span class="muted" title="стек лаборатории не публикует /neurobot/gesture">${DASH}</span>` : DASH)}</td>
          <td class="n num">${num(g.conf, 2)}</td><td class="n num">${num(g.d_rec_s, 2)}</td>
          <td class="n num">${num(g.d_cmd_s, 2)}${g.early ? html` <span class="tag" title="реакция раньше t0: в K1 не входит">ранний</span>` : ''}</td>
          <td>${g.maneuver || DASH}</td>
          <td>${g.outcome ? (OUTCOMES[g.outcome] || {}).text : html`<span class="muted">ждём</span>`}${g.stopped_by ? html` <span class="muted">· защита: ${g.stopped_by.join(', ')}</span>` : ''}</td>
        </tr>`)}</tbody>
      </table>
      ${shown.length === 0 ? html`<div class="empty">жестов ещё не было</div>` : ''}
    </section>
  </div>`;
}
