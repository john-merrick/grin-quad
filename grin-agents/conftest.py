import sys
from pathlib import Path

# Tools are plain scripts, not a package; make them importable in tests.
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "tools"))
sys.path.insert(0, str(Path(__file__).parent / "evals" / "config-auditor"))

import os
import tempfile

# Tests must never touch the real ledger key in ~/.config/grinos.
os.environ.setdefault("GRINOS_KEY_FILE", os.path.join(tempfile.mkdtemp(), "ledger.key"))
# ...nor write traces into the real var/traces, nor export anywhere.
os.environ.setdefault("GRINOS_HOME", tempfile.mkdtemp())
os.environ.pop("LANGFUSE_HOST", None)
os.environ["GRINOS_LANGFUSE"] = "off"
