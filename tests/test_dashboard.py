"""The Grafana dashboard in the chart has to match the metrics the service exports.

The end-to-end suite runs every panel's query against a real Prometheus. This
is the quick check that needs no cluster: a metric renamed in the service, or
misspelt in the dashboard, fails here.
"""

import json
import re
from pathlib import Path

from tests.helpers import Service

DASHBOARD = Path(__file__).parent.parent / "chart" / "dashboards" / "inference.json"


async def test_the_dashboard_asks_only_for_series_the_service_exports(service: Service):
    dashboard = json.loads(DASHBOARD.read_text())
    queries = [target["expr"] for panel in dashboard["panels"] for target in panel["targets"]]
    queries += [variable["query"] for variable in dashboard["templating"]["list"]]
    asked_for = set(re.findall(r"inference_\w+", " ".join(queries)))

    exported = {series.split("{")[0] for series in await service.metrics()}

    assert asked_for, "no query on the dashboard names a metric of the service"
    assert asked_for <= exported
