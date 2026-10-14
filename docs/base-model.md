# The base model

`orion.connect(posthog, { base: true })` runs a model that ships inside orion.js, so a site gets a reading before it has
trained its own. It is model `orion-base`, target `affect`, labels `neutral` or `negative`, scored every 2 seconds on
the last 10 seconds of input like any deployed model.

**What it reads:** typing rhythm (press-to-press and release-to-press quartiles, hold quartiles, pauses) and pointer
movement shape (directness, distance off the direct path, midline crossings, direction consistency, turning, speed and
peak-speed spread, when the peak comes, early against late speed, short-scale jitter). It reads no activity counts
(how much someone typed or moved), no keys and no text.

**What it says:** the probability that the window looks like the negative episodes (mostly confusion, some anger) that
observers marked in DUX. It labels a window `negative` from a probability of 0.25, about twice the rate of negative
windows in its training data; otherwise `neutral`. `confidence` is that probability, so a `negative` label typically
carries a confidence near 0.3.

## How good it is

Measured on people the model never saw (five folds, no person in two folds), 3,460 ten-second windows from 46 people:

| | |
| --- | --- |
| AUC, negative against neutral windows | 0.69 |
| AUC within the same form step | 0.58 |
| Models trained on shuffled labels matching it | 0 of 19 (p = 0.05) |
| Windows labelled `negative` | 10% |
| …of which annotated negative | 25% (13% by chance) |
| Annotated negative windows it finds | 19% |
| Calibration, predicted → observed, by fifth | 0.03 → 0.03, 0.06 → 0.07, 0.12 → 0.14, 0.18 → 0.19, 0.27 → 0.23 |

Most of its overall score comes from telling form steps apart: within the same step it drops to 0.58. It is weak.
When it says `negative` it is wrong about three times in four.

It does not carry over to everyday computer use. On the Freihaut and Göritz field study (174 people, 10,774 reports
of valence and arousal after 5 minutes of their own mouse use), the same model scores AUC 0.50 for valence below the
person's usual, 0.50 for arousal above it and 0.51 for valence below the midpoint. No model we trained on that study
did better than chance either: logistic regression and boosted trees, on the old pointer features, the new movement
features, both, and each with per-person baselines (AUC 0.48 to 0.51).

Architectures compared on DUX (held-out people, all features): logistic regression 0.71, boosted trees 0.70, random
forest 0.70, a small neural network 0.66 (and it never predicted the rare class). Logistic regression was best or tied
in every feature set and is the one that ships. Ten-second windows beat twenty (0.70 against 0.64).

## How to use it

- To choose which replays to watch and when to ask people how it is going: a stretch of `negative` labels is about
  twice as likely as chance to be a bad moment.
- Not to change what an assistant says on its own. `context()` prefers a site's own trained model, then the base
  model, then the mood reading; the server-side checks in the orion-context skill should leave `orion-base` out of
  prompts unless you have looked at its labels on your site.
- Train your own model (`orion train`): answers to the question are better labels for your site than DUX's.

## Rebuilding it

```sh
python scripts/base_model.py     # downloads DUX (320 MB, checksummed) once, prints the evaluation, writes src/base-model.js
```

`test/base-model.test.js` checks that the browser gives exactly scikit-learn's probabilities on real DUX windows
(`test/fixtures/base-model-windows.json`) and that live scoring gives the offline label. Replaying all 46 sessions
through the live labeller every 2 seconds (19,666 scores) gave the offline label every time.

## Data

- **DUX** (training): Bieber, C., Leppich, D., Harms, P. and Schubert, U. (2023). *DUX: A dataset of User Interactions
  and User Emotions.* Zenodo, doi:10.5281/zenodo.7778612. CC BY 4.0. 46 people filling in a travel expense web form
  with frustration triggers; observers annotated emotion episodes at three intensities. Orion turns its events into
  records the way the timing mask does: keys are paired, then forgotten.
- **Freihaut and Göritz field study** (evaluation only, not shipped): Freihaut, P. and Göritz, A. S. *Study data:
  longitudinal study about the relationship between computer mouse usage and emotional states.* Zenodo,
  doi:10.5281/zenodo.6559329. CC BY 4.0.

Other public datasets were checked and not used: the online stress experiment and keyboard field study by the same
authors are GPL or AGPL, SWELL-KW and EmoSurv are non-commercial, Banholzer et al. publish only aggregated features
without a licence, and the larger lab studies (Naegelin et al., Wampfler et al.) have not released their data.
