// For the event check: starts the app the way the developer would (its dev script) and finds the address it prints,
// or serves a plain-HTML site from this machine, since module scripts do not load from file:// pages. Whatever starts
// here stops when the check ends.
import { spawn } from 'node:child_process';
import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';

const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g;
const LOCAL_URL = /\bhttps?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\])(?::\d+)?[^\s'")\]]*/;

/** The first local address in a dev server's output, with 0.0.0.0 made openable. */
export function localUrl(text) {
  return text.replace(ANSI, '').match(LOCAL_URL)?.[0].replace(/\/\/(?:0\.0\.0\.0|\[::1?\])/, '//localhost') ?? null;
}

/**
 * Runs `command` (a dev script) in `cwd`. Resolves { url, stop, output } once it prints a local address, or with
 * url null after `wait` ms or when it exits (exited true; output holds its last lines).
 */
export function startDevServer(command, { cwd, env = process.env, wait = 90_000 }) {
  return new Promise(resolve => {
    // Its own process group, so stopping it also stops whatever it started (the bundler, file watchers).
    const child = spawn(command, { cwd, shell: true, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...env, BROWSER: 'none', FORCE_COLOR: '0', NO_COLOR: '1' } });
    let output = '', done = false, exited = false;
    const stop = () => {
      if (exited) return;
      try { process.platform === 'win32' ? child.kill() : process.kill(-child.pid, 'SIGTERM'); } catch { /* gone already */ }
    };
    const finish = url => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve({ url, stop, exited, output: () => output });
    };
    const read = chunk => {
      output = (output + chunk).slice(-8000);
      const url = localUrl(output);
      if (url) finish(url);
    };
    child.stdout.on('data', read);
    child.stderr.on('data', read);
    child.on('error', error => { output += error.message; exited = true; finish(null); });
    child.on('exit', () => { exited = true; finish(null); });
    const timer = setTimeout(() => finish(null), wait);
  });
}

const TYPES = { '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.map': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp', '.ico': 'image/x-icon',
  '.wasm': 'application/wasm', '.onnx': 'application/octet-stream', '.txt': 'text/plain; charset=utf-8', '.woff2': 'font/woff2' };

/** Serves `root` on 127.0.0.1 (a free port). Resolves { url, stop }; `page` is opened first. */
export async function serveFolder(root, { page = 'index.html' } = {}) {
  root = path.resolve(root);
  const server = http.createServer(async (req, res) => {
    let rel;
    try { rel = decodeURIComponent(new URL(req.url, 'http://localhost').pathname); } catch { res.writeHead(400).end(); return; }
    let file = path.join(root, rel);
    if (file !== root && !file.startsWith(root + path.sep)) { res.writeHead(403).end(); return; }
    let info = await stat(file).catch(() => null);
    if (info?.isDirectory()) { file = path.join(file, 'index.html'); info = await stat(file).catch(() => null); }
    if (!info?.isFile()) { res.writeHead(404, { 'content-type': 'text/plain' }).end('Not found'); return; }
    res.writeHead(200, { 'content-type': TYPES[path.extname(file).toLowerCase()] ?? 'application/octet-stream', 'cache-control': 'no-store' });
    createReadStream(file).pipe(res);
  });
  await new Promise((resolve, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}/${page === 'index.html' ? '' : page.split('/').map(encodeURIComponent).join('/')}`;
  return { url, stop: () => { server.close(); server.closeAllConnections?.(); } };
}
