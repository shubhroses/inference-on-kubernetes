"""Sends requests at a fixed rate and counts what comes back.

The client model, which is what a run of this stands for:

Requests are due at fixed times, `rate` of them a second, whether or not the
earlier ones have been answered, and a request's latency counts from the
moment it was due. They alternate between two kinds of client.

- Clients that keep a connection open. There are `connections` of them and
  they take requests in turn. Each uses one HTTP/1.1 connection for as long as
  the server lets it: when a response says `Connection: close`, the client
  closes the connection and opens a new one for its next request. It does not
  use a connection that has been idle for longer than `max_idle` seconds,
  which has to be shorter than the server's keep-alive timeout.
- Clients that connect, send one request and disconnect.

No request is ever sent twice, and no client checks whether a connection is
still open before using it. A request fails if it gets anything but a 200:
its connection is refused, reset or closed without an answer, the answer does
not come within `request_timeout` seconds, or it has another status.

With `fail_ratio`, that share of the requests, spread evenly over the run,
asks the service to fail (`?fail=true`). The service does so, with a 500, only
if its fault injection is switched on; such a request then counts as failed
like any other.

The name in the URL is resolved once, when the run begins. The load is about
the pods behind the Service's address, not about the cluster's DNS.
"""

import asyncio
import logging
import math
import socket
from collections import Counter
from urllib.parse import urlsplit

from loadgen.client import Connection

logger = logging.getLogger("loadgen")

BODY = b'{"input": "load"}'
PROGRESS_SECONDS = 5


class Load:
    """Where the requests of one run go, and what has come back so far."""

    def __init__(
        self,
        host: str,
        address: str,
        port: int,
        path: str,
        max_idle: float,
        request_timeout: float,
        fail_ratio: float,
    ) -> None:
        self.host = host  # as written in the URL: sent in the Host header
        self.address = address  # the IP address that name resolved to
        self.port = port
        self.path = path
        self.max_idle = max_idle
        self.request_timeout = request_timeout
        self.fail_ratio = fail_ratio
        self.begun = 0  # requests begun so far, answered or not
        self.latencies: list[float] = []  # seconds, one for each request answered with 200
        self.errors: Counter[str] = Counter()  # what went wrong, and how often
        self.opened = 0  # connections opened by the clients that keep theirs

    async def request(
        self, due: float, connection: Connection | None, *, keep: bool
    ) -> Connection | None:
        """Send the request that was due at `due`, on `connection` or else on a new one.

        Returns the connection if it can carry another request. A client that
        keeps its connection (`keep`) passes that in the next time.
        """
        client = "kept connection" if keep else "new connection"
        loop = asyncio.get_running_loop()
        # The fault parameter goes on each request with which the count of
        # requests times `fail_ratio` reaches the next whole number: with 0.3,
        # on the 4th, the 7th and the 10th of every ten.
        self.begun += 1
        asked_to_fail = int(self.begun * self.fail_ratio) > int((self.begun - 1) * self.fail_ratio)
        path = self.path + "?fail=true" if asked_to_fail else self.path
        reusable = False
        try:
            async with asyncio.timeout(self.request_timeout):
                if connection is None:
                    connection = await Connection.open(self.address, self.port)
                    if keep:
                        self.opened += 1
                status, reusable = await connection.post(self.host, path, BODY, last=not keep)
        except (OSError, EOFError, ValueError) as error:
            self.fail(f"{client}: {type(error).__name__}")
        else:
            if status == 200:
                self.latencies.append(loop.time() - due)
            else:
                self.fail(f"{client}: status {status}")
        if connection is not None and not reusable:
            await connection.close()
            connection = None
        return connection

    def fail(self, error: str) -> None:
        self.errors[error] += 1
        logger.warning("a request failed on a %s", error)

    async def keeping_client(self, requests: asyncio.Queue[float | None]) -> None:
        """A client that keeps its connection open from one request to the next."""
        connection = None
        while (due := await requests.get()) is not None:
            if connection is not None and connection.idle_seconds() > self.max_idle:
                await connection.close()
                connection = None
            connection = await self.request(due, connection, keep=True)
        if connection is not None:
            await connection.close()

    def summary(self) -> dict[str, object]:
        failures = sum(self.errors.values())
        return {
            "requests": len(self.latencies) + failures,
            "failures": failures,
            "errors": dict(self.errors),
            "latency_seconds": {
                "p50": percentile(self.latencies, 50),
                "p95": percentile(self.latencies, 95),
                "p99": percentile(self.latencies, 99),
                "max": percentile(self.latencies, 100),
            },
            "kept_connections_opened": self.opened,
        }


def percentile(values: list[float], percent: float) -> float | None:
    """The smallest of the values that `percent` per cent of them do not exceed."""
    if not values:
        return None
    rank = math.ceil(len(values) * percent / 100)
    return round(sorted(values)[max(rank, 1) - 1], 4)


async def run(
    url: str,
    rate: float,
    seconds: float,
    connections: int,
    max_idle: float,
    request_timeout: float,
    fail_ratio: float,
) -> dict[str, object]:
    """Send `rate` requests a second to `url` for `seconds` seconds. Returns the summary."""
    loop = asyncio.get_running_loop()
    parts = urlsplit(url)
    port = parts.port or 80
    resolved = await loop.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    address = resolved[0][4][0]
    path = parts.path or "/"
    load = Load(parts.netloc, address, port, path, max_idle, request_timeout, fail_ratio)
    total = round(rate * seconds)
    progress_every = max(1, round(rate * PROGRESS_SECONDS))
    logger.info("sending %d requests to %s at %s, %g a second", total, url, address, rate)

    requests: asyncio.Queue[float | None] = asyncio.Queue()
    started = loop.time()
    async with asyncio.TaskGroup() as clients:
        for _ in range(connections):
            clients.create_task(load.keeping_client(requests))
        for number in range(total):
            due = started + number / rate
            await asyncio.sleep(due - loop.time())
            if number % 2 == 0:
                requests.put_nowait(due)
            else:
                clients.create_task(load.request(due, None, keep=False))
            if (number + 1) % progress_every == 0:
                logger.info("%d requests sent, %d failed", number + 1, sum(load.errors.values()))
        for _ in range(connections):
            requests.put_nowait(None)  # tells one client that there is no more to do
    return load.summary()
