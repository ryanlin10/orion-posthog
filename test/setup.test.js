import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { chmod, cp, mkdtemp, readFile, rm, stat, symlink, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { PassThrough } from 'node:stream';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { runAgent } from '../setup/agent.js';
import { brief, manualSteps, optionsLiteral, scriptSrc } from '../setup/brief.js';
import { claudeEvent, codexEvent, runCodingAgent } from '../setup/coding-agents.js';
import { createWorkspace, diffLines, restore, snapshot, ToolError } from '../setup/files.js';
import { findKey, readKeys, saveKeys } from '../setup/keys.js';
import { main } from '../setup/main.js';
import { accessToken, authFile, loginFor, SCOPES as SIGN_IN_SCOPES, signIn } from '../setup/posthog-auth.js';
import { detect, installCommand, posthogPlaces, vendorDir } from '../setup/project.js';
import { checkKey, createClient, providerFor } from '../setup/providers.js';
import { createUI } from '../setup/ui.js';
import { findProject, orionEvents, waitForEvents } from '../setup/verify.js';

const APPS = path.join(path.dirname(fileURLToPath(import.meta.url)), 'fixtures', 'apps');

async function copyApp(name, extra = {}) {
  const dir = await mkdtemp(path.join(os.tmpdir(), `orion-setup-${name}-`));
  await cp(path.join(APPS, name), dir, { recursive: true });
  for (const [file, text] of Object.entries(extra)) await writeFile(path.join(dir, file), text);
  return dir;
}

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

test('detect: framework, package manager, where PostHog starts, and its key and region', async () => {
  const vite = await detect(await copyApp('vite-react', { '.env.local': 'VITE_POSTHOG_KEY=phc_vite1234567890abcdefghijklmnop\n' }));
  assert.equal(vite.framework, 'React (Vite)');
  assert.equal(vite.packageManager, 'npm');
  assert.equal(vite.posthog.kind, 'npm');
  assert.deepEqual(vite.posthog.places.map(p => `${p.file}:${p.line}`), ['src/main.tsx:6']);
  assert.equal(vite.posthog.token, 'phc_vite1234567890abcdefghijklmnop', 'the public key comes from .env, read locally');
  assert.equal(vite.posthog.region, 'eu');
  assert.equal(vite.devCommand, 'npm run dev');

  const next = await detect(path.join(APPS, 'next-app'));
  assert.equal(next.framework, 'Next.js (App Router)');
  assert.equal(next.packageManager, 'pnpm');
  assert.equal(next.devCommand, 'pnpm dev');
  assert.equal(next.posthog.places[0].file, 'app/providers.tsx');

  const site = await detect(path.join(APPS, 'static-site'));
  assert.equal(site.posthog.kind, 'snippet');
  assert.equal(site.posthog.places[0].file, 'index.html', 'entry pages first');
  assert.equal(site.posthog.places.length, 2);
  assert.equal(site.packageDir, null);
  assert.equal(vendorDir(site), 'orion');

  const none = await detect(path.join(APPS, 'no-posthog'));
  assert.equal(none.posthog.kind, null);
});

test('detect: only real PostHog starts count', () => {
  assert.equal(posthogPlaces('a.ts', "import i18n from 'i18next'\ni18n.init({})").length, 0, 'no posthog-js import');
  assert.equal(posthogPlaces('a.ts', "import posthog from 'posthog-js'\nimport i18n from 'i18next'\ni18n.init({})").length, 0);
  assert.equal(posthogPlaces('a.ts', "import ph from 'posthog-js'\nph.init('phc_x')")[0].kind, 'init');
  assert.equal(posthogPlaces('a.tsx', "import { PostHogProvider } from 'posthog-js/react'\n<PostHogProvider apiKey={k}>")[0].kind, 'provider');
  assert.equal(posthogPlaces('a.html', '<script>analytics.init()</script>').length, 0);
  assert.deepEqual(installCommand('pnpm', 'github:o/r'), ['pnpm', ['add', '@paradigmmachines/orion@github:o/r']]);
  assert.deepEqual(installCommand('npm', 'github:o/r'), ['npm', ['install', '@paradigmmachines/orion@github:o/r']]);
});

test('brief: the same task for every agent, and the copy-it-yourself code', async () => {
  const vite = await detect(path.join(APPS, 'vite-react'));
  const options = { mask: 'timing', mood: true };
  assert.equal(optionsLiteral(options), "{ mask: 'timing', mood: true }");
  const task = brief(vite, options);
  assert.match(task, /import \{ orion \} from '@paradigmmachines\/orion'/);
  assert.match(task, /orion\.connect\(posthog, \{ mask: 'timing', mood: true \}\)/);
  assert.match(task, /src\/main\.tsx:6/);
  assert.match(task, /Never open \.env files/);
  assert.match(manualSteps(vite, options).join('\n'), /In src\/main\.tsx, next to posthog\.init \(line 6\)/);

  const site = await detect(path.join(APPS, 'static-site'));
  assert.match(brief(site, { mask: 'off' }, { vendor: 'orion' }), /<script type="module" src="\.\/orion\/auto\.js"><\/script>/);
  assert.equal(scriptSrc('public/orion', 'src/pages/a.html'), '/orion/auto.js');
  assert.equal(scriptSrc('site/orion', 'site/blog/post.html'), '../orion/auto.js');
});

test('workspace: stays inside the project, keeps secrets closed, and edits exactly once', async () => {
  const dir = await copyApp('vite-react', { '.env.local': 'VITE_POSTHOG_KEY=phc_secret\n' });
  const ws = createWorkspace(dir);
  await assert.rejects(ws.read({ path: '../outside.txt' }), ToolError);
  await assert.rejects(ws.read({ path: '/etc/passwd' }), ToolError, 'absolute paths are taken as project-relative');
  await assert.rejects(ws.read({ path: '.env.local' }), /secrets/);
  await assert.rejects(ws.edit({ path: 'package-lock.json', old_text: '{', new_text: '[' }), /package manager/);
  assert.match(await ws.list({ path: '.' }), /src\//);
  assert.match(await ws.search({ pattern: 'posthog\\.init', path: '.' }), /src\/main\.tsx:6:/);
  assert.doesNotMatch(await ws.search({ pattern: 'phc_', path: '.' }), /secret/, 'search skips .env files');
  await assert.rejects(ws.edit({ path: 'src/main.tsx', old_text: 'nothing like this', new_text: 'x' }), /not found/);
  await assert.rejects(ws.edit({ path: 'src/main.tsx', old_text: 'import', new_text: 'x' }), /appears 5 times/);
  const before = await snapshot(dir);
  await ws.edit({ path: 'src/main.tsx', old_text: "import App from './App'", new_text: "import App from './App'\nimport { orion } from '@paradigmmachines/orion'" });
  await ws.create({ path: 'src/orion.ts', content: 'export {}\n' });
  await assert.rejects(ws.create({ path: 'src/orion.ts', content: '' }), /exists/);
  assert.deepEqual((await before.changes()).map(c => [c.path, c.before === null]), [['src/main.tsx', false], ['src/orion.ts', true]]);
  await rm(dir, { recursive: true });
});

test('workspace: links that lead out of the project are refused, dangling ones included', async () => {
  const dir = await copyApp('vite-react');
  const outside = await mkdtemp(path.join(os.tmpdir(), 'orion-outside-'));
  await writeFile(path.join(outside, 'secret.txt'), 'x');
  await symlink(path.join(outside, 'secret.txt'), path.join(dir, 'src/link.txt'));
  await symlink(path.join(outside, 'not-yet.txt'), path.join(dir, 'src/dangling.ts'));
  const ws = createWorkspace(dir);
  await assert.rejects(ws.read({ path: 'src/link.txt' }), /leads outside/);
  await assert.rejects(ws.create({ path: 'src/dangling.ts', content: 'x' }), /leads outside/);
  await assert.rejects(stat(path.join(outside, 'not-yet.txt')), 'nothing was written outside');
  await rm(dir, { recursive: true });
  await rm(outside, { recursive: true });
});

test('snapshot: finds new, changed and deleted files, and restore puts them back', async () => {
  const dir = await copyApp('next-app');
  const before = await snapshot(dir);
  await writeFile(path.join(dir, 'app/providers.tsx'), 'changed\n');
  await writeFile(path.join(dir, 'app/orion.tsx'), 'new\n');
  await rm(path.join(dir, 'app/page.tsx'));
  const changes = await before.changes();
  assert.deepEqual(changes.map(c => [c.path, c.before === null ? 'new' : c.after === null ? 'deleted' : 'changed']),
    [['app/orion.tsx', 'new'], ['app/page.tsx', 'deleted'], ['app/providers.tsx', 'changed']]);
  assert.deepEqual(await restore(dir, changes), []);
  assert.deepEqual(await (await snapshot(dir)).changes(), []);
  assert.equal(await readFile(path.join(dir, 'app/providers.tsx'), 'utf8'), await readFile(path.join(APPS, 'next-app/app/providers.tsx'), 'utf8'));
  await assert.rejects(stat(path.join(dir, 'app/orion.tsx')));
  await rm(dir, { recursive: true });
});

test('diffLines: changed lines with one line of context, separate hunks folded', () => {
  const a = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'].join('\n');
  const b = ['a', 'b', 'B2', 'c', 'd', 'e', 'f', 'g', 'G2', 'h'].join('\n');
  const diff = diffLines(a, b);
  assert.equal(diff.added, 2);
  assert.equal(diff.removed, 0);
  assert.deepEqual(diff.lines, [[' ', 'b'], ['+', 'B2'], [' ', 'c'], ['…', ''], [' ', 'g'], ['+', 'G2'], [' ', 'h']]);
  assert.deepEqual(diffLines(null, 'x\ny').lines, [['+', 'x'], ['+', 'y']]);
});

test('keys: the same file as the Python tools, comments kept, readable only by you', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'orion-keys-'));
  const env = { ORION_KEYS_FILE: path.join(dir, 'keys.env') };
  await writeFile(env.ORION_KEYS_FILE, '# mine\nexport OPENAI_API_KEY="sk-old"\n');
  await saveKeys({ OPENAI_API_KEY: 'sk-new', POSTHOG_PERSONAL_API_KEY: 'phx_1' }, env);
  assert.equal(await readFile(env.ORION_KEYS_FILE, 'utf8'), '# mine\nOPENAI_API_KEY=sk-new\nPOSTHOG_PERSONAL_API_KEY=phx_1\n');
  assert.equal((await stat(env.ORION_KEYS_FILE)).mode & 0o777, 0o600);
  assert.deepEqual(await findKey('OPENAI_API_KEY', env), { value: 'sk-new', from: env.ORION_KEYS_FILE });
  assert.deepEqual(await findKey('GEMINI_API_KEY', { ...env, GOOGLE_API_KEY: 'g' }), { value: 'g', from: '$GOOGLE_API_KEY' });
  assert.deepEqual(await readKeys(path.join(dir, 'missing')), {});
  await assert.rejects(saveKeys({ 'bad name': 'x' }, env));
});

// A scripted model: look, read, edit twice in one turn, then a one-line answer. Each provider's fake speaks its own
// wire format, and every request after the first is checked for the previous reply sent back unchanged.
const EDITS = [
  { path: 'src/main.tsx', old_text: "import App from './App'", new_text: "import App from './App'\nimport { orion } from '@paradigmmachines/orion'" },
  { path: 'src/main.tsx', old_text: "  person_profiles: 'identified_only',\n})", new_text: "  person_profiles: 'identified_only',\n})\norion.connect(posthog, { mask: 'timing', mood: true })" },
];
const SCRIPT = [
  [['list_files', { path: '.' }], ['read_file', { path: 'src/main.tsx' }]],
  [['edit_file', EDITS[0]], ['edit_file', EDITS[1]]],
  'I changed src/main.tsx to connect Orion after posthog.init.',
];

function fakeProvider(provider) {
  const requests = [];
  const fetch = async (url, init) => {
    if (!init.body) return json({ data: [] });  // the key check lists models
    const body = JSON.parse(init.body);
    requests.push({ url, headers: init.headers, body });
    const step = SCRIPT[requests.length - 1];
    if (provider === 'anthropic') {
      if (typeof step === 'string') return json({ stop_reason: 'end_turn', content: [{ type: 'text', text: step }] });
      return json({ stop_reason: 'tool_use', content: [{ type: 'thinking', thinking: '', signature: `sig-${requests.length}` },
        ...step.map(([name, input], i) => ({ type: 'tool_use', id: `tu_${requests.length}_${i}`, name, input }))] });
    }
    if (provider === 'openai') {
      if (typeof step === 'string') return json({ status: 'completed', output: [{ type: 'message', role: 'assistant', content: [{ type: 'output_text', text: step }] }] });
      return json({ status: 'completed', output: [{ type: 'reasoning', id: `rs_${requests.length}`, encrypted_content: 'enc', summary: [] },
        ...step.map(([name, input], i) => ({ type: 'function_call', call_id: `call_${requests.length}_${i}`, name, arguments: JSON.stringify(input) }))] });
    }
    if (typeof step === 'string') return json({ candidates: [{ content: { role: 'model', parts: [{ text: step }] } }] });
    return json({ candidates: [{ content: { role: 'model', parts: step.map(([name, args], i) => ({ functionCall: { name, args, id: `fc_${requests.length}_${i}` }, ...(i === 0 && { thoughtSignature: `ts-${requests.length}` }) })) } }] });
  };
  return { fetch, requests };
}

for (const provider of ['anthropic', 'openai', 'gemini', 'vertex']) {
  test(`agent on ${provider}: tools run in the project and replies go back in the provider's own format`, async () => {
    const dir = await copyApp('vite-react');
    const { fetch, requests } = fakeProvider(provider);
    const steps = [];
    const result = await runAgent({ provider, model: { anthropic: 'claude-opus-5-5', openai: 'gpt-5.6-sol', gemini: 'gemini-3.8-flash', vertex: 'gemini-3.8-flash' }[provider],
      key: 'test-key', brief: 'Add Orion', workspace: createWorkspace(dir), ask: async () => null, onStep: s => steps.push(s), fetch });
    assert.equal(result.summary, SCRIPT[2]);
    assert.equal(result.turns, 3);
    assert.deepEqual(steps, ['Looked in the project', 'Read src/main.tsx', 'Edited src/main.tsx', 'Edited src/main.tsx']);
    const main = await readFile(path.join(dir, 'src/main.tsx'), 'utf8');
    assert.match(main, /import \{ orion \} from '@paradigmmachines\/orion'\n/);
    assert.match(main, /\}\)\norion\.connect\(posthog, \{ mask: 'timing', mood: true \}\)\n/);

    const second = requests[1].body, third = requests[2].body;
    if (provider === 'anthropic') {
      assert.equal(requests[0].url, 'https://api.anthropic.com/v1/messages');
      assert.equal(requests[0].headers['x-api-key'], 'test-key');
      assert.equal(requests[0].headers['anthropic-beta'], 'server-side-fallback-2026-07-01');
      assert.equal(requests[0].body.fallbacks, 'default');
      assert.equal(requests[0].body.model, 'claude-opus-5-5');
      assert.ok(requests[0].body.tools.every(t => t.strict && t.input_schema.additionalProperties === false));
      assert.equal(second.messages[1].content[0].signature, 'sig-1', 'thinking blocks go back unchanged');
      assert.deepEqual(second.messages[2].content.map(r => [r.type, r.tool_use_id]), [['tool_result', 'tu_1_0'], ['tool_result', 'tu_1_1']]);
      assert.match(second.messages[2].content[1].content, /posthog\.init/);
    } else if (provider === 'openai') {
      assert.equal(requests[0].url, 'https://api.openai.com/v1/responses');
      assert.equal(requests[0].headers.authorization, 'Bearer test-key');
      assert.equal(requests[0].body.store, false);
      assert.deepEqual(requests[0].body.include, ['reasoning.encrypted_content']);
      assert.deepEqual(second.input.slice(1).map(i => i.type), ['reasoning', 'function_call', 'function_call', 'function_call_output', 'function_call_output']);
      assert.equal(second.input.at(-1).call_id, 'call_1_1');
      assert.equal(third.input.at(-1).output, 'Edited src/main.tsx');
    } else {
      assert.equal(requests[0].url, provider === 'gemini' ? 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent'
        : 'https://aiplatform.googleapis.com/v1beta1/publishers/google/models/gemini-3.8-flash:generateContent');
      assert.equal(requests[0].headers['x-goog-api-key'], 'test-key');
      assert.ok(!JSON.stringify(requests[0].body.tools).includes('additionalProperties'));
      assert.equal(second.contents[1].parts[0].thoughtSignature, 'ts-1', 'thought signatures go back unchanged');
      assert.deepEqual(second.contents[2].parts.map(p => [p.functionResponse.name, p.functionResponse.id]), [['list_files', 'fc_1_0'], ['read_file', 'fc_1_1']]);
    }
    await rm(dir, { recursive: true });
  });
}

test('agent: a tool error goes back to the model as an error, and a refused path changes nothing', async () => {
  const dir = await copyApp('vite-react', { '.env.local': 'X=1\n' });
  const replies = [
    { stop_reason: 'tool_use', content: [{ type: 'tool_use', id: 't1', name: 'read_file', input: { path: '.env.local' } }] },
    { stop_reason: 'end_turn', content: [{ type: 'text', text: 'Stopped.' }] },
  ];
  const sent = [];
  const fetch = async (url, init) => { sent.push(JSON.parse(init.body)); return json(replies[sent.length - 1]); };
  await runAgent({ provider: 'anthropic', model: 'claude-opus-5-5', key: 'k', brief: 'x', workspace: createWorkspace(dir), ask: async () => null, fetch });
  const result = sent[1].messages[2].content[0];
  assert.equal(result.is_error, true);
  assert.match(result.content, /secrets/);
  await rm(dir, { recursive: true });
});

test('anthropic: a 400 about the fallback beta retries once without it; 401 is a setup error', async () => {
  const sent = [];
  const fetch = async (url, init) => {
    sent.push({ headers: init.headers, body: JSON.parse(init.body) });
    if (sent.length === 1) return json({ error: { message: 'Unexpected value for anthropic-beta' } }, 400);
    return json({ stop_reason: 'end_turn', content: [{ type: 'text', text: 'ok' }] });
  };
  const client = createClient({ provider: 'anthropic', model: 'claude-opus-5-5', key: 'k', system: 's', prompt: 'p', tools: [], fetch });
  assert.equal((await client.send(null)).text, 'ok');
  assert.equal(sent[1].headers['anthropic-beta'], undefined);
  assert.equal(sent[1].body.fallbacks, undefined);

  const denied = createClient({ provider: 'openai', model: 'gpt-5.6-sol', key: 'sk-secret-value', system: 's', prompt: 'p', tools: [],
    fetch: async () => json({ error: { message: 'Incorrect API key provided: sk-secret-value' } }, 401) });
  const error = await denied.send(null).catch(e => e);
  assert.equal(error.status, 401);
  assert.equal(error.message, 'OpenAI did not accept the API key (HTTP 401)');
  assert.doesNotMatch(error.message, /sk-secret-value/, 'the key never shows in a message');
  assert.deepEqual(providerFor('openai:my-model'), ['openai', 'my-model']);
  assert.deepEqual(providerFor('gemini-3.8-flash'), ['gemini', 'gemini-3.8-flash']);
  assert.deepEqual(providerFor('vertex:gemini-3.8-pro'), ['vertex', 'gemini-3.8-pro']);
});

test('key check: Vertex AI counts tokens, and a Google Cloud key the Gemini API refuses moves to Vertex AI', async () => {
  const seen = [];
  const google = cloudKey => async (url, init) => {
    seen.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    if (url.startsWith('https://generativelanguage.googleapis.com/')) return json({ error: { status: 'PERMISSION_DENIED' } }, 403);
    return init.headers['x-goog-api-key'] === cloudKey ? json({ totalTokens: 3 }) : json({ error: { status: 'PERMISSION_DENIED' } }, 403);
  };
  assert.deepEqual(await checkKey('vertex', 'AQ.cloud', { fetch: google('AQ.cloud') }), { ok: true, message: 'Vertex AI accepted the key' });
  assert.deepEqual(seen[0], { url: 'https://aiplatform.googleapis.com/v1beta1/publishers/google/models/gemini-3.8-flash:countTokens', method: 'POST',
    body: { contents: [{ role: 'user', parts: [{ text: 'Orion key check' }] }] } });
  const moved = await checkKey('gemini', 'AQ.cloud', { fetch: google('AQ.cloud') });
  assert.equal(moved.ok, true);
  assert.equal(moved.provider, 'vertex');
  assert.deepEqual(await checkKey('gemini', 'AQ.dead', { fetch: google('AQ.cloud') }), { ok: false, message: 'Gemini did not accept the key (HTTP 403)' });
  assert.equal((await checkKey('openai', 'sk', { fetch: async () => json({}, 401) })).provider, undefined);
});

test('coding agents: Claude Code and Codex events become progress lines and a summary', () => {
  const root = '/work/app';
  assert.deepEqual(claudeEvent({ type: 'assistant', message: { content: [
    { type: 'tool_use', name: 'Read', input: { file_path: '/work/app/src/main.tsx' } },
    { type: 'tool_use', name: 'Edit', input: { file_path: '/work/app/src/main.tsx' } },
    { type: 'tool_use', name: 'Grep', input: { pattern: 'posthog' } }] } }, root).steps,
  ['Read src/main.tsx', 'Edited src/main.tsx', 'Searched for posthog']);
  assert.deepEqual(claudeEvent({ type: 'result', subtype: 'success', is_error: false, result: 'Done.' }, root), { summary: 'Done.' });
  assert.match(claudeEvent({ type: 'result', subtype: 'success', is_error: true, result: 'Invalid API key · Please run /login' }, root).error, /login/);
  assert.deepEqual(codexEvent({ type: 'item.started', item: { type: 'command_execution', command: "/bin/zsh -lc 'cat src/main.tsx'" } }, root).steps, ['Ran cat src/main.tsx']);
  assert.deepEqual(codexEvent({ type: 'item.completed', item: { type: 'file_change', changes: [{ path: '/work/app/src/main.tsx', kind: 'update' }] } }, root).steps, ['Edited src/main.tsx']);
  assert.equal(codexEvent({ type: 'item.completed', item: { type: 'agent_message', text: 'Done.' } }, root).summary, 'Done.');
  assert.equal(codexEvent({ type: 'turn.failed', error: { message: 'Not signed in' } }, root).error, 'Not signed in');
});

test('coding agents: a run reads the brief on stdin, streams steps, and reports failures', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'orion-agent-'));
  const bin = path.join(dir, 'fake-claude.js');
  await writeFile(bin, `#!/usr/bin/env node
let brief = '';
process.stdin.on('data', c => brief += c).on('end', () => {
  const say = e => process.stdout.write(JSON.stringify(e) + '\\n');
  say({ type: 'assistant', message: { content: [{ type: 'tool_use', name: 'Read', input: { file_path: process.cwd() + '/src/main.tsx' } }] } });
  if (brief.includes('fail')) { say({ type: 'result', subtype: 'success', is_error: true, result: 'Not logged in' }); process.exit(1); }
  say({ type: 'result', subtype: 'success', is_error: false, result: 'Edited ' + brief.length });
});
`);
  await chmod(bin, 0o755);
  const agent = { id: 'claude', name: 'Claude Code', path: bin, args: () => [], read: claudeEvent };
  const steps = [];
  assert.deepEqual(await runCodingAgent(agent, { root: dir, brief: 'add orion', onStep: s => steps.push(s) }), { summary: 'Edited 9' });
  assert.deepEqual(steps, ['Read src/main.tsx']);
  await assert.rejects(runCodingAgent(agent, { root: dir, brief: 'fail please' }), /Claude Code: Not logged in/);
  await rm(dir, { recursive: true });
});

test('verify: finds the project by its public key across regions, then counts Orion events', async () => {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, body: init.body && JSON.parse(init.body) });
    if (url.startsWith('https://us.posthog.com')) return json({ detail: 'nope' }, 401);
    if (url.endsWith('/api/projects/')) return json({ results: [{ id: 1, name: 'Other', api_token: 'phc_other' }, { id: 2, name: 'Acme', api_token: 'phc_acme' }] });
    return json({ results: [[calls.length > 3 ? 12 : 0, '2026-10-12T20:00:00Z', 'Chrome', 'localhost:5173']] });
  };
  const place = await findProject({ key: 'phx', token: 'phc_acme', region: null, fetch });
  assert.equal(place.host, 'https://eu.posthog.com');
  assert.equal(place.project.name, 'Acme');
  const first = await orionEvents({ host: place.host, key: 'phx', projectId: 2, seconds: 90.2, fetch });
  assert.equal(first.count, 0);
  assert.match(calls.at(-1).body.query.query, /event in \('orion_session_start', 'orion_input'\)/);
  assert.match(calls.at(-1).body.query.query, /interval 91 second/);
  const found = await waitForEvents({ host: place.host, key: 'phx', projectId: 2, since: Date.now(), fetch, sleep: async () => {} });
  assert.deepEqual(found, { count: 12, latest: '2026-10-12T20:00:00Z', browser: 'Chrome', host: 'localhost:5173' });
  await assert.rejects(findProject({ key: 'phx', fetch: async () => json({}, 403) }), /query:read/);
});

test('posthog sign-in: PKCE in the browser, one project, a private file the training tools read, rotating refresh tokens', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'orion-auth-'));
  const env = { ORION_KEYS_FILE: path.join(dir, 'keys.env') };
  const codes = new Map();
  let authorize = null, issued = 0, refresh = null;
  const server = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://posthog.test');
    if (url.pathname === '/oauth/authorize/') {  // The person clicks Authorize at once.
      authorize = Object.fromEntries(url.searchParams);
      const code = `code-${codes.size}`;
      codes.set(code, authorize);
      res.writeHead(302, { location: `${authorize.redirect_uri}?code=${code}&state=${encodeURIComponent(authorize.state)}` }).end();
      return;
    }
    let body = '';
    for await (const chunk of req) body += chunk;
    const form = Object.fromEntries(new URLSearchParams(body));
    if (form.grant_type === 'authorization_code') {
      const asked = codes.get(form.code);
      if (!asked || asked.code_challenge !== createHash('sha256').update(form.code_verifier).digest('base64url') || asked.redirect_uri !== form.redirect_uri) {
        res.writeHead(400).end('{"error":"invalid_grant"}');
        return;
      }
    } else if (form.refresh_token !== refresh) {
      res.writeHead(400).end('{"error":"invalid_grant"}');
      return;
    }
    issued += 1;
    refresh = `phr_${issued}`;
    res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({
      access_token: `pha_${issued}`, refresh_token: refresh, expires_in: 3600, scope: SIGN_IN_SCOPES.join(' '), scoped_teams: [9] }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const host = `http://127.0.0.1:${server.address().port}`;
  try {
    const record = await signIn(host, { openUrl: url => { fetch(url).then(r => r.text()); }, env });
    assert.equal(record.project, '9');
    assert.equal(authorize.required_access_level, 'project');
    assert.equal(authorize.code_challenge_method, 'S256');
    assert.equal(authorize.scope, 'project:read query:read batch_export:read batch_export:write');
    assert.match(authorize.redirect_uri, /^http:\/\/127\.0\.0\.1:\d+\/callback$/);
    const file = authFile(env);
    assert.equal(file, path.join(dir, 'posthog-auth.json'));
    assert.equal((await stat(file)).mode & 0o777, 0o600);
    const saved = JSON.parse(await readFile(file, 'utf8'));
    assert.deepEqual(Object.keys(saved.logins), [`${host} 9`]);  // the layout paradigm_orion/posthog_auth.py reads
    assert.equal(await accessToken(host, '9', { env }), 'pha_1');
    saved.logins[`${host} 9`].expires_at = Math.floor(Date.now() / 1000) + 10;
    await writeFile(file, JSON.stringify(saved));
    assert.equal(await accessToken(host, '9', { env }), 'pha_2');
    assert.equal((await loginFor(host, null, env)).refresh_token, 'phr_2');
    refresh = 'phr_revoked';
    await assert.rejects(accessToken(host, '9', { env, rejected: 'pha_2' }), /sign-in has ended/);
  } finally {
    server.close();
    await rm(dir, { recursive: true });
  }
});

test('main: a no-questions run with Orion\'s agent edits the app, shows the change, and keeps it', async () => {
  const dir = await copyApp('vite-react');
  // Listed already, so the run skips the install step (no network in tests).
  const pkg = JSON.parse(await readFile(path.join(dir, 'package.json'), 'utf8'));
  pkg.dependencies['@paradigmmachines/orion'] = 'github:ryanlin10/orion-posthog';
  await writeFile(path.join(dir, 'package.json'), JSON.stringify(pkg, null, 2));
  const output = new PassThrough();
  let printed = '';
  output.on('data', chunk => { printed += chunk; });
  const ui = createUI({ input: new PassThrough(), output, interactive: false, color: false });
  const { fetch, requests } = fakeProvider('anthropic');
  const env = { PATH: '', ANTHROPIC_API_KEY: 'test-key', ORION_KEYS_FILE: path.join(dir, 'no-keys.env') };
  const code = await main(['--dir', dir, '--yes', '--agent', 'api'], { ui, env, fetch });
  assert.equal(code, 0, printed);
  assert.match(requests[0].body.messages[0].content, /orion\.connect\(posthog, \{ mask: 'timing', mood: true, base: true \}\)/);
  assert.match(await readFile(path.join(dir, 'src/main.tsx'), 'utf8'), /orion\.connect/);
  assert.match(printed, /◇  Your project\n│  .*\n│  React \(Vite\) · npm\n│  PostHog starts in src\/main\.tsx:6/);
  assert.match(printed, /M src\/main\.tsx  \+2 −0\n│      import App from '\.\/App'\n│    \+ import \{ orion \} from '@paradigmmachines\/orion'/);
  assert.match(printed, /│    \+ orion\.connect\(posthog, \{ mask: 'timing', mood: true \}\)/);
  assert.match(printed, /Orion is set up\./);
  await rm(dir, { recursive: true });
});

test('main: with no PostHog and no terminal it stops before changing anything', async () => {
  const dir = await copyApp('no-posthog');
  const output = new PassThrough();
  let printed = '';
  output.on('data', chunk => { printed += chunk; });
  const ui = createUI({ input: new PassThrough(), output, interactive: false, color: false });
  const before = await snapshot(dir);
  assert.equal(await main(['--dir', dir, '--yes'], { ui, env: { PATH: '' } }), 1);
  assert.match(printed, /PostHog: not found/);
  assert.match(printed, /npx -y @posthog\/wizard@latest/);
  assert.deepEqual(await before.changes(), []);
  await rm(dir, { recursive: true });
});

test('main: a saved key that the provider rejects is not used', async () => {
  const dir = await copyApp('vite-react');
  const pkg = JSON.parse(await readFile(path.join(dir, 'package.json'), 'utf8'));
  pkg.dependencies['@paradigmmachines/orion'] = 'github:ryanlin10/orion-posthog';
  await writeFile(path.join(dir, 'package.json'), JSON.stringify(pkg, null, 2));
  const output = new PassThrough();
  let printed = '';
  output.on('data', chunk => { printed += chunk; });
  const ui = createUI({ input: new PassThrough(), output, interactive: false, color: false });
  const fetch = async () => json({ error: { message: 'invalid x-api-key' } }, 401);
  const before = await snapshot(dir);
  const code = await main(['--dir', dir, '--yes', '--agent', 'api'], { ui, env: { PATH: '', ANTHROPIC_API_KEY: 'dead', ORION_KEYS_FILE: path.join(dir, 'k.env') }, fetch });
  assert.equal(code, 1);
  assert.match(printed, /No AI key found/);
  assert.deepEqual(await before.changes(), [], 'nothing changed');
  await rm(dir, { recursive: true });
});

test('ui: roomFor waits for a big enough window, and Enter goes on anyway', async () => {
  const make = () => {
    const output = new PassThrough();
    Object.assign(output, { columns: 101, rows: 24 });
    output.resume();
    return { input: new PassThrough(), output };
  };
  const a = make();
  const ui = createUI({ ...a, interactive: true, color: false });
  const waiting = ui.roomFor({ columns: 80, rows: 28, message: 'Bigger' });
  a.output.rows = 26;
  a.output.emit('resize');
  a.output.rows = 30;
  a.output.emit('resize');
  assert.equal(await waiting, true);

  const b = make();
  const pressed = createUI({ ...b, interactive: true, color: false }).roomFor({ columns: 80, rows: 28, message: 'Bigger' });
  b.input.emit('keypress', '\r', { name: 'return' });
  assert.equal(await pressed, false);
  assert.equal(await createUI({ ...make(), interactive: false }).roomFor({ columns: 80, rows: 28, message: 'x' }), true);
});

test('main: refuses to run in the home folder', async () => {
  const output = new PassThrough();
  let printed = '';
  output.on('data', chunk => { printed += chunk; });
  const ui = createUI({ input: new PassThrough(), output, interactive: false, color: false });
  assert.equal(await main(['--dir', os.homedir(), '--yes'], { ui, env: { PATH: '' } }), 1);
  assert.match(printed, /is your home folder, not an app/);
  assert.doesNotMatch(printed, /Your project/, 'stops before reading anything');
});
