# Deployment, build state, and development (the original README)

Django 5.2 LTS implementation of the authoritative application, built from the
owner-approved handoff in `../ctf-prototype/docs/` and the recorded owner
decisions in `../ctf-prototype/docs/DECISIONS.md` (2026-11-14).

## Stack

- Django 5.2 LTS + django-allauth 65 (Discord provider, identify scope only)
- PostgreSQL 17 (authoritative database; `compose.yaml` runs it)
- Caddy gateway (portal + Guacamole WebSocket path) behind a Cloudflare
  Tunnel/Worker proxy (`worker/`)
- Guacamole 1.6.0 (JSON auth extension) + per-workhorse `guacd` on the host
  network; per-lab isolated libvirt networks on the workhorses (`agent/`)

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in before production use
docker compose up -d db
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

## Deployed server (controller machine)

```bash
cp .env.example .env   # fill in: GUACD_HOSTNAME is required — see the note
sudo ./server-setup.sh # idempotent: build+up, guacamole_db schema if missing,
                       # portal check, daily backup timer + first backup
```

Re-run after `git pull` to update the deployment (migrations and
collectstatic run in the app entrypoint). Workhorse provisioning is a
separate runbook: `agent/ADD-A-WORKHORSE.md` (firewall §17 + guacd
included); clean rebuilds: `agent/REBUILD.md`; backups: `backup/BACKUPS.md`.

## Build sequence (handoff §24)

- [x] Steps 4–5: authoritative application — accounts/approval, Discord identity,
      pools, challenges, questions, accepted answers, submissions, hints,
      scoring, corrections engine, operator pages
- [x] Step 6 (application side): allocation, queue, leases, warnings, host
      outage handling, scheduler command — database-constraint enforced
- [x] Packaging: Docker image (gunicorn), compose stack (db + app + lab-tick +
      Caddy gateway), Worker proxy
- [x] Steps 1–3: connectivity proof (workers.dev + VPC + Tunnel) — §15 checks
      in `worker/PROOF-RESULTS.md`
- [x] Steps 6 (infrastructure): workhorse agents, Guacamole grants, recovery;
      browser desktop streams end-to-end over workers.dev
      (`worker/DESKTOP-DEBUG.md`)
- [x] Steps 7 (isolation): per-lab libvirt networks + host firewall +
      `agent/verify-isolation.sh` audit; guest-side probes documented there
- [x] Steps 7 (backups): PostgreSQL (app + guacamole_db), attachments volume,
      and pinned-template backups with 14-day retention, restore, and a
      restore drill proving gate §22.39 (`backup/BACKUPS.md`; owner policy:
      local-only, daily, log-file alerting). Deployed controller still needs
      the timer installed (`backup/ctf-backup.timer`) and
      `WORKHORSE_SSH_TARGETS` set in `.env`.
- [ ] Step 8: launch content; real Kali/target templates are owner-supplied
      (demo templates in `agent/demo-images/` until then)
