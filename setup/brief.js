// What any agent is asked to do, written once so Orion's own agent, Claude Code, Codex and Cursor all get the same
// task, and so the copy-it-yourself path shows the same code.
import path from 'node:path';
import { PACKAGE } from './project.js';

export function optionsLiteral(options) {
  const parts = Object.entries(options).map(([key, value]) => `${key}: ${typeof value === 'string' ? `'${value}'` : value}`);
  return `{ ${parts.join(', ')} }`;
}

const places = project => project.posthog.places.slice(0, 8).map(p => `  - ${p.file}:${p.line}  ${p.text}`).join('\n');

/** The script tags for a page, with src relative to that page (or root-relative for a public/ or static/ folder). */
function scriptTags(options, src) {
  return [`<script>window.orionOptions = ${optionsLiteral(options)}</script>`, `<script type="module" src="${src}"></script>`];
}

export function scriptSrc(vendor, page) {
  if (vendor.startsWith('public/')) return `/${vendor.slice('public/'.length)}/auto.js`;
  if (vendor.startsWith('static/')) return `/${vendor}/auto.js`;
  const rel = path.posix.relative(path.posix.dirname(page), vendor) || '.';
  return `${rel.startsWith('.') ? rel : `./${rel}`}/auto.js`;
}

export function brief(project, options, { vendor } = {}) {
  const head = `Add Orion to this web app. Orion is a small browser library that attaches to the app's existing PostHog client and
records input timing (keyboard, pointer and scrolling) into the same PostHog sessions.

About the project
- Folder: ${project.root}
- Framework: ${project.framework}
- PostHog starts here:
${places(project)}`;

  const rules = `Rules
- Make the smallest change that does this. Follow each file's existing style (quotes, semicolons, TypeScript). Add no comments.
- Edit only files inside the project folder. Do not install packages, run commands, or change package.json or lockfiles.
- Never open .env files or anything that holds keys.
- When you are done, reply with one or two plain sentences naming each file you changed.`;

  if (project.posthog.kind === 'snippet') {
    const page = project.posthog.places[0].file;
    return `${head}
- Orion's browser files are already copied into ${vendor}/ (auto.js is the entry point).

The change
In every page or shared layout/template that contains the PostHog snippet, add these two tags right after the
snippet's closing </script>:
${scriptTags(options, scriptSrc(vendor, page)).map(t => `    ${t}`).join('\n')}
- The src must reach ${vendor}/auto.js from that page: a relative path for plain HTML files, or the framework's
  static-file helper for server templates (for example Django's {% static %}).
- Add the tags once per page, inside the same consent check as the snippet if there is one.

${rules}`;
  }
  return `${head}
- The package ${PACKAGE} is installed already${project.packageDir && project.packageDir !== project.root ? ` in ${path.relative(project.root, project.packageDir)}` : ''}.

The change
1. Where posthog-js starts in the browser, import Orion:
     import { orion } from '${PACKAGE}'
2. Right after posthog.init(...) runs in the browser, connect Orion to the same client:
     orion.connect(posthog, ${optionsLiteral(options)})
   - Use the client the app already has (the posthog-js default import, or the client given to PostHogProvider).
   - Keep the call in the same browser-only place as posthog.init: the same client component, effect,
     instrumentation-client file or typeof window check. It must never run during server rendering.
   - If posthog.init is inside a consent check or another condition, put the call in the same branch.
   - If <PostHogProvider> gets apiKey/options rather than a client, add a small client component inside the provider
     that gets the client with usePostHog() from 'posthog-js/react' and calls orion.connect once in an effect.
   - A second connect() for the same client is ignored, so React's double effects in development are harmless.

${rules}`;
}

/** The same change as text a developer can paste. */
export function manualSteps(project, options, { vendor } = {}) {
  if (project.posthog.kind === 'snippet') {
    const page = project.posthog.places[0].file;
    return [`After the PostHog snippet in ${page} (and every page that has it):`, '',
      ...scriptTags(options, scriptSrc(vendor, page)).map(t => `  ${t}`), '', `Orion's files are in ${vendor}/.`];
  }
  const where = project.posthog.places[0];
  return [`In ${where.file}, next to posthog.init (line ${where.line}):`, '',
    `  import { orion } from '${PACKAGE}'`, '', `  orion.connect(posthog, ${optionsLiteral(options)})`, '',
    'Keep the call in the browser only, right after posthog.init.'];
}
