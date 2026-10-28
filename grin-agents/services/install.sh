#!/usr/bin/env bash
# Install Grin OS as macOS LaunchAgents: the dashboard/voice room and the Telegram bot start at
# login and restart if they crash. Logs: grin-agents/var/logs/. Undo with: services/install.sh --uninstall
# Options: --temporal adds the local Temporal server and the grinos worker; --langfuse starts the local
# Langfuse stack (docker) for traces.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
py="$here/../val-machine/.venv/bin/python"
agents="$HOME/Library/LaunchAgents"
mkdir -p "$agents" "$here/var/logs"

plist() {  # label, grinos args...
  local label="$1"; shift
  plist_cmd "$label" "$py" -m grinos "$@"
}

plist_cmd() {  # label, program, args...
  local label="$1"; shift
  local args=""; for a in "$@"; do args+="<string>$a</string>"; done
  cat > "$agents/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>$args</array>
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

uninstall=0; langfuse=0; temporal=0
for a in "$@"; do
  case "$a" in
    --uninstall) uninstall=1 ;;
    --langfuse) langfuse=1 ;;
    --temporal) temporal=1 ;;
    *) echo "unknown option $a (use --temporal, --langfuse, --uninstall)"; exit 2 ;;
  esac
done

for label in com.grinos.dashboard com.grinos.telegram com.grinos.temporal com.grinos.worker; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$agents/$label.plist"
done
if [[ $uninstall == 1 ]]; then echo "removed Grin OS LaunchAgents"; exit 0; fi

if [[ $langfuse == 1 ]]; then
  # Local Langfuse (docker compose). Secrets are generated once into a gitignored file; grinos reads
  # the project keys from it directly, so they are never copied into .env.
  lf="$here/services/langfuse"
  if [[ ! -f "$lf/.env" ]]; then
    r() { openssl rand -hex "$1"; }
    umask 077
    cat > "$lf/.env" <<ENV
POSTGRES_PASSWORD=$(r 16)
CLICKHOUSE_PASSWORD=$(r 16)
MINIO_ROOT_PASSWORD=$(r 16)
REDIS_AUTH=$(r 16)
SALT=$(r 16)
ENCRYPTION_KEY=$(r 32)
NEXTAUTH_SECRET=$(r 32)
LANGFUSE_INIT_PROJECT_ID=grin-os
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-$(r 16)
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-$(r 24)
LANGFUSE_INIT_USER_EMAIL=${USER}@grinos.local
LANGFUSE_INIT_USER_NAME=${USER}
LANGFUSE_INIT_USER_PASSWORD=$(r 12)
ENV
    echo "langfuse: generated $lf/.env (sign-in email and password are in it)"
  fi
  docker compose -f "$lf/docker-compose.yml" --env-file "$lf/.env" up -d
  echo "langfuse: http://127.0.0.1:3000"
fi

if [[ $temporal == 1 ]]; then
  # Local Temporal dev server (SQLite under var/temporal, UI http://127.0.0.1:8233) and the grinos worker.
  tbin="$(command -v temporal || echo /opt/homebrew/bin/temporal)"
  [[ -x "$tbin" ]] || { echo "temporal CLI not found: brew install temporal"; exit 1; }
  "$py" -c "import temporalio" 2>/dev/null || { echo "install the worker's dependency first: $py -m pip install -r $here/requirements.txt"; exit 1; }
  mkdir -p "$here/var/temporal"
  plist_cmd com.grinos.temporal "$tbin" server start-dev --ip 127.0.0.1 --db-filename "$here/var/temporal/temporal.db" \
    --ui-port 8233
  launchctl bootstrap "gui/$(id -u)" "$agents/com.grinos.temporal.plist"
  echo "temporal: http://127.0.0.1:8233"
  plist com.grinos.worker worker
  launchctl bootstrap "gui/$(id -u)" "$agents/com.grinos.worker.plist"
  echo "worker: serving the Grin OS task queues"
fi

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
