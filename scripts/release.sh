#!/usr/bin/env bash
# Trigger a canary on the kind lab. good: new version. bad: same version with podinfo's random-error mode on.
set -euo pipefail
case "${1:-}" in
  good) patch='[{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"ghcr.io/stefanprodan/podinfo:6.7.0"}]' ;;
  bad)  patch='[{"op":"add","path":"/spec/template/spec/containers/0/env","value":[{"name":"PODINFO_RANDOM_ERROR","value":"true"}]}]' ;;
  *) echo "usage: $0 good|bad" >&2; exit 1 ;;
esac
kubectl -n demo patch rollout podinfo --type json -p "$patch"
echo "watch it: kubectl -n demo get rollout podinfo -w"
