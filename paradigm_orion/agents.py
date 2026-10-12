"""Labels for agents: the current state of one session, and search across sessions.

Both read the `orion_label` events the browser sends, through PostHog's query API (small results only).
PostHog usually makes new events queryable within a minute; in the page itself, `orion.context()` is instant.
"""
import json
import re

SESSION_LABELS = """
SELECT timestamp, properties.model AS model, properties.model_version AS version, properties.target AS target,
       properties.label AS label, properties.confidence AS confidence, properties.evidence AS evidence,
       properties.since AS since, properties.$pathname AS page, properties.agents AS agents
FROM events
WHERE event = 'orion_label' AND properties.$session_id = {session} AND timestamp > now() - toIntervalDay(30)
ORDER BY timestamp DESC
LIMIT 200"""


def _json(value):
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def context(client, session_id, agent=None):
    rows = client.query(SESSION_LABELS, {"session": session_id}, name="orion context")
    latest, history = {}, []
    for row in rows:
        agents = _json(row.get("agents")) or []
        if agent and agents and agent not in agents:
            continue
        entry = {"model": row["model"], "version": row["version"], "target": row["target"], "label": row["label"],
                 "confidence": row["confidence"], "evidence": _json(row["evidence"]) or [], "since": row["since"],
                 "page": row["page"], "at": str(row["timestamp"])}
        history.append(entry)
        latest.setdefault(row["model"], entry)
    for model, entry in latest.items():
        earlier = []
        for h in history:
            if h["model"] == model and h["label"] != entry["label"] and h["label"] not in earlier:
                earlier.append(h["label"])
        entry["earlier"] = earlier
    return {"session_id": session_id, "labels": list(latest.values()), "history": history, "replay_url": client.replay_url(session_id)}


def context_text(ctx):
    if not ctx["labels"]:
        return f"# Orion: no labels for session {ctx['session_id']} yet\nuser_state: none"
    lines = []
    for c in ctx["labels"]:
        lines += [f"# from Orion · {c['model']} v{c['version']}", "user_state:", f"  {c['target']}: {c['label']}"]
        if isinstance(c["confidence"], (int, float)):
            lines.append(f"  confidence: {c['confidence']:.2f}")
        lines.append(f"  since: {c['since']}")
        if c["earlier"]:
            lines.append(f"  earlier: [{', '.join(map(str, c['earlier']))}]")
        if c["evidence"]:
            lines.append(f"  evidence: {'; '.join(map(str, c['evidence']))}")
        if c["page"]:
            lines.append(f"  page: {c['page']}")
    lines.append(f"  replay: {ctx['replay_url']}")
    return "\n".join(lines)


def find_cohort(client, cohort):
    """A cohort by id or exact name, as {"id", "name"}."""
    found = client.request("GET", client.project_path("cohorts/?limit=500")).get("results", [])
    text = str(cohort).strip()
    matches = [c for c in found if (text.isdigit() and int(text) == c.get("id")) or c.get("name") == text]
    if len(matches) != 1:
        names = ", ".join(f"{c.get('name')} ({c.get('id')})" for c in found[:20])
        raise ValueError(f"Found {len(matches)} cohorts matching {text!r}" + (f"; cohorts: {names}" if names else ""))
    return {"id": int(matches[0]["id"]), "name": matches[0].get("name")}


def _filters(label, page, model, cohort_id=None):
    clauses, values = ["event = 'orion_label'", "timestamp > now() - toIntervalDay({days})"], {}
    if label is not None:
        clauses.append("toString(properties.label) = {label}")
        values["label"] = str(label)
    if model:
        clauses.append("properties.model = {model}")
        values["model"] = model
    if page:
        clauses.append("properties.$pathname LIKE {page}")
        values["page"] = page.replace("%", "\\%").replace("_", "\\_") + "%"
    if cohort_id is not None:
        clauses.append(f"person_id IN COHORT {int(cohort_id)}")  # HogQL's cohort test; the id is a checked integer.
    return " AND ".join(clauses), values


def search(client, label=None, page=None, last_days=30, model=None, limit=100, cohort=None):
    """Sessions that carried a label, and the pages where it shows up most often relative to all labelled sessions.
    With a cohort, both count only that cohort's people."""
    if not isinstance(last_days, int) or not 1 <= last_days <= 365:
        raise ValueError("last must be between 1 and 365 days")
    limit = max(1, min(int(limit), 1000))
    group = find_cohort(client, cohort) if cohort is not None else None
    cohort_id = group["id"] if group else None
    where, values = _filters(label, page, model, cohort_id)
    values["days"] = last_days
    sessions = client.query(f"""
        SELECT properties.$session_id AS session_id, min(timestamp) AS first_seen, max(timestamp) AS last_seen, count() AS labels,
               groupUniqArray(properties.$pathname) AS pages, max(toFloat(properties.confidence)) AS max_confidence
        FROM events WHERE {where} GROUP BY session_id ORDER BY first_seen DESC LIMIT {limit}""", values, name="orion search sessions")
    labelled_where, labelled_values = _filters(None, page, model, cohort_id)
    labelled_values["days"] = last_days
    by_page = client.query(f"""
        SELECT properties.$pathname AS page, count(DISTINCT properties.$session_id) AS sessions
        FROM events WHERE {where} GROUP BY page ORDER BY sessions DESC LIMIT 25""", values, name="orion search pages")
    totals = {r["page"]: r["sessions"] for r in client.query(f"""
        SELECT properties.$pathname AS page, count(DISTINCT properties.$session_id) AS sessions
        FROM events WHERE {labelled_where} GROUP BY page ORDER BY sessions DESC LIMIT 200""", labelled_values, name="orion search totals")}
    pages = [{"page": r["page"], "sessions": r["sessions"], "of_labelled_sessions": totals.get(r["page"]),
              "share": r["sessions"] / totals[r["page"]] if totals.get(r["page"]) else None} for r in by_page]
    for s in sessions:
        s["pages"] = _json(s["pages"]) or []
        s["replay_url"] = client.replay_url(s["session_id"])
    return {"label": label, "page": page, "last_days": last_days, "model": model, "cohort": group, "sessions": sessions, "pages": pages}


def parse_days(text):
    match = re.fullmatch(r"\s*(\d+)\s*d?\s*", str(text))
    if not match:
        raise ValueError("Use a number of days such as 30d")
    return int(match.group(1))
