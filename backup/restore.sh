#!/usr/bin/env bash
# Restore the authoritative data from a backup produced by backup.sh.
#
#   restore.sh db          [dumpfile]   # latest app-db dump by default
#   restore.sh guacamole   [dumpfile]
#   restore.sh attachments [tarfile]
#   restore.sh all
#
# Restoring REPLACES the live database / attachments volume. The current
# attachments are snapshotted to attachments-pre-restore-*.tar.gz first;
# the pre-restore database is NOT kept (restore into the drill instead if
# you want a second opinion: backup/restore-drill.sh).
#
# Templates are NOT auto-restored: they are plain files. Copy them back
# manually, e.g.
#   rsync -a backups/templates/workhorse-02/ workhorse-02:/var/lib/libvirt/images/ctf/images/
# then recreate pinned templates per agent/REBUILD.md if volumes were wiped.
#
# Runbook: backup/BACKUPS.md. Owner policy: local-only destination, log-file
# alerting — run backup/restore-drill.sh after any restore drill or real
# restore to re-close validation gate §22.39.

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi
BACKUP_DIR="${BACKUP_DIR:-./backups}"
PGUSER="${POSTGRES_USER:-ctf}"
APP_DB="${POSTGRES_DB:-ctf}"

ASSUME_YES=0
ARGS=()
for arg in "$@"; do
  if [[ "$arg" == "--yes" ]]; then
    ASSUME_YES=1
  else
    ARGS+=("$arg")
  fi
done

log()  { echo "==> $*"; }
fail() { echo "==> FATAL: $*" >&2; exit 1; }

confirm() {
  [[ "$ASSUME_YES" == 1 ]] && return 0
  read -r -p "Replace the live $1? Type 'restore' to proceed: " answer
  [[ "$answer" == "restore" ]] || fail "aborted by operator"
}

latest_of() { ls -1t "$BACKUP_DIR"/$1 2>/dev/null | head -n1 || true; }

docker compose ps >/dev/null 2>&1 || fail "docker compose stack is not running"

stop_serving() {
  log "Stopping app/lab-tick/guacamole (db stays up)"
  docker compose stop app lab-tick guacamole
}
start_serving() {
  log "Restarting app/lab-tick/guacamole"
  docker compose start guacamole app lab-tick
}

restore_db() {
  local database="$1" pattern="$2" dumpfile="${3:-}"
  dumpfile="${dumpfile:-$(latest_of "$pattern")}"
  [[ -n "$dumpfile" && -s "$dumpfile" ]] || fail "no dump matching $pattern in $BACKUP_DIR"
  confirm "database '$database' with $(basename "$dumpfile")"

  stop_serving
  # Drop + recreate so no stale objects survive the restore.
  docker compose exec -T db psql -U "$PGUSER" -d postgres -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$database' AND pid <> pg_backend_pid();"
  docker compose exec -T db psql -U "$PGUSER" -d postgres -c "DROP DATABASE IF EXISTS \"$database\";"
  docker compose exec -T db psql -U "$PGUSER" -d postgres -c "CREATE DATABASE \"$database\";"
  # Custom-format dumps stream from stdin (the dump lives on the host).
  docker compose exec -i db pg_restore -U "$PGUSER" -d "$database" --no-owner < "$dumpfile"

  # Smoke check: migrations table is populated and every migration is applied.
  local applied expected
  applied="$(docker compose exec -T db psql -U "$PGUSER" -d "$database" -tAc \
    "SELECT count(*) FROM django_migrations WHERE applied IS NOT NULL;")"
  expected="$(docker compose exec -T db psql -U "$PGUSER" -d "$database" -tAc \
    "SELECT count(*) FROM django_migrations;")"
  [[ "$applied" -gt 0 && "$applied" == "$expected" ]] \
    || fail "restore smoke check failed: $applied/$expected migrations applied"

  start_serving
  log "RESTORE OK: $database from $(basename "$dumpfile")"
}

restore_attachments() {
  local tarfile="${1:-$(latest_of 'attachments-*.tar.gz')}"
  restore_volume "$tarfile" attachments
}

restore_templates() {
  local tarfile="${1:-$(latest_of 'templates-*.tar.gz')}"
  restore_volume "$tarfile" templates
}

restore_volume() {
  local tarfile="$1" kind="$2"
  [[ -n "$tarfile" && -s "$tarfile" ]] || fail "no $kind tar in $BACKUP_DIR"
  confirm "$kind volume with $(basename "$tarfile")"

  APP_CID="$(docker compose ps -q app)"
  local dest
  case "$kind" in
    attachments) dest="/app/private_attachments" ;;
    templates)   dest="/app/private_templates" ;;
  esac
  VOL="$(docker inspect -f "{{range .Mounts}}{{if eq .Destination \"$dest\"}}{{.Name}}{{end}}{{end}}" "$APP_CID")"
  [[ -n "$VOL" ]] || fail "$kind volume not mounted on the app container"

  # Safety snapshot of the current contents before they are replaced.
  local pre="$BACKUP_DIR/$kind-pre-restore-$(date +%Y-%m-%d_%H%M%S).tar.gz"
  log "Snapshotting current $kind to $(basename "$pre")"
  docker run --rm -v "$VOL":/src:ro -v "$(cd "$BACKUP_DIR" && pwd)":/dst \
    alpine tar czf "/dst/$(basename "$pre")" -C /src .

  stop_serving
  log "Clearing and restoring $kind volume '$VOL'"
  docker run --rm -v "$VOL":/src -v "$(cd "$(dirname "$tarfile")" && pwd)":/dst:ro \
    alpine sh -c 'rm -rf /src/..?* /src/.[!.]* /src/*; tar xzf "/dst/$(basename "$0")"' "$tarfile"
  start_serving
  log "RESTORE OK: $kind from $(basename "$tarfile")"
}

[[ -d "$BACKUP_DIR" ]] || fail "no backup directory at $BACKUP_DIR"

case "${ARGS[0]:-}" in
  db)          restore_db "$APP_DB" "db-$APP_DB-*.dump" "${ARGS[1]:-}" ;;
  guacamole)   restore_db guacamole_db "db-guacamole_db-*.dump" "${ARGS[1]:-}" ;;
  attachments) restore_attachments "${ARGS[1]:-}" ;;
  templates)   restore_templates "${ARGS[1]:-}" ;;
  all)         restore_db "$APP_DB" "db-$APP_DB-*.dump"; restore_db guacamole_db "db-guacamole_db-*.dump"; restore_attachments; restore_templates ;;
  *) cat <<EOF >&2
Usage: $0 {db|guacamole|attachments|templates|all} [file] [--yes]

  db          [dumpfile]   restore the app database (latest if omitted)
  guacamole   [dumpfile]   restore guacamole_db
  attachments [tarfile]    restore the attachments volume (auto-snapshots first)
  templates   [tarfile]    restore the machine-templates volume (auto-snapshots first)
  all                      db + guacamole + attachments + templates

--yes skips the interactive confirmation.
EOF
    exit 2 ;;
esac
