// Мелкие общие части экранов: знак тревоги, значение с единицей, возраст, подсказка «i», цвета темы, полоса
// состояний safety_gate.
import { h } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import htm from 'htm';
import { age as fmtAge, DASH } from './fmt.js';

export const html = htm.bind(h);

// приоритет кодируется дважды — цветом и формой с цифрой (ISA-101, WCAG 1.4.1): 1 — красный квадрат,
// 2 — оранжевый треугольник; снятая тревога — тот же знак контуром, без цвета (цвет — только у стоящих)
export function Sign({ prio, blink = false, off = false, title }) {
  const cls = 'sign' + (blink ? ' blink' : '') + (off ? ' off' : '');
  const t = title || (prio ? `приоритет ${prio}${off ? ', снята' : ''}` : 'норма');
  if (prio === 1) {
    return html`<svg class=${cls} viewBox="0 0 18 18" role="img" aria-label=${t}><title>${t}</title>
      <rect class="s1" x="1.5" y="1.5" width="15" height="15"/><text class="t1" x="9" y="13" text-anchor="middle">1</text></svg>`;
  }
  if (prio === 2) {
    return html`<svg class=${cls} viewBox="0 0 18 18" role="img" aria-label=${t}><title>${t}</title>
      <path class="s2" d="M9 1.2 L17.2 16.5 L0.8 16.5 Z"/><text class="t2" x="9" y="14.6" text-anchor="middle">2</text></svg>`;
  }
  return html`<svg class=${cls} viewBox="0 0 18 18" role="img" aria-label=${t}><title>${t}</title>
    <circle class="s0" cx="9" cy="9" r="4.5"/></svg>`;
}

// значение и единица: единица — только у значения, не у «—»; вместе не переносятся
export function V({ v, u, cls = '' }) {
  return html`<span class=${'num ' + cls}>${v}${v !== DASH && u ? html`<span class="u">${u}</span>` : ''}</span>`;
}

export function Age({ sec, stale }) {
  return html`<span class=${'num ' + (stale ? '' : 'muted')} title="возраст значения">${stale ? 'устарело ' : ''}${fmtAge(sec)}</span>`;
}

// пояснение к панели — во всплывающей подсказке, а не текстом на экране
export function Info({ text }) {
  return html`<span class="info" title=${text} aria-label=${'пояснение: ' + text} role="note">i</span>`;
}

// медиа-запрос как состояние: раскладка экрана под 1366×768 и под 1920×1080
export function useMedia(query) {
  const [on, setOn] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const f = () => setOn(mq.matches);
    mq.addEventListener('change', f);
    return () => mq.removeEventListener('change', f);
  }, [query]);
  return on;
}

// значение не старше max секунд на момент T, иначе null
export function fresh(m, T, max) {
  return m && T - m.t <= max ? m : null;
}

// жест входит в K1 (как metrics.latency): с командой, не ранний (реакция раньше t0), RTF двойника в норме
export function inK1(g) {
  return g.kind === 'gesture' && g.d_cmd_s != null && !g.early && g.rtf_ok !== false;
}

// цвета темы из токенов CSS — для canvas и uPlot
export function palette() {
  const s = getComputedStyle(document.documentElement);
  const g = (n) => s.getPropertyValue(n).trim();
  return {
    bg: g('--bg'), panel: g('--panel'), panel2: g('--panel2'), text: g('--text'), muted: g('--muted'),
    line: g('--line'), faint: g('--faint'), accent: g('--accent'), base: g('--base'),
    p1: g('--p1'), p2: g('--p2'), stOk: g('--st-ok'), stSlow: g('--st-slow'),
    font: "12px 'JetBrains Mono', ui-monospace, Menlo, monospace",
  };
}

function alarmColor(pal, level) {
  return level === 1 ? pal.p1 : level === 2 ? pal.p2 : null;
}

function stateColor(pal, state) {
  return state === 'ok' ? pal.stOk : state === 'slow' ? pal.stSlow : pal.base;
}

// отрезки состояния safety_state в [x0, x1] (время — от начала расписания); запись ≈1 Гц, перерыв > gap — «нет данных»
export function safetySegments(list, T, x0, x1, t0, gap = 2.5) {
  const out = [];
  for (let i = 0; i < list.length; i++) {
    const m = list[i];
    const a = m.t - t0;
    if (m.t > T) break;
    const nextT = i + 1 < list.length && list[i + 1].t <= T ? list[i + 1].t : T;
    const b = Math.min(nextT, m.t + gap) - t0;
    if (b < x0 || a > x1) continue;
    out.push([Math.max(a, x0), Math.min(b, x1), m.level, m.state]);
  }
  return out;
}

// полоса состояний на canvas: серое — норма; тревога — цвет и высота (P1 — во всю высоту, P2 — нижние 2/3, над ней —
// серое состояние): уровни различимы и без цвета; пусто — штриховка «нет данных»
export function drawBand(ctx, pal, segs, x0, x1, left, width, top, height) {
  ctx.save();
  ctx.fillStyle = pal.panel2;
  ctx.fillRect(left, top, width, height);
  ctx.strokeStyle = pal.faint;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let x = left - height; x < left + width; x += 6) { ctx.moveTo(x, top + height); ctx.lineTo(x + height, top); }
  ctx.save(); ctx.rect(left, top, width, height); ctx.clip(); ctx.stroke(); ctx.restore();
  const k = width / Math.max(1e-9, x1 - x0);
  for (const [a, b, level, state] of segs) {
    const xa = Math.floor(left + (a - x0) * k), xb = Math.ceil(left + (b - x0) * k);
    const w = Math.max(1, xb - xa);   // целые пиксели: без просветов между отрезками
    ctx.fillStyle = stateColor(pal, state);
    ctx.fillRect(xa, top, w, height);
    const c = alarmColor(pal, level);
    if (c) {
      const hh = level === 1 ? height : Math.round((height * 2) / 3);
      ctx.fillStyle = c;
      ctx.fillRect(xa, top + height - hh, w, hh);
    }
  }
  ctx.restore();
}

export function stateWord(state) {
  return { ok: 'OK', slow: 'SLOW', blocked: 'BLOCKED', estop: 'ESTOP' }[state] || DASH;
}

export const OUTCOMES = {
  done: { text: 'выполнен' },
  wrong: { text: 'другой манёвр' },
  none: { text: 'нет реакции' },
  cut: { text: 'оборван' },
  preempted: { text: 'вытеснен агентом' },
  unobserved: { text: 'не виден' },
  false_action: { text: 'движение без жеста' },
};
