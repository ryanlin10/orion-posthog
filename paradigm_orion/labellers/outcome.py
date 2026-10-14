"""Outcome labels: what a session went on to do, from goal events the site already sends to PostHog.

The recipe maps target classes to goal events (event names, or page paths for page views). The first goal event in a
session labels the windows before it with its class; sessions without one get the `otherwise` class once they have
ended. Windows are the recipe's length, step back from `skip` before the event for up to `within`, and keep the
`windows` most recent that hold telemetry. Orion records no goal events itself: pull them with `orion pull --recipe`.
"""
from ..labels import make_label
from ..posthog import epoch_ms
from ..store import Clock, recorders_by_session

VERSION = "outcome-1"
ENDED_MS = 3600000  # A session without a goal counts as finished an hour after its last telemetry.


def goal_events(recipe):
    """The PostHog event names a recipe's outcome labels read (page paths need $pageview)."""
    names = {n for events in recipe["labels"]["outcome"]["events"].values() for n in events}
    return sorted({"$pageview" if n.startswith("/") else n for n in names})


def goal_class(goals, row):
    for cls, names in goals.items():
        for name in names:
            if row["event"] == name or (name.startswith("/") and row["event"] == "$pageview" and (row.get("pathname") or "").startswith(name)):
                return cls, name
    return None


def label_outcome(store, recipe):
    cfg = recipe["labels"]["outcome"]
    length = recipe["window"]["length_ms"]
    recordings = store.recordings()
    stats = {"sessions": 0, "goal_sessions": 0, "otherwise_sessions": 0, "unfinished": 0, "no_telemetry": 0, "labelled": 0}
    if not recordings:
        return [], stats
    first = {}  # session → (epoch, class, goal)
    for row in store.iter_context(set(goal_events(recipe))):
        match = goal_class(cfg["events"], row)
        if match and row.get("session_id"):
            t = epoch_ms(row["timestamp"])
            if row["session_id"] not in first or t < first[row["session_id"]][0]:
                first[row["session_id"]] = (t, *match)
    newest = max(epoch_ms(r["last_ts"]) for r in recordings)
    labels = []
    for session, recorders in sorted(recorders_by_session(recordings).items()):
        stats["sessions"] += 1
        if session in first:
            end, cls, goal = first[session]
            stats["goal_sessions"] += 1
        elif cfg["otherwise"] is not None:
            end, cls, goal = max(epoch_ms(r["last_ts"]) for r in recorders), cfg["otherwise"], None
            if newest - end < ENDED_MS:
                stats["unfinished"] += 1
                continue
            stats["otherwise_sessions"] += 1
        else:
            continue
        candidates = []
        for r in recorders:
            if not r.get("anchors") or not r.get("spans"):
                continue
            stop = Clock(r).to_recorder(end) - cfg["skip_ms"]
            for k in range(max(0, int((cfg["within_ms"] - cfg["skip_ms"]) // length))):
                a, b = stop - (k + 1) * length, stop - k * length
                if any(start < b and finish > a for start, finish, *_ in r["spans"]):
                    candidates.append((k, r["recorder_id"], a, b))
        if not candidates:
            stats["no_telemetry"] += 1
            continue
        for _, rid, a, b in sorted(candidates)[:cfg["windows"]]:
            labels.append(make_label(recipe, "outcome", rid, a, b, cls, 1.0, VERSION, source="outcome", session_id=session, goal=goal))
            stats["labelled"] += 1
    return labels, stats
