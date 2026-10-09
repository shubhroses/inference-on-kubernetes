"""The real server on a real socket: what SIGTERM does to it."""

import asyncio
import os
import signal
import sys

import httpx
import pytest

from app.api import create_app
from app.config import Settings
from app.server import DrainingServer
from tests.helpers import (
    PATIENCE_SECONDS,
    POLL_SECONDS,
    REPOSITORY,
    Gate,
    Service,
    free_port,
    until,
)

# Longer than any wait in the test below, so that no connection idles out by
# itself while the test is looking at what the drain does to it.
KEEP_ALIVE_SECONDS = 2 * PATIENCE_SECONDS


async def test_a_drain_stops_listening_retires_open_connections_and_finishes_its_requests(
    gate: Gate,
):
    gate.open()  # lets the model load
    lame_duck = Gate()  # the server waits here where it would sleep through the lame duck
    app = create_app(Settings(model_slots=1, queue_size=1), sleep=gate)
    server = DrainingServer(app, "127.0.0.1", 0, KEEP_ALIVE_SECONDS, sleep=lame_duck)
    assert server.config.timeout_keep_alive == KEEP_ALIVE_SECONDS
    serving = asyncio.create_task(server.serve())
    await until(lambda: server.started)
    listener = server.servers[0]
    port = listener.sockets[0].getsockname()[1]

    address = f"http://127.0.0.1:{port}"
    never_expire = httpx.Limits(keepalive_expiry=None)
    async with (
        httpx.AsyncClient(base_url=address, timeout=PATIENCE_SECONDS) as http,
        httpx.AsyncClient(base_url=address, timeout=PATIENCE_SECONDS, limits=never_expire) as other,
    ):
        service = Service(http)
        await service.wait_until_ready()
        gate.close()
        # Two more clients, each with a connection open before the drain begins:
        # one that has been answered and is kept alive, one that is never used.
        assert "connection" not in (await other.get("/healthz")).headers
        unused, unused_writer = await asyncio.open_connection("127.0.0.1", port)
        running = service.infer()
        await service.wait_for(in_flight=1, queued=0)
        waiting = service.infer()
        await service.wait_for(in_flight=1, queued=1)

        # Call the signal handler directly: a real SIGTERM would also reach pytest.
        server.handle_exit(signal.SIGTERM, None)

        # The server stops listening and begins the lame duck.
        await until(lambda: not listener.is_serving())
        assert app.state.draining
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_connection("127.0.0.1", port)
        assert lame_duck.requested == [KEEP_ALIVE_SECONDS]

        # A connection that was open is still served, and told to close. Had the
        # server closed it, the client would have needed a new one and been refused.
        answer = await other.get("/readyz")
        assert (answer.status_code, answer.json()) == (503, {"status": "draining"})
        assert answer.headers["connection"] == "close"
        assert not unused.at_eof()
        assert not running.done()
        assert not waiting.done()

        # The lame duck ends: the connection nobody used is closed, and the
        # requests the server had accepted are still answered.
        lame_duck.open()
        assert await asyncio.wait_for(unused.read(), PATIENCE_SECONDS) == b""
        unused_writer.close()
        await unused_writer.wait_closed()
        assert not running.done()
        assert not waiting.done()

        gate.open()
        responses = await asyncio.gather(running, waiting)

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.headers["connection"] for response in responses] == ["close", "close"]
    await asyncio.wait_for(serving, PATIENCE_SECONDS)


async def test_the_process_answers_its_request_and_exits_with_status_0_on_sigterm():
    port = free_port()
    environment = os.environ | {
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "MODEL_LOAD_SECONDS": "0",
        "MODEL_INFER_SECONDS": "1",
        "KEEP_ALIVE_SECONDS": "1",  # the shortest lame duck there is: this one is slept through
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app", cwd=REPOSITORY, env=environment
    )
    try:
        address = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=address, timeout=PATIENCE_SECONDS) as http:
            service = Service(http)
            await service.wait_until_ready()
            request = service.infer()
            # Send the signal once the request holds a slot. If a very slow machine
            # lets the request finish first, the test still passes; it has then
            # only shown a clean exit, and the test above covers the drain itself.
            async with asyncio.timeout(PATIENCE_SECONDS):
                while not request.done():
                    if (await service.metrics())["inference_requests_in_flight"] == 1:
                        break
                    await asyncio.sleep(POLL_SECONDS)
            process.send_signal(signal.SIGTERM)
            response = await request

        assert response.status_code == 200
        assert await asyncio.wait_for(process.wait(), PATIENCE_SECONDS) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
