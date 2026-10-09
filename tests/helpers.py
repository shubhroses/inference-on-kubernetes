"""Test helpers.

No test waits on a real clock for the model: the model sleeps on a Gate that the
test opens. Where a test has to wait for the service to react, it waits for a
condition, never for a fixed time.
"""

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from prometheus_client.parser import text_string_to_metric_families

from app.api import create_app
from app.config import Settings

# The longest a test waits for something that normally takes milliseconds. It is
# generous on purpose: only a test that is about to fail ever waits this long.
PATIENCE_SECONDS = 30
POLL_SECONDS = 0.005

# Where `python -m app` and `python -m loadgen` are started from.
REPOSITORY = Path(__file__).parent.parent


async def until(condition: Callable[[], bool]) -> None:
    """Let the event loop run until `condition` holds."""
    async with asyncio.timeout(PATIENCE_SECONDS):
        while not condition():
            await asyncio.sleep(POLL_SECONDS)


def free_port() -> int:
    """A port on the loopback interface that nothing is listening on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Gate:
    """Takes the place of asyncio.sleep in the model: sleepers wake when the test opens it."""

    def __init__(self) -> None:
        self.requested: list[float] = []  # every duration the model asked to sleep for
        self._open = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.requested.append(seconds)
        await self._open.wait()

    def open(self) -> None:
        self._open.set()

    def close(self) -> None:
        self._open.clear()


def parse_metrics(text: str) -> dict[str, float]:
    """Turn a /metrics page into {'name{label="value"}': value}, written as in PromQL."""
    samples = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            labels = ",".join(f'{key}="{value}"' for key, value in sorted(sample.labels.items()))
            samples[f"{sample.name}{{{labels}}}" if labels else sample.name] = sample.value
    return samples


class Service:
    """The service as a client sees it: every method here goes through HTTP."""

    def __init__(self, http: httpx.AsyncClient, app: FastAPI | None = None) -> None:
        self.http = http
        self.app = app  # set when the application runs inside the test process

    def infer(self, **faults: object) -> asyncio.Task[httpx.Response]:
        """Send an inference request in the background; await the task for the response."""
        request = self.http.post("/v1/infer", json={"input": "hello"}, params=faults)
        return asyncio.create_task(request)

    async def metrics(self) -> dict[str, float]:
        response = await self.http.get("/metrics")
        return parse_metrics(response.text)

    async def is_ready(self) -> bool:
        try:
            response = await self.http.get("/readyz")
        except httpx.TransportError:  # a server in another process may not be listening yet
            return False
        return response.status_code == 200

    async def wait_until_ready(self) -> None:
        async with asyncio.timeout(PATIENCE_SECONDS):
            while not await self.is_ready():
                await asyncio.sleep(POLL_SECONDS)

    async def occupancy(self) -> tuple[float, float]:
        """The two gauges: requests holding a slot, and requests waiting for one."""
        metrics = await self.metrics()
        return metrics["inference_requests_in_flight"], metrics["inference_requests_queued"]

    async def wait_for(self, *, in_flight: int, queued: int) -> None:
        """Wait until the gauges report this many requests running and waiting."""
        async with asyncio.timeout(PATIENCE_SECONDS):
            while await self.occupancy() != (in_flight, queued):
                await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def in_process(settings: Settings, gate: Gate) -> AsyncIterator[Service]:
    """Run the application inside the test's event loop, with no sockets involved."""
    app = create_app(settings, sleep=gate)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://service") as http,
    ):
        yield Service(http, app)
