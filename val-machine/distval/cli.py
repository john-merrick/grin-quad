"""CLI entry point — distval <subcommand> [args]."""
import argparse
import sys
from datetime import date, datetime
from decimal import Decimal

from distval.engine import value as _value
from distval.loader import load_drivers
from distval.macro import DEFAULT_MACRO, MacroAssumptions
from distval.record import write_snapshot
from distval.schema import Valuation


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date {s!r}: expected YYYY-MM-DD")


def _add_ticker_and_date(p: argparse.ArgumentParser) -> None:
    p.add_argument("ticker", help="Company ticker")
    p.add_argument("--as-of", type=_parse_date, default=None, metavar="DATE",
                   help="Point-in-time date YYYY-MM-DD (default: today)")
    p.add_argument("--years", type=int, default=10, metavar="N",
                   help="Years of 10-K history to fetch (default: 10)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="distval",
        description="Systematic valuation engine for US public companies.",
    )
    sub = p.add_subparsers(dest="command", required=True,
                           metavar="{value,ingest,normalise}")

    # --- value ---
    val_p = sub.add_parser("value", help="Run a DCF valuation from companies/<ticker>.yaml.")
    val_p.add_argument("ticker", help="Ticker — must match companies/<ticker>.yaml")
    val_p.add_argument("--net-debt", type=Decimal, required=True, metavar="M",
                       help="Net debt in millions (total_debt − cash)")
    val_p.add_argument("--shares-diluted", type=Decimal, required=True, metavar="M",
                       help="Diluted shares outstanding in millions")
    val_p.add_argument("--minority-interest", type=Decimal, default=Decimal("0"), metavar="M",
                       help="Minority interest in millions (default: 0)")
    val_p.add_argument("--price", type=Decimal, default=None, metavar="USD",
                       help="Current share price for upside calculation (optional)")
    val_p.add_argument("--as-of", type=_parse_date, default=None, metavar="DATE",
                       help="Valuation date YYYY-MM-DD (default: today)")
    val_p.add_argument("--save", action="store_true",
                       help="Write a snapshot JSON to ./snapshots/")

    # --- ingest ---
    ing_p = sub.add_parser("ingest", help="Fetch 10-K history from SEC EDGAR and display it.")
    _add_ticker_and_date(ing_p)
    ing_p.add_argument("--save", action="store_true",
                       help="Write fetched financials as JSON to ./snapshots/")

    # --- normalise ---
    nor_p = sub.add_parser("normalise", help="Compute mid-cycle drivers and surface flags.")
    _add_ticker_and_date(nor_p)

    return p


def _print_valuation(val: Valuation) -> None:
    w = 52
    print(f"\n{'=' * w}")
    print(f"  {val.ticker}  |  {val.industry}  |  as of {val.as_of}")
    print(f"{'=' * w}")
    fcf_str = "  ".join(f"{float(f):,.0f}" for f in val.fcf_path)
    print(f"  Discount rate:        {val.discount_rate:.1%}")
    print(f"  Terminal multiple:    {val.terminal_multiple:.2f}x")
    print(f"  FCF path ($M):        [{fcf_str}]")
    print(f"  Terminal value:       ${float(val.terminal_value):>13,.1f}M")
    print(f"  Enterprise value:     ${float(val.enterprise_value):>13,.1f}M")
    print(f"  Net debt:             ${float(val.net_debt):>13,.1f}M")
    print(f"  Minority interest:    ${float(val.minority_interest):>13,.1f}M")
    print(f"  Equity value:         ${float(val.equity_value):>13,.1f}M")
    print(f"  Value per share:      ${float(val.value_per_share):>13.2f}")
    if val.price is not None:
        print(f"  Current price:        ${float(val.price):>13.2f}")
        sign = "+" if val.upside_pct and val.upside_pct > 0 else ""
        print(f"  Upside:               {sign}{val.upside_pct:.1f}%")
    print(f"{'=' * w}")
    print(f"  input hash:  {val.input_hash[:12]}")
    print(f"{'=' * w}\n")


def _cmd_value(args: argparse.Namespace) -> int:
    as_of = args.as_of or date.today()
    try:
        engine_inputs = load_drivers(args.ticker)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    macro = _resolve_macro(args.ticker.upper(), as_of)

    val = _value(
        engine_inputs=engine_inputs,
        macro=macro,
        net_debt=args.net_debt,
        minority_interest=args.minority_interest,
        shares_diluted=args.shares_diluted,
        price=args.price,
        as_of=as_of,
    )
    _print_valuation(val)

    if args.save:
        path = write_snapshot(val)
        print(f"snapshot written → {path}")

    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from distval.ingest import fetch_financials

    as_of = args.as_of or date.today()
    try:
        history = fetch_financials(args.ticker.upper(), as_of=as_of, years=args.years)
    except (ImportError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not history:
        print(f"error: no 10-K filings found for {args.ticker.upper()} as of {as_of}",
              file=sys.stderr)
        return 1

    w = 116
    print(f"\n{'=' * w}")
    print(f"  {args.ticker.upper()}  |  10-K history  |  as of {as_of}  (all $M)")
    print(f"{'=' * w}")
    print(f"  {'Year':>4}  {'Filed':>10}  {'Revenue':>10}  {'Gross Profit':>12}  "
          f"{'Op. Inc':>9}  {'D&A':>7}  {'CapEx':>7}  "
          f"{'Inventory':>10}  {'Recv':>8}  {'Pay':>8}  {'Debt':>8}  {'Cash':>8}")
    print(f"  {'-' * (w - 2)}")
    for f in history:
        M = 1_000_000
        print(
            f"  {f.fiscal_year:>4}  {str(f.filed_date):>10}"
            f"  {float(f.revenue)/M:>10,.1f}"
            f"  {float(f.gross_profit)/M:>12,.1f}"
            f"  {float(f.operating_income)/M:>9,.1f}"
            f"  {float(f.d_and_a)/M:>7,.1f}"
            f"  {float(f.capex)/M:>7,.1f}"
            f"  {float(f.inventory)/M:>10,.1f}"
            f"  {float(f.receivables)/M:>8,.1f}"
            f"  {float(f.payables)/M:>8,.1f}"
            f"  {float(f.total_debt)/M:>8,.1f}"
            f"  {float(f.cash)/M:>8,.1f}"
        )
    print(f"{'=' * w}\n")

    if args.save:
        import json
        from pathlib import Path
        out_dir = Path("snapshots")
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / f"ingest_{args.ticker.lower()}_{as_of}.json"
        payload = [f.model_dump(mode="json") for f in history]
        out_path.write_text(json.dumps(payload, indent=2, default=str))
        print(f"saved → {out_path}")

    return 0


def _cmd_normalise(args: argparse.Namespace) -> int:
    from distval.ingest import fetch_financials
    from distval.normalise import avg_wc_days, detect_flags, mean_gross_margin, mean_opex_pct

    as_of = args.as_of or date.today()
    try:
        history = fetch_financials(args.ticker.upper(), as_of=as_of, years=args.years)
    except (ImportError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if len(history) < 10:
        print(
            f"error: only {len(history)} year(s) of history for {args.ticker.upper()}; "
            "10 required for mid-cycle derivation",
            file=sys.stderr,
        )
        return 1

    w = 72
    print(f"\n{'=' * w}")
    print(f"  {args.ticker.upper()}  |  Normalisation summary  |  as of {as_of}")
    print(f"{'=' * w}")

    sorted_h = sorted(history, key=lambda f: f.fiscal_year)
    print(f"  {'Year':>4}  {'Rev ($M)':>10}  {'Gross Margin':>12}  {'Opex %':>7}"
          f"  {'DIO':>6}  {'DSO':>6}  {'DPO':>6}")
    print(f"  {'-' * (w - 2)}")
    for f in sorted_h:
        gm = float(f.gross_profit / f.revenue)
        opex_pct = float((f.gross_profit - f.operating_income) / f.revenue)
        dio = float(f.inventory / f.revenue * 365)
        dso = float(f.receivables / f.revenue * 365)
        dpo = float(f.payables / f.revenue * 365)
        print(
            f"  {f.fiscal_year:>4}  {float(f.revenue)/1_000_000:>10,.1f}"
            f"  {gm:>12.1%}  {opex_pct:>7.1%}"
            f"  {dio:>6.1f}  {dso:>6.1f}  {dpo:>6.1f}"
        )
    print(f"  {'-' * (w - 2)}")

    mid_gm = mean_gross_margin(history)
    mid_opex = mean_opex_pct(history)
    dio_avg, dso_avg, dpo_avg = avg_wc_days(history)

    print(f"\n  Mid-cycle gross margin:  {mid_gm:.1%}")
    print(f"  Mid-cycle opex %:        {mid_opex:.1%}")
    print(f"  Implied EBIT margin:     {mid_gm - mid_opex:.1%}")
    print(f"  Avg DIO / DSO / DPO:     {dio_avg:.1f} / {dso_avg:.1f} / {dpo_avg:.1f}")

    flags = detect_flags(history)
    print(f"\n  Flags:")
    if not flags.any_set():
        print("    [none]")
    else:
        for name, val in vars(flags).items():
            if val:
                print(f"    [!] {name}")

    # --- EV / EBIT history ---
    print(f"\n{'=' * w}")
    print(f"  EV / EBIT History  (fiscal-year-end price via yfinance)")
    print(f"{'=' * w}")
    try:
        from distval.normalise import compute_ev_ebit_history, ev_ebit_stats
        ev_points = compute_ev_ebit_history(sorted_h)
        if ev_points:
            print(f"  {'Year':>4}  {'EBIT ($M)':>10}  {'Net Debt ($M)':>13}  "
                  f"{'Mkt Cap ($M)':>12}  {'EV ($M)':>10}  {'EV/EBIT':>8}")
            print(f"  {'-' * (w - 2)}")
            for p in ev_points:
                print(
                    f"  {p.year:>4}  {p.ebit_m:>10,.1f}  {p.net_debt_m:>13,.1f}  "
                    f"{p.market_cap_m:>12,.1f}  {p.ev_m:>10,.1f}  {p.multiple:>7.1f}x"
                )
            print(f"  {'-' * (w - 2)}")
            s = ev_ebit_stats(ev_points)
            print(f"\n  Box & Whisker (n={s['n']}):")
            print(f"  {'Min':>7}  {'P25':>7}  {'Median':>7}  {'P75':>7}  {'Max':>7}  {'Mean':>7}")
            print(
                f"  {s['min']:>6.1f}x  {s['p25']:>6.1f}x  {s['median']:>6.1f}x  "
                f"{s['p75']:>6.1f}x  {s['max']:>6.1f}x  {s['mean']:>6.1f}x"
            )
            print(f"\n  Suggested market_avg_multiple: {s['median']:.1f}x  (median EV/EBIT)")
        else:
            print("  (no data — all years excluded or price unavailable)")
    except ImportError as exc:
        print(f"  (skipped — {exc})")
    except Exception as exc:
        print(f"  (EV/EBIT fetch failed — {exc})")

    print(f"\n{'=' * w}\n")
    return 0


def _load_env() -> None:
    """Load .env file if python-dotenv is installed (soft dependency)."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def _resolve_macro(ticker: str, as_of: date) -> MacroAssumptions:
    """
    Build MacroAssumptions from live data:
      - sustainable_risk_free_rate: 20-year avg of FRED GS10
      - equity_risk_premium: FRED_ERP env var (default 0.05)
      - market_avg_multiple: median EV/EBIT from company's own filing history

    Falls back gracefully — any failure prints a warning and uses defaults.
    """
    market_multiple = 15.0
    multiple_source = "default"

    try:
        from distval.ingest import fetch_financials
        from distval.normalise import compute_ev_ebit_history, ev_ebit_stats
        history = fetch_financials(ticker, as_of=as_of, years=10)
        if history:
            points = compute_ev_ebit_history(history)
            if points:
                stats = ev_ebit_stats(points)
                market_multiple = stats["median"]
                multiple_source = f"median of {stats['n']}-year EV/EBIT history"
    except Exception as exc:
        print(f"  Note: EV/EBIT multiple computation skipped ({exc}). Using {market_multiple:.1f}x.",
              file=sys.stderr)

    try:
        from distval.macro import fetch_macro
        macro = fetch_macro(market_avg_multiple=market_multiple)
        print(f"  Macro (live): RFR={macro.sustainable_risk_free_rate:.2%}  "
              f"ERP={macro.equity_risk_premium:.2%}  "
              f"Multiple={macro.market_avg_multiple:.1f}x  ({multiple_source})")
        return macro
    except Exception as exc:
        print(f"  Warning: FRED fetch failed ({exc}). Using default macro + {multiple_source}.",
              file=sys.stderr)
        return MacroAssumptions(market_avg_multiple=market_multiple)


def main(argv: list[str] | None = None) -> int:
    _load_env()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "value":
        return _cmd_value(args)
    if args.command == "ingest":
        return _cmd_ingest(args)
    if args.command == "normalise":
        return _cmd_normalise(args)
    return 1
