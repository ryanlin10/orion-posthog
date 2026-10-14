<h1 align="center">Orion</h1>

<h3 align="center">See how your users feel from how they type and move the mouse</h3>

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

By default Orion keeps when a key went down and came up, never which key. It also notes when a field is pasted into or
autofilled (never what), how long the tab was hidden, how far down the page someone scrolled, and the box of the button
they pressed. Password fields and `.ph-no-capture` send nothing. Event contents: [docs/data-format.md](docs/data-format.md).

Before you train anything, `base: true` runs a small model that ships with Orion: signs of negative feeling from typing
rhythm and pointer movement, trained on the public DUX dataset. It is weak, and [docs/base-model.md](docs/base-model.md)
says how weak.

## Train

```sh
uv tool install "paradigm-orion[sequence] @ git+https://github.com/ryanlin10/orion-posthog"   # or pipx install; Python 3.11+
orion init      # sign in to PostHog in your browser and add an AI key; each is checked as you go
orion check     # what works, and what fixes the rest
orion train --deploy   # pull, label, build, train, check, and deploy if it beat shuffled labels
```

Start from [recipes/site-feelings.yaml](recipes/site-feelings.yaml) (the setup agent writes the same file). Every
setting: [orion-datasets skill](skills/orion-datasets/SKILL.md).

## Use the labels

```js
window.orion.contextText()   // the user's current labels, for your agent's prompt
```

```sh
orion context <session_id>
orion search --label frustrated --page /checkout --last 30d
```

[MIT](LICENSE). Not affiliated with PostHog.
