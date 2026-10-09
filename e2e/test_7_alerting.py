"""7. Alerting: Prometheus has the chart's alert rules and can evaluate them.

When the alerts fire is worked out, and tested without a cluster, in
tests/rules/.
"""

import re
from pathlib import Path
from typing import Any

import pytest

from e2e.cluster import RELEASE, eventually
from e2e.monitoring import CATCH_UP_SECONDS, PROMETHEUS, get

# The first test may wait for Prometheus up to CATCH_UP_SECONDS.
pytestmark = pytest.mark.timeout(900)

VERSIONS = Path(__file__).resolve().parent.parent / "versions.env"


def test_prometheus_evaluates_every_rule_of_the_chart() -> None:
    """Prometheus has loaded the chart's rules, and none of them ends in an error."""

    def every_rule_is_healthy() -> None:
        rules = chart_rules()
        assert rules, "Prometheus has not loaded the chart's rules"
        unhealthy = [rule for rule in rules if rule["health"] != "ok"]
        assert not unhealthy, f"not evaluated yet, or evaluated with an error: {unhealthy}"

    eventually(every_rule_is_healthy, CATCH_UP_SECONDS)


def test_the_cluster_runs_the_prometheus_release_whose_promtool_checked_the_rules() -> None:
    """ci.yml checks the rules with the promtool of PROMETHEUS_IMAGE in versions.env."""
    image = re.search(r"^PROMETHEUS_IMAGE=\S+:v(\d+\.\d+\.\d+)", VERSIONS.read_text(), re.MULTILINE)
    assert image, f"{VERSIONS} names no Prometheus release"

    running = get(PROMETHEUS, "/api/v1/status/buildinfo")["data"]["version"]

    assert running == image[1]


def chart_rules() -> list[dict[str, Any]]:
    """The rules of the chart's group, each with its health and, for an alert, its state."""
    groups = get(PROMETHEUS, "/api/v1/rules")["data"]["groups"]
    return [rule for group in groups if group["name"] == RELEASE for rule in group["rules"]]
