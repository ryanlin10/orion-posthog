// Runs an exported Orion model (format "orion-model", version 1) in the browser.
// Python writes these files with `orion train`; see paradigm_orion/export.py for the producer.

const sigmoid = z => 1 / (1 + Math.exp(-z));
function softmax(z) {
  const m = Math.max(...z);
  const e = z.map(v => Math.exp(v - m));
  const s = e.reduce((a, b) => a + b, 0);
  return e.map(v => v / s);
}
function dot(w, x) {
  let s = 0;
  for (let i = 0; i < w.length; i++) s += w[i] * x[i];
  return s;
}
function leaf(tree, x) {
  let node = 0;
  while (tree.left[node] !== -1) node = x[tree.feature[node]] <= tree.threshold[node] ? tree.left[node] : tree.right[node];
  return tree.value[node];
}

export function validateModel(spec) {
  if (spec?.format !== 'orion-model' || spec.format_version !== 1) throw new TypeError('Not an Orion model (format orion-model v1)');
  if (!['categorical', 'continuous'].includes(spec.target?.type)) throw new TypeError('Unsupported target type');
  if (spec.estimator?.kind === 'onnx') {
    const e = spec.estimator;
    if (typeof e.file !== 'string' || !Number.isInteger(e.steps) || !(e.step_ms > 0)) throw new TypeError('Sequence model is missing its ONNX file or step settings');
    return spec;
  }
  const { names } = spec.features ?? {};
  const p = spec.preprocess ?? {};
  if (!Array.isArray(names) || !names.length) throw new TypeError('Model has no feature names');
  if (p.impute?.length !== names.length) throw new TypeError('Imputation does not match the features');
  const inputs = names.length + (p.indicators?.length ?? 0);
  if (p.mean?.length !== inputs || p.scale?.length !== inputs) throw new TypeError('Scaling does not match the features');
  if (!['linear', 'forest', 'boosted'].includes(spec.estimator?.kind)) throw new TypeError(`Unsupported estimator ${spec.estimator?.kind}`);
  return spec;
}

/** Feature object → model inputs, exactly as the training pipeline's imputer and scaler did. */
function prepare(spec, features) {
  const { names } = spec.features;
  const { impute, indicators = [], mean, scale } = spec.preprocess;
  const raw = names.map(n => features[n] ?? NaN);
  const x = raw.map((v, i) => (Number.isNaN(v) ? impute[i] : v));
  for (const i of indicators) x.push(Number.isNaN(raw[i]) ? 1 : 0);
  return x.map((v, i) => (v - mean[i]) / scale[i]);
}

function outputs(spec, x) {
  const e = spec.estimator;
  if (e.kind === 'linear') return e.coef.map((w, k) => dot(w, x) + e.intercept[k]);
  if (e.kind === 'forest') {
    const width = e.trees[0].value[0].length;
    const sum = new Array(width).fill(0);
    for (const tree of e.trees) leaf(tree, x).forEach((v, k) => { sum[k] += v; });
    return sum.map(v => v / e.trees.length);
  }
  // Boosted: one tree per output per stage, added to the initial raw score.
  const raw = [...e.init];
  for (const stage of e.trees) stage.forEach((tree, k) => { raw[k] += e.learning_rate * leaf(tree, x)[0]; });
  return raw;
}

/** Raw model outputs → { label, confidence, probabilities } for categories, { value } for scores. */
function interpret(spec, out) {
  if (spec.target.type === 'continuous') {
    const { min, max } = spec.target;
    return { value: Math.min(max, Math.max(min, out[0])) };
  }
  const classes = spec.estimator.classes;
  let probs;
  if (spec.estimator.kind === 'forest') probs = out;
  else if (out.length === 1) { const p = sigmoid(out[0]); probs = [1 - p, p]; }
  else probs = softmax(out);
  let best = 0;
  for (let k = 1; k < probs.length; k++) if (probs[k] > probs[best]) best = k;
  return { label: classes[best], confidence: probs[best], probabilities: Object.fromEntries(classes.map((c, k) => [c, probs[k]])) };
}

/** Tabular models: window features → prediction. */
export function predict(spec, features) {
  return interpret(spec, outputs(spec, prepare(spec, features)));
}

/** Sequence models: time steps → prediction, through an onnxruntime-web session. */
export async function predictSteps(spec, session, ort, steps) {
  const e = spec.estimator;
  const input = new ort.Tensor('float32', steps, [1, e.steps, steps.length / e.steps]);
  const result = await session.run({ [e.input ?? 'steps']: input });
  return interpret(spec, Array.from(result[e.output ?? 'out'].data));
}

const NAMES = {
  key_hold_ms: ['key holds', 'ms'], key_interval_ms: ['time between key presses', 'ms'],
  key_overlap_fraction: ['overlapping key presses', '%'], key_pause_fraction: ['pauses while typing', '%'],
  pointer_interval_ms: ['time between pointer samples', 'ms'], pointer_speed_px_s: ['pointer speed', 'px/s'],
  click_hold_ms: ['click holds', 'ms'], click_interval_ms: ['time between clicks', 'ms'],
  scroll_interval_ms: ['time between scroll steps', 'ms'], focus_dwell_ms: ['time in each field', 'ms'],
  key_down_count: ['key presses', ''], pointer_path_px: ['pointer travel', 'px'], event_count: ['input events', ''], duration_ms: ['window length', 'ms'],
};
function describe(name) {
  const cv = name.endsWith('_cv');
  const base = name.replace(/_(median|cv)$/, '');
  const [words, unit] = NAMES[base] ?? [base.replace(/_/g, ' '), ''];
  return cv ? [`variation in ${words}`, ''] : [words, unit];
}
function fmt(v, unit) {
  if (unit === '%') return `${Math.round(v * 100)}%`;
  const n = Math.abs(v) >= 100 ? Math.round(v).toLocaleString('en-US') : (Math.round(v * 100) / 100).toString();
  return unit ? `${n} ${unit}` : n;
}

/** The few features furthest from their training reference, weighted by importance, in plain words. */
export function evidence(spec, features, limit = 2) {
  const ref = spec.evidence?.reference ?? {};
  const spread = spec.evidence?.spread ?? {};
  const weight = spec.evidence?.importance ?? {};
  const scored = [];
  for (const name of spec.features.names) {
    const v = features[name];
    if (v === null || v === undefined || ref[name] === undefined || !spread[name]) continue;
    const z = (v - ref[name]) / spread[name];
    scored.push({ name, v, z, score: Math.abs(z) * (weight[name] ?? 1) });
  }
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, limit).filter(s => Math.abs(s.z) >= 0.5).map(({ name, v, z }) => {
    const [words, unit] = describe(name);
    return `${words} ${z > 0 ? 'higher' : 'lower'} than usual (${fmt(v, unit)} vs ${fmt(ref[name], unit)})`;
  });
}
