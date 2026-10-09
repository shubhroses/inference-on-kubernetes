"""Slot and queue accounting, on the Slots class alone."""

import asyncio

import pytest

from app.slots import QueueFull, Slots
from tests.helpers import until


class Request:
    """Takes a slot and keeps it until the test lets it finish."""

    def __init__(self, slots: Slots) -> None:
        self.waited: float | None = None  # seconds queued; None until the slot is held
        self.may_finish = asyncio.Event()
        self.task = asyncio.create_task(self._run(slots))

    async def _run(self, slots: Slots) -> None:
        async with slots.hold() as waited:
            self.waited = waited
            await self.may_finish.wait()

    @property
    def holds_a_slot(self) -> bool:
        return self.waited is not None and not self.task.done()

    async def finish(self) -> None:
        self.may_finish.set()
        await self.task


async def test_a_free_slot_is_taken_without_queueing():
    slots = Slots(total=2, queue_size=2)

    request = Request(slots)
    await until(lambda: request.holds_a_slot)

    assert (slots.in_flight, slots.queued) == (1, 0)
    await request.finish()
    assert (slots.in_flight, slots.queued) == (0, 0)


async def test_requests_wait_in_the_queue_while_every_slot_is_busy():
    slots = Slots(total=2, queue_size=2)
    first, second = Request(slots), Request(slots)
    await until(lambda: slots.in_flight == 2)

    third, fourth = Request(slots), Request(slots)
    await until(lambda: slots.queued == 2)

    assert slots.in_flight == 2
    assert not third.holds_a_slot
    assert not fourth.holds_a_slot

    await first.finish()
    await until(lambda: slots.queued == 1)
    assert slots.in_flight == 2

    for request in (second, third, fourth):
        await request.finish()
    assert (slots.in_flight, slots.queued) == (0, 0)


async def test_a_request_is_shed_when_the_queue_is_full():
    slots = Slots(total=1, queue_size=1)
    running = Request(slots)
    await until(lambda: slots.in_flight == 1)
    waiting = Request(slots)
    await until(lambda: slots.queued == 1)

    with pytest.raises(QueueFull):
        async with slots.hold():
            pass

    assert (slots.in_flight, slots.queued) == (1, 1)
    await running.finish()
    await waiting.finish()


async def test_requests_are_admitted_again_once_the_queue_has_room():
    slots = Slots(total=1, queue_size=1)
    running = Request(slots)
    await until(lambda: slots.in_flight == 1)
    waiting = Request(slots)
    await until(lambda: slots.queued == 1)
    with pytest.raises(QueueFull):
        async with slots.hold():
            pass

    await running.finish()
    await until(lambda: waiting.holds_a_slot)
    late = Request(slots)
    await until(lambda: slots.queued == 1)

    await waiting.finish()
    await late.finish()
    assert (slots.in_flight, slots.queued) == (0, 0)


async def test_a_queue_of_zero_sheds_as_soon_as_the_slots_are_busy():
    slots = Slots(total=1, queue_size=0)
    running = Request(slots)
    await until(lambda: slots.in_flight == 1)

    with pytest.raises(QueueFull):
        async with slots.hold():
            pass

    await running.finish()


async def test_waiting_requests_get_slots_in_arrival_order():
    slots = Slots(total=1, queue_size=3)
    requests = [Request(slots)]
    await until(lambda: slots.in_flight == 1)
    for place in (1, 2, 3):
        requests.append(Request(slots))
        await until(lambda place=place: slots.queued == place)

    for position, request in enumerate(requests):
        await until(lambda request=request: request.holds_a_slot)
        assert not any(later.holds_a_slot for later in requests[position + 1 :])
        await request.finish()


async def test_a_newcomer_does_not_overtake_a_waiting_request():
    slots = Slots(total=1, queue_size=2)
    running = Request(slots)
    await until(lambda: slots.in_flight == 1)
    waiting = Request(slots)
    await until(lambda: slots.queued == 1)

    # The newcomer arrives in the same turn of the event loop in which the slot is
    # released: after the slot has been handed on, before the waiter has resumed.
    running.may_finish.set()
    newcomer = Request(slots)
    await running.task

    await until(lambda: waiting.holds_a_slot)
    assert not newcomer.holds_a_slot
    assert (slots.in_flight, slots.queued) == (1, 1)
    await waiting.finish()
    await newcomer.finish()


async def test_the_time_spent_queued_is_measured():
    slots = Slots(total=1, queue_size=1)
    running = Request(slots)
    await until(lambda: slots.in_flight == 1)
    waiting = Request(slots)
    await until(lambda: slots.queued == 1)

    await asyncio.sleep(0.05)
    await running.finish()
    await until(lambda: waiting.holds_a_slot)

    assert running.waited is not None
    assert waiting.waited is not None
    assert running.waited >= 0
    assert waiting.waited >= 0.04  # a lower bound only: a slow machine may add to it
    await waiting.finish()


async def test_a_cancelled_waiter_gives_up_its_place_and_no_slot_is_lost():
    slots = Slots(total=1, queue_size=1)
    running = Request(slots)
    await until(lambda: slots.in_flight == 1)
    impatient = Request(slots)
    await until(lambda: slots.queued == 1)

    impatient.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await impatient.task
    assert (slots.in_flight, slots.queued) == (1, 0)

    patient = Request(slots)
    await until(lambda: slots.queued == 1)
    await running.finish()
    await until(lambda: patient.holds_a_slot)
    await patient.finish()
    assert (slots.in_flight, slots.queued) == (0, 0)


async def test_a_request_that_fails_gives_its_slot_back():
    slots = Slots(total=1, queue_size=0)

    with pytest.raises(RuntimeError, match="model crashed"):
        async with slots.hold():
            raise RuntimeError("model crashed")

    assert slots.in_flight == 0
    async with slots.hold():
        assert slots.in_flight == 1
