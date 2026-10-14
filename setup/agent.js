// Orion's own setup agent: a language model with six tools (list, read, search, edit, create, ask), confined to the
// project by the workspace in files.js. It runs on the developer's own Anthropic, OpenAI or Gemini key.
import { ToolError } from './files.js';
import { createClient, ProviderError } from './providers.js';

const pathProp = { type: 'string', description: 'Path relative to the project folder, e.g. "src/main.tsx" or "." for the folder itself' };
const schema = (properties, ...required) => ({ type: 'object', properties, required, additionalProperties: false });

const TOOLS = [
  { name: 'list_files', description: 'List the files and folders in one folder of the project. Dependency and build folders are marked as skipped.',
    parameters: schema({ path: pathProp }, 'path') },
  { name: 'read_file', description: 'Read a text file from the project.', parameters: schema({ path: pathProp }, 'path') },
  { name: 'search', description: 'Search source files for a JavaScript regular expression. Returns up to 100 lines as file:line: text.',
    parameters: schema({ pattern: { type: 'string' }, path: pathProp }, 'pattern', 'path') },
  { name: 'edit_file', description: 'Replace one exact piece of text in a file. old_text must appear exactly once, so include enough surrounding lines; new_text replaces it.',
    parameters: schema({ path: pathProp, old_text: { type: 'string' }, new_text: { type: 'string' } }, 'path', 'old_text', 'new_text') },
  { name: 'create_file', description: 'Create a new file. Fails if the file exists; use edit_file for existing files.',
    parameters: schema({ path: pathProp, content: { type: 'string' } }, 'path', 'content') },
  { name: 'ask_user', description: 'Ask the developer a multiple-choice question when the code leaves a real choice open, such as which of several apps to change. Never ask for permission to make the change itself.',
    parameters: schema({ question: { type: 'string' }, choices: { type: 'array', items: { type: 'string' } } }, 'question', 'choices') },
];

const SYSTEM = `You are Orion's setup agent, running inside a developer's terminal. You make one small, careful change to their
web app with the tools you have, then stop. Look before you edit: find the file, read it, then edit it. Prefer editing an
existing file over creating one. If the project has several apps, ask which one with ask_user. Your final reply is shown
to the developer as is, so keep it to one or two plain sentences without Markdown.`;

/** What the progress line says for a tool call. */
function describe(name, input) {
  const p = input?.path && input.path !== '.' ? input.path : 'the project';
  return { list_files: `Looked in ${p}`, read_file: `Read ${p}`, search: `Searched for ${input?.pattern ?? '…'}`,
    edit_file: `Edited ${p}`, create_file: `Created ${p}`, ask_user: 'Asked you a question' }[name] ?? name;
}

export async function runAgent({ provider, model, key, brief, workspace, ask, onStep = () => {}, maxTurns = 40, fetch, sleep, env }) {
  const client = createClient({ provider, model, key, system: SYSTEM, prompt: brief, tools: TOOLS, fetch, sleep, env, onRetry: onStep });
  const handlers = {
    list_files: workspace.list, read_file: workspace.read, search: workspace.search, edit_file: workspace.edit, create_file: workspace.create,
    ask_user: async ({ question, choices }) => (await ask(question, choices)) ?? 'No answer; pick the most sensible option and say which.',
  };
  let results = null;
  for (let turn = 0; turn < maxTurns; turn++) {
    const { text, calls } = await client.send(results);
    if (!calls.length) return { summary: text.trim(), turns: turn + 1 };
    results = [];
    for (const call of calls) {
      const handler = handlers[call.name];
      try {
        if (!handler) throw new ToolError(`There is no tool called ${call.name}`);
        const output = await handler(call.input);
        onStep(describe(call.name, call.input));
        results.push({ id: call.id, name: call.name, output: String(output) });
      } catch (error) {
        if (!(error instanceof ToolError)) throw error;
        results.push({ id: call.id, name: call.name, output: error.message, error: true });
      }
    }
  }
  throw new ProviderError(`The agent did not finish within ${maxTurns} steps`);
}
