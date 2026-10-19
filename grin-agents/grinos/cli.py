"""
grinos — operate the evidence pipeline and inspect the audit trail.

  grinos ingest SOURCE [--claims claims.json] --company ACME --kind 10-K --published 2026-02-20 \\
                --uri sec://ACME/10-K/FY2025 --as-of 2026-03-01 [--config companies/acme.yaml]
  grinos runs                      list runs
  grinos show RUN_ID               every step of a run, with actors and verdicts
  grinos trail CLAIM_ID            full history of one claim (source → decision → rules → verdict)
  grinos evidence [--company X]    what analyst agents are allowed to see (verified only)
  grinos resolve CLAIM_ID verified|rejected --note "..."   human verdict for needs_human claims
  grinos audit                     recompute the hash chain and every source blob

  grinos analyse ACME --config companies/acme.yaml [--net-debt M --shares M]   analyst proposals
  grinos proposals                 pending proposals (passed checks, awaiting you)
  grinos approve P_ID --note "..." / grinos reject P_ID --note "..."
  grinos capture [results|sync|go|push] …   completed research → LongGamma Notion workflow (list by default)
  grinos registry [--push]         Agent Registry rows from agents/ + eval baselines
  grinos notion-check              read-only: token works, which LongGamma databases it can see

  grinos serve [--port 8765]       dashboard + voice room at http://127.0.0.1:8765
  grinos telegram                  Telegram bot (TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS)
  grinos telegram-pair             one-time: allowlist yourself by sending the bot a code
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from grinos.ledger import Ledger, LedgerCorrupt
from grinos import notion
from grinos.env import load_env
from grinos.pipeline import human_verdict, ingest
from grinos.proposals import PAST, analyse, decide_proposal, pending, proposal_state

def default_root() -> Path:
    return Path(os.environ.get("GRINOS_HOME", Path(__file__).parents[1] / "var"))


def _ledger(args) -> Ledger:
    return Ledger(Path(args.root))


def cmd_ingest(args) -> int:
    meta = {"uri": args.uri or str(Path(args.source).resolve()), "kind": args.kind,
            "company": args.company, "published_at": date.fromisoformat(args.published)}
    claims = json.loads(Path(args.claims).read_text()) if args.claims else None   # None → extractor runs
    config = yaml.safe_load(Path(args.config).read_text()) if args.config else None
    s = ingest(_ledger(args), Path(args.source).read_bytes(), meta, claims,
               date.fromisoformat(args.as_of), config, model=args.model)
    print(f"{s['run_id']}  ({len(s['claims'])} claims)\n")
    for r in s["claims"]:
        print(f"  {r['claim_id']}  {r['action']:<8} {r['nature']:<15} {str(r['lever']):<15} "
              f"{r['verdict'] or '-':<12} {r['statement'][:60]}")
    return 0


def cmd_runs(args) -> int:
    runs: dict[str, Counter] = defaultdict(Counter)
    first: dict[str, str] = {}
    for e in _ledger(args).entries():
        runs[e["run_id"]][e["stage"]] += 1
        if e["stage"] == "verdict":
            runs[e["run_id"]][e["payload"]["verdict"]] += 1
        first.setdefault(e["run_id"], e["ts"])
    for rid, c in runs.items():
        print(f"{first[rid]}  {rid:<32} claims={c['claim']:<3} verified={c['verified']:<3} "
              f"rejected={c['rejected']:<3} needs_human={c['needs_human']:<3} discarded={c['claim'] - c['verdict']}")
    return 0


def _print_entry(e: dict) -> None:
    a, p = e["actor"], e["payload"]
    who = f"{a['kind']}:{a['name']}@{a['version'][:8]}" + (f"/{a['model']}" if a.get("model") else "")
    detail = {
        "source": lambda: f"{p['source']['kind']} {p['source']['uri']} published {p['source']['published_at']}",
        "claim": lambda: f"{p['claim']['metric']} {p['claim']['period']} = {p['claim']['value']} — {p['claim']['statement']}",
        "decision": lambda: f"{p['action']} · {p['nature']} · {p['materiality']} · lever={p['lever_facts']['lever']} — {p['rationale']}",
        "rule_checks": lambda: f"{p['rules_verdict']}: " + ", ".join(f"{c['id']}={'ok' if c['ok'] else 'FAIL'}" for c in p["checks"]),
        "verdict": lambda: f"{p['verdict'].upper()} — " + "; ".join(f"{r['code']}: {r['detail']}" for r in p["reasons"]),
    }.get(e["stage"], lambda: json.dumps(p)[:150])()
    print(f"  #{e['seq']:<4} {e['stage']:<12} {who:<44} {detail}")


def cmd_show(args) -> int:
    for e in _ledger(args).entries():
        if e["run_id"] == args.run_id:
            _print_entry(e)
    return 0


def cmd_trail(args) -> int:
    ledger = _ledger(args)
    entries = list(ledger.entries())
    blob = next((e["payload"]["claim"]["source_blob"] for e in entries
                 if e["stage"] == "claim" and e["payload"]["claim_id"] == args.claim_id), None)
    for e in entries:
        p = e["payload"]
        if p.get("claim_id") == args.claim_id or (e["stage"] == "source" and p["source"]["blob"] == blob):
            _print_entry(e)
    return 0


def cmd_evidence(args) -> int:
    try:
        claims = _ledger(args).verified_claims(args.company)
    except LedgerCorrupt as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    for c in claims:
        cl = c["claim"]
        print(f"{c['claim_id']}  {cl['company']:<6} {cl['metric']:<18} {cl['period']:<10} {cl['value']!s:<8} {cl['statement']}")
    return 0


def cmd_resolve(args) -> int:
    e = human_verdict(_ledger(args), args.claim_id, args.verdict, args.who, args.note)
    print(f"recorded #{e['seq']} {args.verdict} for {args.claim_id}")
    return 0


def cmd_audit(args) -> int:
    ledger = _ledger(args)
    problems = ledger.audit()
    n = sum(1 for _ in ledger.entries())
    if problems:
        print(f"LEDGER BROKEN — {len(problems)} problem(s) in {n} entries:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"ledger intact: {n} entries, chain and all source blobs verified")
    return 0


def cmd_why(args) -> int:
    from grinos.desk import company_config
    from grinos.graph import Graph, why
    print(why(Graph.open(_ledger(args)), " ".join(args.query), company_config))
    return 0


def cmd_graph(args) -> int:
    from grinos import graph
    ledger = _ledger(args)
    path = ledger.root / "graph.sqlite"
    if args.rebuild:
        path.unlink(missing_ok=True)
    g = graph.Graph.open(ledger, path)
    s = g.stats()
    print(f"{path}\n  nodes: " + ", ".join(f"{k} {v}" for k, v in s["nodes"].items())
          + "\n  edges: " + ", ".join(f"{k} {v}" for k, v in s["edges"].items()))
    for a, b in g.conflicts(args.company.upper() if args.company else None) if args.conflicts else []:
        print(f"  ⚠ {a['label']}: {a['value']} ({a['published']}) vs {b['value']} ({b['published']})  {a['id'][6:]} / {b['id'][6:]}")
    return 0


def _getter(args):
    from grinos import sec
    return sec.directory_getter(Path(args.offline)) if getattr(args, "offline", None) else sec.http_get


def _narrator(args):
    """--narrative: a memo-writer draft, validated and bounded (grinos.narrative); off by default."""
    if not getattr(args, "narrative", False):
        return None
    from grinos import narrative
    return narrative.narrate


def cmd_value(args) -> int:
    from grinos import valuation as V
    req = V.load_request(Path(args.assumptions), ticker=args.ticker, valuation_date=args.date, price=args.price)
    r = V.start(_ledger(args), req, _getter(args), who=args.who, refresh=args.refresh, narrate=_narrator(args))
    print(V.summary(r))
    return 0 if r["status"] in ("complete", "blocked") else 1


def cmd_valuation(args) -> int:
    from grinos import valuation as V
    ledger = _ledger(args)
    if args.action == "list":
        for r in V.runs(ledger):
            vps = r.get("value_per_share") or {}
            print(f"{r['run_id']}  {r['status']:<11} {r['step'] or '-':<12} price {r['price']}  "
                  + " ".join(f"{k} {Decimal(str(v)):.2f}" for k, v in vps.items()))
        return 0
    if not args.run_id:
        print("give a run id", file=sys.stderr)
        return 2
    if args.action == "decide":
        e = V.decide(ledger, args.run_id, args.target, args.decision, args.who, args.note,
                     Decimal(args.value) if args.value else None)
        print(f"recorded {args.target} {args.decision} (ledger #{e['seq']}); resuming")
    r = V.resume(ledger, args.run_id, narrate=_narrator(args))
    if args.action == "memo" and r["status"] == "complete":
        print(Path(r["out"]["memo"]["path"]).read_text())
    else:
        print(V.summary(r))
    return 0


def cmd_analyse(args) -> int:
    config = yaml.safe_load(Path(args.config).read_text())
    res = analyse(_ledger(args), args.company, config, args.config, model=args.model,
                  net_debt=args.net_debt, shares=args.shares)
    print(f"{res['run_id']}\n")
    for p in res["proposals"]:
        imp = p["impact"] or {}
        print(f"  {p['proposal_id']}  {'OK  ' if p['ok'] else 'FAIL'} {p['field']} → {p['new_value']}  "
              f"EV {imp.get('ev_change_pct', 0):+.1%}  {'; '.join(p['problems'])}")
    print(f"\n  no change: {res['no_change']}")
    return 0


def cmd_proposals(args) -> int:
    for s in (pending(_ledger(args)) if not args.all else proposal_state(_ledger(args)).values()):
        if not s["proposal"]:
            continue
        p, c = s["proposal"]["payload"], s["check"]["payload"]
        status = PAST[s["approval"]["payload"]["decision"]] if s["approval"] else ("pending" if c["ok"] else "failed checks")
        if s["published"]:
            status = "published"
        imp = c["impact"] or {}
        print(f"{p['proposal_id']}  {p['company']:<6} {p['proposal']['field']:<16} → {c.get('normalised_value')!s:<22} "
              f"EV {imp.get('ev_change_pct', 0):+.1%}  [{status}]  {p['proposal']['rationale'][:80]}")
    return 0


def cmd_approve(args, decision: str) -> int:
    try:
        e = decide_proposal(_ledger(args), args.proposal_id, decision, args.who, args.note)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"recorded #{e['seq']}: {args.proposal_id} {PAST[decision]} by {args.who}")
    return 0


def cmd_publish(args) -> int:
    """An approved proposal goes to Notion as a LongGamma model sync (diff → your go → push)."""
    print(f"publish now follows longgamma-model-sync: grinos capture sync {args.proposal_id}", file=sys.stderr)
    args.kind, args.source_id = "sync", args.proposal_id
    return cmd_capture(args)


def cmd_capture(args) -> int:
    from grinos import longgamma as LG
    ledger = _ledger(args)
    try:
        if args.kind in (None, "list"):
            rows = LG.pending(ledger)
            for r in rows:
                print(f"{r['id']:<58} {r['state']:<13} {r['label']}")
            print(f"{len(rows)} completed item(s) not yet in Notion" if rows else "everything completed is in Notion")
            return 0
        if args.kind in ("results", "sync"):
            p = LG.prepare(ledger, args.kind, args.source_id, refresh=args.refresh)
            print(LG.describe(p))
            lg = LG._id(args.kind, args.source_id)
            print(f"\nnext: grinos capture go {lg} --rationale \"…\"" if args.kind == "sync" else f"\nnext: grinos capture push {lg}")
            return 0
        if args.kind == "go":
            LG.go(ledger, args.source_id, args.who, args.rationale or "", confirm=args.confirm)
            print(f"go recorded for {args.source_id}: next grinos capture push {args.source_id}")
            return 0
        if args.kind == "push":
            out = LG.push(ledger, args.source_id)
            print("in Notion: " + ", ".join(f"{k} {v.get('url') if isinstance(v, dict) else len(v)}"
                                            for k, v in out.items() if k != "prepared"))
            return 0
    except notion.NotPublishable as exc:
        print(f"NOT CAPTURED: {exc}", file=sys.stderr)
        return 2
    return 2


def cmd_registry(args) -> int:
    root = Path(__file__).parents[1]
    rows = notion.registry_rows(root / "agents", eval_status(root))
    for r in rows:
        print(f"  {r['Agent']:<20} {r['Status']:<8} {r['Owner Script']}")
    if args.push:
        print(f"pushed {len(notion.push_registry(rows))} rows")
    return 0


def eval_status(root: Path) -> dict[str, str]:
    """agent → Live (current prompt passed its eval) | Paused (prompt changed since) | Planned."""
    from grinos.llm import agent_actor
    path = root / "evals" / "STATUS.json"
    status = json.loads(path.read_text()) if path.exists() else {}
    out = {}
    for agent, s in status.items():
        current = agent_actor(agent, s["model"]).version
        out[agent] = "Live" if s["passed"] and s["prompt_version"] == current else ("Paused" if s["passed"] else "Planned")
    return out


def cmd_notion_check(args) -> int:
    try:
        out = notion.check_access()
    except notion.NotPublishable as exc:
        print(f"NOT CONFIGURED: {exc}", file=sys.stderr)
        return 2
    if not out.get("integration"):
        print(f"FAILED: {out.get('error')}")
        return 1
    print(f"integration: {out['integration']}")
    ok = True
    for name, d in out["data_sources"].items():
        ok &= d["ok"]
        print(f"  {'ok ' if d['ok'] else 'NO '} {name:<15} {d.get('title', '')} {d.get('error', '')}")
    if "company_tickers" in out:
        print(f"  companies with tickers: {', '.join(out['company_tickers']) or '(none)'}")
    return 0 if ok else 1


def cmd_telegram_pair(args) -> int:
    from grinos.telegram import pair
    return pair(Path(__file__).parents[1] / ".env")


def cmd_serve(args) -> int:
    from grinos.web.server import serve
    return serve(_ledger(args), port=args.port, model=args.model)


def cmd_telegram(args) -> int:
    from grinos.telegram import run
    return run(_ledger(args), model=args.model)


def main(argv: list[str] | None = None) -> int:
    load_env()
    ap = argparse.ArgumentParser(prog="grinos")
    ap.add_argument("--root", default=str(default_root()), help="ledger home (default: $GRINOS_HOME or ./var)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest")
    p.add_argument("source")
    p.add_argument("--claims", help="claims JSON; omit to have the claim-extractor agent extract them")
    p.add_argument("--company", required=True)
    p.add_argument("--kind", required=True, choices=["10-K", "10-Q", "transcript", "broker", "call", "news", "other"])
    p.add_argument("--published", required=True)
    p.add_argument("--as-of", required=True)
    p.add_argument("--uri")
    p.add_argument("--config")
    p.add_argument("--model", default="sonnet")
    p.set_defaults(fn=cmd_ingest)

    sub.add_parser("runs").set_defaults(fn=cmd_runs)
    p = sub.add_parser("show")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("trail")
    p.add_argument("claim_id")
    p.set_defaults(fn=cmd_trail)
    p = sub.add_parser("evidence")
    p.add_argument("--company")
    p.set_defaults(fn=cmd_evidence)
    p = sub.add_parser("resolve")
    p.add_argument("claim_id")
    p.add_argument("verdict", choices=["verified", "rejected"])
    p.add_argument("--note", required=True)
    p.add_argument("--who", default=os.environ.get("USER", "human"))
    p.set_defaults(fn=cmd_resolve)
    sub.add_parser("audit").set_defaults(fn=cmd_audit)

    p = sub.add_parser("analyse")
    p.add_argument("company")
    p.add_argument("--config", required=True)
    p.add_argument("--net-debt", type=float, default=0.0)
    p.add_argument("--shares", type=float, default=1.0)
    p.add_argument("--model", default="sonnet")
    p.set_defaults(fn=cmd_analyse)
    p = sub.add_parser("proposals")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_proposals)
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("proposal_id")
        p.add_argument("--note", required=True)
        p.add_argument("--who", default=os.environ.get("USER", "human"))
        p.set_defaults(fn=lambda a, d=name: cmd_approve(a, d))
    p = sub.add_parser("publish")
    p.add_argument("proposal_id")
    p.add_argument("--push", action="store_true")
    p.set_defaults(fn=cmd_publish)
    sub.add_parser("notion-check").set_defaults(fn=cmd_notion_check)
    sub.add_parser("telegram-pair").set_defaults(fn=cmd_telegram_pair)
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--model", default="sonnet")
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("telegram")
    p.add_argument("--model", default="sonnet")
    p.set_defaults(fn=cmd_telegram)
    p = sub.add_parser("capture", help="completed research → the LongGamma Notion workflow")
    p.add_argument("kind", nargs="?", choices=["list", "results", "sync", "go", "push"], default="list")
    p.add_argument("source_id", nargs="?", help="results: ingest run · sync: valuation run or proposal · go/push: lg_… id")
    p.add_argument("--rationale", help="go: your rationale, written verbatim into the rationale note")
    p.add_argument("--confirm", help="go: the ticker, when PW Target moves more than 30%%")
    p.add_argument("--who", default=os.environ.get("USER", "analyst"))
    p.add_argument("--refresh", action="store_true", help="results: rebuild a note already in Notion")
    p.set_defaults(fn=cmd_capture)
    p = sub.add_parser("value", help="run (or resume) a fundamental FCFF valuation from an assumptions file")
    p.add_argument("--assumptions", required=True, help="YAML; see examples/valuation/acme.yaml")
    p.add_argument("--ticker")
    p.add_argument("--date", help="valuation date YYYY-MM-DD (overrides the file)")
    p.add_argument("--price", help="market price per share on the valuation date (overrides the file)")
    p.add_argument("--offline", help="folder of saved SEC responses instead of the network")
    p.add_argument("--refresh", action="store_true", help="re-fetch SEC data; new filings make a new version")
    p.add_argument("--who", default=os.environ.get("USER", "analyst"))
    p.add_argument("--narrative", action="store_true", help="add a model-drafted, validated narrative (memo-writer)")
    p.set_defaults(fn=cmd_value)
    p = sub.add_parser("valuation", help="list, show, resume or decide on valuation runs")
    p.add_argument("action", choices=["list", "show", "resume", "decide", "memo"])
    p.add_argument("run_id", nargs="?")
    p.add_argument("target", nargs="?", help="decide: review item or adjustment id")
    p.add_argument("decision", nargs="?", choices=["accept", "value", "reject", "approve"])
    p.add_argument("--note", default="")
    p.add_argument("--value", help="decide value: the figure in millions")
    p.add_argument("--who", default=os.environ.get("USER", "analyst"))
    p.add_argument("--narrative", action="store_true", help="add a model-drafted, validated narrative (memo-writer)")
    p.set_defaults(fn=cmd_valuation)
    p = sub.add_parser("why", help="trace an assumption, fact or claim back to its sources")
    p.add_argument("query", nargs="+", help="<claim_id> | <TICKER> <assumption> | <TICKER> <metric> <period> | <TICKER> conflicts")
    p.set_defaults(fn=cmd_why)
    p = sub.add_parser("graph", help="rebuild / summarise the research graph (var/graph.sqlite)")
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--conflicts", action="store_true")
    p.add_argument("--company")
    p.set_defaults(fn=cmd_graph)
    p = sub.add_parser("registry")
    p.add_argument("--push", action="store_true")
    p.set_defaults(fn=cmd_registry)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
