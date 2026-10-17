"""Load a company config from companies/<ticker>.yaml into EngineInputs via its industry adapter."""
import importlib
from decimal import Decimal
from pathlib import Path

import yaml

from distval.schema import EngineInputs

_COMPANIES_DIR = Path(__file__).parent / "companies"


def _load_adapter_class(dotted_path: str):
    module_path, cls_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, cls_name)


def load_drivers(ticker: str) -> EngineInputs:
    if not ticker.replace("-", "").isalnum():
        raise ValueError(f"Invalid ticker {ticker!r}: must be alphanumeric")
    safe = ticker.strip().upper()
    path = _COMPANIES_DIR / f"{safe.lower()}.yaml"
    if not path.resolve().is_relative_to(_COMPANIES_DIR.resolve()):
        raise ValueError(f"Ticker {ticker!r} resolves outside companies directory")
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise FileNotFoundError(
            f"No company config found for ticker {ticker!r}. Expected: {path}"
        ) from None
    except yaml.YAMLError as exc:
        raise ValueError(f"Malformed YAML for {ticker!r} at {path}: {exc}") from exc

    industry = raw.get("industry", "distributor")

    from distval.industries.registry import REGISTRY
    adapter_path = REGISTRY.get(industry)
    if adapter_path is None:
        raise ValueError(
            f"Unknown industry {industry!r} for {ticker!r}. "
            f"Registered: {list(REGISTRY.keys())}"
        )

    adapter_cls = _load_adapter_class(adapter_path)
    adapter = adapter_cls()

    raw = adapter.coerce_raw(raw)
    # Strip loader-only keys that aren't part of the drivers schema.
    schema_fields = set(adapter.drivers_schema().model_fields.keys())
    raw_for_schema = {k: v for k, v in raw.items() if k in schema_fields}
    drivers = adapter.drivers_schema()(**raw_for_schema)
    return adapter.to_engine_inputs(drivers)
