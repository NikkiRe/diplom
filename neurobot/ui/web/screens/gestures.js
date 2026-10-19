// Жесты: знак исхода (форма — исход, U4 §4.4) и лента за 10 мин. Исход — не тревога: знаки серые.
import { useEffect, useRef, useState } from 'preact/hooks';
import { html } from '../lib/ui.js';
import { num, unit, dur } from '../lib/fmt.js';

// круг — выполнен · ромб — другой манёвр · кольцо — нет реакции · полукруг — оборван · ромб-контур — вытеснен ·
// крест — движение без жеста · точка — не виден · квадрат пунктиром — исход ещё не ясен
export function shape(outcome, x, y, r = 5) {
  switch (outcome) {
    case 'done': return html`<circle cx=${x} cy=${y} r=${r} class="gm-fill" />`;
    case 'wrong': return html`<path d=${`M${x} ${y - r - 1} L${x + r + 1} ${y} L${x} ${y + r + 1} L${x - r - 1} ${y} Z`} class="gm-fill" />`;
    case 'none': return html`<circle cx=${x} cy=${y} r=${r - 0.5} class="gm-line" />`;
    case 'cut': return html`<g><circle cx=${x} cy=${y} r=${r - 0.5} class="gm-line" />
      <path d=${`M${x} ${y - r + 0.5} A${r - 0.5} ${r - 0.5} 0 0 0 ${x} ${y + r - 0.5} Z`} class="gm-fill" /></g>`;
    case 'preempted': return html`<path d=${`M${x} ${y - r} L${x + r} ${y} L${x} ${y + r} L${x - r} ${y} Z`} class="gm-line" />`;
    case 'false_action': return html`<path d=${`M${x - r + 1} ${y - r + 1} L${x + r - 1} ${y + r - 1} M${x + r - 1} ${y - r + 1} L${x - r + 1} ${y + r - 1}`} class="gm-line gm-thick" />`;
    case 'unobserved': return html`<circle cx=${x} cy=${y} r="1.8" class="gm-fill" />`;
    default: return html`<rect x=${x - r + 1} y=${y - r + 1} width=${2 * r - 2} height=${2 * r - 2} class="gm-line gm-dash" />`;
  }
}

export function GMark({ outcome }) {
  return html`<svg class="gm" viewBox="0 0 14 14" aria-hidden="true">${shape(outcome, 7, 7, 5)}</svg>`;
}

// лента: x — время (с от начала расписания), окно [x0, x1]; ширина — по контейнеру (текст не масштабируется),
// подпись жеста — только если помещается в своей дорожке (сверху и снизу по очереди) с зазором в знак; подписи шкалы
// и жестов прижаты к краям, а не обрезаны
export function Tape({ gestures, t0, x0, x1, labels = true, height }) {
  const box = useRef(null);
  const [W, setW] = useState(1000);
  useEffect(() => {
    const el = box.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(() => { if (el.clientWidth) setW(el.clientWidth); });
    ro.observe(el);
    setW(el.clientWidth || 1000);
    return () => ro.disconnect();
  }, []);
  const H = height || (labels ? 104 : 56), mid = labels ? 50 : 22;
  const X = (t) => 8 + ((t - t0 - x0) / Math.max(1e-9, x1 - x0)) * (W - 16);
  const ticks = [];
  const step = x1 - x0 > 1200 ? 300 : 60;
  for (let s = Math.ceil(x0 / step) * step; s <= x1; s += step) ticks.push(s);
  const vis = gestures.filter((g) => g.first - t0 >= x0 && g.first - t0 <= x1);
  const laneEnd = [-Infinity, -Infinity];
  const CH = 7.3, CT = 6.7;   // ширина знака моноширинного 12 и 11 px
  // центр подписи длиной n знаков шириной ch — не ближе полуширины к краю
  const clampX = (x, n, ch) => Math.min(W - (n * ch) / 2 - 2, Math.max((n * ch) / 2 + 2, x));
  return html`<div class="tapewrap" ref=${box}><svg class="tape" width=${W} height=${H} viewBox=${`0 0 ${W} ${H}`} role="img" aria-label="лента жестов">
    <line x1="0" x2=${W} y1=${mid} y2=${mid} class="tape-axis" />
    ${ticks.map((s) => html`<g><line x1=${X(t0 + s)} x2=${X(t0 + s)} y1=${mid - 4} y2=${mid + 4} class="tape-axis" />
      <text x=${clampX(X(t0 + s), dur(s, true).length, CT)} y=${H - 2} class="tape-t" text-anchor="middle">${dur(s, true)}</text></g>`)}
    ${vis.map((g, i) => {
      const x = X(g.first);
      const lane = i % 2;
      const up = lane === 0;
      const label = (g.kind === 'negative' ? 'без жеста' : (g.name || g.maneuver || '—')) + (g.conf != null ? ' ' + num(g.conf, 2) : '');
      const cx = clampX(x, label.length, CH);
      const half = (label.length * CH) / 2 + CH;
      const fits = labels && cx - half > laneEnd[lane];
      if (fits) laneEnd[lane] = cx + half;
      return html`<g>
        ${fits ? html`<line x1=${x} x2=${x} y1=${up ? mid - 9 : mid + 9} y2=${up ? mid - 14 : mid + 14} class="tape-axis" />
          <text x=${cx} y=${up ? mid - 18 : mid + 26} class="tape-l" text-anchor="middle">${label}</text>
          ${g.d_cmd_s != null ? html`<text x=${cx} y=${up ? mid - 31 : mid + 39} class="tape-d" text-anchor="middle">${unit(g.d_cmd_s, 2, 'с')}</text>` : ''}` : ''}
        ${shape(g.outcome, x, mid, 6)}
      </g>`;
    })}
  </svg></div>`;
}
