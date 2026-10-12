"""Self-report labels: what people answered when orion.js asked them how it was going.

An answer labels the window that ended when the question appeared, so moving to and clicking the question is never
part of it. Answers come from `orion_self_report` events, which carry the recorder and its clock, so no time
matching is needed.
"""
from ..labels import make_label

VERSION = "self-report-1"


def answer_value(recipe, answer):
    """The target value an answer gives, or None when the answer says nothing about this target."""
    target, offered = recipe["target"], recipe["labels"]["self_report"]
    if target["type"] == "categorical" and not target.get("binary"):
        return answer if answer in target["classes"] else None
    # "frustrated" names the target "frustrated" or "frustration"; positive answers rule it out.
    named = answer == target["name"] or answer[:6] == target["name"][:6]
    positive = answer in offered["positive"]
    if target.get("binary"):
        return "yes" if named else "no" if positive else None
    return target["max"] if named else target["min"] if positive else None


def label_self_report(store, recipe):
    length = recipe["window"]["length_ms"]
    recorders = {r["recorder_id"] for r in store.recordings()}
    labels = []
    stats = {"answers": 0, "dismissed": 0, "no_telemetry": 0, "not_this_target": 0, "labelled": 0}
    for row in store.iter_context({"orion_self_report"}):
        detail = row.get("detail") or {}
        answer, rid, shown = detail.get("answer"), detail.get("recorder_id"), detail.get("shown_t")
        if not answer:
            stats["dismissed"] += 1
            continue
        stats["answers"] += 1
        if rid not in recorders or not isinstance(shown, (int, float)):
            stats["no_telemetry"] += 1
            continue
        value = answer_value(recipe, str(answer))
        if value is None:
            stats["not_this_target"] += 1
            continue
        labels.append(make_label(recipe, "self_report", rid, max(0.0, shown - length), shown, value, 1.0, VERSION, source="self_report",
                                 session_id=row.get("session_id"), answer=answer, options=detail.get("options")))
        stats["labelled"] += 1
    return labels, stats
