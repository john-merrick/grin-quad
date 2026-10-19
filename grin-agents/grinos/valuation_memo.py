"""
The dated investment memo for a valuation run: deterministic Markdown from the run's step outputs.
Every reported number carries its fact id [x_…]; the appendix maps each id to concept, period,
accession and filing URL. An optional model-drafted narrative is inserted only after validation.
"""
from datetime import date
from decimal import Decimal

DISCLAIMER = ("Analysis only. This memo is not investment advice or a recommendation: the workflow "
              "places no trades and issues no buy or sell action. A person decides what, if anything, to do.")


def m(v, places: int = 1) -> str:
    """Millions with thousands separators; negatives in parentheses."""
    if v is None:
        return "n/a"
    v = Decimal(str(v))
    s = f"{abs(v):,.{places}f}"
    return f"({s})" if v < 0 else s


def pct(v, places: int = 1) -> str:
    return "n/a" if v is None else f"{Decimal(str(v)) * 100:.{places}f}%"


def ps(v) -> str:
    return "n/a" if v is None else f"${Decimal(str(v)):,.2f}"


def refs(ids) -> str:
    return " ".join(f"[{i}]" for i in ids or [])


def _cell(item, places=1):
    return f"{m(item['value'], places)} {refs(item['fact_ids'])}" if item else "not reported"


def render(ctx, narrative: str | None = None) -> str:
    o, req = ctx.out, ctx.request
    res, rec = o["resolve"], o["reconcile"]["reported"]
    val, fc, sens = o["valuation"], o["forecast"], o["sensitivity"]
    L: list[str] = []
    add = L.append

    add(f"# {res['name']} ({req.ticker}): fundamental valuation")
    add("")
    add(f"**Valuation date** {req.valuation_date} · **Prepared** {date.today()} · **Market price supplied** "
        f"{ps(req.price)} · **Currency** USD, figures in millions except per share")
    add(f"**Run** `{ctx.run_id}` · **SEC snapshot** `{ctx.snap['version'][:16]}` (facts filed on or before "
        f"{req.valuation_date}; fetched {ctx.snap['fetched_at'][:10]}) · SIC {res['sic']} {res['sicDescription']}")
    add("")
    add(f"> {DISCLAIMER}")
    add("")

    add("## Result")
    add("")
    add("| scenario | WACC | terminal growth | enterprise value | equity value | value per share | vs price | terminal value share of EV |")
    add("|---|---|---|---|---|---|---|---|")
    for n in ("bear", "base", "bull"):
        s, f = val["scenarios"][n], fc["scenarios"][n]
        add(f"| {n} | {pct(f['wacc']['wacc'], 2)} | {pct(f['assumptions']['terminal_growth'], 2)} | {m(s['enterprise_value'])} | "
            f"{m(s['equity_value'])} | **{ps(s['value_per_share'])}** | {pct(s['vs_price'])} | {pct(s['terminal_share'], 0)} |")
    add("")
    rd = sens["reverse_dcf"]
    base = fc["scenarios"]["base"]["assumptions"]
    add(f"At {ps(req.price)}, the market price implies a constant five-year revenue growth of "
        f"**{pct(rd['implied_revenue_growth'])}** (base case: {', '.join(pct(g) for g in base['revenue_growth'])}), or a "
        f"constant EBIT margin of **{pct(rd['implied_ebit_margin'])}** (base: {', '.join(pct(x) for x in base['ebit_margin'])}), "
        "with everything else in the base case held.")
    add("")

    add("## What could invalidate this valuation")
    add("")
    for d in o["evidence"]["sections"]["disconfirming"]:
        add(f"- {d['text']} {_src(d)}")
    g_imp = rd["implied_revenue_growth"]
    if g_imp is not None:
        g_base = sum(base["revenue_growth"]) / len(base["revenue_growth"])
        side = "understates" if g_imp > g_base else "overstates"
        add(f"- The market price implies {pct(g_imp)} revenue growth a year against {pct(g_base)} in the base case. "
            f"Evidence that growth will run nearer the market-implied rate would mean the base case {side} value.")
    for c in o["validate"]["checks"]:
        if not c["ok"]:
            add(f"- Check `{c['id']}`: {c['message']}")
    for t, d in sorted(ctx.decisions.items()):
        if d["kind"] != "adjustment" and d["decision"] in ("accept", "value"):
            add(f"- Review item `{t}` was resolved by {d['by']} ({d['decision']}): {d['note']}")
    add("")

    if narrative:
        add("## Analyst narrative (model-drafted, citations checked)")
        add("")
        add(narrative)
        add("")

    for section, title in (("drivers", "Business drivers"), ("risks", "Risks")):
        add(f"## {title}")
        add("")
        items = o["evidence"]["sections"][section]
        if not items:
            add("_None recorded._")
        for d in items:
            add(f"- {d['text']} {_src(d)}")
        add("")
    add(f"Verified claims in the evidence pipeline for {req.ticker} available on the valuation date: "
        f"{o['evidence']['verified_claims_available']}.")
    add("")

    add("## Reported financials (as filed)")
    add("")
    years = rec["annual"]
    add("| | " + " | ".join(r["period"].replace("FY ending ", "FY ") for r in years) + " |")
    add("|---|" + "---|" * len(years))
    for key, label in (("revenue", "Revenue"), ("operating_income", "Operating income"), ("pretax_income", "Pretax income"),
                       ("income_tax", "Income tax"), ("d_and_a", "D&A"), ("capex", "Capex"),
                       ("receivables", "Receivables"), ("inventory", "Inventory"), ("payables", "Payables")):
        add(f"| {label} | " + " | ".join(_cell(r.get(key)) for r in years) + " |")
    add("")
    b = rec["balance"]
    add(f"Latest balance sheet {b['date']}: cash {_cell(b.get('cash'))}, debt {_cell(b.get('debt'))} "
        f"({b['debt']['formula'] if b.get('debt') else 'n/a'}), operating leases {_cell(b.get('operating_leases'))}. "
        f"Diluted shares {_cell(rec['diluted_shares'], 3)} ({rec['diluted_shares']['period'] if rec['diluted_shares'] else 'n/a'}).")
    add("")

    add("## Normalisation adjustments (separate from reported figures)")
    add("")
    adj = o["adjustments"]
    if adj["proposed"]:
        add("| id | origin | period | item | amount | status | decided by | rationale | source |")
        add("|---|---|---|---|---|---|---|---|---|")
        for p in adj["proposed"]:
            src = refs(p["source"].get("fact_ids")) or (p["source"].get("cited") or "")
            add(f"| `{p['id']}` | {p['origin']} | {p['period']} | {p['item']} | {m(p['amount'])} | {p['status']} | "
                f"{p['decided_by'] or '-'} | {p['rationale']} | {src} |")
    else:
        add("_No adjustments proposed._ One-off charges under 0.5% of operating income are not proposed.")
    add("")
    add("| period | reported operating income | approved adjustments | normalised | normalised margin |")
    add("|---|---|---|---|---|")
    for row in adj["normalised"]:
        add(f"| {row['period']} | {m(row['reported_operating_income'])} | {m(row['approved_adjustments'])} | "
            f"{m(row['normalised_operating_income'])} | {pct(row['normalised_margin'])} |")
    add("")

    add("## Forecast assumptions")
    add("")
    for n in ("bear", "base", "bull"):
        f = fc["scenarios"][n]
        a, w = f["assumptions"], f["wacc"]
        add(f"**{n}**: revenue growth {_series(a['revenue_growth'])}; EBIT margin {_series(a['ebit_margin'])}; "
            f"tax {_series(a['tax_rate'])}; net capex {_series(a['net_capex_pct_revenue'])} of revenue; "
            f"working capital {pct(a['nwc_pct_revenue'])} of revenue; terminal growth {pct(a['terminal_growth'], 2)}.  ")
        if w["method"] == "build-up":
            add(f"WACC {pct(w['wacc'], 2)} = {pct(1 - w['debt_weight'])} × cost of equity {pct(w['cost_of_equity'], 2)} "
                f"+ {pct(w['debt_weight'])} × after-tax cost of debt {pct(w['after_tax_cost_of_debt'], 2)} "
                f"(inputs {w['inputs']}; market equity value {m(fc['market_equity'])}).  ")
        else:
            add(f"WACC {pct(w['wacc'], 2)} (supplied).  ")
        for pv in f["provenance"]:
            add(f"_{pv['assumption']} from {pv['from']} {refs(pv.get('fact_ids'))}_  ")
        add("")

    add("## Valuation detail")
    add("")
    for n in ("bear", "base", "bull"):
        s = val["scenarios"][n]
        add(f"**{n}**")
        add("")
        add("| year | revenue | EBIT | NOPAT | net capex | Δ working capital | FCFF | PV |")
        add("|---|---|---|---|---|---|---|---|")
        for y in s["years"]:
            add(f"| {y['year']} | {m(y['revenue'])} | {m(y['ebit'])} | {m(y['nopat'])} | {m(y['net_capex'])} | "
                f"{m(y['delta_nwc'])} | {m(y['fcff'])} | {m(y['pv_fcff'])} |")
        add(f"| terminal | | | | | | {m(s['terminal_fcff'])} → TV {m(s['terminal_value'])} | {m(s['pv_terminal'])} |")
        add("")
    br = val["bridge"]
    add("## Equity bridge (base case)")
    add("")
    bs = val["scenarios"]["base"]
    add("| | USD m |")
    add("|---|---|")
    add(f"| Enterprise value | {m(bs['enterprise_value'])} |")
    add(f"| − Debt | {m(br['debt'])} |")
    add(f"| + Cash | {m(br['cash'])} |")
    add(f"| − Preferred | {m(br['preferred'])} |")
    add(f"| − Minority interest | {m(br['minority_interest'])} |")
    add(f"| − Operating leases | {m(br['other_claims'])} "
        f"({'treated as debt' if br['leases_treated_as_debt'] else 'excluded: lease cost is inside EBIT; reported ' + m(br['operating_leases'])}) |")
    add(f"| = Equity value | {m(bs['equity_value'])} |")
    add(f"| ÷ Diluted shares (m) | {m(br['diluted_shares'], 3)} |")
    add(f"| = Value per share | **{ps(bs['value_per_share'])}** |")
    add("")

    add("## Sensitivity: base-case value per share, WACC × terminal growth")
    add("")
    g = sens["grid"]
    add("| WACC \\ g | " + " | ".join(pct(x, 2) for x in g["terminal_growth"]) + " |")
    add("|---|" + "---|" * len(g["terminal_growth"]))
    for w, row in zip(g["wacc"], g["value_per_share"]):
        add(f"| {pct(w, 2)} | " + " | ".join(ps(v) if v is not None else "n/a (g ≥ WACC)" for v in row) + " |")
    add("")

    add("## Validation")
    add("")
    for c in o["validate"]["checks"]:
        add(f"- {'✓' if c['ok'] else ('✗' if c['severity'] == 'block' else '⚠')} `{c['id']}` {c['message']}")
    add("")

    if ctx.decisions:
        add("## Analyst decisions")
        add("")
        for t, d in sorted(ctx.decisions.items()):
            add(f"- `{t}` {d['decision']}{' ' + d['value'] if d.get('value') else ''} by {d['by']} via {d['channel']}: {d['note']}")
        add("")

    add("## Sources")
    add("")
    used: dict[str, dict] = {}
    for f in rec["facts"].values():
        used.setdefault(f["accn"], f)
    for accn, f in sorted(used.items(), key=lambda kv: kv[1]["filed"]):
        add(f"- {f['form']} filed {f['filed']}, accession {accn}: {f['url']}")
    add("")
    add("### Fact appendix")
    add("")
    add("| id | concept | period | value (as reported) | unit | form | filed | accession |")
    add("|---|---|---|---|---|---|---|---|")
    for fid, f in rec["facts"].items():
        period = f"{f['start']} to {f['end']}" if f["start"] else f"at {f['end']}"
        restated = f" (restated; superseded {len(f['superseded'])})" if f["superseded"] else ""
        add(f"| {fid} | {f['concept']} | {period} | {f['value']}{restated} | {f['unit']} | {f['form']} | {f['filed']} | {f['accn']} |")
    add("")
    return "\n".join(L)


def _series(v) -> str:
    vals = v if isinstance(v, list) else [v]
    return pct(vals[0]) if len(set(map(str, vals))) == 1 else "/".join(pct(x) for x in vals)


def _src(d: dict) -> str:
    s = d.get("source")
    if not s:
        return "_(no source)_"
    tag = "verified claim" if d.get("verified") and str(s).startswith("c_") else (
        "reported fact" if d.get("verified") else "analyst-cited, unverified")
    quote = f": “{d['quote'][:140]}”" if d.get("quote") else ""
    problem = f" ⚠ {d['problem']}" if d.get("problem") else ""
    return f"[{s}] ({tag}{quote}){problem}"
