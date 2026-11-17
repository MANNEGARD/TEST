"""Tak för förfrågningskroppar som tolkas i minnet (JSON och formulär utan filer).

FastAPI läser och tolkar hela kroppen innan beroendena – och därmed inloggningen – körs. En
JSON-kropp blir ungefär 25 gånger större som Python-objekt, så en enda oinloggad förfrågan på
några tiotal MB skulle kunna ta över en gigabyte minne. Mellanlagret svarar därför 413 innan
kroppen läses när den är större än taket.

Uppladdningar (multipart/form-data: import av gamla körjournaler och loggan) berörs inte: de
skrivs till tillfälliga filer och har kvar sina egna gränser. Webbens och Home Assistants
JSON-anrop är några kB; den största legitima kroppen (exporten med 5 000 id) är cirka 200 kB.
"""

from __future__ import annotations

import json
from typing import Any

MAX_BODY_BYTES = 2 * 1024 * 1024

_DETAIL = "Förfrågan är för stor."
_CODE = "payload_too_large"


class _TooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Rent ASGI-mellanlager (strömmar kroppen vidare, buffrar ingenting)."""

    def __init__(self, app: Any, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers") or []}
        content_type = headers.get(b"content-type", b"").decode("latin-1").strip().lower()
        if content_type.startswith("multipart/form-data"):
            await self.app(scope, receive, send)
            return
        length = headers.get(b"content-length")
        if length is not None:
            try:
                too_large = int(length) > self.max_bytes
            except ValueError:
                too_large = False
            if too_large:
                await _reject(send)
                return
            await self.app(scope, receive, send)
            return

        # Utan Content-Length (chunked): räkna byte medan kroppen läses och avbryt vid taket.
        state = {"received": 0, "exceeded": False, "started": False}

        async def limited_receive() -> dict:
            message = await receive()
            if message.get("type") == "http.request":
                state["received"] += len(message.get("body") or b"")
                if state["received"] > self.max_bytes:
                    state["exceeded"] = True
                    raise _TooLarge()
            return message

        async def guarded_send(message: dict) -> None:
            if state["exceeded"]:
                # Appen gjorde om avbrottet till ett eget felsvar; det ersätts med 413.
                if message.get("type") == "http.response.start" and not state["started"]:
                    state["started"] = True
                    await _reject(send)
                return
            if message.get("type") == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _TooLarge:
            if not state["started"]:
                state["started"] = True
                await _reject(send)


async def _reject(send: Any) -> None:
    body = json.dumps({"detail": _DETAIL, "code": _CODE}, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"connection", b"close"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


__all__ = ["MAX_BODY_BYTES", "BodyLimitMiddleware"]
