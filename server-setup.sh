#!/usr/bin/env bash
# Controller (server) setup — idempotent. Brings the deployed portal stack
# fully up on this machine (handoff §15–§19):
#
#   1. .env present and complete (GUACD_HOSTNAME is required, compose fails
#      fast without it)
#   2. app/lab-tick images built; full compose stack up
#   3. guacamole_db + role + schema (guac/schema.sql) — created ONLY if
#      missing, so re-runs never touch existing data
#   4. portal reachable through the Caddy gateway
#   5. daily backup timer installed (backup/BACKUPS.md), with an initial
#      backup on first run
#
# Re-running after `git pull` is how the deployment gets updated
# (migrations + collectstatic run in the app entrypoint).
#
# Workhorses are provisioned separately: agent/ADD-A-WORKHORSE.md →
# agent/SETUP.md (install.sh, isolation-install.sh, guacd-install.sh).

set -euo pipefail
cd "$(dirname "$0")"
REPO_DIR="$(pwd)"

log()  { echo "==> $*"; }
warn() { echo "==> WARNING: $*"; }
fail() { echo "==> FATAL: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "Run with sudo: sudo ./server-setup.sh"

# --- 1. .env -------------------------------------------------------------------
if [[ ! -f .env ]]; then
  cp .env.example .env
  log "Created .env from .env.example."
  log "Fill it in (GUACD_HOSTNAME, secrets, Discord OAuth, CSRF origins) and re-run."
  exit 1
fi
set -a; source .env; set +a

grep -Eq '^GUACD_HOSTNAME=..*' .env \
  || fail "GUACD_HOSTNAME is not set in .env — the address of the workhorse guacd (agent/ADD-A-WORKHORSE.md §8). Compose fails fast without it."
PGUSER="${POSTGRES_USER:-ctf}"
APP_DB="${POSTGRES_DB:-ctf}"
GUAC_DB_PASSWORD="${GUACAMOLE_DB_PASSWORD:-}"

# Loud placeholder warnings; deployment proceeds but fix before players arrive.
for pair in "DJANGO_SECRET_KEY:change-me" "POSTGRES_PASSWORD:change-me"; do
  var="${pair%%:*}" placeholder="${pair##*:}"
  if [[ -z "${!var:-}" || "${!var}" == "$placeholder" ]]; then
    warn "$var is unset or still the placeholder — set a real value in .env."
  fi
done
[[ -n "$GUAC_DB_PASSWORD" ]] || warn "GUACAMOLE_DB_PASSWORD is unset; guacamole would fall back to the compose default."

# --- 2. Build + up ---------------------------------------------------------------
log "Building app/lab-tick images and starting the stack"
docker compose build app lab-tick
docker compose up -d

# --- 3. guacamole_db (role + database + schema) -----------------------------------
# The signed-JSON extension needs its schema; data lives here (connection
# records). Guard everything so re-runs never recreate or wipe.
docker compose exec -T db psql -U "$PGUSER" -d postgres -tAc \
  "SELECT 1 FROM pg_roles WHERE rolname='guacamole'" | grep -q 1 \
  || docker compose exec -T db psql -U "$PGUSER" -d postgres \
       -c "CREATE ROLE \"guacamole\" LOGIN PASSWORD '${GUAC_DB_PASSWORD}';"
if ! docker compose exec -T db psql -U "$PGUSER" -d postgres -tAc \
     "SELECT 1 FROM pg_database WHERE datname='guacamole_db'" | grep -q 1; then
  log "Creating guacamole_db with schema from guac/schema.sql"
  docker compose exec -T db psql -U "$PGUSER" -d postgres \
    -c "CREATE DATABASE \"guacamole_db\" OWNER \"guacamole\";"
  docker compose exec -T -e PGPASSWORD="$GUAC_DB_PASSWORD" db \
    psql -U guacamole -d guacamole_db < guac/schema.sql
elif ! docker compose exec -T db psql -U "$PGUSER" -d guacamole_db -tAc \
     "SELECT to_regclass('guacamole_connection')" | grep -q guacamole_connection; then
  log "guacamole_db exists but is empty — applying guac/schema.sql"
  docker compose exec -T -e PGPASSWORD="$GUAC_DB_PASSWORD" db \
    psql -U guacamole -d guacamole_db < guac/schema.sql
else
  log "guacamole_db already initialized — leaving it untouched"
fi

# --- 4. Portal reachable -----------------------------------------------------------
log "Waiting for the portal through the Caddy gateway"
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/ || true)"
  [[ "$code" == 302 || "$code" == 200 ]] && break
  sleep 2
done
[[ "$code" == 302 || "$code" == 200 ]] \
  || fail "portal did not come up (last status: $code). Check: docker compose logs app"

# --- 5. Daily backup timer -----------------------------------------------------------
log "Installing the backup timer (backup/BACKUPS.md)"
# ExecStart must be absolute for systemd; bake in this checkout's path.
sed "s|^ExecStart=.*|ExecStart=/usr/bin/bash $REPO_DIR/backup/backup.sh|" \
  backup/ctf-backup.service > /etc/systemd/system/ctf-backup.service
cp backup/ctf-backup.timer /etc/systemd/system/ctf-backup.timer
chown root:root /etc/systemd/system/ctf-backup.{service,timer}
chmod 644 /etc/systemd/system/ctf-backup.{service,timer}
systemctl daemon-reload
systemctl enable --now ctf-backup.timer
# First run: prove the pipeline immediately instead of waiting for 04:30.
if ! compgen -G "$REPO_DIR/backups/db-*.dump" >/dev/null; then
  log "No backups yet — running the first one now"
  "$REPO_DIR/backup/backup.sh"
fi

# --- Summary --------------------------------------------------------------------------
log "Stack:"
docker compose ps --format '{{.Service}}\t{{.Status}}' | sed 's/^/    /'
log "Backup schedule:"
systemctl list-timers ctf-backup.timer --no-pager | sed -n '1,2p' | sed 's/^/    /'
log "Server setup complete."
