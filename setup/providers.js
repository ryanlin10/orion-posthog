// Tool calling on Anthropic, OpenAI, Gemini and Gemini on Vertex AI (Google Cloud) with the developer's own key, for
// Orion's built-in setup agent.
// Each client keeps the conversation in its provider's own shape and sends replies back unchanged (thinking blocks,
// reasoning items and thought signatures included), so every provider sees a consistent, append-only history.
// ORION_ANTHROPIC_URL, ORION_OPENAI_URL, ORION_GEMINI_URL and ORION_VERTEX_URL point the clients elsewhere (scheme and
// host, no /v1).

export const PROVIDERS = ['anthropic', 'openai', 'gemini', 'vertex'];
export const NAMES = { anthropic: 'Anthropic', openai: 'OpenAI', gemini: 'Gemini', vertex: 'Vertex AI' };
export const KEY_NAMES = { anthropic: 'ANTHROPIC_API_KEY', openai: 'OPENAI_API_KEY', gemini: 'GEMINI_API_KEY', vertex: 'VERTEX_API_KEY' };
export const DEFAULT_MODELS = { anthropic: 'claude-opus-5-5', openai: 'gpt-5.6-sol', gemini: 'gemini-3.8-flash', vertex: 'gemini-3.8-flash' };
const BASE = { anthropic: 'https://api.anthropic.com', openai: 'https://api.openai.com', gemini: 'https://generativelanguage.googleapis.com',
  vertex: 'https://aiplatform.googleapis.com' };
// Vertex AI takes the Gemini API's requests at another path; with an API key it needs no project or region in it.
const MODELS_PATH = { gemini: '/v1beta/models', vertex: '/v1beta1/publishers/google/models' };
const RETRY = new Set([408, 429, 500, 502, 503, 504, 529]);

export class ProviderError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export function providerFor(model) {
  const [prefix, rest] = String(model).split(/:(.*)/s);
  if (rest && PROVIDERS.includes(prefix.toLowerCase())) return [prefix.toLowerCase(), rest];
  if (/^claude-/i.test(model)) return ['anthropic', model];
  if (/^(gpt-|chatgpt-|o\d)/i.test(model)) return ['openai', model];
  if (/^gemini-/i.test(model)) return ['gemini', model];
  throw new ProviderError(`Cannot tell which provider serves ${model}: use a claude-*, gpt-* or gemini-* name, or put anthropic:, openai:, gemini: or vertex: in front`);
}

const base = (provider, env = process.env) => (env[`ORION_${provider.toUpperCase()}_URL`] || BASE[provider]).replace(/\/+$/, '');

async function post(name, url, headers, body, { fetch = globalThis.fetch, sleep, attempts = 4 } = {}) {
  for (let attempt = 0; ; attempt++) {
    let response;
    try {
      response = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json', ...headers },
        body: JSON.stringify(body), signal: AbortSignal.timeout(300_000) });
    } catch (error) {
      if (attempt < attempts - 1) { await (sleep ?? wait)(2000 * 2 ** attempt); continue; }
      throw new ProviderError(`Could not reach ${name}: ${error.cause?.code ?? error.message}`);
    }
    const text = await response.text();
    if (response.ok) {
      try { return JSON.parse(text); } catch { throw new ProviderError(`${name} replied with something other than JSON`); }
    }
    if (RETRY.has(response.status) && attempt < attempts - 1) {
      const after = Number(response.headers.get('retry-after'));
      await (sleep ?? wait)(Number.isFinite(after) && after > 0 ? Math.min(after, 60) * 1000 : 2000 * 2 ** attempt);
      continue;
    }
    let detail = text.replace(/\s+/g, ' ').slice(0, 300);
    for (const value of Object.values(headers)) if (value.length > 12) detail = detail.replaceAll(value.replace(/^Bearer /, ''), '[key]');
    const plainly = { 401: `${name} did not accept the API key`, 403: `${name} refused the API key`, 404: `${name} does not know that model` }[response.status];
    throw new ProviderError(plainly ? `${plainly} (HTTP ${response.status})` : `${name} returned HTTP ${response.status}: ${detail}`, response.status);
  }
}

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

// Tool schemas are written once in JSON Schema with every property required, which all three strict modes accept.
const geminiSchema = schema => JSON.parse(JSON.stringify(schema, (key, value) => (key === 'additionalProperties' ? undefined : value)));

/** A conversation with tools. send(results) returns { text, calls: [{ id, name, input }] }; no calls means done. */
export function createClient({ provider, model, key, system, prompt, tools, fetch, sleep, env }) {
  const options = { fetch, sleep };
  if (provider === 'anthropic') {
    const messages = [{ role: 'user', content: prompt }];
    let fallbacks = true;
    return {
      async send(results) {
        if (results) messages.push({ role: 'user', content: results.map(r => ({ type: 'tool_result', tool_use_id: r.id, content: r.output, ...(r.error && { is_error: true }) })) });
        const body = { model, max_tokens: 16000, system, messages, cache_control: { type: 'ephemeral' },
          output_config: { effort: 'medium' },
          tools: tools.map(t => ({ name: t.name, description: t.description, input_schema: t.parameters, strict: true })) };
        const headers = { 'x-api-key': key, 'anthropic-version': '2023-06-01' };
        let reply;
        try {
          // On a safety decline the API retries the turn on another Claude model by itself.
          reply = await post('Anthropic', `${base('anthropic', env)}/v1/messages`,
            fallbacks ? { ...headers, 'anthropic-beta': 'server-side-fallback-2026-07-01' } : headers,
            fallbacks ? { ...body, fallbacks: 'default' } : body, options);
        } catch (error) {
          if (!(fallbacks && error.status === 400 && /fallback|beta/i.test(error.message))) throw error;
          fallbacks = false;
          reply = await post('Anthropic', `${base('anthropic', env)}/v1/messages`, headers, body, options);
        }
        if (reply.stop_reason === 'refusal') throw new ProviderError('The model declined this request');
        messages.push({ role: 'assistant', content: reply.content });
        const text = reply.content.filter(b => b.type === 'text').map(b => b.text).join('');
        const calls = reply.content.filter(b => b.type === 'tool_use').map(b => ({ id: b.id, name: b.name, input: b.input ?? {} }));
        if (!calls.length && reply.stop_reason === 'max_tokens') throw new ProviderError('The model ran out of room before finishing');
        return { text, calls };
      },
    };
  }
  if (provider === 'openai') {
    const input = [{ role: 'user', content: prompt }];
    return {
      async send(results) {
        if (results) input.push(...results.map(r => ({ type: 'function_call_output', call_id: r.id, output: r.output })));
        // store: false keeps the code off OpenAI's servers; the encrypted reasoning carries the model's thinking across turns.
        const reply = await post('OpenAI', `${base('openai', env)}/v1/responses`, { authorization: `Bearer ${key}` }, {
          model, instructions: system, input, store: false, include: ['reasoning.encrypted_content'], max_output_tokens: 16000,
          tools: tools.map(t => ({ type: 'function', name: t.name, description: t.description, parameters: t.parameters, strict: true })),
        }, options);
        const output = reply.output ?? [];
        input.push(...output);
        const calls = output.filter(item => item.type === 'function_call').map(item => {
          let args;
          try { args = JSON.parse(item.arguments || '{}'); } catch { args = {}; }
          return { id: item.call_id, name: item.name, input: args };
        });
        const text = output.filter(item => item.type === 'message').flatMap(item => item.content ?? [])
          .filter(part => part.type === 'output_text').map(part => part.text).join('');
        if (!calls.length && reply.status === 'incomplete') throw new ProviderError(`OpenAI stopped early (${reply.incomplete_details?.reason ?? 'unknown'})`);
        return { text, calls };
      },
    };
  }
  if (provider === 'gemini' || provider === 'vertex') {
    const contents = [{ role: 'user', parts: [{ text: prompt }] }];
    let pending = [];  // the last reply's function calls, answered in the same order
    return {
      async send(results) {
        if (results) {
          contents.push({ role: 'user', parts: results.map((r, i) => ({ functionResponse: {
            name: r.name, ...(pending[i]?.id && { id: pending[i].id }), response: r.error ? { error: r.output } : { result: r.output } } })) });
        }
        const reply = await post(NAMES[provider], `${base(provider, env)}${MODELS_PATH[provider]}/${encodeURIComponent(model)}:generateContent`, { 'x-goog-api-key': key }, {
          systemInstruction: { parts: [{ text: system }] }, contents,
          tools: [{ functionDeclarations: tools.map(t => ({ name: t.name, description: t.description, parameters: geminiSchema(t.parameters) })) }],
          generationConfig: { maxOutputTokens: 16000 },
        }, options);
        const candidate = reply.candidates?.[0];
        if (!candidate?.content) throw new ProviderError(`Gemini gave no answer (${reply.promptFeedback?.blockReason ?? candidate?.finishReason ?? 'no candidates'})`);
        contents.push(candidate.content);
        const parts = candidate.content.parts ?? [];
        pending = parts.filter(p => p.functionCall).map(p => p.functionCall);
        const calls = pending.map((call, i) => ({ id: call.id ?? `call-${contents.length}-${i}`, name: call.name, input: call.args ?? {} }));
        const text = parts.filter(p => p.text && !p.thought).map(p => p.text).join('');
        return { text, calls };
      },
    };
  }
  throw new ProviderError(`Unknown provider ${provider}`);
}

/** Checks a key by listing the provider's models, which needs a valid key and costs nothing. Vertex AI lists no models
 * for an API key, so its check counts tokens instead (also free). A key the Gemini API refuses gets a second check on
 * Vertex AI, since Google Cloud keys are often allowed only there; then the result says provider: 'vertex'. */
export async function checkKey(provider, key, { fetch = globalThis.fetch, env } = {}) {
  const url = { anthropic: '/v1/models', openai: '/v1/models', gemini: '/v1beta/models',
    vertex: `${MODELS_PATH.vertex}/${DEFAULT_MODELS.vertex}:countTokens` }[provider];
  const headers = { anthropic: { 'x-api-key': key, 'anthropic-version': '2023-06-01' }, openai: { authorization: `Bearer ${key}` },
    gemini: { 'x-goog-api-key': key }, vertex: { 'x-goog-api-key': key, 'content-type': 'application/json' } }[provider];
  const body = provider === 'vertex' ? JSON.stringify({ contents: [{ role: 'user', parts: [{ text: 'Orion key check' }] }] }) : undefined;
  try {
    const response = await fetch(base(provider, env) + url, { method: body ? 'POST' : 'GET', headers, body, signal: AbortSignal.timeout(8000) });
    if (response.ok) return { ok: true, message: `${NAMES[provider]} accepted the key` };
    if (provider === 'gemini' && (await checkKey('vertex', key, { fetch, env })).ok) {
      return { ok: true, provider: 'vertex', message: 'That is a Google Cloud key for Vertex AI, not the Gemini API, so Orion will run Gemini on Vertex AI' };
    }
    return { ok: false, message: `${NAMES[provider]} did not accept the key (HTTP ${response.status})` };
  } catch (error) {
    return { ok: false, message: `Could not reach ${NAMES[provider]} (${error.cause?.code ?? error.message})` };
  }
}
