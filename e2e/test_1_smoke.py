"""1. Smoke: the Service reaches the pods, and the pods run the commit under test."""

import json

import pytest

from e2e.cluster import COMMIT, request

# Longer than the unit tests' limit, because each request starts a pod first.
pytestmark = pytest.mark.timeout(300)


def test_a_request_through_the_service_returns_200() -> None:
    status, body = request("POST", "/v1/infer", {"input": "hello"})

    assert status == 200, body
    assert json.loads(body)["input_chars"] == 5


def test_the_build_info_metric_reports_the_commit_under_test() -> None:
    status, body = request("GET", "/metrics")

    assert status == 200, body
    build_info = [line for line in body.splitlines() if line.startswith("inference_build_info{")]
    assert build_info == [f'inference_build_info{{git_sha="{COMMIT}"}} 1.0']
