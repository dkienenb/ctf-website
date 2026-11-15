# Workers VPC connectivity proof — results

Date: 2026-11-14. Account: dkienenb@gmail.com (free plan, no domain purchased).
Everything below was exercised through the public URL **only**:
`https://ctf-portal-proxy.dkienenb-jtac-ctf.workers.dev`

## Chain under test

Browser → (TLS, workers.dev) → Worker `ctf-portal-proxy` → Workers VPC
service `ctf-portal-http` (01a1db89-8030-7a33-85a3-5c5a1b1d08ce) →
Cloudflare Tunnel `ctf-portal` (e9f325c4-cf7a-46f5-a79b-53eddfc67c8b,
connector on this machine) → Caddy gateway (172.20.201.125:8090) →
gunicorn/Django → PostgreSQL.

## Verified

| # | Check | Result |
| --- | --- | --- |
| 1 | Portal reachable at workers.dev; auth redirects round-trip | ✅ 302 → login chain |
| 2 | Themed Discord sign-in page renders | ✅ 200 |
| 3 | Static assets (CSS) serve through the chain | ✅ 200 |
| 4 | CSRF-protected POST rejects token-less requests | ✅ 403 |
| 5 | CSRF-protected POST with valid token + Referer accepted | ✅ 302 hand-off |
| 6 | Agent API reachable but token-gated | ✅ 401 without credentials |
| 7 | Unknown paths handled | ✅ 404 (production page, DEBUG=0) |
| 8 | Protected attachment redirects anonymous users to login | ✅ 302 with `next` |
| 9 | **Full Discord OAuth round-trip** (authorize → callback → session) | ✅ verified interactively |
| 10 | Identity consistency across hosts | ✅ one account; login through the public URL reused it |
| 11 | OAuth tokens stored for the explicit profile refresh | ✅ SocialToken recorded, expires 2026-11-21 |
| 12 | Cookie/session behavior through the proxy | ✅ interactive login succeeded |
| 13 | Public URL correctness for Django (Host/scheme behind the VPC binding) | ✅ PublicHostMiddleware + X-Public-Host/X-Public-Proto |

## Implementation notes (durable behaviors, not one-offs)

- The VPC binding **rewrites standard proxy headers** (`Host`,
  `X-Forwarded-Host`, `X-Forwarded-Proto`) to the private target. The Worker
  ships the public hostname/scheme in `X-Public-Host`/`X-Public-Proto`, and
  `PublicHostMiddleware` promotes them. All host/scheme-sensitive behavior
  (OAuth callback URLs, CSRF origins, absolute redirects) now derives from
  the true public URL.
- `DJANGO_ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` list the public hostname.
- `DJANGO_DEBUG=0` in production.
- The worker's destination is fixed by configuration; no user-supplied URL
  selects an internal target (§15 boundary).

## Remaining

- **WebSocket upgrade + long-lived desktop streaming** through this exact
  path: ✅ verified 2026-11-15 end-to-end over workers.dev — a real
  click-through streamed the lab desktop in the browser. §15 is closed.
  (Fixes that made it work are in `worker/DESKTOP-DEBUG.md`.)
- Idle/reconnect behavior at lab expiry: ✅ owner-tested 2026-11-14 with
  real desktop sessions (lease expiry saved the guests and the desktop
  flow behaved as expected). Validation gate §22 "workhorse-local expiry
  runs through controller disconnection" is closed.
- Free-tier consumption under expected concurrency: measured at launch.

## Setup record

- Tunnel: `ctf-portal` (wrangler tunnel create; connector runs via
  `wrangler tunnel run` on this machine)
- VPC service: `ctf-portal-http` (http, 172.20.201.125:8090, tunnel-bound)
- Worker: `ctf-portal-proxy` (vpc_services binding GATEWAY; vars
  ORIGIN_HOST/ORIGIN_PORT) — config in `worker/wrangler.toml`
- Subdomain: `dkienenb-jtac-ctf.workers.dev` (registered via API)
