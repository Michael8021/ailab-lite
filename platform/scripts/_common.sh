#!/usr/bin/env bash
# Shared by the k3s helper scripts:  . "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
# Finds a kubeconfig and a kubectl without assuming whose machine this is.

find_kubeconfig() {
  local f
  for f in "${KUBECONFIG:-}" "$HOME/.kube/config" /etc/rancher/k3s/k3s.yaml; do
    if [ -n "$f" ] && [ -r "$f" ]; then echo "$f"; return 0; fi
  done
  return 1
}

if KUBECONFIG=$(find_kubeconfig); then
  export KUBECONFIG
else
  echo "no readable kubeconfig (tried \$KUBECONFIG, ~/.kube/config, /etc/rancher/k3s/k3s.yaml); is k3s installed?" >&2
  return 1 2>/dev/null || exit 1
fi

# k3s ships a kubectl that matches its server; a separately installed one can be several versions off.
if [ -z "${KUBECTL:-}" ]; then
  if command -v k3s >/dev/null 2>&1; then KUBECTL="k3s kubectl"; else KUBECTL=kubectl; fi
fi
export KUBECTL
