import json, statistics as st, collections
rows=[json.loads(l) for l in open('pilot_results.jsonl')]
by=collections.OrderedDict()
for r in rows: by.setdefault(r['tag'],[]).append(r)
def pct(xs,p):
    xs=sorted(xs); k=(len(xs)-1)*p; f=int(k); c=min(f+1,len(xs)-1); return xs[f]+(xs[c]-xs[f])*(k-f)
print('| Конфигурация | N | Верно | Токены рассуждения p50 / p90 / max | Всего decode-токенов p50 / p90 | Decode, с p50 / p90 | ток/с decode (медиана) | Wall, с p50 / p90 |')
print('|---|---:|---:|---|---|---|---:|---|')
base=None
for tag,rs in by.items():
    rt=[r['reasoning_tokens'] for r in rs]; pn=[r['predicted_n'] for r in rs]; pm=[r['predicted_ms']/1000 for r in rs]
    tps=[r['predicted_n']/(r['predicted_ms']/1000) for r in rs]; wall=[r['wall_s'] for r in rs]
    ok=sum(r['correct'] for r in rs)
    print(f"| {tag} | {len(rs)} | {ok}/{len(rs)} | {pct(rt,.5):.0f} / {pct(rt,.9):.0f} / {max(rt)} | {pct(pn,.5):.0f} / {pct(pn,.9):.0f} | {pct(pm,.5):.2f} / {pct(pm,.9):.2f} | {st.median(tps):.1f} | {pct(wall,.5):.2f} / {pct(wall,.9):.2f} |")
print()
# prefill
pr=[(r['tag'],r['idx'],r['prompt_n'],r['cache_n'],r['prompt_ms']) for r in rows]
first=[p for p in pr if p[1]==0]
print('first-request prefill (cold within server run):',[(t,n,c,round(ms)) for t,i,n,c,ms in first])
warm=[p for p in pr if p[1]>0]
print('warm requests: prompt_n median',st.median([p[2] for p in warm]),'cache_n median',st.median([p[3] for p in warm]),'prompt_ms median',round(st.median([p[4] for p in warm])))
# per-category reasoning tokens for unlimited
u=by['think_unl']
cat=collections.defaultdict(list)
for r in u: cat[r['cat']].append(r['reasoning_tokens'])
print('think_unl reasoning tokens by category (mean):',{k:round(st.mean(v)) for k,v in cat.items()})
# ratio vs nothink
n=by['nothink']
print('decode tokens ratio think_unl/nothink (median of per-task ratio):', round(st.median([a['predicted_n']/b['predicted_n'] for a,b in zip(u,n)]),2))
print('wall ratio think_unl/nothink (median per-task):', round(st.median([a['wall_s']/b['wall_s'] for a,b in zip(u,n)]),2))
print('sum decode seconds: ', {t: round(sum(r['predicted_ms'] for r in rs)/1000,1) for t,rs in by.items()})
print('finish reasons:', {t: collections.Counter(r['finish'] for r in rs) for t,rs in by.items()})
# b128: how many hit the budget
for t in ['think_b128','think_b32','think_b32_qwenmsg']:
    print(t,'reasoning tokens list:',[r['reasoning_tokens'] for r in by[t]])
print('think_unl reasoning tokens list:',[r['reasoning_tokens'] for r in u])
