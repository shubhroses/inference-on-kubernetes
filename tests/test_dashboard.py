"""The Grafana dashboard in the chart has to match the series there are to draw.

The end-to-end suite runs every panel's query against a real Prometheus. This
is the quick check that needs no cluster: a metric renamed in the service or
in a recording rule, or misspelt in the dashboard, fails here. So does a
dashboard that draws another objective or other thresholds than the alerts
use: it is a file of its own and cannot read the chart's values.
"""

from tests.chart import PANELS, RULES, dashboard_queries, numbers, recorded_series, scraped_series
from tests.helpers import Service


async def test_the_dashboard_asks_only_for_series_the_service_exports(service: Service):
    asked_for = scraped_series(dashboard_queries())

    exported = {series.split("{")[0] for series in await service.metrics()}

    assert asked_for, "no query on the dashboard names a metric of the service"
    assert asked_for <= exported


def test_the_dashboard_asks_only_for_recorded_series_the_rules_record():
    asked_for = recorded_series(dashboard_queries())

    assert asked_for, "no query on the dashboard names a recorded series"
    assert asked_for <= recorded_series(RULES)


def test_the_dashboard_draws_the_error_budget_and_the_burn_rates_the_alerts_use():
    (objective,) = numbers("objective")
    budget = f"{1 - objective:g}"  # 0.01, as a query would have it
    fast_burn, slow_burn = numbers("burnRate")

    assert lines_on("Error ratio against the SLO") == {float(budget)}
    assert lines_on("Burn rate") == {1, slow_burn, fast_burn}
    # A burn rate is an error ratio divided by the budget.
    assert [query.split(" / ")[1] for query in queries_of("Burn rate")] == [budget]


def queries_of(panel: str) -> list[str]:
    return [target["expr"] for target in PANELS[panel]["targets"]]


def lines_on(panel: str) -> set[float]:
    """The values at which the panel draws a threshold line."""
    steps = PANELS[panel]["fieldConfig"]["defaults"]["thresholds"]["steps"]
    return {step["value"] for step in steps if step["value"] is not None}
