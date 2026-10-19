// Тренды на uPlot: без анимаций и сглаживания, курсоры графиков синхронны, целевые линии пунктиром с подписью справа
// от области построения. Легенда — статичная строка над графиком (экран), значения — во всплывающей подсказке у курсора.
// Ось x — секунды от начала расписания прогона (T), подписи «+м:сс». Ширина осей y у всех графиков одна, поэтому
// области построения совпадают и полоса состояний ложится ровно под графики.
import { useEffect, useRef } from 'preact/hooks';
import uPlot from 'uplot';
import { html, palette, drawBand } from './ui.js';
import { dur, num } from './fmt.js';

export const AXIS_W = 64;
const LABEL_W = 14;
const X_INCRS = [1, 5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600];
const FONT = "11px 'JetBrains Mono', ui-monospace, monospace";

// горизонтальные целевые линии: [{y, label, color(pal)}]; подпись — в пустом правом поле на высоте линии
export function targetsPlugin(getTargets, scale = 'y') {
  return {
    hooks: {
      draw: [(u) => {
        const pal = palette();
        const ctx = u.ctx;
        const dpr = devicePixelRatio;
        const { left, top, width, height } = u.bbox;
        ctx.save();
        ctx.font = `${11 * dpr}px 'JetBrains Mono', ui-monospace, monospace`;
        ctx.textBaseline = 'middle';
        const shown = getTargets().map((t) => ({ ...t, py: Math.round(u.valToPos(t.y, scale, true)) }))
          .filter((t) => t.py >= top && t.py <= top + height).sort((a, b) => a.py - b.py);
        let ly = -Infinity;
        for (const t of shown) {
          const c = t.color ? t.color(pal) : pal.text;
          ctx.strokeStyle = c;
          ctx.lineWidth = 1.5 * dpr;
          ctx.setLineDash([6 * dpr, 4 * dpr]);
          ctx.beginPath(); ctx.moveTo(left, t.py); ctx.lineTo(left + width, t.py); ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = c;
          ly = Math.max(t.py, ly + 13 * dpr);      // близкие линии — подписи друг под другом, не внахлёст
          ctx.fillText(t.label, left + width + 6 * dpr, ly);
        }
        ctx.restore();
      }],
    },
  };
}

// полоса safety_state на всю высоту области построения (отдельная строка под графиками): getSegs(x0, x1) → [[a, b, level, state]]
export function bandPlugin(getSegs) {
  return {
    hooks: {
      drawClear: [(u) => {
        const pal = palette();
        const { left, top, width, height } = u.bbox;
        const x0 = u.scales.x.min, x1 = u.scales.x.max;
        drawBand(u.ctx, pal, getSegs(x0, x1), x0, x1, left, width, top, height);
      }],
    },
  };
}

// подсказка у курсора: T и значения рядов — только на графике под указателем (курсоры остальных идут синхронно)
function tooltipPlugin(fmts) {
  let tip = null, inside = false;
  return {
    hooks: {
      init: [(u) => {
        tip = document.createElement('div');
        tip.className = 'u-tip';
        tip.hidden = true;
        u.over.appendChild(tip);
        u.over.addEventListener('mouseenter', () => { inside = true; });
        u.over.addEventListener('mouseleave', () => { inside = false; tip.hidden = true; });
      }],
      setCursor: [(u) => {
        const i = u.cursor.idx;
        if (!inside || i == null) { tip.hidden = true; return; }
        const rows = [`T${dur(u.data[0][i], true)}`];
        u.series.forEach((s, k) => {
          const v = k > 0 ? u.data[k][i] : null;
          if (v != null) rows.push(`${s.label}: ${fmts[k - 1](v)}`);
        });
        tip.replaceChildren(...rows.map((r) => { const d = document.createElement('div'); d.textContent = r; return d; }));
        tip.hidden = false;
        const x = u.cursor.left, w = u.over.clientWidth;
        tip.style.left = `${Math.min(x + 12, Math.max(0, w - tip.offsetWidth - 4))}px`;
        tip.style.top = `${Math.max(0, u.cursor.top - tip.offsetHeight - 8)}px`;
      }],
    },
  };
}

function axes(pal, s) {
  const { yLabel, yFmt, y2 } = s;
  const base = { stroke: pal.muted, font: FONT, labelFont: FONT, ticks: { stroke: pal.line, width: 1, size: 4 },
    grid: { stroke: pal.faint, width: 1 } };
  const x = { ...base, size: 26, values: (u, splits) => splits.map((v) => dur(v, true)), space: 80, incrs: X_INCRS };
  const out = [
    // xAxis: false — подписи времени только у нижнего графика пары (сетка остаётся)
    s.xAxis === false ? { ...x, size: 4, values: () => [], ticks: { show: false } } : x,
    { ...base, size: AXIS_W, label: yLabel || ' ', labelSize: LABEL_W, values: (u, splits) => splits.map(yFmt || ((v) => num(v, 2))),
      ...(s.ySpace ? { space: s.ySpace } : {}) },
  ];
  // правая ось есть всегда (пустая без y2 — там подписи целей): области построения всех графиков совпадают
  out.push(y2 ? { ...base, scale: 'y2', side: 1, size: AXIS_W, grid: { show: false }, label: y2.label, labelSize: LABEL_W,
    values: (u, splits) => splits.map(y2.fmt || ((v) => num(v, 0))) } : blankAxis(1));
  return out;
}

// пустая ось той же ширины, что ось с подписью (size + labelSize)
function blankAxis(side) {
  return { scale: 'y', side, size: AXIS_W + LABEL_W, values: () => [], grid: { show: false }, ticks: { show: false }, stroke: 'transparent' };
}

// точки: квадрат-контур для рядов «на жест», круг для остальных
function points(x, pal) {
  if (!x.points) return { show: false };
  const c = x.color(pal);
  return x.hollow ? { show: true, size: x.points, stroke: c, fill: pal.panel, width: 1.5 }
    : { show: true, size: x.points, stroke: c, fill: c, width: 1 };
}

// spec: {height, yLabel, yFmt, ySpace?, xAxis?, y2?, series: [{label, color(pal), width?, dash?, points?, hollow?, paths?,
//        scale?, fill?, fmt?}], targets?: () => [...], band?: (x0, x1) => segs, yRange?: [lo, hi] | fn, padding?}
export function Chart({ spec, data, theme, sync = 'nb', xRange, testid }) {
  const box = useRef(null);
  const plot = useRef(null);
  const specRef = useRef(spec);
  specRef.current = spec;
  const xr = useRef(xRange);
  xr.current = xRange;

  useEffect(() => {
    const pal = palette();
    const s = specRef.current;
    const fmts = s.series.map((x) => x.fmt || ((y) => num(y, 2)));
    const series = [{ label: 'T' }].concat(s.series.map((x) => ({
      label: x.label, stroke: x.color(pal), width: x.width || 1.5, dash: x.dash, scale: x.scale || 'y',
      fill: x.fill ? x.fill(pal) : undefined, spanGaps: false, points: points(x, pal),
      paths: x.paths === 'none' ? () => null : x.paths === 'bars' ? uPlot.paths.bars({ size: [0.9, 50] })
        : x.paths === 'step' ? uPlot.paths.stepped({ align: 1 }) : undefined,
    })));
    const plugins = [];
    if (s.targets) plugins.push(targetsPlugin(() => specRef.current.targets()));
    if (s.band) plugins.push(bandPlugin((a, b) => specRef.current.band(a, b)));
    else plugins.push(tooltipPlugin(fmts));
    const opts = {
      width: box.current.clientWidth || 600, height: s.height || 160, ...(s.padding ? { padding: s.padding } : {}),
      cursor: { sync: { key: sync }, drag: { x: false, y: false }, points: { size: 6 } },
      legend: { show: false },
      scales: {
        x: { time: false, range: () => (xr.current ? xr.current() : [0, 1]) },
        y: { range: s.yRange || ((u, lo, hi) => [Math.min(0, lo ?? 0), hi == null || hi <= 0 ? 1 : hi * 1.12]) },
        ...(s.y2 ? { y2: { range: s.y2.range || ((u, lo, hi) => [0, hi == null || hi <= 0 ? 1 : hi * 1.12]) } } : {}),
      },
      axes: s.axes === false ? [{ show: false }, blankAxis(3), blankAxis(1)] : axes(pal, s),
      series, plugins,
    };
    const u = new uPlot(opts, data, box.current);
    plot.current = u;
    const ro = new ResizeObserver(() => {
      const w = box.current ? box.current.clientWidth : 0;
      if (w > 0 && Math.abs(w - u.width) > 1) u.setSize({ width: w, height: u.height });
    });
    ro.observe(box.current);
    return () => { ro.disconnect(); u.destroy(); plot.current = null; };
  }, [theme, spec.key]);

  useEffect(() => {
    if (plot.current) plot.current.setData(data, true);
  }, [data]);

  return html`<div class="chart" ref=${box} data-testid=${testid}></div>`;
}

// статичная легенда ряда: черта, пунктир, точка, квадрат-контур или столбец — тем же цветом токена, что на графике
export function Key({ kind = 'line', color = 'text', children }) {
  return html`<span class="key"><i class=${'k-' + kind + ' c-' + color}></i>${children}</span>`;
}
