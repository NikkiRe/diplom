"""Печать метаданных arXiv по списку id (arXiv API). Использование: python3 arxiv_meta.py out.xml id1 id2 ..."""
import sys, urllib.request, xml.etree.ElementTree as ET
out, ids = sys.argv[1], sys.argv[2:]
url = "https://export.arxiv.org/api/query?max_results=50&id_list=" + ",".join(ids)
data = urllib.request.urlopen(url, timeout=60).read()
open(out, "wb").write(data)
ns = {'a': 'http://www.w3.org/2005/Atom', 'arxiv': 'http://arxiv.org/schemas/atom'}
for e in ET.fromstring(data).findall('a:entry', ns):
    g = lambda p: (e.find(p, ns).text if e.find(p, ns) is not None else None)
    print('==', g('a:id'), '|', ' '.join(g('a:title').split()))
    print('authors:', ', '.join(a.find('a:name', ns).text for a in e.findall('a:author', ns)))
    print('published:', g('a:published'), 'updated:', g('a:updated'))
    print('comment:', g('arxiv:comment'), '| jref:', g('arxiv:journal_ref'), '| doi:', g('arxiv:doi'))
    print('abs:', ' '.join(g('a:summary').split()))
    print()
