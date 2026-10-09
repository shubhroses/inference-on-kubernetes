"""Test helpers.

No test waits on a real clock for the model: the model sleeps on a Gate that the
test opens. Where a test has to wait for the service to react, it waits for a
condition, never for a fixed time.
"""

import asyncio
from collections.abc import Callable

# The longest a test waits for something that normally takes milliseconds. It is
# generous on purpose: only a test that is about to fail ever waits this long.
PATIENCE_SECONDS = 30
POLL_SECONDS = 0.005


async def until(condition: Callable[[], bool]) -> None:
    """Let the event loop run until `condition` holds."""
    async with asyncio.timeout(PATIENCE_SECONDS):
        while not condition():
            await asyncio.sleep(POLL_SECONDS)


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
