#!/usr/bin/env bash
# Runs the image the way the cluster will run it: as the image's own non-root
# user, with a read-only root filesystem and without capabilities. Then checks
# that it serves requests and that it stops cleanly on SIGTERM.
#
# Usage: scripts/test-image.sh IMAGE GIT_SHA
# GIT_SHA is the commit the image was built from, as given to
# `docker build --build-arg GIT_SHA=...`.
set -euo pipefail

usage="usage: scripts/test-image.sh IMAGE GIT_SHA"
image=${1:?$usage}
git_sha=${2:?$usage}
container="test-image-$$"

fail() {
  echo "FAILED: $1" >&2
  exit 1
}

# On the way out, whatever the result: show the service's log if a check
# failed, and remove the container.
cleanup() {
  if [ "$?" -ne 0 ]; then
    echo "--- log of the container" >&2
    docker logs "$container" >&2 || true
  fi
  docker rm --force "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --detach --name "$container" \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --publish 127.0.0.1::8000 \
  "$image" >/dev/null

# Docker picked a free port on the loopback interface. This prints 127.0.0.1:PORT.
address=$(docker port "$container" 8000/tcp) || fail "the container exited as soon as it started"

get() {
  curl --silent --show-error --fail --max-time 5 "http://$address$1"
}

# /readyz answers 503 until the simulated model load is over, 2 seconds by default.
ready=false
for _ in $(seq 60); do
  if get /readyz >/dev/null 2>&1; then
    ready=true
    break
  fi
  [ "$(docker inspect --format '{{.State.Running}}' "$container")" = true ] \
    || fail "the container exited instead of serving"
  sleep 1
done
[ "$ready" = true ] || fail "/readyz did not answer 200 within 60 seconds"
echo "ok: /readyz answers 200 on a read-only root filesystem"

get /healthz >/dev/null || fail "/healthz did not answer 200"
echo "ok: /healthz answers 200"

response=$(curl --silent --show-error --fail --max-time 10 \
  --header 'Content-Type: application/json' --data '{"input": "hello"}' \
  "http://$address/v1/infer") || fail "POST /v1/infer did not answer 200"
[[ $response == *'"input_chars":5'* ]] || fail "unexpected answer from /v1/infer: $response"
echo "ok: /v1/infer answers $response"

get /metrics | grep --fixed-strings "inference_build_info{git_sha=\"$git_sha\"} 1.0" \
  || fail "/metrics does not report build $git_sha"
echo "ok: the build-info metric reports the commit"

# The owner of /proc/1 is the user that process 1, the service, runs as.
user_id=$(docker exec "$container" stat --format %u /proc/1)
[ "$user_id" != 0 ] || fail "the service runs as root"
echo "ok: the service runs as user $user_id"

# `docker stop` sends SIGTERM and, if the container is still there 30 seconds
# later, SIGKILL: the time the chart gives a pod. The service spends five of
# them on its drain, keeping open connections alive while it tells them to
# close. Status 0 means that it drained and exited by itself; 137 would mean
# that it had to be killed. (-t is --timeout, which older Dockers call --time.)
docker stop -t 30 "$container" >/dev/null
exit_status=$(docker inspect --format '{{.State.ExitCode}}' "$container")
[ "$exit_status" = 0 ] || fail "exit status $exit_status after SIGTERM, expected 0"
echo "ok: SIGTERM ends the service with exit status 0"
