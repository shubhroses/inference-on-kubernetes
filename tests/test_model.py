"""The stand-in model: it spends the configured time and nothing else."""

import asyncio

import pytest

from app.model import ModelError, StandInModel
from tests.helpers import Gate, until


async def test_the_model_is_loaded_only_after_the_load_time(gate: Gate):
    model = StandInModel(load_seconds=2.0, infer_seconds=0.1, sleep=gate)

    loading = asyncio.create_task(model.load())
    await until(lambda: gate.requested == [2.0])
    assert not model.loaded

    gate.open()
    await loading
    assert model.loaded


async def test_infer_takes_the_model_time_and_describes_its_input(gate: Gate):
    gate.open()
    model = StandInModel(load_seconds=2.0, infer_seconds=0.1, sleep=gate)

    result = await model.infer("hello")

    assert result == {"model": "stand-in", "input_chars": 5}
    assert gate.requested == [0.1]


async def test_delay_is_added_to_the_model_time(gate: Gate):
    gate.open()
    model = StandInModel(load_seconds=2.0, infer_seconds=0.1, sleep=gate)

    await model.infer("hello", delay=0.25)

    assert gate.requested == [pytest.approx(0.35)]


async def test_fail_raises_after_the_model_time_has_been_spent(gate: Gate):
    gate.open()
    model = StandInModel(load_seconds=2.0, infer_seconds=0.1, sleep=gate)

    with pytest.raises(ModelError):
        await model.infer("hello", fail=True)

    assert gate.requested == [0.1]


async def test_the_model_sleeps_on_the_real_clock_by_default():
    model = StandInModel(load_seconds=0, infer_seconds=0)

    await model.load()

    assert model.loaded
    assert await model.infer("hello") == {"model": "stand-in", "input_chars": 5}
