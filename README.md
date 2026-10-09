# inference-on-kubernetes

An inference-style HTTP API that will be run on Kubernetes the way a production service is run: a Helm chart, Prometheus error-budget alerts, a Grafana dashboard and KEDA autoscaling on queue depth, with every operational claim backed by a test that CI executes on a kind cluster.

The "model" is a stand-in that holds a slot for a configurable time and returns a small JSON result; there is no real model and no GPU here.

Under construction: so far there is the service (`app/`) with its unit tests, its container image (`Dockerfile`), a Helm chart (`chart/`), and a smoke test (`e2e/`) that GitHub Actions runs against the chart installed on a kind cluster. Monitoring, alerts, autoscaling, the rollout tests and the full README are still to come.
