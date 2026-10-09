"""What the tests know about the cluster, and how they act on it.

Everything goes through kubectl. An HTTP request is made from a pod inside the
cluster, the way a client of the service makes it: to the Service's DNS name.
`kubectl port-forward` would be less work, but it connects to one pod and so
goes around the Service that the tests are about.
"""

import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable

import pytest

# The names `make up` uses. The chart names the Deployment and the Service
# after the release.
NAMESPACE = "inference"
RELEASE = "inference"
SERVICE_URL = f"http://{RELEASE}.{NAMESPACE}.svc.cluster.local"

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


def kubectl(*arguments: str) -> str:
    """Run kubectl and return what it printed.

    A command that fails, fails the test: with an assertion, so that a test
    which is waiting for the cluster to catch up can use `eventually` to try again.
    """
    result = subprocess.run(
        ["kubectl", *arguments], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, f"kubectl {' '.join(arguments)} failed:\n{result.stderr}"
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


def run_in_pod(*command: str) -> str:
    """Run a command in a new pod of the service's image and return what it printed."""
    image = kubectl(
        "get",
        "deployment",
        RELEASE,
        f"--namespace={NAMESPACE}",
        "--output=jsonpath={.spec.template.spec.containers[0].image}",
    )
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
