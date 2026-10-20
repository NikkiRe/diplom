import { html } from './ui.js';

const paths = {
  connect: 'M7 7h10v10H7zM10 10h4v4h-4zM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4',
  assistant: 'M21 11a8 8 0 0 1-8 8H6l-4 3V11a9 9 0 0 1 19 0M7 10h10M7 14h6',
  lab: 'M9 3h6M10 3v6l-6 10a1.3 1.3 0 0 0 1 2h14a1.3 1.3 0 0 0 1-2L14 9V3M8 14h8M10 17h.01M14 18h.01',
  equipment: 'M4 5h5v5H4zM15 4h5v5h-5zM8 16h6v5H8zM9 7h6M6.5 10v3h4.5v3M17.5 9v4H11',
  network: 'M4 5h5v5H4zM15 4h5v5h-5zM8 16h6v5H8zM9 7h6M6.5 10v3h4.5v3M17.5 9v4H11',
  search: 'M16 10a6 6 0 1 1-12 0 6 6 0 0 1 12 0m-1.5 4.5L21 21',
  overview: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  safety: 'M12 3 3.5 6.5v5c0 4.5 3 7.5 8.5 10 5.5-2.5 8.5-5.5 8.5-10v-5L12 3zM8.5 12l2.5 2.5 4.5-5',
  trends: 'M4 3v17h17M7 14l4-5 4 3 5-7',
  chip: 'M7 7h10v10H7zM10 10h4v4h-4zM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4',
  sun: 'M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6 7 7M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  moon: 'M20.5 13.4A8.5 8.5 0 0 1 10.6 3.5 8.5 8.5 0 1 0 20.5 13.4z',
  keyboard: 'M3 6h18v12H3zM6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10',
  close: 'm6 6 12 12M6 18 18 6',
  pulse: 'M2 12h5l3-8 4 16 3-8h5',
  chevron: 'm9 5 7 7-7 7',
  clock: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M12 7v5l3 2',
};

export function Icon({ name, size = 20, cls = '' }) {
  return html`<svg class=${'icon ' + cls} width=${size} height=${size} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d=${paths[name] || paths.pulse} /></svg>`;
}
