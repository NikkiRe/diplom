"""Поиск записи в Crossref по библиографической строке; печать DOI, названия, авторов, площадки, страниц."""
import sys, json, urllib.request, urllib.parse
q = sys.argv[1]; n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
url = "https://api.crossref.org/works?rows=%d&query.bibliographic=%s" % (n, urllib.parse.quote(q))
req = urllib.request.Request(url, headers={"User-Agent": "diploma-research (mailto:none@example.org)"})
d = json.load(urllib.request.urlopen(req, timeout=60))
for it in d["message"]["items"]:
    au = ", ".join((a.get("family","") + " " + a.get("given","")[:1]) for a in it.get("author", [])[:8])
    yr = (it.get("issued", {}).get("date-parts") or [[None]])[0][0]
    print(it.get("DOI"), "|", (it.get("title") or [""])[0], "|", au, "|", (it.get("container-title") or [""])[0], "|", yr, "| pp", it.get("page"), "| type", it.get("type"))
