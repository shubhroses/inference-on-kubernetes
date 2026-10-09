"""The stand-in model. It computes nothing: loading takes time, and so does every call."""

import asyncio
from collections.abc import Awaitable, Callable


class ModelError(Exception):
    """The model failed to produce a result."""


class StandInModel:
    def __init__(
        self,
        load_seconds: float,
        infer_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.loaded = False
        self._load_seconds = load_seconds
        self._infer_seconds = infer_seconds
        # The only way time passes here. Tests replace it so they never wait on a clock.
        self._sleep = sleep

    async def load(self) -> None:
        await self._sleep(self._load_seconds)
        self.loaded = True

    async def infer(self, text: str, fail: bool = False, delay: float = 0.0) -> dict[str, object]:
        """Take the usual model time plus `delay`, then return a result or, if `fail`, raise."""
        await self._sleep(self._infer_seconds + delay)
        if fail:
            raise ModelError("the request asked the model to fail")
        return {"model": "stand-in", "input_chars": len(text)}
