---
name: orion-deploy
description: Deploy a trained Orion classifier so it labels live sessions in visitors' browsers and routes labels to agents (in-page context, PostHog events, webhooks). Use when asked to ship, roll back or wire a classifier into an assistant.
---

# Orion deploy

```sh
orion deploy checkout-sentiment --out public/orion --context support-agent
orion deploy checkout-sentiment --version 3 --out public/orion              # roll back to v3
```

This copies `model.json` (and the `.onnx` file for sequence models) into a folder the site serves and prints the
snippet. Check `report.json` before deploying (see orion-train): do not ship a model that failed the shuffled-label check.

## Wire the site

After the PostHog snippet:

```html
<script>window.orionOptions = { mask: 'timing', models: ['/orion/checkout-sentiment.json'] };</script>
<script src="/vendor/orion.js" defer></script>
```

or with a bundler:

```js
import { orion } from '@paradigmmachines/orion';
const o = orion.connect(posthog, { mask: 'timing', models: ['/orion/checkout-sentiment.json'] });
await o.ready;          // names of the models that loaded
```

Sequence models (GRU, TCN, transformer) also need onnxruntime-web on the page first (`window.ort`), or pass
`ort` to `connect`.

## Where labels go

- **In the page:** `orion.context()`, `orion.contextText()`, `orion.onLabel(fn)`. Send `contextText()` to the
  assistant's backend with each message.
- **PostHog:** an `orion_label` event on each change (and every 60 s while unchanged), on the same session as the
  replay. `orion context <session>` and `orion search` read these.
- **Webhook:** in PostHog, add a destination (Data pipelines → Destinations → HTTP Webhook) filtered to the
  `orion_label` event, optionally on `properties.agents` containing the agent's name.

## Verify

Open the site, use it for a minute, then in the console: `orion.getStats().live` (models loaded, windows scored,
reasons for no read) and `orion.labels()`. Then `orion context <session id>` from a terminal to see the label in PostHog.
