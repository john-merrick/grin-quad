"""Tests for the distval CLI (distval/cli.py)."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from distval.cli import build_parser, main

# POOL FY2023 balance-sheet figures used across tests (in millions)
_POOL_ARGV = [
    "value",
    "POOL",
    "--net-debt", "1469",
    "--shares-diluted", "39.5",
    "--as-of", "2024-02-22",
]


class TestParser:
    def test_required_args_parsed(self):
        parser = build_parser()
        ns = parser.parse_args(_POOL_ARGV)
        assert ns.command == "value"
        assert ns.ticker == "POOL"
        assert ns.net_debt == Decimal("1469")
        assert ns.shares_diluted == Decimal("39.5")
        assert ns.minority_interest == Decimal("0")
        assert ns.price is None
        assert ns.as_of == date(2024, 2, 22)
        assert ns.save is False

    def test_optional_price_parsed(self):
        parser = build_parser()
        ns = parser.parse_args([*_POOL_ARGV, "--price", "335"])
        assert ns.price == Decimal("335")

    def test_save_flag(self):
        parser = build_parser()
        ns = parser.parse_args([*_POOL_ARGV, "--save"])
        assert ns.save is True

    def test_bad_date_raises(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["value", "POOL", "--net-debt", "1469",
                               "--shares-diluted", "39.5", "--as-of", "not-a-date"])

    def test_ingest_parser(self):
        parser = build_parser()
        ns = parser.parse_args(["ingest", "GWW", "--as-of", "2024-01-01", "--years", "5"])
        assert ns.command == "ingest"
        assert ns.ticker == "GWW"
        assert ns.as_of == date(2024, 1, 1)
        assert ns.years == 5
        assert ns.save is False

    def test_normalise_parser(self):
        parser = build_parser()
        ns = parser.parse_args(["normalise", "GWW", "--as-of", "2024-01-01"])
        assert ns.command == "normalise"
        assert ns.ticker == "GWW"
        assert ns.years == 10


class TestMain:
    def test_pool_exits_zero(self, capsys):
        rc = main(_POOL_ARGV)
        assert rc == 0

    def test_pool_output_contains_ticker(self, capsys):
        main(_POOL_ARGV)
        out = capsys.readouterr().out
        assert "POOL" in out

    def test_pool_output_contains_value_per_share(self, capsys):
        main(_POOL_ARGV)
        out = capsys.readouterr().out
        assert "Value per share" in out

    def test_pool_output_contains_enterprise_value(self, capsys):
        main(_POOL_ARGV)
        out = capsys.readouterr().out
        assert "Enterprise value" in out

    def test_pool_upside_shown_when_price_given(self, capsys):
        main([*_POOL_ARGV, "--price", "335"])
        out = capsys.readouterr().out
        assert "Upside" in out
        assert "Current price" in out

    def test_unknown_ticker_exits_nonzero(self, capsys):
        rc = main(["value", "NOTREAL", "--net-debt", "100", "--shares-diluted", "10"])
        assert rc != 0
        err = capsys.readouterr().err
        assert "error" in err.lower()

    def test_save_writes_snapshot(self, tmp_path, monkeypatch, capsys):
        import distval.record as record_mod
        monkeypatch.setattr(record_mod, "_SNAPSHOTS_DIR", tmp_path)

        rc = main([*_POOL_ARGV, "--save"])
        assert rc == 0

        snapshots = list(tmp_path.glob("pool_*.json"))
        assert len(snapshots) == 1

        payload = json.loads(snapshots[0].read_text())
        assert payload["ticker"] == "POOL"
        assert payload["industry"] == "distributor"
        assert "enterprise_value" in payload
        assert "value_per_share" in payload

    def test_save_snapshot_is_deterministic(self, tmp_path, monkeypatch):
        import distval.record as record_mod
        monkeypatch.setattr(record_mod, "_SNAPSHOTS_DIR", tmp_path)

        main([*_POOL_ARGV, "--save"])
        main([*_POOL_ARGV, "--save"])

        # Same inputs → same hash → same filename → only one file
        assert len(list(tmp_path.glob("pool_*.json"))) == 1

    def test_pool_valuation_sanity(self, capsys):
        """Value-per-share should be a plausible non-zero positive number."""
        from distval.loader import load_drivers
        from distval.engine import value
        from distval.macro import DEFAULT_MACRO

        engine_inputs = load_drivers("POOL")
        val = value(
            engine_inputs=engine_inputs,
            macro=DEFAULT_MACRO,
            net_debt=Decimal("1469"),
            minority_interest=Decimal("0"),
            shares_diluted=Decimal("39.5"),
            price=Decimal("335"),
            as_of=date(2024, 2, 22),
        )
        assert val.value_per_share > 0
        assert val.enterprise_value > 0
        assert val.equity_value > 0
        assert val.upside_pct is not None
        assert isinstance(val.upside_pct, float)
        assert val.industry == "distributor"
