"""Example trusted recipe hook. No labels, key identities, or cross-window mutable state."""


def extract(events, context):
    down_times = [e["t"] for e in events if e["kind"] == "pointerdown"]
    gaps = [b - a for a, b in zip(down_times, down_times[1:])]
    return {"custom_click_interval_max_ms": max(gaps) if gaps else None}
