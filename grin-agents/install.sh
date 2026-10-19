#!/usr/bin/env bash
# Symlink grin-agents' agents and skills into grin-quad/.claude so Claude Code loads them.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="$here/../.claude"
mkdir -p "$dest/agents" "$dest/skills"
for f in "$here"/agents/*.md; do ln -sfn "../../grin-agents/agents/$(basename "$f")" "$dest/agents/$(basename "$f")"; done
for d in "$here"/skills/*/; do n="$(basename "$d")"; ln -sfn "../../grin-agents/skills/$n" "$dest/skills/$n"; done
echo "linked into $dest:"; ls -l "$dest/agents" "$dest/skills" | grep -- '->'
