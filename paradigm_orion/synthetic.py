"""Generated PostHog exports with a planted state, for trying Orion without a live project and for tests.

Each generated person has their own typing and pointer speed. Each session is a checkout page visit split
into segments that are either calm or frustrated. Frustrated segments have quicker, jerkier pointer movement,
bursts of repeated clicks and shorter key holds; they also produce rage clicks and annoyed chat messages.
The signal is deliberately moderate and varies by person: it exercises the pipeline, not a claim about people.
"""
import gzip
import json
import math
import random
import uuid
from datetime import datetime, timedelta, timezone

CALM_TEXT = ["Can you show me the delivery options?", "What does express shipping cost?", "Thanks, that helps.",
             "Is there a size guide for this?", "Can I pay with Apple Pay?", "ok great, adding the blue one"]
ANGRY_TEXT = ["this isn't working, the coupon won't apply", "why does it keep rejecting my card again",
              "I've clicked apply five times, nothing happens", "seriously, the total didn't change", "this is broken"]


def _uuid(rng):
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _segment(rng, t, length, state, person, records, press):
    """Append one segment's input records from relative time t; returns the next press id."""
    angry = state == "frustrated"
    end = t + length
    x, y = rng.uniform(200, 1000), rng.uniform(150, 650)
    while t < end:
        roll = rng.random()
        if roll < 0.45:  # A movement burst sampled at about 120 Hz.
            speed = (720 if angry else 300) * person["pointer"] * rng.uniform(0.6, 1.4)
            heading = rng.uniform(0, 2 * math.pi)
            for _ in range(rng.randint(20, 90)):
                if angry and rng.random() < 0.3:
                    heading += rng.uniform(-1.5, 1.5)
                step = speed / 120
                x = min(1400, max(0, x + math.cos(heading) * step)); y = min(880, max(0, y + math.sin(heading) * step))
                t += rng.uniform(7.6, 9.1)
                records.append({"kind": "pointermove", "t": round(t, 3), "x": round(x, 2), "y": round(y, 2), "pointer_id": 1,
                                "pointer_type": "mouse", "trusted": True, "target": 2, "coalesced": True})
        elif roll < 0.62:  # Clicks: frustrated people click the same thing in quick bursts.
            for _ in range(rng.randint(3, 5) if angry and rng.random() < 0.6 else 1):
                hold = rng.gauss(70 if angry else 105, 15) * person["keys"]
                records.append({"kind": "pointerdown", "t": round(t, 3), "x": x, "y": y, "pointer_id": 1, "button": 0, "trusted": True, "target": 3})
                records.append({"kind": "pointerup", "t": round(t + max(30, hold), 3), "x": x, "y": y, "pointer_id": 1, "button": 0, "trusted": True, "target": 3})
                records.append({"kind": "click", "t": round(t + max(30, hold) + 1, 3), "x": x, "y": y, "pointer_id": 1, "button": 0, "trusted": True, "target": 3})
                t += rng.uniform(180, 320) if angry else rng.uniform(1500, 3500)
        elif roll < 0.82:  # Typing a few words.
            for _ in range(rng.randint(5, 25)):
                hold = max(35, rng.gauss(78 if angry else 98, 18) * person["keys"])
                records.append({"kind": "keydown", "t": round(t, 3), "repeat": False, "press_id": press, "trusted": True, "target": 5})
                records.append({"kind": "keyup", "t": round(t + hold, 3), "repeat": False, "press_id": press, "trusted": True, "target": 5})
                press += 1
                t += max(40, rng.gauss(150 if angry else 190, 50) * person["keys"])
        else:  # Scrolling.
            for _ in range(rng.randint(3, 12)):
                records.append({"kind": "wheel", "t": round(t, 3), "dx": 0, "dy": rng.choice([-1, 1]) * rng.uniform(20, 120), "dz": 0, "mode": 0, "trusted": True, "target": 4})
                t += rng.uniform(15, 40) if angry else rng.uniform(25, 70)
        t += rng.uniform(150, 900)
    records.sort(key=lambda r: r["t"])
    return press


def generate(people=24, sessions_per_person=3, seed=0, start=None, message_noise=0.1):
    """Returns (export rows as PostHog would export them, ground truth segments)."""
    rng = random.Random(seed)
    start = start or (datetime.now(timezone.utc) - timedelta(days=10)).replace(microsecond=0)
    rows, truth = [], []
    for p in range(people):
        person = {"id": f"person-{p:03d}", "pointer": rng.uniform(0.7, 1.4), "keys": rng.uniform(0.75, 1.3)}
        for s in range(sessions_per_person):
            session_id = f"ses-{p:03d}-{s}"
            recorder = _uuid(rng)
            begin = start + timedelta(days=rng.uniform(0, 9), hours=rng.uniform(8, 20))
            origin = begin.timestamp() * 1000 - 500
            page = rng.choice(["/checkout", "/cart", "/checkout/payment"])
            rows.append({"uuid": _uuid(rng), "event": "$pageview", "timestamp": (origin + 400) / 1000, "distinct_id": person["id"], "person_id": person["id"],
                         "properties": {"$session_id": session_id, "$pathname": page}, "elements_chain": ""})
            records, press, t = [], 1, 1000.0
            for _ in range(rng.randint(4, 7)):
                state = "frustrated" if rng.random() < 0.4 else "calm"
                length = rng.uniform(20000, 40000)
                first = len(records)
                press = _segment(rng, t, length, state, person, records, press)
                truth.append({"session_id": session_id, "recorder_id": recorder, "start_ms": t, "end_ms": t + length, "state": state})
                seg = records[first:]
                for r in seg:
                    if r["kind"] == "click" and rng.random() < 0.5:
                        rows.append({"uuid": _uuid(rng), "event": "$autocapture", "timestamp": (origin + r["t"]) / 1000, "distinct_id": person["id"],
                                     "person_id": person["id"], "properties": {"$session_id": session_id, "$pathname": page, "$event_type": "click"},
                                     "elements_chain": 'button.btn:nth-child="2"nth-of-type="1"text="Apply coupon"'})
                if state == "frustrated" and seg:
                    rt = seg[len(seg) // 2]["t"]
                    rows.append({"uuid": _uuid(rng), "event": "$rageclick", "timestamp": (origin + rt) / 1000, "distinct_id": person["id"], "person_id": person["id"],
                                 "properties": {"$session_id": session_id, "$pathname": page}, "elements_chain": 'button.btn:text="Apply coupon"'})
                # The person writes to the shop's assistant at the end of most segments.
                if rng.random() < 0.85:
                    angry = (state == "frustrated") != (rng.random() < message_noise)
                    text = rng.choice(ANGRY_TEXT if angry else CALM_TEXT)
                    sent = origin + t + length - rng.uniform(200, 1500)
                    rows.append({"uuid": _uuid(rng), "event": "$ai_generation", "timestamp": (sent + 1300) / 1000, "distinct_id": person["id"],
                                 "person_id": person["id"], "properties": {"$session_id": session_id, "$pathname": page, "$ai_latency": 1.3,
                                                                           "$ai_input": [{"role": "user", "content": text}], "$ai_model": "assistant"}})
                t += length + rng.uniform(300, 2000)
            rows.extend(_batches(rng, records, recorder, session_id, page, person, origin))
    for r in rows:
        r["timestamp"] = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).isoformat()
    rows.sort(key=lambda r: r["timestamp"])
    return rows, truth


def _batches(rng, records, recorder, session_id, page, person, origin, size=400):
    out = []
    for seq, i in enumerate(range(0, len(records), size)):
        chunk = records[i:i + size]
        end = chunk[-1]["t"]
        out.append({"uuid": _uuid(rng), "event": "orion_input", "timestamp": (origin + end + 50) / 1000, "distinct_id": person["id"], "person_id": person["id"],
                    "properties": {"schema_version": "1.3", "recorder_id": recorder, "burst_id": f"{recorder}:{seq}", "chunk_index": 0, "batch_sequence": seq,
                                   "reason": "limit", "time_origin_ms": origin, "start_ms": chunk[0]["t"], "end_ms": end,
                                   "clock": {"epoch_ms": origin + end + 50, "perf_ms": end + 50}, "$session_id": session_id, "$pathname": page,
                                   "capture": {"movement_source": "pointerrawupdate", "keyboard": "timing", "target_hz": 120, "synthetic_allowed": False},
                                   "quality": {"record_count": len(chunk), "dropped_before_batch": 0, "excluded_events_total": 0, "sdk_refused_batches": 0},
                                   "records": chunk}})
    return out


def write_export(rows, path):
    """Rows as a gzip JSON Lines file in PostHog's export layout (properties as objects)."""
    with gzip.open(path, "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
