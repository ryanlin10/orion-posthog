"""AI labeller that reads what users wrote (chat or assistant messages) and labels the telemetry around each one.

Messages come from a PostHog event that carries the text (by default PostHog LLM analytics' `$ai_generation`
with `$ai_input`) or from a JSONL file of {session_id, timestamp, text}. The model sees one session's messages
together, so it can judge each against how this person usually writes. The window around each message follows
the recipe (past: while composing), clipped so that neighbouring messages never share telemetry.
"""
from collections import defaultdict

from ..labels import coerce, describe_target, make_label
from ..llm import LLMSetupError, parse_json
from ..posthog import epoch_ms
from ..store import recorder_at, recorders_by_session
from ..workspace import read_jsonl

VERSION = "text-v1"
CHUNK = 30
MAX_CHARS = 1500

SYSTEM = """You label how a person felt at the moment they wrote each message, for training a classifier on their \
input timing. You only see their words; judge the person's state, not the topic.
- Compare each message with how this same person writes elsewhere in the conversation; a change from their own baseline matters more than tone in general.
- A calm bug report, a correction or a terse instruction is not frustration by itself.
- When the words give no real evidence, say so with null instead of guessing.
Reply with JSON only."""


def message_text(value):
    """Text from a plain string, or the last user turn of a chat message list."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for message in reversed(value):
            if isinstance(message, dict) and message.get("role", "user") == "user":
                content = message.get("content")
                if isinstance(content, list):
                    content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
                if isinstance(content, str) and content.strip():
                    return content
    return None


def collect_messages(store, recipe):
    cfg = recipe["labels"]["model"]["text"]
    messages = []
    if cfg.get("file"):
        for row in read_jsonl(cfg["file"]):
            if row.get("session_id") and row.get("text") and row.get("timestamp") is not None:
                messages.append({"session_id": row["session_id"], "epoch_ms": epoch_ms(row["timestamp"]), "text": str(row["text"])})
    else:
        for row in store.iter_context(events={cfg["event"]}):
            props = row.get("properties") or {}
            text = message_text(props.get(cfg["property"]))
            session = props.get("$session_id") or row.get("session_id")
            if not text or not session:
                continue
            t = epoch_ms(row["timestamp"])
            latency = props.get("$ai_latency")
            if row["event"] == "$ai_generation" and isinstance(latency, (int, float)):
                t -= latency * 1000  # Generation events are captured after the reply; the user sent it earlier.
            messages.append({"session_id": session, "epoch_ms": t, "text": text})
    by_session = defaultdict(list)
    for m in messages:
        by_session[m["session_id"]].append(m)
    for items in by_session.values():
        items.sort(key=lambda m: m["epoch_ms"])
    return by_session


def prompt_for(recipe, items):
    target = recipe["target"]
    start = items[0]["epoch_ms"]
    lines = []
    for i, m in enumerate(items):
        text = m["text"].strip().replace("\n", " ")
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + " …"
        lines.append(f"[{i}] +{(m['epoch_ms'] - start) / 1000:.0f}s: {text}")
    guide = recipe["labels"]["model"]["guide"].strip()
    return (f"Label each message for {describe_target(target)}.\n"
            + (f"Definitions from the team:\n{guide}\n" if guide else "")
            + "\nMessages from one session, oldest first:\n" + "\n".join(lines)
            + f'\n\nReturn a JSON array with one object per message: {{"message": <number>, "{target["name"]}": <value or null>, '
              '"confidence": <0 to 1>, "reason": "<a few words>"}.')


def window(align, t, length, previous_t, next_t):
    if align == "past":
        a, b = t - length, t
    elif align == "future":
        a, b = t, t + length
    else:
        a, b = t - length / 2, t + length / 2
    if previous_t is not None:
        a = max(a, previous_t)
    if next_t is not None:
        b = min(b, next_t)
    return a, b


def label_text(store, recipe, llm, log=print):
    target, length, align = recipe["target"], recipe["window"]["length_ms"], recipe["window"]["align"]
    sessions = collect_messages(store, recipe)
    recorders = recorders_by_session(store.recordings())
    labels, stats = [], {"sessions": 0, "messages": 0, "labelled": 0, "no_telemetry": 0, "no_answer": 0, "too_short": 0}
    for session_id, items in list(sessions.items())[: recipe["labels"]["model"]["max_sessions"]]:
        candidates = recorders.get(session_id, [])
        if not candidates:
            stats["no_telemetry"] += len(items)
            continue
        stats["sessions"] += 1
        answers = {}
        for offset in range(0, len(items), CHUNK):
            chunk = items[offset:offset + CHUNK]
            try:
                reply = parse_json(llm.complete(SYSTEM, prompt_for(recipe, chunk)))
            except LLMSetupError:
                raise
            except Exception as error:  # One bad reply should not stop the run.
                log(f"  session {session_id}: model reply unusable ({error})")
                continue
            for entry in reply if isinstance(reply, list) else []:
                if isinstance(entry, dict) and isinstance(entry.get("message"), int) and 0 <= entry["message"] < len(chunk):
                    answers[offset + entry["message"]] = entry
        placed = []
        for i, m in enumerate(items):
            stats["messages"] += 1
            found, distance = recorder_at(candidates, m["epoch_ms"])
            if not found or distance > length:
                stats["no_telemetry"] += 1
                continue
            placed.append((i, found[0], found[1]))
        for k, (i, rec, t) in enumerate(placed):
            entry = answers.get(i) or {}
            value = coerce(target, entry.get(target["name"]))
            if value is None:
                stats["no_answer"] += 1
                continue
            prev_t = next((p[2] for p in reversed(placed[:k]) if p[1] is rec), None)
            next_t = next((p[2] for p in placed[k + 1:] if p[1] is rec), None)
            a, b = window(align, t, length, prev_t, next_t)
            if b - a < 1000:
                stats["too_short"] += 1
                continue
            labels.append(make_label(recipe, "text", rec["recorder_id"], a, b, value, entry.get("confidence", 0.5),
                                     f"{VERSION}:{llm.model}", session_id=session_id, reason=str(entry.get("reason", ""))[:200]))
            stats["labelled"] += 1
    return labels, stats

