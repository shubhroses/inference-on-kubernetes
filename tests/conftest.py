from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack

import pytest

from app.config import Settings
from tests.helpers import Gate, Service, in_process


@pytest.fixture
def gate() -> Gate:
    """The gate the model under test sleeps on. Closed until the test opens it."""
    return Gate()


@pytest.fixture
async def start(gate: Gate) -> AsyncIterator[Callable[..., Awaitable[Service]]]:
    """A function that starts a service with the given settings, stopped after the test.

    When it returns, the model has loaded and the gate is closed again.
    """
    async with AsyncExitStack() as running:

        async def start_service(**settings: object) -> Service:
            gate.open()
            service = await running.enter_async_context(in_process(Settings(**settings), gate))
            await service.wait_until_ready()
            gate.close()
            gate.requested.clear()  # forget the load: tests look at what requests asked for
            return service

        yield start_service


@pytest.fixture
async def service(start: Callable[..., Awaitable[Service]]) -> Service:
    """The usual service under test: two slots and a queue of two."""
    return await start(model_slots=2, queue_size=2)
