"""
Deterministic verifier rules: what can be proven about a claim from the source bytes alone.

    reject  quote_not_in_source   quote is not verbatim in the source (after whitespace/quote normalisation)
    reject  value_not_in_quote    claimed value does not appear in the quote, unit-aware; a bare number
                                  under an "(In thousands)" table header also counts in thousands
    reject  lookahead             source published after the valuation as-of date
    warn    sign_differs          the quote has the value with the opposite sign ("cash used", "(36,569)");
                                  the verifier agent judges the sign convention
    warn    period_not_in_context the claim's year isn't near the quote

Any reject ends the claim's path: the verifier agent is never asked to overrule a rule.
"""
import re
import unicodedata
from datetime import date

from grinos.schemas import Claim, Source

CONTEXT_CHARS = 400
REL_TOL = 0.005

_PUNCT = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                        "–": "-", "—": "-", "−": "-", " ": " "})
_NUM = re.compile(
    r"(?P<neg>\()?\$?\s?(?P<n>\d[\d,]*(?:\.\d+)?)\s?"
    r"(?P<scale>%|percent|basis points?|bps\b|bp\b|billion|bn|million|mn|m\b|days|x\b)?",
    re.IGNORECASE,
)


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_PUNCT)
    return re.sub(r"\s+", " ", text).strip().casefold()


_TABLE_SCALE = re.compile(r"\(in (thousands|millions)\b")


def table_scale(norm_src: str, pos: int) -> int:
    """1000 when the nearest table-scale header before `pos` says "(In thousands)", else 1."""
    headers = list(_TABLE_SCALE.finditer(norm_src, 0, max(pos, 0)))
    return 1000 if headers and headers[-1][1] == "thousands" else 1


def numbers_in(text: str, unit: str | None, per: int = 1) -> list[float]:
    """Every value the text could mean, expressed in `unit` (rates as decimals, money in $M).
    `per` is a table scale: bare numbers are also read as that many units (thousands → /1000)."""
    out: list[float] = []
    for m in _NUM.finditer(text):
        n = float(m["n"].replace(",", ""))
        if m["neg"]:
            n = -n
        scale = (m["scale"] or "").lower()
        if unit == "pct":
            if scale in {"%", "percent"}:
                out.append(n / 100)
            elif scale.startswith("basis point") or scale in {"bps", "bp"}:
                out.append(n / 10_000)
        elif unit == "usd_m":
            if scale in {"billion", "bn"}:
                out.append(n * 1000)
            elif scale not in {"%", "percent", "days", "x", "bps", "bp"} and not scale.startswith("basis"):
                out.append(n)
                if per != 1 and not scale:
                    out.append(n / per)
        else:
            if scale not in {"%", "percent"}:
                out.append(n)
                if per != 1 and unit == "count_m" and not scale:
                    out.append(n / per)
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(abs(b) * REL_TOL, 1e-9)


def _check(cid: str, ok: bool, severity: str, message: str) -> dict:
    return {"id": cid, "ok": ok, "severity": severity, "message": message}


def check_claim(claim: Claim, source: Source, source_text: str, as_of: date) -> dict:
    checks = []
    norm_src, norm_quote = normalise(source_text), normalise(claim.quote)
    pos = norm_src.find(norm_quote) if norm_quote else -1

    checks.append(_check("quote_not_in_source", pos >= 0, "reject",
                         "quote found verbatim" if pos >= 0 else "quote does not appear in the source"))

    if claim.value is not None:
        cands = numbers_in(claim.quote, claim.unit, table_scale(norm_src, pos))
        ok = any(_close(c, claim.value) for c in cands)
        flipped = not ok and any(_close(-c, claim.value) for c in cands)
        checks.append(_check("value_not_in_quote", ok or flipped, "reject",
                             f"value {claim.value} {claim.unit} "
                             + ("found in quote" if ok or flipped else f"not in quote (quote yields {cands})")))
        if flipped:
            checks.append(_check("sign_differs", False, "warn",
                                 f"quote has {-claim.value}; check the sign convention"))

    ok = source.published_at <= as_of
    checks.append(_check("lookahead", ok, "reject",
                         f"source published {source.published_at}, as-of {as_of}"))

    if pos >= 0:
        years = re.findall(r"(?:19|20)\d\d", claim.period)
        window = norm_src[max(0, pos - CONTEXT_CHARS): pos + len(norm_quote) + CONTEXT_CHARS]
        ok = all(y in window for y in years)
        checks.append(_check("period_not_in_context", ok, "warn",
                             f"period {claim.period} " + ("appears" if ok else "does not appear") + " near the quote"))

    rejected = [c for c in checks if c["severity"] == "reject" and not c["ok"]]
    return {"claim_id": claim.id, "checks": checks, "rules_verdict": "rejected" if rejected else "pass"}


def context_window(source_text: str, quote: str, chars: int = 1500) -> str:
    """The part of the source the verifier agent reads: around the quote, or the whole thing if short."""
    if len(source_text) <= chars * 3:
        return source_text
    i = normalise(source_text).find(normalise(quote))
    if i < 0:
        return source_text[: chars * 2]
    # Normalisation shifts offsets slightly; a generous window absorbs it.
    return source_text[max(0, i - chars): i + len(quote) + chars]
