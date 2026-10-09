#!/usr/bin/env bash
# Installs cluster tools at the versions pinned in versions.env. CI uses this
# and not the copies a runner comes with, because those change from one week
# to the next.
#
# Usage: scripts/install-tools.sh DIRECTORY TOOL...
# TOOL is kind, kubectl or helm. The downloads are the linux/amd64 builds,
# which is what the runners are. Each one is checked against the SHA-256
# recorded in versions.env before it is installed into DIRECTORY.
set -euo pipefail

usage="usage: scripts/install-tools.sh DIRECTORY TOOL..."
directory=${1:?$usage}
shift
[ "$#" -gt 0 ] || { echo "$usage" >&2; exit 2; }

# shellcheck source-path=SCRIPTDIR source=../versions.env
. "$(dirname "$0")/../versions.env"

downloaded=$(mktemp)
unpacked=$(mktemp -d)
trap 'rm -rf "$downloaded" "$unpacked"' EXIT

# Fetches URL into $downloaded and stops unless its SHA-256 is the expected one.
download() {
  local url=$1 sha256=$2
  curl --fail --silent --show-error --location --retry 3 --output "$downloaded" "$url"
  if ! echo "$sha256  $downloaded" | sha256sum --check --status; then
    echo "FAILED: $url does not have the SHA-256 recorded in versions.env" >&2
    exit 1
  fi
}

mkdir -p "$directory"
for tool in "$@"; do
  case "$tool" in
    kind)
      download "https://github.com/kubernetes-sigs/kind/releases/download/v$KIND_VERSION/kind-linux-amd64" "$KIND_SHA256"
      install "$downloaded" "$directory/kind"
      ;;
    kubectl)
      download "https://dl.k8s.io/release/v$KUBERNETES_VERSION/bin/linux/amd64/kubectl" "$KUBECTL_SHA256"
      install "$downloaded" "$directory/kubectl"
      ;;
    helm)
      # Helm is published as an archive with the binary inside.
      download "https://get.helm.sh/helm-v$HELM_VERSION-linux-amd64.tar.gz" "$HELM_SHA256"
      tar --extract --gzip --file "$downloaded" --directory "$unpacked" linux-amd64/helm
      install "$unpacked/linux-amd64/helm" "$directory/helm"
      ;;
    *)
      echo "unknown tool: $tool" >&2
      echo "$usage" >&2
      exit 2
      ;;
  esac
  echo "installed $tool in $directory"
done
