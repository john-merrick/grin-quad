"""
SEC EDGAR client: one User-Agent, one throttle, bounded retries. Used by the valuation workflow
(companyfacts XBRL, submissions) and tools/edgar.py (earnings-release text).

Identity: EDGAR_IDENTITY (the same variable distval uses), e.g. "Jane Analyst jane@example.com".
SEC fair access: at most 10 requests/second; we stay near 5 and back off on 429/503.

Anything that fetches takes a `get(url) -> bytes`, so tests and the offline fixture path swap in
a dict of canned responses (`fixture_getter`).
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

DEFAULT_UA = "grin-agents research bot admin@grin-agents.local"
MIN_INTERVAL = 0.2          # seconds between requests, process-wide
TRIES = 5

Getter = Callable[[str], bytes]
_lock = threading.Lock()
_last = [0.0]
_warned = [False]


def user_agent() -> str:
    ua = os.environ.get("EDGAR_IDENTITY", "").strip()
    if not ua and not _warned[0]:
        _warned[0] = True
        print("note: EDGAR_IDENTITY not set; using a generic User-Agent. SEC asks for a name and email.",
              file=sys.stderr)
    return ua or DEFAULT_UA


def http_get(url: str, tries: int = TRIES) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept-Encoding": "identity"})
    for attempt in range(tries):
        with _lock:                                        # throttle across threads
            wait = _last[0] + MIN_INTERVAL * 4 ** attempt - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == tries - 1:
                raise
    raise AssertionError("unreachable")


def fixture_getter(responses: dict[str, Path | bytes]) -> Getter:
    """Offline: serve canned bytes by URL; any other URL is an error (never a silent network call)."""
    def get(url: str) -> bytes:
        if url not in responses:
            raise KeyError(f"offline fixture has no response for {url}")
        v = responses[url]
        return v.read_bytes() if isinstance(v, Path) else v
    return get


TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def submissions_url(cik: int) -> str:
    return f"https://data.sec.gov/submissions/CIK{cik:010d}.json"


def companyfacts_url(cik: int) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


def filing_url(cik: int, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}-index.html"


def cik_for(ticker: str, tickers_json: bytes) -> int | None:
    for row in json.loads(tickers_json).values():
        if row["ticker"].upper() == ticker.upper():
            return int(row["cik_str"])
    return None


def offline_path(url: str) -> str:
    """Where a saved response lives under an --offline folder: the URL's path, without the host."""
    return url.split("://", 1)[1].split("/", 1)[1]


def directory_getter(folder: Path) -> Getter:
    """Offline from a folder of saved responses (see grinos/tests/sec_fixture.py). Never the network."""
    def get(url: str) -> bytes:
        path = folder / offline_path(url)
        if not path.exists():
            raise FileNotFoundError(f"offline: no saved response for {url} at {path}")
        return path.read_bytes()
    return get
