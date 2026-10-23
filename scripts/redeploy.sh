#!/bin/sh
# ===========================================================
# freqtrade-ki-bot – Redeploy für Webhook (schnell, ohne Build)
#
# Images sind fertig (freqtradeorg/freqtrade:stable) → kein build.
# Webhook-Timeout (~5 Min) killt sonst mit Exit 130 = SIGINT.
#
# Webhook cwd MUSS sein:
#   /var/services/homes/nasadmin/Projekte/freqtrade-ki-bot
# ===========================================================
set -e

PROJECT="${PROJECT_NAME:-freqtrade-ki-bot}"
LOG_TIME="$(date '+%Y-%m-%d %H:%M:%S')"

if docker info >/dev/null 2>&1; then
  DOCKER="docker"
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER="sudo -n docker"
elif sudo docker info >/dev/null 2>&1; then
  DOCKER="sudo docker"
else
  echo "[ERROR] $LOG_TIME | $PROJECT | docker nicht erreichbar" >&2
  exit 1
fi

log()  { echo "[INFO]  $LOG_TIME | $PROJECT | $*"; }
err()  { echo "[ERROR] $LOG_TIME | $PROJECT | $*" >&2; }

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

log "==== Redeploy gestartet ===="
log "Arbeitsverzeichnis: $ROOT"

case "$ROOT" in
  */Projekte/freqtrade-ki-bot|*/projekte/freqtrade-ki-bot)
    ;;
  */data/projekte/freqtrade-ki-bot)
    err "Falscher cwd: $ROOT – bitte Webhook auf ~/Projekte/freqtrade-ki-bot umstellen"
    exit 1
    ;;
esac

if [ ! -f docker-compose.yaml ]; then
  err "Keine docker-compose.yaml"
  exit 1
fi
if [ ! -f user_data/config.json ]; then
  err "user_data/config.json fehlt"
  exit 1
fi
if [ ! -f .env ]; then
  err ".env fehlt"
  exit 1
fi

log "Synchronisiere mit origin/main..."
git fetch --quiet origin main
git reset --hard origin/main
log "Sync ok. Stand: $(git rev-parse --short HEAD)"

# ftuser ≈ 1000
if command -v sudo >/dev/null 2>&1; then
  sudo chown -R 1000:1000 "$ROOT/user_data" 2>/dev/null || true
fi
mkdir -p user_data/logs user_data/data user_data/optimizer/reports

log "Container neu starten (ohne Build, Stop-Timeout 30s)..."
# -t 30: nicht auf lange Grace-Periods warten (Webhook-Limit ~5 Min / Exit 130)
$DOCKER compose up -d --force-recreate --remove-orphans --pull missing -t 30

log "Mount:"
$DOCKER inspect freqtrade --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' || true
$DOCKER compose ps
log "==== Redeploy OK ===="
