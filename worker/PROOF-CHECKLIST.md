# Connectivity proof checklist (handoff §15) — run once the Cloudflare account exists

1. `npx wrangler login` (interactive, in the browser).
2. Create the Cloudflare Tunnel (`cloudflared`) bound to this machine's Caddy
   gateway (port 8080) and register it with the Workers VPC private service.
3. Set `ORIGIN_HOST` in `worker/wrangler.toml` to the private service hostname.
4. `npx wrangler deploy` — the workers.dev URL must now serve the portal.
5. Verify through the workers.dev URL **only**:
   - Discord OAuth callback round-trip (cookies, redirects)
   - CSRF login/logout POSTs
   - Attachment download (full request/response body)
   - **WebSocket upgrade + long-lived streaming** (Guacamole path — the top risk)
   - Idle/reconnect behavior at lab expiry
6. Confirm no alternative public origin or unprotected Guacamole route exists.
7. Record results in this directory and update the handoff status notes.
