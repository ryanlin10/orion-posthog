# Set up Orion with a coding agent

Copy this prompt into Claude Code, Codex, Cursor or any coding agent that reads skills, in your app's folder. The
agent installs Orion's skills for itself, then follows the `orion-setup` skill: it adds Orion to the browser,
installs the training tools, writes a first recipe and tells you what is left for later.

```text
Set up Orion in this project: https://github.com/ryanlin10/orion-posthog

Orion sits on top of our PostHog. It records fine-grained input timing (never what anyone types), shows a live mood
reading, can ask visitors how it is going, and trains sentiment classifiers whose labels feed our AI assistant.

1. Install Orion's skills for yourself in this project:
   npx skills add ryanlin10/orion-posthog --skill '*' --agent <you> --yes
   where <you> is claude-code, codex, cursor, github-copilot, gemini-cli or windsurf (npx skills add --help lists
   the rest). They land in .claude/skills/ or .agents/skills/. Read orion-setup/SKILL.md now and follow it step by
   step; the other orion-* skills cover the later steps.
2. Look before changing anything. Tell me where PostHog is initialised, how consent works, and where our AI
   assistant (if any) builds its prompts. If the app has no PostHog, stop and tell me.
3. Ask me before turning on the in-page question and before deciding what Orion should detect. Decide the rest
   yourself.
4. Never ask me to paste API keys into this chat and never write keys into files. When keys are needed, tell me to
   run `orion init` in my own terminal.
5. Check your work as orion-setup describes, then finish with a short summary: what you changed, what I need to do,
   and what to do once data has arrived.
```

The skills stay in the project, so later sessions find them too: ask the agent to train, deploy or investigate, and
it uses `orion-train`, `orion-deploy`, `orion-context` and `orion-research`.
