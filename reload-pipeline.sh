#!/usr/bin/env bash
# Reload pipeline processes so the new js_script.py (regex literal fix) takes
# effect. The running daemon and uvicorn API server hold the old JS string in
# memory; only a process restart picks up the fix.
#
# Run from any directory:
#   bash $HOME/.claude/skills/NEW/reload-pipeline.sh
set -uo pipefail

UID_NUM="$(id -u)"
API_LABEL="com.doi.fbpipeline.api"
DAEMON_LABEL="com.doi.fbpipeline.daemon"
API_PLIST="$HOME/Library/LaunchAgents/${API_LABEL}.plist"
DAEMON_PLIST="$HOME/Library/LaunchAgents/${DAEMON_LABEL}.plist"

echo "==> Stopping pipeline processes"

# Try a graceful launchctl restart first (KeepAlive=true → auto-respawn).
launchctl kickstart -k "gui/${UID_NUM}/${API_LABEL}" 2>/dev/null && echo "  kickstart API OK"
launchctl kickstart -k "gui/${UID_NUM}/${DAEMON_LABEL}" 2>/dev/null && echo "  kickstart daemon OK"

# Hard fallback: kill anything still listening on 8000 + any fb-scan-pipeline
# python process; then re-load the plists.
sleep 1
API_PID="$(lsof -tiTCP:8000 -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "${API_PID}" ]; then
  echo "  killing API PID ${API_PID}"
  kill -TERM "${API_PID}" 2>/dev/null || kill -KILL "${API_PID}" 2>/dev/null || true
fi
pkill -f 'fb-scan-pipeline-v8.py' 2>/dev/null && echo "  pkill daemon OK" || true

sleep 2

echo "==> Re-loading launchd plists"
launchctl unload "${API_PLIST}" 2>/dev/null || true
launchctl unload "${DAEMON_PLIST}" 2>/dev/null || true
launchctl load   "${API_PLIST}"    2>/dev/null && echo "  load API OK"    || echo "  load API FAILED — try: launchctl bootstrap gui/${UID_NUM} ${API_PLIST}"
launchctl load   "${DAEMON_PLIST}" 2>/dev/null && echo "  load daemon OK" || echo "  load daemon FAILED — try: launchctl bootstrap gui/${UID_NUM} ${DAEMON_PLIST}"

sleep 3

echo "==> Health check"
curl -sS -m 5 http://127.0.0.1:8000/health | head -c 200; echo

echo "==> Trigger one keyword scrape (sleep mask, US)"
curl -sS -m 60 -X POST 'http://127.0.0.1:8000/admin/scrape-keyword?keyword=sleep%20mask&country=US' | head -c 400; echo

echo
echo "==> Done. Watch live extraction:"
echo "  tail -F $HOME/.claude/skills/NEW/logs/pipeline.log | grep -E 'JS 执行|count=|保存|graphql merge'"
