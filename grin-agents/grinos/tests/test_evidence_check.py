from datetime import date

import pytest

from grinos.evidence_check import check_claim, normalise, numbers_in
from grinos.schemas import Claim, Source

TEXT = ("Net sales for fiscal 2025 were $1,000.0 million, up 4.2%.\n"
        "Gross margin was 30.0% in fiscal 2025. We crossed $1.0 billion in sales. "
        "Operating loss was ($5.0) million.")
SRC = Source(blob="b" * 64, uri="x", kind="10-K", company="ACME", published_at=date(2026, 2, 20))


def _claim(**kw):
    base = dict(source_blob="b" * 64, company="ACME", metric="revenue", period="FY2025",
                value=1000.0, unit="usd_m", quote="Net sales for fiscal 2025 were $1,000.0 million",
                statement="s")
    return Claim(**(base | kw))


def _failed(result):
    return {c["id"] for c in result["checks"] if not c["ok"]}


def test_valid_claim_passes():
    r = check_claim(_claim(), SRC, TEXT, date(2026, 3, 1))
    assert r["rules_verdict"] == "pass" and not _failed(r)


def test_quote_matching_tolerates_whitespace_and_curly_quotes():
    assert normalise("Net  sales\nwere “up”") == normalise('net sales were "up"')


@pytest.mark.parametrize("text,unit,want", [
    ("30.0%", "pct", 0.30), ("$1.0 billion", "usd_m", 1000.0), ("$1,000.0 million", "usd_m", 1000.0),
    ("($5.0) million", "usd_m", -5.0), ("60 days", "days", 60.0),
    ("roughly 30 basis points a year", "pct", 0.003), ("up 45bps", "pct", 0.0045),
])
def test_numbers_in(text, unit, want):
    assert any(abs(v - want) < 1e-9 for v in numbers_in(text, unit)), numbers_in(text, unit)


def test_percent_not_read_as_money():
    assert numbers_in("up 4.2%", "usd_m") == []


@pytest.mark.parametrize("kw,failed", [
    ({"quote": "Net sales were a record $1,050.0 million"}, "quote_not_in_source"),
    ({"value": 1050.0}, "value_not_in_quote"),
    ({"metric": "gross_margin", "unit": "pct", "value": 30.0,
      "quote": "Gross margin was 30.0% in fiscal 2025."}, "value_not_in_quote"),
    ({"value": 1.0, "quote": "We crossed $1.0 billion in sales."}, "value_not_in_quote"),
])
def test_rule_rejections(kw, failed):
    r = check_claim(_claim(**kw), SRC, TEXT, date(2026, 3, 1))
    assert failed in _failed(r) and r["rules_verdict"] == "rejected"


def test_lookahead_rejected():
    r = check_claim(_claim(), SRC, TEXT, date(2026, 2, 1))
    assert "lookahead" in _failed(r)


def test_period_warning_does_not_reject():
    r = check_claim(_claim(period="FY2019"), SRC, TEXT, date(2026, 3, 1))
    assert "period_not_in_context" in _failed(r) and r["rules_verdict"] == "pass"


def test_qualitative_claim_skips_value_check():
    r = check_claim(_claim(value=None, unit=None), SRC, TEXT, date(2026, 3, 1))
    assert "value_not_in_quote" not in {c["id"] for c in r["checks"]}


TABLE = ("Net sales were $1.8 billion.\n(In thousands, except per share data)\n"
         "Long-term debt, net | 1,327,273 | 1,212,533\nDiluted | 36,132 | 37,407\n"
         "(In millions)\nRestricted cash | 12.5\n")


@pytest.mark.parametrize("quote,value,unit,ok", [
    ("Long-term debt, net | 1,327,273", 1327.273, "usd_m", True),     # thousands header applies
    ("Diluted | 36,132", 36.132, "count_m", True),
    ("Long-term debt, net | 1,327,273", 1.327273, "usd_m", False),     # scale applied once only
    ("Restricted cash | 12.5", 0.0125, "usd_m", False),                # a later millions header wins
    ("Net sales were $1.8 billion.", 1.8, "usd_m", False),             # prose above the header: no scale
])
def test_table_scale_header(quote, value, unit, ok):
    r = check_claim(_claim(quote=quote, value=value, unit=unit, period="Q2 FY2026"), SRC, TABLE, date(2026, 3, 1))
    assert ("value_not_in_quote" not in _failed(r)) is ok, r["checks"]


def test_opposite_sign_warns_for_the_verifier_instead_of_rejecting():
    r = check_claim(_claim(quote="Operating loss was ($5.0) million.", value=5.0), SRC, TEXT, date(2026, 3, 1))
    assert r["rules_verdict"] == "pass" and _failed(r) == {"sign_differs"}
    r = check_claim(_claim(quote="Operating loss was ($5.0) million.", value=6.0), SRC, TEXT, date(2026, 3, 1))
    assert r["rules_verdict"] == "rejected"
