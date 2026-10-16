import sys,json,urllib.request,urllib.parse
q=sys.argv[1]
u="https://api.openalex.org/works?per-page=4&search="+urllib.parse.quote(q)
d=json.load(urllib.request.urlopen(u,timeout=60))
for w in d['results']:
    src=((w.get('primary_location') or {}).get('source') or {}).get('display_name')
    locs=[((l.get('source') or {}).get('display_name')) for l in w.get('locations',[])]
    print(w['publication_year'],'|',w['display_name'],'|',src,'|',w.get('doi'),'| cited',w.get('cited_by_count'),'| locs',[x for x in locs if x][:4])
