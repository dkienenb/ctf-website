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

- [x] Step 4 (in progress): authoritative application model — accounts/approval,
      pools, challenges, questions, accepted answers, submissions with
      database-level attempt/solve/redemption invariants
- [ ] Submission/scoring/correction services + views
- [ ] Steps 1–3: connectivity proof (workers.dev + VPC + Tunnel), one Guacamole
      desktop + isolated target, coordinated save/resume
- [ ] Step 6: durable allocation, queue/leases, connection grants, agents
- [ ] Steps 7–8: isolation/backup verification, launch content
