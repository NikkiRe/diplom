"""Эмпирические интенсивности событий датчиков умного дома (CASAS) для модели нагрузки «дня работы».

Данные: CASAS Smart Home dataset — free living, motion, door, activity labels.
Zenodo record 15708568 (CC-BY-4.0, опубликован 2025-06-20), файл labeled_data.zip (236 037 656 байт).
Скрипт НЕ скачивает архив целиком: читает центральный каталог zip и нужные CSV через HTTP Range
(curl -r), около 24 МБ на 14 домов. Формат строки: date,time,sensor,message[,activity="begin"|"end"].

Что считается (по каждому дому, только полные сутки между первым и последним днём записи):
  * raw     — сообщения ON и OPEN (OFF/CLOSE парные, не считаются);
  * room    — смена комнаты: событие ON/OPEN в другой локации, чем предыдущее (дешёвый локальный фильтр);
  * door    — OPEN/CLOSE-датчики (OPEN) и датчики OutsideDoor/FrontDoor (ON);
  * act     — начала размеченных активностей (activity="begin"), прокси «семантического» события,
              которое локальный детектор мог бы передать агенту;
  * часовой профиль raw и act (доля событий по часам суток, локальное время дома);
  * межсобытийные интервалы: медиана, доля > 5/10/30/60 мин, коэффициент берстовости
    B = (σ − μ)/(σ + μ) (Goh & Barabási 2008).
Запуск:  python3 casas_rates.py [каталог_с_csv]   (по умолчанию ./casas_cache, докачает недостающее)
Вывод:   casas_rates_output.json + таблица в stdout.
"""
import collections, datetime as dt, hashlib, io, json, os, statistics as st, subprocess, sys, zipfile

URL = "https://zenodo.org/records/15708568/files/labeled_data.zip?download=1"
SIZE = 236037656
HOMES = {  # дом -> число жителей (по описанию записи Zenodo)
    'hh101': 1, 'hh102': 1, 'hh103': 1, 'hh105': 1, 'hh106': 1, 'hh110': 1, 'hh114': 1,
    'hh118': 1, 'hh122': 1, 'hh126': 1, 'hh129': 1, 'hh130': 1, 'hh107': 2, 'hh121': 2,
}


class HTTPRange(io.RawIOBase):
    def __init__(self, url, size):
        self.url, self.size, self.pos, self.fetched = url, size, 0, 0
    def seekable(self): return True
    def readable(self): return True
    def tell(self): return self.pos
    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos
    def read(self, n=-1):
        if n < 0: n = self.size - self.pos
        if n == 0 or self.pos >= self.size: return b''
        end = min(self.size, self.pos + n) - 1
        d = subprocess.run(['curl', '-s', '-r', f'{self.pos}-{end}', self.url], capture_output=True).stdout
        self.pos += len(d); self.fetched += len(d)
        return d
    def readinto(self, b):
        d = self.read(len(b)); b[:len(d)] = d; return len(d)


def ensure(cache):
    os.makedirs(cache, exist_ok=True)
    missing = [h for h in HOMES if not os.path.exists(os.path.join(cache, h + '.csv'))]
    if not missing: return
    f = HTTPRange(URL, SIZE)
    z = zipfile.ZipFile(io.BufferedReader(f, buffer_size=1 << 20))
    for h in missing:
        open(os.path.join(cache, h + '.csv'), 'wb').write(z.read(f'labeled/{h}.csv'))
    print(f'докачано {f.fetched/1e6:.1f} МБ', file=sys.stderr)


def burstiness(g):
    m, s = st.mean(g), st.pstdev(g)
    return (s - m) / (s + m)


def gap_stats(ts):
    g = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    if len(g) < 3: return None
    return {
        'n_gaps': len(g), 'median_s': st.median(g), 'mean_s': st.mean(g),
        'p_gt_5min': sum(x > 300 for x in g) / len(g), 'p_gt_10min': sum(x > 600 for x in g) / len(g),
        'p_gt_30min': sum(x > 1800 for x in g) / len(g), 'p_gt_60min': sum(x > 3600 for x in g) / len(g),
        'B': burstiness(g),
    }


def analyse(path):
    raw, room, door, act = [], [], [], []
    prev_loc = None
    for line in open(path, encoding='utf-8', errors='ignore'):
        p = line.rstrip('\n').split(',')
        if len(p) < 4: continue
        try:
            t = dt.datetime.fromisoformat(p[0] + ' ' + p[1])
        except ValueError:
            continue
        sensor, msg = p[2], p[3]
        if len(p) >= 5 and p[4].endswith('="begin"'):
            act.append(t)
        if msg not in ('ON', 'OPEN'): continue
        raw.append(t)
        if sensor != prev_loc: room.append(t)
        prev_loc = sensor
        if msg == 'OPEN' or 'Door' in sensor: door.append(t)
    d0, d1 = raw[0].date() + dt.timedelta(days=1), raw[-1].date() - dt.timedelta(days=1)  # только полные сутки
    def per_day(ts):
        c = collections.Counter(t.date() for t in ts if d0 <= t.date() <= d1)
        days = [(d0 + dt.timedelta(days=i)) for i in range((d1 - d0).days + 1)]
        return [c.get(d, 0) for d in days]
    raw_d = per_day(raw)
    active = [i for i, x in enumerate(raw_d) if x > 0]
    def summary(ts):
        pd = per_day(ts)
        pd_act = [pd[i] for i in active]
        sel = [t for t in ts if d0 <= t.date() <= d1]
        hrs = collections.Counter(t.hour for t in sel)
        tot = sum(hrs.values()) or 1
        return {'per_day_median': st.median(pd_act), 'per_day_mean': st.mean(pd_act),
                'per_day_p25': st.quantiles(pd_act, n=4)[0], 'per_day_p75': st.quantiles(pd_act, n=4)[2],
                'hour_share': [hrs.get(h, 0) / tot for h in range(24)],
                'gaps': gap_stats([t.timestamp() for t in sel])}
    return {'days_span': len(raw_d), 'days_active': len(active), 'first': str(d0), 'last': str(d1),
            'raw': summary(raw), 'room': summary(room), 'door': summary(door), 'act': summary(act)}


if __name__ == '__main__':
    cache = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), 'casas_cache')
    ensure(cache)
    res = {}
    for h in HOMES:
        path = os.path.join(cache, h + '.csv')
        res[h] = analyse(path)
        res[h]['residents'] = HOMES[h]
        res[h]['sha256'] = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    hdr = f"{'дом':<6}{'жит':>4}{'сут':>5}{'raw/сут':>9}{'room/сут':>9}{'door/сут':>9}{'act/сут':>8}" \
          f"{'B_raw':>7}{'B_act':>7}{'act>10м':>8}{'act>30м':>8}"
    print(hdr)
    for h, r in res.items():
        ga = r['act']['gaps'] or {}
        print(f"{h:<6}{r['residents']:>4}{r['days_active']:>5}{r['raw']['per_day_median']:>9.0f}"
              f"{r['room']['per_day_median']:>9.0f}{r['door']['per_day_median']:>9.0f}{r['act']['per_day_median']:>8.1f}"
              f"{r['raw']['gaps']['B']:>7.2f}{ga.get('B', float('nan')):>7.2f}"
              f"{ga.get('p_gt_10min', float('nan')):>8.2f}{ga.get('p_gt_30min', float('nan')):>8.2f}")
    # агрегат: медиана по домам и средний часовой профиль
    agg = {}
    for k in ('raw', 'room', 'door', 'act'):
        agg[k] = {
            'per_day_median_across_homes': st.median(r[k]['per_day_median'] for r in res.values()),
            'per_day_range_across_homes': [min(r[k]['per_day_median'] for r in res.values()),
                                           max(r[k]['per_day_median'] for r in res.values())],
            'hour_share_mean': [st.mean(r[k]['hour_share'][h] for r in res.values()) for h in range(24)],
        }
        gs = [r[k]['gaps'] for r in res.values() if r[k]['gaps']]
        for q in ('B', 'p_gt_5min', 'p_gt_10min', 'p_gt_30min', 'p_gt_60min', 'median_s'):
            agg[k][q + '_median_across_homes'] = st.median(g[q] for g in gs)
    print('\nМедиана по домам, событий в сутки:',
          {k: agg[k]['per_day_median_across_homes'] for k in agg})
    print('Диапазон медиан по домам:', {k: agg[k]['per_day_range_across_homes'] for k in agg})
    for k in agg:
        print(k, 'B=%.2f  P(gap>5м)=%.2f  >10м=%.2f  >30м=%.2f  >60м=%.2f  медиана gap=%.0f с' % tuple(
            agg[k][q + '_median_across_homes'] for q in ('B', 'p_gt_5min', 'p_gt_10min', 'p_gt_30min', 'p_gt_60min', 'median_s')))
    print('\nЧасовой профиль act (средняя доля по домам, %):')
    print(' '.join(f'{h:02d}:{100*x:4.1f}' for h, x in enumerate(agg['act']['hour_share_mean'])))
    print('Часовой профиль raw (%):')
    print(' '.join(f'{h:02d}:{100*x:4.1f}' for h, x in enumerate(agg['raw']['hour_share_mean'])))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'casas_rates_output.json')
    json.dump({'source': URL, 'homes': res, 'aggregate': agg}, open(out, 'w'), ensure_ascii=False, indent=1)
    print('\nзаписано', out)
