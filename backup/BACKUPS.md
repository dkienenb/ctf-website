# Backups — runbook (handoff §19, validation gate §22.39)

Owner policy (2026-11-15): **local-only** destination for now, **daily** run,
**14-day retention**, **log-file-only** alerting. Template rsync is enabled
(workhorses → controller). The off-machine copy decision is deferred; a hook
(`BACKUP_REMOTE_DEST`) is reserved for it.

## What is backed up

| Data class (§19) | How | Where |
| --- | --- | --- |
| PostgreSQL app DB (users, approvals, content, answers, submissions, solves, redemptions) | `pg_dump -Fc` via the compose `db` container | `BACKUP_DIR/db-<name>-<stamp>.dump` |
| guacamole_db (connection records the grants point at) | `pg_dump -Fc` | `BACKUP_DIR/db-guacamole_db-<stamp>.dump` |
| Challenge attachments | tar of the compose `attachments` volume | `BACKUP_DIR/attachments-<stamp>.tar.gz` |
| Pinned VM templates (owner-supplied Kali/target) | rsync from each workhorse's `images/` (templates only — writable guest disks are **not** backed up; guest-state loss falls back to the pinned template per the accepted policy) | `BACKUP_DIR/templates/<host>/` |
| Guest writable-disk state (`disks/`) | **deliberately not backed up** | — |

Retention prunes each family independently to the newest
`BACKUP_RETENTION_DAYS` (14) sets.

## Schedule (controller machine)

```bash
sudo ./server-setup.sh   # installs backup/ctf-backup.{service,timer} and
                         # enables the timer (idempotent; also runs the
                         # first backup if none exists yet)
systemctl list-timers ctf-backup.timer   # next run
```

Manual run: `backup/backup.sh` (idempotent; safe to run any time).

## Alerts / response process

Owner decision (§23, 2026-11-14): operational warnings arrive as **on-platform
admin notifications** (in-app inbox), not a webhook. `backup.sh` delivers one
automatically on any failure via `manage.py notify_admins` (when the stack
itself is down, only the log is available — that's the first thing to check).
The daily response check is:

1. Admin notification inbox (an alert lands there on any backup failure),
2. `tail -n 20 backups/backup.log` — last run summary ("BACKUP OK"),
3. `systemctl status ctf-backup.service` — timer fired and exited 0.

If `WORKHORSE_SSH_TARGETS` is empty the run **warns loudly** and skips
template sync — templates are owner-supplied launch content; either fill in
the targets or keep your own off-machine copy of the template files.

## Restoring (real incident)

`backup/restore.sh db|guacamole|attachments|all [--yes] [file]` — restores
into the live stack (stops app/lab-tick/guacamole, drops + recreates the
database, `pg_restore`, smoke-checks migrations, restarts). The current
attachments are snapshotted to `attachments-pre-restore-*.tar.gz` first.
Restore templates by copying the plain files back:

```bash
rsync -a backups/templates/workhorse-02/ workhorse-02:/var/lib/libvirt/images/ctf/images/
```

(then recreate pinned templates per `agent/REBUILD.md` if the volumes were
wiped — the agent recreates templates from the image files on demand).

## Restore drill — validation gate §22.39

`backup/restore-drill.sh` proves the backups restore: dumps are restored
into **scratch** databases (`ctf_drill`, `guacamole_drill`; live DBs are
never touched), Django is pointed at the drill DB and must pass
`check --database default` and `migrate --check`, key row counts are
reported, and the attachments tar must be a readable non-empty archive.
Run it:

- once right after enabling backups,
- after any change to `backup.sh` / compose volumes / retention,
- quarterly, and after any real restore.

Drills verify the **local** tree; once an off-machine destination is
configured, re-run the drill from the remote host too.
