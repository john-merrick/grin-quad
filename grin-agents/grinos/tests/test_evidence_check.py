from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from grinos.evidence_check import check_claim, context_window, locate, normalise, normalise_with_map, numbers_in
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


# --- locate: exact raw spans for highlighting --------------------------------------------------

ALPHABET = st.sampled_from(list("ab Z9|$%().,\n\t") + [" ", "’", "“", "–", "−",
                                                       "ß", "ﬁ", "…", "  "])


@settings(max_examples=300, deadline=None)
@given(st.lists(ALPHABET, max_size=60).map("".join))
def test_normalise_with_map_matches_normalise(text):
    norm, idx = normalise_with_map(text)
    assert norm == normalise(text) and len(idx) == len(norm)
    assert idx == sorted(idx) and all(0 <= i < len(text) for i in idx)


@settings(max_examples=300, deadline=None)
@given(st.lists(ALPHABET, min_size=1, max_size=40).map("".join), st.integers(0, 40), st.integers(1, 20))
def test_located_span_normalises_to_the_quote(text, start, length):
    quote = text[start: start + length]
    if not normalise(quote):
        return
    spans = locate(text, quote)
    assert spans and all(normalise(text[s:e]) == normalise(quote) for s, e in spans)


def test_locate_finds_a_quote_across_reflowed_whitespace_and_typography():
    raw = "Net sales   for fiscal\n2025 were $1,000.0 million — a record. SG&A | $ 3,931 | 23.8%"
    (s, e), = locate(raw, "net sales for fiscal 2025 were $1,000.0 million - a record")
    assert raw[s:e] == "Net sales   for fiscal\n2025 were $1,000.0 million — a record"
    (s, e), = locate(raw, "SG&A | $ 3,931 | 23.8%")
    assert raw[s:e] == "SG&A | $ 3,931 | 23.8%"
    assert locate(raw, "not in the source") == []
    assert len(locate("x 1 x 1 x", "x 1")) == 2


def test_locate_falls_back_when_nfkc_composes_across_characters():
    raw = "Caf" + "é revenue was $5 million."          # e + combining acute composes to é
    (s, e), = locate(raw, "café revenue was $5 million")
    assert normalise(raw[s:e]) == normalise("café revenue was $5 million")


def test_context_window_is_centred_on_the_exact_match():
    raw = ("filler " * 1000) + "The quoted sentence." + (" tail" * 1000)
    w = context_window(raw, "the  quoted sentence.", chars=50)
    assert "The quoted sentence." in w and len(w) <= 50 * 2 + len("The quoted sentence.")
