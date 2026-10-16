"""Извлекает из CSV CASAS (см. casas_rates.py) моменты начала размеченных активностей по суткам:
{дом: {дата: [секунды от полуночи, ...]}}, только полные сутки. Производные данные CASAS (CC-BY-4.0,
Zenodo 15708568). Используются в compression_bias.py как реальная трасса «семантических» событий датчиков."""
import collections, datetime as dt, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from casas_rates import HOMES, ensure

cache = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'casas_cache')
ensure(cache)
out = {}
for h in HOMES:
    days = collections.defaultdict(list); first = last = None
    for line in open(os.path.join(cache, h + '.csv'), encoding='utf-8', errors='ignore'):
        p = line.rstrip('\n').split(',')
        if len(p) < 4: continue
        try: t = dt.datetime.fromisoformat(p[0] + ' ' + p[1])
        except ValueError: continue
        first = first or t.date(); last = t.date()
        if len(p) >= 5 and p[4].endswith('="begin"'):
            days[str(t.date())].append(round(t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6, 1))
    full = {d: v for d, v in days.items() if first < dt.date.fromisoformat(d) < last}
    out[h] = full
json.dump({'source': 'CASAS Zenodo 15708568 labeled_data.zip (CC-BY-4.0); activity="begin" moments, s from midnight',
           'homes': out}, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'casas_act_days.json'), 'w'))
print({h: len(v) for h, v in out.items()}, sum(len(v) for v in out.values()), 'home-days')
