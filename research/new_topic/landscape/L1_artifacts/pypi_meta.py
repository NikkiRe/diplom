import json, sys, urllib.request
pkgs = sys.argv[1:]
for p in pkgs:
    try:
        d = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{p}/json", timeout=30))
    except Exception as e:
        print(p, "ERR", e); continue
    info = d["info"]; ver = info["version"]
    files = d["releases"].get(ver, [])
    upload = min((f["upload_time"] for f in files), default="?")
    first = None
    for v, fl in d["releases"].items():
        for f in fl:
            if first is None or f["upload_time"] < first: first = f["upload_time"]
    reqs = info.get("requires_dist") or []
    core = [r for r in reqs if "extra ==" not in r]
    extras = info.get("provides_extra") or []
    whl = [ (f["filename"], f["size"]) for f in files if f["packagetype"]=="bdist_wheel"]
    print(json.dumps({"pkg":p,"version":ver,"released":upload[:10],"first_release":(first or "?")[:10],"requires_python":info.get("requires_python"),
      "license":info.get("license_expression") or (info.get("license") or "")[:40],"n_core_deps":len(core),"core_deps":core,"n_extras":len(extras),"extras":extras[:60],"wheels":whl[:3]}, ensure_ascii=False))
