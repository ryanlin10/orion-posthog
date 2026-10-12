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
  },
  {
    id: 'codex', name: 'Codex', bin: 'codex',
    args: dir => ['exec', '--json', '--sandbox', 'workspace-write', '--skip-git-repo-check', '--ephemeral', '-C', dir, '-'],
    read: codexEvent,
  },
];

/** The coding agents on PATH. */
export async function findCodingAgents(env = process.env) {
  const dirs = (env.PATH ?? '').split(path.delimiter).filter(Boolean);
  const exts = process.platform === 'win32' ? ['.cmd', '.exe', ''] : [''];
  const found = [];
  for (const agent of CODING_AGENTS) {
    for (const dir of dirs) {
      const hit = await Promise.any(exts.map(ext => access(path.join(dir, agent.bin + ext), constants.X_OK).then(() => path.join(dir, agent.bin + ext)))).catch(() => null);
      if (hit) { found.push({ ...agent, path: hit }); break; }
    }
  }
  return found;
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

/** Runs one coding agent on the brief, for at most `timeout` ms. Resolves { summary }; onStep gets progress lines. */
export function runCodingAgent(agent, { root, brief, onStep = () => {}, timeout = 10 * 60_000, env = process.env }) {
  return new Promise((resolve, reject) => {
    const child = spawn(agent.path ?? agent.bin, agent.args(root), { cwd: root, env, stdio: ['pipe', 'pipe', 'pipe'],
      shell: process.platform === 'win32' });
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
      if (buffer.trim()) handle(buffer);
      if (error) return finish(new Error(`${agent.name}: ${error}`));
      if (code !== 0) return finish(new Error(`${agent.name} exited with code ${code}${stderr.trim() ? `: ${stderr.trim().split('\n').slice(-3).join(' ')}` : ''}`));
      finish(null, { summary });
    });
    child.stdin.end(brief);
  });
}
