#!/usr/bin/env bash
# Local platform on kind: Argo Rollouts, Prometheus, External Secrets, Gateway API CRDs, metrics-server.
# Then renders examples/podinfo-kind.yaml with IntentOps and applies it. Needs docker, kind, kubectl, helm.
set -euo pipefail
cd "$(dirname "$0")/.."

kind get clusters | grep -qx intentops || kind create cluster --name intentops

kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/latest/download/standard-install.yaml
kubectl create namespace argo-rollouts --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argo-rollouts -f https://github.com/argoproj/argo-rollouts/releases/latest/download/install.yaml

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add external-secrets https://charts.external-secrets.io >/dev/null
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server >/dev/null
helm repo update >/dev/null
helm upgrade --install kps prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
  --set fullnameOverride=kps \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false --wait --timeout 10m
helm upgrade --install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace --wait
helm upgrade --install metrics-server metrics-server/metrics-server -n kube-system --set 'args={--kubelet-insecure-tls}' --wait

kubectl apply -f deploy/kind/platform.yaml
python -m intentops render examples/podinfo-kind.yaml --cloud kind -o out
kubectl apply -k out/kind/podinfo
kubectl apply -f deploy/kind/loadgen.yaml
kubectl -n demo rollout status deploy/loadgen --timeout=120s

cat <<MSG

podinfo is running from one intent file. Try a release:
  make release-good   # canary 10% -> 50% -> 100%, analysis passes
  make release-bad    # canary returns random 500s -> AnalysisRun fails -> automatic rollback
  kubectl -n demo get rollout,analysisrun -w
MSG
