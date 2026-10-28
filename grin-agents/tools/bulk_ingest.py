"""
Ingest every source in tools/edgar.py manifests through the full evidence pipeline.

Oldest first within a company, companies in parallel. A source whose exact bytes are already in
the ledger is skipped, so a rerun resumes where it stopped.

    python tools/bulk_ingest.py var/inbox/POOL var/inbox/FAST --as-of 2026-10-19 --configs ../val-machine/distval/companies
"""
import argparse
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
from grinos.cli import default_root  # noqa: E402
from grinos.ledger import Ledger  # noqa: E402


def ingested_blobs(ledger: Ledger) -> set[str]:
    return {e["payload"]["source"]["blob"] for e in ledger.entries() if e["stage"] == "source"}


def run_company(folder: Path, as_of: str, configs: Path, done: set[str], log: Path) -> list[str]:
    ticker = folder.name
    config = configs / f"{ticker.lower()}.yaml"
    results = []
    for m in json.loads((folder / "manifest.json").read_text()):
        path = Path(m["file"])
        if hashlib.sha256(path.read_bytes()).hexdigest() in done:
            results.append(f"{ticker} {m['date']} skipped (already ingested)")
            continue
        cmd = [sys.executable, "-m", "grinos", "ingest", str(path), "--company", ticker, "--kind", "other",
               "--published", m["date"], "--as-of", as_of, "--uri", m["url"]]
        if config.exists():
            cmd += ["--config", str(config)]
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        with log.open("a") as f:
            f.write(f"\n### {ticker} {m['date']} rc={out.returncode}\n{out.stdout}{out.stderr}")
        head = (out.stdout.strip().splitlines() or ["(no output)"])[0]
        results.append(f"{ticker} {m['date']} rc={out.returncode} {head}")
        print(results[-1], flush=True)
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("folders", nargs="+", type=Path)
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--configs", type=Path, default=ROOT.parent / "val-machine/distval/companies")
    ap.add_argument("--log", type=Path, default=default_root() / "logs/bulk_ingest.log")
    a = ap.parse_args()
    a.log.parent.mkdir(parents=True, exist_ok=True)
    done = ingested_blobs(Ledger(default_root()))
    with ThreadPoolExecutor(len(a.folders)) as pool:
        futures = [pool.submit(run_company, f, a.as_of, a.configs, done, a.log) for f in a.folders]
        lines = [line for fut in futures for line in fut.result()]
    print("\n".join(["", "summary:"] + lines))


if __name__ == "__main__":
    main()
