"""Records that flow through the evidence pipeline. Every one lands in the ledger."""
import hashlib
import json
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel

SourceKind = Literal["10-K", "10-Q", "transcript", "broker", "call", "news", "other"]
Unit = Literal["usd_m", "pct", "days", "x", "count_m"]
Metric = Literal[
    "revenue", "organic_growth", "pricing", "market_share",
    "gross_margin", "opex_pct_sales", "ebit", "ebit_margin",
    "dio", "dso", "dpo", "capex", "d_and_a", "tax_rate",
    "customer_retention", "net_debt", "shares_diluted",
    "guidance_revenue", "guidance_ebit", "guidance_margin",
    "macro_rate", "other",
]
Verdict = Literal["verified", "rejected", "needs_human"]


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_TAG_OPEN = re.compile(r"<(?=[/!?A-Za-z])")


def fence(text: str) -> str:
    """Untrusted text going inside a <tag> block of a prompt: can't close the tag or open a new one.
    Only a tag-like "<" is escaped, so the agent sees (and quotes) "SG&A", "<1%" and "a > b" verbatim."""
    return _TAG_OPEN.sub("&lt;", str(text))


def unfence(text: str) -> str:
    """Undo fence() on text an agent copied out of a fenced block (a claim's quote)."""
    return str(text).replace("&lt;", "<")


def attr(text: str) -> str:
    """Untrusted text going inside a tag attribute."""
    return fence(text).replace('"', "'")


class Source(BaseModel):
    blob: str               # sha256 of the exact source bytes in the blob store
    uri: str                # where it came from (EDGAR URL, file path, call id)
    kind: SourceKind
    company: str
    published_at: date      # when the market could first see it (lookahead guard)


class Claim(BaseModel):
    """One atomic fact as printed in a source. Derived numbers are computed by tools, not claimed."""

    source_blob: str
    company: str
    metric: Metric
    period: str             # "FY2025", "Q3 FY2026"
    value: float | None     # in `unit`; rates are decimals (0.296), not percents
    unit: Unit | None
    quote: str              # verbatim from the source
    statement: str          # the claim in plain words, as an analyst would use it
    segment: str | None = None   # e.g. "Industrial"; None = company-wide
    adjusted: bool = False       # True = non-GAAP / adjusted / "excluding" figure, labelled as such

    @property
    def id(self) -> str:
        return "c_" + sha256(canonical(self.model_dump(mode="json")).encode())[:12]


class Actor(BaseModel):
    """Who produced a ledger entry, precisely enough to replay it."""

    kind: Literal["rule", "agent", "human", "system"]
    name: str
    version: str            # rule module hash, or prompt sha256 for agents
    model: str | None = None
