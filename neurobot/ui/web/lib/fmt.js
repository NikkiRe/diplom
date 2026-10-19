// Числа пульта по-русски (U4 §7.1) — зеркало neurobot/ui/fmt.py: запятая в дробях, «—» вместо отсутствующего,
// минус «−», неразрывный пробел в разрядах (с 5 цифр; узкого U+202F в JetBrains Mono нет). Округление — как у
// Python «%.nf»: по точному значению double, ничья — к чётному. Общие векторы проверяет тест
// (neurobot/ui/tests/test_ui_fmt.py).

export const DASH = '—';
const MINUS = '−';
const GROUP = '\u00a0';
const NBSP = '\u00a0';

function group(digits) {
  if (digits.length < 5) return digits;
  const head = digits.length % 3 || 3;
  const parts = [digits.slice(0, head)];
  for (let i = head; i < digits.length; i += 3) parts.push(digits.slice(i, i + 3));
  return parts.join(GROUP);
}

// |x| с nd знаками: 30 лишних знаков toFixed хватает, чтобы отличить точную ничью от «почти ничьей»
function fixed(ax, nd) {
  const s = ax.toFixed(Math.min(100, nd + 30));
  const [whole, frac = ''] = s.split('.');
  let digits = whole + frac.slice(0, nd);
  const rest = frac.slice(nd);
  const tie = rest[0] === '5' && /^0*$/.test(rest.slice(1));
  const up = rest[0] > '5' || (rest[0] === '5' && !tie) || (tie && Number(digits[digits.length - 1]) % 2 === 1);
  if (up) {
    const arr = digits.split('');
    let i = arr.length - 1;
    while (i >= 0) {
      if (arr[i] === '9') { arr[i] = '0'; i--; } else { arr[i] = String(Number(arr[i]) + 1); break; }
    }
    if (i < 0) arr.unshift('1');
    digits = arr.join('');
  }
  const w = digits.slice(0, digits.length - nd) || '0';
  return { whole: w, frac: nd > 0 ? digits.slice(digits.length - nd) : '' };
}

export function num(v, nd = 2) {
  if (v === null || v === undefined || typeof v === 'boolean') return DASH;
  const x = Number(v);
  if (!Number.isFinite(x)) return DASH;
  const n = Math.max(0, nd | 0);
  const { whole, frac } = fixed(Math.abs(x), n);
  const zero = /^0*$/.test(whole + frac);
  return (x < 0 && !zero ? MINUS : '') + group(whole) + (frac ? ',' + frac : '');
}

export function unit(v, nd, u) {
  const s = num(v, nd);
  return s === DASH ? s : s + NBSP + u;
}

export function pct(share, nd = 0) {
  return share === null || share === undefined ? DASH : unit(100 * share, nd, '%');
}

// длительность, с → «м:сс» или «ч:мм:сс»; знак — для оси «от начала расписания»
export function dur(sec, signed = false) {
  if (sec === null || sec === undefined || !Number.isFinite(sec)) return DASH;
  const neg = sec < 0;
  let s = Math.floor(Math.abs(sec));
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  const p2 = (x) => String(x).padStart(2, '0');
  const body = h ? `${h}:${p2(m)}:${p2(s)}` : `${m}:${p2(s)}`;
  return (signed ? (neg ? MINUS : '+') : (neg ? MINUS : '')) + body;
}

// стенные часы робота → «ЧЧ:ММ:СС» местного времени
export function clock(t) {
  if (!Number.isFinite(t)) return DASH;
  const d = new Date(t * 1000);
  const p2 = (x) => String(x).padStart(2, '0');
  return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
}

// возраст значения: «0,4 с», «12 с», «3 мин»
export function age(sec) {
  if (sec === null || sec === undefined || !Number.isFinite(sec)) return DASH;
  if (sec < 10) return unit(Math.max(0, sec), 1, 'с');
  if (sec < 120) return unit(sec, 0, 'с');
  return unit(sec / 60, 0, 'мин');
}

// целые с разрядами: токены, вызовы
export function int(v) { return num(v, 0); }

// готовый текст (ячейки compare.md, причины): число и следующая единица — через неразрывный пробел, без переноса
export function nbsp(text) {
  return text == null ? DASH : String(text).replace(/(\d) (?=[%a-zа-яё°₽])/gi, '$1' + NBSP);
}

// «1 вызов», «2 вызова», «5 вызовов»
export function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b === 1) return one;
  return b >= 2 && b <= 4 ? few : many;
}
