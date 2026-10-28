"""Tests that approve a proposal first confirm its cites, as the review gate requires on every channel."""
from grinos.proposals import proposal_state
from grinos.review import record_review


def confirm_all(ledger, pid, who="isaac"):
    for cid in proposal_state(ledger)[pid]["proposal"]["payload"]["proposal"]["cites"]:
        record_review(ledger, "claim", cid, pid, "confirmed", who, "matches the source")
