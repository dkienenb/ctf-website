#!/usr/bin/env bash
# Daily backup of the authoritative data (handoff §19): the PostgreSQL app
# database + guacamole_db, the challenge-attachments volume, and the pinned
# VM templates rsynced off the workhorses. Guest disk state (writable disks)
# is deliberately NOT backed up: per the owner's policy, guest-state loss
# falls back to the pinned template (labs/services.py notifies users).
#
# Owner policy (2026-11-15): LOCAL-ONLY destination for now, daily run,
# 14-day retention, log-file-only alerting (this script logs loudly and
# exits nonzero on failure). Restore: backup/restore.sh; the restore drill
# that satisfies validation gate §22.39 ("backups can actually be
# restored") is backup/restore-drill.sh. Runbook: backup/BACKUPS.md.

set -euo pipefail

cd "$(dirname "$0")/.."

# Configuration: defaults live in .env; real environment overrides.
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi
BACKUP_DIR="${BACKUP_DIR:-./backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_LOG="$BACKUP_DIR/backup.log"
# Space-separated SSH targets (aliases from ~/.ssh/config or user@host) of
# the workhorses whose pinned templates are rsynced into the backup tree.
# Empty = template sync is SKIPPED with a loud warning.
WORKHORSE_SSH_TARGETS="${WORKHORSE_SSH_TARGETS:-}"
WORKHORSE_IMAGES_DIR="${WORKHORSE_IMAGES_DIR:-/var/lib/libvirt/images/ctf/images}"
# Reserved for the future off-machine copy (owner deferred the decision):
# when nonempty, the backup tree is mirrored there with rsync -a --delete.
BACKUP_REMOTE_DEST="${BACKUP_REMOTE_DEST:-}"

stamp="$(date +%Y-%m-%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
exec > >(tee -a "$BACKUP_LOG") 2>&1

log()  { echo "==> [$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
fail() { log "FATAL: $*"; exit 1; }

# §23 owner decision (2026-11-14): operational warnings go through the
# on-platform notification system. On any nonzero exit, try to leave an
# admin notification; when the stack itself is down, the log is all there is.
alert_failure() {
  status=$?
  [[ $status -eq 0 ]] && return 0
  log "Attempting on-platform admin notification for the failure"
  docker compose exec -T app python manage.py notify_admins \
    "Nightly backup FAILED (exit $status). Details in backups/backup.log on the controller." \
    >/dev/null 2>&1 || log "(on-platform notification could not be delivered — stack down? check the log on the host)"
  exit $status
}
trap alert_failure EXIT

[[ -f compose.yaml ]] || fail "compose.yaml not found (run from the repo root)"
docker compose ps --status running >/dev/null 2>&1 \
  || fail "docker compose stack is not running (docker compose up -d db first)"

PGUSER="${POSTGRES_USER:-ctf}"
APP_DB="${POSTGRES_DB:-ctf}"

# --- 1. Application database ------------------------------------------------
log "Dumping app database '$APP_DB'"
docker compose exec -T db pg_dump -U "$PGUSER" -d "$APP_DB" -Fc \
  > "$BACKUP_DIR/db-$APP_DB-$stamp.dump"
APP_DUMP="$BACKUP_DIR/db-$APP_DB-$stamp.dump"
[[ -s "$APP_DUMP" ]] || fail "app database dump is empty"
docker compose exec -i db sh -c 'pg_restore --list' < "$APP_DUMP" >/dev/null \
  || fail "app database dump failed pg_restore --list sanity check"

# --- 2. guacamole_db ----------------------------------------------------------
# Grants are re-issuable via the signed-JSON extension, but guacamole_db
# stores the connection records the grants point at; dump it so a restore
# needs no re-registration dance. The app DB user is the cluster superuser.
log "Dumping guacamole_db"
docker compose exec -T db pg_dump -U "$PGUSER" -d guacamole_db -Fc \
  > "$BACKUP_DIR/db-guacamole_db-$stamp.dump"
GUAC_DUMP="$BACKUP_DIR/db-guacamole_db-$stamp.dump"
[[ -s "$GUAC_DUMP" ]] || fail "guacamole_db dump is empty"

# --- 3. Attachments volume ----------------------------------------------------
APP_CID="$(docker compose ps -q app)"
ATTACH_VOL="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/private_attachments"}}{{.Name}}{{end}}{{end}}' "$APP_CID")"
[[ -n "$ATTACH_VOL" ]] || fail "attachments volume not mounted on the app container"
log "Tarring attachments volume '$ATTACH_VOL'"
docker run --rm -v "$ATTACH_VOL":/src:ro -v "$(cd "$BACKUP_DIR" && pwd)":/dst \
  alpine tar czf "/dst/attachments-$stamp.tar.gz" -C /src .
[[ -s "$BACKUP_DIR/attachments-$stamp.tar.gz" ]] || fail "attachments tar is empty"

# --- 3b. Templates volume -------------------------------------------------------
# Operator-uploaded machine templates (§19 data class): same treatment as
# attachments. The volume name is resolved from the app container mounts.
APP_CID="$(docker compose ps -q app)"
TEMPLATES_VOL="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/private_templates"}}{{.Name}}{{end}}{{end}}' "$APP_CID")"
[[ -n "$TEMPLATES_VOL" ]] || fail "templates volume not mounted on the app container"
log "Tarring templates volume '$TEMPLATES_VOL'"
docker run --rm -v "$TEMPLATES_VOL":/src:ro -v "$(cd "$BACKUP_DIR" && pwd)":/dst \
  alpine tar czf "/dst/templates-$stamp.tar.gz" -C /src .
[[ -s "$BACKUP_DIR/templates-$stamp.tar.gz" ]] || fail "templates tar is empty"

# --- 4. Workhorse pinned templates -------------------------------------------
if [[ -n "$WORKHORSE_SSH_TARGETS" ]]; then
  for host in $WORKHORSE_SSH_TARGETS; do
    log "Rsyncing pinned templates from $host:$WORKHORSE_IMAGES_DIR"
    rsync -a --delete "$host:$WORKHORSE_IMAGES_DIR/" "$BACKUP_DIR/templates/$host/"
  done
else
  log "WARNING: WORKHORSE_SSH_TARGETS is empty — template sync SKIPPED."
  log "WARNING: Templates are owner-supplied launch content (§19); without"
  log "WARNING: this sync or your own off-machine copy they are not backed up."
fi

# --- 5. Retention --------------------------------------------------------------
# Keep the newest $BACKUP_RETENTION_DAYS sets: prune per family (app db,
# guacamole_db, attachments) by count so one backup run never starves the
# other families of their full window.
prune_family() {
  local pattern="$1" keep="$2"
  ls -1t "$BACKUP_DIR"/$pattern 2>/dev/null | tail -n +$((keep + 1)) | xargs -r rm -f --
}
prune_family "db-$APP_DB-*.dump" "$BACKUP_RETENTION_DAYS"
prune_family "db-guacamole_db-*.dump" "$BACKUP_RETENTION_DAYS"
prune_family "attachments-*.tar.gz" "$BACKUP_RETENTION_DAYS"
prune_family "templates-*.tar.gz" "$BACKUP_RETENTION_DAYS"
log "Retention: keeping the last $BACKUP_RETENTION_DAYS of each family"

# --- 6. Reserved off-machine hook (owner deferred) ------------------------------
if [[ -n "$BACKUP_REMOTE_DEST" ]]; then
  log "Mirroring backup tree to $BACKUP_REMOTE_DEST"
  rsync -a --delete "$BACKUP_DIR/" "$BACKUP_REMOTE_DEST/"
else
  log "Off-machine copy: none configured (BACKUP_REMOTE_DEST empty) — local-only policy"
fi

log "Summary (this run):"
ls -lh "$APP_DUMP" "$GUAC_DUMP" "$BACKUP_DIR/attachments-$stamp.tar.gz" "$BACKUP_DIR/templates-$stamp.tar.gz" | sed 's/^/    /'
log "BACKUP OK"
