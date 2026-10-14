"""Timing and geometry only (plus key classes with the types mask). Label evidence and key identities never enter
this module. Mirrors src/features.js; test/parity.test.js checks the two against each other."""
import math
import statistics

# A press counts as overshooting when the pointer reached the control, left it by at least this much, and came back.
OVERSHOOT_PX = 2
APPROACH_MS = 1000
# Typing: a gap of a second or more between presses is a pause, not part of the rhythm.
PAUSE_MS = 1000
# Pointer movements: samples less than 100 ms apart belong to one movement; shorter or shorter-travelled ones are noise.
# Shapes read the pointer at most every 40 ms, so a 1000 Hz gaming mouse and a 60 Hz trackpad give comparable movements.
THIN_MS = 40
MOVE_GAP_MS = 100
MIN_MOVE_SAMPLES = 5
MIN_MOVE_PX = 20
STEP_MS = 100


# Plain left-to-right sums, as the browser adds them (Python's sum() compensates and can differ in the last bits).
def total(values):
    out = 0.0
    for v in values:
        out += v
    return out


def mean(values):
    return total(values) / len(values)


def sd(values):
    m = mean(values)
    return math.sqrt(total((v - m) ** 2 for v in values) / len(values))


def quartiles(prefix, values):
    """Quartiles by linear interpolation (numpy's default), with the same arithmetic as features.js. Fewer than 4: None."""
    s = sorted(values)

    def at(q):
        p = q * (len(s) - 1)
        lo, hi = math.floor(p), math.ceil(p)
        return s[lo] + (s[hi] - s[lo]) * (p - lo)
    ok = len(s) >= 4
    return {f"{prefix}_q1": at(0.25) if ok else None, f"{prefix}_median": at(0.5) if ok else None, f"{prefix}_q3": at(0.75) if ok else None}


def typing_features(events):
    """Typing rhythm: press-to-press and release-to-press intervals, hold times, and pauses. Timing only, no keys."""
    downs, ups, segment = [], {}, 0
    for row in events:
        if row["kind"] == "boundary":
            segment += 1
        elif row["kind"] == "keydown" and not row.get("repeat", False):
            downs.append((row["t"], row.get("press_id"), segment))
        elif row["kind"] == "keyup" and row.get("press_id") is not None and row["press_id"] not in ups:
            ups[row["press_id"]] = row["t"]
    dd, flights, holds, pauses = [], [], [], []
    for i, (t, press, seg) in enumerate(downs):
        up = ups.get(press) if press is not None else None
        if up is not None and up >= t:
            holds.append(up - t)
        if i + 1 == len(downs) or downs[i + 1][2] != seg:
            continue
        gap = downs[i + 1][0] - t
        if gap >= PAUSE_MS:
            pauses.append(gap)
            continue
        dd.append(gap)
        if up is not None and up >= t:
            flights.append(downs[i + 1][0] - up)
    out = {**quartiles("typing_dd_ms", dd), **quartiles("typing_flight_ms", flights), **quartiles("typing_hold_ms", holds)}
    paused, typed = total(pauses), total(dd)
    out.update(typing_pause_count=len(pauses), typing_pause_ms_mean=mean(pauses) if pauses else None,
               typing_pause_ms_sd=sd(pauses) if len(pauses) >= 2 else None,
               typing_pause_time_fraction=paused / (paused + typed) if paused + typed > 0 else None)
    return out


def movement(s, start):
    """One pointer movement's shape: directness, deviation, reversals, turning, speed profile and short-scale instability."""
    first, last = s[0], s[-1]
    dur = last["t"] - first["t"]
    cx, cy = last["x"] - first["x"], last["y"] - first["y"]
    chord, quarter = math.hypot(cx, cy), dur / 4
    path = peak = turning = early = late = 0.0
    peak_t, steady, sx, sy, prev, bins = first["t"], 0, 0, 0, None, {}
    for a, b in zip(s, s[1:]):
        dx, dy = b["x"] - a["x"], b["y"] - a["y"]
        d = math.hypot(dx, dy)
        path += d
        v = d * 1000 / (b["t"] - a["t"])
        if v > peak:
            peak, peak_t = v, b["t"]
        reversed_ = False  # A change in the sign of x or y movement, against the last direction that axis moved.
        if dx:
            g = 1 if dx > 0 else -1
            reversed_ = reversed_ or (sx != 0 and g != sx)
            sx = g
        if dy:
            g = 1 if dy > 0 else -1
            reversed_ = reversed_ or (sy != 0 and g != sy)
            sy = g
        steady += not reversed_
        if d > 0.5:
            if prev:
                turning += math.acos(max(-1.0, min(1.0, (dx * prev[0] + dy * prev[1]) / (d * prev[2]))))
            prev = (dx, dy, d)
        if b["t"] <= first["t"] + quarter:
            early += d
        if a["t"] >= first["t"] + 3 * quarter:
            late += d
        k = math.floor((b["t"] - start) / STEP_MS)
        bins[k] = bins.get(k, 0) + d
    if path < MIN_MOVE_PX or dur <= 0:
        return None
    deviation, crossings, side = 0, 0, 0
    for p in s:
        cross = (p["x"] - first["x"]) * cy - (p["y"] - first["y"]) * cx
        deviation = max(deviation, abs(cross) / chord if chord > 0 else math.hypot(p["x"] - first["x"], p["y"] - first["y"]))
        if chord > 0 and cross:
            g = 1 if cross > 0 else -1
            crossings += side != 0 and g != side
            side = g
    instability = [abs(bins[k + 1] - d) for k, d in bins.items() if k + 1 in bins]
    return {"efficiency": chord / path, "excess": path - chord, "deviation": deviation / max(chord, MIN_MOVE_PX), "crossings": crossings,
            "consistency": steady / (len(s) - 1), "turning": turning * 100 / path, "speed": path * 1000 / dur, "peak": peak,
            "peak_at": (peak_t - first["t"]) / dur, "late_early": late / early if early > 0 else None, "instability": instability}


def motion_features(events, context):
    """Pointer movement shape over the window, from movements cut at pauses, presses, scrolls and gaps."""
    moves, open_ = [], {}

    def close(pointer):
        s = open_.pop(pointer, None)
        m = movement(s, context["start_ms"]) if s and len(s) >= MIN_MOVE_SAMPLES else None
        if m:
            moves.append(m)
    for row in events:
        kind = row["kind"]
        if kind in ("boundary", "wheel", "scroll"):
            for pointer in list(open_):
                close(pointer)
        elif kind in ("pointerdown", "pointerup", "pointercancel"):
            close(row.get("pointer_id", 0))
        elif kind == "pointermove" and "x" in row and "y" in row:
            pointer = row.get("pointer_id", 0)
            s = open_.get(pointer)
            if s and row["t"] - s[-1]["t"] < THIN_MS:
                continue  # Also skips repeated or earlier timestamps.
            if s and row["t"] - s[-1]["t"] < MOVE_GAP_MS:
                s.append(row)
            else:
                close(pointer)
                open_[pointer] = [row]
    for pointer in list(open_):
        close(pointer)

    def values(key):
        return [m[key] for m in moves if m[key] is not None]

    def middle(key):
        v = values(key)
        return statistics.median(v) if v else None
    peaks, diffs = values("peak"), [d for m in moves for d in m["instability"]]
    out = {"motion_moves": len(moves), "motion_efficiency_median": middle("efficiency"), "motion_excess_px_median": middle("excess"),
           "motion_deviation_median": middle("deviation"), "motion_deviation_max": max(values("deviation")) if moves else None,
           "motion_crossings_mean": mean(values("crossings")) if moves else None, "motion_consistency_median": middle("consistency"),
           "motion_turning_median": middle("turning"), "motion_speed_median": middle("speed"), "motion_peak_speed_median": middle("peak"),
           "motion_peak_speed_cv": sd(peaks) / mean(peaks) if len(peaks) >= 2 and mean(peaks) > 0 else None,
           "motion_peak_at_median": middle("peak_at"), "motion_late_early_median": middle("late_early"),
           "motion_instability_mean": mean(diffs) if diffs else None, "motion_instability_peak": max(diffs) if diffs else None}
    # Speed and directional consistency matter together (Banholzer et al. 2021), which a linear model needs spelled out.
    out["motion_speed_x_consistency"] = out["motion_speed_median"] * out["motion_consistency_median"] if moves else None
    return out


def touch_features(events):
    """Touch and pen contact: contact area and genuine pressure (0 and 0.5 are what browsers report without a sensor)."""
    areas, pressures = [], []
    for row in events:
        if row["kind"] not in ("pointerdown", "pointermove") or "contact_w" not in row:
            continue
        areas.append(row["contact_w"] * row["contact_h"])
        if "pressure" in row and row["pressure"] not in (0, 0.5):
            pressures.append(row["pressure"])
    return {"touch_area_mean": mean(areas) if areas else None, "touch_area_max": max(areas) if areas else None,
            "touch_area_cv": sd(areas) / mean(areas) if len(areas) >= 2 and mean(areas) > 0 else None,
            "touch_pressure_mean": mean(pressures) if pressures else None,
            "touch_pressure_cv": sd(pressures) / mean(pressures) if len(pressures) >= 2 and mean(pressures) > 0 else None}


def summary(prefix, values):
    if not values:
        return {f"{prefix}_median": None, f"{prefix}_cv": None}
    mean = statistics.fmean(values)
    return {f"{prefix}_median": statistics.median(values),
            f"{prefix}_cv": statistics.pstdev(values) / mean if mean else None}


def outside(p, box):
    """Distance from a point to a box, zero inside it."""
    dx = max(box["box_x"] - p["x"], 0, p["x"] - (box["box_x"] + box["box_w"]))
    dy = max(box["box_y"] - p["y"], 0, p["y"] - (box["box_y"] + box["box_h"]))
    return math.hypot(dx, dy)


def extract_features(events, context, families):
    holds, intervals, movement_dt, speeds, scroll_dt, click_holds, click_dt, dwell, overshoots = [], [], [], [], [], [], [], [], []
    held, pointers, click_held, approach = {}, {}, {}, {}
    previous_down = previous_scroll = previous_click = focus_start = depth = None
    downs = unmatched = overlaps = classified = deletes = pastes = autofills = returns = aimed = 0
    path = hidden = 0.0
    for row in events:
        kind, t = row["kind"], row["t"]
        if kind == "boundary":
            held.clear(); pointers.clear(); click_held.clear(); approach.clear(); previous_down = previous_scroll = previous_click = focus_start = None
            continue
        if kind == "keydown" and not row.get("repeat", False):
            downs += 1
            if "key_type" in row:
                classified += 1
                deletes += row["key_type"] == "delete"
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
            if "x" in row and "y" in row:
                approach.setdefault(pointer, []).append(row)
        elif kind in ("wheel", "scroll"):
            approach.clear()  # The page moved under the pointer, so earlier positions no longer lead to the control.
            if kind == "wheel":
                if previous_scroll is not None and t >= previous_scroll:
                    scroll_dt.append(t - previous_scroll)
                previous_scroll = t
            elif row.get("content_height", 0) > 0 and "view_height" in row and "y" in row:
                depth = max(0 if depth is None else depth, min(1, (row["y"] + row["view_height"]) / row["content_height"]))
        elif kind == "pointerdown":
            if previous_click is not None and t >= previous_click:
                click_dt.append(t - previous_click)
            previous_click = t
            if "press_id" in row:
                click_held[row["press_id"]] = (t, row.get("pointer_id", 0))
            trail = [p for p in approach.pop(row.get("pointer_id", 0), []) if p["t"] >= t - APPROACH_MS]
            if "box_w" in row and trail:
                aimed += 1
                reached, past = False, 0
                for p in trail:
                    d = outside(p, row)
                    if d == 0:
                        reached = True
                    elif reached:
                        past = max(past, d)
                if past >= OVERSHOOT_PX:
                    overshoots.append(past)
        elif kind == "pointerup":
            down = click_held.pop(row.get("press_id"), None)
            if down and t >= down[0]:
                click_holds.append(t - down[0])
            approach.pop(row.get("pointer_id", 0), None)
        elif kind == "pointercancel":
            click_held = {k:v for k,v in click_held.items() if v[1] != row.get("pointer_id", 0)}
            approach.pop(row.get("pointer_id", 0), None)
        elif kind == "input":
            if row.get("via") == "fill":
                autofills += 1
            else:
                pastes += 1
        elif kind == "visible":
            returns += 1
            hidden += min(row["hidden_ms"], t - context["start_ms"])  # Only the time away inside this window.
        elif kind == "focusin":
            focus_start = t
        elif kind == "focusout":
            if focus_start is not None and t >= focus_start:
                dwell.append(t - focus_start)
            focus_start = None
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
    if "focus" in families:
        result.update(summary("focus_dwell_ms", dwell))
    if "volume" in families:
        result.update(key_down_count=downs, pointer_path_px=path, event_count=len(events), duration_ms=context["end_ms"] - context["start_ms"])
    if "fills" in families:
        result.update(paste_count=pastes, autofill_count=autofills)
    if "visibility" in families:
        result.update(hidden_count=returns, hidden_ms=hidden)
    if "depth" in families:
        result["scroll_depth_max"] = depth
    if "overshoot" in families:
        result["overshoot_fraction"] = len(overshoots) / aimed if aimed else None
        result["overshoot_px_median"] = statistics.median(overshoots) if overshoots else None
        result["overshoot_px_sd"] = sd(overshoots) if len(overshoots) >= 2 else None
    if "typing" in families:
        result.update(typing_features(events))
    if "motion" in families:
        result.update(motion_features(events, context))
    if "touch" in families:
        result.update(touch_features(events))
    if "corrections" in families:
        result["correction_rate"] = deletes / classified if classified else None  # Needs key classes (mask 'types').
    quality = {"matched_key_pairs": len(holds), "unmatched_key_ups": unmatched, "open_key_downs": len(held),
               "matched_pointer_pairs": len(click_holds),
               "movement_intervals": len(movement_dt), "measured_median_hz": 1000 / statistics.median(movement_dt) if movement_dt else None}
    return result, quality
