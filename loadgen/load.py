"""Puts load on the service and counts what comes back.

The client model, which is what a run of this stands for. There are two kinds
of client, and two ways of deciding when a request is sent.

- Clients that keep a connection open. Each uses one HTTP/1.1 connection for
  as long as the server lets it: when a response says `Connection: close`, the
  client closes the connection and opens a new one for its next request. It
  does not use a connection that has been idle for longer than `max_idle`
  seconds, which has to be shorter than the server's keep-alive timeout.
- Clients that connect, send one request and disconnect.

At a fixed rate (`rate`): requests are due at fixed times, that many a second,
whether or not the earlier ones have been answered, and a request's latency
counts from the moment it was due. They alternate between the two kinds of
client: `connections` clients that keep a connection take every other request
in turn, and each of the others is sent by a client of the second kind.

From a fixed number of clients (`clients`): each sends a request, waits for
the answer and sends its next one, so there are never more requests under way
than there are clients, and how many get sent depends on how fast the service
answers. These clients are of the second kind. Every request is then routed
afresh, and a pod that has just been added gets its share at once; clients
that kept their connections would stay with the pods they first reached. The
clients begin one after another, over the first second, so that they do not
move in step: clients that know nothing of each other do not all send at the
same instant. A client whose request has failed waits a second before its
next one, which is what the service asks for when it sheds a request
(`Retry-After: 1`).

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
import json
import logging
import math
import socket
from collections import Counter
from urllib.parse import urlsplit

from loadgen.client import Connection

logger = logging.getLogger("loadgen")

BODY = b'{"input": "load"}'
PROGRESS_SECONDS = 5
# How long a client that sends one request after another waits after a failure.
RETRY_SECONDS = 1
# The summary says how long requests waited for a slot in each stretch of the
# run that is this long, so that the beginning of a run can be told from its end.
SLICE_SECONDS = 30


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
        self.started = asyncio.get_running_loop().time()
        self.begun = 0  # requests begun so far, answered or not
        self.latencies: list[float] = []  # seconds, one for each request answered with 200
        # For each of those whose answer says how long the request waited for a
        # slot: the second of the run in which it was sent, and that wait.
        self.queue_waits: list[tuple[float, float]] = []
        self.errors: Counter[str] = Counter()  # what went wrong, and how often
        self.opened = 0  # connections opened by the clients that keep theirs

    async def request(
        self, due: float, connection: Connection | None, *, keep: bool
    ) -> tuple[Connection | None, bool]:
        """Send the request that was due at `due`, on `connection` or else on a new one.

        Returns the connection, if it can carry another request, and whether
        the request was answered with a 200. A client that keeps its
        connection (`keep`) passes the connection in again the next time.
        """
        client = "kept connection" if keep else "new connection"
        loop = asyncio.get_running_loop()
        # The fault parameter goes on each request with which the count of
        # requests times `fail_ratio` reaches the next whole number: with 0.3,
        # on the 4th, the 7th and the 10th of every ten.
        self.begun += 1
        asked_to_fail = int(self.begun * self.fail_ratio) > int((self.begun - 1) * self.fail_ratio)
        path = self.path + "?fail=true" if asked_to_fail else self.path
        status, reusable = None, False
        try:
            async with asyncio.timeout(self.request_timeout):
                if connection is None:
                    connection = await Connection.open(self.address, self.port)
                    if keep:
                        self.opened += 1
                status, body, reusable = await connection.post(self.host, path, BODY, last=not keep)
        except (OSError, EOFError, ValueError) as error:
            self.fail(f"{client}: {type(error).__name__}")
        else:
            if status == 200:
                self.latencies.append(loop.time() - due)
                # The service puts the time the request waited for a slot in its answer.
                waited = json.loads(body or b"{}").get("queue_wait_seconds")
                if waited is not None:
                    self.queue_waits.append((due - self.started, waited))
            else:
                self.fail(f"{client}: status {status}")
        if connection is not None and not reusable:
            await connection.close()
            connection = None
        return connection, status == 200

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
            connection, _ = await self.request(due, connection, keep=True)
        if connection is not None:
            await connection.close()

    async def at_a_rate(self, rate: float, seconds: float, connections: int) -> None:
        """Send `rate` requests a second for `seconds` seconds, the two kinds of client in turn."""
        loop = asyncio.get_running_loop()
        requests: asyncio.Queue[float | None] = asyncio.Queue()
        async with asyncio.TaskGroup() as clients:
            for _ in range(connections):
                clients.create_task(self.keeping_client(requests))
            for number in range(round(rate * seconds)):
                due = self.started + number / rate
                await asyncio.sleep(due - loop.time())
                if number % 2 == 0:
                    requests.put_nowait(due)
                else:
                    clients.create_task(self.request(due, None, keep=False))
            for _ in range(connections):
                requests.put_nowait(None)  # tells one client that there is no more to do

    async def from_clients(self, clients: int, seconds: float) -> None:
        """Have `clients` clients each send one request after another for `seconds` seconds."""
        # They begin at even intervals over the first second, or over the
        # whole run if it is shorter than that.
        apart = min(1.0, seconds) / clients
        async with asyncio.TaskGroup() as sending:
            for number in range(clients):
                begins = self.started + number * apart
                sending.create_task(self.one_after_another(begins, self.started + seconds))

    async def one_after_another(self, begins: float, until: float) -> None:
        """A client that sends its next request when the last one has been answered."""
        loop = asyncio.get_running_loop()
        await asyncio.sleep(begins - loop.time())
        while (now := loop.time()) < until:
            _, answered = await self.request(now, None, keep=False)
            if not answered:
                await asyncio.sleep(RETRY_SECONDS)

    async def report_progress(self) -> None:
        """Say every few seconds how far the run has got. It goes on until it is cancelled."""
        while True:
            await asyncio.sleep(PROGRESS_SECONDS)
            logger.info("%d requests sent, %d failed", self.begun, sum(self.errors.values()))

    def summary(self) -> dict[str, object]:
        failures = sum(self.errors.values())
        return {
            "requests": len(self.latencies) + failures,
            "failures": failures,
            "errors": dict(self.errors),
            "latency_seconds": percentiles(self.latencies),
            "queue_wait_seconds": by_slice(self.queue_waits),
            "kept_connections_opened": self.opened,
        }


def by_slice(queue_waits: list[tuple[float, float]]) -> list[dict[str, float | None]]:
    """The percentiles of the queue waits, for each slice of the run in which a request was sent."""
    slices: dict[int, list[float]] = {}
    for sent, waited in queue_waits:
        slices.setdefault(int(sent // SLICE_SECONDS), []).append(waited)
    return [
        {"from_second": number * SLICE_SECONDS, **percentiles(waits)}
        for number, waits in sorted(slices.items())
    ]


def percentiles(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": percentile(values, 100),
    }


def percentile(values: list[float], percent: float) -> float | None:
    """The smallest of the values that `percent` per cent of them do not exceed."""
    if not values:
        return None
    rank = math.ceil(len(values) * percent / 100)
    return round(sorted(values)[max(rank, 1) - 1], 4)


async def run(
    url: str,
    seconds: float,
    rate: float | None,
    clients: int | None,
    connections: int,
    max_idle: float,
    request_timeout: float,
    fail_ratio: float,
) -> dict[str, object]:
    """Put load on `url` for `seconds` seconds. Returns the summary.

    Either `rate` requests a second, or `clients` clients that each send one
    request after another.
    """
    loop = asyncio.get_running_loop()
    parts = urlsplit(url)
    port = parts.port or 80
    resolved = await loop.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    address = resolved[0][4][0]
    path = parts.path or "/"
    load = Load(parts.netloc, address, port, path, max_idle, request_timeout, fail_ratio)
    async with asyncio.TaskGroup() as tasks:
        reporting = tasks.create_task(load.report_progress())
        if clients is None:
            logger.info("sending %g requests a second to %s at %s", rate, url, address)
            await load.at_a_rate(rate, seconds, connections)
        else:
            logger.info("%d clients sending to %s at %s", clients, url, address)
            await load.from_clients(clients, seconds)
        reporting.cancel()
    return load.summary()
