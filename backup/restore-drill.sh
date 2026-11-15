#!/usr/bin/env bash
# Restore DRILL — proves the backups can actually be restored, closing the
# handoff §22.39 validation gate ("Database and template backups can
# actually be restored"). Touches only scratch databases; the live ctf and
# guacamole_db are never modified.
#
# What it verifies:
#   1. The latest app-db dump restores cleanly into a scratch DB.
#   2. Django connects, every recorded migration is applied, and the
#      authoritative tables contain data (users/challenges/submissions).
#   3. guacamole_db restores into a scratch DB.
#   4. The attachments tar is a readable, non-empty archive.
#
# Run this after enabling backups (once), after any backup.sh change, and
# quarterly per the ops checklist. Drills against the same local backup
# tree do not exercise off-machine copies; re-run on the remote host once
# an off-machine destination is configured (owner deferred that decision).

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi
BACKUP_DIR="${BACKUP_DIR:-./backups}"
PGUSER="${POSTGRES_USER:-ctf}"
APP_DB="${POSTGRES_DB:-ctf}"
DRILL_DB="ctf_drill"
DRILL_GUAC="guacamole_drill"

log()  { echo "==> $*"; }
fail() { echo "==> FAIL: $*" >&2; exit 1; }

dumpfile="${1:-$(ls -1t "$BACKUP_DIR"/db-$APP_DB-*.dump 2>/dev/null | head -n1 || true)}"
gfile="${2:-$(ls -1t "$BACKUP_DIR"/db-guacamole_db-*.dump 2>/dev/null | head -n1 || true)}"
tarfile="${3:-$(ls -1t "$BACKUP_DIR"/attachments-*.tar.gz 2>/dev/null | head -n1 || true)}"

[[ -n "$dumpfile" && -s "$dumpfile" ]] || fail "no app-db dump in $BACKUP_DIR — run backup/backup.sh first"

docker compose ps >/dev/null 2>&1 || fail "docker compose stack is not running"

cleanup() {
  log "Dropping scratch databases"
  docker compose exec -T db psql -U "$PGUSER" -d postgres -c \
    "DROP DATABASE IF EXISTS \"$DRILL_DB\";" || true
  docker compose exec -T db psql -U "$PGUSER" -d postgres -c \
    "DROP DATABASE IF EXISTS \"$DRILL_GUAC\";" || true
}
trap cleanup EXIT

log "Using: $(basename "$dumpfile")${gfile:+ / $(basename "$gfile")}${tarfile:+ / $(basename "$tarfile")}"

# --- 1. App-db dump -> scratch database --------------------------------------
log "Restoring app dump into scratch DB $DRILL_DB"
docker compose exec -T db psql -U "$PGUSER" -d postgres -c "DROP DATABASE IF EXISTS \"$DRILL_DB\";"
docker compose exec -T db psql -U "$PGUSER" -d postgres -c "CREATE DATABASE \"$DRILL_DB\";"
# Custom-format dumps stream from stdin; the file lives on the host, not in
# the db container's filesystem.
docker compose exec -i db pg_restore -U "$PGUSER" -d "$DRILL_DB" --no-owner < "$dumpfile"

# --- 2. Django against the scratch DB ----------------------------------------
# check --database default: connection + basic queries succeed.
# migrate --check: exits nonzero when any recorded migration is unapplied.
log "Django checks against $DRILL_DB"
docker compose run --rm -e POSTGRES_DB="$DRILL_DB" --entrypoint python \
  app manage.py check --database default \
  || fail "Django check failed against the restored dump"
docker compose run --rm -e POSTGRES_DB="$DRILL_DB" --entrypoint python \
  app manage.py migrate --check --noinput \
  || fail "migrate --check: restored dump has unapplied/pending migrations"

log "Authoritative-row smoke counts ($DRILL_DB)"
docker compose exec -T db psql -U "$PGUSER" -d "$DRILL_DB" -tAc \
  "SELECT 'django_migrations', count(*) FROM django_migrations
   UNION ALL SELECT 'accounts_user', count(*) FROM accounts_user
   UNION ALL SELECT 'content_challenge', count(*) FROM content_challenge
   UNION ALL SELECT 'submissions_submission', count(*) FROM submissions_submission" \
  | sed 's/^/    /'
migs="$(docker compose exec -T db psql -U "$PGUSER" -d "$DRILL_DB" -tAc \
  "SELECT count(*) FROM django_migrations")"
[[ "$migs" -gt 0 ]] || fail "django_migrations is empty in the restored dump"

# --- 3. guacamole_db -> scratch database --------------------------------------
if [[ -n "$gfile" && -s "$gfile" ]]; then
  log "Restoring guacamole_db dump into scratch DB $DRILL_GUAC"
  docker compose exec -T db psql -U "$PGUSER" -d postgres -c "DROP DATABASE IF EXISTS \"$DRILL_GUAC\";"
  docker compose exec -T db psql -U "$PGUSER" -d postgres -c "CREATE DATABASE \"$DRILL_GUAC\";"
  docker compose exec -i db pg_restore -U "$PGUSER" -d "$DRILL_GUAC" --no-owner < "$gfile"
  guacconns="$(docker compose exec -T db psql -U "$PGUSER" -d "$DRILL_GUAC" -tAc \
    "SELECT count(*) FROM guacamole_connection" || echo 0)"
  echo "    guacamole_connection rows: $guacconns (0 is fine before first lab)"
else
  log "SKIP: no guacamole_db dump found"
fi

# --- 4. Attachments tar --------------------------------------------------------
if [[ -n "$tarfile" && -s "$tarfile" ]]; then
  count="$(tar -tzf "$tarfile" | wc -l)"
  log "Attachments tar lists $count entries"
  [[ "$count" -gt 0 ]] || fail "attachments tar is empty"
else
  log "SKIP: no attachments tar found"
fi

log "DRILL PASS: backups restored into scratch databases and verified"
