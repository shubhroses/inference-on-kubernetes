"""How the tests ask Prometheus and Grafana.

Both are Services with no address outside the cluster. The Kubernetes API
server passes a request on to a Service when the request's path names one, and
`kubectl get --raw` sends such a request. So the tests need no port-forward
running beside them and no credentials other than the kubeconfig.
"""

import json
from typing import Any
from urllib.parse import urlencode

from e2e.cluster import kubectl

# What `make up` installs: the kube-prometheus-stack chart, released under its
# own name in this namespace. The chart names its Services after the release.
NAMESPACE = "monitoring"
PROMETHEUS = "kube-prometheus-stack-prometheus:9090"
GRAFANA = "kube-prometheus-stack-grafana:80"

# How long a test gives Prometheus and Grafana to catch up with the cluster.
# Prometheus starts scraping a new ServiceMonitor about a minute after it is
# created and sees every later change at its next scrape; Grafana loads a new
# dashboard within seconds. Only a test that is about to fail waits this long.
CATCH_UP_SECONDS = 300

# The labels of a series, and its value at the moment of the query.
Series = tuple[dict[str, str], float]


def get(service: str, path: str, **parameters: str) -> Any:
    """GET a path from a Service in the monitoring namespace and decode the JSON it answers with.

    kubectl fails, and with it the assertion in `kubectl`, if there is no answer
    or the answer is an error: a pod that is not ready yet, a dashboard Grafana
    has not loaded yet.
    """
    url = f"/api/v1/namespaces/{NAMESPACE}/services/{service}/proxy{path}"
    if parameters:
        url += "?" + urlencode(parameters)
    return json.loads(kubectl("get", "--raw", url))


def query_prometheus(expression: str) -> list[Series]:
    """Evaluate a PromQL expression as of now. Returns every series of the result."""
    return series_of(get(PROMETHEUS, "/api/v1/query", query=expression))


def query_through_grafana(datasource: str, expression: str) -> list[Series]:
    """The same question, put to Grafana, which passes it on to its data source with that UID."""
    path = f"/api/datasources/proxy/uid/{datasource}/api/v1/query"
    return series_of(get(GRAFANA, path, query=expression))


def series_of(answer: dict[str, Any]) -> list[Series]:
    """Unpack the answer of Prometheus's query API. A value arrives as [time, "number"]."""
    data = answer["data"]
    assert data["resultType"] == "vector", f"expected series, got a {data['resultType']}"
    return [(series["metric"], float(series["value"][1])) for series in data["result"]]
