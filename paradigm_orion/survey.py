"""`orion survey <recipe>`: rank the self-report answers for each page and mood ahead of time.

orion.js asks people how it is going with two positive and two negative answers. Which four depends on the page
and on the visitor's recent input, read by the built-in mood reading. The ranking happens here, once: by Jev when
a TypeSafe key is saved, otherwise by the AI labeller's model. The result is a small JSON file the site serves, so
no key and no server are involved when the question is shown.
"""
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# What orion.js's mood reading saw; "quiet" is little or no pointer movement.
MOOD_TEXT = {
    "angry": "fast, forceful pointer movement with bursts of rapid clicks",
    "frustrated": "side-to-side shaking of the pointer and repeated clicks",
    "calm": "slow, steady pointer movement",
    "anxious": "small jittery corrections and hesitant stops",
    "confused": "circling, wandering pointer paths with stops",
    "pleased": "smooth, direct pointer movement at a comfortable pace",
    "quiet": "little or no pointer movement",
}
ANSWER_TEXT = {
    "calm": "relaxed and unhurried", "pleased": "happy with how it is going", "engaged": "interested and absorbed",
    "confident": "sure of what to do next", "frustrated": "blocked or annoyed by something not working",
    "confused": "unsure where to go or what something means", "anxious": "worried about getting something wrong",
    "angry": "irritated or fed up", "bored": "waiting, disengaged or uninterested",
}


def page_contexts(store, pages=(), limit=20):
    """(page, plain-words description) for the busiest pages in the pulled data, site-wide ("*") first."""
    seen = defaultdict(lambda: {"sessions": set(), "clicks": Counter(), "rage": Counter(), "dead": Counter(), "errors": Counter()})
    for row in store.iter_context({"$pageview", "$autocapture", "$rageclick", "$dead_click", "$exception"}):
        path = row.get("pathname")
        if not path or (pages and not path.startswith(tuple(pages))):
            continue
        p, detail = seen[path], row.get("detail") or {}
        p["sessions"].add(row.get("session_id"))
        text = (detail.get("text") or "").strip()[:60]
        if row["event"] == "$autocapture" and detail.get("type") == "click" and text:
            p["clicks"][text] += 1
        elif row["event"] == "$rageclick":
            p["rage"][text or detail.get("tag") or "an element"] += 1
        elif row["event"] == "$dead_click":
            p["dead"][text or detail.get("tag") or "an element"] += 1
        elif row["event"] == "$exception" and detail.get("message"):
            p["errors"][detail["message"][:100]] += 1

    def describe(path, p):
        lines = [f"Page: {path} ({len(p['sessions'])} sessions in the data)."]
        for key, words in (("clicks", "Most clicked"), ("rage", "Rage clicks on"), ("dead", "Clicks that did nothing on"), ("errors", "Errors seen")):
            if p[key]:
                lines.append(f"{words}: " + ", ".join(f"“{t}”" for t, _ in p[key].most_common(5)) + ".")
        return " ".join(lines)

    busiest = sorted(seen, key=lambda k: (-len(seen[k]["sessions"]), k))[:limit]
    site = (f"Any page of the product. Busiest pages: {', '.join(busiest[:8])}." if busiest else "Any page of the product.")
    return [("*", site)] + [(path, describe(path, seen[path])) for path in busiest]


def situation(page_text, mood, question):
    return (f"A visitor is using a website. {page_text}\nTheir input over the last few seconds: {MOOD_TEXT[mood]}.\n"
            f"We are about to ask them “{question}” and offer two positive and two negative answers.")


def rank_with_jev(jev, state, positive, negative):
    questions = {name: {"type": "choice", "criteria": {a: ANSWER_TEXT.get(a, a.replace("_", " ")) for a in pool},
                        "instructions": f"Which of these {name} answers is this visitor most likely to recognise as true for them right now?"}
                 for name, pool in (("positive", positive), ("negative", negative))}
    answers = jev.ask(state, questions)

    def ranked(name, pool):
        p = (answers.get(name) or {}).get("probabilities") or {}
        return sorted(pool, key=lambda a: (-float(p.get(a, 0)), pool.index(a)))[:2]

    return ranked("positive", positive) + ranked("negative", negative)


def rank_with_llm(llm, page_text, question, positive, negative):
    """One call per page covering every mood; anything missing falls back to the listed order."""
    from .llm import parse_json
    system = "You rank answer options for a short in-product survey question. Reply with JSON only."
    prompt = (f"A visitor is using a website. {page_text}\nWe will ask them “{question}” and offer two positive and two negative answers.\n"
              f"Positive answers: {', '.join(positive)}. Negative answers: {', '.join(negative)}.\n"
              "For each situation below, rank each list from the answer this visitor is most likely to recognise as true for them to the least.\n"
              + "\n".join(f"- {mood}: their input over the last few seconds shows {text}" for mood, text in MOOD_TEXT.items())
              + '\nReply as {"<situation>": {"positive": [...], "negative": [...]}, ...} using only the listed answers.')
    reply = parse_json(llm.complete(system, prompt))
    out = {}
    for mood in MOOD_TEXT:
        entry = reply.get(mood) if isinstance(reply, dict) else None
        entry = entry if isinstance(entry, dict) else {}
        pick = lambda key, pool: ([a for a in entry.get(key, []) if a in pool] + [a for a in pool if a not in entry.get(key, [])])[:2]
        out[mood] = pick("positive", positive) + pick("negative", negative)
    return out


def build_survey(store, recipe, jev=None, llm=None, pages=20, log=print):
    offered = recipe["labels"]["self_report"]
    question, positive, negative = offered["question"], offered["positive"], offered["negative"]
    contexts = page_contexts(store, recipe["source"]["pages"], pages)
    table = {}
    if jev is not None:
        jobs = [(page, mood, situation(text, mood, question)) for page, text in contexts for mood in MOOD_TEXT]
        log(f"Ranking answers for {len(contexts)} pages × {len(MOOD_TEXT)} moods with Jev ({jev.model})")
        with ThreadPoolExecutor(8) as pool:
            ranked = list(pool.map(lambda job: rank_with_jev(jev, job[2], positive, negative), jobs))
        for (page, mood, _), order in zip(jobs, ranked):
            table.setdefault(page, {})[mood] = order
        ranked_by = jev.model
    elif llm is not None:
        log(f"Ranking answers for {len(contexts)} pages with {llm.model} (save a TypeSafe key to use Jev)")
        for page, text in contexts:
            table[page] = rank_with_llm(llm, text, question, positive, negative)
        ranked_by = llm.model
    else:
        raise ValueError("Ranking answers needs Jev (a TypeSafe key) or an AI labeller key; run `orion init`")
    return {"format": "orion-survey", "format_version": 1, "name": recipe["name"], "question": question, "positive": positive,
            "negative": negative, "ranked_by": ranked_by, "moods": list(MOOD_TEXT), "table": table,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def save_survey(workspace, survey):
    path = workspace.path("surveys", f"{survey['name']}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(survey, indent=2) + "\n")
    return path
