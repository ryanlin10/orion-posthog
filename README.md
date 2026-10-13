<h1 align="center">Orion</h1>

<h3 align="center">See how your visitors feel from how they type and move the mouse</h3>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://github.com/ryanlin10/orion-posthog/actions/workflows/ci.yml"><img src="https://github.com/ryanlin10/orion-posthog/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

Keyboard and pointer telemetry on top of PostHog, sentiment classifiers trained from it, and live labels for your
agents.

## Set up

In your web app's folder:

```sh
npx github:ryanlin10/orion-posthog
```

The wizard adds Orion next to `posthog.init` and shows you the diff before anything changes. To add it by hand, put
this after your PostHog snippet:

```html
<script src="https://cdn.paradigmmachines.ai/orion.js" defer></script>
```

By default Orion keeps when a key went down and came up, never which key. Password fields and `.ph-no-capture` send
nothing. Event contents: [docs/data-format.md](docs/data-format.md).

## Train

```sh
pip install "paradigm-orion[sequence] @ git+https://github.com/ryanlin10/orion-posthog"   # Python 3.11+
orion init
orion pull --since 30d --recipe recipes/checkout-sentiment.yaml
orion train recipes/checkout-sentiment.yaml
orion deploy checkout-sentiment
```

Recipe format: [orion-datasets skill](skills/orion-datasets/SKILL.md).

## Use the labels

```js
window.orion.contextText()   // the visitor's current labels, for your agent's prompt
```

```sh
orion context <session_id>
orion search --label frustrated --page /checkout --last 30d
```

[MIT](LICENSE). Not affiliated with PostHog.
