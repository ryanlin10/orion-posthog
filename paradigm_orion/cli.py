"""The `orion` command. Run `orion <command> --help` for details."""
import argparse
import json
import sys

from . import Orion, __version__


def cmd_init(args):
    from .onboarding import init
    init(args)


def cmd_pull(args):
    o = Orion()
    if args.file:
        from datetime import datetime
        counts = o.store.import_files(args.file, f"import-{datetime.now():%Y%m%dT%H%M%S}", args.text_event or ())
        o.store.index()
        print(f"Imported {counts[0]} telemetry and {counts[1]} other events")
        return
    result = o.pull(args.since, args.events or (), args.recipe)
    print(f"Pulled {result['telemetry_events']} telemetry and {result['context_events']} other events; {result['recordings']} recordings in total")


def cmd_label(args):
    o = Orion()
    recipe = o.recipe(args.recipe)
    if args.labeller == "human":
        from .labellers.human import Session, serve
        session = Session(o.store, recipe, o.posthog, args.limit, args.show_ai)
        server, _ = serve(session, args.port, not args.no_browser)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print(f"\nSaved {session.state()['done']} labels to {session.path}")
        return
    from .labels import save_labels
    if args.labeller == "self_report":
        from .labellers.self_report import label_self_report
        labels, stats = label_self_report(o.store, recipe)
        save_labels(o.workspace, recipe, "self_report", labels)
        print(f"self-report labels: {json.dumps(stats)}")
        return
    llm = o.llm({**recipe, "labels": {**recipe["labels"], "by": ["model"]}})
    if args.labeller == "text":
        from .labellers.text import label_text as run
    else:
        from .labellers.replay import label_replay as run
    labels, stats = run(o.store, recipe, llm)
    save_labels(o.workspace, recipe, args.labeller, labels)
    print(f"{args.labeller} labels: {json.dumps(stats)} (model calls made: {llm.calls})")


def cmd_build(args):
    Orion().build(args.recipe, args.relabel)


def cmd_train(args):
    clf = Orion().train(args.recipe, args.arch, args.evaluate_test, rebuild=args.rebuild, shuffles=args.shuffles)
    print(f"Model: {clf.path}")


def cmd_deploy(args):
    from .deploy import deploy
    from .workspace import Workspace
    target, extra, snippet, spec = deploy(Workspace(), args.name, args.out, args.version, args.context, args.url_prefix)
    print(f"Deployed {spec['name']} v{spec['version']} to {target}" + (f" (+ {', '.join(map(str, extra))})" if extra else ""))
    if spec.get("agents"):
        print(f"Labels will be tagged for: {', '.join(spec['agents'])}")
    print("\nAdd to your site after PostHog:\n\n" + snippet)


def cmd_survey(args):
    o = Orion()
    survey, path = o.survey(args.recipe, args.pages, args.out)
    print(f"Ranked the answers to “{survey['question']}” for {len(survey['table'])} pages with {survey['ranked_by']}: {path}")
    if args.out:
        url = f"{args.url_prefix.rstrip('/')}/{path.name}"
        print(f"\nAdd to orionOptions on your site:\n\n  selfReport: {{ survey: '{url}' }}")


def cmd_context(args):
    o = Orion()
    print(json.dumps(o.context(args.session, args.agent), indent=2, default=str) if args.json else o.context(args.session, args.agent, text=True))


def cmd_search(args):
    result = Orion().search(args.label, args.page, args.last, args.model, args.limit, args.cohort)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    where = (f" on {args.page}" if args.page else "") + (f" in cohort {result['cohort']['name']}" if result.get("cohort") else "")
    print(f"{len(result['sessions'])} sessions labelled {args.label or 'anything'}{where} in the last {result['last_days']} days")
    for p in result["pages"][:10]:
        share = f" ({p['share']:.0%} of labelled sessions there)" if p["share"] is not None else ""
        print(f"  {p['page']}: {p['sessions']} sessions{share}")
    for s in result["sessions"][:10]:
        print(f"  {s['session_id']}  {s['first_seen']}  {s['replay_url']}")


def cmd_status(args):
    from .store import Store
    from .workspace import Workspace
    ws = Workspace()
    recs = Store(ws).recordings()
    print(f"Workspace: {ws.root}")
    print(f"PostHog: {json.dumps(ws.config())}")
    print(f"Recordings pulled: {len(recs)} from {len({r.get('person_id') for r in recs})} people over {len({r['day'] for r in recs})} days")
    for kind in ("datasets", "models"):
        base = ws.path(kind)
        if base.exists():
            for d in sorted(base.iterdir()):
                versions = ws.versions(kind, d.name)
                if versions:
                    print(f"{kind[:-1].title()} {d.name}: v{versions[-1]} ({len(versions)} versions)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="orion", description="Orion: input telemetry on PostHog, sentiment classifiers, and live labels for agents.")
    p.add_argument("--version", action="version", version=f"orion {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="Set up this folder: PostHog host, key and project, the AI labeller and its key")
    s.add_argument("--host", help="PostHog app host, e.g. https://us.posthog.com or https://eu.posthog.com")
    s.add_argument("--project", help="PostHog project id")
    s.add_argument("--ui-host", help="Host for replay links if it differs from --host")
    s.add_argument("--llm", help="AI labeller: a provider (anthropic, openai, gemini) or a model, e.g. gpt-5.6-sol")
    s.add_argument("--no-input", action="store_true", help="Ask nothing; save the flags as given")
    s.add_argument("--no-check", action="store_true", help="Do not check the keys with PostHog or the AI provider")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("pull", help="Export Orion telemetry and the events around it from PostHog")
    s.add_argument("--since", default="30d")
    s.add_argument("--events", type=lambda v: [x for x in v.split(",") if x], help="Extra event names, comma-separated")
    s.add_argument("--recipe", help="Also pull the text event this recipe labels from")
    s.add_argument("--file", nargs="+", help="Import exported JSON Lines files instead of calling PostHog")
    s.add_argument("--text-event", action="append", help="With --file: event names whose text the text labeller reads")
    s.set_defaults(fn=cmd_pull)

    s = sub.add_parser("label", help="Label windows: AI on text, AI on the replay, people on a local page, or visitors' own answers")
    s.add_argument("labeller", choices=("text", "replay", "human", "self_report"))
    s.add_argument("recipe")
    s.add_argument("--port", type=int, default=8795)
    s.add_argument("--limit", type=int, default=200, help="Human page: windows to offer")
    s.add_argument("--show-ai", action="store_true", help="Human page: show the AI labels (off by default to avoid anchoring)")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_label)

    s = sub.add_parser("build", help="Label (AI labels are cached), merge, split and build a dataset version")
    s.add_argument("recipe")
    s.add_argument("--relabel", action="store_true", help="Ask the AI labellers again")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("train", help="Build and train a classifier; saves a new model version")
    s.add_argument("recipe", help="Recipe file or name")
    s.add_argument("--arch", choices=("logistic", "random_forest", "boosted_trees", "gru", "tcn", "transformer"))
    s.add_argument("--evaluate-test", action="store_true", help="Also score the test split (do this once, after choosing a recipe)")
    s.add_argument("--no-rebuild", dest="rebuild", action="store_false", help="Train on the latest dataset version as is")
    s.add_argument("--shuffles", type=int, help="Shuffled-label models to compare against (default 20; 3 for sequence models)")
    s.set_defaults(fn=cmd_train)

    s = sub.add_parser("deploy", help="Copy a model to your site's static files for live labelling")
    s.add_argument("name")
    s.add_argument("--out", default="public/orion", help="Folder your site serves (default public/orion)")
    s.add_argument("--version", type=int)
    s.add_argument("--context", action="append", help="Agent that should receive these labels (repeatable)")
    s.add_argument("--url-prefix", default="/orion/", help="URL path where --out is served")
    s.set_defaults(fn=cmd_deploy)

    s = sub.add_parser("survey", help="Rank the self-report answers for each page and mood (Jev, or the AI labeller's model)")
    s.add_argument("recipe")
    s.add_argument("--pages", type=int, default=20, help="Busiest pages to rank answers for (plus one site-wide entry)")
    s.add_argument("--out", help="Also write the survey file here, e.g. public/orion")
    s.add_argument("--url-prefix", default="/orion/", help="URL path where --out is served")
    s.set_defaults(fn=cmd_survey)

    s = sub.add_parser("context", help="The live label for one session, with evidence and a replay link")
    s.add_argument("session")
    s.add_argument("--agent", help="Only labels tagged for this agent")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_context)

    s = sub.add_parser("search", help="Find sessions and pages by inferred label")
    s.add_argument("--label")
    s.add_argument("--page", help="Path prefix, e.g. /checkout")
    s.add_argument("--cohort", help="Only people in this PostHog cohort (its id or exact name)")
    s.add_argument("--last", default="30d")
    s.add_argument("--model")
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("status", help="What has been pulled, built and trained here")
    s.set_defaults(fn=cmd_status)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (ValueError, FileNotFoundError, RuntimeError) as error:
        sys.stdout.flush()
        print(f"orion: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
