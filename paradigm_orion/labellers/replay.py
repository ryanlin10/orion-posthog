"""AI labeller that reads a session's replay activity and labels fixed windows of its telemetry.

The model sees the session as PostHog recorded it around the input: page views, clicks with the clicked
element's text, rage and dead clicks, errors, survey answers and custom events, with times. It never sees
Orion's own labels. The model reads clicks that the classifier also measures, so a classifier trained only on
these labels may relearn click patterns; prefer text or human labels for validation sets.
"""
from collections import defaultdict

from ..labels import coerce, describe_target, make_label
from ..llm import LLMSetupError, parse_json
from ..posthog import epoch_ms
from ..store import Clock, recorders_by_session

VERSION = "replay-v1"
MAX_LINES = 400
MAX_WINDOWS = 60
HIDDEN = {"orion_label", "orion_session_start"}

SYSTEM = """You review a recorded product session and judge how the person felt during given time windows, \
for training a classifier on their input timing. You see what happened on screen as events, not their face or words.
- Use only evidence in the timeline: repeated clicks on the same thing, errors, going back and forth, abandoning a step, smooth progress.
- Quiet periods with no events are not evidence of calm or of frustration; return null when a window shows too little.
Reply with JSON only."""


def describe(event, start):
    t = (epoch_ms(event["timestamp"]) - start) / 1000
    stamp = f"{int(t // 60):02d}:{t % 60:04.1f}"
    d = event.get("detail") or {}
    name = event["event"]
    if name == "$pageview":
        return f"{stamp} opened page {event.get('pathname') or ''}"
    if name == "$pageleave":
        return f"{stamp} left page {event.get('pathname') or ''}"
    if name == "$autocapture":
        what = d.get("text") or d.get("tag") or "an element"
        return f"{stamp} {d.get('type') or 'interacted'} on {d.get('tag') or 'element'} \"{what}\""
    if name == "$rageclick":
        return f"{stamp} RAGE CLICK on \"{d.get('text') or d.get('tag') or 'an element'}\""
    if name == "$dead_click":
        return f"{stamp} click with no response on \"{d.get('text') or d.get('tag') or 'an element'}\""
    if name == "$exception":
        return f"{stamp} error: {d.get('message', '')}"
    if name == "survey sent":
        return f"{stamp} survey answer: {d}"
    return f"{stamp} {name} {d or ''}".rstrip()


def session_windows(recordings, length, stride):
    """Windows of `length` every `stride` over each recorder's captured spans in the session."""
    out = []
    for r in recordings:
        if not r["spans"] or not r.get("anchors"):
            continue
        clock = Clock(r)
        spans = [(s[0], s[1]) for s in r["spans"]]
        t, last = spans[0][0], max(b for _, b in spans)
        while t < last:
            a, b = t, t + length
            if any(sa < b and a <= sb for sa, sb in spans):
                out.append({"recorder": r, "start_ms": a, "end_ms": b, "epoch_start": clock.to_epoch(a), "epoch_end": clock.to_epoch(b)})
            t += stride
    out.sort(key=lambda w: w["epoch_start"])
    return out


def prompt_for(recipe, lines, windows, start, truncated):
    target = recipe["target"]
    guide = recipe["labels"]["model"]["guide"].strip()
    spans = [f"[{i}] {(w['epoch_start'] - start) / 1000:.1f}s to {(w['epoch_end'] - start) / 1000:.1f}s" for i, w in enumerate(windows)]
    return (f"Judge {describe_target(target)} for each window.\n"
            + (f"Definitions from the team:\n{guide}\n" if guide else "")
            + "\nSession timeline (minutes:seconds from the start):\n" + "\n".join(lines)
            + ("\n(timeline truncated)" if truncated else "")
            + "\n\nWindows (seconds from the start):\n" + "\n".join(spans)
            + f'\n\nReturn a JSON array with one object per window: {{"window": <number>, "{target["name"]}": <value or null>, '
              '"confidence": <0 to 1>, "reason": "<a few words>"}.')


def label_replay(store, recipe, llm, log=print):
    target = recipe["target"]
    length, stride = recipe["window"]["length_ms"], recipe["window"]["stride_ms"]
    by_session = recorders_by_session(store.recordings())
    events = defaultdict(list)
    for row in store.iter_context():
        if row["event"] not in HIDDEN and row.get("session_id") in by_session:
            events[row["session_id"]].append(row)
    labels, stats = [], {"sessions": 0, "windows": 0, "labelled": 0, "no_answer": 0, "no_events": 0}
    sessions = [s for s in by_session if events.get(s)]
    stats["no_events"] = len(by_session) - len(sessions)
    for session_id in sessions[: recipe["labels"]["model"]["max_sessions"]]:
        timeline = sorted(events[session_id], key=lambda e: e["timestamp"])
        windows = session_windows(by_session[session_id], length, stride)
        if not windows:
            continue
        stats["sessions"] += 1
        start = min(epoch_ms(timeline[0]["timestamp"]), windows[0]["epoch_start"])
        lines = [describe(e, start) for e in timeline]
        truncated = len(lines) > MAX_LINES
        lines = lines[:MAX_LINES]
        for offset in range(0, len(windows), MAX_WINDOWS):
            chunk = windows[offset:offset + MAX_WINDOWS]
            stats["windows"] += len(chunk)
            try:
                reply = parse_json(llm.complete(SYSTEM, prompt_for(recipe, lines, chunk, start, truncated)))
            except LLMSetupError:
                raise
            except Exception as error:
                log(f"  session {session_id}: model reply unusable ({error})")
                continue
            answered = {e["window"]: e for e in reply if isinstance(e, dict) and isinstance(e.get("window"), int)} if isinstance(reply, list) else {}
            for i, w in enumerate(chunk):
                entry = answered.get(i) or {}
                value = coerce(target, entry.get(target["name"]))
                if value is None:
                    stats["no_answer"] += 1
                    continue
                labels.append(make_label(recipe, "replay", w["recorder"]["recorder_id"], w["start_ms"], w["end_ms"], value,
                                         entry.get("confidence", 0.5), f"{VERSION}:{llm.model}", session_id=session_id,
                                         reason=str(entry.get("reason", ""))[:200]))
                stats["labelled"] += 1
    return labels, stats
