"""The alert rules in the chart have to match the metrics the service exports.

`promtool test rules` (scripts/test-rules.sh) checks when the alerts fire, on
series written out by hand under the names the rules use. It would go on
passing if the service renamed a metric. These tests tie the rules to the
service itself.
"""

from app.metrics import BUCKETS
from tests.chart import RULES, numbers, scraped_series
from tests.helpers import Service


async def test_the_rules_ask_only_for_series_the_service_exports(service: Service):
    asked_for = scraped_series(RULES)

    exported = {series.split("{")[0] for series in await service.metrics()}

    assert asked_for, "no rule names a metric of the service"
    assert asked_for <= exported


def test_the_latency_alert_compares_with_a_bound_of_the_duration_histogram():
    """Between two bounds a percentile is a guess: Prometheus draws a straight line."""
    (threshold,) = numbers("p95Seconds")

    assert threshold in BUCKETS
