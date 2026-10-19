#!/usr/bin/env bash
# Install Grin OS as macOS LaunchAgents: the dashboard/voice room and the Telegram bot start at
# login and restart if they crash. Logs: grin-agents/var/logs/. Undo with: services/install.sh --uninstall
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
py="$here/../val-machine/.venv/bin/python"
agents="$HOME/Library/LaunchAgents"
mkdir -p "$agents" "$here/var/logs"

plist() {  # label, args...
  local label="$1"; shift
  local args=""; for a in "$@"; do args+="<string>$a</string>"; done
  cat > "$agents/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$py</string><string>-m</string><string>grinos</string>$args</array>
  <key>WorkingDirectory</key><string>$here</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$here/var/logs/$label.log</string>
  <key>StandardErrorPath</key><string>$here/var/logs/$label.log</string>
</dict></plist>
PLIST
}

for label in com.grinos.dashboard com.grinos.telegram; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$agents/$label.plist"
done
if [[ "${1:-}" == "--uninstall" ]]; then echo "removed Grin OS LaunchAgents"; exit 0; fi

plist com.grinos.dashboard serve --port 8765
launchctl bootstrap "gui/$(id -u)" "$agents/com.grinos.dashboard.plist"
echo "dashboard: http://127.0.0.1:8765  (voice room: /room)"

if grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$here/.env" 2>/dev/null && grep -q '^TELEGRAM_ALLOWED_USER_IDS=.\+' "$here/.env"; then
  plist com.grinos.telegram telegram
  launchctl bootstrap "gui/$(id -u)" "$agents/com.grinos.telegram.plist"
  echo "telegram bot: running"
else
  echo "telegram bot: skipped (set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS in .env, then re-run)"
fi
