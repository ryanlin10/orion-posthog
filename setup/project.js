// What the wizard learns about an app before anything changes: the framework, the package manager, and where PostHog
// starts in the browser. Reading here is local; nothing in this file sends anything anywhere.
import { execFile } from 'node:child_process';
import { readdir, readFile, stat } from 'node:fs/promises';
import path from 'node:path';

export const PACKAGE = '@paradigmmachines/orion';
// Skipped when walking a project: dependencies, build output, caches and tool folders.
export const SKIP_DIRS = new Set(['node_modules', 'bower_components', 'jspm_packages', 'dist', 'build', 'out', 'coverage',
  'vendor', 'target', 'tmp', 'venv', '__pycache__', 'Pods', 'DerivedData']);
const SOURCE = /\.(m?[jt]sx?|c[jt]s|vue|svelte|astro|html?|ejs|hbs|handlebars|erb|php|jinja2?|j2|liquid|twig|njk|pug)$/i;
const HTML = /\.(html?|ejs|hbs|handlebars|erb|php|jinja2?|j2|liquid|twig|njk|pug)$/i;
const MAX_BYTES = 1 << 20;

/** Every file under root, as relative paths with forward slashes, skipping SKIP_DIRS and dot folders. */
export async function walk(root, { limit = 50000 } = {}) {
  const found = [];
  async function visit(rel) {
    if (found.length >= limit) return;
    let entries;
    try { entries = await readdir(path.join(root, rel), { withFileTypes: true }); } catch { return; }
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      const child = rel ? `${rel}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        if (!entry.name.startsWith('.') && !SKIP_DIRS.has(entry.name)) await visit(child);
      } else if (entry.isFile()) {
        found.push(child);
        if (found.length >= limit) return;
      }
    }
  }
  await visit('');
  return found;
}

async function readText(file) {
  try {
    const info = await stat(file);
    return info.size > MAX_BYTES ? null : await readFile(file, 'utf8');
  } catch { return null; }
}

async function readJson(file) {
  try { return JSON.parse(await readFile(file, 'utf8')); } catch { return null; }
}

const deps = pkg => ({ ...pkg?.dependencies, ...pkg?.devDependencies });
const lineOf = (text, index) => text.slice(0, index).split('\n').length;
function excerpt(text, index) {
  const end = text.indexOf('\n', index);
  return text.slice(text.lastIndexOf('\n', index) + 1, end < 0 ? undefined : end).trim().slice(0, 120);
}

/** Where PostHog starts in one file, or nothing. Bundled code must import posthog-js; HTML must hold the snippet. */
export function posthogPlaces(file, text) {
  const places = [];
  const html = HTML.test(file);
  const imports = /from\s*['"]posthog-js(?:\/react)?['"]|require\(\s*['"]posthog-js['"]\s*\)|import\(\s*['"]posthog-js['"]\s*\)/.test(text);
  if (!html && !imports) return places;
  // The name posthog-js was imported as (import ph from 'posthog-js'), besides anything called posthog.
  const alias = text.match(/import\s+([\w$]+)\s*(?:,\s*\{[^}]*\}\s*)?from\s*['"]posthog-js['"]/)?.[1];
  for (const match of text.matchAll(/\b([\w$]+)\.init\s*\(|<PostHogProvider\b/g)) {
    const name = match[1];
    if (name && !(name === alias || /^posthog/i.test(name))) continue;
    if (html && !name) continue;
    places.push({ file, line: lineOf(text, match.index), text: excerpt(text, match.index),
      kind: html ? 'snippet' : match[0].startsWith('<') ? 'provider' : 'init' });
  }
  return places;
}

function orionPlaces(file, text) {
  return [...text.matchAll(/@paradigmmachines\/orion|\borion\.connect\s*\(|orion(?:\.min)?\.js|\/orion\/auto\.js/g)]
    .map(m => ({ file, line: lineOf(text, m.index), text: excerpt(text, m.index) }))
    .filter((p, i, all) => all.findIndex(q => q.file === p.file) === i);
}

function framework(pkg, files) {
  const d = deps(pkg);
  const has = name => name in d;
  const dirs = new Set(files.map(f => f.split('/').slice(0, -1).join('/')));
  if (has('next')) return [...dirs].some(x => /^(src\/)?app(\/|$)/.test(x)) ? 'Next.js (App Router)' : 'Next.js (Pages Router)';
  if (has('nuxt')) return 'Nuxt';
  if (has('@sveltejs/kit')) return 'SvelteKit';
  if (has('astro')) return 'Astro';
  if (has('@remix-run/react')) return 'Remix';
  if (has('@react-router/dev')) return 'React Router';
  if (has('gatsby')) return 'Gatsby';
  if (has('@angular/core')) return 'Angular';
  if (has('vue')) return has('vite') ? 'Vue (Vite)' : 'Vue';
  if (has('svelte')) return 'Svelte';
  if (has('solid-js')) return 'Solid';
  if (has('react')) return has('vite') ? 'React (Vite)' : has('react-scripts') ? 'React (Create React App)' : 'React';
  if (files.includes('manage.py')) return 'Django';
  if (files.includes('Gemfile')) return 'Rails';
  if (files.includes('composer.json')) return 'PHP';
  if (has('vite')) return 'Vite';
  return files.some(f => /\.html?$/i.test(f)) ? 'Static HTML' : pkg ? 'JavaScript' : 'Unknown';
}

const LOCKS = [['pnpm-lock.yaml', 'pnpm'], ['yarn.lock', 'yarn'], ['bun.lock', 'bun'], ['bun.lockb', 'bun'], ['package-lock.json', 'npm']];
export const LOCKFILES = new Set(LOCKS.map(([file]) => file).concat('npm-shrinkwrap.json'));

/** The package manager from the nearest lockfile or packageManager field, from dir up through any workspace. */
async function packageManager(dir, root) {
  const exists = file => stat(file).then(() => true, () => false);
  for (let at = dir; ; at = path.dirname(at)) {
    for (const [file, name] of LOCKS) {
      if (await exists(path.join(at, file))) return name;
    }
    const declared = (await readJson(path.join(at, 'package.json')))?.packageManager;
    if (declared) return declared.split('@')[0];
    const parent = path.dirname(at);
    // Inside the project keep climbing; above it, only while the parent is a workspace with its own package.json.
    if (parent === at || (!at.startsWith(root + path.sep) && !(await exists(path.join(parent, 'package.json'))))) return 'npm';
  }
}

export function installCommand(manager, spec) {
  const target = `${PACKAGE}@${spec}`;
  return { pnpm: ['pnpm', ['add', target]], yarn: ['yarn', ['add', target]], bun: ['bun', ['add', target]] }[manager]
    ?? ['npm', ['install', target]];
}

function git(root) {
  return new Promise(resolve => execFile('git', ['-C', root, 'status', '--porcelain'], { timeout: 5000 }, (error, out) =>
    resolve(error ? { repo: false, dirty: 0 } : { repo: true, dirty: out.split('\n').filter(Boolean).length })));
}

// PostHog's public project key and region, for checking events later. .env files are read here, on this machine,
// only for these two values; they are never shown to an AI.
async function posthogSettings(root, files, texts) {
  const envs = files.filter(f => path.posix.basename(f).startsWith('.env'));
  const all = [...texts.values()];
  for (const name of envs) all.push(await readText(path.join(root, name)) ?? '');
  const joined = all.join('\n');
  const token = joined.match(/\b(phc_[A-Za-z0-9]{20,})\b/)?.[1] ?? null;
  const host = joined.match(/https?:\/\/(?:(us|eu)(?:-assets)?\.i\.posthog\.com|(us|eu|app)\.posthog\.com)/)?.slice(1).find(Boolean) ?? null;
  return { token, region: host === 'app' ? 'us' : host };
}

export async function detect(root) {
  root = path.resolve(root);
  const files = await walk(root);
  const pkg = await readJson(path.join(root, 'package.json'));
  const texts = new Map();
  const places = [], orion = [];
  for (const file of files.filter(f => SOURCE.test(f))) {
    const text = await readText(path.join(root, file));
    if (text == null) continue;
    const found = posthogPlaces(file, text);
    if (found.length) { places.push(...found); texts.set(file, text); }
    orion.push(...orionPlaces(file, text));
  }
  // Bundled apps first: a posthog-js import beats an HTML snippet in the same project.
  const bundled = places.filter(p => p.kind !== 'snippet');
  // Entry pages first (index.html, main.tsx), so the agent and the copy-it-yourself steps start where people look.
  const chosen = (bundled.length ? bundled : places).sort((a, b) => /(^|\/)(index|main)\./.test(b.file) - /(^|\/)(index|main)\./.test(a.file));
  const kind = bundled.length ? 'npm' : places.length ? 'snippet' : null;

  let packageDir = null;
  if (kind === 'npm') {
    // Install where posthog-js is declared: the nearest package.json above the first init that lists it, else the root.
    for (let at = path.dirname(path.join(root, chosen[0].file)); at !== root && at.startsWith(root); at = path.dirname(at)) {
      if ('posthog-js' in deps(await readJson(path.join(at, 'package.json')))) { packageDir = at; break; }
    }
    packageDir ??= root;
  }
  const appPkg = packageDir ? await readJson(path.join(packageDir, 'package.json')) : pkg;
  const manager = await packageManager(packageDir ?? root, root);
  const script = ['dev', 'start', 'serve'].find(s => appPkg?.scripts?.[s]);
  return {
    root,
    name: appPkg?.name ?? pkg?.name ?? path.basename(root),
    framework: framework(appPkg ?? pkg, files),
    files: files.length,
    paths: files,
    hasPackageJson: Boolean(pkg || appPkg),
    htmlFiles: files.filter(f => /\.html?$/i.test(f)).length,
    packageManager: manager,
    packageDir,
    devCommand: script ? (manager === 'npm' ? `npm run ${script}` : `${manager} ${script}`) : null,
    posthog: { kind, places: chosen, ...(await posthogSettings(root, files, texts)) },
    orion: { installed: PACKAGE in deps(appPkg), places: orion },
    git: await git(root),
  };
}

/** Where to copy Orion's browser files for a site that loads PostHog from its HTML snippet. */
export function vendorDir(project) {
  const top = new Set(project.paths.map(f => f.split('/')[0]));
  if (top.has('public')) return 'public/orion';
  if (top.has('static')) return 'static/orion';
  const first = project.posthog.places[0]?.file ?? 'index.html';
  const dir = path.posix.dirname(first);
  return dir === '.' ? 'orion' : `${dir}/orion`;
}
