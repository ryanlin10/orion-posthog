// Signs in to PostHog in the browser for the event check, so a new user needs no personal API key: OAuth with PKCE,
// a client registered on the spot (PostHog supports dynamic client registration), only the two read scopes the check
// uses, and a callback on this machine. The token stays in memory and is revoked when the check ends.
import { createHash, randomBytes } from 'node:crypto';
import http from 'node:http';

const OAUTH = 'https://oauth.posthog.com';
const SIGN_IN_SCOPES = 'project:read query:read';

const page = (title, text) => `<!doctype html><meta charset="utf-8"><title>${title}</title>
<body style="font:16px system-ui,sans-serif;max-width:32rem;margin:20vh auto;padding:0 1rem;color:#222">
<h1 style="font-size:1.3rem">${title}</h1><p>${text}</p></body>`;

async function post(fetch, url, body) {
  let response;
  try {
    response = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
      signal: AbortSignal.timeout(20_000) });
  } catch (error) {
    throw new Error(`Could not reach PostHog (${error.cause?.code ?? error.message})`);
  }
  const reply = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(`PostHog sign-in failed: ${reply.error_description ?? reply.error ?? `HTTP ${response.status}`}`);
  return reply;
}

/**
 * Opens PostHog's sign-in page and waits for the developer to approve; onUrl gets a short local address that leads
 * there, for when no browser opens. Resolves { key, region, projectIds, revoke }
 * (key is a short-lived access token), or null when `signal` aborts first. Throws on a refusal or failure.
 */
export async function signIn({ open, onUrl = () => {}, signal, fetch = globalThis.fetch, base = OAUTH, timeout = 10 * 60_000 }) {
  const verifier = randomBytes(32).toString('base64url');
  const challenge = createHash('sha256').update(verifier).digest('base64url');
  const state = randomBytes(16).toString('base64url');
  let settle, authorize = null;
  const answered = new Promise(resolve => { settle = resolve; });
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    // A short address to show and open; it forwards to PostHog's (long) sign-in page.
    if (url.pathname === '/' && authorize) { res.writeHead(302, { location: authorize }).end(); return; }
    if (url.pathname !== '/callback' || url.searchParams.get('state') !== state) { res.writeHead(404).end(); return; }
    const code = url.searchParams.get('code');
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', connection: 'close' }).end(code
      ? page('PostHog approval received', 'Return to the terminal to finish signing in and check your events. You can close this tab.')
      : page('Sign-in cancelled', 'Go back to the terminal to paste a key or skip the check.'));
    settle(code ? { code } : { refused: url.searchParams.get('error_description') ?? url.searchParams.get('error') ?? 'cancelled' });
  });
  await new Promise((resolve, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolve));
  const local = `http://127.0.0.1:${server.address().port}`;
  const redirect = `${local}/callback`;
  const stop = new Promise(resolve => {
    const timer = setTimeout(() => resolve({ timedOut: true }), timeout);
    timer.unref?.();
    signal?.addEventListener('abort', () => { clearTimeout(timer); resolve({ aborted: true }); }, { once: true });
  });
  try {
    const client = await post(fetch, `${base}/oauth/register/`, {
      client_name: 'Orion setup', redirect_uris: [redirect], grant_types: ['authorization_code'], response_types: ['code'],
      token_endpoint_auth_method: 'none', scope: SIGN_IN_SCOPES });
    const url = new URL(`${base}/oauth/authorize/`);
    for (const [name, value] of Object.entries({ client_id: client.client_id, redirect_uri: redirect, response_type: 'code',
      scope: SIGN_IN_SCOPES, code_challenge: challenge, code_challenge_method: 'S256', state, required_access_level: 'project' })) {
      url.searchParams.set(name, value);
    }
    authorize = url.href;
    onUrl(`${local}/`);
    open(`${local}/`);
    const result = await Promise.race([answered, stop]);
    if (result.aborted) return null;
    if (result.timedOut) throw new Error('PostHog sign-in timed out');
    if (result.refused) throw new Error(`PostHog sign-in was not approved (${result.refused})`);
    const token = await post(fetch, `${base}/oauth/token/`, { grant_type: 'authorization_code', code: result.code,
      redirect_uri: redirect, client_id: client.client_id, code_verifier: verifier });
    if (!token.access_token) throw new Error('PostHog sign-in returned no token');
    return {
      key: token.access_token,
      region: token.posthog_region ?? null,
      projectIds: token.scoped_teams ?? [],
      // Best effort: the token is short-lived anyway.
      revoke: () => fetch(`${base}/oauth/revoke/`, { method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ token: token.access_token, client_id: client.client_id }), signal: AbortSignal.timeout(5000) }).catch(() => {}),
    };
  } finally {
    server.close();
    server.closeAllConnections?.();
  }
}
