// Мини-лидар «Обзора» — Canvas 2D, вид сверху, курс вверх, робот в центре. Скана в прошлых записях нет, поэтому
// рисуется сводка safety_state (PLAN §7, П1): поле лидара ±119,9° (сзади датчика нет), маска корпуса камеры,
// клин глубины 87°, корпус Ø0,39 м и коридор, зазор min_clearance_m (пунктир) и тормозной путь d_stop(v) при скорости
// /cmd_vel (черта поперёк коридора). У A0 safety_gate нет — только корпус и подпись: защиты, которой нет, не рисуем.
// Пеленг θ — в базе робота, «+» влево (ROS): экран x = cx − r·sin θ, y = cy − r·cos θ.
import { num } from './fmt.js';

const FWD_M = 1.6;             // вперёд видно не меньше, м
const SIDE_M = 1.0;            // вбок — не меньше (дальше обрезается: перед важнее)
const VIEW_M = 4.0;            // радиус секторов (обрезаются краем)
const RINGS = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0];

export function sectors(reasons) {
  // «нет данных лидара 36,6…43,9° у корпуса» → [{code, a, b}]
  const out = [];
  for (const r of reasons || []) {
    const code = String(r).split(':')[0].trim();
    if (code !== 'lidar_unseen' && code !== 'lidar_nodata') continue;
    const m = String(r).match(/(-?\d+(?:[.,]\d+)?)\s*…\s*(-?\d+(?:[.,]\d+)?)\s*°/);
    if (m) out.push({ code, a: parseFloat(m[1].replace(',', '.')), b: parseFloat(m[2].replace(',', '.')) });
  }
  return out;
}

// out — /cmd_vel не старше 2 с (иначе null: тормозной путь не рисуется)
export function drawLidar(canvas, { geo, safety, out, pal, hasSafety, level }) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 360;
  const hgt = canvas.clientHeight || w;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(hgt * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(hgt * dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, hgt);
  const cx = w / 2, cy = hgt * 0.66;
  const k = Math.min((cy - 14) / FWD_M, (w / 2 - 8) / SIDE_M);
  ctx.save();
  ctx.beginPath(); ctx.rect(0, 0, w, hgt); ctx.clip();
  const R = geo.robot_radius;
  const pt = (r, deg) => {
    const t = (deg * Math.PI) / 180;
    return [cx - r * k * Math.sin(t), cy - r * k * Math.cos(t)];
  };
  // дуга от a до b градусов (пеленг), радиусы r0…r1
  const wedge = (a, b, r0, r1) => {
    ctx.beginPath();
    const steps = Math.max(4, Math.ceil(Math.abs(b - a) / 3));
    for (let i = 0; i <= steps; i++) { const [x, y] = pt(r1, a + ((b - a) * i) / steps); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
    for (let i = steps; i >= 0; i--) { const [x, y] = pt(r0, a + ((b - a) * i) / steps); ctx.lineTo(x, y); }
    ctx.closePath();
  };
  const hatch = (color) => {
    ctx.save(); ctx.clip();
    ctx.strokeStyle = color; ctx.lineWidth = 1;
    ctx.beginPath();
    for (let x = -hgt; x < w + hgt; x += 7) { ctx.moveTo(x, hgt); ctx.lineTo(x + hgt, 0); }
    ctx.stroke(); ctx.restore();
  };
  ctx.font = pal.font;
  ctx.lineWidth = 1;

  // кольца шкалы; подпись — только если целиком на холсте
  ctx.strokeStyle = pal.line; ctx.globalAlpha = 0.6;
  for (const r of RINGS) { ctx.beginPath(); ctx.arc(cx, cy, r * k, 0, 2 * Math.PI); ctx.stroke(); }
  ctx.globalAlpha = 1;
  ctx.fillStyle = pal.muted;
  for (const r of RINGS) {
    const label = num(r, 1) + ' м';
    const [x, y] = pt(r, 30);
    const lw = ctx.measureText(label).width;
    if (x - lw - 4 >= 2 && y - 10 >= 2) ctx.fillText(label, x - lw - 4, y - 2);
  }

  if (hasSafety) {
    // сзади датчика нет: ±119,9° — поле URG-04LX; маска корпуса камеры D435i перед лидаром: там видит только глубина
    const half = geo.lidar_half_fov_deg;
    wedge(half, 360 - half, R, VIEW_M); hatch(pal.faint);
    const [ma, mb] = geo.mask_robot_deg;
    wedge(ma, mb, R, VIEW_M); hatch(pal.faint);

    // клин камеры глубины 87° до 2,5 м (depth_max_m safety_gate)
    ctx.setLineDash([2, 3]); ctx.strokeStyle = pal.muted;
    for (const s of [-1, 1]) { const [x, y] = pt(2.5, (s * geo.depth_fov_deg) / 2); ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(x, y); ctx.stroke(); }
    ctx.setLineDash([]);
  }
  // коридор корпуса: ширина 2·(R + lateral_margin) вперёд
  const halfW = (R + geo.lateral_margin) * k;
  if (hasSafety) {
    ctx.strokeStyle = pal.muted;
    ctx.beginPath();
    ctx.moveTo(cx - halfW, cy); ctx.lineTo(cx - halfW, 0);
    ctx.moveTo(cx + halfW, cy); ctx.lineTo(cx + halfW, 0);
    ctx.stroke();

    // участки «нет данных лидара» из причин (lidar_unseen — норма, lidar_nodata при blocked — тревога)
    const st = safety ? safety.state : null;
    for (const s of sectors(safety ? safety.reasons : [])) {
      const c = s.code === 'lidar_nodata' && (st === 'blocked' || st === 'estop') ? pal.p2 : pal.muted;
      ctx.strokeStyle = c; ctx.lineWidth = 2;
      wedge(s.a, s.b, R + 0.02, VIEW_M); ctx.stroke(); ctx.lineWidth = 1;
    }
  }

  // корпус и курс
  ctx.fillStyle = pal.panel2; ctx.strokeStyle = pal.text; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.arc(cx, cy, R * k, 0, 2 * Math.PI); ctx.fill(); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(cx, cy - R * k + 4); ctx.lineTo(cx - 6, cy - R * k + 14); ctx.lineTo(cx + 6, cy - R * k + 14); ctx.closePath();
  ctx.fillStyle = pal.text; ctx.fill();
  ctx.lineWidth = 1;

  if (!hasSafety) {
    ctx.fillStyle = pal.muted; ctx.textAlign = 'center';
    ctx.fillText('safety_gate нет', cx, cy + R * k + 22);
    ctx.restore();
    return;
  }
  const ac = level === 1 ? pal.p1 : level === 2 ? pal.p2 : null;
  // зазор: ближайшая опасность в любом направлении — от края корпуса; при тревоге — её цветом (P1 и P2 — не ниже 3:1 к
  // панели в обеих темах, WCAG 1.4.11; жёлтого уровня больше нет)
  if (safety && safety.min_clearance_m != null && safety.min_clearance_m <= VIEW_M) {
    const r = (R + safety.min_clearance_m) * k;
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = ac || pal.text; ctx.lineWidth = ac ? 2.5 : 1.2;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, 2 * Math.PI); ctx.stroke();
    ctx.setLineDash([]); ctx.lineWidth = 1;
  }
  // тормозной путь при текущей скорости вперёд — черта поперёк коридора
  if (out && out.d_stop_m != null) {
    const y = cy - (R + out.d_stop_m) * k;
    ctx.strokeStyle = pal.text; ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.moveTo(cx - halfW, y); ctx.lineTo(cx + halfW, y); ctx.stroke(); ctx.lineWidth = 1;
  }
  ctx.restore();
}
