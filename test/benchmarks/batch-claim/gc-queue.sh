#!/usr/bin/env bash
# Prints kube-controller-manager's garbage-collector work-queue metrics from the kind node, using
# the node's admin credentials. Usage: gc-queue.sh [node-container]
set -euo pipefail
NODE=${1:-batch-bench-control-plane}
docker exec -i "$NODE" bash -s <<'EOF'
set -euo pipefail
dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
grep client-certificate-data /etc/kubernetes/admin.conf | awk '{print $2}' | base64 -d > "$dir/c.crt"
grep client-key-data /etc/kubernetes/admin.conf | awk '{print $2}' | base64 -d > "$dir/c.key"
curl -sk --cert "$dir/c.crt" --key "$dir/c.key" https://127.0.0.1:10257/metrics |
  grep -E '^workqueue_(depth|unfinished_work_seconds|longest_running_processor_seconds|adds_total)\{name="garbage_collector'
EOF
