"""The HTTP API: admission, shedding, readiness, drain, fault parameters and metric values.

The application runs inside the test's event loop, so several requests can be
in the service at once and the test decides when the model lets them go.
"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from app.config import Settings
from tests.helpers import Gate, Service, in_process, parse_metrics

Start = Callable[..., Awaitable[Service]]

OK = 'inference_requests_total{outcome="ok"}'
ERROR = 'inference_requests_total{outcome="error"}'
REJECTED = 'inference_requests_total{outcome="rejected"}'


# Admission


async def test_infer_returns_the_stand_in_result(service: Service, gate: Gate):
    gate.open()

    response = await service.infer()

    assert response.status_code == 200
    assert response.json()["model"] == "stand-in"
    assert response.json()["input_chars"] == len("hello")
    assert response.json()["queue_wait_seconds"] >= 0
    assert gate.requested == [Settings.model_infer_seconds]


async def test_requests_beyond_the_slots_wait_in_the_queue(service: Service, gate: Gate):
    running = [service.infer(), service.infer()]
    await service.wait_for(in_flight=2, queued=0)

    waiting = [service.infer(), service.infer()]
    await service.wait_for(in_flight=2, queued=2)
    assert not any(request.done() for request in running + waiting)

    gate.open()
    responses = await asyncio.gather(*running, *waiting)
    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    await service.wait_for(in_flight=0, queued=0)


async def test_requests_beyond_the_queue_are_shed_with_429(service: Service, gate: Gate):
    admitted = [service.infer() for _ in range(4)]
    await service.wait_for(in_flight=2, queued=2)

    shed = await service.infer()

    assert shed.status_code == 429
    assert shed.headers["retry-after"] == "1"
    assert (await service.metrics())[REJECTED] == 1
    assert await service.occupancy() == (2, 2)

    gate.open()
    responses = await asyncio.gather(*admitted)
    assert [response.status_code for response in responses] == [200, 200, 200, 200]


async def test_requests_are_admitted_again_after_shedding(service: Service, gate: Gate):
    admitted = [service.infer() for _ in range(4)]
    await service.wait_for(in_flight=2, queued=2)
    assert (await service.infer()).status_code == 429

    gate.open()
    await asyncio.gather(*admitted)

    assert (await service.infer()).status_code == 200


async def test_the_probes_still_answer_when_the_service_is_saturated(service: Service, gate: Gate):
    admitted = [service.infer() for _ in range(4)]
    await service.wait_for(in_flight=2, queued=2)

    # A full service sheds load with 429s; it does not stop being alive or ready.
    assert (await service.http.get("/healthz")).status_code == 200
    assert (await service.http.get("/readyz")).status_code == 200

    gate.open()
    await asyncio.gather(*admitted)


# Readiness and drain


async def test_the_service_is_not_ready_until_the_model_has_loaded(gate: Gate):
    async with in_process(Settings(), gate) as service:
        loading = await service.http.get("/readyz")
        assert (loading.status_code, loading.json()) == (503, {"status": "loading"})
        assert (await service.http.get("/healthz")).status_code == 200

        gate.open()
        await service.wait_until_ready()

        ready = await service.http.get("/readyz")
        assert (ready.status_code, ready.json()) == (200, {"status": "ready"})
        assert gate.requested == [Settings.model_load_seconds]


async def test_inference_is_refused_with_503_while_the_model_loads(gate: Gate):
    async with in_process(Settings(), gate) as service:
        response = await service.infer()

        assert response.status_code == 503
        metrics = await service.metrics()
        assert metrics[ERROR] == 1
        assert metrics["inference_queue_wait_seconds_count"] == 0  # it never asked for a slot


async def test_the_service_is_not_ready_while_draining(service: Service):
    service.app.state.draining = True  # what the server does when SIGTERM arrives

    response = await service.http.get("/readyz")

    assert (response.status_code, response.json()) == (503, {"status": "draining"})
    assert (await service.http.get("/healthz")).status_code == 200


async def test_requests_already_in_the_service_are_answered_while_draining(
    service: Service, gate: Gate
):
    running = [service.infer(), service.infer()]
    await service.wait_for(in_flight=2, queued=0)
    waiting = service.infer()
    await service.wait_for(in_flight=2, queued=1)

    service.app.state.draining = True
    gate.open()
    responses = await asyncio.gather(*running, waiting)

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert [response.headers["connection"] for response in responses] == ["close"] * 3


async def test_connections_are_kept_open_when_not_draining(service: Service, gate: Gate):
    gate.open()

    response = await service.infer()

    assert "connection" not in response.headers


# Fault parameters


async def test_fault_parameters_do_nothing_by_default(service: Service, gate: Gate):
    gate.open()

    response = await service.infer(fail="true", delay=5)

    assert response.status_code == 200
    assert gate.requested == [Settings.model_infer_seconds]
    assert (await service.metrics())[ERROR] == 0


async def test_fail_returns_500_after_holding_a_slot_when_faults_are_enabled(
    start: Start, gate: Gate
):
    service = await start(model_slots=1, queue_size=0, fault_injection=True)

    request = service.infer(fail="true")
    await service.wait_for(in_flight=1, queued=0)
    gate.open()
    response = await request

    assert response.status_code == 500
    metrics = await service.metrics()
    assert (metrics[OK], metrics[ERROR]) == (0, 1)
    assert metrics["inference_requests_in_flight"] == 0


async def test_delay_is_added_to_the_model_time_when_faults_are_enabled(start: Start, gate: Gate):
    service = await start(fault_injection=True)
    gate.open()

    response = await service.infer(delay=0.25)

    assert response.status_code == 200
    assert gate.requested == [pytest.approx(Settings.model_infer_seconds + 0.25)]


@pytest.mark.parametrize(
    ("body", "query"),
    [
        ({}, {}),
        ({"input": 5}, {}),
        ({"input": "x" * 10_001}, {}),
        ({"input": "hello"}, {"delay": -1}),
        ({"input": "hello"}, {"delay": 61}),
        ({"input": "hello"}, {"fail": "maybe"}),
    ],
)
async def test_invalid_requests_get_422_and_are_not_counted(
    service: Service, body: dict[str, object], query: dict[str, object]
):
    response = await service.http.post("/v1/infer", json=body, params=query)

    assert response.status_code == 422
    metrics = await service.metrics()
    assert (metrics[OK], metrics[ERROR], metrics[REJECTED]) == (0, 0, 0)


# Metric values


async def test_every_series_is_exported_before_the_first_request(start: Start):
    service = await start(model_slots=3, git_sha="0123abc")

    response = await service.http.get("/metrics")

    assert response.headers["content-type"].startswith("text/plain")
    metrics = parse_metrics(response.text)
    for outcome in ("ok", "error", "rejected"):
        assert metrics[f'inference_requests_total{{outcome="{outcome}"}}'] == 0
        assert metrics[f'inference_request_duration_seconds_count{{outcome="{outcome}"}}'] == 0
    assert metrics["inference_queue_wait_seconds_count"] == 0
    assert metrics["inference_requests_in_flight"] == 0
    assert metrics["inference_requests_queued"] == 0
    assert metrics["inference_model_slots"] == 3
    assert metrics['inference_build_info{git_sha="0123abc"}'] == 1
    assert not any(name.split("{")[0].endswith("_created") for name in metrics)


async def test_requests_are_counted_by_outcome(start: Start, gate: Gate):
    service = await start(model_slots=1, queue_size=0, fault_injection=True)

    held = service.infer()
    await service.wait_for(in_flight=1, queued=0)
    rejected = await service.infer()
    gate.open()
    ok = await held
    failed = await service.infer(fail="true")

    assert [ok.status_code, rejected.status_code, failed.status_code] == [200, 429, 500]
    metrics = await service.metrics()
    for outcome in ("ok", "error", "rejected"):
        assert metrics[f'inference_requests_total{{outcome="{outcome}"}}'] == 1
        assert metrics[f'inference_request_duration_seconds_count{{outcome="{outcome}"}}'] == 1
    # The shed request never waited for a slot; the other two did.
    assert metrics["inference_queue_wait_seconds_count"] == 2


async def test_the_histograms_record_real_durations(start: Start, gate: Gate):
    service = await start(model_slots=1, queue_size=1)
    running = service.infer()
    await service.wait_for(in_flight=1, queued=0)
    waiting = service.infer()
    await service.wait_for(in_flight=1, queued=1)

    await asyncio.sleep(0.05)
    gate.open()
    await asyncio.gather(running, waiting)

    # Lower bounds only: both requests were in the service for the 0.05 s above,
    # and one of them spent that time in the queue. A slow machine only adds to it.
    metrics = await service.metrics()
    assert metrics['inference_request_duration_seconds_sum{outcome="ok"}'] >= 2 * 0.04
    assert metrics['inference_request_duration_seconds_bucket{le="0.025",outcome="ok"}'] == 0
    assert metrics['inference_request_duration_seconds_bucket{le="+Inf",outcome="ok"}'] == 2
    assert metrics["inference_queue_wait_seconds_sum"] >= 0.04
    assert metrics['inference_queue_wait_seconds_bucket{le="0.025"}'] <= 1
    assert metrics['inference_queue_wait_seconds_bucket{le="+Inf"}'] == 2
    assert (await waiting).json()["queue_wait_seconds"] >= 0.04
