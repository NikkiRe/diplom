import json, sys, io, zipfile, urllib.request, os, concurrent.futures as cf
cache_f = "unzip_cache.json"
cache = json.load(open(cache_f)) if os.path.exists(cache_f) else {}
class HTTPRange(io.RawIOBase):
    def __init__(self, url):
        self.url = url; self.pos = 0
        req = urllib.request.Request(url, method="HEAD"); r = urllib.request.urlopen(req, timeout=30)
        self.size = int(r.headers["Content-Length"])
    def seekable(self): return True
    def readable(self): return True
    def tell(self): return self.pos
    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off); return self.pos
    def read(self, n=-1):
        if n < 0: n = self.size - self.pos
        if n == 0 or self.pos >= self.size: return b""
        end = min(self.pos + n, self.size) - 1
        req = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{end}"})
        data = urllib.request.urlopen(req, timeout=60).read(); self.pos += len(data); return data
    def readinto(self, b):
        d = self.read(len(b)); b[:len(d)] = d; return len(d)
def urls_for(pkg, ver):
    d = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/{ver}/json", timeout=30))
    return {u["filename"]: u["url"] for u in d["urls"]}
def usize(item):
    p, v, fn = item; k = fn
    if k in cache: return k, cache[k]
    local = os.path.join("wh", fn)
    try:
        if os.path.exists(local): z = zipfile.ZipFile(local)
        else:
            url = urls_for(p, v)[fn]; z = zipfile.ZipFile(io.BufferedReader(HTTPRange(url), buffer_size=1<<16))
        s = sum(i.file_size for i in z.infolist()); return k, s
    except Exception as e:
        return k, None
for f in sys.argv[1:]:
    for r in json.load(open(f)):
        if not r.get("ok"): continue
        with cf.ThreadPoolExecutor(8) as ex: res = list(ex.map(usize, r["pkgs"]))
        tot = 0; miss = 0
        for k, s in res:
            if s is None: miss += 1
            else: cache[k] = s; tot += s
        print(f"{f}\t{r['name']}\t{len(r['pkgs'])}\t{tot/1e6:.0f} MB unpacked\tmissing={miss}", flush=True)
        json.dump(cache, open(cache_f, "w"))
