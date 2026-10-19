"""Eval the eval: the validator accepts a faithful draft and catches each failure the gates count."""
import importlib.util
from pathlib import Path

import pytest

from grinos.narrative import check, narrate

# Loaded by path under its own name: every eval has a `cases` module, and pytest shares one session.
_spec = importlib.util.spec_from_file_location("memo_writer_cases", Path(__file__).parents[1] / "cases.py")
_cases = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cases)
CASES = _cases.CASES



def oracle(sh):
    """A hand-written draft that uses only sheet numbers and ids."""
    b, rd = sh["items"]["scenario:base"], sh["items"]["reverse_dcf"]
    return {"paragraphs": [
        {"text": f"The base case values {sh['company']} at ${b['value_per_share_usd']} a share with a WACC of "
                 f"{b['wacc_pct']}% and terminal growth of {b['terminal_growth_pct']}%.", "cites": ["scenario:base"]},
        {"text": f"The market price of ${rd['market_price_usd']} implies revenue growth of "
                 f"{rd['implied_revenue_growth_pct']}% a year.", "cites": ["reverse_dcf"]},
        {"text": "A further restatement would weaken the base year.", "cites": ["disconfirming:1"]}]}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_oracle_passes(case):
    assert check(case["sheet"], oracle(case["sheet"])) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["paragraphs"][0].update(text="Revenue will reach $5,000 million."), "not in the valuation"),
    (lambda d: d["paragraphs"][0].update(text="We recommend investors buy the shares."), "recommendation language"),
    (lambda d: d["paragraphs"][0].update(cites=["c_ffffffffffff"]), "unknown ids"),
    (lambda d: d["paragraphs"][0].update(cites=[]), "cites nothing"),
    (lambda d: d["paragraphs"].pop(), "disconfirming"),
    (lambda d: d["paragraphs"].pop(1), "reverse DCF"),
    (lambda d: d["paragraphs"][0].update(text="Our price target is the bull value."), "recommendation language"),
])
def test_validator_catches(mutate, expect):
    d = oracle(CASES[0]["sheet"])
    mutate(d)
    assert any(expect in p for p in check(CASES[0]["sheet"], d))


def test_signed_sheet_numbers_may_be_stated_as_magnitudes():
    sh = CASES[0]["sheet"]
    d = oracle(sh)
    d["paragraphs"][0]["text"] += f" That is {abs(sh['items']['scenario:base']['vs_price_pct'])}% below the market price."
    assert check(sh, d) == []


def test_dates_are_not_read_as_figures():
    d = oracle(CASES[0]["sheet"])
    d["paragraphs"][0]["text"] += " As of 2026-06-30, FY ending 2025-12-31."
    assert check(CASES[0]["sheet"], d) == []


def test_silent_system_and_loop_bound(monkeypatch):
    import grinos.narrative as N
    sh = CASES[0]["sheet"]
    assert check(sh, None) == ["no paragraphs"]
    calls = []

    def fake(name, message, schema, model="sonnet"):
        calls.append(message)
        return {"paragraphs": [{"text": "Buy now.", "cites": []}]}, {}
    monkeypatch.setattr(N, "sheet", lambda ctx: sh)
    text, trace = narrate(object(), call=fake)
    assert text is None and len(calls) == N.MAX_ATTEMPTS == len(trace["attempts"])
    assert "rejected for" in calls[1]                         # the retry carries the validator's feedback
