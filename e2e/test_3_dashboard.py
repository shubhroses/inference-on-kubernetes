"""3. Dashboard: Grafana serves the dashboard, and every panel on it has something to show."""

import math
from typing import Any

import pytest

from e2e.cluster import NAMESPACE, RELEASE, eventually, request
from e2e.monitoring import CATCH_UP_SECONDS, GRAFANA, get, query_through_grafana

# Each test may wait for Prometheus and Grafana up to CATCH_UP_SECONDS.
pytestmark = pytest.mark.timeout(900)

# The UID in chart/dashboards/inference.json, and the panels it has to have.
UID = "inference"
PANELS = [
    "Request rate by outcome",
    "Error ratio against the SLO",
    "Latency percentiles",
    "In-flight and queued against slots",
    "Replicas",
    "Burn rate",
]

# A panel's query has placeholders that Grafana fills in when it draws the
# panel. The first two are the dashboard's variables, here set to the release
# under test. Grafana picks the third to suit the time range on screen, never
# less than four scrape intervals of the data source, which is provisioned as
# scraping every 30 seconds; the test takes that shortest window.
PLACEHOLDERS = {
    "$namespace": NAMESPACE,
    "$release": RELEASE,
    "$__rate_interval": "2m",
}


@pytest.fixture(scope="module")
def dashboard() -> dict[str, Any]:
    """The dashboard as Grafana serves it. Grafana loads it some seconds after the chart goes in."""
    answer = eventually(lambda: get(GRAFANA, f"/api/dashboards/uid/{UID}"), CATCH_UP_SECONDS)
    return answer["dashboard"]


def test_grafana_serves_the_dashboard_by_uid(dashboard: dict[str, Any]) -> None:
    assert dashboard["title"] == "Inference service"
    assert [panel["title"] for panel in dashboard["panels"]] == PANELS


@pytest.mark.parametrize("title", PANELS)
def test_every_query_of_the_panel_returns_a_series(dashboard: dict[str, Any], title: str) -> None:
    panel = {panel["title"]: panel for panel in dashboard["panels"]}[title]
    datasource = panel["datasource"]["uid"]
    queries = [fill_in(target["expr"]) for target in panel["targets"]]
    assert queries, "the panel has no queries"

    def every_query_returns_a_series() -> None:
        # A request first, so that every window the queries look back over has
        # traffic in it, however long Prometheus took to get this far.
        status, body = request("POST", "/v1/infer", {"input": "draw me"})
        assert status == 200, body
        for query in queries:
            values = [value for _, value in query_through_grafana(datasource, query)]
            # A value that is not a number draws nothing, so it does not count.
            # A ratio is one of those for as long as there are no requests.
            numbers = [value for value in values if math.isfinite(value)]
            assert numbers, f"nothing to draw for: {query}"

    eventually(every_query_returns_a_series, CATCH_UP_SECONDS)


def fill_in(query: str) -> str:
    """Put a value in the place of each placeholder in a panel's query, as Grafana would."""
    for placeholder, value in PLACEHOLDERS.items():
        query = query.replace(placeholder, value)
    assert "$" not in query, f"a placeholder this test does not know: {query}"
    return query
