"""Admission control: a fixed number of model slots and a bounded queue for them."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class QueueFull(Exception):
    """Every slot is busy and so is every place in the queue."""


class Slots:
    def __init__(self, total: int, queue_size: int) -> None:
        self.total = total
        self.queue_size = queue_size
        self.in_flight = 0  # requests holding a slot
        self.queued = 0  # requests waiting for a slot
        self._semaphore = asyncio.Semaphore(total)

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[float]:
        """Hold one slot for the body of the `async with`.

        Yields the seconds spent waiting for the slot. Raises QueueFull at once,
        without waiting, when the request would be one too many for the queue.
        """
        # locked() means acquire() would have to wait: either no slot is free, or
        # earlier arrivals are still ahead (the semaphore is first in, first out).
        if self._semaphore.locked() and self.queued >= self.queue_size:
            raise QueueFull
        arrived = time.perf_counter()
        # When a slot is free acquire() returns without suspending, so no other
        # request ever sees this one counted as queued.
        self.queued += 1
        try:
            await self._semaphore.acquire()
        finally:
            self.queued -= 1
        self.in_flight += 1
        try:
            yield time.perf_counter() - arrived
        finally:
            self.in_flight -= 1
            self._semaphore.release()
