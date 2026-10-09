"""2. Scrape: Prometheus scrapes every pod, and what it stores follows the traffic."""

import pytest

from e2e.cluster import COMMIT, NAMESPACE, RELEASE, eventually, kubectl, request
from e2e.monitoring import CATCH_UP_SECONDS, query_prometheus

# Each test may wait for Prometheus up to CATCH_UP_SECONDS, more than once.
pytestmark = pytest.mark.timeout(900)

# The queries. Prometheus labels what it scrapes from this release's pods with
# the namespace of the Service and, as the job, with the Service's name.
FROM_THE_SERVICE = f'namespace="{NAMESPACE}", job="{RELEASE}"'
UP = f"up{{{FROM_THE_SERVICE}}}"
BUILD_INFO = f"inference_build_info{{{FROM_THE_SERVICE}}}"
REQUESTS_ANSWERED_OK = f'sum(inference_requests_total{{{FROM_THE_SERVICE}, outcome="ok"}})'


def pod_names() -> set[str]:
    names = kubectl(
        "get",
        "pods",
        f"--namespace={NAMESPACE}",
        f"--selector=app.kubernetes.io/instance={RELEASE}",
        "--output=jsonpath={.items[*].metadata.name}",
    )
    return set(names.split())


def test_prometheus_reports_every_pod_as_up() -> None:
    def every_pod_is_up() -> None:
        pods = pod_names()
        up = {labels["pod"]: value for labels, value in query_prometheus(UP)}
        assert pods, "the release has no pods"
        assert up == dict.fromkeys(pods, 1.0)

    eventually(every_pod_is_up, CATCH_UP_SECONDS)


def test_the_request_counter_moves_with_the_requests() -> None:
    requests = 3

    def answered_ok() -> float:
        total = query_prometheus(REQUESTS_ANSWERED_OK)
        assert len(total) == 1, "Prometheus has no request counter for the service yet"
        return total[0][1]

    before = eventually(answered_ok, CATCH_UP_SECONDS)
    for _ in range(requests):
        status, body = request("POST", "/v1/infer", {"input": "count me"})
        assert status == 200, body

    # At least, not exactly: a request answered just before this test began may
    # not have been scraped yet when `before` was read.
    def the_requests_are_counted() -> None:
        assert answered_ok() >= before + requests

    eventually(the_requests_are_counted, CATCH_UP_SECONDS)


def test_every_pod_reports_the_commit_under_test() -> None:
    """The smoke test reads the build of whichever pod answers it. Prometheus has all of them."""

    def every_pod_runs_the_commit() -> None:
        pods = pod_names()
        commits = {labels["pod"]: labels["git_sha"] for labels, _ in query_prometheus(BUILD_INFO)}
        assert pods, "the release has no pods"
        assert commits == dict.fromkeys(pods, COMMIT)

    eventually(every_pod_runs_the_commit, CATCH_UP_SECONDS)
