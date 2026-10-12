// The files side of the edit step. Orion's own agent works through a workspace that keeps it inside the project and
// never opens .env files or keys. Whoever edits, a snapshot taken beforehand finds every change, so the developer
// sees each one and can undo all of them.
import { lstat, mkdir, readFile, readdir, realpath, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { LOCKFILES, SKIP_DIRS, walk } from './project.js';

const SECRET = /(^|\/)(\.env[^/]*|.*\.(pem|key|p12|pfx)|id_(rsa|ed25519|ecdsa)[^/]*|keys\.env|\.npmrc|\.netrc|credentials(\.json)?)$/i;
const MAX_READ = 200_000;

export class ToolError extends Error {}

/** Resolves a model-given path inside root, or throws. Symlinks may not lead out of the project either. */
async function inside(root, given, { write = false } = {}) {
  const rel = path.posix.normalize(String(given ?? '.').replaceAll('\\', '/').replace(/^\/+/, '') || '.');
  const full = path.resolve(root, rel);
  if (full !== root && !full.startsWith(root + path.sep)) throw new ToolError(`${given} is outside the project`);
  const parts = rel.split('/');
  if (parts.includes('.git')) throw new ToolError('The .git folder is off limits');
  if (SECRET.test(rel)) throw new ToolError(`${rel} may hold secrets, so it stays closed. Ask the developer instead.`);
  if (write && (parts.includes('node_modules') || LOCKFILES.has(path.basename(rel)))) {
    throw new ToolError(`${rel} is managed by the package manager; leave it as it is`);
  }
  // Symlinks may not lead out either: check the nearest part of the path that exists (a new file's folder).
  let probe = full;
  while (probe !== root && !(await lstat(probe).then(() => true, () => false))) probe = path.dirname(probe);
  const [real, realRoot] = await Promise.all([realpath(probe).catch(() => null), realpath(root)]);
  if (!real || (real !== realRoot && !real.startsWith(realRoot + path.sep))) throw new ToolError(`${given} leads outside the project`);
  return { rel, full };
}

export function createWorkspace(root) {
  root = path.resolve(root);
  return {
    async list({ path: given }) {
      const { rel, full } = await inside(root, given);
      let entries;
      try { entries = await readdir(full, { withFileTypes: true }); } catch { throw new ToolError(`No folder ${rel}`); }
      const lines = entries.sort((a, b) => a.name.localeCompare(b.name)).slice(0, 400).map(e => {
        if (!e.isDirectory()) return e.name;
        return SKIP_DIRS.has(e.name) || e.name === '.git' ? `${e.name}/ (skipped)` : `${e.name}/`;
      });
      return lines.join('\n') || '(empty)';
    },
    async read({ path: given }) {
      const { rel, full } = await inside(root, given);
      let text;
      try { text = await readFile(full, 'utf8'); } catch { throw new ToolError(`No file ${rel}`); }
      if (text.includes('\u0000')) throw new ToolError(`${rel} is not a text file`);
      return text.length > MAX_READ ? `${text.slice(0, MAX_READ)}\n… (cut at ${MAX_READ} characters)` : text;
    },
    async search({ pattern, path: given }) {
      const { rel } = await inside(root, given);
      let regex;
      try { regex = new RegExp(pattern); } catch { regex = new RegExp(pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')); }
      const base = rel === '.' ? root : path.join(root, rel);
      const files = (await stat(base).catch(() => null))?.isFile() ? [rel] : (await walk(base, { limit: 20000 })).map(f => (rel === '.' ? f : `${rel}/${f}`));
      const hits = [];
      for (const file of files) {
        if (SECRET.test(file) || !/\.(m?[jt]sx?|c[jt]s|vue|svelte|astro|html?|ejs|hbs|erb|php|jinja2?|j2|liquid|twig|njk|pug|json|ya?ml|toml|md|css|scss)$/i.test(file)) continue;
        const text = await readFile(path.join(root, file), 'utf8').catch(() => '');
        if (text.length > 1 << 20) continue;
        text.split('\n').forEach((line, i) => { if (hits.length < 100 && regex.test(line)) hits.push(`${file}:${i + 1}: ${line.trim().slice(0, 200)}`); });
        if (hits.length >= 100) break;
      }
      return hits.join('\n') || 'No matches';
    },
    async edit({ path: given, old_text: before, new_text: after }) {
      const { rel, full } = await inside(root, given, { write: true });
      let text;
      try { text = await readFile(full, 'utf8'); } catch { throw new ToolError(`No file ${rel}; use create_file for a new one`); }
      const count = before ? text.split(before).length - 1 : 0;
      if (count !== 1) throw new ToolError(count ? `old_text appears ${count} times in ${rel}; include more lines so it appears once` : `old_text was not found in ${rel}; read the file again and copy the text exactly`);
      await writeFile(full, text.replace(before, () => after));
      return `Edited ${rel}`;
    },
    async create({ path: given, content }) {
      const { rel, full } = await inside(root, given, { write: true });
      if (await stat(full).then(() => true, () => false)) throw new ToolError(`${rel} exists already; use edit_file`);
      await mkdir(path.dirname(full), { recursive: true });
      await writeFile(full, content);
      return `Created ${rel}`;
    },
  };
}

/** Every text file under root up to a size budget, to find and undo what any agent or installer changes later. */
export async function snapshot(root, { maxFile = 2 << 20, budget = 256 << 20 } = {}) {
  root = path.resolve(root);
  const files = new Map();
  let used = 0, partial = false;
  for (const rel of await walk(root)) {
    const full = path.join(root, rel);
    const info = await stat(full).catch(() => null);
    if (!info) continue;
    let content;
    if (info.size <= maxFile && used + info.size <= budget) {
      const buffer = await readFile(full).catch(() => null);
      content = buffer && !buffer.includes(0) ? buffer.toString('utf8') : undefined;
      if (content !== undefined) used += info.size;
    } else partial = true;
    files.set(rel, { size: info.size, mtime: info.mtimeMs, content });
  }
  return {
    root, partial,
    /** What differs from the snapshot now: [{ path, before, after }], before null for a new file, after null for a deleted one. */
    async changes() {
      const now = new Set(await walk(root));
      const out = [];
      for (const rel of now) {
        const full = path.join(root, rel);
        const old = files.get(rel);
        const info = await stat(full).catch(() => null);
        if (!info) continue;
        if (old && old.size === info.size && old.mtime === info.mtimeMs) continue;
        const after = info.size <= maxFile ? await readFile(full, 'utf8').catch(() => null) : null;
        if (old?.content !== undefined && old?.content === after) continue;
        out.push({ path: rel, before: old ? old.content : null, after });
      }
      for (const rel of files.keys()) if (!now.has(rel)) out.push({ path: rel, before: files.get(rel).content, after: null });
      return out.sort((a, b) => a.path.localeCompare(b.path));
    },
  };
}

/** Puts every changed file back as it was. Files whose old content was too big to keep are reported, not touched. */
export async function restore(root, changes) {
  const kept = [];
  for (const change of changes) {
    const full = path.join(root, change.path);
    if (change.before === null) await rm(full, { force: true });
    else if (change.before === undefined) kept.push(change.path);
    else { await mkdir(path.dirname(full), { recursive: true }); await writeFile(full, change.before); }
  }
  return kept;
}

/** A short line diff: the changed lines with one line of context, at most `limit` lines per file. */
export function diffLines(before, after, limit = 40) {
  const a = before == null ? [] : before.split('\n');
  const b = after == null ? [] : after.split('\n');
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length, endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
  const midA = a.slice(start, endA), midB = b.slice(start, endB);
  // Longest common subsequence over the changed middle, small enough here to do directly.
  const ops = [];
  if (midA.length * midB.length <= 250_000) {
    const table = Array.from({ length: midA.length + 1 }, () => new Uint16Array(midB.length + 1));
    for (let i = midA.length - 1; i >= 0; i--) {
      for (let j = midB.length - 1; j >= 0; j--) {
        table[i][j] = midA[i] === midB[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
      }
    }
    let i = 0, j = 0;
    while (i < midA.length || j < midB.length) {
      if (i < midA.length && j < midB.length && midA[i] === midB[j]) { ops.push([' ', midA[i]]); i++; j++; }
      else if (j < midB.length && (i === midA.length || table[i][j + 1] >= table[i + 1][j])) ops.push(['+', midB[j++]]);
      else ops.push(['-', midA[i++]]);
    }
  } else {
    midA.forEach(l => ops.push(['-', l]));
    midB.forEach(l => ops.push(['+', l]));
  }
  if (before != null && start > 0) ops.unshift([' ', a[start - 1]]);
  if (before != null && endA < a.length) ops.push([' ', a[endA]]);
  // Keep one line of context around each change; fold longer unchanged runs.
  const shown = [];
  const changed = op => op && op[0] !== ' ';
  ops.forEach((op, i) => {
    if (changed(op) || changed(ops[i - 1]) || changed(ops[i + 1])) shown.push(op);
    else if (shown.at(-1)?.[0] !== '…') shown.push(['…', '']);
  });
  const added = ops.filter(op => op[0] === '+').length, removed = ops.filter(op => op[0] === '-').length;
  return { added, removed, lines: shown.slice(0, limit), more: Math.max(0, shown.length - limit) };
}
