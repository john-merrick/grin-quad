"""
Desk-router eval: which agents should answer a free-text message. `must` agents have to be
picked; `ok` agents are acceptable extras; anything else is a wrong pick. At most 3 agents.
"""

AGENTS = ["mid-cycle-analyst", "verifier", "decision-model", "config-auditor", "claim-extractor"]


def r(cid, text, must, ok=()):
    return {"id": cid, "text": text, "must": set(must), "ok": set(must) | set(ok)}


CASES = [
    r("why_rejected", "Why was the claim about 31.2% gross margin thrown out?", ["verifier"], ["decision-model"]),
    r("is_quote_real", "Is that 94% retention number actually in the transcript or did someone make it up?", ["verifier"]),
    r("move_multiple", "Should we raise the duration score for ACME after the competitor exit?", ["mid-cycle-analyst"], ["decision-model"]),
    r("mid_cycle_q", "What's your view on ACME's sustainable EBIT now?", ["mid-cycle-analyst"]),
    r("cyclical_or_not", "Is the Q4 margin expansion structural or just volume leverage?", ["decision-model"], ["mid-cycle-analyst"]),
    r("lifo_treatment", "How are we treating the LIFO charge, does it hit mid-cycle?", ["decision-model", "mid-cycle-analyst"], []),
    r("yaml_check", "Can you check pool.yaml before I run the valuation? I think the DIO is off.", ["config-auditor"]),
    r("wc_basis", "Are FAST's working capital days on a COGS basis in the config?", ["config-auditor"]),
    r("what_extracted", "What facts did you pull out of the Q4 call?", ["claim-extractor"], ["verifier"]),
    r("missed_fact", "Did the extraction miss the segment numbers from the 10-K?", ["claim-extractor"]),
    r("conference", "Everyone: walk me through what changed in ACME's valuation this week and how confident we are.",
      ["mid-cycle-analyst", "verifier"], ["decision-model"]),
    r("greeting", "morning team", ["mid-cycle-analyst"], ["verifier", "decision-model", "config-auditor", "claim-extractor"]),
]
