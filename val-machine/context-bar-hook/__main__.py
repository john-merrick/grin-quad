#!/usr/bin/env python3
"""
Context Bar - Claude Code statusLine renderer + Stop hook pass-through.

StatusLine mode (stdin has 'context_window'): renders token bar in bottom bar.
Stop hook mode (stdin has 'transcript_path' only): pass-through, writes cache.
"""

import json
import os
import sys

MAX_CONTEXT = 200_000  # Sonnet/Opus 4.x context window

CACHE = os.path.expanduser("~/.claude/token-bar-cache.json")

R  = "\033[0m"
G  = "\033[38;2;64;160;43m"   # green
Y  = "\033[38;2;223;142;29m"  # yellow
RD = "\033[38;2;229;83;75m"   # red
C  = "\033[38;2;23;146;153m"  # cyan
GR = "\033[38;2;76;79;105m"   # gray


def fmt_k(n):
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


def bar(pct, width=20):
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def color(pct):
    return G if pct < 50 else Y if pct < 80 else RD


def parse_transcript(path):
    """Return (latest_context_tokens, total_output_tokens) from a JSONL transcript."""
    if not path or not os.path.exists(path):
        return 0, 0
    ctx, out = 0, 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    u = e.get("message", {}).get("usage")
                    if not u:
                        continue
                    reads  = u.get("cache_read_input_tokens", 0) or 0
                    create = u.get("cache_creation_input_tokens", 0) or 0
                    outtok = u.get("output_tokens", 0) or 0
                    ctx = max(ctx, reads + create)
                    out += outtok
                except Exception:
                    pass
    except Exception:
        pass
    return ctx, out


def write_cache(ctx, out):
    try:
        with open(CACHE, "w") as f:
            json.dump({"ctx": ctx, "out": out}, f)
    except Exception:
        pass


def read_cache():
    try:
        with open(CACHE) as f:
            d = json.load(f)
            return d.get("ctx", 0), d.get("out", 0)
    except Exception:
        return 0, 0


def render_statusline(data):
    cw = data.get("context_window", {})
    remaining = cw.get("remaining_percentage")

    if remaining is not None:
        used_pct = 100 - remaining
    else:
        used_pct = None

    # Try cache first (written by Stop hook), fall back to live parse
    ctx, out = read_cache()
    if ctx == 0:
        ctx, out = parse_transcript(data.get("transcript_path", ""))

    parts = []

    if used_pct is not None:
        c = color(used_pct)
        b = bar(used_pct)
        ctx_label = fmt_k(ctx) if ctx else f"{used_pct:.0f}%"
        parts.append(f"{c}[{b}]{R} {c}{ctx_label}/{fmt_k(MAX_CONTEXT)}{R}")

    if out:
        parts.append(f"{GR}↑{fmt_k(out)}{R}")

    print(" ".join(parts) if parts else "", end="")


def stop_hook(raw, data):
    # Parse transcript and update cache for statusLine to use
    transcript_path = data.get("transcript_path", "")
    ctx, out = parse_transcript(transcript_path)
    if ctx or out:
        write_cache(ctx, out)
    # Pass through: Stop hooks must return the original input
    sys.stdout.write(raw)
    sys.stdout.flush()


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
        sys.stdout.write(raw)
        return

    if "context_window" in data:
        render_statusline(data)
    else:
        stop_hook(raw, data)


if __name__ == "__main__":
    main()
