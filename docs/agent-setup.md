# Set up Orion with a coding agent

Copy this prompt into Claude Code, Codex, Cursor or any coding agent that reads skills, in your app's folder. The
agent installs Orion's skills for itself, then follows the `orion-setup` skill: it adds Orion to the browser,
installs the training tools, writes a first recipe and tells you what is left for later.

```text
Set up Orion in this project: https://github.com/ryanlin10/orion-posthog

Orion sits on top of our PostHog. It records fine-grained input timing (never what anyone types), shows a live mood reading, can ask visitors how it is going, and trains sentiment classifiers whose labels feed our AI assistant.

1. Install Orion's skills for yourself in this project:
   npx skills add ryanlin10/orion-posthog --skill '*' --agent <you> --yes
   <you> is claude-code, codex, cursor, github-copilot, gemini-cli or windsurf (npx skills add --help lists the rest). The skills land in .claude/skills/ or .agents/skills/. If your sandbox blocks the install, ask me to run that command in my terminal, and meanwhile read the skills at https://github.com/ryanlin10/orion-posthog/tree/main/skills.
   Then read orion-setup/SKILL.md and follow it step by step. The other orion-* skills cover the later steps.
2. Look before changing anything. Tell me where PostHog is initialised, how consent works, and where our AI assistant (if any) builds its prompts. If the app has no PostHog, stop and tell me.
3. Ask me before turning on the in-page question. Decide the rest yourself with the defaults in orion-setup.
4. Never ask me to paste API keys into this chat, and never write keys into files. When keys are needed, run `orion init` from the app's folder: it opens a page in my browser where I sign in to PostHog and add the AI key. Then run `orion check`.
5. Check your work as orion-setup describes. Then finish with a short summary in your reply (don't add notes files to the project): what you changed, what I need to do, and what to do once data has arrived.
```

The skills stay in the project, so later sessions find them too: ask the agent to train, deploy or investigate, and
it uses `orion-train`, `orion-deploy`, `orion-context` and `orion-research`.
