"""
The research graph: a deterministic, rebuildable projection of the ledger. The ledger stays the
only source of truth; delete var/graph.sqlite and it is rebuilt identically.

    company     POOL
    source      one filing / transcript (blob)          ─ ABOUT → company
    claim       one extracted fact, with its status    ─ EXTRACTED_FROM → source
                                                        ─ ASSERTS → fact
                                                        ─ BEARS_ON → assumption   (decision model's lever)
    fact        company | metric | period | segment | adjusted, the same number across filings
                (not for metric "other", a catch-all that would equate unrelated numbers)
    assumption  company.config_field (mid_cycle_ebit, duration_score, gross_margin, ...)
    proposal    analyst proposal + check + human decision + publication
                                                        ─ CITES → claim
                                                        ─ CHANGES → assumption
    claim ─ CONFIRMS / CONFLICTS → the first verified claim of the same fact
    valuation   one FCFF valuation run (grinos.valuation): status, value per share by scenario
                                                        ─ ABOUT → company
                                                        ─ USES → xfact            (each reported XBRL fact)
                                                        ─ CITES → claim           (drivers, risks, disconfirming)
                                                        ─ SUPERSEDES → valuation  (same inputs, newer filings)
    xfact       one SEC XBRL fact: concept, period, value, accession, filing URL

Only verified claims (served by Ledger.verified_claims, the one door) confirm or conflict.
"""
import json
import math
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from grinos.ledger import Ledger

VERSION = 3
SCHEMA = """
CREATE TABLE meta  (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, company TEXT, label TEXT, props TEXT);
CREATE TABLE edges (src TEXT, type TEXT, dst TEXT, props TEXT);
CREATE INDEX edges_src ON edges (src, type);
CREATE INDEX edges_dst ON edges (dst, type);
CREATE INDEX nodes_type ON nodes (type, company);
"""


def normalise_period(period: str) -> str:
    """'Q2 FY2026', 'q2 2026', 'FY 2025', '6M FY2026' → 'Q2 FY2026', 'FY FY2025', 'H1 FY2026'.
    Shapes without a year pass through, uppercased."""
    p = period.upper()
    year = re.search(r"((?:19|20)\d\d)", p)
    if not year:
        return re.sub(r"\s+", " ", p).strip()
    found = re.search(r"\b(Q[1-4]|H[12]|9M|6M)\b", p)
    token = {"6M": "H1"}.get(found[1], found[1]) if found else "FY"
    return f"{token} FY{year[1]}"


def fact_key(claim: dict) -> str:
    return "fact:" + "|".join([claim["company"], claim["metric"], normalise_period(claim["period"]),
                               claim.get("segment") or "", "adj" if claim.get("adjusted") else "gaap"])


def _precision(v: float) -> float:
    """Half a unit in the last significant digit as printed: 1800.0 → 50, 1822.938 → 0.0005."""
    if v == 0:
        return 0.0
    digits = f"{abs(v):.6g}".replace(".", "").lstrip("0").rstrip("0") or "0"
    exp = math.floor(math.log10(abs(v)))
    return 0.5 * 10 ** (exp - len(digits) + 1)


def agree(a: float, b: float) -> bool:
    """Same number allowing for rounding in the less precise of the two ("$1.8 billion" ≈ 1,822.9)."""
    return abs(a - b) <= max(_precision(a), _precision(b), 1e-9 + 0.001 * max(abs(a), abs(b)))


def project(ledger: Ledger) -> dict:
    """Ledger → {nodes: {id: node}, edges: [(src, type, dst, props)], head}. Pure and deterministic."""
    entries = list(ledger.entries())
    served = {c["claim_id"] for c in ledger.verified_claims()}
    nodes: dict[str, dict] = {}
    edges: list[tuple[str, str, str, dict]] = []

    def node(nid: str, typ: str, company: str | None, label: str, **props):
        n = nodes.setdefault(nid, {"type": typ, "company": company, "label": label, "props": {}})
        n["props"].update(props)
        return n

    sources: dict[str, dict] = {}
    claims: dict[str, dict] = {}
    latest_steps: dict[str, dict] = {}
    for e in entries:
        p, st = e["payload"], e["stage"]
        if st == "source":
            s = p["source"]
            sources[s["blob"]] = s
            node(f"company:{s['company']}", "company", s["company"], s["company"])
            node(f"source:{s['blob']}", "source", s["company"], f"{s['company']} {s['kind']} {s['published_at']}",
                 uri=s["uri"], kind=s["kind"], published=s["published_at"], seq=e["seq"])
            edges.append((f"source:{s['blob']}", "ABOUT", f"company:{s['company']}", {}))
        elif st == "claim":
            c = p["claim"]
            claims[p["claim_id"]] = c
            published = sources.get(c["source_blob"], {}).get("published_at")
            node(f"claim:{p['claim_id']}", "claim", c["company"], c["statement"], quote=c["quote"],
                 metric=c["metric"], period=c["period"], value=c.get("value"), unit=c.get("unit"),
                 published=published, seq=e["seq"], hash=e["hash"], status="pending")
            edges.append((f"claim:{p['claim_id']}", "EXTRACTED_FROM", f"source:{c['source_blob']}", {}))
            if c.get("value") is not None and c["metric"] != "other":   # "other" names no fact: never compared
                node(fact_key(c), "fact", c["company"],
                     f"{c['company']} {c['metric']} {normalise_period(c['period'])}"
                     + (f" [{c['segment']}]" if c.get("segment") else "") + (" adjusted" if c.get("adjusted") else ""))
                edges.append((f"claim:{p['claim_id']}", "ASSERTS", fact_key(c), {}))
        elif st == "decision":
            n = nodes[f"claim:{p['claim_id']}"]["props"]
            n.update(action=p["action"], nature=p.get("nature"), lever=p["lever_facts"]["lever"])
            if p["action"] != "verify":
                n["status"] = "discarded"
            field = p["lever_facts"].get("config_field")
            if p["action"] == "verify" and field:
                c = claims[p["claim_id"]]
                aid = f"assumption:{c['company']}.{field}"
                node(aid, "assumption", c["company"], f"{c['company']} {field}", field=field)
                edges.append((f"claim:{p['claim_id']}", "BEARS_ON", aid, {}))
        elif st == "verdict":
            n = nodes[f"claim:{p['claim_id']}"]["props"]
            if e["actor"]["kind"] == "human" or not n.get("human"):
                n.update(verdict=p["verdict"], verdict_by=f"{e['actor']['kind']}:{e['actor']['name']}",
                         reasons=[r.get("detail", r.get("code")) for r in p.get("reasons", [])][:2],
                         human=n.get("human") or e["actor"]["kind"] == "human")
        elif st == "proposal":
            pr = p["proposal"]
            pid = f"proposal:{p['proposal_id']}"
            aid = f"assumption:{p['company']}.{pr['field']}"
            node(aid, "assumption", p["company"], f"{p['company']} {pr['field']}", field=pr["field"])
            node(pid, "proposal", p["company"], f"{p['company']} {pr['field']} → {pr['new_value']}",
                 field=pr["field"], new_value=pr["new_value"], old_value=p["config"].get(pr["field"]),
                 nature=pr["nature"], rationale=pr["rationale"], ts=e["ts"], seq=e["seq"], status="proposed")
            edges.append((pid, "CHANGES", aid, {}))
            edges += [(pid, "CITES", f"claim:{c}", {}) for c in pr["cites"]]
        elif st == "proposal_check":
            n = nodes[f"proposal:{p['proposal_id']}"]["props"]
            n.update(check_ok=p["ok"], problems=p["problems"], impact=p.get("impact"))
            if not p["ok"]:
                n["status"] = "failed_checks"
        elif st == "approval":
            n = nodes[f"proposal:{p['proposal_id']}"]["props"]
            if "decided_by" not in n:                                   # first decision is final
                n.update(status={"approve": "approved", "reject": "rejected"}[p["decision"]],
                         decided_by=e["actor"]["name"], note=p["note"], decided_ts=e["ts"])
        elif st == "valuation_request":
            r = p["request"]
            vid = f"valuation:{e['run_id']}"
            node(f"company:{r['ticker']}", "company", r["ticker"], r["ticker"])
            node(vid, "valuation", r["ticker"], f"{r['ticker']} valuation {r['valuation_date']} at ${r['price']}",
                 valuation_date=r["valuation_date"], price=r["price"], seq=e["seq"], status="started", decisions=[])
            edges.append((vid, "ABOUT", f"company:{r['ticker']}", {}))
            if p.get("supersedes"):
                edges.append((vid, "SUPERSEDES", f"valuation:{p['supersedes']}", {}))
        elif st == "valuation_status" and f"valuation:{e['run_id']}" in nodes:
            nodes[f"valuation:{e['run_id']}"]["props"].update(status=p["status"], step=p["step"], blocking=p["blocking"])
        elif st == "valuation_decision" and f"valuation:{e['run_id']}" in nodes:
            nodes[f"valuation:{e['run_id']}"]["props"]["decisions"].append(
                {k: p[k] for k in ("target", "kind", "decision", "note", "by", "value")} | {"seq": e["seq"]})
        elif st == "valuation_step" and f"valuation:{e['run_id']}" in nodes:
            vid = f"valuation:{e['run_id']}"
            latest_steps.setdefault(vid, {})[p["step"]] = p
        elif st == "published":
            n = nodes.get(f"proposal:{p.get('proposal_id')}")
            if n and "published" not in n["props"]:
                n["props"]["published"] = {k: v for k, v in p.items() if k != "proposal_id"}

    # Valuations: from each run's latest step outputs (a step is re-recorded only when it changes).
    for vid, steps in latest_steps.items():
        props = nodes[vid]["props"]
        rec = steps.get("reconcile", {}).get("output", {}).get("reported", {})
        for fid, f in rec.get("facts", {}).items():
            period = f"{f['start']} to {f['end']}" if f.get("start") else f"at {f['end']}"
            node(f"xfact:{fid}", "xfact", nodes[vid]["company"], f"{f['concept']} {period} = {f['value']} {f['unit']}",
                 concept=f["concept"], value=f["value"], unit=f["unit"], period=period, accn=f["accn"],
                 form=f["form"], filed=f["filed"], url=f["url"], restated=bool(f["superseded"]))
            edges.append((vid, "USES", f"xfact:{fid}", {}))
        for section, items in steps.get("evidence", {}).get("output", {}).get("sections", {}).items():
            for i in items:
                if i.get("verified") and str(i.get("source", "")).startswith("c_"):
                    edges.append((vid, "CITES", f"claim:{i['source']}", {"section": section}))
        val = steps.get("valuation", {}).get("output", {})
        if val:
            props["value_per_share"] = {k: v["value_per_share"] for k, v in val["scenarios"].items()}
            props["bridge"] = val["bridge"]
        fc = steps.get("forecast", {}).get("output", {})
        if fc.get("scenarios"):
            props["wacc"] = {k: v["wacc"]["wacc"] for k, v in fc["scenarios"].items()}
            props["terminal_growth"] = {k: v["assumptions"]["terminal_growth"] for k, v in fc["scenarios"].items()}
        props["adjustments"] = [{k: a[k] for k in ("id", "period", "amount", "status", "rationale")}
                                for a in steps.get("adjustments", {}).get("output", {}).get("proposed", [])]
        props["balance"] = {k: (v or {}).get("fact_ids") for k, v in rec.get("balance", {}).items() if k != "date"}
        props["balance_date"] = rec.get("balance", {}).get("date")
        props["shares_fact_ids"] = (rec.get("diluted_shares") or {}).get("fact_ids")
        props["reverse_dcf"] = steps.get("sensitivity", {}).get("output", {}).get("reverse_dcf")

    for cid in claims:
        n = nodes[f"claim:{cid}"]["props"]
        if cid in served:
            n["status"] = "verified"
        elif n["status"] != "discarded" and n.get("verdict"):
            n["status"] = n["verdict"]

    # Same fact across claims: later verified claims confirm or conflict with the first one.
    by_fact = defaultdict(list)
    for src, typ, dst, _ in edges:
        if typ == "ASSERTS" and nodes[src]["props"]["status"] == "verified":
            by_fact[dst].append(src)
    for fid, cids in by_fact.items():
        cids.sort(key=lambda c: (nodes[c]["props"]["published"] or "", nodes[c]["props"]["seq"]))
        first = nodes[cids[0]]["props"]
        for c in cids[1:]:
            n = nodes[c]["props"]
            if n["unit"] != first["unit"]:
                continue
            same = agree(n["value"], first["value"])
            edges.append((c, "CONFIRMS" if same else "CONFLICTS", cids[0], {"fact": fid}))

    return {"nodes": nodes, "edges": edges, "head": entries[-1]["hash"] if entries else "", "seq": len(entries)}


def write(graph: dict, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO meta VALUES (?, ?)",
                   [("version", str(VERSION)), ("ledger_head", graph["head"]), ("ledger_entries", str(graph["seq"]))])
    db.executemany("INSERT INTO nodes VALUES (?, ?, ?, ?, ?)",
                   [(k, n["type"], n["company"], n["label"], json.dumps(n["props"], sort_keys=True, default=str))
                    for k, n in sorted(graph["nodes"].items())])
    db.executemany("INSERT INTO edges VALUES (?, ?, ?, ?)",
                   sorted((s, t, d, json.dumps(p, sort_keys=True)) for s, t, d, p in graph["edges"]))
    db.commit()
    db.close()
    tmp.replace(path)


class Graph:
    """Read side. `Graph.open(ledger)` rebuilds the SQLite file first if the ledger has moved on."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    @classmethod
    def open(cls, ledger: Ledger, path: Path | None = None) -> "Graph":
        path = path or ledger.root / "graph.sqlite"
        head = next(iter(reversed(list(ledger.entries()))), {}).get("hash", "")
        if not cls._fresh(path, head):
            write(project(ledger), path)
        return cls(sqlite3.connect(f"file:{path}?mode=ro", uri=True))

    @staticmethod
    def _fresh(path: Path, head: str) -> bool:
        if not path.exists():
            return False
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                meta = dict(db.execute("SELECT key, value FROM meta"))
        except sqlite3.DatabaseError:
            return False
        return meta.get("version") == str(VERSION) and meta.get("ledger_head") == head

    def node(self, nid: str) -> dict | None:
        row = self.db.execute("SELECT id, type, company, label, props FROM nodes WHERE id = ?", (nid,)).fetchone()
        return None if row is None else {"id": row[0], "type": row[1], "company": row[2], "label": row[3],
                                         **json.loads(row[4])}

    def _nodes(self, ids) -> list[dict]:
        """Nodes for edge endpoints; an edge to an id with no node (e.g. a cited claim id) is skipped."""
        return [n for (i,) in ids if (n := self.node(i)) is not None]

    def out(self, nid: str, typ: str) -> list[dict]:
        return self._nodes(self.db.execute("SELECT dst FROM edges WHERE src = ? AND type = ?", (nid, typ)))

    def into(self, nid: str, typ: str) -> list[dict]:
        return self._nodes(self.db.execute("SELECT src FROM edges WHERE dst = ? AND type = ?", (nid, typ)))

    def stats(self) -> dict:
        return {"nodes": dict(self.db.execute("SELECT type, count(*) FROM nodes GROUP BY type ORDER BY type")),
                "edges": dict(self.db.execute("SELECT type, count(*) FROM edges GROUP BY type ORDER BY type"))}

    def conflicts(self, company: str | None = None) -> list[tuple[dict, dict]]:
        rows = self.db.execute("SELECT e.src, e.dst FROM edges e JOIN nodes n ON n.id = e.src "
                               "WHERE e.type = 'CONFLICTS' AND (? IS NULL OR n.company = ?) ORDER BY e.src",
                               (company, company))
        pairs = [(self.node(s), self.node(d)) for s, d in rows]
        return [(a, b) for a, b in pairs if a is not None and b is not None]


# --- /why ---------------------------------------------------------------------------------

def _src(g: Graph, claim: dict) -> str:
    found = g.out(claim["id"], "EXTRACTED_FROM")
    return f"{found[0]['kind']} {found[0]['published']} {found[0]['uri']}" if found else "?"


def _claim_line(g: Graph, c: dict) -> str:
    return (f"{c['id'][6:]} [{c['status']}] {c['label']}\n"
            f"    “{c['quote'][:160]}”\n    ↳ {_src(g, c)}  (ledger #{c['seq']})")


def why_claim(g: Graph, cid: str) -> str:
    c = g.node(f"claim:{cid}")
    if not c:
        return f"No claim {cid} in the ledger."
    lines = [_claim_line(g, c),
             f"  decision: {c.get('action', '-')} · {c.get('nature', '-')} · lever {c.get('lever') or '-'}",
             f"  verdict: {c.get('verdict', '-')} by {c.get('verdict_by', '-')}"
             + (f" — {c['reasons'][0][:200]}" if c.get("reasons") else "")]
    for f in g.out(c["id"], "ASSERTS"):
        others = [o for o in g.into(f["id"], "ASSERTS") if o["id"] != c["id"]]
        lines.append(f"  fact {f['label']}: " + (", ".join(f"{o['value']} ({o['published']}, {o['status']})"
                                                         for o in others[:6]) or "no other claims"))
    for t in ("CONFIRMS", "CONFLICTS"):
        lines += [f"  {t.lower()} {o['id'][6:]} ({o['value']}, {o['published']})" for o in g.out(c["id"], t)]
        lines += [f"  {'confirmed' if t == 'CONFIRMS' else 'contradicted'} by {o['id'][6:]} ({o['value']}, {o['published']})"
                  for o in g.into(c["id"], t)]
    lines += [f"  cited by {p['id'][9:]} [{p['status']}] {p['label']}" for p in g.into(c["id"], "CITES")]
    lines += [f"  bears on {a['label']}" for a in g.out(c["id"], "BEARS_ON")]
    return "\n".join(lines)


def why_assumption(g: Graph, company: str, field: str, current=None, limit: int = 8) -> str:
    a = g.node(f"assumption:{company}.{field}")
    head = f"{company} {field}" + (f" = {current}" if current is not None else "")
    if not a:
        return f"{head}\n  nothing in the ledger bears on this assumption yet."
    lines = [head]
    proposals = sorted(g.into(a["id"], "CHANGES"), key=lambda p: p["seq"], reverse=True)
    decided = [p for p in proposals if p["status"] in {"approved", "rejected"}]
    lines.append("  changes on record:" if decided else "  no approved or rejected change on record; value is from the config as authored")
    for p in decided:
        pub = p.get("published") or {}
        lines.append(f"  • {p['id'][9:]} {p['status']} by {p['decided_by']} {p['decided_ts'][:10]}: "
                     f"{p.get('old_value')} → {p['new_value']} ({p['nature']}) — {p['note'][:120]}"
                     + (f"\n    published {pub.get('url') or pub.get('research_note', {}).get('url', '')}" if pub else ""))
        for c in g.out(p["id"], "CITES"):
            lines.append("    " + _claim_line(g, c).replace("\n", "\n    "))
            lines += [f"    ⚠ contradicted by {o['id'][6:]} ({o['value']}, {o['published']})"
                      for o in g.into(c["id"], "CONFLICTS")]
    open_ = [p for p in proposals if p["status"] in {"proposed", "failed_checks"}]
    lines += [f"  • open {p['id'][9:]} [{p['status']}] → {p['new_value']}: {p['rationale'][:140]}" for p in open_]
    evidence = sorted((c for c in g.into(a["id"], "BEARS_ON") if c["status"] == "verified"),
                      key=lambda c: (c["published"] or "", c["seq"]), reverse=True)
    if evidence:
        by_nature: dict[str, int] = defaultdict(int)
        for c in evidence:
            by_nature[c.get("nature") or "-"] += 1
        lines.append(f"  verified evidence bearing on it: {len(evidence)} claims ("
                     + ", ".join(f"{k} {v}" for k, v in sorted(by_nature.items())) + "), newest first:")
        lines += ["  " + _claim_line(g, c).replace("\n", "\n  ") for c in evidence[:limit]]
    conflicts = [(c, o) for c in evidence for o in g.out(c["id"], "CONFLICTS")]
    lines += [f"  ⚠ {c['id'][6:]} ({c['value']}, {c['published']}) conflicts with {o['id'][6:]} ({o['value']}, {o['published']})"
              for c, o in conflicts]
    return "\n".join(lines)


def why_fact(g: Graph, company: str, metric: str, period: str) -> str:
    rows = g.db.execute("SELECT id FROM nodes WHERE type = 'fact' AND id LIKE ?",
                        (f"fact:{company}|{metric}|{normalise_period(period)}|%",)).fetchall()
    if not rows:
        return f"No claims for {company} {metric} {normalise_period(period)}."
    lines = []
    for (fid,) in rows:
        lines.append(g.node(fid)["label"])  # type: ignore[index]  # ids come from the nodes table
        claims = sorted(g.into(fid, "ASSERTS"), key=lambda c: (c["published"] or "", c["seq"]))
        lines += ["  " + _claim_line(g, c).replace("\n", "\n  ") for c in claims]
    return "\n".join(lines)


def why_valuation(g: Graph, run_id: str) -> str:
    v = g.node(f"valuation:{run_id}")
    if not v:
        return f"No valuation run {run_id}."
    lines = [f"{v['label']} — {v['status']}" + (f" at {v.get('step')}" if v["status"] != "complete" else "")]
    for prev in g.out(v["id"], "SUPERSEDES"):
        lines.append(f"  supersedes {prev['id'][10:]} (same inputs, newer filings)")
    if v.get("value_per_share"):
        lines.append("  value per share: " + ", ".join(
            f"{k} ${float(x):,.2f} (WACC {float(v['wacc'][k]):.2%}, g {float(v['terminal_growth'][k]):.2%})"
            for k, x in v["value_per_share"].items()))
    facts = {f["id"][6:]: f for f in g.out(v["id"], "USES")}
    def fact_line(label, ids):
        for fid in ids or []:
            f = facts.get(fid)
            if f:
                lines.append(f"  {label}: {f['label']}\n      ↳ {f['form']} filed {f['filed']} {f['accn']} {f['url']}")
    for k in ("debt", "cash", "minority_interest", "preferred", "operating_leases"):
        fact_line(k, (v.get("balance") or {}).get(k))
    fact_line("diluted shares", v.get("shares_fact_ids"))
    for a in v.get("adjustments") or []:
        lines.append(f"  adjustment {a['id']} {a['status']}: {a['period']} {float(a['amount']):+.1f} — {a['rationale']}")
    for d in v.get("decisions") or []:
        lines.append(f"  decision {d['target']} {d['decision']} by {d['by']} (ledger #{d['seq']}): {d['note']}")
    for c in g.out(v["id"], "CITES"):
        lines.append("  cites " + _claim_line(g, c).replace("\n", "\n  "))
    restated = [f for f in facts.values() if f.get("restated")]
    if restated:
        lines.append(f"  ⚠ {len(restated)} reported fact(s) were restated; latest-filed values used")
    lines.append(f"  {len(facts)} XBRL facts in total: `grinos valuation memo {run_id}` has the full appendix")
    return "\n".join(lines)


WHY_USAGE = ("Usage: /why <claim_id> · /why <valuation run id> · /why <TICKER> valuations · /why <TICKER> <assumption> (e.g. /why POOL mid_cycle_ebit) · "
             "/why <TICKER> <metric> <period> (e.g. /why POOL revenue Q2 FY2026) · /why <TICKER> conflicts")


def why(g: Graph, arg: str, config_for=None) -> str:
    """Parse a /why argument. `config_for(ticker)` returns the company config dict or None."""
    parts = arg.split()
    if not parts:
        return WHY_USAGE
    if re.fullmatch(r"c_[0-9a-f]{12}", parts[0]):
        return why_claim(g, parts[0])
    if parts[0].startswith("val_"):
        return why_valuation(g, parts[0])
    company = parts[0].upper()
    if len(parts) == 1:
        return WHY_USAGE
    if parts[1] in ("valuation", "valuations"):
        vals = g._nodes(g.db.execute("SELECT id FROM nodes WHERE type = 'valuation' AND company = ? ORDER BY id",
                                     (company,)))
        return "\n".join(f"• {x['id'][10:]} {x['status']}" + (
            " · " + ", ".join(f"{k} ${float(y):,.2f}" for k, y in x["value_per_share"].items()) if x.get("value_per_share") else "")
            for x in vals) or f"No valuations of {company}."
    if parts[1] == "conflicts":
        pairs = g.conflicts(company)
        return "\n".join(f"⚠ {a['label']}: {a['value']} ({a['published']}) vs {b['value']} ({b['published']}) "
                         f"— {a['id'][6:]} / {b['id'][6:]}" for a, b in pairs) or f"No conflicting verified facts for {company}."
    if len(parts) >= 3:
        return why_fact(g, company, parts[1], " ".join(parts[2:]))
    cfg = config_for(company) if config_for else None
    return why_assumption(g, company, parts[1], (cfg or {}).get(parts[1]))
