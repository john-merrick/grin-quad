"""
Content-addressed blob store + append-only, hash-chained ledger.

    var/store/blobs/<sha256>   exact source bytes, write-once
    var/ledger.jsonl           one entry per line; entry.prev = hash of the entry before it

`audit()` recomputes the whole chain and every referenced blob, so an edited, reordered,
truncated or deleted entry, or a swapped source file, is detected. `verified_claims()` is
the only door analyst agents get evidence through, and it refuses to open on a broken chain.

Keyed: every entry carries an HMAC over its hash with a secret kept OUTSIDE var/
(GRINOS_KEY_FILE, default ~/.config/grinos/ledger.key, mode 0600), and var/ledger.head anchors
the latest seq+hash. Rewriting the chain without the key, or chopping off its tail, is detected.
Limit: anyone who can read the key file can still forge, so keep it out of shared or synced dirs.
"""
import fcntl
import hmac
import json
import os
import secrets
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from grinos import trace
from grinos.schemas import Actor, canonical, sha256

GENESIS = "0" * 64
DEFAULT_KEY = Path.home() / ".config" / "grinos" / "ledger.key"


class _Exists(Exception):
    pass


def _find_idem(lines: list[str], idem: str) -> dict | None:
    needle = canonical({"idem": idem})[1:-1]      # '"idem":"…"' as canonical() writes it
    for ln in lines:
        if needle in ln:
            e = json.loads(ln)
            if e["payload"].get("idem") == idem:
                return e
    return None


class LedgerCorrupt(RuntimeError):
    pass


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def entry_hash(entry: dict) -> str:
    return sha256(canonical({k: v for k, v in entry.items() if k not in {"hash", "mac"}}).encode())


def load_key(path: Path | None = None) -> bytes:
    path = Path(path or os.environ.get("GRINOS_KEY_FILE") or DEFAULT_KEY)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    return bytes.fromhex(path.read_text().strip())


class Ledger:
    def __init__(self, root: Path, clock: Callable[[], str] = _utcnow, key: bytes | None = None):
        self.root = Path(root)
        self.blobs = self.root / "store" / "blobs"
        self.path = self.root / "ledger.jsonl"
        self.head = self.root / "ledger.head"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self.clock = clock
        self.key = key or load_key()

    def _mac(self, digest: str) -> str:
        return hmac.new(self.key, digest.encode(), "sha256").hexdigest()

    # --- blobs -------------------------------------------------------------

    def put_blob(self, data: bytes) -> str:
        digest = sha256(data)
        path = self.blobs / digest
        if not path.exists():
            tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(data)
            tmp.rename(path)
            path.chmod(0o444)
        return digest

    def get_blob(self, digest: str) -> bytes:
        data = (self.blobs / digest).read_bytes()
        if sha256(data) != digest:
            raise LedgerCorrupt(f"blob {digest[:12]} content does not match its hash")
        return data

    # --- entries -----------------------------------------------------------

    def entries(self) -> Iterator[dict]:
        with self.path.open() as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)   # never read a half-written line
            lines = fh.readlines()
        for line in lines:
            if line.strip():
                yield json.loads(line)

    def append(self, run_id: str, stage: str, actor: Actor, payload: dict,
               refs: list[str] | None = None) -> dict:
        """Append one entry under an exclusive lock; returns it with seq and hash set."""
        return self.append_if(run_id, stage, actor, payload, refs, guard=None)

    def append_if(self, run_id: str, stage: str, actor: Actor, payload: dict,
                  refs: list[str] | None = None, guard: Callable[[list[dict]], str | None] | None = None) -> dict:
        """
        Append only if `guard(all_entries)` returns None, evaluated under the same exclusive lock
        as the write. Closes check-then-act races between the CLI, dashboard and Telegram.
        A guard that returns a string aborts with ValueError(that string).
        """
        def check(lines: list[str]) -> None:
            if guard is not None:
                problem = guard([json.loads(ln) for ln in lines])
                if problem:
                    raise ValueError(problem)
        return self._locked(run_id, stage, actor, payload, refs, check)

    def append_once(self, run_id: str, stage: str, actor: Actor, payload: dict, idem: str,
                    refs: list[str] | None = None) -> tuple[dict, bool]:
        """
        Append unless an entry with this idempotency key already exists; returns (entry, created).
        Checked under the write lock, so a retried activity (Temporal) or a double click can't write twice.
        """
        found: list[dict] = []

        def check(lines: list[str]) -> None:
            hit = _find_idem(lines, idem)
            if hit is not None:
                found.append(hit)
                raise _Exists()
        try:
            return self._locked(run_id, stage, actor, payload | {"idem": idem}, refs, check), True
        except _Exists:
            return found[0], False

    def find_idem(self, idem: str) -> dict | None:
        with self.path.open() as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            lines = fh.read().splitlines()
        return _find_idem(lines, idem)

    def _locked(self, run_id: str, stage: str, actor: Actor, payload: dict, refs: list[str] | None,
                check: Callable[[list[str]], None]) -> dict:
        with self.path.open("r+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
            check(lines)
            last = json.loads(lines[-1]) if lines else None
            entry = {
                "seq": (last["seq"] + 1) if last else 0,
                "ts": self.clock(),
                "run_id": run_id,
                "stage": stage,
                "actor": actor.model_dump(),
                "refs": refs or [],
                "payload": payload,
                "prev": last["hash"] if last else GENESIS,
            }
            ids = trace.current_ids()
            if ids:
                entry["trace_id"] = ids["trace_id"]   # links the entry to its trace (covered by the hash)
            entry["hash"] = entry_hash(entry)
            entry["mac"] = self._mac(entry["hash"])
            fh.write(canonical(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            self.head.write_text(canonical({"seq": entry["seq"], "hash": entry["hash"], "mac": entry["mac"]}))
        return entry

    # --- audit -------------------------------------------------------------

    def audit(self) -> list[str]:
        """Return every integrity problem found; empty list means the ledger is intact."""
        problems: list[str] = []
        prev, expected_seq = GENESIS, 0
        for e in self.entries():
            if e.get("seq") != expected_seq:
                problems.append(f"seq {e.get('seq')}: expected {expected_seq} (entry missing or reordered)")
            if e.get("prev") != prev:
                problems.append(f"seq {e.get('seq')}: prev hash does not match previous entry")
            if entry_hash(e) != e.get("hash"):
                problems.append(f"seq {e.get('seq')}: content does not match its hash (edited)")
            elif not hmac.compare_digest(self._mac(e["hash"]), e.get("mac", "")):
                problems.append(f"seq {e.get('seq')}: MAC invalid (chain rewritten without the ledger key)")
            for ref in e.get("refs", []):
                if ref.startswith("blob:"):
                    try:
                        self.get_blob(ref[5:])
                    except (FileNotFoundError, LedgerCorrupt) as exc:
                        problems.append(f"seq {e.get('seq')}: {ref[:17]}… {exc}")
            prev, expected_seq = str(e.get("hash", "")), expected_seq + 1
        if expected_seq:
            try:
                head = json.loads(self.head.read_text())
            except (OSError, ValueError):
                head = None
            if not head or head.get("hash") != prev or head.get("seq") != expected_seq - 1 \
                    or not hmac.compare_digest(self._mac(prev), head.get("mac", "")):
                problems.append("head anchor does not match the last entry (ledger truncated or head tampered)")
        return problems

    # --- the analyst gate --------------------------------------------------

    def verified_claims(self, company: str | None = None) -> list[dict]:
        """
        Claims an analyst agent may use: triaged by the decision model as relevant AND
        finally verified. A human verdict outranks any rule/agent verdict (latest human wins);
        otherwise the latest verdict wins. Refuses if the ledger fails audit.
        """
        problems = self.audit()
        if problems:
            raise LedgerCorrupt("refusing to serve evidence from a broken ledger: " + "; ".join(problems[:3]))
        claims: dict[str, dict] = {}
        routed: dict[str, bool] = {}
        verdict: dict[str, str] = {}
        human: dict[str, str] = {}
        for e in self.entries():
            p = e["payload"]
            if e["stage"] == "claim":
                claims[p["claim_id"]] = p | {"ledger_hash": e["hash"]}
            elif e["stage"] == "decision":
                routed[p["claim_id"]] = p["action"] == "verify"
            elif e["stage"] == "verdict":
                (human if e["actor"]["kind"] == "human" else verdict)[p["claim_id"]] = p["verdict"]
        verdict |= human
        return [
            c for cid, c in claims.items()
            if (routed.get(cid) or cid in human) and verdict.get(cid) == "verified"
            and (company is None or c["claim"]["company"] == company)
        ]
