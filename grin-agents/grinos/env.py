"""
Environment and secrets. grin-agents/.env (gitignored) is loaded into os.environ; real environment
variables win. Values of the form op://vault/item/field are 1Password references: they stay as
references until a command actually needs the secret, then `op read` resolves them once per process.
So `grinos runs` never touches 1Password; `grinos publish --push` asks for exactly one secret.
"""
import os
import subprocess
import sys
from pathlib import Path

ENV_FILE = Path(__file__).parents[1] / ".env"
_cache: dict[str, str] = {}


def load_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = (s.strip() for s in line.split("=", 1))
        v = v.strip('"').strip("'")
        if v:
            os.environ.setdefault(k, v)


def secret(name: str) -> str | None:
    """The value of `name`, resolving a 1Password reference on first use. None if unset or unreadable."""
    v = os.environ.get(name)
    if not v or not v.startswith("op://"):
        return v or None
    if name not in _cache:
        try:
            out = subprocess.run(["op", "read", v], capture_output=True, text=True, timeout=120)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            print(f"{name}: couldn't run the 1Password CLI ({type(exc).__name__}); "
                  "unlock the 1Password app and approve the CLI prompt", file=sys.stderr)
            return None
        if out.returncode != 0 or not out.stdout.strip():
            print(f"{name}: 1Password read of {v} failed: {out.stderr.strip()[:200]}", file=sys.stderr)
            return None
        _cache[name] = out.stdout.strip()
    return _cache[name]
