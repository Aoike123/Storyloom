"""The reverse proxy must be the only source of a client address.

Trusting every peer ("*") let a visitor send its own X-Forwarded-For and appear as a new address,
which reset the per-IP request limits. These tests pin the behaviour the container now relies on.
"""
import asyncio

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

SCOPE = {"type": "http", "method": "GET", "path": "/", "headers": [], "client": ("172.28.0.5", 40000)}


def seen_client(trusted: str, forwarded: str) -> str:
    captured = {}

    async def app(scope, _receive, _send):
        captured["client"] = scope["client"]

    async def run():
        scope = {**SCOPE, "headers": [(b"x-forwarded-for", forwarded.encode())]}
        await ProxyHeadersMiddleware(app, trusted_hosts=trusted)(scope, None, None)

    asyncio.run(run())
    return captured["client"][0]


def test_forged_header_is_ignored_when_only_the_proxy_network_is_trusted():
    # Caddy appends the real address, so the last untrusted entry wins over the forged prefix.
    assert seen_client("172.28.0.0/16", "1.2.3.4, 198.51.100.7") == "198.51.100.7"


def test_trusting_every_peer_would_believe_the_forged_address():
    # Documents the old behaviour the deployment moved away from.
    assert seen_client("*", "1.2.3.4, 198.51.100.7") == "1.2.3.4"


def test_direct_connections_outside_the_proxy_network_keep_their_real_address():
    captured = {}

    async def app(scope, _receive, _send):
        captured["client"] = scope["client"]

    async def run():
        scope = {**SCOPE, "client": ("203.0.113.9", 40000),
                 "headers": [(b"x-forwarded-for", b"1.2.3.4")]}
        await ProxyHeadersMiddleware(app, trusted_hosts="172.28.0.0/16")(scope, None, None)

    asyncio.run(run())
    assert captured["client"][0] == "203.0.113.9"
