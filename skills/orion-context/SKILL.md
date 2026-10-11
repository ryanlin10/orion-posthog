---
name: orion-context
description: Read Orion's live label for a user session (for example "frustrated since 01:02", with evidence and a replay link) and use it in an assistant or support agent's context. Use when an agent should adapt to how the person on the other end is doing, or when asked what state a session is in.
---

# Orion context

Orion labels each live session in the visitor's browser with a classifier trained from their input timing
(pointer, keys, scroll, focus). Labels are inferences with a confidence, not facts about a person.

## Get the label

**In the page (instant).** If the site runs `orion.js`, the current label is local:

```js
const ctx = window.orion.context();      // { model, version, target, label, confidence, since, earlier, evidence, replay_url } or null
const text = window.orion.contextText(); // the same as a short YAML block for a prompt
window.orion.onLabel(ctx => { /* called when the label changes */ });
```

Send `contextText()` with each message the page sends to the assistant's backend.

**On a server (about a minute behind).** Labels also reach PostHog as `orion_label` events:

```sh
orion context <session_id>              # YAML block for a prompt
orion context <session_id> --json       # structured
orion context <session_id> --agent support-agent   # only models tagged for this agent
```

```python
from paradigm_orion import Orion
Orion().context(session_id, text=True)
```

The PostHog session ID is `posthog.get_session_id()` in the browser; pass it to the backend with each request.
Needs `POSTHOG_PERSONAL_API_KEY` (query:read) and the project in `.orion/config.json` or `POSTHOG_PROJECT_ID`.

## Use it well

- Put the block in the system or developer context, not in the user's message.
- Adapt how you answer: shorter answers, lead with the fix, check the obvious failure first. Do not tell the
  person how they feel ("I can see you're frustrated") and never quote the label back to them.
- Weigh `confidence` and `since`. A label older than a few minutes, or below about 0.6 confidence, is weak.
- `evidence` lists the input patterns behind the label in plain words; use it to decide what probably went wrong.
- `replay_url` opens the PostHog replay at that moment, for people investigating later.
- No label (`null` or `user_state: none`) means not enough recent input, a gap in capture, or no consent. Treat it as unknown.
