import json, sys, urllib.request, time, os, glob
cache_f = "size_cache.json"
cache = json.load(open(cache_f)) if os.path.exists(cache_f) else {}
def sizes(pkg, ver):
    k = f"{pkg}=={ver}"
    if k in cache and cache[k]: return cache[k]
    for t in range(5):
        try:
            d = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/{ver}/json", timeout=30))
            cache[k] = {u["filename"]: u["size"] for u in d["urls"]}; return cache[k]
        except Exception as e:
            time.sleep(2 + 2*t)
    return {}
def local(fn):
    p = os.path.join("wh", fn)
    return os.path.getsize(p) if os.path.exists(p) else None
res = {}
for f in sys.argv[1:]:
    for r in json.load(open(f)):
        if not r.get("ok"): res[r["name"]+"|"+f] = {"ok": False}; continue
        tot = 0; miss = []; big = []
        for p, v, fn in r["pkgs"]:
            s = sizes(p, v).get(fn)
            if s is None: s = local(fn)
            if s is None: miss.append(fn); s = 0
            tot += s; big.append((s, p, v))
        big.sort(reverse=True)
        res[r["name"]+"|"+f] = {"ok": True, "n": len(r["pkgs"]), "mb": round(tot/1e6, 1), "missing": miss, "top": [(p, v, round(s/1e6,1)) for s,p,v in big[:5]],
                                 "vers": {p.lower().replace('_','-'): v for p, v, fn in r["pkgs"]}}
json.dump(cache, open(cache_f, "w"))
json.dump(res, open("recomputed.json", "w"), indent=1)
for k, v in res.items():
    print(k, v.get("n"), v.get("mb"), "missing=", v.get("missing"))
