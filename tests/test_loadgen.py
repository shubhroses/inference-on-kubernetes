"""The load generator: what it sends, what it does with its connections, what it counts as failed.

The end-to-end tests conclude from its report that no request was lost, so the
report has to be right, and the generator has to be the client it says it is:
one that reuses connections, never sends a request twice, and calls everything
but a 200 a failure. A test that needs failures counts on it to make exactly
the share of the requests fail that it is told to, and the autoscaling test on
it to keep exactly as many requests under way as it has clients.
"""

import asyncio
import contextlib
import json
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest

from app.api import create_app
from app.config import Settings
from app.server import DrainingServer
from loadgen import load as loadgen
from loadgen.load import by_slice, percentile, run
from tests.helpers import PATIENCE_SECONDS, REPOSITORY, Gate, Service, free_port, until

# A run of 40 requests, sent within a fifth of a second: 20 for the 4 clients
# that keep a connection open and 20 that each open a connection of their own.
RATE = 200
SECONDS = 0.2
CONNECTIONS = 4

# What a scripted server sends in answer to a request.
OK = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{}"
OK_AND_CLOSE = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\n{}"
OK_OF_UNKNOWN_LENGTH = b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
UNAVAILABLE = b"HTTP/1.1 503 Service Unavailable\r\ncontent-length: 2\r\n\r\n{}"
NOTHING = b""
HALF_A_HEAD = b"HTTP/1.1 200 OK\r\ncontent-le"
HALF_A_BODY = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{"


@dataclass
class Seen:
    """What a scripted server saw of its clients."""

    connections: int = 0
    requests: int = 0
    asked_to_fail: int = 0  # requests that carried the fault parameter


@asynccontextmanager
async def scripted_server(
    answer: bytes | None, *, then_close: bool = False
) -> AsyncIterator[tuple[str, Seen]]:
    """A server that treats every request the same way, as the test tells it to.

    It sends `answer` and, with `then_close`, closes the connection. With no
    answer at all (None) it leaves the request unanswered and the connection
    open. Yields the URL to send requests to, and what the server has seen.
    """
    seen = Seen()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                await reader.readexactly(int(re.search(rb"Content-Length: (\d+)", head)[1]))
                seen.requests += 1
                seen.asked_to_fail += head.startswith(b"POST /v1/infer?fail=true ")
                if answer is None:
                    await reader.read()  # returns when the client gives up and disconnects
                    break
                writer.write(answer)
                await writer.drain()
                if then_close:
                    break
        except asyncio.IncompleteReadError:
            pass  # the client closed the connection where a request would have begun
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/v1/infer", seen


@asynccontextmanager
async def the_service(*, model: Gate | None = None, **settings: object) -> AsyncIterator[str]:
    """The real service on a socket, with a model that takes no time. Yields its URL.

    With a gate for `model`, every request stays in the model until the test
    opens the gate.
    """
    no_lame_duck = Gate()
    no_lame_duck.open()
    takes_no_time = model or Gate()
    takes_no_time.open()  # for the model's load, at least
    app = create_app(Settings(**settings), sleep=takes_no_time)
    # A keep-alive timeout no test outlasts: the server never closes an idle connection.
    server = DrainingServer(app, "127.0.0.1", 0, 2 * PATIENCE_SECONDS, sleep=no_lame_duck)
    serving = asyncio.create_task(server.serve())
    await until(lambda: server.started)
    address = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    async with httpx.AsyncClient(base_url=address) as http:
        await Service(http).wait_until_ready()
    if model:
        model.close()
    try:
        yield address + "/v1/infer"
    finally:
        server.should_exit = True
        await serving


async def load(url: str, **changes: float | None) -> dict:
    settings = {
        "rate": RATE,
        "clients": None,
        "seconds": SECONDS,
        "connections": CONNECTIONS,
        "max_idle": PATIENCE_SECONDS,
        "request_timeout": PATIENCE_SECONDS,
        "fail_ratio": 0,
    }
    return await run(url, **(settings | changes))


async def test_the_service_answers_every_request_of_a_run():
    async with the_service() as url:
        summary = await load(url)

    assert summary["requests"] == RATE * SECONDS
    assert summary["failures"] == 0
    assert summary["errors"] == {}
    latency = summary["latency_seconds"]
    assert 0 < latency["p50"] <= latency["p95"] <= latency["p99"] <= latency["max"]
    # What the service said of each request's wait for a slot, for the one
    # slice of the run there was.
    (queue_wait,) = summary["queue_wait_seconds"]
    assert queue_wait["from_second"] == 0
    assert 0 <= queue_wait["p50"] <= queue_wait["max"]


async def test_half_the_requests_share_the_kept_connections_and_half_open_their_own():
    async with scripted_server(OK) as (url, seen):
        summary = await load(url)

    assert summary["failures"] == 0
    assert summary["kept_connections_opened"] == CONNECTIONS
    assert seen.requests == 40
    assert seen.connections == CONNECTIONS + 20
    assert seen.asked_to_fail == 0


async def test_a_client_told_to_close_its_connection_opens_another_for_its_next_request():
    async with scripted_server(OK_AND_CLOSE, then_close=True) as (url, seen):
        summary = await load(url)

    assert summary["failures"] == 0
    assert summary["kept_connections_opened"] == 20
    assert seen.connections == 40


async def test_a_connection_that_has_been_idle_for_too_long_is_not_used_again():
    async with scripted_server(OK) as (url, seen):
        summary = await load(url, max_idle=0)

    assert summary["failures"] == 0
    assert summary["kept_connections_opened"] == 20
    assert seen.connections == 40


async def test_a_response_of_unknown_length_counts_but_ends_its_connection():
    async with scripted_server(OK_OF_UNKNOWN_LENGTH) as (url, seen):
        summary = await load(url)

    assert summary["failures"] == 0
    assert summary["kept_connections_opened"] == 20
    assert seen.connections == 40


async def test_a_request_that_is_not_answered_has_failed_and_is_not_sent_again():
    async with scripted_server(NOTHING, then_close=True) as (url, seen):
        summary = await load(url)

    assert summary["requests"] == 40
    assert summary["failures"] == 40
    assert summary["errors"] == {
        "kept connection: ClosedWithoutResponse": 20,
        "new connection: ClosedWithoutResponse": 20,
    }
    assert summary["latency_seconds"]["p50"] is None
    assert seen.requests == 40


@pytest.mark.parametrize("answer", [HALF_A_HEAD, HALF_A_BODY])
async def test_a_response_that_is_cut_short_is_a_failure(answer: bytes):
    async with scripted_server(answer, then_close=True) as (url, seen):
        summary = await load(url)

    assert summary["errors"] == {
        "kept connection: IncompleteReadError": 20,
        "new connection: IncompleteReadError": 20,
    }
    assert seen.requests == 40


async def test_any_status_but_200_is_a_failure():
    async with scripted_server(UNAVAILABLE) as (url, _):
        summary = await load(url)

    assert summary["failures"] == 40
    assert summary["errors"] == {
        "kept connection: status 503": 20,
        "new connection: status 503": 20,
    }
    # A response is a response: the connection that carried it is used again.
    assert summary["kept_connections_opened"] == CONNECTIONS


async def test_a_refused_connection_is_a_failure():
    summary = await load(f"http://127.0.0.1:{free_port()}/v1/infer")

    assert summary["errors"] == {
        "kept connection: ConnectionRefusedError": 20,
        "new connection: ConnectionRefusedError": 20,
    }


async def test_a_request_that_is_not_answered_in_time_is_a_failure():
    async with scripted_server(None) as (url, seen):
        summary = await load(url, rate=100, seconds=0.04, request_timeout=0.05)

    assert summary["errors"] == {
        "kept connection: TimeoutError": 2,
        "new connection: TimeoutError": 2,
    }
    assert seen.requests == 4


@pytest.mark.parametrize(("fail_ratio", "asked_to_fail"), [(0.3, 12), (0.25, 10), (1, 40)])
async def test_the_share_of_requests_it_is_told_to_carries_the_fault_parameter(
    fail_ratio: float, asked_to_fail: int
):
    async with scripted_server(OK) as (url, seen):
        await load(url, fail_ratio=fail_ratio)

    assert seen.requests == 40
    assert seen.asked_to_fail == asked_to_fail


async def test_the_requests_that_ask_to_fail_are_failed_by_a_service_with_fault_injection():
    async with the_service(fault_injection=True) as url:
        summary = await load(url, fail_ratio=0.25)

    assert summary["requests"] == 40
    assert summary["failures"] == 10
    assert set(summary["errors"]) <= {"kept connection: status 500", "new connection: status 500"}


async def test_a_service_without_fault_injection_fails_none_of_them():
    async with the_service() as url:
        summary = await load(url, fail_ratio=0.25)

    assert (summary["requests"], summary["failures"]) == (40, 0)


async def test_clients_that_send_one_request_after_another_each_have_one_under_way():
    clients, slots = 3, 2
    model = Gate()  # closed: every request stays in the model until the test lets it go
    async with (
        the_service(model=model, model_slots=slots) as url,
        httpx.AsyncClient(base_url=url.removesuffix("/v1/infer")) as http,
    ):
        seconds = 0.05
        running = asyncio.create_task(load(url, rate=None, clients=clients, seconds=seconds))
        # One request from each client: two hold the slots and the third waits.
        await Service(http).wait_for(in_flight=slots, queued=clients - slots)
        await asyncio.sleep(seconds)  # the run's time is up, so no client sends another
        model.open()
        summary = await running

    # Had a client not waited for its answer, there would have been a fourth.
    assert (summary["requests"], summary["failures"]) == (clients, 0)
    assert summary["kept_connections_opened"] == 0
    (queue_wait,) = summary["queue_wait_seconds"]
    assert queue_wait["p50"] < queue_wait["max"]  # one of the three had to wait for a slot


async def test_clients_that_send_one_request_after_another_connect_anew_for_each():
    async with scripted_server(OK) as (url, seen):
        summary = await load(url, rate=None, clients=2, seconds=0.05)

    assert summary["failures"] == 0
    assert summary["requests"] == seen.requests >= 2
    assert seen.connections == seen.requests
    assert summary["kept_connections_opened"] == 0


async def test_a_client_whose_request_failed_waits_before_it_sends_the_next(
    monkeypatch: pytest.MonkeyPatch,
):
    # Longer than the run, so that the wait outlasts it.
    monkeypatch.setattr(loadgen, "RETRY_SECONDS", 0.2)
    async with scripted_server(UNAVAILABLE) as (url, seen):
        summary = await load(url, rate=None, clients=2, seconds=0.05)

    assert summary["errors"] == {"new connection: status 503": 2}
    assert seen.requests == 2


def test_queue_waits_are_reported_for_each_half_minute_of_the_run():
    # Each is the second of the run in which a request was sent, and its wait.
    waits = [(0.0, 0.2), (29.9, 0.4), (30.0, 0.1), (95.0, 0.3)]

    assert by_slice(waits) == [
        {"from_second": 0, "p50": 0.2, "p95": 0.4, "p99": 0.4, "max": 0.4},
        {"from_second": 30, "p50": 0.1, "p95": 0.1, "p99": 0.1, "max": 0.1},
        {"from_second": 90, "p50": 0.3, "p95": 0.3, "p99": 0.3, "max": 0.3},
    ]


def test_percentiles_are_values_that_were_measured():
    hundred = [number / 100 for number in range(100, 0, -1)]  # 1.00 down to 0.01

    assert percentile(hundred, 50) == 0.5
    assert percentile(hundred, 95) == 0.95
    assert percentile(hundred, 99) == 0.99
    assert percentile(hundred, 100) == 1.0
    assert percentile([0.25], 50) == 0.25
    assert percentile([], 50) is None


async def command(*arguments: str) -> tuple[int, bytes]:
    """Run `python -m loadgen` with these arguments. Returns its exit status and standard output."""
    process = await asyncio.create_subprocess_exec(
        *[sys.executable, "-m", "loadgen", *arguments],
        cwd=REPOSITORY,
        stdout=asyncio.subprocess.PIPE,
    )
    output, _ = await asyncio.wait_for(process.communicate(), PATIENCE_SECONDS)
    return process.returncode, output


async def test_the_command_prints_the_summary_as_one_line_of_json():
    async with the_service() as url:
        status, output = await command("--url", url, "--rate", "100", "--seconds", "0.2")

    assert status == 0
    summary = json.loads(output)
    assert (summary["requests"], summary["failures"]) == (20, 0)
    assert len(output.splitlines()) == 1


async def test_the_command_takes_a_number_of_clients_in_place_of_a_rate():
    async with the_service() as url:
        status, output = await command("--url", url, "--clients", "2", "--seconds", "0.1")

    assert status == 0
    summary = json.loads(output)
    assert summary["requests"] >= 2
    assert summary["failures"] == 0


@pytest.mark.parametrize(
    "how", [[], ["--rate", "100", "--clients", "2"], ["--clients", "0"]], ids=str
)
async def test_the_command_wants_either_a_rate_or_a_number_of_clients(how: list[str]):
    status, output = await command("--url", "http://127.0.0.1:1/v1/infer", "--seconds", "0.1", *how)

    assert (status, output) == (2, b"")


async def test_the_command_takes_the_share_of_requests_to_fail():
    run = ["--rate", "100", "--seconds", "0.2", "--fail-ratio", "0.5"]
    async with the_service(fault_injection=True) as url:
        status, output = await command("--url", url, *run)

    assert status == 0
    summary = json.loads(output)
    assert (summary["requests"], summary["failures"]) == (20, 10)


async def test_the_command_refuses_a_share_that_is_not_between_0_and_1():
    run = ["--rate", "100", "--seconds", "0.2", "--fail-ratio", "1.5"]
    status, output = await command("--url", "http://127.0.0.1:1/v1/infer", *run)

    # 2 is the status of a command that was given arguments it cannot use.
    assert (status, output) == (2, b"")
