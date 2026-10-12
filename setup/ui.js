// The terminal side of `orion-setup`: a rail of steps down the left and arrow-key menus, drawn the way current setup
// wizards draw them. No dependencies. Without a terminal (CI, a pipe) or with --yes, every prompt takes its default and
// prints the choice, so a run still reads well in a log.
import readline from 'node:readline';

export class Cancelled extends Error {
  constructor() { super('Setup cancelled'); }
}

const S = { start: '┌', bar: '│', end: '└', active: '◆', done: '◇', cancel: '■', warn: '▲', on: '●', off: '○',
  checked: '◼', unchecked: '◻', spin: ['◒', '◐', '◓', '◑'] };
const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g;
const visible = text => String(text).replace(ANSI, '');

function colors(enabled) {
  const paint = (open, close) => text => (enabled ? `\x1b[${open}m${text}\x1b[${close}m` : String(text));
  return { dim: paint(2, 22), bold: paint(1, 22), green: paint(32, 39), red: paint(31, 39), yellow: paint(33, 39),
    cyan: paint(36, 39), magenta: paint(35, 39), gray: paint(90, 39), underline: paint(4, 24) };
}

export function createUI({ input = process.stdin, output = process.stdout, interactive, color } = {}) {
  interactive ??= Boolean(input.isTTY && output.isTTY);
  const c = colors(color ?? (!process.env.NO_COLOR && (process.env.FORCE_COLOR ? process.env.FORCE_COLOR !== '0' : Boolean(output.isTTY))));
  const write = text => output.write(text);
  const width = () => Math.max(40, output.columns || 80);
  const fit = text => {
    const room = width() - 4;
    return visible(text).length > room ? `${visible(text).slice(0, room - 1)}…` : text;
  };
  const rail = c.gray(S.bar);
  let active = null;  // the running spinner, if any
  const line = (text = '') => write(`${rail}${text ? `  ${text}` : ''}\n`);

  function rows(text) {
    return text.replace(/\n$/, '').split('\n').reduce((n, l) => n + Math.max(1, Math.ceil(visible(l).length / width())), 0);
  }

  // One prompt: draws its frame, redraws it on every key, and leaves a one-line summary behind when answered.
  function prompt({ message, draw, key, summary, resize }) {
    return new Promise((resolve, reject) => {
      let drawn = 0;
      const paint = (text, final = false) => {
        if (drawn) write(`\x1b[${drawn}A\r\x1b[J`);
        write(text);
        drawn = final ? 0 : rows(text);
      };
      const head = (symbol, body) => `${rail}\n${symbol}  ${message}\n${body}`;
      const render = () => paint(head(c.cyan(S.active), draw()) + `${c.cyan(S.end)}\n`);
      readline.emitKeypressEvents(input);
      const wasRaw = input.isRaw;
      input.setRawMode?.(true);
      input.resume();
      write('\x1b[?25l');
      const finish = (error, value) => {
        input.off('keypress', onKey);
        output.off?.('resize', onResize);
        input.setRawMode?.(wasRaw ?? false);
        input.pause();
        if (error) paint(head(c.red(S.cancel), `${rail}  ${c.dim(c.red('Cancelled'))}\n`), true);
        else paint(head(c.green(S.done), `${rail}  ${c.dim(summary(value))}\n`), true);
        write('\x1b[?25h');
        error ? reject(error) : resolve(value);
      };
      const onKey = (str, k = {}) => {
        if ((k.ctrl && k.name === 'c') || k.name === 'escape') return finish(new Cancelled());
        const result = key(str, k);
        if (result && 'value' in result) return finish(null, result.value);
        render();
      };
      // A prompt can also answer itself when the window changes size.
      const onResize = () => {
        const result = resize?.();
        if (result && 'value' in result) return finish(null, result.value);
        render();
      };
      input.on('keypress', onKey);
      output.on?.('resize', onResize);
      render();
    });
  }

  function answered(message, text) {
    write(`${rail}\n${c.green(S.done)}  ${message}\n${rail}  ${c.dim(text)}\n`);
  }

  const optionLine = (o, focused, mark) =>
    `${rail}  ${mark} ${focused ? o.label : c.dim(o.label)}${focused && o.hint ? c.dim(`  ${o.hint}`) : ''}`;

  return {
    c, interactive,
    intro(title, subtitle) {
      write(`\n${c.gray(S.start)}  ${c.bold(title)}${subtitle ? `  ${c.dim(subtitle)}` : ''}\n`);
    },
    outro(text, lines = []) {
      write(`${rail}\n${c.gray(S.end)}  ${text}\n`);
      for (const l of lines) write(`   ${l}\n`);
      write('\n');
    },
    step(title, lines = []) {
      write(`${rail}\n${c.green(S.done)}  ${title}\n`);
      for (const l of lines) line(fit(l));
    },
    note: lines => [].concat(lines).forEach(l => line(l)),
    warn(text, lines = []) {
      write(`${rail}\n${c.yellow(S.warn)}  ${text}\n`);
      for (const l of lines) line(c.dim(l));
    },
    error(text, lines = []) {
      write(`${rail}\n${c.red(S.cancel)}  ${text}\n`);
      for (const l of lines) line(c.dim(l));
    },

    async select({ message, options, initial }) {
      let i = Math.max(0, options.findIndex(o => o.value === initial));
      if (!interactive) { answered(message, options[i].label); return options[i].value; }
      return prompt({
        message,
        draw: () => options.map((o, n) => optionLine(o, n === i, n === i ? c.green(S.on) : c.dim(S.off))).join('\n') + '\n',
        key: (str, k) => {
          if (k.name === 'up' || k.name === 'k') i = (i + options.length - 1) % options.length;
          else if (k.name === 'down' || k.name === 'j' || k.name === 'tab') i = (i + 1) % options.length;
          else if (k.name === 'return') return { value: options[i].value };
        },
        summary: value => options.find(o => o.value === value).label,
      });
    },

    async multiselect({ message, options, initial = [] }) {
      const chosen = new Set(initial);
      const pick = () => options.filter(o => chosen.has(o.value)).map(o => o.value);
      const summary = values => options.filter(o => values.includes(o.value)).map(o => o.label).join(', ') || 'None';
      if (!interactive) { answered(message, summary(pick())); return pick(); }
      let i = 0;
      return prompt({
        message,
        draw: () => options.map((o, n) =>
          optionLine(o, n === i, chosen.has(o.value) ? c.green(S.checked) : c.dim(S.unchecked))).join('\n') +
          `\n${rail}  ${c.dim('Space to choose, Enter to go on')}\n`,
        key: (str, k) => {
          if (k.name === 'up' || k.name === 'k') i = (i + options.length - 1) % options.length;
          else if (k.name === 'down' || k.name === 'j' || k.name === 'tab') i = (i + 1) % options.length;
          else if (k.name === 'space') chosen.has(options[i].value) ? chosen.delete(options[i].value) : chosen.add(options[i].value);
          else if (k.name === 'return') return { value: pick() };
        },
        summary,
      });
    },

    async confirm({ message, initial = true, yes = 'Yes', no = 'No' }) {
      let value = initial;
      if (!interactive) { answered(message, value ? yes : no); return value; }
      return prompt({
        message,
        draw: () => `${rail}  ${value ? `${c.green(S.on)} ${yes}` : c.dim(`${S.off} ${yes}`)} ${c.dim('/')} ${!value ? `${c.green(S.on)} ${no}` : c.dim(`${S.off} ${no}`)}\n`,
        key: (str, k) => {
          if (['left', 'right', 'tab', 'h', 'l'].includes(k.name)) value = !value;
          else if (k.name === 'y') return { value: true };
          else if (k.name === 'n') return { value: false };
          else if (k.name === 'return') return { value };
        },
        summary: v => (v ? yes : no),
      });
    },

    async text({ message, placeholder = '', initial = '', secret = false, validate }) {
      if (!interactive) { answered(message, secret ? (initial ? 'given' : 'none') : initial || 'none'); return initial; }
      let value = initial, problem = '';
      return prompt({
        message,
        draw: () => {
          const shown = secret ? '•'.repeat(Math.min(value.length, 48)) : value;
          return `${rail}  ${value ? shown : c.dim(placeholder)}${c.cyan('▏')}\n${problem ? `${rail}  ${c.yellow(problem)}\n` : ''}`;
        },
        key: (str, k) => {
          problem = '';
          if (k.name === 'return') {
            problem = validate?.(value.trim()) || '';
            if (!problem) return { value: value.trim() };
          } else if (k.name === 'backspace') value = value.slice(0, -1);
          else if (k.ctrl && k.name === 'u') value = '';
          else if (str && !k.ctrl && !k.meta && /^[^\x00-\x1f\x7f]+$/.test(str)) value += str;
        },
        summary: v => (secret ? (v ? 'Saved' : 'Skipped') : v || 'Skipped'),
      });
    },

    /** Waits until the window is at least columns × rows; resizing goes on by itself, Enter goes on anyway. */
    async roomFor({ columns, rows, message }) {
      const size = () => `${output.columns}×${output.rows}`;
      const fits = () => output.columns >= columns && output.rows >= rows;
      if (!interactive || !output.rows || fits()) return true;
      return prompt({
        message,
        draw: () => `${rail}  ${c.yellow(`This window is ${size()}; it needs at least ${columns}×${rows}.`)}\n` +
          `${rail}  ${c.dim('Make it bigger and setup carries on, or press Enter to go on anyway.')}\n`,
        key: (str, k) => (k.name === 'return' ? { value: false } : undefined),
        resize: () => (fits() ? { value: true } : undefined),
        summary: ok => (ok ? `Now ${size()}` : `Going on at ${size()}`),
      });
    },

    // A spinner that can also log finished actions above itself (the agent's reads and edits).
    spinner() {
      let timer = null, message = '', frame = 0;
      const self = {};
      const tick = () => write(`\r\x1b[K${c.magenta(S.spin[frame++ % S.spin.length])}  ${fit(message)}`);
      return Object.assign(self, {
        start(text) {
          active = self;
          message = text;
          write(`${rail}\n`);
          if (!interactive) { write(`${c.magenta(S.spin[0])}  ${text}\n`); return; }
          write('\x1b[?25l');
          tick();
          timer = setInterval(tick, 90);
        },
        message(text) { message = text; },
        pause() { clearInterval(timer); if (interactive) write('\r\x1b[K\x1b[?25h'); },
        resume() { if (interactive) { write('\x1b[?25l'); tick(); timer = setInterval(tick, 90); } },
        log(text) {
          if (!interactive) { line(c.dim(fit(text))); return; }
          write(`\r\x1b[K${rail}  ${c.dim(fit(text))}\n`);
          tick();
        },
        stop(text, ok = true) {
          clearInterval(timer);
          if (active === self) active = null;
          const symbol = ok ? c.green(S.done) : c.red(S.cancel);
          write(`${interactive ? '\r\x1b[K' : ''}${symbol}  ${text}\n`);
          if (interactive) write('\x1b[?25h');
        },
      });
    },

    /** Stops whatever is spinning (on Ctrl-C) so the next line starts clean. */
    halt() {
      active?.pause();
      active = null;
    },

    // Enter, Esc or q while something waits (the event check): an AbortSignal for the waiting code.
    skippable() {
      const controller = new AbortController();
      if (!interactive) return { signal: controller.signal, done() {} };
      readline.emitKeypressEvents(input);
      const wasRaw = input.isRaw;
      input.setRawMode?.(true);
      input.resume();
      const onKey = (str, k = {}) => {
        if (k.name === 'return' || k.name === 'escape' || k.name === 'q' || (k.ctrl && k.name === 'c')) controller.abort();
      };
      input.on('keypress', onKey);
      return {
        signal: controller.signal,
        done() { input.off('keypress', onKey); input.setRawMode?.(wasRaw ?? false); input.pause(); },
      };
    },
  };
}
