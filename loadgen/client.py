"""One HTTP/1.1 connection, written on asyncio streams.

It is hand-written, and not a library, for two reasons. The load generator
runs in the service's own image, so it has the standard library and nothing
else. And what a client does with its connections is the subject of the
rollout tests, so it is spelled out here: nothing reconnects or retries behind
this code's back.

It speaks just enough HTTP for this service: one request at a time, and a body
with a Content-Length in each direction.
"""

import asyncio
import contextlib
import time


class ClosedWithoutResponse(ConnectionError):
    """The server closed the connection without answering the request sent on it."""


class Connection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._last_used = time.monotonic()

    @classmethod
    async def open(cls, address: str, port: int) -> "Connection":
        return cls(*await asyncio.open_connection(address, port))

    def idle_seconds(self) -> float:
        """The time since this connection was opened or last carried a response."""
        return time.monotonic() - self._last_used

    async def post(
        self, host: str, path: str, body: bytes, *, last: bool = False
    ) -> tuple[int, bool]:
        """Send one request and read the response to it.

        Returns the status, and whether the connection may carry another
        request. With `last`, the request tells the server that it is the only
        one this connection will carry.
        """
        lines = [
            f"POST {path} HTTP/1.1",
            f"Host: {host}",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
        ]
        if last:
            lines.append("Connection: close")
        self._writer.write("\r\n".join(lines).encode("ascii") + b"\r\n\r\n" + body)
        await self._writer.drain()

        try:
            head = await self._reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError as error:
            if error.partial:
                raise  # the connection ended in the middle of the response
            raise ClosedWithoutResponse from None
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        status = int(status_line.split()[1])
        headers = {}
        for line in filter(None, header_lines):
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip().lower()

        if "content-length" not in headers:
            # A body of unknown length. The status is all the caller wants, so
            # the body is left unread, and the connection cannot be used again.
            return status, False
        await self._reader.readexactly(int(headers["content-length"]))
        self._last_used = time.monotonic()
        told_to_close = "close" in headers.get("connection", "").replace(" ", "").split(",")
        return status, not (last or told_to_close)

    async def close(self) -> None:
        self._writer.close()
        # A connection the other side has reset reports that once more here.
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()
