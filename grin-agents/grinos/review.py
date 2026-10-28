"""
Human review of evidence: the reviewer sees each cited claim's exact span in its source, confirms or
disputes it, and adds their own perspective. All of it is in the ledger.

  evidence_review  confirm / dispute one cited claim, in the context of a proposal (or a driver)  actor: human
  feedback         a reviewer's perspective, correction, dispute or praise on any target           actor: human

A proposal can be approved only once every claim it cites is confirmed in its context
(proposals.decide_proposal). A dispute can also reject the claim outright (a human verdict), which
takes it out of the verified evidence the analyst may use.
"""
import re
from datetime import datetime
from urllib.parse import quote as urlquote

from grinos.evidence_check import locate
from grinos.ledger import Ledger
from grinos.pipeline import human_verdict
from grinos.schemas import Actor

EXCERPT_CHARS = 600
SOURCE_MAX = 20_000
DECISIONS = {"confirmed", "disputed"}
TARGET_KINDS = {"claim", "fact"}
FEEDBACK_TARGETS = {"proposal", "claim", "fact", "valuation_step", "driver", "coverage", "agent_message", "company"}
FEEDBACK_KINDS = {"perspective", "correction", "dispute", "praise"}
SEC = re.compile(r"^https://www\.sec\.gov/")


# --- read ---------------------------------------------------------------------------------------

class Index:
    """One pass over the ledger: everything the review views need."""

    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.claims: dict[str, dict] = {}
        self.sources: dict[str, dict] = {}
        self.decisions: dict[str, dict] = {}
        self.rules: dict[str, dict] = {}
        self.verdicts: dict[str, list[dict]] = {}
        self.reviews: list[dict] = []
        self.feedback: list[dict] = []
        for e in ledger.entries():
            p, st = e["payload"], e["stage"]
            if st == "claim":
                self.claims[p["claim_id"]] = p["claim"]
            elif st == "source":
                self.sources.setdefault(p["source"]["blob"], p["source"])
            elif st == "decision":
                self.decisions[p["claim_id"]] = p
            elif st == "rule_checks":
                self.rules[p["claim_id"]] = p
            elif st == "verdict":
                self.verdicts.setdefault(p["claim_id"], []).append(
                    {"verdict": p["verdict"], "by": f"{e['actor']['kind']}:{e['actor']['name']}", "ts": e["ts"],
                     "reasons": p.get("reasons", [])})
            elif st == "evidence_review":
                self.reviews.append(p | {"by": e["actor"]["name"], "ts": e["ts"], "seq": e["seq"]})
            elif st == "feedback":
                self.feedback.append(p | {"by": e["actor"]["name"], "ts": e["ts"], "seq": e["seq"]})

    def review(self, target_id: str, context_id: str) -> dict | None:
        return next((r for r in self.reviews if r["target_id"] == target_id and r["context_id"] == context_id), None)


def edgar_links(uri: str, quote: str) -> dict:
    """The filing on EDGAR, and a best-effort text-fragment link that scrolls the browser to the quote."""
    if not SEC.match(uri or ""):
        return {"edgar_url": None, "fragment_url": None}
    words = [w for w in re.split(r"[\s|]+", quote) if w and not re.fullmatch(r"[$%()\-]+", w)][:8]
    frag = urlquote(" ".join(words), safe="") if words else ""
    return {"edgar_url": uri, "fragment_url": f"{uri}#:~:text={frag}" if frag else uri}


def excerpt(text: str, quote: str, n: int = 0, chars: int = EXCERPT_CHARS) -> dict:
    spans = locate(text, quote)
    if not spans:
        return {"found": False, "n_matches": 0, "before": "", "match": "", "after": text[:chars],
                "raw_start": None, "raw_end": None, "total_len": len(text)}
    s, e = spans[min(n, len(spans) - 1)]
    return {"found": True, "n_matches": len(spans), "match_index": min(n, len(spans) - 1),
            "before": text[max(0, s - chars): s], "match": text[s:e], "after": text[e: e + chars],
            "raw_start": s, "raw_end": e, "total_len": len(text), "spans": spans}


def claim_detail(ix: Index, cid: str, context_id: str | None = None, match: int = 0) -> dict:
    c = ix.claims.get(cid)
    if c is None:
        raise KeyError(f"no claim {cid}")
    src = ix.sources.get(c["source_blob"]) or {}
    text = ix.ledger.get_blob(c["source_blob"]).decode("utf-8", errors="replace")
    verdicts = ix.verdicts.get(cid, [])
    human = [v for v in verdicts if v["by"].startswith("human:")]
    return {
        "claim_id": cid, "claim": c,
        "source": {"blob": c["source_blob"], "uri": src.get("uri"), "kind": src.get("kind"),
                   "published_at": src.get("published_at"), **edgar_links(src.get("uri", ""), c["quote"])},
        "excerpt": excerpt(text, c["quote"], match),
        "decision": {k: (ix.decisions.get(cid) or {}).get(k) for k in ("action", "nature", "materiality", "rationale")},
        "rule_checks": [{"id": k["id"], "ok": k["ok"], "severity": k["severity"], "message": k["message"]}
                        for k in (ix.rules.get(cid) or {}).get("checks", [])],
        "verdicts": verdicts,
        "verdict": (human or verdicts or [{"verdict": None}])[-1]["verdict"],
        "review": ix.review(cid, context_id) if context_id else None,
        "feedback": [f for f in ix.feedback if f["target_id"] == cid],
    }


def proposal_detail(ledger: Ledger, pid: str) -> dict:
    from grinos.proposals import proposal_state
    s = proposal_state(ledger).get(pid)
    if not s or not s["proposal"]:
        raise KeyError(f"no proposal {pid}")
    ix = Index(ledger)
    p, chk = s["proposal"]["payload"], (s["check"] or {}).get("payload", {})
    cites = [claim_detail(ix, cid, pid) for cid in p["proposal"]["cites"] if cid in ix.claims]
    state = {c["claim_id"]: (c["review"] or {}).get("decision") for c in cites}
    return {
        "proposal_id": pid, "company": p["company"], "run_id": s["proposal"]["run_id"],
        "field": p["proposal"]["field"], "nature": p["proposal"].get("nature"),
        "rationale": p["proposal"]["rationale"],
        "old_value": (chk.get("impact") or {}).get("old_value"),
        "new_value": chk.get("normalised_value", p["proposal"]["new_value"]),
        "impact": {k: (chk.get("impact") or {}).get(k) for k in ("ev_before", "ev_after", "ev_change_pct",
                                                                 "vps_before", "vps_after")},
        "check_ok": chk.get("ok"), "problems": chk.get("problems", []),
        "approval": (s["approval"] or {}).get("payload"),
        "cites": cites, "review_state": state,
        "all_confirmed": bool(cites) and all(v == "confirmed" for v in state.values()),
        "feedback": [f for f in ix.feedback if f["target_id"] == pid],
    }


def source_text(ledger: Ledger, blob: str, start: int = 0, end: int | None = None) -> str:
    """A slice of an ingested source, for "expand context". Only blobs the ledger records as sources."""
    ix = Index(ledger)
    if blob not in ix.sources:
        raise KeyError("not a recorded source")
    text = ledger.get_blob(blob).decode("utf-8", errors="replace")
    start = max(0, start)
    end = min(len(text), end if end is not None else start + SOURCE_MAX, start + SOURCE_MAX)
    return text[start:end]


def unreviewed(ledger: Ledger, pid: str, cites: list[str]) -> list[str]:
    """Cites without a `confirmed` review in this proposal's context."""
    ix = Index(ledger)
    return [c for c in cites if (ix.review(c, pid) or {}).get("decision") != "confirmed"]


def perspectives(ledger: Ledger, company: str, limit: int = 12) -> list[dict]:
    """The reviewer's recent perspectives and corrections on a company, newest last (analyst context)."""
    ix = Index(ledger)
    rows = [f for f in ix.feedback if f.get("company") == company and f["kind"] in ("perspective", "correction")]
    return [{"kind": f["kind"], "target": f"{f['target_kind']}:{f['target_id']}", "text": f["text"], "ts": f["ts"]}
            for f in rows[-limit:]]


# --- write --------------------------------------------------------------------------------------

def record_review(ledger: Ledger, target_kind: str, target_id: str, context_id: str, decision: str,
                  who: str, note: str, channel: str = "dashboard", reject_claim: bool = False) -> dict:
    if target_kind not in TARGET_KINDS:
        raise ValueError(f"target_kind must be one of {sorted(TARGET_KINDS)}")
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
    if decision == "disputed" and not note.strip():
        raise ValueError("a dispute needs a note: what is wrong with it")
    if target_kind == "claim" and target_id not in Index(ledger).claims:
        raise ValueError(f"no claim {target_id}")

    def first(entries: list[dict]) -> str | None:
        hit = next((e for e in entries if e["stage"] == "evidence_review" and e["payload"]["target_id"] == target_id
                    and e["payload"]["context_id"] == context_id), None)
        return f"{target_id} already {hit['payload']['decision']} for {context_id}" if hit else None

    entry = ledger.append_if(f"review_{context_id}", "evidence_review", Actor(kind="human", name=who, version="-"),
                             {"target_kind": target_kind, "target_id": target_id, "context_id": context_id,
                              "decision": decision, "note": note.strip(), "channel": channel},
                             refs=[f"{target_kind}:{target_id}"], guard=first)
    if decision == "disputed" and reject_claim and target_kind == "claim":
        human_verdict(ledger, target_id, "rejected", who, f"disputed in review of {context_id}: {note.strip()}",
                      channel=channel)
    return entry


def record_feedback(ledger: Ledger, target_kind: str, target_id: str, kind: str, text: str, who: str,
                    company: str | None = None, agent: str | None = None, channel: str = "dashboard") -> dict:
    if target_kind not in FEEDBACK_TARGETS:
        raise ValueError(f"target_kind must be one of {sorted(FEEDBACK_TARGETS)}")
    if kind not in FEEDBACK_KINDS:
        raise ValueError(f"kind must be one of {sorted(FEEDBACK_KINDS)}")
    if not text.strip():
        raise ValueError("feedback needs text")
    return ledger.append(f"feedback_{datetime.now():%Y%m%d}", "feedback", Actor(kind="human", name=who, version="-"),
                         {"target_kind": target_kind, "target_id": target_id, "kind": kind, "text": text.strip(),
                          "company": company, "agent": agent, "channel": channel},
                         refs=[f"{target_kind}:{target_id}"])
