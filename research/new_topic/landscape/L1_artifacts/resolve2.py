import json, subprocess, sys, os, urllib.request, functools
def platforms(maxglibc):
    p = [f"manylinux_2_{v}_aarch64" for v in range(17, maxglibc+1)] + ["manylinux2014_aarch64"]
    return p
def resolve(name, reqs, maxglibc=27, py="3.12"):
    rep = f"rep_{name}_g{maxglibc}_py{py.replace('.','')}.json"
    cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet", "--report", rep, "--no-cache-dir",
           "--only-binary=:all:", "--python-version", py, "--implementation", "cp", "--abi", "cp"+py.replace('.',''), "--abi", "abi3", "--abi", "none"]
    for p in platforms(maxglibc): cmd += ["--platform", p]
    cmd += ["--find-links", "./wh", "--target", "./tgt"] + reqs
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return {"name": name, "ok": False, "err": (r.stderr or r.stdout)[-1500:]}
    d = json.load(open(rep))
    pk = []
    for i in d["install"]:
        url = i["download_info"]["url"]; fn = url.split("/")[-1]; fn = fn.split("#")[0]
        pk.append((i["metadata"]["name"], i["metadata"]["version"], fn))
    return {"name": name, "ok": True, "pkgs": pk}
@functools.lru_cache(None)
def sizes(pkg, ver):
    try:
        d = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/{ver}/json", timeout=30))
        return {u["filename"]: u["size"] for u in d["urls"]}
    except Exception as e:
        return {}
if __name__ == "__main__":
    cfg = json.load(open(sys.argv[1])); g = int(sys.argv[2]); py = sys.argv[3] if len(sys.argv)>3 else "3.12"
    out = []
    for name, reqs in cfg.items():
        res = resolve(name, reqs, g, py)
        if res["ok"]:
            tot = 0; big = []
            for (p, v, fn) in res["pkgs"]:
                s = sizes(p, v).get(fn, 0); tot += s; big.append((s, p, v, fn))
            big.sort(reverse=True)
            res["n"] = len(res["pkgs"]); res["total_mb"] = round(tot/1e6, 1)
            res["top"] = [(p, v, round(s/1e6,1)) for s,p,v,fn in big[:6]]
            res["tags"] = sorted({fn.rsplit('-',1)[-1].replace('.whl','') for _,_,fn in res["pkgs"] if 'manylinux' in fn})
            print(f"{name}: OK n={res['n']} wheels={res['total_mb']} MB top={res['top']}", flush=True)
        else:
            print(f"{name}: FAIL {res['err'][-600:]}", flush=True)
        out.append(res)
    json.dump(out, open(os.environ.get("OUT", f"closure_g{g}_py{py.replace('.','')}.json"),"w"), indent=1, ensure_ascii=False)
