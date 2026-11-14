# Clean rebuild: both machines from zero

When state has drifted through debugging cycles, rebuild instead of patching —
every fix lives upstream in the repo, so a rebuild is the shortest path to a
known-good state. Three scopes; pick what you need.

## Scope A — workhorse wipe + reinstall (~10 minutes per machine)

On the workhorse (this works identically on workhorse-01 and workhorse-02):

```sh
cd ~/ctf-website && git pull
sudo ./agent/wipe.sh          # removes agent, config, VMs, demo network, storage
sudo ./agent/install.sh       # reinstalls agent + storage dirs (idempotent)
sudo ./agent/demo-setup.sh    # demo templates + private network
sudo systemctl restart ctf-agent
```

Then on the **controller**, rotate the token and hand it over:

```sh
.venv/bin/python manage.py register_host workhorse-02 --capacity 1
# paste the printed token into /etc/ctf/agent.toml on the workhorse:
sudoedit /etc/ctf/agent.toml   # controller_url, host_name, token
sudo systemctl restart ctf-agent
```

Verify (workhorse): `systemctl status ctf-agent` stays running, no tracebacks.
Verify (controller): heartbeat fresh, resources reported, `admits` correct.

## Scope B — controller data reset (~5 minutes)

Full application-data reset (accounts, content, labs — keeps `.env` secrets,
schema, and the deployed stack):

```sh
cd /home/dkienenb/Apps/ctf/ctf-website
docker compose exec -T db psql -U ctf -d guacamole_db < guac/schema.sql   # only on first-ever init
.venv/bin/python manage.py flush --noinput        # truncate all application data
.venv/bin/python manage.py migrate                # re-apply any pending migrations
.venv/bin/python manage.py seed_demo              # demo content
.venv/bin/python manage.py register_host workhorse-01 --capacity 1
.venv/bin/python manage.py register_host workhorse-02 --capacity 1
```

Note what a flush **keeps by design**: your Django admin bootstrap is gone
(re-create `manage.py createsuperuser` if you use /admin/), Discord accounts
vanish (next Discord login recreates yours, pending — approve + admin again
via the steps in DECISIONS.md or `manage.py shell`), and Guacamole's
guacamole_db is separate (not flushed).

**Total wipe** (schema + volumes, including attachments volume): add
`docker compose down -v && docker compose up -d` before the migrate step.
This deletes the private attachments too; run `seed_demo --update` after.

## Scope C — everything (~20 minutes, all fresh)

1. Scope B (controller reset), including each workhorse's fresh tokens.
2. Scope A on every workhorse.
3. Sanity pass: portal pages, Discord login, one lab request → claim →
   desktop, save → release → resume.

## What never needs rebuilding

- `.env` (Discord OAuth, JSON secrets) — survives every scope; keep it.
- The Cloudflare side (tunnel, VPC service, Worker deployment, DNS).
- Guacamole's guacamole_db (independent schema and data).
- KVM/libvirt packages and the OS on the workhorses.
