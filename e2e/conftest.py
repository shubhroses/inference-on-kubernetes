"""The end-to-end tests run against the kind cluster that `make up` creates.

Start them with `make e2e`. They create and delete objects in whatever cluster
kubectl is pointed at, so before any test is collected the check below refuses
to go on unless that is the kind cluster and nothing else.
"""

import os
import subprocess
from pathlib import Path

import pytest

# The kubeconfig the Makefile exports, and the context kind writes to it for
# the cluster the Makefile calls inference-on-kubernetes.
KUBECONFIG = Path(__file__).resolve().parent.parent / ".kube" / "config"
CONTEXT = "kind-inference-on-kubernetes"


def pytest_sessionstart(session: pytest.Session) -> None:
    """Called by pytest once, before it collects any test."""
    kubeconfig = os.environ.get("KUBECONFIG")
    if kubeconfig is None or Path(kubeconfig).resolve() != KUBECONFIG:
        refuse(f"KUBECONFIG is {kubeconfig!r}, not this repository's own {KUBECONFIG}")

    # Reads the file only: this command does not contact a cluster.
    current = subprocess.run(
        ["kubectl", "config", "current-context"], capture_output=True, text=True, check=False
    )
    context = current.stdout.strip()
    if context != CONTEXT:
        refuse(f"the current context is {context!r}, not {CONTEXT!r}")


def refuse(reason: str) -> None:
    pytest.exit(
        f"Refusing to run the end-to-end tests: {reason}. Run `make up`, then `make e2e`.",
        returncode=pytest.ExitCode.USAGE_ERROR,
    )
