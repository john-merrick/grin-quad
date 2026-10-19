"""
Fetch a company's quarterly earnings releases from SEC EDGAR as plain text, ready for `grinos ingest`.

An earnings release is the exhibit 99.1 of an 8-K filed under item 2.02 (Results of Operations).
Files land in <out>/<TICKER>/<filing date>.txt with a manifest.json of {date, url, file}, oldest
first so history is ingested in the order it was published.

    python tools/edgar.py POOL FAST GWW --since 2023-10-01 --out var/inbox
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1]))
from html_to_text import html_to_text  # noqa: E402

from grinos import sec  # noqa: E402

# A filing index row: <a href="/Archives/.../doc.htm">doc.htm</a></td><td ...>EX-99.1</td>
EX99_ROW = re.compile(r'href="(?P<href>/Archives/[^"]+\.html?)"[^<]*</a></td>\s*<td[^>]*>EX-99\.?1?\s*<',
                      re.IGNORECASE)
_get = sec.http_get


def cik_for(ticker: str) -> int:
    cik = sec.cik_for(ticker, _get(sec.TICKERS_URL))
    if cik is None:
        raise SystemExit(f"{ticker}: not in SEC ticker list")
    return cik


def earnings_filings(cik: int, since: str) -> list[dict]:
    recent = json.loads(_get(sec.submissions_url(cik)))["filings"]["recent"]
    rows = [{"date": recent["filingDate"][i], "accession": recent["accessionNumber"][i]}
            for i in range(len(recent["form"]))
            if recent["form"][i] == "8-K" and "2.02" in recent["items"][i] and recent["filingDate"][i] >= since]
    return sorted(rows, key=lambda r: r["date"])


def exhibit_url(cik: int, accession: str) -> str | None:
    """The document the filing index types as EX-99.1 (or EX-99 when there is only one)."""
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}"
    index = _get(f"{base}/{accession}-index.html").decode("utf-8", errors="replace")
    hit = EX99_ROW.search(index)
    return f"https://www.sec.gov{hit['href']}" if hit else None


def fetch(ticker: str, since: str, out: Path) -> list[dict]:
    cik = cik_for(ticker)
    folder = out / ticker.upper()
    folder.mkdir(parents=True, exist_ok=True)
    old = folder / "manifest.json"
    known = {m["date"]: m["url"] for m in json.loads(old.read_text())} if old.exists() else {}
    manifest = []
    for f in earnings_filings(cik, since):
        path = folder / f"{f['date']}.txt"
        if path.exists() and f["date"] in known:
            manifest.append({"date": f["date"], "url": known[f["date"]], "file": str(path)})
            continue
        url = exhibit_url(cik, f["accession"])
        if not url:
            print(f"  {ticker} {f['date']}: no exhibit 99.1, skipped", file=sys.stderr)
            continue
        path.write_text(html_to_text(_get(url).decode("utf-8", errors="replace")), encoding="utf-8")
        manifest.append({"date": f["date"], "url": url, "file": str(path)})
        old.write_text(json.dumps(manifest, indent=2))     # after each file, so a rerun resumes
        print(f"  {ticker} {f['date']}  {path.stat().st_size:>7} bytes  {url}")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--since", required=True, help="YYYY-MM-DD, earliest filing date")
    ap.add_argument("--out", type=Path, default=Path("var/inbox"))
    a = ap.parse_args()
    for t in a.tickers:
        fetch(t, a.since, a.out)


if __name__ == "__main__":
    main()
