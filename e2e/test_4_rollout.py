"""4. Rollout: a rolling update under load fails no request."""

import time
import uuid

import pytest

from e2e import load
from e2e.cluster import CHART, NAMESPACE, RELEASE, deployment, eventually, helm, kubectl, pods
from e2e.cluster import ready_replicas as watching_ready_replicas

# The load runs for a minute and a half, and each wait has its own patience.
pytestmark = pytest.mark.timeout(900)


def test_a_rolling_update_under_load_fails_no_request() -> None:
    replicas = int(deployment(".spec.replicas"))
    old_pods = set(pods())
    assert len(old_pods) == replicas, f"expected {replicas} pods before the update: {old_pods}"

    job = load.start("rollout")
    began = time.monotonic()
    with watching_ready_replicas() as ready_replicas:
        # An upgrade that changes nothing but an annotation on the pods. Any
        # change to the pod template makes the Deployment replace every pod,
        # just as a new image tag would.
        helm(
            "upgrade",
            RELEASE,
            str(CHART),
            f"--namespace={NAMESPACE}",
            "--reuse-values",
            f"--set-string=podAnnotations.rollout={uuid.uuid4().hex[:8]}",
            "--wait",
            "--timeout=5m",
            timeout=330,
        )
        # Fails unless the rollout has finished: every pod new, and Ready.
        kubectl(
            "rollout",
            "status",
            f"deployment/{RELEASE}",
            f"--namespace={NAMESPACE}",
            "--timeout=120s",
            timeout=150,
        )

        # The old pods are still draining at that point, which is the part
        # of an update this test is most about.
        def the_old_pods_are_gone() -> set[str]:
            now = set(pods())
            assert not now & old_pods, f"still there: {now & old_pods}"
            return now

        new_pods = eventually(the_old_pods_are_gone)
    took = time.monotonic() - began
    load_outlasted_the_update = load.is_running(job)

    summary = load.summary(job)
    load.save_report(
        "4-rollout",
        "Rollout",
        summary,
        [
            f"The update replaced {len(old_pods)} pods in {took:.0f} s, their drain included.",
            f"Ready replicas while it ran: {', '.join(map(str, ready_replicas))}.",
        ],
    )

    # Had the load ended first, it would say nothing about the end of the update.
    assert load_outlasted_the_update, f"the update took {took:.0f} s, longer than the load"
    assert len(new_pods) == replicas, f"expected {replicas} pods after the update: {new_pods}"
    # maxUnavailable 0: never fewer Ready pods than the Deployment asks for.
    assert min(ready_replicas) >= replicas, ready_replicas
    load.assert_no_request_failed(summary)
