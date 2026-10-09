"""What the tests know about the cluster, and how they act on it.

Everything goes through kubectl, and through helm where a test upgrades the
release. An HTTP request is made from a pod inside the cluster, the way a
client of the service makes it: to the Service's DNS name. `kubectl
port-forward` would be less work, but it connects to one pod and so goes
around the Service that the tests are about.
"""

import itertools
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

# The names `make up` uses. The chart names the Deployment and the Service
# after the release.
NAMESPACE = "inference"
RELEASE = "inference"
SERVICE_URL = f"http://{RELEASE}.{NAMESPACE}.svc.cluster.local"
CHART = Path(__file__).resolve().parent.parent / "chart"

# Client pods run outside the service's namespace, as its clients would. That
# namespace would not admit them anyway: it enforces the restricted Pod
# Security Standard, which a pod made by a plain `kubectl run` does not meet.
IN_CLIENT_NAMESPACE = "--namespace=default"

# The commit the cluster is supposed to be running. `make e2e` passes it in.
COMMIT = os.environ["GIT_SHA"]

# The longest a test waits for something that normally takes a few seconds. It
# is generous on purpose: shared runners are slow, and only a test that is
# about to fail ever waits this long.
PATIENCE_SECONDS = 120

# Run with `python -c` in a pod: makes one request, then prints the status and
# the body as JSON. It needs nothing but the standard library, so the service's
# own image, which is on every node already, can run it.
CLIENT = """
import json, sys, urllib.error, urllib.request

method, url, *body = sys.argv[1:]
request = urllib.request.Request(
    url,
    method=method,
    data=body[0].encode() if body else None,
    headers={"Content-Type": "application/json"},
)
try:
    response = urllib.request.urlopen(request, timeout=30)
except urllib.error.HTTPError as error:  # an answer, but not a 2xx one
    response = error
print(json.dumps({"status": response.status, "body": response.read().decode()}))
"""


def kubectl(*arguments: str, stdin: str | None = None, timeout: float = 60) -> str:
    """Run kubectl and return what it printed.

    A command that fails, fails the test: with an assertion, so that a test
    which is waiting for the cluster to catch up can use `eventually` to try again.
    """
    return run("kubectl", *arguments, stdin=stdin, timeout=timeout)


def helm(*arguments: str, timeout: float = 60) -> str:
    """Run helm and return what it printed. It reads the same kubeconfig as kubectl."""
    return run("helm", *arguments, stdin=None, timeout=timeout)


def run(*command: str, stdin: str | None, timeout: float) -> str:
    result = subprocess.run(
        command, input=stdin, capture_output=True, text=True, timeout=timeout, check=False
    )
    assert result.returncode == 0, f"{' '.join(command)} failed:\n{result.stderr}"
    return result.stdout


def request(method: str, path: str, body: dict[str, object] | None = None) -> tuple[int, str]:
    """Send one HTTP request to the Service from inside the cluster.

    Returns the status and the body of the answer.
    """
    arguments = [method, SERVICE_URL + path]
    if body is not None:
        arguments.append(json.dumps(body))
    answer = json.loads(run_in_pod("python", "-c", CLIENT, *arguments))
    return answer["status"], answer["body"]


def deployment(field: str) -> str:
    """One field of the service's Deployment, named the way kubectl's jsonpath names it."""
    return kubectl(
        "get", "deployment", RELEASE, f"--namespace={NAMESPACE}", f"--output=jsonpath={{{field}}}"
    )


def pods() -> dict[str, str]:
    """The service's pods, each with the node it is on.

    A pod that has been told to go is listed until it is gone.
    """
    listed = kubectl(
        "get",
        "pods",
        f"--namespace={NAMESPACE}",
        f"--selector=app.kubernetes.io/instance={RELEASE}",
        "--output=json",
    )
    return {
        pod["metadata"]["name"]: pod["spec"].get("nodeName", "")
        for pod in json.loads(listed)["items"]
    }


@contextmanager
def ready_replicas() -> Iterator[list[int]]:
    """Collect the numbers of Ready replicas the Deployment goes through during the `with` block.

    `kubectl get --watch` prints the object once and then again at each change
    to it, so a number that held only for a moment is in the list too. The
    list is filled in when the block ends, with each change of the number
    listed once.
    """
    watch = subprocess.Popen(
        [
            "kubectl",
            "get",
            "deployment",
            RELEASE,
            f"--namespace={NAMESPACE}",
            "--watch",
            # The field is left out of the object while it is zero, which
            # prints as an empty line.
            '--output=jsonpath={.status.readyReplicas}{"\\n"}',
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    numbers: list[int] = []
    try:
        yield numbers
    finally:
        watched_to_the_end = watch.poll() is None
        watch.terminate()
        printed, _ = watch.communicate(timeout=30)
    assert watched_to_the_end, "kubectl stopped watching the Deployment before the test was done"
    reported = [int(line or 0) for line in printed.splitlines()]
    assert reported, "kubectl reported nothing about the Deployment"
    numbers.extend(number for number, _ in itertools.groupby(reported))


def run_in_pod(*command: str) -> str:
    """Run a command in a new pod of the service's image and return what it printed."""
    image = deployment(".spec.template.spec.containers[0].image")
    pod = f"client-{uuid.uuid4().hex[:8]}"
    kubectl(
        "run",
        pod,
        IN_CLIENT_NAMESPACE,
        f"--image={image}",
        "--image-pull-policy=Never",  # the image is on the nodes; there is no registry
        "--restart=Never",  # a pod that runs the command once, not a server to keep up
        "--command",
        "--",
        *command,
    )
    try:
        phase = wait_until_finished(pod)
        output = kubectl("logs", pod, IN_CLIENT_NAMESPACE)
        if phase == "Failed":
            pytest.fail(f"the command in pod {pod} failed:\n{output}")
        return output
    finally:
        kubectl("delete", "pod", pod, IN_CLIENT_NAMESPACE, "--wait=false")


def eventually[Result](check: Callable[[], Result], seconds: float = PATIENCE_SECONDS) -> Result:
    """Call a check that asserts, again and again, until it passes. Returns what it returns.

    The cluster reaches the state a test expects in its own time, so a test
    states that state as assertions and hands them to this function. If they
    still do not hold when the time is up, the test fails with the last failure.
    """
    deadline = time.monotonic() + seconds
    while True:
        try:
            return check()
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
        time.sleep(2)


def wait_until_finished(pod: str) -> str:
    """Wait for the pod's command to exit. Returns the pod's phase: Succeeded or Failed."""
    deadline = time.monotonic() + PATIENCE_SECONDS
    while time.monotonic() < deadline:
        phase = kubectl("get", "pod", pod, IN_CLIENT_NAMESPACE, "--output=jsonpath={.status.phase}")
        if phase in ("Succeeded", "Failed"):
            return phase
        time.sleep(1)
    description = kubectl("describe", "pod", pod, IN_CLIENT_NAMESPACE)
    pytest.fail(f"pod {pod} had not finished after {PATIENCE_SECONDS} seconds:\n{description}")
