"""Load on the Service while a test changes the cluster under it.

The load generator in loadgen/ runs as a Job inside the cluster and sends its
requests to the Service's DNS name, so each new connection is routed to a pod
by the cluster, the way a real client's is. Its module docstring describes the
clients it stands for.
"""

import json
import re
import string
from pathlib import Path
from typing import Any

from e2e.cluster import (
    IN_CLIENT_NAMESPACE,
    PATIENCE_SECONDS,
    SERVICE_URL,
    deployment,
    eventually,
    kubectl,
)

REPOSITORY = Path(__file__).resolve().parent.parent
JOB = Path(__file__).with_name("loadgen-job.yaml")
# What each test writes here, CI shows in the job summary and keeps as an artifact.
REPORTS = REPOSITORY / "reports"

# The load of the rollout and the drain test. Half of the requests go over the
# kept connections, half over a new connection each. The alert test sends at
# the same rate, for as long as it needs.
RATE = 20  # requests per second
SECONDS = 90
REQUESTS = RATE * SECONDS
KEPT_CONNECTIONS = 8


def start(test: str, *, seconds: int = SECONDS, fail_ratio: float = 0, clients: int = 0) -> str:
    """Start the load. Returns the Job's name, once the load is under way.

    The load is RATE requests a second or, with `clients`, comes from that
    many clients which each send one request after another. With `fail_ratio`,
    that share of the requests asks the service to fail.
    """
    job = f"load-{test}"
    # The source travels as a ConfigMap, which the Job mounts into a pod of
    # the service's image. `create --dry-run` only writes the ConfigMap down;
    # `apply` then creates it or brings an older one up to date.
    source = kubectl(
        "create",
        "configmap",
        "loadgen",
        f"--from-file={REPOSITORY / 'loadgen'}",
        IN_CLIENT_NAMESPACE,
        "--dry-run=client",
        "--output=yaml",
    )
    kubectl("apply", "--filename=-", IN_CLIENT_NAMESPACE, stdin=source)
    arguments = [f"--url={SERVICE_URL}/v1/infer", f"--seconds={seconds}"]
    if clients:
        arguments += [f"--clients={clients}"]
    else:
        arguments += [f"--rate={RATE}", f"--connections={KEPT_CONNECTIONS}"]
    arguments += [f"--fail-ratio={fail_ratio}"]
    manifest = string.Template(JOB.read_text()).substitute(
        name=job,
        image=deployment(".spec.template.spec.containers[0].image"),
        arguments=json.dumps(arguments),
    )
    # A Job of this name may be left from an earlier run against the same cluster.
    kubectl("delete", "job", job, IN_CLIENT_NAMESPACE, "--ignore-not-found")
    kubectl("create", "--filename=-", IN_CLIENT_NAMESPACE, stdin=manifest)

    # The load generator reports its progress every five seconds. By its first
    # report the kept connections are open and in use.
    def the_load_is_under_way() -> None:
        log = kubectl("logs", f"job/{job}", IN_CLIENT_NAMESPACE)
        assert re.search(r"\d+ requests sent", log), f"the load has not got going yet:\n{log}"

    eventually(the_load_is_under_way)
    return job


def is_running(job: str) -> bool:
    active = kubectl("get", "job", job, IN_CLIENT_NAMESPACE, "--output=jsonpath={.status.active}")
    return active == "1"


def progress(job: str) -> tuple[int, int]:
    """How many requests the load has sent, and how many have failed, by its latest report."""
    log = kubectl("logs", f"job/{job}", IN_CLIENT_NAMESPACE)
    sent, failed = re.findall(r"(\d+) requests sent, (\d+) failed", log)[-1]
    return int(sent), int(failed)


def stop(job: str) -> None:
    """End a load before its time is up. Returns when its pod is gone, and its requests with it."""
    # Deleting a Job leaves its pod to be cleaned up afterwards. In the
    # foreground, the Job is gone only when the pod is.
    kubectl("delete", "job", job, IN_CLIENT_NAMESPACE, "--cascade=foreground", timeout=120)


def summary(job: str) -> dict[str, Any]:
    """Wait for the load to end. Returns the load generator's summary of the run."""

    def ended() -> dict[str, Any]:
        job_status = json.loads(kubectl("get", "job", job, IN_CLIENT_NAMESPACE, "--output=json"))
        status = job_status["status"]
        assert status.get("succeeded") or status.get("failed"), "the load is still running"
        return status

    status = eventually(ended, SECONDS + PATIENCE_SECONDS)
    log = kubectl("logs", f"job/{job}", IN_CLIENT_NAMESPACE)
    assert status.get("succeeded"), f"the load generator itself failed:\n{log}"
    # The summary is the last line; the lines before it are progress and failures.
    return json.loads(log.splitlines()[-1])


def save_report(
    name: str,
    title: str,
    load: dict[str, Any] | None,
    notes: list[str],
    timeline: list[tuple[str, str]] | None = None,
) -> None:
    """Keep what a test measured, whether or not the test goes on to pass.

    `load` is the load generator's summary of a run, if the test has one.
    `timeline` is a table of what the test saw change, and when, if it
    followed something over time. Its first row names the two columns.
    """
    REPORTS.mkdir(exist_ok=True)
    report = {"test": title, "load": load, "timeline": timeline or [], "notes": notes}
    (REPORTS / f"{name}.json").write_text(json.dumps(report, indent=2) + "\n")


def assert_no_request_failed(summary: dict[str, Any]) -> None:
    assert summary["requests"] == REQUESTS
    assert summary["failures"] == 0, summary["errors"]
    # Two checks on the load itself. Every client that keeps a connection
    # opened one at the start and had to open another when the pod at its
    # other end was told to go: at least twice as many connections as clients.
    # And the clients did keep them: far fewer connections than the 900
    # requests that went over them.
    opened = summary["kept_connections_opened"]
    assert 2 * KEPT_CONNECTIONS <= opened <= REQUESTS / 20, opened
