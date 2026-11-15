# Desktop streaming — current state and debugging handoff

Written 2026-11-15 for continuation in a fresh conversation. Read this first:
the ONLY broken piece is the last hop of the browser-desktop flow. Everything
else in the platform is built, deployed, and verified.

## The single remaining problem

Portal → Guacamole works up to the client UI. Opening the connection
(`#/client/json.lab-1?data=<grant>`) loads the client page and starts the
connection, which then dies with:

> "An internal error has occurred within the Guacamole server, and the
> connection has been terminated."
> (A second pane, from the post-failure reconnect attempt, shows "The
> requested connection does not exist" — that is a symptom of the first
> error killing the session, not a separate problem.)

"Internal error within the Guacamole server" in Guacamole terms = the
**webapp ↔ guacd** hop or **guacd ↔ VNC** hop failed. Evidence gathered so
far:

- guacd on the workhorse is reachable from the controller:
  `172.20.201.175:4822` accepts connections (verified via TCP probe).
- The grant's connection parameters route to the per-workhorse guacd:
  `guacd-hostname=172.20.201.175`, `guacd-port=4822`, `guacd-encryption=""`,
  `hostname=127.0.0.1`, `port=5901` (per-lab deterministic port).
- The attacker VM was defined with that exact port (allocate result:
  `vnc_port: 5901`), listening on loopback of workhorse-02.
- The connection DOES resolve (the client page and connection attempt
  happen), so auth + connection provisioning are fine.

## FIRST thing to check in the fresh session

```sh
cd /home/dkienenb/Apps/ctf/ctf-website
docker compose logs guacamole --since 10m | grep -iE "guacd|error|connect"
```

then click "Open browser desktop" again and watch. The log will name the
guacd address the webapp actually dialed and the failure mode:

- If it dialed `guacd:4822` (the CONTROLLER's local guacd container) instead
  of `172.20.201.175:4822` → the **per-connection guacd-hostname parameter
  was not honored**. Likely causes: parameter name wrong for 1.6
  (`guacd-hostname`/`guacd-port`/`guacd-encryption` are documented connection
  parameters in `configuring-guacamole.html` — verify against 1.6 docs), the
  JSON extension stripping unknown parameters, or the empty-string
  `guacd-encryption` value being rejected (try omitting it entirely).
- If it dialed `172.20.201.175:4822` and failed → network or TLS: check
  `guacd-encryption` (we send empty string = plaintext; try `"none"` or
  omitting), and check the guacd container on workhorse-02
  (`docker logs ctf-guacd`) for "inbound connection" errors.
- If guacd accepted but the VNC hop failed, the workhorse guacd log shows
  the VNC connect error (wrong port / VM not running / loopback binding).

## What is verified and working (do not re-litigate)

- **Application** (`ctf-website` repo, deployed via `docker compose` on the
  controller machine): Django 5.2 portal — Discord-only auth (allauth, works
  through workers.dev), approval/reject/suspend/pre-approve, pools/challenges/
  questions/answers/hints/attachments, idempotent submissions with 10s
  cooldown, scoring/ranking, corrections engine (3 modes + previews +
  revision guard), operator pages at /manage/, demo content seeded.
- **Labs application side**: allocation/queue/lease rules with DB-enforced
  single allocation, durable HostCommand queue + agent API (token auth),
  per-lab deterministic VNC ports (5900 + lab.id % 1000), notifications
  (toasts live in the UI now), admin reset/recover commands.
- **Workhorse-02** (remote Kali, user student13): agent installed as
  systemd `ctf-agent`, repo clone at `~/ctf-website` (NOTE: not
  /opt/ctf/website), agent code at /opt/ctf/agent, config at
  /etc/ctf/agent.toml (controller_url, host_name workhorse-02, token,
  management_ip=172.20.201.175). Real VMs boot; full lifecycle verified:
  allocate → run → **save (virsh save both guests, artifacts in
  /var/lib/libvirt/images/ctf/disks/saved-lab1)** → release → resume.
  workhorse-01 (this controller machine) is registered but NOT installed;
  its disk is 92% full → admissions correctly refused there.
- **Guacamole containers on the controller** (compose: `guacd` +
  `guacamole` with WEBAPP_CONTEXT=ROOT, JSON auth extension 1.6.0 mounted at
  /root/.guacamole/extensions, its own DB guacamole_db in the same Postgres,
  schema in guac/schema.sql). Signed+encrypted JSON grants implemented in
  `portal/apps/labs/guacamole.py` per the official contract (HMAC prepended,
  AES-128-CBC zero IV, `data` parameter) — verified against the live
  extension: POST /api/tokens → 200 + authToken; connections materialize
  under the `json` data source (`/api/session/data/json/connections` shows
  `lab-1`).
- **Public path**: Worker `ctf-portal-proxy` → Workers VPC service → Tunnel
  `ctf-portal` → Caddy (127.0.0.1:8090 and 172.20.201.125:8090) → app.
  All §15 checks passed except WebSocket streaming (this problem).
  Details in `worker/PROOF-RESULTS.md`.

## Gotchas discovered the hard way (already fixed; don't reintroduce)

- Cloudflare's VPC binding **rewrites Host/X-Forwarded-Host/X-Forwarded-Proto**
  — the Worker sends `X-Public-Host`/`X-Public-Proto` and
  `PublicHostMiddleware` promotes them.
- Guacamole SPA reads auth params **from inside the URL fragment**
  (`#/client/json.lab-1?data=...`), and client URLs address connections as
  `<dataSource>.<connectionId>`.
- Deep-link fragment contains the grant: tests must strip the fragment
  before decrypting (`url.split("data=")[1].split("#")[0]`).
- The lab's domain names are **lab-keyed** (`ctf-lab1-atk/tgt`), not
  op-keyed; the agent reuses a preserved attacker but rebuilds a
  port-mismatched one; target is always rebuilt.
- Never `rm` disk files of a running lab (causes unlinked-inode save
  failures); use admin reset actions.
- `/var/lib/libvirt/images/ctf` (not /var/lib/ctf/agent) is the storage root
  — AppArmor-permitted by default on Kali.
- Compose passes ONLY explicitly declared env vars to containers.

## Tools already in the repo

- `agent/ADD-A-WORKHORSE.md` — full provisioning runbook
- `agent/REBUILD.md` — clean-rebuild scopes (wipe.sh included)
- `agent/debug-save.sh` — save-failure isolation (standalone disk vs backing)
- `agent/demo-setup.sh` — demo VM templates (Alpine virt ISO, per-lab VNC
  port placeholder @VNC_PORT@, target has no graphics)
- `agent/guacd-install.sh` — per-workhorse guacd + firewall guard

## Likely next steps (in order)

1. Capture `docker compose logs guacamole` during a connection attempt
   (see FIRST THING above) — the dialed guacd address decides the branch.
2. If per-connection guacd params are the issue, simplest robust fallback:
   point the CONTROLLER's local guacd... no — instead make the webapp reach
   the workhorse guacd by another means: run guacd for the webapp on the
   controller but have it proxy... not possible. Realistic fallback: set the
   webapp's GUACD_HOSTNAME to a single guacd per test and validate guacd→VNC
   hop first, then reintroduce per-connection routing.
3. Once the desktop streams via workers.dev → update
   `worker/PROOF-RESULTS.md` (WebSocket check = §15 fully closed).
4. Remaining platform work after that: real Kali/target templates
   (owner-supplied), §17 network isolation hardening, launch content.
