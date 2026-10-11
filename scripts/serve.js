import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname } from 'node:path';
const publicFiles = new Set([
  '/examples/index.html', '/examples/sdk-test.html', '/src/index.js', '/src/capture.js', '/src/features.js', '/src/model.js',
  '/src/live.js', '/src/sequence.js', '/src/analysis.js', '/src/codec.js', '/dist/orion.js', '/dist/orion.js.map', '/node_modules/posthog-js/dist/array.js',
  '/test/fixtures/clicky-model.json',
]);
export function serve(port = 4173) {
  return createServer(async (req, res) => {
    const path = new URL(req.url, 'http://localhost').pathname.replace(/^\/$/, '/examples/index.html');
    // Never serve private CrowdCast fixtures, credentials or arbitrary repo files.
    if (!publicFiles.has(path)) { res.writeHead(403).end(); return; }
    try { const data = await readFile(new URL(`..${path}`, import.meta.url)); res.setHeader('Content-Type', ({ '.js': 'text/javascript', '.html': 'text/html', '.json': 'application/json' })[extname(path)] ?? 'text/plain'); res.end(data); }
    catch { res.writeHead(404).end(); }
  }).listen(port, '127.0.0.1');
}
if (process.argv[1]?.endsWith('/serve.js')) { serve(); console.log('Local demo: http://127.0.0.1:4173'); }
