# A minimal synchronous driver for the ASGI application under test.
#
# Every request goes through the real application — the same transport the
# HTTP server uses — rather than calling the graph directly, because the
# adapter is where caller authentication, the size caps and the caller-contract
# screen live, and a test that skipped it would prove nothing about what a
# caller actually receives.
#
# Driven through the raw ASGI callable with the standard library only: a test
# client would add a dependency and, on the pinned set, a deprecation warning
# the suite would then carry.
#
# The non-finite cases send a RAW body rather than json.dumps output. The
# standard encoder refuses to serialise NaN, while the decoder on the server
# side ACCEPTS the bare NaN and Infinity tokens by default — building the body
# the easy way would silently test nothing.

import asyncio
import json
from typing import Any, Dict, Mapping, Optional, Tuple


def request(
    app: Any,
    method: str,
    path: str,
    body: bytes = b"",
    headers: Optional[Mapping[str, str]] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Send one request through *app*; return (status, decoded JSON body)."""
    raw_headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode(), value.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }
    messages: list = []
    received = {"body": b""}

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)
        if message["type"] == "http.response.body":
            received["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["status"], json.loads(received["body"].decode() or "{}")


def post_raw(app: Any, raw_body: bytes, headers: Optional[Mapping[str, str]] = None) -> Tuple[int, Dict[str, Any]]:
    return request(app, "POST", "/invoke", raw_body, headers)


def post(
    app: Any, payload: Mapping[str, Any], headers: Optional[Mapping[str, str]] = None
) -> Tuple[int, Dict[str, Any]]:
    return post_raw(app, json.dumps(payload, ensure_ascii=False).encode(), headers)


def get_health(app: Any) -> Dict[str, Any]:
    return request(app, "GET", "/health")[1]
