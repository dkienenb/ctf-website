"""Cloudflare Workers VPC binding rewrites standard proxy headers (Host,
X-Forwarded-Host) to the service target. The Worker sends the public
hostname in a custom header; this middleware promotes it so Django's host
perception (redirects, CSRF, OAuth callback URLs) matches the public URL.
"""


class PublicHostMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        public_host = request.META.get("HTTP_X_PUBLIC_HOST")
        if public_host:
            request.META["HTTP_X_FORWARDED_HOST"] = public_host
        public_proto = request.META.get("HTTP_X_PUBLIC_PROTO")
        if public_proto:
            request.META["HTTP_X_FORWARDED_PROTO"] = public_proto
        return self.get_response(request)
