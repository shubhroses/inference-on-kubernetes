#!/usr/bin/env bash
# Checks the chart's alert rules, and the query its autoscaler scales on,
# without a cluster. It renders both twice, with the chart's defaults and with
# what the kind cluster changes, has promtool check that Prometheus would
# accept them, and runs the unit tests in tests/rules/ against them.
#
# Usage: scripts/test-rules.sh
# It needs helm and docker. promtool comes from the image of the Prometheus
# release the test cluster runs, pinned in versions.env.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source-path=SCRIPTDIR source=../versions.env
. ./versions.env

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# render VALUES RULES
# Writes the rules the chart renders with the values file VALUES to the file
# RULES, in the form Prometheus itself reads. A PrometheusRule object holds
# exactly such a file under its key `spec`: awk keeps the lines after that one
# and takes off the two spaces they are indented by. The release is named
# `inference` and goes into the namespace `inference`, as on the kind cluster:
# the rules select their series by those two names, and the series in the
# tests carry them.
render() {
  local values=$1 rules=$2
  helm template inference chart --namespace inference --values "$values" \
    --set-string image.tag=unused --show-only templates/prometheusrule.yaml |
    awk 'inside { sub(/^  /, ""); print } /^spec:$/ { inside = 1 }' > "$work/$rules"
}

# render_query VALUES RULES
# Writes the query of the chart's ScaledObject, as rendered with the values
# file VALUES, to the file RULES as a rule that records it under the name
# `autoscaler:requests`. The query is not a rule: KEDA puts it to Prometheus
# itself. But rules are what promtool can check and can evaluate over made-up
# series, so the query is dressed as one. awk keeps the lines of the block
# that follows `query:` and gives them the indentation the rule file needs.
render_query() {
  local values=$1 rules=$2
  printf 'groups:\n  - name: autoscaler\n    rules:\n      - record: autoscaler:requests\n        expr: |\n' \
    > "$work/$rules"
  helm template inference chart --namespace inference --values "$values" \
    --set-string image.tag=unused --show-only templates/scaledobject.yaml |
    awk '/^ *threshold:/ { inside = 0 }
         inside { sub(/^ */, "          "); print }
         /^ *query: \|-$/ { inside = 1 }' >> "$work/$rules"
}

# As the user who runs this script, so that the container can read the files
# that user has just written.
promtool() {
  docker run --rm --user "$(id -u):$(id -g)" --volume "$work:/rules:ro" --workdir /rules \
    --entrypoint promtool "$PROMETHEUS_IMAGE" "$@"
}

render chart/values.yaml production.rules.yaml
render chart/values-kind.yaml kind.rules.yaml
render_query chart/values.yaml autoscaling.rules.yaml
render_query chart/values-kind.yaml autoscaling-kind.rules.yaml
cp tests/rules/*.test.yaml "$work"

# A rule that Prometheus would refuse, or two rules that record the same
# series, fails here. The kind cluster would take the object all the same: the
# Prometheus Operator's admission webhook, which could refuse it, is switched
# off there. So is KEDA's, and an autoscaler's query that Prometheus cannot
# parse would show on the cluster only as an autoscaler that never becomes
# active. Here it fails by name.
promtool check rules --lint-fatal production.rules.yaml kind.rules.yaml \
  autoscaling.rules.yaml autoscaling-kind.rules.yaml

promtool test rules production.test.yaml kind.test.yaml autoscaling.test.yaml
