import fs from 'fs';
const dirs = process.argv.slice(2);
for (const d of dirs) {
  const lock = JSON.parse(fs.readFileSync(`${d}/package-lock.json`));
  const entries = Object.entries(lock.packages).filter(([k]) => k);
  const keep = entries.filter(([k, v]) => {
    const okOs = !v.os || v.os.includes('linux'); const okCpu = !v.cpu || v.cpu.includes('arm64');
    const libc = !v.libc || v.libc.includes('glibc');
    return okOs && okCpu && libc;
  });
  let total = 0, missing = 0; const big = [];
  const chunks = []; for (let i = 0; i < keep.length; i += 25) chunks.push(keep.slice(i, i + 25));
  for (const ch of chunks) {
    await Promise.all(ch.map(async ([k, v]) => {
      const name = k.split('node_modules/').pop();
      try {
        const r = await fetch(`https://registry.npmjs.org/${name.replace('/', '%2f')}/${v.version}`);
        const j = await r.json(); const s = j.dist?.unpackedSize || 0; if (!s) missing++;
        total += s; big.push([s, name]);
      } catch { missing++; }
    }));
  }
  big.sort((a, b) => b[0] - a[0]);
  const engines = new Set(keep.map(([k, v]) => v.engines?.node).filter(Boolean));
  console.log(`${d}: linux-arm64 pkgs=${keep.length}/${entries.length} unpacked=${(total/1e6).toFixed(1)}MB missing=${missing} top=${big.slice(0,4).map(([s,n])=>n+':'+(s/1e6).toFixed(1)).join(', ')}`);
}
