"""The autoscaler's settings in the chart have to fit the service they scale.

The end-to-end suite watches the autoscaler at work on a cluster. These are
the quick checks that need none: a metric renamed in the service would leave
the autoscaler asking Prometheus for a series that is no longer there.
"""

from tests.chart import CHART, numbers, scraped_series
from tests.helpers import Service

SCALED_OBJECT = (CHART / "templates" / "scaledobject.yaml").read_text()


async def test_the_autoscaler_asks_only_for_series_the_service_exports(service: Service):
    asked_for = scraped_series(SCALED_OBJECT)

    exported = {series.split("{")[0] for series in await service.metrics()}

    assert asked_for, "the ScaledObject names no metric of the service"
    assert asked_for <= exported


def test_the_autoscaler_aims_at_fewer_requests_per_pod_than_a_pod_has_slots():
    """With slots to spare at the target, pods are added before requests have to wait."""
    (target,) = numbers("targetRequestsPerPod")
    (slots,) = numbers("slots")

    assert target < slots
