// The same key store as the Python tools (paradigm_orion/credentials.py): environment variables first, then
// $ORION_KEYS_FILE, else $XDG_CONFIG_HOME/orion/keys.env, else ~/.config/orion/keys.env, written readable only by you.
// A key the wizard saves is the one `orion label` and `orion pull` use later. Nothing here prints a key.
import { chmod, mkdir, open, readFile, rename, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const ALIASES = { GEMINI_API_KEY: ['GOOGLE_API_KEY'] };
const NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

export function keysFile(env = process.env) {
  if (env.ORION_KEYS_FILE) return env.ORION_KEYS_FILE.replace(/^~(?=$|\/)/, os.homedir());
  return path.join(env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config'), 'orion', 'keys.env');
}

function parseLine(line) {
  let text = line.trim();
  if (text.startsWith('export ')) text = text.slice(7).trimStart();
  const at = text.indexOf('=');
  const name = at < 0 ? '' : text.slice(0, at).trim();
  if (!NAME.test(name)) return [null, null];
  const value = text.slice(at + 1).trim();
  if (value.length >= 2 && value[0] === value.at(-1) && `"'`.includes(value[0])) return [name, value.slice(1, -1)];
  return [name, value.split(/\s+#/)[0]];
}

export async function readKeys(file = keysFile()) {
  try {
    return Object.fromEntries((await readFile(file, 'utf8')).split('\n').map(parseLine).filter(([n]) => n));
  } catch { return {}; }
}

const names = name => [name, ...(ALIASES[name] ?? [])];

/** { value, from } for a key: from is the environment variable's name or the keys file's path. Null when unset. */
export async function findKey(name, env = process.env) {
  for (const n of names(name)) if (env[n]?.trim()) return { value: env[n].trim(), from: `$${n}` };
  const stored = await readKeys(keysFile(env));
  for (const n of names(name)) if (stored[n]) return { value: stored[n], from: keysFile(env) };
  return null;
}

/** Merges keys into the keys file, keeping comments and other keys, in one atomic replace. Returns the path. */
export async function saveKeys(values, env = process.env) {
  const file = keysFile(env);
  for (const [name, value] of Object.entries(values)) {
    if (!NAME.test(name) || /\s/.test(value)) throw new Error(`Cannot save ${name}: key names are letters, digits and _, and keys have no spaces`);
  }
  await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  if (!env.ORION_KEYS_FILE) await chmod(path.dirname(file), 0o700);
  let lines = [];
  try { lines = (await readFile(file, 'utf8')).replace(/\n$/, '').split('\n'); } catch { /* a new file */ }
  const written = new Set();
  lines = lines.map(line => {
    const [name] = parseLine(line);
    if (name && name in values) { written.add(name); return `${name}=${values[name]}`; }
    return line;
  });
  for (const [name, value] of Object.entries(values)) if (!written.has(name)) lines.push(`${name}=${value}`);
  const tmp = path.join(path.dirname(file), `.keys-${process.pid}-${Date.now()}`);
  const handle = await open(tmp, 'wx', 0o600);
  try {
    await handle.writeFile(lines.filter((l, i) => l || i < lines.length - 1).join('\n') + '\n');
    await handle.sync();
    await handle.close();
    await rename(tmp, file);
  } catch (error) {
    await handle.close().catch(() => {});
    await rm(tmp, { force: true });
    throw error;
  }
  return file;
}
