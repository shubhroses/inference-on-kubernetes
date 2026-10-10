"""7. Alerting: the page fires while requests fail, and clears when they stop.

The cluster's alerts have the short windows of chart/values-kind.yaml, so this
test is about the wiring: the chart's rules reach Prometheus, Prometheus can
evaluate them, and the page follows what the service does. When the alerts
fire with the windows a real cluster gets is worked out, and tested, in
tests/rules/.
"""

import re
import time
from pathlib import Path
from typing import Any

import pytest

from e2e import load
from e2e.cluster import NAMESPACE, RELEASE, eventually
from e2e.monitoring import CATCH_UP_SECONDS, PROMETHEUS, get, query_prometheus

# Two waits of up to three minutes each, and four times a load starts or stops.
pytestmark = pytest.mark.timeout(900)

# The fast-burn alert, and the error ratios it compares with its threshold.
PAGE = "InferenceErrorBudgetFastBurn"
ERROR_RATIOS = (
    "namespace_job:inference_errors_per_request:ratio_rate"
    f'{{namespace="{NAMESPACE}", job="{RELEASE}"}}'
)

# The share of the requests that is made to fail, and how long the page has to
# fire once they do, and to clear once they stop.
FAIL_RATIO = 0.3
DEADLINE_SECONDS = 180

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


def test_the_page_fires_while_requests_fail_and_clears_when_they_stop() -> None:
    # Whatever the tests before this one did to the error ratio has to be over.
    eventually(lambda: the_page_is("inactive"), CATCH_UP_SECONDS)

    # What the page did and when, for the report: a table, column names first.
    timeline = [("When", PAGE), ("before the failures", "inactive")]
    notes: list[str] = []
    try:
        began = time.monotonic()
        failing = load.start("alert-failures", seconds=DEADLINE_SECONDS, fail_ratio=FAIL_RATIO)
        eventually(lambda: the_page_is("firing"), began + DEADLINE_SECONDS - time.monotonic())
        fired_after = time.monotonic() - began
        sent, failed = load.progress(failing)
        timeline.append(
            (
                f"{fired_after:.0f} s after a load began in which {FAIL_RATIO:.0%} of the"
                " requests ask to fail",
                "firing",
            )
        )
        notes.append(
            f"When the page was seen firing, {failed} of {sent} requests had failed,"
            f" and the error ratio was {error_ratios()}."
        )
        # The page has to be about these requests and nothing else.
        assert abs(failed / sent - FAIL_RATIO) < 0.05, f"{failed} of {sent} requests failed"

        # The failures stop: clients whose requests all succeed take over.
        load.stop(failing)
        stopped = time.monotonic()
        recovering = load.start("alert-recovery", seconds=DEADLINE_SECONDS)
        eventually(lambda: the_page_is("inactive"), stopped + DEADLINE_SECONDS - time.monotonic())
        cleared_after = time.monotonic() - stopped
        timeline.append((f"{cleared_after:.0f} s after the failures stopped", "inactive"))
        load.stop(recovering)
    finally:
        load.save_report("7-alerting", "Alerting", None, notes, timeline)


def the_page_is(state: str) -> None:
    """Fails unless the page is in this state: inactive, or firing."""
    rules = [rule for rule in chart_rules() if rule["name"] == PAGE]
    assert rules, f"Prometheus has no rule named {PAGE}"
    now = rules[0]["state"]
    assert now == state, f"{PAGE} is {now}, not {state}; the error ratio is {error_ratios()}"


def chart_rules() -> list[dict[str, Any]]:
    """The rules of the chart's group, each with its health and, for an alert, its state."""
    groups = get(PROMETHEUS, "/api/v1/rules")["data"]["groups"]
    return [rule for group in groups if group["name"] == RELEASE for rule in group["rules"]]


def error_ratios() -> str:
    """The recorded error ratios as of now, for a message: "30.1% over 30s, 15.2% over 2m"."""
    ratios = sorted(query_prometheus(ERROR_RATIOS), key=lambda series: series[1], reverse=True)
    return ", ".join(f"{value:.1%} over {labels['window']}" for labels, value in ratios)
