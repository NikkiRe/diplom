// Сообщения потока neurobot-ui/1 по видам, по времени: «Запись» загружает прогон целиком, живой режим (lib/live.js)
// дописывает push() — после hello и снимка (stream.connect), так что списки видов остаются упорядоченными по t.
// Экран читает срез на момент T — последнее значение, окно, свёртку жестов и тревог.

export class Timeline {
  constructor(msgs = []) {
    this.by = {};
    for (const m of msgs) this.push(m);
  }

  push(m) {
    (this.by[m.k] || (this.by[m.k] = [])).push(m);
  }

  list(kind) { return this.by[kind] || []; }

  // живой режим: подробные виды старше before браузеру не нужны (тренды длиннее — по агрегатам)
  trim(kind, before) {
    const i = this.idx(kind, before);
    if (i >= 0) this.by[kind] = this.list(kind).slice(i + 1);
  }

  // индекс последнего сообщения с t ≤ T, −1 — нет
  idx(kind, T) {
    const a = this.list(kind);
    let lo = 0, hi = a.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (a[mid].t <= T) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }

  at(kind, T) {
    const i = this.idx(kind, T);
    return i < 0 ? null : this.list(kind)[i];
  }

  // последнее сообщение вида с условием (поиск назад от T)
  atWhere(kind, T, pred) {
    const a = this.list(kind);
    for (let i = this.idx(kind, T); i >= 0; i--) if (pred(a[i])) return a[i];
    return null;
  }

  // сообщения с t в (T0, T]
  range(kind, T0, T) {
    const a = this.list(kind);
    return a.slice(this.idx(kind, T0) + 1, this.idx(kind, T) + 1);
  }

  until(kind, T) { return this.list(kind).slice(0, this.idx(kind, T) + 1); }

  // жесты на момент T: частичные обновления по id сливаются; порядок — по первому появлению; first — первое сообщение
  gestures(T) {
    const map = new Map();
    for (const m of this.until('gesture', T)) {
      const g = map.get(m.id) || { id: m.id, first: m.t };
      Object.assign(g, m, { first: g.first, last: m.t });
      map.set(m.id, g);
    }
    return [...map.values()];
  }

  // тревоги на момент T: ключ → {prio, text, on, n, since, last, off}
  alarms(T) {
    const map = new Map();
    for (const m of this.until('alarm', T)) {
      const a = map.get(m.key) || { key: m.key, code: m.code, src: m.src };
      if (m.on && !a.on) a.since = m.t;
      Object.assign(a, { prio: m.prio, text: m.text, on: m.on, n: m.n, last: m.t });
      if (!m.on) a.off = m.t;
      map.set(m.key, a);
    }
    return [...map.values()];
  }
}
