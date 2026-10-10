"""6. Autoscaling: the replica count follows the requests in the service, one pod at a time.

The load is 24 clients that each keep one request under way. The number the
autoscaler acts on, requests running or waiting, is therefore just under 24
for as long as the load lasts: on the CI cluster it averaged 22 to 23, since
every request spends a few milliseconds on its way to a pod and back, where
no pod counts it. The autoscaler wants that number divided by the chart's
target of 6, rounded up: 4 pods, where 2 run when nobody asks anything of the
service. What it counted per pod goes into the report of every run, next to
the replica counts.
"""

import itertools
import json
import time
from typing import Any, NamedTuple

import pytest

from e2e import load
from e2e.cluster import NAMESPACE, RELEASE, deployment, eventually, helm, kubectl
from e2e.monitoring import CATCH_UP_SECONDS
from loadgen.load import SLICE_SECONDS

# Three minutes of load, and then the wait for the pods it brought to go again.
pytestmark = pytest.mark.timeout(1200)

CLIENTS = 24
SECONDS = 180
# The chart's target: this many requests per pod, running or waiting.
TARGET = 6
# The replica counts that go with it: the chart's minimum when there is no
# load, and under this load what 22 to 24 requests at 6 a pod round up to.
WITHOUT_LOAD = 2
UNDER_LOAD = 4
# The chart's limit on scaling up: one more pod in any 30 seconds. It is
# written out here and not read from the release, so that a limit loosened in
# the chart's values fails the test and does not change what it checks.
ONE_POD = 1
EVERY_SECONDS = 30
# How often the test asks how many pods the autoscaler wants.
SAMPLE_SECONDS = 5


class Sample(NamedTuple):
    """The replica count the Deployment was told to have, read once.

    The count is what the API server answered with, so it held at some moment
    between the asking and the answer, and at no particular one.
    """

    asked: float
    answered: float
    replicas: int


@pytest.fixture(scope="module")
def autoscaling() -> dict[str, Any]:
    """The release's settings under `autoscaling`: the chart's values as the cluster got them."""
    values = helm("get", "values", RELEASE, f"--namespace={NAMESPACE}", "--all", "--output=json")
    return json.loads(values)["autoscaling"]


def test_keda_has_made_the_autoscaler_the_chart_describes(autoscaling: dict[str, Any]) -> None:
    """The chart holds a ScaledObject. What scales the Deployment is the
    HorizontalPodAutoscaler KEDA makes from it, so that is where the chart's
    settings have to have arrived."""

    def the_autoscaler_has_read_its_number() -> dict[str, Any]:
        made = autoscaler()
        conditions = {
            condition["type"]: condition["status"]
            for condition in made["status"].get("conditions", [])
        }
        # True once Kubernetes has got the number from KEDA, and KEDA from Prometheus.
        assert conditions.get("ScalingActive") == "True", made["status"]
        return made

    spec = eventually(the_autoscaler_has_read_its_number, CATCH_UP_SECONDS)["spec"]

    assert spec["scaleTargetRef"]["kind"] == "Deployment"
    assert spec["scaleTargetRef"]["name"] == RELEASE
    assert spec["minReplicas"] == autoscaling["minReplicas"]
    assert spec["maxReplicas"] == autoscaling["maxReplicas"]
    # One number, from outside Kubernetes, compared per pod with the target.
    (metric,) = spec["metrics"]
    assert autoscaling["targetRequestsPerPod"] == TARGET
    assert metric["external"]["target"] == {"type": "AverageValue", "averageValue": str(TARGET)}
    assert spec["behavior"]["scaleUp"]["policies"] == [
        {
            "type": "Pods",
            "value": autoscaling["scaleUp"]["pods"],
            "periodSeconds": autoscaling["scaleUp"]["periodSeconds"],
        }
    ]
    assert (
        spec["behavior"]["scaleDown"]["stabilizationWindowSeconds"]
        == autoscaling["scaleDown"]["stabilizationWindowSeconds"]
    )


def test_the_replica_count_follows_the_load_one_pod_at_a_time_and_returns() -> None:
    samples = [sample()]
    assert samples[0].replicas == WITHOUT_LOAD, f"{samples[0].replicas} replicas before the load"

    began = time.monotonic()
    job = load.start("autoscaling", seconds=SECONDS, clients=CLIENTS)
    ended = None  # when the load was seen to be over
    # The requests per pod the autoscaler counted, for each number of pods it
    # counted them on.
    counted: dict[int, list[float]] = {}
    while True:
        samples.append(sample())
        # Only while the load is sure to be running: it started after `began`
        # and lasts SECONDS.
        if time.monotonic() - began < SECONDS and (count := requests_per_pod()):
            pods, per_pod = count
            counted.setdefault(pods, []).append(per_pod)
        if ended is None and not load.is_running(job):
            ended = time.monotonic()
        # Sampling goes on after the load, until the pods it brought are no
        # longer asked for, or for as long as that can reasonably take.
        if ended is not None and (
            samples[-1].replicas == WITHOUT_LOAD or time.monotonic() - ended > CATCH_UP_SECONDS
        ):
            break
        time.sleep(max(0.0, samples[-1].asked + SAMPLE_SECONDS - time.monotonic()))

    summary = load.summary(job)
    most = max(sample.replicas for sample in samples)
    too_fast = rises_faster_than(ONE_POD, EVERY_SECONDS, samples)
    # How long requests waited for a slot, for each half minute of the load.
    queue_waits = summary["queue_wait_seconds"]
    notes = [f"{CLIENTS} clients for {SECONDS} s, each with one request under way."]
    if counted:
        notes.append(
            f"Requests per pod as the autoscaler counted them during the load,"
            f" against its target of {TARGET}: {spans(counted)}."
        )
    if queue_waits:
        notes.append(
            f"Queue wait in the first {SLICE_SECONDS} s of the load:"
            f" {percentiles(queue_waits[0])}."
            f" In its last {SLICE_SECONDS} s: {percentiles(queue_waits[-1])}."
        )
    timeline = replica_timeline(samples, began, ended)
    load.save_report("6-autoscaling", "Autoscaling", summary, notes, timeline)

    # A request that is shed, or lost while a pod starts, is a failure here too.
    assert summary["failures"] == 0, summary["errors"]
    assert most >= UNDER_LOAD, f"the autoscaler never asked for more than {most} pods"
    assert not too_fast, f"more than {ONE_POD} more within {EVERY_SECONDS} s: {'; '.join(too_fast)}"
    assert samples[-1].replicas == WITHOUT_LOAD, (
        f"still {samples[-1].replicas} replicas {CATCH_UP_SECONDS} s after the load ended"
    )
    # What the pods were added for: most requests now find a slot free.
    assert queue_waits[-1]["p50"] < queue_waits[0]["p50"], f"no shorter a wait: {queue_waits}"


def autoscaler() -> dict[str, Any]:
    """The HorizontalPodAutoscaler KEDA made for the release, as Kubernetes holds it."""
    return json.loads(
        kubectl(
            "get", "horizontalpodautoscaler", RELEASE, f"--namespace={NAMESPACE}", "--output=json"
        )
    )


def sample() -> Sample:
    asked = time.monotonic()
    replicas = int(eventually(lambda: deployment(".spec.replicas")))
    return Sample(asked, time.monotonic(), replicas)


def requests_per_pod() -> tuple[int, float] | None:
    """The autoscaler's latest count: on how many pods, and how many requests per pod.

    It is the number the autoscaler compares with its target. Every 15
    seconds it works the number out afresh and writes it into its status,
    next to the number of pods it divided by. None before it has done so.
    """
    status = eventually(autoscaler)["status"]
    metrics = status.get("currentMetrics") or []
    if not metrics:
        return None
    # A Kubernetes quantity: "6" is 6, and "5656m" is 5.656.
    average = metrics[0]["external"]["current"]["averageValue"]
    per_pod = float(average[:-1]) / 1000 if average.endswith("m") else float(average)
    return status["currentReplicas"], per_pod


def rises_faster_than(pods: int, seconds: float, samples: list[Sample]) -> list[str]:
    """Every rise by more than `pods` that two of the samples prove took `seconds` at most.

    Two samples prove that if the later one was answered no more than
    `seconds` after the earlier one was asked for: whenever exactly the two
    counts held, they held no further apart than that. Measured from answer
    to answer, two steps that the autoscaler took a full period apart could
    look closer together than they were, by as long as one answer took.
    """
    quickest: dict[tuple[int, int], float] = {}
    for earlier, later in itertools.combinations(samples, 2):
        took = later.answered - earlier.asked
        if took <= seconds and later.replicas - earlier.replicas > pods:
            rise = (earlier.replicas, later.replicas)
            quickest[rise] = min(took, quickest.get(rise, took))
    return [f"from {low} to {high} within {took:.0f} s" for (low, high), took in quickest.items()]


def replica_timeline(samples: list[Sample], began: float, ended: float) -> list[tuple[str, str]]:
    """The counts the samples went through, each with the time it was first seen.

    A table for the report: the first row names its columns.
    """
    rows = [("When", "Replicas asked for"), ("before the load", str(samples[0].replicas))]
    for before, after in itertools.pairwise(samples):
        if after.replicas != before.replicas:
            if after.answered <= ended:
                when = f"{after.answered - began:.0f} s after the load began"
            else:
                when = f"{after.answered - ended:.0f} s after the load ended"
            rows.append((when, str(after.replicas)))
    return rows


def spans(counted: dict[int, list[float]]) -> str:
    """For each number of pods, the least and the most requests per pod counted on it."""
    told = []
    for pods, counts in sorted(counted.items()):
        least, most = f"{min(counts):.1f}", f"{max(counts):.1f}"
        span = least if least == most else f"{least} to {most}"
        told.append(f"{span} on {pods} pods")
    return ", ".join(told)


def percentiles(queue_wait: dict[str, float]) -> str:
    return ", ".join(f"{key} {queue_wait[key] * 1000:.0f} ms" for key in ("p50", "p95", "p99"))
