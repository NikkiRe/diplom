// Спарклайн плитки «Обзора» на canvas: ряд за 15 мин и целевая линия пунктиром; полоса состояний — drawBand.
import { useEffect, useRef } from 'preact/hooks';
import { html, palette, drawBand } from './ui.js';

// kind: 'line' | 'step' | 'points' | 'bars' | 'band'; xs, ys — время (с от начала расписания) и значения;
// ticks — отметки событий (жестов) у нижнего края, тонкими чертами
export function Spark({ kind = 'line', xs = [], ys = [], x0, x1, y0 = 0, y1, target, ticks = [], segs, theme, title }) {
  const ref = useRef(null);
  useEffect(() => {
    const c = ref.current;
    if (!c) return;
    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth, h = c.clientHeight;
    if (!w || !h) return;
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
    const ctx = c.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const pal = palette();
    ctx.clearRect(0, 0, w, h);
    if (kind === 'band') {
      drawBand(ctx, pal, segs || [], x0, x1, 0, w, 0, h);
      return;
    }
    const top = Math.max(y1 ?? 0, ...(target != null ? [target * 1.15] : []), ...ys.filter(Number.isFinite)) || 1;
    const X = (x) => ((x - x0) / Math.max(1e-9, x1 - x0)) * (w - 2) + 1;
    const Y = (y) => h - 3 - ((y - y0) / Math.max(1e-9, top - y0)) * (h - 8);
    ctx.strokeStyle = pal.faint; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, h - 2.5); ctx.lineTo(w, h - 2.5); ctx.stroke();
    if (target != null) {
      ctx.strokeStyle = pal.text; ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(0, Y(target)); ctx.lineTo(w, Y(target)); ctx.stroke(); ctx.setLineDash([]);
    }
    ctx.strokeStyle = pal.base; ctx.fillStyle = pal.base; ctx.lineWidth = 1.5;
    if (kind === 'line' || kind === 'step') {
      ctx.beginPath();
      let pen = false, py = 0;
      for (let i = 0; i < xs.length; i++) {
        if (!Number.isFinite(ys[i])) { pen = false; continue; }
        const x = X(xs[i]), y = Y(ys[i]);
        if (pen && kind === 'step') ctx.lineTo(x, py);
        pen ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        pen = true; py = y;
      }
      ctx.stroke();
    } else if (kind === 'points') {
      for (let i = 0; i < xs.length; i++) {
        if (!Number.isFinite(ys[i])) continue;
        ctx.beginPath(); ctx.arc(X(xs[i]), Y(ys[i]), 2.2, 0, 2 * Math.PI); ctx.fill();
      }
    } else if (kind === 'bars') {
      const bw = Math.max(1, ((w - 2) / Math.max(1, (x1 - x0) / 60)) * 0.8);
      for (let i = 0; i < xs.length; i++) if (ys[i] > 0) ctx.fillRect(X(xs[i]) - bw / 2, Y(ys[i]), bw, h - 3 - Y(ys[i]));
    }
    ctx.fillStyle = pal.muted;
    for (const x of ticks) ctx.fillRect(Math.round(X(x)), h - 7, 1, 5);
  });
  return html`<canvas class="spark" ref=${ref} role="img" aria-label=${title || 'тренд за 15 мин'}></canvas>`;
}
