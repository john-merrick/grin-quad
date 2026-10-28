"""
The dashboard's Home page: the guide (docs/guide.yaml), what needs Isaac right now, and whether each service
is up. Every check is quick and best effort: a slow or missing service shows as down, never breaks the page.
"""
import socket
import subprocess
import urllib.request
from pathlib import Path

import yaml

from grinos.ledger import Ledger

GUIDE = Path(__file__).parents[2] / "docs" / "guide.yaml"


def _tcp(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _http(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - down is down
        return False


def _running(pattern: str) -> bool:
    try:
        return subprocess.run(["pgrep", "-f", pattern], capture_output=True, timeout=2).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def status(ledger: Ledger) -> list[dict]:
    from grinos import voice
    problems = ledger.audit()
    return [
        {"name": "Ledger", "ok": not problems, "detail": "intact" if not problems else f"{len(problems)} problem(s): run grinos audit"},
        {"name": "Temporal", "ok": _tcp(7233), "detail": "workflows and agent queues", "url": "http://127.0.0.1:8233"},
        {"name": "Worker", "ok": _running("grinos worker"), "detail": "runs the agents' work on Temporal"},
        {"name": "Telegram bot", "ok": _running("grinos telegram"), "detail": "talk to the orchestrator from your phone"},
        {"name": "Langfuse", "ok": _http("http://127.0.0.1:3300/api/public/health"), "detail": "full traces", "url": "http://127.0.0.1:3300"},
        {"name": "VoiceStudio", "ok": voice.health() is not None, "detail": "voice notes and the voice room (optional)"},
    ]


def needs_you(ledger: Ledger) -> list[dict]:
    """Everything waiting on Isaac, each with where to act on it."""
    from grinos import coverage, evalwatch, improve, longgamma
    from grinos.proposals import pending
    from grinos.web.server import needs_human
    out = []

    def add(label: str, n: int, tab: str, detail: str = "") -> None:
        if n:
            out.append({"label": label, "count": n, "tab": tab, "detail": detail})
    add("proposals to review", len(pending(ledger)), "Pending", "check each cited claim in its source, then decide")
    add("claims needing your verdict", len(needs_human(ledger)), "Pending", "the verifier couldn't settle these")
    blocked = [r for r in coverage.runs(ledger) if r["status"] == "blocked"]
    add("coverage runs waiting on you", len(blocked), "Coverage", ", ".join(f"{r['ticker']} at {r['step']}" for r in blocked[:4]))
    proms = improve.promotions(ledger)
    add("agent improvements to approve", len(proms), "Agents", ", ".join(sorted({p["agent"] for p in proms})))
    import sys
    sys.path.insert(0, str(Path(__file__).parents[2] / "evals"))
    from run_all import EVALS
    slipped = [a for a in EVALS if evalwatch.compare(a).get("status") == "regressed"]
    add("agents whose evals slipped", len(slipped), "Agents", ", ".join(slipped))
    try:
        cap = longgamma.pending(ledger)
    except Exception:  # noqa: BLE001
        cap = []
    add("items not yet in Notion", len(cap), "Desk", "type /capture in the Desk to see them")
    return out


def home(ledger: Ledger) -> dict:
    guide = yaml.safe_load(GUIDE.read_text()) if GUIDE.exists() else {}
    return {"guide": guide, "status": status(ledger), "needs_you": needs_you(ledger)}
