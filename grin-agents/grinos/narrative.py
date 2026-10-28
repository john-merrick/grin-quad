"""
Optional model-drafted memo narrative, held to the valuation's own numbers. The model only drafts
prose; every check is deterministic and a failed draft is dropped, never patched.

    sheet(ctx)                 the only facts the writer may use, each with a citable id
    check(sheet, draft)        problems: numbers not in the sheet, unknown or missing citations,
                               recommendation language, no disconfirming evidence, no reverse DCF
    narrate(ctx)               at most MAX_ATTEMPTS calls; returns validated Markdown or None
"""
import json
import re
from decimal import Decimal
from typing import Any

from grinos.llm import call_agent
from grinos.schemas import canonical, fence, sha256

MAX_ATTEMPTS = 2
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["paragraphs"],
    "properties": {"paragraphs": {"type": "array", "minItems": 2, "maxItems": 5, "items": {
        "type": "object", "additionalProperties": False, "required": ["text", "cites"],
        "properties": {"text": {"type": "string"}, "cites": {"type": "array", "items": {"type": "string"}}}}}},
}
RECOMMENDATION = re.compile(
    r"\b(buy|sell|hold|overweight|underweight|outperform|underperform|accumulate|"
    r"price target|target price|recommend\w*|we would (own|avoid)|attractive entry|undervalued|overvalued)\b",
    re.IGNORECASE)
NUMBER = re.compile(r"(?<![\w.])-?\$?\d[\d,]*(?:\.\d+)?%?")
ISO_DATE = re.compile(r"\b(19|20)\d\d-\d\d-\d\d\b")     # dates are not financial figures


def _d(x) -> Decimal:
    return Decimal(str(x))


def sheet(ctx) -> dict:
    """Facts the narrative may state, keyed by citable id. Rates as percents, money in $ millions."""
    o, req = ctx.out, ctx.request
    items: dict[str, dict] = {}
    for n in ("bear", "base", "bull"):
        s, f = o["valuation"]["scenarios"][n], o["forecast"]["scenarios"][n]
        a = f["assumptions"]
        items[f"scenario:{n}"] = {
            "value_per_share_usd": round(_d(s["value_per_share"]), 2), "vs_price_pct": round(_d(s["vs_price"]) * 100, 1),
            "wacc_pct": round(_d(f["wacc"]["wacc"]) * 100, 2), "terminal_growth_pct": round(_d(a["terminal_growth"]) * 100, 2),
            "terminal_value_share_of_ev_pct": round(_d(s["terminal_share"]) * 100, 0),
            "revenue_growth_pct": [round(_d(x) * 100, 1) for x in a["revenue_growth"]],
            "ebit_margin_pct": [round(_d(x) * 100, 1) for x in a["ebit_margin"]],
            "enterprise_value_usd_m": round(_d(s["enterprise_value"]), 1), "equity_value_usd_m": round(_d(s["equity_value"]), 1)}
    rd = o["sensitivity"]["reverse_dcf"]
    items["reverse_dcf"] = {"market_price_usd": round(_d(req.price), 2),
                            "implied_revenue_growth_pct": None if rd["implied_revenue_growth"] is None
                            else round(_d(rd["implied_revenue_growth"]) * 100, 1),
                            "implied_ebit_margin_pct": None if rd["implied_ebit_margin"] is None
                            else round(_d(rd["implied_ebit_margin"]) * 100, 1)}
    latest = o["reconcile"]["reported"]["annual"][-1]
    for key in ("revenue", "operating_income"):
        if latest.get(key):
            items[latest[key]["fact_ids"][0]] = {"item": key, "period": latest["period"], "usd_m": round(_d(latest[key]["value"]), 1)}
    for section in ("drivers", "risks", "disconfirming"):
        for i, d in enumerate(o["evidence"]["sections"][section]):
            items[f"{section}:{i + 1}"] = {"text": d["text"], "source": d.get("source"), "verified": d.get("verified")}
    flags = [c["message"] for c in o["validate"]["checks"] if not c["ok"]]
    return {"company": o["resolve"]["name"], "ticker": req.ticker, "valuation_date": str(req.valuation_date),
            "items": items, "flags": flags}


def allowed_numbers(sh: dict) -> set[Decimal]:
    """Every number in the sheet, at the precision written or rounder; plus small integers for counts."""
    out = {Decimal(n) for n in range(0, 11)}

    def add(v):
        if isinstance(v, list):
            for x in v:
                add(x)
        elif isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
            d = abs(_d(v))                       # "48.3% below" states -48.3 as a magnitude
            for places in (0, 1, 2):
                out.add(round(d, places))
        elif isinstance(v, str):
            for m in NUMBER.finditer(v):
                out.add(abs(_d(m[0].strip("$%").replace(",", ""))))

    for item in sh["items"].values():
        for v in item.values():
            add(v)
    for f in sh["flags"]:
        add(f)
    out.add(_d(sh["valuation_date"][:4]))
    return out


def check(sh: dict, draft: dict | None) -> list[str]:
    if not draft or not draft.get("paragraphs"):
        return ["no paragraphs"]
    allowed, ids = allowed_numbers(sh), set(sh["items"])
    problems = []
    cited: set[str] = set()
    for i, p in enumerate(draft["paragraphs"], start=1):
        text = p["text"]
        if not p["cites"]:
            problems.append(f"paragraph {i} cites nothing")
        unknown = [c for c in p["cites"] if c not in ids]
        if unknown:
            problems.append(f"paragraph {i} cites unknown ids {unknown}")
        cited |= set(p["cites"]) & ids
        if m := RECOMMENDATION.search(text):
            problems.append(f"paragraph {i} uses recommendation language: '{m[0]}'")
        for m in NUMBER.finditer(ISO_DATE.sub(" ", text)):
            raw = m[0].strip("$%").replace(",", "")
            if re.fullmatch(r"(19|20)\d\d", raw):                # a year
                continue
            n = abs(_d(raw))
            if not any(abs(n - a) < Decimal("0.0001") for a in allowed):
                problems.append(f"paragraph {i} states {m[0]}, which is not in the valuation")
    if not any(c.startswith("disconfirming:") for c in cited) and any(k.startswith("disconfirming:") for k in ids):
        problems.append("no paragraph cites the disconfirming evidence")
    if "reverse_dcf" not in cited:
        problems.append("no paragraph cites the reverse DCF")
    return problems


def render(draft: dict) -> str:
    return "\n\n".join(f"{p['text']} " + " ".join(f"[{c}]" for c in p["cites"]) for p in draft["paragraphs"])


def message(sh: dict, feedback: list[str] | None = None) -> str:
    msg = ("Draft the narrative section of this valuation memo from the sheet below.\n\n"
           f"<valuation_sheet>\n{fence(json.dumps(sh, indent=2, default=str))}\n</valuation_sheet>")
    if feedback:
        msg += "\n\nYour previous draft was rejected for:\n" + "\n".join(f"- {f}" for f in feedback) + \
               "\nWrite a new draft that fixes every one of these."
    return msg


def narrate(ctx, model: str = "sonnet", call=call_agent) -> tuple[str | None, dict]:
    """Bounded: MAX_ATTEMPTS model calls at most. Returns (validated Markdown or None, trace).
    The trace (sheet hash, every attempt's problems and cost) goes into the ledger with the memo."""
    sh, feedback = sheet(ctx), None
    trace: dict[str, Any] = {"sheet_hash": sha256(canonical(sh).encode()), "model": model, "attempts": []}
    for _ in range(MAX_ATTEMPTS):
        draft, meta = call("memo-writer", message(sh, feedback), SCHEMA, model=model)
        feedback = check(sh, draft)
        trace["attempts"].append({"problems": feedback, "cost_usd": meta.get("cost_usd"),
                                  "session_id": meta.get("session_id")})
        if not feedback:
            return render(draft), trace
    return None, trace
