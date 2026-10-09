"""The real server on a real socket: what SIGTERM does to it."""

import asyncio
import os
import signal
import socket
import sys
from pathlib import Path

import httpx
import pytest

from app.api import create_app
from app.config import Settings
from app.server import DrainingServer
from tests.helpers import PATIENCE_SECONDS, POLL_SECONDS, Gate, Service, until

REPOSITORY = Path(__file__).parent.parent


async def test_a_drain_refuses_new_connections_and_finishes_the_requests_it_has(gate: Gate):
    gate.open()  # lets the model load
    app = create_app(Settings(model_slots=1, queue_size=1), sleep=gate)
    server = DrainingServer(app, host="127.0.0.1", port=0)
    serving = asyncio.create_task(server.serve())
    await until(lambda: server.started)
    listener = server.servers[0]
    port = listener.sockets[0].getsockname()[1]

    address = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=address, timeout=PATIENCE_SECONDS) as http:
        service = Service(http)
        await service.wait_until_ready()
        gate.close()
        running = service.infer()
        await service.wait_for(in_flight=1, queued=0)
        waiting = service.infer()
        await service.wait_for(in_flight=1, queued=1)

        # Call the signal handler directly: a real SIGTERM would also reach pytest.
        server.handle_exit(signal.SIGTERM, None)

        await until(lambda: not listener.is_serving())
        assert app.state.draining
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_connection("127.0.0.1", port)
        assert not running.done()
        assert not waiting.done()

        gate.open()
        responses = await asyncio.gather(running, waiting)

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.headers["connection"] for response in responses] == ["close", "close"]
    await asyncio.wait_for(serving, PATIENCE_SECONDS)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_the_process_answers_its_request_and_exits_with_status_0_on_sigterm():
    port = free_port()
    environment = os.environ | {
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "MODEL_LOAD_SECONDS": "0",
        "MODEL_INFER_SECONDS": "1",
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
