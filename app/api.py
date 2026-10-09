"""The HTTP API: POST /v1/infer, the two probes and /metrics."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from app.config import Settings
from app.metrics import Metrics
from app.model import ModelError, StandInModel
from app.slots import QueueFull, Slots

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 10_000
MAX_DELAY_SECONDS = 60.0


class InferRequest(BaseModel):
    input: str = Field(max_length=MAX_INPUT_CHARS)


class InferResponse(BaseModel):
    model: str
    input_chars: int
    queue_wait_seconds: float


def create_app(
    settings: Settings, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> FastAPI:
    """Build the application. Tests pass their own `sleep` to decide when time passes."""
    model = StandInModel(settings.model_load_seconds, settings.model_infer_seconds, sleep)
    slots = Slots(settings.model_slots, settings.queue_size)
    metrics = Metrics(slots, settings.git_sha)

    async def load_model() -> None:
        await model.load()
        logger.info("model loaded, ready for requests")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Load in the background: the server has to answer its probes meanwhile.
        loading = asyncio.create_task(load_model())
        yield
        loading.cancel()

    app = FastAPI(title="inference-on-kubernetes", lifespan=lifespan)
    # Becomes true when the server is told to stop; see app/server.py.
    app.state.draining = False

    if settings.fault_injection:
        logger.warning("fault injection is on: requests can ask to fail or to be delayed")

    @app.middleware("http")
    async def close_connections_while_draining(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if app.state.draining:
            # Tells a keep-alive client to send its next request on a new
            # connection, and makes uvicorn close this one after the response.
            response.headers["Connection"] = "close"
        return response

    @app.post("/v1/infer")
    async def infer(
        body: InferRequest,
        fail: bool = False,
        delay: Annotated[float, Query(ge=0, le=MAX_DELAY_SECONDS)] = 0.0,
    ) -> InferResponse:
        """Run the stand-in model on `input`.

        `fail` and `delay` are faults for tests. They are ignored unless the
        service was started with FAULT_INJECTION=true.
        """
        if not settings.fault_injection:
            fail, delay = False, 0.0
        started = time.perf_counter()
        outcome = "error"  # unless the request is answered or shed below
        try:
            if not model.loaded:
                raise HTTPException(status_code=503, detail="the model is still loading")
            async with slots.hold() as queue_wait:
                metrics.queue_wait.observe(queue_wait)
                result = await model.infer(body.input, fail=fail, delay=delay)
            outcome = "ok"
            return InferResponse(**result, queue_wait_seconds=queue_wait)
        except QueueFull:
            outcome = "rejected"
            raise HTTPException(
                status_code=429,
                detail="every model slot is busy and the queue is full",
                headers={"Retry-After": "1"},
            ) from None
        except ModelError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        finally:
            metrics.requests.labels(outcome).inc()
            metrics.request_duration.labels(outcome).observe(time.perf_counter() - started)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up and its event loop is turning.

        It says nothing about loading or draining on purpose, so that a liveness
        probe never restarts a pod for being busy starting or stopping.
        """
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        """Readiness: 200 once the model has loaded and until the drain begins."""
        if app.state.draining:
            status = "draining"
        elif not model.loaded:
            status = "loading"
        else:
            return {"status": "ready"}
        response.status_code = 503
        return {"status": status}

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return app
