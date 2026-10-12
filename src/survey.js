// Self-report: a small question in the corner of the page, like a PostHog survey, asking how it is going. It offers
// four answers, two positive and two negative, picked for relevance to the page and the visitor's recent input:
// from a survey file ranked ahead of time by Jev (`orion survey`), a function of yours, or the built-in mood reading.
// Each answer goes to PostHog as an `orion_self_report` event that labels the input just before the question.
export const SELF_REPORT_EVENT = 'orion_self_report';
export const POSITIVE = ['calm', 'pleased', 'engaged', 'confident'];
export const NEGATIVE = ['frustrated', 'confused', 'anxious', 'angry', 'bored'];
const NEGATIVE_MOODS = new Set(['angry', 'frustrated', 'anxious', 'confused']);
// How strongly each built-in mood reading suggests each answer, for ranking without a survey file.
const AFFINITY = {
  calm: { calm: 1 }, pleased: { pleased: 1 }, engaged: { pleased: 0.5, calm: 0.3 }, confident: { calm: 0.5, pleased: 0.5 },
  frustrated: { frustrated: 1, angry: 0.3 }, confused: { confused: 1, anxious: 0.2 }, anxious: { anxious: 1, confused: 0.2 },
  angry: { angry: 1, frustrated: 0.3 }, bored: { calm: 0.4 },
};
const STORAGE_KEY = 'orion-self-report';

/** The survey file's entry for a page: the longest matching path prefix, else '*'. */
export function surveyEntry(table, page) {
  let best = null;
  for (const key of Object.keys(table ?? {})) {
    if (key !== '*' && page.startsWith(key) && (best === null || key.length > best.length)) best = key;
  }
  return table?.[best ?? '*'] ?? null;
}

/** Two positive and two negative answers, most relevant first, and what ranked them. */
export function rankAnswers({ positive = POSITIVE, negative = NEGATIVE, table, page = '/', mood = null, probabilities = null }) {
  const entry = surveyEntry(table, page);
  const ranked = entry?.[mood ?? 'quiet'] ?? entry?.['*'];
  if (Array.isArray(ranked)) {
    const pos = ranked.filter(a => positive.includes(a)), neg = ranked.filter(a => negative.includes(a));
    if (pos.length >= 2 && neg.length >= 2) return { positive: pos.slice(0, 2), negative: neg.slice(0, 2), ranked_by: 'survey' };
  }
  if (probabilities) {
    const score = a => Object.entries(AFFINITY[a] ?? { [a]: 1 }).reduce((sum, [m, w]) => sum + w * (probabilities[m] ?? 0), 0);
    const order = list => list.map((a, i) => [a, score(a), i]).sort((x, y) => y[1] - x[1] || x[2] - y[2]).map(x => x[0]);
    return { positive: order(positive).slice(0, 2), negative: order(negative).slice(0, 2), ranked_by: 'mood' };
  }
  return { positive: positive.slice(0, 2), negative: negative.slice(0, 2), ranked_by: 'default' };
}

const CSS = `
:host { all: initial; }
.card { position: fixed; bottom: 20px; right: 20px; z-index: var(--orion-z, 2147483000); box-sizing: border-box;
  width: min(300px, calc(100vw - 32px)); padding: 16px 16px 14px; border-radius: 12px;
  background: var(--orion-bg, #fff); color: var(--orion-fg, #151515); border: 1px solid var(--orion-line, rgba(0,0,0,.12));
  box-shadow: 0 10px 30px rgba(0,0,0,.14); font: 14px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  animation: rise .18s ease-out; }
.card.left { right: auto; left: 20px; }
@media (max-width: 420px) { .card { right: 16px; left: 16px; bottom: 16px; width: auto; } .card.left { left: 16px; } }
@keyframes rise { from { opacity: 0; transform: translateY(8px); } }
@media (prefers-reduced-motion: reduce) { .card { animation: none; } }
@media (prefers-color-scheme: dark) { .card { background: var(--orion-bg-dark, #1c1c1e); color: var(--orion-fg-dark, #f2f2f2);
  border-color: var(--orion-line-dark, rgba(255,255,255,.16)); } }
p { margin: 0 28px 12px 0; font-weight: 600; }
.opts { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.opts button { font: inherit; padding: 9px 10px; border-radius: 8px; border: 1px solid currentColor; border-color: var(--orion-line, rgba(0,0,0,.16));
  background: transparent; color: inherit; cursor: pointer; text-align: center; }
@media (prefers-color-scheme: dark) { .opts button { border-color: var(--orion-line-dark, rgba(255,255,255,.2)); } }
.opts button:hover { background: rgba(127,127,127,.12); }
button:focus-visible { outline: 2px solid var(--orion-accent, #3b6cf6); outline-offset: 2px; }
.close { position: absolute; top: 8px; right: 8px; width: 28px; height: 28px; border: 0; border-radius: 6px; background: transparent;
  color: inherit; font: 18px/1 system-ui, sans-serif; cursor: pointer; opacity: .6; }
.close:hover { opacity: 1; background: rgba(127,127,127,.12); }
.thanks { margin: 0; font-weight: 500; }
`;

const title = answer => answer.charAt(0).toUpperCase() + answer.slice(1).replace(/[_-]/g, ' ');

function shuffle(list, random) {
  const out = [...list];
  for (let i = out.length - 1; i > 0; i--) { const j = Math.floor(random() * (i + 1)); [out[i], out[j]] = [out[j], out[i]]; }
  return out;
}

export function createSelfReport({ win, posthog, permitted, recorderId, mood, fetchJson, options = {} }) {
  const config = {
    question: options.question ?? 'How is it going so far?',
    positive: options.positive ?? POSITIVE,
    negative: options.negative ?? NEGATIVE,
    trigger: options.trigger ?? 'time',
    afterMs: options.afterMs ?? 45000,
    sample: options.sample ?? 1,
    cooldownDays: options.cooldownDays ?? 14,
    pages: options.pages ?? [],
    position: options.position ?? 'right',
    labels: options.labels ?? {},
    rank: options.rank ?? null,
    theme: options.theme ?? {},
    random: options.random ?? Math.random,
  };
  if (!['time', 'mood', 'manual'].includes(config.trigger)) throw new RangeError('selfReport.trigger must be time, mood or manual');
  for (const key of ['positive', 'negative']) {
    if (!Array.isArray(config[key]) || config[key].length < 2) throw new RangeError(`selfReport.${key} needs at least two answers`);
  }
  const doc = win.document;
  const started = win.performance.now();
  const sampled = config.random() < config.sample;
  let table = null, asked = false, host = null, pending = null, settle = null, timer, stopped = false;
  let lastInput = -1e9, lastKey = -1e9, pressed = 0, negativeSince = null;

  const ready = (async () => {
    const source = options.survey;
    if (!source) return;
    try {
      const survey = typeof source === 'string' ? await fetchJson(source) : source;
      if (survey?.format !== 'orion-survey') throw new Error('not an Orion survey file');
      table = survey.table;
      // A survey file built for a recipe brings that recipe's question and answers, unless the page set its own.
      if (!options.question && survey.question) config.question = survey.question;
      if (!options.positive && Array.isArray(survey.positive)) config.positive = survey.positive;
      if (!options.negative && Array.isArray(survey.negative)) config.negative = survey.negative;
    } catch (error) { console.error('[orion] self-report survey', error); }
  })();

  function cooledDown() {
    try {
      const at = JSON.parse(win.localStorage.getItem(STORAGE_KEY) ?? 'null')?.at;
      return !at || Date.now() - at > config.cooldownDays * 86400000;
    } catch { return true; }
  }
  function remember() { try { win.localStorage.setItem(STORAGE_KEY, JSON.stringify({ at: Date.now() })); } catch { /* storage blocked */ } }
  const page = () => win.location?.pathname ?? '/';

  // Ask while someone is using the page, never mid-word or mid-click.
  function settled(now) {
    return permitted() && !doc.hidden && now - lastInput < 10000 && now - lastKey > 3000 && pressed === 0;
  }
  function due(now) {
    if (stopped || asked || host || !sampled || config.trigger === 'manual' || now - started < config.afterMs) return false;
    if (config.pages.length && !config.pages.some(p => page().startsWith(p))) return false;
    if (config.trigger === 'mood') {
      const state = mood();
      const negative = state && NEGATIVE_MOODS.has(state.mood);
      negativeSince = negative ? negativeSince ?? now : null;
      if (negativeSince === null || now - negativeSince < 3000) return false;
    }
    return settled(now) && cooledDown();
  }

  async function choose() {
    await ready;
    const state = mood();
    const base = { positive: config.positive, negative: config.negative, table, page: page(), mood: state?.mood ?? null, probabilities: state?.probabilities ?? null };
    if (config.rank) {
      try {
        const custom = await Promise.race([config.rank({ ...base }), new Promise(resolve => win.setTimeout(() => resolve(null), 1500))]);
        const list = Array.isArray(custom) ? custom : custom ? [...(custom.positive ?? []), ...(custom.negative ?? [])] : [];
        const pos = list.filter(a => config.positive.includes(a)), neg = list.filter(a => config.negative.includes(a));
        if (pos.length >= 2 && neg.length >= 2) return { positive: pos.slice(0, 2), negative: neg.slice(0, 2), ranked_by: 'custom', state };
      } catch (error) { console.error('[orion] selfReport.rank', error); }
    }
    return { ...rankAnswers(base), state };
  }

  function send(props) {
    try { posthog.capture(SELF_REPORT_EVENT, props); } catch { /* the SDK reports its own failures */ }
  }

  function render(choice, trigger, resolve) {
    host = doc.createElement('div');
    // Orion's own capture ignores the question, so answering it never becomes part of anyone's telemetry.
    host.setAttribute('data-ph-behavior-ignore', '');
    host.setAttribute('data-orion-self-report', '');
    for (const [key, value] of Object.entries(config.theme)) host.style.setProperty(`--orion-${key}`, String(value));
    const root = host.attachShadow({ mode: 'open' });
    try {
      const sheet = new win.CSSStyleSheet();
      sheet.replaceSync(CSS);
      root.adoptedStyleSheets = [sheet]; // Constructed sheets are not blocked by a strict style-src.
    } catch {
      const style = doc.createElement('style');
      style.textContent = CSS;
      root.append(style);
    }
    const card = doc.createElement('div');
    card.className = config.position === 'left' ? 'card left' : 'card';
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-labelledby', 'orion-q');
    const close = doc.createElement('button');
    close.type = 'button'; close.className = 'close'; close.setAttribute('aria-label', 'Close'); close.textContent = '×';
    const question = doc.createElement('p');
    question.id = 'orion-q'; question.textContent = config.question;
    const opts = doc.createElement('div');
    opts.className = 'opts';
    const options = shuffle([...choice.positive, ...choice.negative], config.random);
    for (const answer of options) {
      const button = doc.createElement('button');
      button.type = 'button'; button.dataset.answer = answer; button.textContent = config.labels[answer] ?? title(answer);
      opts.append(button);
    }
    card.append(close, question, opts);
    root.append(card);
    doc.body.append(host);
    const shown = win.performance.now();
    const props = { question: config.question, options, positive: choice.positive, negative: choice.negative, ranked_by: choice.ranked_by,
      trigger, recorder_id: recorderId(), shown_t: shown, mood: choice.state?.mood ?? null, mood_probabilities: choice.state?.probabilities ?? null };
    settle = resolve;
    const finish = answer => {
      if (!host || !settle) return;
      send({ ...props, answer, dismissed: answer === null, answered_t: win.performance.now() });
      remember();
      settle = null;
      resolve(answer);
      if (answer === null) { hide(); return; }
      const thanks = doc.createElement('p');
      thanks.className = 'thanks'; thanks.textContent = 'Thanks!';
      card.replaceChildren(thanks);
      win.setTimeout(hide, 1200);
    };
    opts.addEventListener('click', e => { const answer = e.target?.closest?.('button')?.dataset.answer; if (answer) finish(answer); });
    close.addEventListener('click', () => finish(null));
    card.addEventListener('keydown', e => { if (e.key === 'Escape') finish(null); });
  }

  // Pausing (consent withdrawn) or stopping closes the question without sending anything.
  function hide() {
    host?.remove(); host = null; pending = null;
    const unanswered = settle;
    settle = null;
    unanswered?.(null);
  }

  /** Show the question now. Resolves with the answer, or null when dismissed or not allowed. */
  function ask(trigger = 'manual') {
    if (pending) return pending;
    if (stopped || !permitted() || !doc.body) return Promise.resolve(null);
    asked = true;
    pending = choose().then(choice => new Promise(resolve => {
      if (stopped || !permitted()) { pending = null; resolve(null); return; }
      render(choice, trigger, resolve);
    }));
    return pending;
  }

  timer = win.setInterval(() => { if (due(win.performance.now())) ask(config.trigger); }, 1000);

  return {
    ask,
    record(row) {
      if (row.kind === 'boundary') { pressed = 0; return; }
      lastInput = Math.max(lastInput, row.t);
      if (row.kind === 'keydown') lastKey = Math.max(lastKey, row.t);
      if (row.kind === 'pointerdown') pressed++;
      if ((row.kind === 'pointerup' || row.kind === 'pointercancel') && pressed > 0) pressed--;
    },
    hide,
    stop() { stopped = true; win.clearInterval(timer); hide(); },
  };
}
