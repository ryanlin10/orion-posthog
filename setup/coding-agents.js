// Hands the edit to a coding agent the developer already has and is signed in to, so no API key is needed: Claude
// Code (`claude -p`) or Codex (`codex exec`). Both run headless in the project folder with file tools only, and their
// event streams become the wizard's progress lines. What they changed is read back from a snapshot (files.js).
import { spawn } from 'node:child_process';
import { realpathSync } from 'node:fs';
import { access, constants } from 'node:fs/promises';
import path from 'node:path';

export const CODING_AGENTS = [
  {
    id: 'claude', name: 'Claude Code', bin: 'claude',
    args: () => ['-p', '--output-format', 'stream-json', '--verbose', '--permission-mode', 'acceptEdits',
      '--allowedTools', 'Read,Edit,Write,Glob,Grep', '--disallowedTools', 'Bash,WebFetch,WebSearch',
      '--strict-mcp-config', '--no-session-persistence'],
    read: claudeEvent,
    status: { args: ['auth', 'status'], signedIn: (code, out) => /"loggedIn"\s*:\s*true/.test(out) },
  },
  {
    id: 'codex', name: 'Codex', bin: 'codex',
    args: dir => ['exec', '--json', '--sandbox', 'workspace-write', '--skip-git-repo-check', '--ephemeral', '-C', dir, '-'],
    read: codexEvent,
    status: { args: ['login', 'status'], signedIn: (code, out) => code === 0 && /logged in/i.test(out) && !/not logged in/i.test(out) },
  },
];

/** Prefer PATH; native Claude and uv installers also use ~/.local/bin, absent from some app-launched shells. */
export async function onPath(bin, env = process.env) {
  const exts = process.platform === 'win32' ? ['.cmd', '.exe', ''] : [''];
  const dirs = (env.PATH ?? '').split(path.delimiter).filter(Boolean);
  if (process.platform !== 'win32' && env.HOME) dirs.push(path.join(env.HOME, '.local', 'bin'));
  for (const dir of new Set(dirs)) {
    const hit = await Promise.any(exts.map(ext => access(path.join(dir, bin + ext), constants.X_OK).then(() => path.join(dir, bin + ext)))).catch(() => null);
    if (hit) return hit;
  }
  return null;
}

/** true, false, or null when the agent's own status command cannot tell within a few seconds. */
function signedIn(agent, file, env) {
  return new Promise(resolve => {
    let out = '';
    const child = spawn(file, agent.status.args, { env, stdio: ['ignore', 'pipe', 'pipe'], shell: process.platform === 'win32' });
    const timer = setTimeout(() => { child.kill('SIGKILL'); resolve(null); }, 5000);
    child.stdout.on('data', chunk => { out += chunk; });
    child.stderr.on('data', chunk => { out += chunk; });
    child.on('error', () => { clearTimeout(timer); resolve(null); });
    child.on('close', code => { clearTimeout(timer); resolve(agent.status.signedIn(code, out)); });
  });
}

/** The coding agents on PATH, each with signedIn true, false or null (unknown). */
export async function findCodingAgents(env = process.env) {
  const found = await Promise.all(CODING_AGENTS.map(async agent => {
    const file = await onPath(agent.bin, env);
    return file && { ...agent, path: file, signedIn: await signedIn(agent, file, env) };
  }));
  return found.filter(Boolean);
}

// Agents report absolute paths, sometimes through symlinks (/var is /private/var on macOS): show them project-relative.
function relative(root, file) {
  if (!file || !path.isAbsolute(file)) return file;
  for (const base of [root, safeRealpath(root)]) {
    const rel = path.relative(base, file);
    if (!rel.startsWith('..')) return rel || '.';
  }
  return file;
}

function safeRealpath(dir) {
  try { return realpathSync(dir); } catch { return dir; }
}

/** Claude Code's stream-json: tool calls become steps, the result event ends the run. */
export function claudeEvent(event, root) {
  if (event.type === 'assistant') {
    const steps = [];
    for (const block of event.message?.content ?? []) {
      if (block.type !== 'tool_use') continue;
      const input = block.input ?? {};
      const file = relative(root, input.file_path ?? input.path);
      steps.push({ Read: `Read ${file}`, Edit: `Edited ${file}`, MultiEdit: `Edited ${file}`, Write: `Wrote ${file}`,
        Glob: `Looked for ${input.pattern}`, Grep: `Searched for ${input.pattern}` }[block.name] ?? `Used ${block.name}`);
    }
    return { steps };
  }
  if (event.type === 'result') {
    const failed = event.is_error || (event.subtype && event.subtype !== 'success');
    return failed ? { error: String(event.result || event.subtype || 'Claude Code stopped').trim() } : { summary: String(event.result ?? '').trim() };
  }
  return {};
}

/** Codex's JSONL: commands and file changes become steps, the last agent message is the summary. */
export function codexEvent(event, root) {
  const item = event.item ?? {};
  if (event.type === 'item.started' && item.type === 'command_execution') {
    const command = String(item.command ?? '').replace(/^\/bin\/\w+ -lc '(.*)'$/s, '$1');
    return { steps: [`Ran ${command}`] };
  }
  if (event.type === 'item.completed' && item.type === 'file_change') {
    return { steps: (item.changes ?? []).map(change => `${change.kind === 'add' ? 'Created' : change.kind === 'delete' ? 'Deleted' : 'Edited'} ${relative(root, change.path)}`) };
  }
  if (event.type === 'item.completed' && item.type === 'agent_message') return { summary: String(item.text ?? '').trim() };
  if (event.type === 'turn.failed' || event.type === 'error') return { error: event.error?.message ?? event.message ?? 'Codex stopped' };
  return {};
}

const running = new Set();

/** Stops any coding agent still running, so nothing edits files while setup puts them back. */
export function stopCodingAgents() {
  for (const child of running) child.kill('SIGTERM');
}

/** Runs one coding agent on the brief, for at most `timeout` ms. Resolves { summary }; onStep gets progress lines. */
export function runCodingAgent(agent, { root, brief, onStep = () => {}, timeout = 10 * 60_000, env = process.env }) {
  return new Promise((resolve, reject) => {
    const child = spawn(agent.path ?? agent.bin, agent.args(root), { cwd: root, env, stdio: ['pipe', 'pipe', 'pipe'],
      shell: process.platform === 'win32' });
    running.add(child);
    let buffer = '', stderr = '', summary = '', error = null, settled = false;
    const finish = (fail, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fail ? reject(fail) : resolve(value);
    };
    const timer = setTimeout(() => { child.kill('SIGTERM'); finish(new Error(`${agent.name} took longer than ${Math.round(timeout / 60000)} minutes`)); }, timeout);
    const handle = line => {
      let event;
      try { event = JSON.parse(line); } catch { return; }
      const read = agent.read(event, root);
      for (const step of read.steps ?? []) onStep(step);
      if (read.summary) summary = read.summary;
      if (read.error) error = read.error;
    };
    child.stdout.on('data', chunk => {
      buffer += chunk;
      let at;
      while ((at = buffer.indexOf('\n')) >= 0) { handle(buffer.slice(0, at)); buffer = buffer.slice(at + 1); }
    });
    child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4000); });
    child.on('error', fail => finish(new Error(`Could not start ${agent.name}: ${fail.message}`)));
    child.on('close', code => {
      running.delete(child);
      if (buffer.trim()) handle(buffer);
      if (error) return finish(new Error(`${agent.name}: ${error}`));
      if (code !== 0) return finish(new Error(`${agent.name} exited with code ${code}${stderr.trim() ? `: ${stderr.trim().split('\n').slice(-3).join(' ')}` : ''}`));
      finish(null, { summary });
    });
    child.stdin.end(brief);
  });
}
