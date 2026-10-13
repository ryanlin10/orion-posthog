// The last step: watch PostHog until Orion's first events arrive from the developer's browser, with a browser sign-in
// (posthog-auth.js) or a personal API key; either is the one `orion pull` uses later. Reads counts only.
export const HOSTS = { us: 'https://us.posthog.com', eu: 'https://eu.posthog.com' };
export const SCOPES = 'project:read, query:read, batch_export:read and batch_export:write';

class PostHogError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function call(fetch, url, key, body) {
  let response;
  try {
    response = await fetch(url, { method: body ? 'POST' : 'GET', signal: AbortSignal.timeout(20_000),
      headers: { authorization: `Bearer ${key}`, ...(body && { 'content-type': 'application/json' }) },
      ...(body && { body: JSON.stringify(body) }) });
  } catch (error) {
    throw new PostHogError(`Could not reach PostHog (${error.cause?.code ?? error.message})`);
  }
  if (response.ok) return response.json();
  const why = { 401: 'PostHog did not accept the key', 403: `The key needs the ${SCOPES} scopes` }[response.status] ?? `PostHog returned HTTP ${response.status}`;
  throw new PostHogError(why, response.status);
}

/**
 * The PostHog cloud the key belongs to and its projects; the project whose public key matches `token` is picked.
 * Tries the region the code names first, then the other one, since a personal key works in one region only.
 */
export async function findProject({ key, token, region, fetch = globalThis.fetch }) {
  const order = region === 'eu' ? ['eu', 'us'] : ['us', 'eu'];
  let last;
  for (const at of order) {
    try {
      const projects = (await call(fetch, `${HOSTS[at]}/api/projects/`, key)).results ?? [];
      const match = projects.find(p => token && p.api_token === token);
      return { host: HOSTS[at], projects, project: match ?? (projects.length === 1 ? projects[0] : null) };
    } catch (error) {
      last = error;
      if (error.status !== 401) throw error;
    }
  }
  throw last;
}

/** One project's details (name, public api_token) as the key or sign-in sees them. */
export async function projectInfo({ host, key, projectId, fetch = globalThis.fetch }) {
  return call(fetch, `${host}/api/projects/${projectId}/`, key);
}

/** Orion events since `seconds` ago: { count, latest, browser, host }. */
export async function orionEvents({ host, key, projectId, seconds, fetch = globalThis.fetch }) {
  const query = `select count(), max(timestamp), argMax(properties.$browser, timestamp), argMax(properties.$host, timestamp)
    from events where event in ('orion_session_start', 'orion_input') and timestamp > now() - interval ${Math.ceil(seconds)} second`;
  const reply = await call(fetch, `${host}/api/projects/${projectId}/query/`, key, { query: { kind: 'HogQLQuery', query } });
  const [count, latest, browser, siteHost] = reply.results?.[0] ?? [0];
  return { count: Number(count) || 0, latest, browser, host: siteHost };
}

/** Polls until events arrive (resolves with them), the signal aborts or `limit` ms pass (both resolve null). */
export async function waitForEvents({ since, signal, interval = 4000, limit = 15 * 60_000, onCheck = () => {}, sleep, ...options }) {
  const started = Date.now();
  const pause = sleep ?? (ms => new Promise(resolve => { const t = setTimeout(resolve, ms); signal?.addEventListener('abort', () => { clearTimeout(t); resolve(); }, { once: true }); }));
  while (!signal?.aborted && Date.now() - started < limit) {
    const found = await orionEvents({ ...options, seconds: (Date.now() - since) / 1000 + 60 });
    onCheck(found);
    if (found.count > 0) return found;
    await pause(interval);
  }
  return null;
}
