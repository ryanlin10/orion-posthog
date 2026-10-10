import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname, sep } from 'node:path';
export function serve(port = 4173) {
  const root = resolve('.');
  return createServer(async (req, res) => {
    const path = resolve(root, '.' + new URL(req.url, 'http://localhost').pathname.replace(/^\/$/, '/examples/index.html'));
    if (!path.startsWith(root + sep)) { res.writeHead(403).end(); return; }
    try { const data = await readFile(path); res.setHeader('Content-Type', ({ '.js': 'text/javascript', '.html': 'text/html', '.json': 'application/json' })[extname(path)] ?? 'text/plain'); res.end(data); }
    catch { res.writeHead(404).end(); }
  }).listen(port, '127.0.0.1');
}
if (process.argv[1]?.endsWith('/serve.js')) { serve(); console.log('Local demo: http://127.0.0.1:4173'); }
