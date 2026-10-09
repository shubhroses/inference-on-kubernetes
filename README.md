# inference-on-kubernetes

An inference-style HTTP API that will be run on Kubernetes the way a production service is run: a Helm chart, Prometheus error-budget alerts, a Grafana dashboard and KEDA autoscaling on queue depth, with every operational claim backed by a test that CI executes on a kind cluster.

The "model" is a stand-in that holds a slot for a configurable time and returns a small JSON result; there is no real model and no GPU here.

Under construction: so far there is the service (`app/`) with its unit tests, its container image (`Dockerfile`), and a Helm chart (`chart/`) that brings a ServiceMonitor, a Grafana dashboard and a PodDisruptionBudget with it. GitHub Actions installs the chart on a kind cluster next to Prometheus and Grafana and runs the end-to-end tests in `e2e/`: a smoke test, a check that Prometheus scrapes every pod, a check that every panel of the dashboard has data to show, and two tests that put load on the service from inside the cluster (`loadgen/`) and assert that not one request fails while a rolling update replaces every pod, or while the node that holds every pod is drained. Alerts, autoscaling and the full README are still to come.
