#!/usr/bin/env bash
# Tear down the k3s platform namespace (keeps the OpenSandbox server and images).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
$KUBECTL delete namespace platform --ignore-not-found --wait=true
