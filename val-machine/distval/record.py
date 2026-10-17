"""
Snapshot writer. Records each valuation run as a deterministic JSON file.
Two runs with identical inputs produce identical hashes and overwrite cleanly.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from distval.schema import Valuation

_SNAPSHOTS_DIR = Path(__file__).parent.parent / "snapshots"


def write_snapshot(
    val: Valuation,
    snapshot_dir: Path | None = None,
    timestamp: datetime | None = None,
) -> Path:
    out_dir = snapshot_dir or _SNAPSHOTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = timestamp or datetime.now(timezone.utc)

    payload = {
        "timestamp": ts.isoformat(),
        "industry": val.industry,
        "ticker": val.ticker,
        "as_of": val.as_of.isoformat(),
        "input_hash": val.input_hash,
        "discount_rate": val.discount_rate,
        "terminal_multiple": val.terminal_multiple,
        "fcf_path": [str(v) for v in val.fcf_path],
        "terminal_value": str(val.terminal_value),
        "enterprise_value": str(val.enterprise_value),
        "other_elements_total": str(val.other_elements_total),
        "net_debt": str(val.net_debt),
        "minority_interest": str(val.minority_interest),
        "equity_value": str(val.equity_value),
        "value_per_share": str(val.value_per_share),
        "price": str(val.price) if val.price is not None else None,
        "upside_pct": val.upside_pct,
    }

    filename = f"{val.ticker.lower()}_{val.input_hash[:12]}.json"
    path = out_dir / filename
    path.write_text(json.dumps(payload, indent=2))
    return path
