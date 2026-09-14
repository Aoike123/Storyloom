"""Fail-closed browser-origin boundary for the public hosted demo."""

from urllib.parse import urlsplit


LOCAL_WORKSPACE_ORIGIN = b"http://127.0.0.1:3000"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def normalize_public_origin(value: str) -> str:
    """Return a canonical HTTPS origin and reject paths or embedded credentials."""
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except (AttributeError, ValueError) as exc:
        raise ValueError("STORYLOOM_PUBLIC_ORIGIN is not a valid URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("STORYLOOM_PUBLIC_ORIGIN must be one HTTPS origin without a path")
    host = parsed.hostname.lower()
    authority = f"{host}:{port}" if port and port != 443 else host
    return f"https://{authority}"


class ProductionOriginBoundary:
    """Validate public write origins, then adapt them to the local-only application."""

    def __init__(self, application, public_origin: str):
        self.application = application
        self.public_origin = normalize_public_origin(public_origin).encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method", "GET").upper() not in UNSAFE_METHODS:
            await self.application(scope, receive, send)
            return

        headers = list(scope.get("headers", []))
        origins = [value for key, value in headers if key.lower() == b"origin"]
        if origins and (len(origins) != 1 or origins[0] != self.public_origin):
            await self._forbidden(send)
            return

        if origins:
            headers = [(key, value) for key, value in headers if key.lower() != b"origin"]
            headers.append((b"origin", LOCAL_WORKSPACE_ORIGIN))
            scope = {**scope, "headers": headers}
        await self.application(scope, receive, send)

    @staticmethod
    async def _forbidden(send):
        body = b'{"detail":"Request origin is not allowed"}'
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
