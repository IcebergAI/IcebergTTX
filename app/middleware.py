"""ASGI middleware: per-request audit context (#23) and CSRF origin checks (#10)."""

from urllib.parse import urlparse
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings
from app.services import audit_service

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def client_ip(request: Request) -> str | None:
    """Source IP as resolved by uvicorn's ProxyHeadersMiddleware (#36).

    uvicorn rewrites ``request.client`` from ``X-Forwarded-For`` **only** when the
    immediate peer is a trusted proxy (``--forwarded-allow-ips`` /
    ``FORWARDED_ALLOW_IPS``). The app is reachable only through the nginx reverse
    proxy, which appends the real client hop, so an untrusted client cannot spoof
    this value (the old hand-rolled leftmost-XFF parse trusted whatever the client
    sent). This IP feeds the audit ``source_ip`` and the login rate-limit key.
    """
    return request.client.host if request.client else None


class AuditContextMiddleware:
    """Populate the audit request context (request id, source IP, method, path)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        request_id = request.headers.get("x-request-id") or uuid4().hex
        token = audit_service.set_request_context(
            request_id=request_id,
            source_ip=client_ip(request),
            method=request.method,
            path=request.url.path,
        )

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            audit_service.reset_request_context(token)


class CSRFOriginMiddleware:
    """Verify the Origin/Referer of cookie-authenticated state-changing requests.

    Requests carrying a Bearer ``Authorization`` header are exempt (they cannot
    be driven by an ambient cookie), as are auth endpoints and safe methods.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    def _origin_allowed(self, request: Request) -> bool:
        source = request.headers.get("origin") or request.headers.get("referer")
        if not source:
            return False
        host = urlparse(source).netloc
        if not host:
            return False
        allowed = set(settings.trusted_origin_set)
        request_host = request.headers.get("host")
        if request_host:
            allowed.add(request_host)
        return host in allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        path = request.url.path
        method = request.method
        headers = Headers(scope=scope)
        needs_check = (
            method not in SAFE_METHODS
            and path.startswith("/api/")
            and not path.startswith("/api/auth/")
            and "access_token" in request.cookies
            and not headers.get("authorization", "").startswith("Bearer ")
        )
        if needs_check and not self._origin_allowed(request):
            audit_service.emit(
                "csrf.blocked",
                result="deny",
                reason="origin check failed",
                severity="warning",
            )
            response = JSONResponse(
                status_code=403, content={"detail": "CSRF origin check failed"}
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
