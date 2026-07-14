#!/usr/bin/env bash
# DB-Wifi Check -> auf Kubernetes deployen.
# Kein Image-Build und keine Registry noetig: der Container nutzt das
# oeffentliche python:3.12-slim und laedt den Code aus einer ConfigMap.
set -euo pipefail

CONTEXT="${KUBE_CONTEXT:-admin@docker-lab}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo ">> [1/4] Kontext waehlen: $CONTEXT"
kubectl config use-context "$CONTEXT"

echo ">> [2/4] Code als ConfigMap anlegen/aktualisieren"
kubectl create configmap db-wifi-check-src \
  --from-file=check.py="$DIR/app/check.py" \
  --dry-run=client -o yaml | kubectl apply -f -

echo ">> [3/4] Manifest anwenden + Neustart (damit neuer Code greift)"
kubectl apply -f "$DIR/k8s.yaml"
kubectl rollout restart deploy/db-wifi-check
kubectl rollout status deploy/db-wifi-check --timeout=120s

echo ">> [4/4] Port-Forward auf http://localhost:8080  (Strg+C beendet nur den Forward)"
kubectl port-forward svc/db-wifi-check 8080:8080
