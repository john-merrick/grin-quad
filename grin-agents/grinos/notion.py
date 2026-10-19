"""
Notion publishing: the finished product only. Nothing reaches Notion unless the proposal passed
its deterministic checks AND a human approved it. Every page carries the ledger hashes that
let anyone trace it back to the source bytes.

Flow:  build_publication() → var/outbox/<proposal_id>.json (+ ledger 'publish_prepared')
       push()              → Notion REST (needs NOTION_TOKEN) (+ ledger 'published' with page ids)

Without NOTION_TOKEN everything up to the outbox works; push() is the only network step.
"""
import contextlib
import fcntl
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from grinos.env import secret
from grinos.ledger import Ledger
from grinos.proposals import proposal_state
from grinos.schemas import Actor, canonical, sha256

API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
# LongGamma data sources (override with env vars if they move).
# Every property push() writes, per data source — notion-check verifies they exist.
WRITES = {
    "valuation_log": ["Change", "Field", "Old Value", "New Value", "Author", "Scenario", "Date", "Company", "Rationale Note"],
    "research_notes": ["Title", "Type", "Author", "Date", "Valuation Impact", "Val Impact %", "Company"],
    "agent_registry": ["Agent", "Purpose", "Interface", "Status", "Owner Script", "Build Order", "Writes To"],
    "companies": ["Ticker"],
}
DS = {
    "valuation_log": os.environ.get("NOTION_DS_VALUATION_LOG", "38e7e14d-a1b9-4889-b295-d9a2bccd9e3d"),
    "research_notes": os.environ.get("NOTION_DS_RESEARCH_NOTES", "cdefe2ae-3cf7-4938-bc41-72f0ead7ae7b"),
    "companies": os.environ.get("NOTION_DS_COMPANIES", "7db9fedb-ae19-4502-981b-777c8e0f9bd6"),
    "agent_registry": os.environ.get("NOTION_DS_AGENT_REGISTRY", "36a0f3f1-eacf-4e56-93fe-4d66f5083aea"),
}
SYSTEM = Actor(kind="system", name="notion-publisher", version="1")


class NotPublishable(RuntimeError):
    pass


# --- building (pure, offline) ---------------------------------------------------

def valuation_log_rows(company: str, impact: dict, today: str) -> list[dict]:
    """Map an engine before/after into the Valuation Log's existing Field options."""
    b, a = impact["before"], impact["after"]
    rows = []

    def row(field, old, new):
        rows.append({"Change": f"{company} {field}: {old:,.2f} → {new:,.2f}", "Field": field,
                     "Old Value": round(old, 4), "New Value": round(new, 4), "Author": "Agent",
                     "Scenario": "Base", "Date": today})

    if abs(a["sustainable_ebit"] - b["sustainable_ebit"]) > 1e-9:
        row("Sustainable EBIT", b["sustainable_ebit"], a["sustainable_ebit"])
    if abs(a["terminal_multiple"] - b["terminal_multiple"]) > 1e-9:
        row("Target Multiple", b["terminal_multiple"], a["terminal_multiple"])
    for i, label in enumerate(["EBIT FY1", "EBIT FY2", "EBIT FY3"]):
        if i < len(b["ebit_path"]) and abs(a["ebit_path"][i] - b["ebit_path"][i]) > 1e-6:
            row(label, b["ebit_path"][i], a["ebit_path"][i])
    if not rows:  # e.g. tax rate or working-capital days: the value moves but no tracked field does
        row("Model Rebuild", impact["ev_before"], impact["ev_after"])
    return rows


def build_publication(ledger: Ledger, proposal_id: str) -> dict:
    s = proposal_state(ledger).get(proposal_id)
    if not s or not s["proposal"]:
        raise NotPublishable(f"no proposal {proposal_id}")
    if not s["check"]["payload"]["ok"]:
        raise NotPublishable(f"{proposal_id} failed its checks")
    if not s["approval"] or s["approval"]["payload"]["decision"] != "approve":
        raise NotPublishable(f"{proposal_id} has not been approved by a human")
    if ledger.audit():
        raise NotPublishable("ledger fails audit; refusing to publish")

    prop, chk, appr = s["proposal"]["payload"], s["check"]["payload"], s["approval"]
    company, p, impact = prop["company"], prop["proposal"], chk["impact"]
    claims = {e["payload"]["claim_id"]: e for e in ledger.entries() if e["stage"] == "claim"}
    sources = {e["payload"]["source"]["blob"]: e["payload"]["source"] for e in ledger.entries() if e["stage"] == "source"}
    # The Valuation Log is read in your local time; ledger timestamps are UTC.
    today = datetime.fromisoformat(appr["ts"]).astimezone().date().isoformat()

    evidence = []
    for cid in p["cites"]:
        ce = claims[cid]
        c = ce["payload"]["claim"]
        evidence.append({"claim_id": cid, "statement": c["statement"], "quote": c["quote"],
                         "source": sources[c["source_blob"]]["uri"], "claim_entry_hash": ce["hash"]})

    change = impact["ev_change_pct"]
    note = {
        "Title": f"{company}: {p['field']} → {chk['normalised_value']} (agent proposal, approved)",
        "Type": "Thesis Update", "Author": "Agent", "Date": today,
        "Valuation Impact": "Up" if change > 0.0005 else "Down" if change < -0.0005 else "None",
        "Val Impact %": round(change, 4),           # decimal, per the Schema Contract (0.25 = 25%)
        "body": [
            ("h2", "Change"),
            ("p", f"{p['field']}: {impact['old_value']} → {chk['normalised_value']}  ({p['nature']})"),
            ("p", f"Enterprise value {impact['ev_before']:,.1f} → {impact['ev_after']:,.1f} ({change:+.1%}), "
                  f"computed by the distval engine."),
            ("h2", "Rationale"), ("p", p["rationale"]),
            ("h2", "Verified evidence"),
            *[("bullet", f"{e['statement']} — “{e['quote']}” [{e['source']}] claim {e['claim_id']} · ledger {e['claim_entry_hash'][:12]}")
              for e in evidence],
            ("h2", "Audit trail"),
            ("bullet", f"Proposal {proposal_id} · ledger {s['proposal']['hash'][:16]} · "
                       f"{s['proposal']['actor']['name']}@{s['proposal']['actor']['version'][:8]}/{s['proposal']['actor']['model']}"),
            ("bullet", f"Checks passed · ledger {s['check']['hash'][:16]}"),
            ("bullet", f"Approved by {appr['actor']['name']} via {appr['payload']['channel']} at {appr['ts']} · "
                       f"ledger {appr['hash'][:16]} — {appr['payload']['note']}"),
            ("p", "Reproduce: grinos trail <claim_id> · grinos audit"),
        ],
    }
    pub = {"proposal_id": proposal_id, "company": company, "research_note": note,
           "valuation_log": valuation_log_rows(company, impact, today),
           "ledger": {"proposal": s["proposal"]["hash"], "check": s["check"]["hash"], "approval": appr["hash"]}}
    pub["publication_hash"] = sha256(canonical(pub).encode())
    return pub


def prepare(ledger: Ledger, proposal_id: str) -> Path:
    with _publish_lock(ledger, proposal_id):
        return _prepare(ledger, proposal_id)


def _prepare(ledger: Ledger, proposal_id: str) -> Path:
    if proposal_state(ledger).get(proposal_id, {}).get("published"):
        raise NotPublishable(f"{proposal_id} is already published")
    pub = build_publication(ledger, proposal_id)
    outbox = ledger.root / "outbox"
    outbox.mkdir(exist_ok=True)
    path = outbox / f"{proposal_id}.json"
    path.write_text(json.dumps(pub, indent=2))
    ledger.append(f"publish_{proposal_id}", "publish_prepared", SYSTEM,
                  {"proposal_id": proposal_id, "publication_hash": pub["publication_hash"], "outbox": str(path)},
                  refs=[f"entry:{pub['ledger']['approval']}"])
    return path


# --- pushing (network) -------------------------------------------------------------

def _request(method: str, path: str, body: dict | None, token: str) -> dict:
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _text(s: str) -> list[dict]:
    return [{"type": "text", "text": {"content": s[i:i + 1900]}} for i in range(0, len(s), 1900)] or \
           [{"type": "text", "text": {"content": ""}}]


def _blocks(body: list[tuple[str, str]]) -> list[dict]:
    kinds = {"h2": "heading_2", "p": "paragraph", "bullet": "bulleted_list_item", "todo": "to_do"}
    return [{"object": "block", "type": kinds[k],
             kinds[k]: {"rich_text": _text(t)} | ({"checked": False} if k == "todo" else {})} for k, t in body]


def find_company(ticker: str, token: str, request=_request) -> str | None:
    res = request("POST", f"/data_sources/{DS['companies']}/query",
                  {"filter": {"property": "Ticker", "rich_text": {"equals": ticker}}, "page_size": 1}, token)
    hits = res.get("results", [])
    return hits[0]["id"] if hits else None


@contextlib.contextmanager
def _publish_lock(ledger: Ledger, proposal_id: str):
    """One publisher per proposal across processes (double-click, Telegram + dashboard)."""
    (ledger.root / "outbox").mkdir(exist_ok=True)
    with open(ledger.root / "outbox" / f"{proposal_id}.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def push(ledger: Ledger, proposal_id: str, token: str | None = None, request=_request) -> dict:
    with _publish_lock(ledger, proposal_id):
        return _push(ledger, proposal_id, token, request)


def _push(ledger: Ledger, proposal_id: str, token: str | None, request) -> dict:
    token = token or secret("NOTION_TOKEN")
    if not token:
        raise NotPublishable("NOTION_TOKEN not set; the publication is in var/outbox, ready to push")
    if proposal_state(ledger)[proposal_id]["published"]:
        raise NotPublishable(f"{proposal_id} is already published")
    pub = json.loads((ledger.root / "outbox" / f"{proposal_id}.json").read_text())
    # The outbox is a convenience copy; the ledger is the truth. Refuse if they disagree.
    if canonical(pub) != canonical(build_publication(ledger, proposal_id)):
        raise NotPublishable("outbox file differs from what the ledger produces; re-run prepare")

    company_id = find_company(pub["company"], token, request)
    if not company_id:
        # A Valuation Log row with no company is an orphan in the decision record: refuse.
        raise NotPublishable(f"no row with Ticker {pub['company']!r} in Notion Companies; add the company "
                             "there first, then publish again")
    rel = [{"id": company_id}]
    n = pub["research_note"]
    note_page = request("POST", "/pages", {
        "parent": {"type": "data_source_id", "data_source_id": DS["research_notes"]},
        "properties": {
            "Title": {"title": _text(n["Title"])}, "Type": {"select": {"name": n["Type"]}},
            "Author": {"select": {"name": n["Author"]}}, "Date": {"date": {"start": n["Date"]}},
            "Valuation Impact": {"select": {"name": n["Valuation Impact"]}},
            "Val Impact %": {"number": n["Val Impact %"]}, "Company": {"relation": rel},
        },
        "children": _blocks([tuple(b) for b in n["body"]]),
    }, token)

    log_pages = []
    for r in pub["valuation_log"]:
        log_pages.append(request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": DS["valuation_log"]},
            "properties": {
                "Change": {"title": _text(r["Change"])}, "Field": {"select": {"name": r["Field"]}},
                "Old Value": {"number": r["Old Value"]}, "New Value": {"number": r["New Value"]},
                "Author": {"select": {"name": r["Author"]}}, "Scenario": {"select": {"name": r["Scenario"]}},
                "Date": {"date": {"start": r["Date"]}}, "Company": {"relation": rel},
                "Rationale Note": {"relation": [{"id": note_page["id"]}]},
            },
        }, token))

    entry = ledger.append_if(f"publish_{proposal_id}", "published", SYSTEM, {
        "proposal_id": proposal_id, "publication_hash": pub["publication_hash"],
        "research_note": {"id": note_page["id"], "url": note_page.get("url")},
        "valuation_log": [{"id": p["id"], "url": p.get("url")} for p in log_pages],
        "company_page": company_id},
        guard=lambda es: f"{proposal_id} published concurrently" if any(
            e["stage"] == "published" and e["payload"]["proposal_id"] == proposal_id for e in es) else None)
    return entry["payload"]


# --- agent registry (system metadata; prepared, pushed only on request) ---------------

def registry_rows(agents_dir: Path, status: dict[str, str]) -> list[dict]:
    """status: agent → Live | Paused | Planned (from evals/STATUS.json via cli.eval_status)."""
    writes = {"mid-cycle-analyst": ["Valuation Log", "Research Notes"]}
    rows = []
    for i, f in enumerate(sorted(agents_dir.glob("*.md")), start=1):
        meta = dict(line.split(": ", 1) for line in f.read_text().split("---")[1].strip().splitlines() if ": " in line)
        rows.append({"Agent": meta["name"], "Purpose": meta.get("description", ""), "Interface": "Python API",
                     "Status": status.get(meta["name"], "Planned"),
                     "Owner Script": f"grin-agents/agents/{f.name}", "Writes To": writes.get(meta["name"], []),
                     "Build Order": i})
    return rows


def push_registry(rows: list[dict], token: str | None = None, request=_request) -> list[str]:
    """Create or update Agent Registry rows, matched by Agent title. Returns page ids."""
    token = token or secret("NOTION_TOKEN")
    if not token:
        raise NotPublishable("NOTION_TOKEN not set")
    ids = []
    for r in rows:
        props = {
            "Agent": {"title": _text(r["Agent"])}, "Purpose": {"rich_text": _text(r["Purpose"])},
            "Interface": {"select": {"name": r["Interface"]}}, "Status": {"select": {"name": r["Status"]}},
            "Owner Script": {"rich_text": _text(r["Owner Script"])}, "Build Order": {"number": r["Build Order"]},
            "Writes To": {"multi_select": [{"name": w} for w in r["Writes To"]]},
        }
        hit = request("POST", f"/data_sources/{DS['agent_registry']}/query",
                      {"filter": {"property": "Agent", "title": {"equals": r["Agent"]}}, "page_size": 1}, token)
        if hit.get("results"):
            page = request("PATCH", f"/pages/{hit['results'][0]['id']}", {"properties": props}, token)
        else:
            page = request("POST", "/pages", {"parent": {"type": "data_source_id",
                                                         "data_source_id": DS["agent_registry"]},
                                              "properties": props}, token)
        ids.append(page["id"])
    return ids


def check_access(token: str | None = None, request=_request) -> dict:
    """Read-only: who the integration is, which LongGamma data sources it can see, and a Companies
    query in exactly the form publishing uses. Writes nothing."""
    token = token or secret("NOTION_TOKEN")
    if not token:
        raise NotPublishable("NOTION_TOKEN not set")
    out: dict = {}
    try:
        me = request("GET", "/users/me", None, token)
        out["integration"] = me.get("name") or me.get("bot", {}).get("workspace_name") or me.get("id")
    except urllib.error.HTTPError as exc:
        return {"integration": None, "error": f"token rejected: HTTP {exc.code}"}
    out["data_sources"] = {}
    for name, ds in DS.items():
        try:
            meta = request("GET", f"/data_sources/{ds}", None, token)
            title = "".join(t.get("plain_text", "") for t in meta.get("title", [])) or name
            missing = [p for p in WRITES.get(name, []) if p not in meta.get("properties", {})]
            out["data_sources"][name] = {"ok": not missing, "title": title,
                                         "error": f"missing properties: {missing}" if missing else ""}
        except urllib.error.HTTPError as exc:
            out["data_sources"][name] = {"ok": False, "error": f"HTTP {exc.code} (not shared with the integration?)"}
    if out["data_sources"].get("companies", {}).get("ok"):
        res = request("POST", f"/data_sources/{DS['companies']}/query", {"page_size": 100}, token)
        tickers = []
        for page in res.get("results", []):
            t = page.get("properties", {}).get("Ticker", {}).get("rich_text", [])
            if t:
                tickers.append("".join(x.get("plain_text", "") for x in t))
        out["company_tickers"] = sorted(tickers)
    return out
