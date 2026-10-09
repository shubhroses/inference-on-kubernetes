#!/usr/bin/env bash
# Checks the cluster-safety rule: make and the end-to-end tests must refuse to
# act unless the kubeconfig is the repository's own and its current context is
# the kind cluster that `make up` created.
#
# The script writes two kubeconfigs and expects every command that would use
# kubectl or helm to stop with a refusal:
#   - one where the Makefile looks for it, whose current context is not the
#     kind cluster's;
#   - a decoy somewhere else, whose current context has exactly the right name.
# Neither file names a cluster, so a command that got past a broken guard would
# still have nothing to connect to.
#
# Usage: scripts/test-cluster-guard.sh
# Run it before `make up` or after `make down`: it will not overwrite a
# kubeconfig that is already there.
set -euo pipefail

cd "$(dirname "$0")/.."
kubeconfig=$PWD/.kube/config

fail() {
  echo "FAILED: $1" >&2
  exit 1
}

[ ! -e "$kubeconfig" ] || fail "$kubeconfig exists. Run 'make down' first."

decoy=$(mktemp)
output=$(mktemp)
cleanup() {
  rm -f "$kubeconfig" "$decoy" "$output"
  rmdir .kube 2>/dev/null || true
}
trap cleanup EXIT

# expect_refusal MESSAGE COMMAND...
# Passes if the command stops and prints a line matching MESSAGE, which names
# the guard that has to refuse and its reason. Running to the end, failing for
# some other reason, or being stopped by a different guard does not count.
expect_refusal() {
  local message=$1
  shift
  if "$@" >"$output" 2>&1; then
    cat "$output" >&2
    fail "'$*' ran, although it should have refused"
  fi
  if ! grep --quiet "$message" "$output"; then
    cat "$output" >&2
    fail "'$*' did not stop with: $message"
  fi
  echo "ok: '$*' refuses"
}

# What the Makefile's check-context prints, and what e2e/conftest.py prints.
make_refuses="Refusing to continue: the current context in .* is"
tests_refuse="Refusing to run the end-to-end tests:"

echo "Before the cluster is created, when there is no kubeconfig:"
expect_refusal "$make_refuses ''" make e2e

mkdir -p .kube
printf 'apiVersion: v1\nkind: Config\ncurrent-context: %s\n' somewhere-else >"$kubeconfig"
printf 'apiVersion: v1\nkind: Config\ncurrent-context: %s\n' kind-inference-on-kubernetes >"$decoy"

echo "When the current context is not the kind cluster:"
expect_refusal "$make_refuses 'somewhere-else'" make nodes
expect_refusal "$make_refuses 'somewhere-else'" make monitoring
expect_refusal "$make_refuses 'somewhere-else'" make deploy
expect_refusal "$make_refuses 'somewhere-else'" make e2e
expect_refusal "$make_refuses 'somewhere-else'" make diagnostics

echo "When another kubeconfig is named on the command line (make must still read its own):"
expect_refusal "$make_refuses 'somewhere-else'" make e2e KUBECONFIG="$decoy"

echo "When the tests are started directly, without make:"
expect_refusal "$tests_refuse the current context is 'somewhere-else'" env KUBECONFIG="$kubeconfig" pytest e2e
expect_refusal "$tests_refuse KUBECONFIG is '$decoy'" env KUBECONFIG="$decoy" pytest e2e
expect_refusal "$tests_refuse KUBECONFIG is None" env -u KUBECONFIG pytest e2e
