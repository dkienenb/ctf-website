// Thin public proxy (handoff §14–§15). The Worker forwards approved
// application traffic to the private web service reached through the
// Cloudflare Tunnel — it must never gain broad reach into the lab network.
//
// STATUS: unproven until the connectivity test with a real Cloudflare
// account passes, including the Guacamole WebSocket upgrade behavior.

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    // Only the gateway origin is reachable; no user-supplied destinations.
    const origin = `https://${env.ORIGIN_HOST}`;
    const upstream = new Request(origin + url.pathname + url.search, request);
    return fetch(upstream);
  },
};
