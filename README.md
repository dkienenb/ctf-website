# CTF Website — production portal

Django 5.2 LTS implementation of the authoritative application, built from the
owner-approved handoff in `../ctf-prototype/docs/` and the recorded owner
decisions in `../ctf-prototype/docs/DECISIONS.md` (2026-11-14).

## Stack

- Django 5.2 LTS + django-allauth 65 (Discord provider, identify scope only)
- PostgreSQL 17 (authoritative database; `compose.yaml` runs it for development)
- Later: Caddy, Guacamole/guacd, Cloudflare Worker proxy, lab controller/agents

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in before production use
docker compose up -d db
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

## Build sequence (handoff §24)

- [x] Steps 4–5: authoritative application — accounts/approval, Discord identity,
      pools, challenges, questions, accepted answers, submissions, hints,
      scoring, corrections engine, operator pages
- [x] Step 6 (application side): allocation, queue, leases, warnings, host
      outage handling, scheduler command — database-constraint enforced;
      real host agents/libvirt remain
- [x] Packaging: Docker image (gunicorn), compose stack (db + app + lab-tick +
      Caddy gateway), Worker proxy scaffold
- [ ] Steps 1–3: connectivity proof (workers.dev + VPC + Tunnel), one Guacamole
      desktop + isolated target, coordinated save/resume
- [ ] Steps 6 (infrastructure): workhorse agents, Guacamole grants, recovery
- [ ] Steps 7–8: isolation/backup verification, launch content
