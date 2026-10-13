// Signing in to PostHog in the browser, shared with the Python tools (paradigm_orion/posthog_auth.py): the same OAuth
// client, scopes, sign-in file (posthog-auth.json beside the keys file, readable only by you) and lock, so a sign-in
// made here is the one `orion pull` uses later. PostHog's consent page asks for one project, and the tokens carry
// exactly the scopes Orion needs. Nothing here prints a token.
import { createHash, randomBytes } from 'node:crypto';
import { mkdir, open, readFile, rename, rm, rmdir, stat } from 'node:fs/promises';
import { createServer } from 'node:http';
import path from 'node:path';
import { keysFile } from './keys.js';

export const CLIENT_ID = 'https://cdn.paradigmmachines.ai/orion/oauth/client-metadata.json';
export const SCOPES = ['project:read', 'query:read', 'batch_export:read', 'batch_export:write'];
const MARGIN = 120;  // seconds of life left below which a token is refreshed before use

export const clientId = (env = process.env) => env.ORION_POSTHOG_CLIENT_ID || CLIENT_ID;

export function authFile(env = process.env) {
  return env.ORION_POSTHOG_AUTH_FILE || path.join(path.dirname(keysFile(env)), 'posthog-auth.json');
}

async function read(env) {
  try { return JSON.parse(await readFile(authFile(env), 'utf8')); } catch { return {}; }
}

async function write(data, env) {
  const file = authFile(env);
  await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = path.join(path.dirname(file), `.auth-${process.pid}-${Date.now()}`);
  const handle = await open(tmp, 'wx', 0o600);
  try {
    await handle.writeFile(`${JSON.stringify(data, null, 2)}\n`);
    await handle.sync();
    await handle.close();
    await rename(tmp, file);
  } catch (error) {
    await handle.close().catch(() => {});
    await rm(tmp, { force: true });
    throw error;
  }
}

/** Runs fn holding the lock the Python tools use too: a folder only one process can create. */
async function locked(env, fn) {
  const lock = `${authFile(env)}.lock`;
  await mkdir(path.dirname(lock), { recursive: true, mode: 0o700 });
  const deadline = Date.now() + 30_000;
  for (;;) {
    try { await mkdir(lock); break; } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      const age = await stat(lock).then(s => Date.now() - s.mtimeMs, () => 0);
      if (age > 60_000) { await rmdir(lock).catch(() => {}); continue; }  // left by a process that crashed
      if (Date.now() > deadline) throw new Error(`Another Orion process is holding ${lock}; try again in a moment`);
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  }
  try { return await fn(); } finally { await rmdir(lock).catch(() => {}); }
}

const keyOf = (host, project) => `${host.replace(/\/+$/, '')} ${project}`;

/** The saved sign-in for this host and project; with no project, the host's only one. Null when there is none. */
export async function loginFor(host, project, env = process.env) {
  const logins = (await read(env)).logins ?? {};
  if (project) return logins[keyOf(host, project)] ?? null;
  const found = Object.values(logins).filter(r => r.host === host.replace(/\/+$/, ''));
  return found.length === 1 ? found[0] : null;
}

/** Keeps a sign-in, under `project` when PostHog left the choice of project to Orion. */
export async function saveLogin(record, env = process.env, project = null) {
  const saved = { ...record, project: String(project ?? record.project ?? '') || null };
  if (!saved.project) throw new Error('A PostHog sign-in needs a project');
  await locked(env, async () => {
    const data = await read(env);
    (data.logins ??= {})[keyOf(saved.host, saved.project)] = saved;
    await write(data, env);
  });
  return saved;
}

async function tokenRequest(host, fields, { fetch, env }) {
  let response;
  try {
    response = await fetch(`${host}/oauth/token/`, { method: 'POST', signal: AbortSignal.timeout(30_000),
      headers: { 'content-type': 'application/x-www-form-urlencoded', accept: 'application/json' },
      body: new URLSearchParams({ ...fields, client_id: clientId(env) }).toString() });
  } catch (error) {
    throw new Error(`Could not reach PostHog at ${host} (${error.cause?.code ?? error.message})`);
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw Object.assign(new Error(`PostHog refused the token request (HTTP ${response.status}${body.error ? `, ${body.error}` : ''})`), { status: response.status });
  }
  return body;
}

function recordFrom(host, reply, previous = {}, env = process.env) {
  const teams = reply.scoped_teams ?? [];
  return { host, project: teams.length === 1 ? String(teams[0]) : previous.project ?? null,
    access_token: reply.access_token, refresh_token: reply.refresh_token ?? previous.refresh_token,
    expires_at: Math.floor(Date.now() / 1000) + Number(reply.expires_in || 3600),
    scope: reply.scope ?? previous.scope ?? SCOPES.join(' '), client_id: clientId(env) };
}

const escape = text => String(text).replace(/[&<>"']/g, ch => `&#${ch.charCodeAt(0)};`);
const page = (ok, title, message) => `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Orion · ${escape(title)}</title><style>:root{color-scheme:light dark}body{margin:0;min-height:100vh;display:grid;place-items:center;font:16px/1.5 system-ui,sans-serif}
main{max-width:420px;padding:32px 24px;margin:16px;border:1px solid #8884;border-radius:12px}p{opacity:.7;margin:8px 0 0}</style>
<main><strong>${ok ? '✓' : '!'} ${escape(title)}</strong><p>${escape(message)}</p></main>`;

/**
 * Signs in through the browser and resolves with the sign-in: { host, project, ... }, saved when PostHog named the
 * project (with project null, choose one and saveLogin(record, env, id)). openUrl(url) shows PostHog's consent page.
 */
export async function signIn(host, { openUrl, fetch = globalThis.fetch, env = process.env, timeout = 600_000 } = {}) {
  host = host.replace(/\/+$/, '');
  const verifier = randomBytes(48).toString('base64url'), state = randomBytes(24).toString('base64url');
  const challenge = createHash('sha256').update(verifier).digest('base64url');
  let settle, redirect;
  const done = new Promise((resolve, reject) => { settle = { resolve, reject }; });
  const server = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const params = Object.fromEntries(url.searchParams);
    if (url.pathname !== '/callback' || params.state !== state) { res.writeHead(url.pathname === '/callback' ? 400 : 404).end(); return; }
    const reply = (ok, title, message) => res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }).end(page(ok, title, message));
    try {
      if (params.error) throw new Error(params.error === 'access_denied' ? 'PostHog sign-in was cancelled' : `PostHog sign-in failed: ${params.error_description || params.error}`);
      const tokens = await tokenRequest(host, { grant_type: 'authorization_code', code: params.code ?? '', redirect_uri: redirect, code_verifier: verifier }, { fetch, env });
      const record = recordFrom(host, tokens, {}, env);
      settle.resolve(record.project ? await saveLogin(record, env) : record);
      reply(true, 'Signed in to PostHog', 'Orion can now read this project. You can close this tab.');
    } catch (error) {
      settle.reject(error);
      reply(false, 'Not signed in', error.message);
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  redirect = `http://127.0.0.1:${server.address().port}/callback`;
  const url = `${host}/oauth/authorize/?${new URLSearchParams({ response_type: 'code', client_id: clientId(env), redirect_uri: redirect,
    scope: SCOPES.join(' '), state, code_challenge: challenge, code_challenge_method: 'S256', required_access_level: 'project' })}`;
  const timer = setTimeout(() => settle.reject(new Error(`PostHog did not answer within ${Math.round(timeout / 60_000)} minutes`)), timeout);
  try {
    openUrl(url);
    return await done;
  } finally {
    clearTimeout(timer);
    server.close();
    server.closeAllConnections?.();
  }
}

/** A working access token for the saved sign-in, refreshed under the shared lock when it is nearly used up. */
export async function accessToken(host, project, { fetch = globalThis.fetch, env = process.env, rejected = null } = {}) {
  const fresh = r => r && r.access_token !== rejected && r.expires_at - Date.now() / 1000 > MARGIN;
  const record = await loginFor(host, project, env);
  if (!record || fresh(record)) return record?.access_token ?? null;
  return locked(env, async () => {
    const data = await read(env);
    const current = data.logins?.[keyOf(host, project)];
    if (!current || fresh(current)) return current?.access_token ?? null;  // another process refreshed it meanwhile
    let tokens;
    try {
      tokens = await tokenRequest(current.host, { grant_type: 'refresh_token', refresh_token: current.refresh_token }, { fetch, env });
    } catch (error) {
      if ([400, 401, 403].includes(error.status)) throw new Error('The PostHog sign-in has ended. Sign in again (run this again, or `orion init`).');
      throw error;
    }
    data.logins[keyOf(host, project)] = recordFrom(current.host, tokens, current, env);
    await write(data, env);
    return data.logins[keyOf(host, project)].access_token;
  });
}
