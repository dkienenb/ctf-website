# CTF Practice Platform

A self-hosted capture-the-flag **practice** site: players sign in with
Discord, solve challenges (questions, files, and hints) to earn points, and
spin up real, isolated lab machines — a **Kali attacker** desktop and a
**vulnerable target** — that they drive from their browser.

Built for a small trusted community (players are individually approved by
an operator). Everything runs on owner-owned hardware: one **controller**
server for the website and one or more **workhorse** machines for the VMs.

```
 Player's browser
        │  (HTTPS over Cloudflare)
        ▼
 Cloudflare Worker proxy ──► Caddy gateway ──► Django app (challenge site)
        (public entry)                          │        │
                                                │        └─► Guacamole (desktop gateway)
                                                │                  │
                                                ▼                  ▼
                                          PostgreSQL 17      guacd + libvirt VMs
                                          (authoritative      on a workhorse
                                           data)              machine, behind
                                                              a host firewall
```

## What players get

- **Challenges** organized into **pools**, with questions worth points,
  downloadable attachments (private, permission-checked), and purchasable
  **hints** that deduct from the score.
- **A lab desktop in the browser**: each challenge can carry a VM pair
  (Kali attacker + vulnerable target). Desktop streaming is provided by
  Apache Guacamole over WebSockets — no VPN, no client install.
- **Real persistence**: labs are **saved to disk on lease expiry** and
  restored on the next allocation, so attacker workspaces survive breaks.
  If a guest can't be recovered, the platform falls back to the pinned
  template and tells the player.
- **Live scoring**: leaderboard, per-challenge progress, early-solve
  tiebreaking, and a correction engine that regrades historical answers
  when operator content changes (players are notified of score impacts).
- **Notifications**: approval decisions, lab saves, recovery fallbacks,
  hint refunds, and operator alerts land in an in-app inbox.

## What operators get

- An **approval workbench**: pre-approve by Discord ID before first login,
  approve/reject/suspend/reinstate accounts — all with confirm pages,
  audit events, and DataRevision guards against concurrent edits.
- **Content authoring**: pools, challenges, questions, accepted answers,
  hint pricing with retroactive-reprice impact previews, lifecycle
  (draft → published → archived) with go-live scheduling.
- **Lab operations**: capacity-based admission with free-storage reserve,
  a durable command queue to workhorse agents, targeted resets (target
  only / full / simulated loss), and host-outage handling.
- **Automatic backups** (PostgreSQL + attachments + VM templates) with a
  restore drill that proves recoverability, and on-platform failure
  alerts. See `backup/BACKUPS.md`.

## The tech stack, in plain terms

| Layer | Technology | Why |
| --- | --- | --- |
| Web application | **Django 5.2 LTS** (Python, gunicorn) | Server-rendered pages, mature admin/ORM, long-term support |
| Sign-in | **django-allauth** with the **Discord** provider (`identify` scope only) | Players are known by their stable Discord ID; no passwords stored |
| Database | **PostgreSQL 17** | Authoritative store for accounts, content, submissions, scores, audit ledger |
| Desktop streaming | **Apache Guacamole 1.6.0** + per-workhorse **guacd** | Renders RDP/VNC desktops to the browser; per-lab connections issued as signed, encrypted JSON grants |
| Lab VMs | **libvirt/KVM** with qcow2 overlay disks on workhorses | Real, isolated machines per lab; save/restore to disk |
| Isolation | Per-lab libvirt networks + a host firewall on workhorses | Labs cannot reach each other, the host, or the home LAN (§17 of the handoff) |
| Public entry | **Cloudflare Tunnel + Worker + VPC service binding** | Exposes only the intended origin over HTTPS; the Worker's destination is fixed by configuration |
| Local gateway | **Caddy** | Terminates the tunnel path and routes `/guacamole/` WebSockets to the Guacamole container |
| Packaging | **Docker Compose** (db, app, lab-tick scheduler, Caddy, Guacamole) | One-command stack on the controller |
| Workhorse agent | Python systemd service with a closed virsh vocabulary | Takes durable commands from the controller; enforces lease deadlines locally even if the controller is down |

## How identity and access work

- A player is exactly one Discord ID. Approval state (pending / approved /
  suspended / rejected) is an authorization condition, not a UI toggle.
- Lab desktops require an approved account; a signed, encrypted grant
  (HMAC + AES, shared secret) carries the connection to Guacamole and
  expires with the lease.
- Operators are flagged `is_admin` separately from approval; recovery if
  all admins are locked out is a CLI command, `manage.py grant_admin`
  (see `POLICIES.md`).

## Repository map

| Path | What's there |
| --- | --- |
| `portal/` | The Django project: `apps/accounts` (approval, identity), `apps/content` (pools, challenges, hints), `apps/submissions` (scoring, corrections, audit, operator pages), `apps/labs` (hosts, leases, grants, desktop) |
| `agent/` | Workhorse-side: the agent (`agent.py`), install/isolation/guacd scripts, demo templates, runbooks (`ADD-A-WORKHORSE.md`, `REBUILD.md`) |
| `worker/` | Cloudflare Worker proxy config and connectivity proof records |
| `caddy/` | Gateway configuration (portal + Guacamole WebSocket route) |
| `backup/` | Nightly backup, restore, and the restore-drill scripts + runbook |
| `compose.yaml` | The controller's service stack |
| `server-setup.sh` | Idempotent controller bootstrap (env, stack, schema, backup timer) |
| `docs/DEPLOYMENT.md` | Deployment steps, development setup, and the build-sequence state |
| `POLICIES.md` | Recorded owner policy decisions and what remains open |

## Getting started

- **Deploy it**: `docs/DEPLOYMENT.md` — one `.env`, one `server-setup.sh`.
- **Add a VM host**: `agent/ADD-A-WORKHORSE.md`.
- **Backups & recovery**: `backup/BACKUPS.md`.

The authoritative product spec and recorded decisions live in
`../ctf-prototype/docs/` (handoff, decisions, traceability, validation).
