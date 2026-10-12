// `npx github:ryanlin10/orion-posthog` sets Orion up in the app in the current folder:
// look at the project → hand over to the PostHog wizard when PostHog is missing → choose what Orion records →
// install → an agent makes the edit → review every change (keep or undo) → watch for the first events →
// skills for coding agents, and the training tools. Nothing is sent to Paradigm Machines.
import { spawn } from 'node:child_process';
import { cp, readFile, stat } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { runAgent } from './agent.js';
import { brief, manualSteps } from './brief.js';
import { CODING_AGENTS, findCodingAgents, runCodingAgent } from './coding-agents.js';
import { createWorkspace, diffLines, restore, snapshot } from './files.js';
import { findKey, keysFile, saveKeys } from './keys.js';
import { detect, installCommand, LOCKFILES, PACKAGE, vendorDir } from './project.js';
import { checkKey, DEFAULT_MODELS, KEY_NAMES, NAMES, PROVIDERS, providerFor } from './providers.js';
import { Cancelled, createUI } from './ui.js';
import { findProject, HOSTS, SCOPES, waitForEvents } from './verify.js';

const REPO = 'ryanlin10/orion-posthog';
const HERE = path.dirname(fileURLToPath(import.meta.url));
const VERSION = '0.3.0';
const POSTHOG_KEY = 'POSTHOG_PERSONAL_API_KEY';

const HELP = `Orion setup: adds Orion to the web app in this folder.

Usage: npx github:${REPO} [options]

  --dir <path>         The app's folder (default: this one)
  --mask <mode>        timing (default), types or off
  --mood, --no-mood    The live mood reading (default: on)
  --self-report        Ask visitors how it is going
  --agent <name>       Who makes the edit: api, claude, codex or manual
  --model <name>       The model for --agent api (default: ${DEFAULT_MODELS.anthropic}, ${DEFAULT_MODELS.openai} or ${DEFAULT_MODELS.gemini})
  --no-verify          Skip waiting for the first events
  -y, --yes            No questions: take the defaults (and keep the changes)
  -h, --help           This help
`;

const tilde = dir => (dir.startsWith(os.homedir()) ? `~${dir.slice(os.homedir().length)}` : dir);

function run(command, args, options = {}) {
  return new Promise(resolve => {
    const child = spawn(command, args, { shell: process.platform === 'win32', ...options });
    let output = '';
    child.stdout?.on('data', chunk => { output = (output + chunk).slice(-8000); });
    child.stderr?.on('data', chunk => { output = (output + chunk).slice(-8000); });
    child.on('error', error => resolve({ code: -1, output: error.message }));
    child.on('close', code => resolve({ code, output }));
  });
}

function openUrl(url) {
  const [cmd, args] = process.platform === 'darwin' ? ['open', [url]] : process.platform === 'win32' ? ['cmd', ['/c', 'start', '', url]] : ['xdg-open', [url]];
  spawn(cmd, args, { stdio: 'ignore', detached: true }).on('error', () => {}).unref();
}

export async function main(argv, { ui = null, env = process.env, fetch = globalThis.fetch } = {}) {
  let args;
  try {
    args = parseArgs({ args: argv, strict: true, options: {
      dir: { type: 'string' }, mask: { type: 'string' }, mood: { type: 'boolean' }, 'no-mood': { type: 'boolean' },
      'self-report': { type: 'boolean' }, agent: { type: 'string' }, model: { type: 'string' }, 'no-verify': { type: 'boolean' },
      yes: { type: 'boolean', short: 'y' }, help: { type: 'boolean', short: 'h' }, version: { type: 'boolean', short: 'v' } } }).values;
  } catch (error) {
    process.stderr.write(`${error.message}\n\n${HELP}`);
    return 2;
  }
  if (args.help) { process.stdout.write(HELP); return 0; }
  if (args.version) { process.stdout.write(`${VERSION}\n`); return 0; }
  if (args.mask && !['timing', 'types', 'off'].includes(args.mask)) { process.stderr.write('--mask is timing, types or off\n'); return 2; }
  if (args.agent && !['api', 'manual', ...CODING_AGENTS.map(a => a.id)].includes(args.agent)) { process.stderr.write('--agent is api, claude, codex or manual\n'); return 2; }

  ui ??= createUI(args.yes ? { interactive: false } : {});
  try {
    return await setup(args, ui, env, fetch);
  } catch (error) {
    if (error instanceof Cancelled) { ui.outro('Setup stopped. Run it again any time.'); return 130; }
    throw error;
  }
}

async function setup(args, ui, env, fetch) {
  const { c } = ui;
  ui.intro('Orion setup', `${VERSION} · runs on your machine`);
  const root = path.resolve(args.dir ?? process.cwd());
  let project = await detect(root);
  const describe = p => [
    tilde(p.root),
    [p.framework, p.packageDir ? p.packageManager : null, p.git.repo ? `git${p.git.dirty ? `, ${p.git.dirty} uncommitted change${p.git.dirty > 1 ? 's' : ''}` : ', clean'}` : null].filter(Boolean).join(' · '),
    p.posthog.kind ? `PostHog ${p.posthog.kind === 'snippet' ? 'snippet' : 'starts'} in ${p.posthog.places[0].file}:${p.posthog.places[0].line}` : 'PostHog: not found',
  ];
  ui.step('Your project', describe(project));
  if (!project.hasPackageJson && !project.htmlFiles) {
    ui.error('There is no web app here: no package.json and no HTML files.', ['Run this in your app\'s folder, or pass --dir <path>.']);
    ui.outro('Nothing changed.');
    return 1;
  }

  if (project.orion.places.length) {
    ui.warn('Orion is already in this project:', project.orion.places.slice(0, 4).map(p => `${p.file}:${p.line}  ${p.text}`));
    const next = await ui.select({ message: 'What now?', initial: 'verify', options: [
      { value: 'verify', label: 'Check that its events arrive' }, { value: 'again', label: 'Set it up again' }, { value: 'stop', label: 'Stop' }] });
    if (next === 'stop') { ui.outro('Nothing changed.'); return 0; }
    if (next === 'verify') return finish(args, ui, project, env, fetch, { coding: [] });
  }

  if (!project.posthog.kind) {
    ui.note(c.dim('Orion records into PostHog, and this app does not start posthog-js yet.'));
    if (!ui.interactive) {
      ui.error('Add PostHog first: npx -y @posthog/wizard@latest');
      ui.outro('Nothing changed.');
      return 1;
    }
    const go = await ui.confirm({ message: 'Run the PostHog wizard to add it now?', yes: 'Run it', no: 'Not now' });
    if (!go) { ui.outro('Add PostHog first (npx -y @posthog/wizard@latest), then run this again.'); return 1; }
    // PostHog's wizard refuses to draw in a window under 80×28, so make room before handing over.
    await ui.roomFor({ columns: 80, rows: 28, message: 'PostHog\'s wizard needs a bigger window' });
    ui.note(c.dim('Handing over to the PostHog wizard. Orion carries on when it finishes.'));
    const { code } = await run('npx', ['-y', '@posthog/wizard@latest', '--install-dir', root], { stdio: 'inherit', cwd: root });
    project = await detect(root);
    if (!project.posthog.kind) {
      ui.error(code ? `The PostHog wizard stopped (exit code ${code}).` : 'PostHog still does not start in this app.');
      ui.outro('Run this again once PostHog is in.');
      return 1;
    }
    ui.step('PostHog is in', describe(project).slice(2));
  }

  const mask = args.mask ?? await ui.select({ message: 'Keyboard: what should Orion keep?', initial: 'timing', options: [
    { value: 'timing', label: 'Timing only', hint: 'when each key went down and up, never which key (recommended)' },
    { value: 'types', label: 'Timing and key types', hint: 'adds letter, digit, space or delete; still never the key' },
    { value: 'off', label: 'Nothing from the keyboard', hint: 'pointer and scrolling only' }] });
  const flagged = args.mood || args['no-mood'] || args['self-report'];
  const extras = flagged ? [!args['no-mood'] && 'mood', args['self-report'] && 'selfReport'].filter(Boolean)
    : await ui.multiselect({ message: 'Live readings in the browser', initial: ['mood'], options: [
      { value: 'mood', label: 'Mood reading', hint: 'angry, frustrated, calm, anxious, confused or pleased, from the pointer' },
      { value: 'selfReport', label: 'Ask visitors how it is going', hint: 'a small question in a corner, at most every 14 days' }] });
  const options = { mask, ...(extras.includes('mood') && { mood: true }), ...(extras.includes('selfReport') && { selfReport: true }) };

  // Who makes the edit: Orion's agent on a key the developer has, a coding agent they are signed in to, or by hand.
  const coding = await findCodingAgents(env);
  // Saved keys are checked first (listing models is free), so a dead key is never the default.
  const preferred = args.model ? providerFor(args.model)[0] : null;
  const keyed = (await Promise.all(PROVIDERS.filter(p => !preferred || p === preferred).map(async provider => {
    const found = await findKey(KEY_NAMES[provider], env);
    return found && { provider, ...found, ok: (await checkKey(provider, found.value, { env, fetch })).ok };
  }))).filter(Boolean);
  const key = keyed.find(k => k.ok) ?? null;
  const rejected = keyed.find(k => !k.ok);
  const editors = [
    { value: 'api', label: key ? `Orion's agent, with your ${NAMES[key.provider]} key` : 'Orion\'s agent, with an API key',
      hint: key ? `from ${tilde(key.from)}` : rejected ? `the saved ${NAMES[rejected.provider]} key was rejected; paste another`
        : 'Anthropic, OpenAI or Gemini; kept on this machine for the training tools too' },
    ...coding.map(a => ({ value: a.id, label: a.name, hint: 'already on this machine and signed in, so no key needed' })),
    { value: 'manual', label: 'Show me the code', hint: 'paste it in yourself' },
  ];
  let editor = args.agent ?? await ui.select({ message: 'Who should make the edit?', options: editors,
    initial: key ? 'api' : coding[0]?.id ?? 'api' });
  ui.note(c.dim(editor === 'manual' ? 'Nothing is sent to any AI.' : 'The agent reads your source files to make the edit. .env files stay closed.'));

  const before = await snapshot(root);
  // From here until the review, stopping (Ctrl-C, Esc) puts every file back as it was.
  const onInterrupt = async () => {
    process.off('SIGINT', onInterrupt);
    ui.halt();
    ui.warn('Stopping. Putting your files back.');
    await undo(ui, root, await before.changes());
    ui.outro('Setup stopped. Nothing changed.');
    process.exit(130);
  };
  process.on('SIGINT', onInterrupt);
  try {
    const vendor = project.posthog.kind === 'snippet' ? vendorDir(project) : null;
    if (project.posthog.kind === 'npm' && !project.orion.installed) {
      const [command, commandArgs] = installCommand(project.packageManager, env.ORION_SETUP_PACKAGE || `github:${REPO}`);
      const spin = ui.spinner();
      spin.start(`Installing ${PACKAGE} with ${command}`);
      const result = await run(command, commandArgs, { cwd: project.packageDir, env });
      // Success is the package landing: pnpm, for one, exits non-zero over other packages' unapproved build scripts.
      const landed = await stat(path.join(project.packageDir, 'node_modules', ...PACKAGE.split('/'), 'package.json')).then(() => true, () => false);
      if (landed && result.code !== 0) {
        spin.stop(`Installed ${PACKAGE} with ${command}`);
        ui.warn(`${command} also reported a problem with other packages:`, result.output.trim().split('\n').filter(l => l.trim()).slice(-4));
      } else if (!landed && result.code !== 0) {  // Yarn PnP keeps no node_modules: exit code 0 is enough.
        spin.stop(`${command} could not install ${PACKAGE}`, false);
        ui.note(result.output.trim().split('\n').slice(-6).map(l => c.dim(l)));
        await undo(ui, root, await before.changes());
        ui.outro(`Install it yourself (${[command, ...commandArgs].join(' ')}), then run this again.`);
        return 1;
      } else spin.stop(`Installed ${PACKAGE} with ${command}`);
    } else if (vendor) {
      const files = await browserFiles(path.join(HERE, '..', 'src'));
      for (const file of files) await cp(path.join(HERE, '..', 'src', file), path.join(root, vendor, file));
      ui.step(`Copied Orion's browser code into ${vendor}/`, [c.dim(`${files.length} files; auto.js connects to the PostHog snippet`)]);
    }

    const task = brief(project, options, { vendor });
    const installed = await snapshot(root);  // so a failed agent's half-done edits can be cleared before another try
    let summary = '';
    for (;;) {
      if (editor === 'manual') {
        ui.step('Add this to your app', manualSteps(project, options, { vendor }).map(l => (l.startsWith('  ') ? c.cyan(l) : l)));
        if (ui.interactive) await ui.confirm({ message: 'Added it?', yes: 'Done', no: 'Later' });
        break;
      }
      const outcome = await edit(editor, { ui, root, task, key, args, env, fetch, coding });
      if (outcome.ok) { summary = outcome.summary; break; }
      if (!ui.interactive) { await undo(ui, root, await before.changes()); return 1; }
      await restore(root, await installed.changes());
      const left = editors.filter(e => e.value !== editor);
      editor = await ui.select({ message: 'Try another way?', options: [...left, { value: 'stop', label: 'Stop and undo' }], initial: left[0].value });
      if (editor === 'stop') { await undo(ui, root, await before.changes()); ui.outro('Nothing changed.'); return 1; }
    }

    const changes = await before.changes();
    if (!(await review(ui, project, options, changes, summary, vendor, editor))) {
      await undo(ui, root, changes);
      ui.outro('Undone. Your project is as it was.');
      return 0;
    }
  } catch (error) {
    if (error instanceof Cancelled) await undo(ui, root, await before.changes());
    throw error;
  } finally {
    process.off('SIGINT', onInterrupt);
  }
  return finish(args, ui, await detect(root), env, fetch, { coding });
}

async function edit(editor, { ui, root, task, key, args, env, fetch, coding }) {
  const spin = ui.spinner();
  try {
    if (editor === 'api') {
      const chosen = await apiKey(ui, key, args, env);
      if (!chosen) return { ok: false };
      const label = `${NAMES[chosen.provider]} ${chosen.model}`;
      spin.start(`Orion's agent (${label}) is looking at your code`);
      const workspace = createWorkspace(root);
      const result = await runAgent({ provider: chosen.provider, model: chosen.model, key: chosen.value, brief: task, workspace, env, fetch,
        onStep: step => { spin.log(step); spin.message(`Orion's agent (${label}) is working`); },
        ask: async (question, choices) => { spin.pause(); const answer = await ui.select({ message: question, options: choices.map(v => ({ value: v, label: v })) }); spin.resume(); return answer; } });
      spin.stop(`Orion's agent finished in ${result.turns} steps`);
      return { ok: true, summary: result.summary };
    }
    const agent = coding.find(a => a.id === editor) ?? CODING_AGENTS.find(a => a.id === editor);
    spin.start(`${agent.name} is looking at your code`);
    const result = await runCodingAgent(agent, { root, brief: task, env,
      onStep: step => { spin.log(step); spin.message(`${agent.name} is working`); } });
    spin.stop(`${agent.name} finished`);
    return { ok: true, summary: result.summary };
  } catch (error) {
    if (error instanceof Cancelled) throw error;
    spin.stop(error.message.split('\n')[0].slice(0, 300), false);
    return { ok: false };
  }
}

/** The key and model for Orion's agent: the one found, or one asked for (checked, and saved if wanted). */
async function apiKey(ui, found, args, env) {
  if (found) {
    const provider = found.provider;
    return { ...found, model: args.model ? providerFor(args.model)[1] : DEFAULT_MODELS[provider] };
  }
  if (!ui.interactive) {
    ui.error(`No AI key found. Set ${Object.values(KEY_NAMES).join(', ')} or use --agent manual.`);
    return null;
  }
  const provider = args.model ? providerFor(args.model)[0] : await ui.select({ message: 'Which AI?', options: PROVIDERS.map(p => ({ value: p, label: NAMES[p], hint: DEFAULT_MODELS[p] })) });
  for (;;) {
    const value = await ui.text({ message: `${NAMES[provider]} API key`, secret: true, placeholder: 'paste it here; it is hidden' });
    if (!value) return null;
    const check = await checkKey(provider, value, { env });
    ui.note(check.ok ? ui.c.green(check.message) : ui.c.yellow(check.message));
    if (!check.ok) continue;
    if (await ui.confirm({ message: `Keep the key for Orion's training tools in ${tilde(keysFile(env))}?`, yes: 'Keep it', no: 'Just this run' })) {
      await saveKeys({ [KEY_NAMES[provider]]: value }, env);
    }
    return { provider, value, model: args.model ? providerFor(args.model)[1] : DEFAULT_MODELS[provider] };
  }
}

async function review(ui, project, options, changes, summary, vendor, editor) {
  const { c } = ui;
  const lines = [];
  const vendored = vendor ? changes.filter(ch => ch.path.startsWith(`${vendor}/`)) : [];
  for (const change of changes) {
    if (vendored.includes(change)) continue;
    const mark = change.before === null ? c.green('A') : change.after === null ? c.red('D') : c.yellow('M');
    if (LOCKFILES.has(path.basename(change.path)) || change.before === undefined) { lines.push(`${mark} ${change.path} ${c.dim('(updated)')}`); continue; }
    const deps = path.basename(change.path) === 'package.json' && dependencyChanges(change.before, change.after);
    if (deps) {
      // Package managers often rewrite the whole file's layout; what matters is which dependencies changed.
      lines.push(`${mark} ${c.bold(change.path)}`, ...deps.map(([op, text]) => (op === '+' ? c.green(`  + ${text}`) : c.red(`  - ${text}`))));
      continue;
    }
    const diff = diffLines(change.before, change.after);
    lines.push(`${mark} ${c.bold(change.path)}  ${c.green(`+${diff.added}`)} ${c.red(`−${diff.removed}`)}`);
    for (const [op, text] of diff.lines) {
      lines.push(op === '+' ? c.green(`  + ${text}`) : op === '-' ? c.red(`  - ${text}`) : op === '…' ? c.dim('    …') : c.dim(`    ${text}`));
    }
    if (diff.more) lines.push(c.dim(`    … ${diff.more} more lines`));
  }
  if (vendored.length) lines.push(`${c.green('A')} ${vendor}/ ${c.dim(`(${vendored.length} files of Orion's browser code)`)}`);
  ui.step('Changes', lines.length ? lines : [c.dim('None')]);
  if (summary) ui.note(c.dim(plain(summary)));
  const code = changes.filter(ch => !vendored.includes(ch) && path.basename(ch.path) !== 'package.json' && !LOCKFILES.has(path.basename(ch.path)));
  if (!code.length && editor !== 'manual') {
    ui.warn('The agent did not change any code. Add it by hand:', manualSteps(project, options, { vendor }).filter(Boolean));
  }
  if (!changes.length) return true;
  return ui.confirm({ message: 'Keep these changes?', yes: 'Keep', no: 'Undo all' });
}

/** auto.js and every module it imports, the browser files a plain-HTML site needs. */
async function browserFiles(src) {
  const files = new Set(), queue = ['auto.js'];
  while (queue.length) {
    const file = queue.pop();
    if (files.has(file)) continue;
    files.add(file);
    const text = await readFile(path.join(src, file), 'utf8');
    for (const [, dep] of text.matchAll(/(?:from|import)\s*['"]\.\/([\w.-]+\.js)['"]/g)) queue.push(dep);
  }
  return [...files].sort();
}

const DEPENDENCY_FIELDS = ['dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'];

/** [op, text] lines for the dependencies a package.json change added or removed, or null if anything else changed. */
function dependencyChanges(before, after) {
  let a, b;
  try { a = JSON.parse(before); b = JSON.parse(after); } catch { return null; }
  const rest = o => JSON.stringify(Object.fromEntries(Object.entries(o).filter(([k]) => !DEPENDENCY_FIELDS.includes(k))));
  if (rest(a) !== rest(b)) return null;
  const lines = [];
  for (const field of DEPENDENCY_FIELDS) {
    const was = a[field] ?? {}, now = b[field] ?? {};
    for (const [name, spec] of Object.entries(now)) if (was[name] !== spec) lines.push(['+', `${field}  ${name} ${spec}`]);
    for (const [name, spec] of Object.entries(was)) if (!(name in now)) lines.push(['-', `${field}  ${name} ${spec}`]);
  }
  return lines;
}

/** An agent's closing words without Markdown, on one line. */
const plain = text => text.replace(/```[\s\S]*?```/g, '').replace(/[`*_]{1,2}([^`*_]+)[`*_]{1,2}/g, '$1').replace(/\s+/g, ' ').trim().slice(0, 600);

async function undo(ui, root, changes) {
  const left = await restore(root, changes);
  if (left.length) ui.warn('These were too big to keep a copy of, so they stay changed:', left);
  if (changes.some(ch => path.basename(ch.path) === 'package.json')) ui.note(ui.c.dim('node_modules may still hold the package; your next install tidies it.'));
}

async function finish(args, ui, project, env, fetch, { coding }) {
  const { c } = ui;
  if (!args['no-verify'] && ui.interactive) await verify(ui, project, env, fetch);
  if (ui.interactive) {
    const skills = await ui.confirm({ message: 'Add Orion\'s skills to your coding agents?', initial: coding.length > 0, yes: 'Add them', no: 'Skip' });
    if (skills) {
      ui.note(c.dim(`Running npx skills add ${REPO}`));
      await run('npx', ['-y', 'skills', 'add', REPO], { stdio: 'inherit', cwd: project.root });
    }
  }
  ui.outro('Orion is set up.', [
    `${c.dim('Train a sentiment model')}  pip install "paradigm-orion @ git+https://github.com/${REPO}"  ${c.dim('then')}  orion init`,
    `${c.dim('Docs')}                     https://github.com/${REPO}#readme`,
  ]);
  return 0;
}

async function verify(ui, project, env, fetch) {
  const { c } = ui;
  let found = await findKey(POSTHOG_KEY, env);
  if (!found) {
    const how = await ui.select({ message: 'Watch for Orion\'s first events in PostHog?', initial: 'paste', options: [
      { value: 'paste', label: 'Paste a personal API key', hint: `needs ${SCOPES}` },
      { value: 'open', label: 'Open PostHog to create one', hint: `choose the ${SCOPES} scopes` },
      { value: 'skip', label: 'Skip the check' }] });
    if (how === 'skip') return;
    if (how === 'open') openUrl(`${HOSTS[project.posthog.region ?? 'us']}/settings/user-api-keys`);
    const value = await ui.text({ message: 'PostHog personal API key', secret: true, placeholder: 'phx_…' });
    if (!value) return;
    found = { value, from: 'typed' };
  }
  let place;
  try {
    place = await findProject({ key: found.value, token: project.posthog.token, region: project.posthog.region, fetch });
  } catch (error) {
    ui.warn(`Cannot check events: ${error.message}.`);
    return;
  }
  if (found.from === 'typed' && await ui.confirm({ message: `Keep the key for Orion's training tools in ${tilde(keysFile(env))}?`, yes: 'Keep it', no: 'Just this run' })) {
    await saveKeys({ [POSTHOG_KEY]: found.value }, env);
  }
  let chosen = place.project;
  if (!chosen) {
    if (!place.projects.length) { ui.warn('The key sees no PostHog projects.'); return; }
    const id = await ui.select({ message: 'Which PostHog project does this app send to?', options: place.projects.map(p => ({ value: p.id, label: p.name, hint: `id ${p.id}` })) });
    chosen = place.projects.find(p => p.id === id);
  }
  ui.step('Now open your app', [`${project.devCommand ? `Start it (${c.cyan(project.devCommand)}), open` : 'Open'} it in a browser, and move the cursor around.`]);
  const spin = ui.spinner();
  spin.start(`Waiting for Orion's first events in ${chosen.name}  ${c.dim('Enter skips')}`);
  const skip = ui.skippable();
  let events = null;
  try {
    events = await waitForEvents({ host: place.host, key: found.value, projectId: chosen.id, since: Date.now(), signal: skip.signal, fetch });
  } catch (error) {
    spin.stop(`Could not check: ${error.message}`, false);
    return;
  } finally {
    skip.done();
  }
  if (events) {
    spin.stop(`Orion is live: ${events.count} event${events.count > 1 ? 's' : ''}${events.browser ? ` from ${events.browser}` : ''}${events.host ? ` on ${events.host}` : ''}`);
    ui.note(c.dim(`${place.host}/project/${chosen.id}/activity/explore`));
  } else {
    spin.stop('Skipped. Orion\'s events show up in PostHog as orion_input once the app runs', true);
  }
}
