// Thin public proxy (handoff §14–§15). The Worker forwards approved
// application traffic to the private web service reached through the
// Workers VPC binding — it must never gain broad reach into the lab network.
//
// Security properties:
// - The destination is fixed by configuration (ORIGIN_HOST/ORIGIN_PORT vars);
//   no user-supplied URL selects an internal target.
// - X-Forwarded-Proto=https tells Django the browser edge was TLS.
// - Host is preserved so redirects/CSRF round-trip back through the public URL.
// - Only the web gateway crosses the tunnel; lab traffic never does.


export default {
  async fetch(request, env) {
    const ORIGIN = `http://${env.ORIGIN_HOST}:${env.ORIGIN_PORT}`;
    // env.ORIGIN_HOST/ORIGIN_PORT come from wrangler.toml [vars].
    const url = new URL(request.url);
    const headers = new Headers(request.headers);
    headers.set("X-Forwarded-Proto", "https");
    headers.set("X-Forwarded-Host", url.host);
    headers.set("Host", url.host);
    // The VPC binding rewrites standard proxy headers to the service target;
    // the public hostname and scheme travel in custom headers instead.
    headers.set("X-Public-Host", url.host);
    headers.set("X-Public-Proto", "https");
    const upstream = new Request(`${ORIGIN}${url.pathname}${url.search}`, {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
      redirect: "manual",
    });
    try {
      return await env.GATEWAY.fetch(upstream);
    } catch (error) {
      return new Response("Portal origin unreachable through the tunnel.", { status: 503 });
    }
  },
};
