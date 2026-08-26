#!/bin/bash
# run-stress-ab.sh -- bootstrap and run the local kind stress-test harness
# against ONE git ref, in an isolated worktree, without touching the
# caller's actual working tree or branch.
#
# Why this exists: dev/tools/stress-test-kind and dev/tools/stress-report
# (the convenience wrappers around `go run ./test/stress` +
# generate_report.py) may not exist yet on every ref you want to test --
# they were still uncommitted work-in-progress as of this writing. This
# script makes that irrelevant: it copies its own bundled copies of those
# tools into the target worktree, and applies the kind/kubeadm
# compatibility patch (see ../create-kind-cluster-stress-support.patch and
# ../../agent-docs/benchmark-testing-learnings.md section 4, lesson 11)
# unconditionally. If those tools have since landed for real in
# dev/tools/, this script's copies just overwrite them with themselves
# (harmless) -- check upstream first if you want to avoid the redundant
# copy.
#
# Usage:
#   ./run-stress-ab.sh <git-ref> <worktree-path> <output-dir> [extra go run ./test/stress flags...]
#
# Example -- reproduce the exact comparison from benchmark-testing-learnings.md:
#   ./run-stress-ab.sh 2fd412d55ecae90861a101a5424a75473de97c36 /tmp/wt-baseline /tmp/out-baseline
#   ./run-stress-ab.sh <pr-branch-tip-sha>                      /tmp/wt-pr       /tmp/out-pr
#   python3 analyze_ab.py /tmp/out-baseline /tmp/out-pr
#
# Runs sequentially by design -- each invocation creates/recreates a kind
# cluster named "agent-sandbox-stress"; run one to completion before
# starting the next so they don't fight over the same cluster name.

set -o errexit
set -o nounset
set -o pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <git-ref> <worktree-path> <output-dir> [extra test/stress flags...]" >&2
  exit 1
fi

REF="$1"; WORKTREE="$2"; OUTPUT_DIR="$3"; shift 3
EXTRA_ARGS=("$@")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel)"

echo "Creating worktree at ${WORKTREE} for ref ${REF}..."
git -C "$REPO_ROOT" worktree add "$WORKTREE" --detach "$REF"

cd "$WORKTREE"

echo "Applying kind/kubeadm compatibility + --workers support..."
git apply --reject --whitespace=nowarn "$SCRIPT_DIR/../create-kind-cluster-stress-support.patch" \
  || echo "  (patch didn't apply cleanly -- probably already present on this ref; continuing)"

echo "Installing stress-test-kind / stress-report tooling..."
cp "$SCRIPT_DIR/tools/stress-test-kind" dev/tools/stress-test-kind
cp "$SCRIPT_DIR/tools/stress-report" dev/tools/stress-report
chmod +x dev/tools/stress-test-kind dev/tools/stress-report

if ! grep -q "^stress-kind:" Makefile; then
  echo "Appending stress Makefile targets..."
  printf '\n' >> Makefile
  cat "$SCRIPT_DIR/tools/Makefile.stress-block.mk" >> Makefile
fi

echo "Running: make stress-venv stress-kind stress-report ..."
STRESS_KIND_WORKERS="${STRESS_KIND_WORKERS:-2}" \
STRESS_RECREATE_CLUSTER=true \
STRESS_OUTPUT_DIR="$OUTPUT_DIR" \
STRESS_REPORT_DIR="$OUTPUT_DIR/report" \
STRESS_EXTRA_ARGS="${EXTRA_ARGS[*]:-}" \
  make stress-venv stress-kind stress-report

echo
echo "Done. Report: $OUTPUT_DIR/report/index.html"
echo "Raw metrics (for analyze_ab.py / aggregate_repeats.py): $OUTPUT_DIR/metrics.jsonl.gz, $OUTPUT_DIR/summary.json"
