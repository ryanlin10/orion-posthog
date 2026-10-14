"""Local copy of a project's Orion telemetry and the surrounding events the labellers read.

.orion/data/telemetry/*.jsonl  orion_input events (the dataset builder's input)
.orion/data/context/*.jsonl    page views, clicks, rage/dead clicks, errors, survey answers, self-reports, labels, text events
.orion/data/recordings.jsonl   one row per recorder: session, person, day, pages, spans and clock anchors
"""
import bisect
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .posthog import read_export, week_chunks
from .workspace import read_jsonl, write_jsonl

TELEMETRY = ("orion_input", "behavioral_burst")
CONTEXT_EVENTS = ("$pageview", "$pageleave", "$autocapture", "$rageclick", "$dead_click", "$exception", "survey sent", "orion_label", "orion_session_start",
                  "orion_self_report")
SELF_REPORT_FIELDS = ("answer", "dismissed", "question", "options", "positive", "negative", "recorder_id", "shown_t", "answered_t",
                      "mood", "ranked_by", "trigger")


def element_text(chain):
    """The innermost element's tag and visible text from an autocapture elements_chain."""
    if not chain:
        return None, None
    first = chain.split(";")[0]
    tag = re.match(r"([a-z0-9-]+)", first)
    text = re.search(r'text="((?:[^"\\]|\\.)*)"', first) or re.search(r'text="((?:[^"\\]|\\.)*)"', chain)
    return (tag.group(1) if tag else None), (text.group(1)[:120] if text else None)


def slim_context(row, text_events):
    props = row["properties"]
    out = {"uuid": row.get("uuid"), "event": row["event"], "timestamp": row["timestamp"], "distinct_id": row.get("distinct_id"),
           "person_id": row.get("person_id"), "session_id": props.get("$session_id"), "pathname": props.get("$pathname")}
    event = row["event"]
    if event in text_events:
        out["properties"] = props  # The text labeller needs the configured property, e.g. $ai_input.
    elif event in ("$autocapture", "$rageclick", "$dead_click"):
        tag, text = element_text(row.get("elements_chain") or props.get("$elements_chain"))
        out["detail"] = {"type": props.get("$event_type"), "tag": tag, "text": text or props.get("$el_text")}
    elif event == "$exception":
        items = props.get("$exception_list") or []
        first = items[0] if items and isinstance(items[0], dict) else {}
        out["detail"] = {"message": str(props.get("$exception_message") or first.get("value") or first.get("type") or "")[:200]}
    elif event == "survey sent":
        out["detail"] = {k: v for k, v in props.items() if k.startswith("$survey_response") or k == "$survey_name"}
    elif event == "orion_self_report":
        out["detail"] = {k: props.get(k) for k in SELF_REPORT_FIELDS}
    elif event == "orion_label":
        out["detail"] = {k: props.get(k) for k in ("model", "model_version", "target", "label", "confidence", "evidence")}
    elif not event.startswith("$"):
        scalars = {k: (v[:200] if isinstance(v, str) else v) for k, v in props.items()
                   if not k.startswith("$") and isinstance(v, (str, int, float, bool))}
        out["detail"] = dict(list(scalars.items())[:10])
    return out


class Store:
    def __init__(self, workspace):
        self.ws = workspace
        self.root = workspace.path("data")

    def import_files(self, files, key, text_events=()):
        """Split exported events into telemetry and context files under one chunk key."""
        telemetry, context = [], []
        seen, text_events = set(), set(text_events)
        for path in files:
            for row in read_export(path):
                uid = row.get("uuid")
                if uid in seen:
                    continue
                seen.add(uid)
                if row["event"] in TELEMETRY:
                    telemetry.append({k: row.get(k) for k in ("uuid", "event", "timestamp", "distinct_id", "person_id")} | {"properties": row["properties"]})
                else:
                    context.append(slim_context(row, text_events))
        telemetry.sort(key=lambda r: (r["timestamp"], r["uuid"] or ""))
        context.sort(key=lambda r: (r["timestamp"], r["uuid"] or ""))
        write_jsonl(self.root / "telemetry" / f"{key}.jsonl", telemetry)
        write_jsonl(self.root / "context" / f"{key}.jsonl", context)
        return len(telemetry), len(context)

    def pull(self, client, since_days, extra_events=(), text_events=(), log=print):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=since_days)
        include = sorted(set(TELEMETRY) | set(CONTEXT_EVENTS) | set(extra_events) | set(text_events))
        totals = [0, 0]
        for a, b in aligned_chunks(start, end):
            key = f"{a:%Y%m%dT%H%M}_{b:%Y%m%dT%H%M}"
            marker = self.root / "exports" / f"{key}.json"
            final = b <= end - timedelta(hours=1)
            if marker.exists():
                done = json.loads(marker.read_text())
                if done.get("final") and set(include) <= set(done.get("include", [])):
                    log(f"  {a:%Y-%m-%d} to {b:%Y-%m-%d}: already pulled")
                    continue
            log(f"  {a:%Y-%m-%d %H:%M} to {b:%Y-%m-%d %H:%M}: exporting from PostHog")
            files = client.export_events(a, b, include, self.root / "exports" / key)
            counts = self.import_files(files, key, text_events)
            for f in files:
                f.unlink()
            marker.write_text(json.dumps({"start": a.isoformat(), "end": b.isoformat(), "include": include, "final": final,
                                          "telemetry_events": counts[0], "context_events": counts[1]}))
            totals[0] += counts[0]; totals[1] += counts[1]
        recordings = self.index()
        return {"telemetry_events": totals[0], "context_events": totals[1], "recordings": len(recordings)}

    def iter_telemetry(self):
        seen = set()
        for path in sorted((self.root / "telemetry").glob("*.jsonl")):
            for row in read_jsonl(path):
                if row.get("uuid") in seen:
                    continue
                seen.add(row.get("uuid"))
                yield row

    def iter_context(self, events=None):
        seen = set()
        for path in sorted((self.root / "context").glob("*.jsonl")):
            for row in read_jsonl(path):
                if row.get("uuid") in seen or (events and row["event"] not in events):
                    continue
                seen.add(row.get("uuid"))
                yield row

    def index(self):
        """Recorder metadata from the telemetry batches (no input records are copied)."""
        meta = {}
        for row in self.iter_telemetry():
            p = row["properties"]
            rid = p.get("recorder_id")
            if not isinstance(rid, str):
                continue
            m = meta.setdefault(rid, {"recorder_id": rid, "sessions": [], "person_id": row.get("person_id"), "distinct_id": row.get("distinct_id"),
                                      "device_id": p.get("$device_id"),
                                      "persons": [], "first_ts": row["timestamp"], "last_ts": row["timestamp"], "pages": [], "spans": [], "anchors": [],
                                      "time_origin_ms": p.get("time_origin_ms"), "batches": 0})
            m["batches"] += 1
            for key, value in (("sessions", p.get("$session_id")), ("persons", row.get("person_id")), ("pages", p.get("$pathname"))):
                if value and value not in m[key]:
                    m[key].append(value)
            m["first_ts"] = min(m["first_ts"], row["timestamp"]); m["last_ts"] = max(m["last_ts"], row["timestamp"])
            if isinstance(p.get("start_ms"), (int, float)) and isinstance(p.get("end_ms"), (int, float)):
                m["spans"].append([p["start_ms"], p["end_ms"], p.get("$session_id"), p.get("$pathname")])
            clock = p.get("clock")
            if isinstance(clock, dict) and isinstance(clock.get("perf_ms"), (int, float)) and isinstance(clock.get("epoch_ms"), (int, float)):
                m["anchors"].append([clock["perf_ms"], clock["epoch_ms"]])
            elif isinstance(p.get("time_origin_ms"), (int, float)) and isinstance(p.get("end_ms"), (int, float)):
                m["anchors"].append([p["end_ms"], p["time_origin_ms"] + p["end_ms"]])  # Schema 1.0 had no clock anchor.
        rows = []
        for m in meta.values():
            m["spans"].sort(); m["anchors"].sort()
            m["session_id"] = m["sessions"][0] if m["sessions"] else None
            m["day"] = str(m["first_ts"])[:10]
            rows.append(m)
        rows.sort(key=lambda m: (m["first_ts"], m["recorder_id"]))
        write_jsonl(self.root / "recordings.jsonl", rows)
        return rows

    def recordings(self):
        return read_jsonl(self.root / "recordings.jsonl")


def aligned_chunks(start, end):
    """Monday-aligned UTC weeks, clipped to [start, end), so repeated pulls reuse finished weeks."""
    monday = (start - timedelta(days=start.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    for a, b in week_chunks(monday, end):
        a, b = max(a, start), min(b, end)
        if a < b:
            yield a, b


class Clock:
    """Maps between a recorder's relative milliseconds and wall-clock epoch milliseconds using its anchors."""

    def __init__(self, recording):
        self.anchors = recording.get("anchors") or []
        self.perf = [a[0] for a in self.anchors]

    def _offset(self, perf_ms):
        if not self.anchors:
            raise ValueError("Recorder has no clock anchors")
        i = bisect.bisect_left(self.perf, perf_ms)
        if i == len(self.perf):
            i -= 1
        elif i > 0 and perf_ms - self.perf[i - 1] < self.perf[i] - perf_ms:
            i -= 1
        return self.anchors[i][1] - self.anchors[i][0]

    def to_epoch(self, t_ms):
        return t_ms + self._offset(t_ms)

    def to_recorder(self, epoch):
        # Offsets barely move between anchors, so one refinement step is enough.
        guess = epoch - (self.anchors[0][1] - self.anchors[0][0])
        return epoch - self._offset(guess)


def recorders_by_session(recordings):
    out = defaultdict(list)
    for r in recordings:
        for s in r["sessions"]:
            out[s].append(r)
    return out


def recorder_at(candidates, epoch):
    """The recorder whose captured spans cover (or come nearest to) an epoch time."""
    best, best_distance = None, None
    for r in candidates:
        if not r.get("anchors"):
            continue
        t = Clock(r).to_recorder(epoch)
        for start, end, *_ in r["spans"]:
            distance = 0 if start <= t <= end else min(abs(t - start), abs(t - end))
            if best_distance is None or distance < best_distance:
                best, best_distance = (r, t), distance
    return best, best_distance
