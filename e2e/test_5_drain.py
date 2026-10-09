"""5. Drain: emptying a node under load fails no request and keeps to the PodDisruptionBudget."""

import json
import time

import pytest

from e2e import load
from e2e.cluster import NAMESPACE, RELEASE, eventually, kubectl, pods
from e2e.cluster import ready_replicas as watching_ready_replicas

# The load runs for a minute and a half, and each wait has its own patience.
pytestmark = pytest.mark.timeout(900)

ROLLOUT_STATUS = (
    "rollout",
    "status",
    f"deployment/{RELEASE}",
    f"--namespace={NAMESPACE}",
    "--timeout=120s",
)


def test_draining_the_node_that_holds_every_pod_fails_no_request() -> None:
    node = gather_every_pod_on_one_worker()
    demanded = eventually(healthy_pods_the_budget_demands)

    job = load.start("drain")
    began = time.monotonic()
    try:
        with watching_ready_replicas() as ready_replicas:
            # Evicts every pod on the node except those of DaemonSets, which
            # belong to the node, and returns when the evicted pods are gone.
            kubectl("drain", node, "--ignore-daemonsets", "--timeout=240s", timeout=270)
            # Returns when the last replacement is Ready.
            kubectl(*ROLLOUT_STATUS, timeout=150)
        took = time.monotonic() - began
        load_outlasted_the_drain = load.is_running(job)
        pods_left_on_the_node = [pod for pod, its_node in pods().items() if its_node == node]
    finally:
        kubectl("uncordon", node)

    summary = load.summary(job)
    load.save_report(
        "5-drain",
        "Drain",
        summary,
        [
            f"Draining the node that held every pod took {took:.0f} s.",
            f"Ready replicas while it ran: {', '.join(map(str, ready_replicas))}."
            f" The PodDisruptionBudget demands at least {demanded}.",
        ],
    )

    # Had the load ended first, it would say nothing about the end of the drain.
    assert load_outlasted_the_drain, f"the drain took {took:.0f} s, longer than the load"
    assert not pods_left_on_the_node, f"{node} was drained, but still has {pods_left_on_the_node}"
    assert min(ready_replicas) >= demanded, ready_replicas
    load.assert_no_request_failed(summary)


def gather_every_pod_on_one_worker() -> str:
    """Put all of the service's pods on one worker node. Returns that node.

    Draining a node that holds every pod is the hard case. The eviction API
    is asked for all of them at once, and only the PodDisruptionBudget makes
    them leave one at a time, each after the last one's replacement is Ready.
    If the pods were spread out, a drain would take one of them at most, and
    the budget would have nothing to do.
    """
    placement = pods()
    node = min(placement.values())
    other_workers = [worker for worker in workers() if worker != node]
    # With the other workers closed to new pods, a pod deleted there can only
    # come back on the chosen node.
    for worker in other_workers:
        kubectl("cordon", worker)
    try:
        elsewhere = [pod for pod, its_node in placement.items() if its_node != node]
        if elsewhere:
            kubectl("delete", "pod", *elsewhere, f"--namespace={NAMESPACE}", timeout=150)
            kubectl(*ROLLOUT_STATUS, timeout=150)
    finally:
        for worker in other_workers:
            kubectl("uncordon", worker)
    assert set(pods().values()) == {node}, f"not every pod is on {node}: {pods()}"
    return node


def workers() -> list[str]:
    not_the_control_plane = "--selector=!node-role.kubernetes.io/control-plane"
    names = kubectl(
        "get", "nodes", not_the_control_plane, "--output=jsonpath={.items[*].metadata.name}"
    )
    return names.split()


def healthy_pods_the_budget_demands() -> int:
    """The number of healthy pods the PodDisruptionBudget insists on.

    Fails for as long as the budget would not permit an eviction.
    """
    budget = kubectl(
        "get", "poddisruptionbudget", RELEASE, f"--namespace={NAMESPACE}", "--output=json"
    )
    status = json.loads(budget)["status"]
    assert status["disruptionsAllowed"] == 1, f"the budget does not allow one eviction: {status}"
    return status["desiredHealthy"]
