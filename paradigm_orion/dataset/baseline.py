"""Per-person baselines: how this browser usually behaves, from its earlier page loads. Mirrors src/baseline.js.

Each page load is cut into tiles of the window length, counted from the page's time origin. A tile closes once
input arrives at least `grace_ms` after it ends (the browser closes it at that moment), and a closed tile that passes
the window rules adds its features to running means. A page load is scored against the means from the page loads
before it: `<feature>_rel` is the window's value minus that mean, once the mean rests on `min_tiles` tiles.
"""
import bisect
import math

from .features import extract_features

INPUTLESS = ("boundary", "visible")
AWAY = ("hidden", "blur", "pagehide")  # The person left; nothing was lost.


def blocking(reason, rules):
    """Whether a gap with this reason rejects a window under the recipe's quality rules."""
    return not rules["allow_gaps"] and not (rules["allow_away"] and reason in AWAY)


def closed_tiles(events, gaps, config, rules, families):
    """Features of each closed tile that passes the window rules, oldest first.
    events: one recorder's normalized rows sorted by (t, ordinal); gaps: (start, end, reason)."""
    inputs = [e["t"] for e in events if e["kind"] not in INPUTLESS]
    if not inputs:
        return []
    size = config["tile_ms"]
    limit = math.floor((max(inputs) - config["grace_ms"]) / size)
    times = [e["t"] for e in events]
    out = []
    for k in sorted({math.floor(t / size) for t in times if math.floor(t / size) < limit}):
        a, b = k * size, (k + 1) * size
        inside = events[bisect.bisect_left(times, a):bisect.bisect_left(times, b)]
        if len(inside) > rules["max_events"] or sum(e["kind"] != "boundary" for e in inside) < rules["min_events"]:
            continue
        if any(start < b and end >= a and blocking(reason, rules) for start, end, reason in gaps):
            continue
        out.append(extract_features(inside, {"start_ms": a, "end_ms": b}, families)[0])
    return out


def merge(stored, tiles, cap):
    """Running means {feature: [mean, tiles]} after adding these tiles' features."""
    sums = {}
    for features in tiles:
        for name, value in features.items():
            if value is not None:
                s = sums.setdefault(name, [0.0, 0])
                s[0] += value
                s[1] += 1
    out = dict(stored)
    for name, (total, count) in sums.items():
        mean, n = stored.get(name, (0.0, 0))
        out[name] = [(mean * n + total) / (n + count), min(n + count, cap)]
    return out


def relative(features, stored, min_tiles):
    """`<feature>_rel`: this window against the person's usual value, or None without enough history."""
    out = {}
    for name, value in features.items():
        base = stored.get(name)
        out[f"{name}_rel"] = value - base[0] if value is not None and base is not None and base[1] >= min_tiles else None
    return out
