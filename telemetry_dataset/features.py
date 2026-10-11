"""Timing and geometry only. Label evidence and key identities never enter this module."""
import math
import statistics


def summary(prefix, values):
    if not values:
        return {f"{prefix}_median": None, f"{prefix}_cv": None}
    mean = statistics.fmean(values)
    return {f"{prefix}_median": statistics.median(values),
            f"{prefix}_cv": statistics.pstdev(values) / mean if mean else None}


def extract_features(events, context, families):
    holds, intervals, movement_dt, speeds, scroll_dt, click_holds, click_dt = [], [], [], [], [], [], []
    held, pointers, click_held = {}, {}, {}
    previous_down = previous_scroll = previous_click = None
    downs = unmatched = overlaps = 0
    path = 0.0
    for row in events:
        kind, t = row["kind"], row["t"]
        if kind == "boundary":
            held.clear(); pointers.clear(); click_held.clear(); previous_down = previous_scroll = previous_click = None
            continue
        if kind == "keydown" and not row.get("repeat", False):
            downs += 1
            if previous_down is not None and t >= previous_down:
                intervals.append(t - previous_down)
            previous_down = t
            overlaps += bool(held)
            if row.get("press_id") is not None:
                held[row["press_id"]] = t
        elif kind == "keyup":
            start = held.pop(row.get("press_id"), None)
            if start is None or t < start:
                unmatched += 1
            else:
                holds.append(t - start)
        elif kind == "pointermove":
            pointer = row.get("pointer_id", 0)
            previous = pointers.get(pointer)
            if previous and t > previous["t"]:
                dt = t - previous["t"]
                # Long gaps are unknown paths; do not bridge idle periods.
                if dt < 100 and all(k in row and k in previous for k in ("x", "y")):
                    distance = math.hypot(row["x"] - previous["x"], row["y"] - previous["y"])
                    movement_dt.append(dt); speeds.append(distance * 1000 / dt); path += distance
            pointers[pointer] = row
        elif kind == "wheel":
            if previous_scroll is not None and t >= previous_scroll:
                scroll_dt.append(t - previous_scroll)
            previous_scroll = t
        elif kind == "pointerdown":
            if previous_click is not None and t >= previous_click:
                click_dt.append(t - previous_click)
            previous_click = t
            if "press_id" in row:
                click_held[row["press_id"]] = (t, row.get("pointer_id", 0))
        elif kind == "pointerup":
            down = click_held.pop(row.get("press_id"), None)
            if down and t >= down[0]:
                click_holds.append(t - down[0])
        elif kind == "pointercancel":
            click_held = {k:v for k,v in click_held.items() if v[1] != row.get("pointer_id", 0)}
    result = {}
    if "keyboard" in families:
        result.update(summary("key_hold_ms", holds))
        result.update(summary("key_interval_ms", intervals))
        result["key_overlap_fraction"] = overlaps / downs if downs else None
        result["key_pause_fraction"] = sum(x >= 1000 for x in intervals) / len(intervals) if intervals else None
    if "pointer" in families:
        result.update(summary("pointer_interval_ms", movement_dt))
        result.update(summary("pointer_speed_px_s", speeds))
        result.update(summary("click_hold_ms", click_holds))
        result.update(summary("click_interval_ms", click_dt))
    if "scroll" in families:
        result.update(summary("scroll_interval_ms", scroll_dt))
    if "volume" in families:
        result.update(key_down_count=downs, pointer_path_px=path, event_count=len(events), duration_ms=context["end_ms"] - context["start_ms"])
    quality = {"matched_key_pairs": len(holds), "unmatched_key_ups": unmatched, "open_key_downs": len(held),
               "matched_pointer_pairs": len(click_holds),
               "movement_intervals": len(movement_dt), "measured_median_hz": 1000 / statistics.median(movement_dt) if movement_dt else None}
    return result, quality
