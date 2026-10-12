---
name: orion-research
description: Investigate how a product feels to use by searching Orion's inferred labels (such as frustrated or confused) across PostHog sessions, lining them up with pages and product events, and testing explanations on held-out sessions. Use for questions like "where do people get frustrated?" or "why do users rate the assistant highly but use it less?".
---

# Orion research

Orion writes one `orion_label` event per label change (plus a heartbeat) into the team's PostHog project, with
`$session_id`, `$pathname`, model, label, confidence and plain-words evidence. Search those, then dig in with
PostHog's own data.

## Loop

1. **Question.** Restate it as something measurable on sessions.
2. **Search.**
   ```sh
   orion search --label frustrated --page /checkout --last 30d          # sessions, plus pages ranked by share
   orion search --label frustrated --last 30d --json > frustrated.json
   orion search --label frustrated --cohort "Paying customers"         # one PostHog cohort, by name or id
   orion context <session_id>                                           # one session's labels and evidence
   ```
   `share` is the fraction of labelled sessions on that page that carried the label. Compare shares, not raw counts.
   Busy pages have more of every label. Run the same search per cohort to compare groups of people.
3. **Hypotheses.** Write each as a testable statement about behaviour or product events, e.g. "frustration rises
   after a coupon error". Use PostHog (SQL, funnels, the PostHog MCP if available) to pull the events around the labelled moments.
4. **Experiments.** Compare labelled and unlabelled sessions on the same page and period. Check whether the pattern
   holds on sessions you did not look at while forming the hypothesis (hold out a random half before step 3).
   If a new classifier is needed, use the orion-datasets and orion-train skills.
5. **Report.** For each hypothesis: holds, doesn't, or unclear; the numbers; and 3 to 5 replay links (`replay_url`)
   that show it. Say how many sessions each claim rests on.

## Cautions

- Labels are model inferences. Check the model's report (`.orion/models/<name>/v<N>/report.json`) before relying on
  a label: its validation score and whether it beat models trained on shuffled labels.
- Absence of a label is not calm. Sessions with little input get no label.
- A page with few sessions can show an extreme share by chance. Prefer findings that hold across weeks.
