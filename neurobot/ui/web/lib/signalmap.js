// Observed signal membership, not a ROS connection graph. Motion is keyed to message timestamps.
import { useEffect, useRef, useState } from 'preact/hooks';
import { html } from './ui.js';
import { age, int, num, plural } from './fmt.js';

const MAX_NODES = 16;
const PULSE_MS = 900;
const STATES = {
  live: 'поток', recorded: 'запись', stale: 'устарело', missing: 'нет данных', error: 'ошибка', degraded: 'частично', described: 'описание',
};
const GROUPS = { sensor: 'Датчик', motion: 'Движение', system: 'Система', event: 'События', mcp: 'MCP' };

function CategoryIcon({ group, cls = '' }) {
  const paths = {
    sensor: 'M5 17a9 9 0 0 1 0-10M8 14a5 5 0 0 1 0-4M19 17a9 9 0 0 0 0-10M16 14a5 5 0 0 0 0-4M12 12v7M10 19h4',
    motion: 'M4 14h4l3-8 3 12 3-8h3M5 20h14',
    system: 'M7 7h10v10H7ZM10 10h4v4h-4M4 8h3M4 12h3M4 16h3M17 8h3M17 12h3M17 16h3M8 4v3M12 4v3M16 4v3M8 17v3M12 17v3M16 17v3',
    event: 'M5 4h14v16H5ZM8 8h8M8 12h8M8 16h5',
    mcp: 'M8 8 5 5l-3 3 3 3 3-3ZM16 16l3-3 3 3-3 3-3-3ZM8 8l8 8M13 5l6 6M5 13l6 6',
  };
  return html`<svg class=${'sm-category-icon ' + cls} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d=${paths[group] || paths.system} /></svg>`;
}

function CollectorIcon() {
  return html`<svg class="sm-collector-icon" viewBox="0 0 60 60" fill="none" aria-hidden="true">
    <circle class="sm-collector-ring" cx="30" cy="30" r="27" />
    <circle class="sm-collector-ring inner" cx="30" cy="30" r="21" />
    <path class="sm-collector-chip" d="M22 22h16v16H22ZM26 26h8v8h-8M18 25h4M18 30h4M18 35h4M38 25h4M38 30h4M38 35h4M25 18v4M30 18v4M35 18v4M25 38v4M30 38v4M35 38v4" />
  </svg>`;
}

function layoutNodes(devices, selected) {
  const shown = devices.slice(0, MAX_NODES);
  const chosen = devices.find(d => d.id === selected);
  if (chosen && !shown.some(d => d.id === selected)) shown[shown.length - 1] = chosen;
  const leftCount = Math.ceil(shown.length / 2);
  return shown.map((device, i) => {
    const left = i < leftCount;
    const n = left ? leftCount : shown.length - leftCount;
    const row = left ? i : i - leftCount;
    const span = Math.min(322, Math.max(0, (n - 1) * 74));
    const cy = n === 1 ? 215 : 215 - span / 2 + row * span / (n - 1);
    const portX = left ? 190 : 410;
    const coreX = left ? 224 : 376;
    return {
      device, left, x: left ? 18 : 410, y: cy - 22, cy,
      path: `M${portX} ${cy} C${left ? 213 : 387} ${cy} ${left ? 201 : 399} 215 ${coreX} 215`,
    };
  });
}

// A retained timestamp baseline and keyed pulse elements prevent parent renders from restarting animations.
function useMessagePulses(devices) {
  const timestamps = useRef(null);
  const timers = useRef(new Map());
  const sequence = useRef(0);
  const [pulses, setPulses] = useState(() => new Map());
  useEffect(() => {
    if (!timestamps.current || (!timestamps.current.size && devices.length)) {
      timestamps.current = new Map(devices.map((d) => [d.id, d.lastTs]));
      return;
    }
    const changes = [];
    const active = new Set(devices.map(d => d.id));
    for (const id of timestamps.current.keys()) if (!active.has(id)) {
      timestamps.current.delete(id);
      if (timers.current.has(id)) { clearTimeout(timers.current.get(id)); timers.current.delete(id); }
    }
    for (const d of devices) {
      const before = timestamps.current.get(d.id);
      if (Number.isFinite(d.lastTs) && before !== d.lastTs && ['live','recorded','degraded'].includes(d.status)) changes.push(d.id);
      timestamps.current.set(d.id, d.lastTs);
    }
    if ([...pulses.keys()].some(id => !active.has(id))) setPulses(current => new Map([...current].filter(([id]) => active.has(id))));
    if (!changes.length) return;
    const updates = new Map();
    for (const id of changes) {
      updates.set(id, ++sequence.current);
      if (timers.current.has(id)) clearTimeout(timers.current.get(id));
      timers.current.set(id, setTimeout(() => {
        setPulses((current) => {
          if (!current.has(id)) return current;
          const next = new Map(current); next.delete(id); return next;
        });
        timers.current.delete(id);
      }, PULSE_MS));
    }
    setPulses((current) => new Map([...current, ...updates]));
  }, [devices]);
  useEffect(() => () => { for (const timer of timers.current.values()) clearTimeout(timer); }, []);
  return pulses;
}

function signalCount(device) { return Array.isArray(device.signals) ? device.signals.length : 0; }
function status(device) { return Object.hasOwn(STATES, device.status) ? device.status : 'missing'; }

function DeviceNode({ node, selected, onSelect, pulse, T }) {
  const d = node.device;
  const state = status(d);
  const count = signalCount(d);
  const now = d.clock === 'wall' ? Date.now() / 1000 : T;
  const seen = Number.isFinite(d.lastTs) && Number.isFinite(now) ? ` · возраст ${age(Math.max(0, now - d.lastTs))}` : '';
  const rate = Number.isFinite(d.rate) ? ` · ${num(d.rate, 1)} Гц` : '';
  const title = `${d.name || d.id} · ${d.source || d.label || GROUPS[d.group] || 'Источник'} · ${STATES[state]}${seen}${rate}`;
  return html`<button type="button" class=${'sm-node ' + (node.left ? 'is-left' : 'is-right') + ' state-' + state + (selected === d.id ? ' is-selected' : '')}
    style=${{ '--sm-x': `${node.x / 6}%`, '--sm-y': `${node.y / 4.3}%` }}
    data-testid=${'map-node-' + d.id} data-status=${state} data-last-ts=${Number.isFinite(d.lastTs) ? d.lastTs : ''}
    aria-pressed=${selected === d.id} aria-label=${title} title=${title} onClick=${() => onSelect && onSelect(d.id)}>
    ${pulse ? html`<span key=${pulse} class="sm-node-arrival" aria-hidden="true"></span>` : ''}
    <span class="sm-node-port" aria-hidden="true"></span>
    <span class="sm-node-icon"><${CategoryIcon} group=${d.group} /></span>
    <span class="sm-node-copy"><span class="sm-node-name">${d.name || d.id}</span><span class="sm-node-meta"><span>${int(count)} ${plural(count, 'сигнал', 'сигнала', 'сигналов')}</span><span class="sm-node-state">${STATES[state]}</span></span></span>
    <span class="sm-node-status" aria-hidden="true"></span>
  </button>`;
}

export function SignalMap({ devices = [], selected, onSelect, T, live = false }) {
  const nodes = layoutNodes(devices, selected);
  const pulses = useMessagePulses(devices);
  const hidden = Math.max(0, devices.length - nodes.length);
  const counts = Object.keys(STATES).map((s) => [s, devices.filter((d) => status(d) === s).length]).filter(([, n]) => n > 0);
  const totalSignals = devices.reduce((sum, d) => sum + signalCount(d), 0);
  return html`<section class="signal-map" data-testid="signal-map" aria-label="Карта сигналов">
    <header class="sm-header"><div><div class="sm-eyebrow">РАБОЧАЯ ПЛОСКОСТЬ</div><h2>Карта сигналов</h2></div><span class="sm-mode">${live ? 'LIVE / ПРИЁМ' : 'ЗАПИСЬ / ПРОСМОТР'}</span></header>
    <div class="sm-description">Связи показывают принадлежность потоку</div>
    ${nodes.length ? html`<div class="sm-workplane">
      <div class="sm-plane-label" aria-hidden="true"><span class="sm-fiducial"></span>ИСТОЧНИКИ / КОНТЕКСТ</div>
      <svg class="sm-connections" viewBox="0 0 600 430" preserveAspectRatio="none" aria-hidden="true">
        <path class="sm-corner-marks" d="M8 26V8h18M574 8h18v18M8 404v18h18M574 422h18v-18" />
        <path class="sm-center-guide" d="M300 18V130M300 300V412" />
        ${nodes.map((n) => html`<g key=${n.device.id} class=${'sm-link state-' + status(n.device) + (selected === n.device.id ? ' is-selected' : '')}>
          <path d=${n.path} />
          ${pulses.get(n.device.id) ? html`<path key=${pulses.get(n.device.id)} class="sm-link-arrival" d=${n.path} />` : ''}
        </g>`)}
      </svg>
      <div class="sm-collector"><span class="sm-collector-port port-left" aria-hidden="true"></span><${CollectorIcon} /><b>КОНТЕКСТ</b><span class="sm-collector-label">поток наблюдения</span><span class="sm-collector-total">${int(totalSignals)} ${plural(totalSignals, 'сигнал', 'сигнала', 'сигналов')}</span><span class="sm-collector-port port-right" aria-hidden="true"></span></div>
      <div class="sm-nodes">${nodes.map((n) => html`<${DeviceNode} key=${n.device.id} node=${n} selected=${selected} onSelect=${onSelect} pulse=${pulses.get(n.device.id)} T=${T} />`)}</div>
      <div class="sm-plane-caption"><span>${int(devices.length)} ${plural(devices.length, 'источник', 'источника', 'источников')}</span><span>${hidden ? `ещё ${int(hidden)} — в каталоге` : 'Выберите источник для просмотра'}</span></div>
    </div>` : html`<div class="sm-empty" data-testid="map-empty"><${CategoryIcon} group="sensor" /><h3>Источников пока нет</h3><p>Добавьте MCP-источник, чтобы увидеть оборудование.</p></div>`}
    <footer class="sm-footer">
      <div class="sm-minimap" aria-hidden="true"><svg viewBox="0 0 70 38"><path d="M35 5v28M8 19h54" />${nodes.map((n) => html`<rect x=${n.left ? 5 : 53} y=${3 + n.cy / 430 * 28} width="11" height="2" class=${'state-' + status(n.device) + (selected === n.device.id ? ' is-selected' : '')} />`)}<rect class="sm-mini-core" x="28" y="12" width="14" height="14" rx="2" /></svg></div>
      <div class="sm-legend" aria-label="Состояния источников">${counts.map(([s, n]) => html`<span class=${'sm-legend-item state-' + s}><i aria-hidden="true"></i><span>${STATES[s]}</span><b>${int(n)}</b></span>`)}</div>
      <span class="sm-membership-note">${live ? 'Импульс — новое сообщение' : 'Единое время записи'}</span>
    </footer>
  </section>`;
}
