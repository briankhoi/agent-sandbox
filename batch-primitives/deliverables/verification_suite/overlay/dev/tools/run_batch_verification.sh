#!/usr/bin/env bash
# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Master Batch Verification and Chaos Pipeline Runner.
#
# Usage:
#   ./dev/tools/run_batch_verification.sh [--quick] [--skip-external] [--perf-only]
#       [--python /path/to/python]
#
#   --perf-only runs only the harness self-check (Stage 0) and the performance stages (1b, 10a,
#   10b), so a branch that is red on correctness stages still gets a clean performance verdict.
#
# Environment overrides:
#   PYTHON_BIN   Python interpreter with pytest, hypothesis, mypy, kubernetes_asyncio installed.
#   PERF_STAGE_TIMEOUT  Seconds before Stage 10a or 10b is killed and fails (default 1800).
#   TLA_DIR      Path to TLA+ specification directory containing runall_subsystems.sh.
#   BFS_DIR      Path to handoff/work directory containing BFS lock/cancel model scripts.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DEFAULT_NOTES_WORK="$(cd "${REPO_ROOT}/.." && pwd)/agent-sandbox-notes/batch-primitives/handoff/work"

QUICK_MODE=0
SKIP_EXTERNAL=0
PERF_ONLY=0
EXPLICIT_TLA=0
EXPLICIT_BFS=0
if [[ -n "${TLA_DIR:-}" ]]; then
  EXPLICIT_TLA=1
fi
if [[ -n "${BFS_DIR:-}" ]]; then
  EXPLICIT_BFS=1
fi
PYTHON_BIN="${PYTHON_BIN:-python3}"
TLA_DIR="${TLA_DIR:-${DEFAULT_NOTES_WORK}/tla}"
BFS_DIR="${BFS_DIR:-${DEFAULT_NOTES_WORK}}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick)
      QUICK_MODE=1
      shift
      ;;
    --skip-external)
      SKIP_EXTERNAL=1
      shift
      ;;
    --perf-only)
      PERF_ONLY=1
      shift
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --tla-dir)
      TLA_DIR="$2"
      EXPLICIT_TLA=1
      shift 2
      ;;
    --bfs-dir)
      BFS_DIR="$2"
      EXPLICIT_BFS=1
      shift 2
      ;;
    *)
      echo "Unknown flag: $1" >&2
      exit 2
      ;;
  esac
done

SDK_SRC_DIR="${REPO_ROOT}/clients/python/agentic-sandbox-client"
SDK_UNIT_DIR="${SDK_SRC_DIR}/k8s_agent_sandbox/test/unit"
SDK_CFG="${SDK_SRC_DIR}/pyproject.toml"
RL_DIR="${REPO_ROOT}/examples/agent-sandbox-rl"

# Force module and mypy resolution against REPO_ROOT rather than any editable install in another worktree.
if [[ -d "${RL_DIR}" ]]; then
  export PYTHONPATH="${SDK_SRC_DIR}:${RL_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
  export MYPYPATH="${SDK_SRC_DIR}:${RL_DIR}${MYPYPATH:+:${MYPYPATH}}"
else
  export PYTHONPATH="${SDK_SRC_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
  export MYPYPATH="${SDK_SRC_DIR}${MYPYPATH:+:${MYPYPATH}}"
fi

CACHE_ROOT="${TMPDIR:-/tmp}/batch_verify_cache_${UID:-0}"
export HYPOTHESIS_STORAGE_DIRECTORY="${CACHE_ROOT}/hypothesis"
PYTEST_CACHE_DIR="${CACHE_ROOT}/pytest"
MYPY_CACHE_SDK="${CACHE_ROOT}/mypy_sdk"
MYPY_CACHE_STRICT="${CACHE_ROOT}/mypy_strict"
MYPY_CACHE_RL="${CACHE_ROOT}/mypy_rl"
mkdir -p "${HYPOTHESIS_STORAGE_DIRECTORY}" "${PYTEST_CACHE_DIR}" "${MYPY_CACHE_SDK}" "${MYPY_CACHE_STRICT}" "${MYPY_CACHE_RL}"

LOG_DIR="${TMPDIR:-/tmp}/batch_verify_logs.$$"
mkdir -p "${LOG_DIR}"
trap 'rm -rf "${LOG_DIR}"' EXIT

declare -a STAGE_NAMES=()
declare -a STAGE_STATUS=()
declare -a STAGE_SECS=()
declare -a STAGE_NOTES=()
OVERALL_RC=0

run_stage() {
  local name="$1"
  shift
  local slug
  slug="$(echo "${name}" | tr ' /:()' '_____' | tr -cd '[:alnum:]_-')"
  local logfile="${LOG_DIR}/${slug}.log"
  local t0 t1 elapsed rc note

  printf ">>> Running Stage: %s ...\n" "${name}"
  t0="$(date +%s)"
  "$@" >"${logfile}" 2>&1
  rc=$?
  t1="$(date +%s)"
  elapsed=$((t1 - t0))

  STAGE_NAMES+=("${name}")
  STAGE_SECS+=("${elapsed}s")

  if [[ ${rc} -eq 0 ]]; then
    STAGE_STATUS+=("PASS")
    note="$(grep -vE '^(-|=|$)' "${logfile}" | tail -n 1 | sed 's/^ *//' | cut -c1-68)"
    STAGE_NOTES+=("${note}")
    printf "    [PASS] (%ss)\n" "${elapsed}"
  else
    STAGE_STATUS+=("FAIL")
    OVERALL_RC=1
    note="$(grep -E '(Summary:|failed|FAILED|Found [0-9]+ error|VIOLATION|ERROR:)' "${logfile}" | tail -n 1 | sed 's/^ *//' | cut -c1-68)"
    if [[ -z "${note}" ]]; then
      note="exit code ${rc}"
    fi
    STAGE_NOTES+=("${note}")
    printf "    [FAIL] (%ss) -> %s\n" "${elapsed}" "${note}"
  fi
}

# Stage 0: Workspace Import Binding & Harness Mypy Self-Check
run_stage "0. Harness Self-Check (Import & Mypy)" \
  bash -c "
    set -euo pipefail
    '${PYTHON_BIN}' -c '
import os, sys, k8s_agent_sandbox
repo = os.path.realpath(sys.argv[1])
mod = os.path.realpath(k8s_agent_sandbox.__file__)
assert mod.startswith(repo + os.sep), f\"ERROR: k8s_agent_sandbox resolved to {mod}, outside {repo}\"
print(f\"Verified import root: {mod}\")
' '${REPO_ROOT}'
    '${PYTHON_BIN}' -m mypy --cache-dir '${MYPY_CACHE_SDK}' --config-file '${SDK_CFG}' \
      '${REPO_ROOT}/dev/tools/verify_batch_static.py' \
      '${SDK_UNIT_DIR}/batch_invariant_oracle.py' \
      '${SDK_UNIT_DIR}/test_batch_fault_injection.py' \
      '${SDK_UNIT_DIR}/test_batch_state_fuzz.py' \
      '${SDK_UNIT_DIR}/test_batch_differential.py' \
      '${SDK_UNIT_DIR}/test_batch_concurrency_stress.py' \
      '${SDK_UNIT_DIR}/test_batch_performance_invariants.py'
    if [[ -f '${RL_DIR}/tests/test_batch_fault_injection_rl.py' ]]; then
      '${PYTHON_BIN}' -m mypy --cache-dir '${MYPY_CACHE_RL}' --ignore-missing-imports --follow-imports=silent '${RL_DIR}/tests/test_batch_fault_injection_rl.py'
    fi
    if [[ -f '${RL_DIR}/tests/test_batch_performance_rl.py' ]]; then
      '${PYTHON_BIN}' -m mypy --cache-dir '${MYPY_CACHE_RL}' --ignore-missing-imports --follow-imports=silent '${RL_DIR}/tests/test_batch_performance_rl.py'
    fi
    if [[ ${SKIP_EXTERNAL} -eq 0 && -f '${BFS_DIR}/verify_delete_lifecycle_model.py' && -f '${BFS_DIR}/verify_cancel5_exercise.py' ]]; then
      '${PYTHON_BIN}' -m mypy --cache-dir '${MYPY_CACHE_RL}' --ignore-missing-imports \
        '${BFS_DIR}/verify_delete_lifecycle_model.py' \
        '${BFS_DIR}/verify_cancel5_exercise.py'
    fi
  "

# Stage 1: Static AST & Call-Graph Auditor
if [[ ${PERF_ONLY} -eq 0 ]]; then
  run_stage "1. Static AST & Call-Graph Auditor" \
    "${PYTHON_BIN}" "${REPO_ROOT}/dev/tools/verify_batch_static.py" --repo-root "${REPO_ROOT}" --fail-on LOW
fi

# Stage 1b: Hot-path complexity check on its own, so a new HOT_PATH finding (or a fix that
# removes one) changes a stage result even while Stage 1 is red on other checks.
run_stage "1b. Static Hot-Path Complexity" \
  "${PYTHON_BIN}" "${REPO_ROOT}/dev/tools/verify_batch_static.py" --repo-root "${REPO_ROOT}" \
  --check HOT_PATH_COMPLEXITY --fail-on LOW

# Stages 2-9 check correctness, not performance; --perf-only skips them.
if [[ ${PERF_ONLY} -eq 0 ]]; then

# Stage 2: Modular TLA+ Formal Suite (deadlock checking enabled)
if [[ ${QUICK_MODE} -eq 1 || ${SKIP_EXTERNAL} -eq 1 ]]; then
  STAGE_NAMES+=("2. Modular TLA+ Formal Suite")
  STAGE_STATUS+=("SKIP")
  STAGE_SECS+=("0s")
  STAGE_NOTES+=("skipped (--quick or --skip-external)")
elif [[ -x "${TLA_DIR}/runall_subsystems.sh" ]]; then
  run_stage "2. Modular TLA+ Formal Suite" \
    bash -c "cd '${TLA_DIR}' && ./runall_subsystems.sh"
elif [[ ${EXPLICIT_TLA} -eq 0 ]]; then
  STAGE_NAMES+=("2. Modular TLA+ Formal Suite")
  STAGE_STATUS+=("SKIP")
  STAGE_SECS+=("0s")
  STAGE_NOTES+=("skipped (external TLA_DIR not present)")
else
  run_stage "2. Modular TLA+ Formal Suite" \
    bash -c "echo 'ERROR: TLA_DIR (${TLA_DIR}/runall_subsystems.sh) missing' >&2; exit 1"
fi

# Stage 3: Exhaustive BFS Lock & Cancel Models
if [[ ${SKIP_EXTERNAL} -eq 1 ]]; then
  STAGE_NAMES+=("3. BFS Lock & Cancel State Models")
  STAGE_STATUS+=("SKIP")
  STAGE_SECS+=("0s")
  STAGE_NOTES+=("skipped (--skip-external)")
elif [[ -f "${BFS_DIR}/verify_delete_lifecycle_model.py" && -f "${BFS_DIR}/verify_cancel5_exercise.py" ]]; then
  run_stage "3. BFS Lock & Cancel State Models" \
    bash -c "'${PYTHON_BIN}' '${BFS_DIR}/verify_delete_lifecycle_model.py' && '${PYTHON_BIN}' '${BFS_DIR}/verify_cancel5_exercise.py'"
elif [[ ${EXPLICIT_BFS} -eq 0 ]]; then
  STAGE_NAMES+=("3. BFS Lock & Cancel State Models")
  STAGE_STATUS+=("SKIP")
  STAGE_SECS+=("0s")
  STAGE_NOTES+=("skipped (external BFS_DIR not present)")
else
  run_stage "3. BFS Lock & Cancel State Models" \
    bash -c "echo 'ERROR: BFS_DIR (${BFS_DIR}) scripts missing' >&2; exit 1"
fi

# Stage 4: BSD/macOS Non-Listening Socket Portability
run_stage "4. BSD/macOS Socket Portability" \
  "${PYTHON_BIN}" -m pytest -c "${SDK_CFG}" -o "cache_dir=${PYTEST_CACHE_DIR}" -q \
  -k "test_batch_custom_object_requests_bsd_non_listening_socket_simulation" \
  "${SDK_UNIT_DIR}/test_k8s_helper.py"

# Stage 5a: SDK 12-Cell Chaos & Fault-Injection Suite
run_stage "5a. SDK Fault-Injection Suite" \
  "${PYTHON_BIN}" -m pytest -c "${SDK_CFG}" -o "cache_dir=${PYTEST_CACHE_DIR}" -q \
  "${SDK_UNIT_DIR}/test_batch_fault_injection.py"

# Stage 5b: RL Batch-Claiming Fault-Injection Suite
if [[ -f "${RL_DIR}/tests/test_batch_fault_injection_rl.py" ]]; then
  run_stage "5b. RL Batch Fault-Injection Suite" \
    "${PYTHON_BIN}" -m pytest -o asyncio_mode=auto -o "cache_dir=${PYTEST_CACHE_DIR}" -q \
    "${RL_DIR}/tests/test_batch_fault_injection_rl.py"
fi

# Stage 6: Stateful Hypothesis Property-Based Fuzzer
if [[ ${QUICK_MODE} -eq 0 ]]; then
  run_stage "6. Stateful Hypothesis Fuzzer" \
    "${PYTHON_BIN}" -m pytest -c "${SDK_CFG}" -o "cache_dir=${PYTEST_CACHE_DIR}" -q \
    "${SDK_UNIT_DIR}/test_batch_state_fuzz.py"
else
  run_stage "6. Stateful Hypothesis Fuzzer (quick)" \
    "${PYTHON_BIN}" -m pytest -c "${SDK_CFG}" -o "cache_dir=${PYTEST_CACHE_DIR}" -q \
    -k "TestFullBatchStateFuzz or TestReattachedBatchStateFuzz or test_late_watch_added" \
    "${SDK_UNIT_DIR}/test_batch_state_fuzz.py"
fi

# Stage 7: Lockstep Sync/Async Differential Fuzzer
run_stage "7. Sync/Async Differential Fuzzer" \
  "${PYTHON_BIN}" -m pytest -c "${SDK_CFG}" -o "cache_dir=${PYTEST_CACHE_DIR}" -q \
  "${SDK_UNIT_DIR}/test_batch_differential.py"

# Stage 8: Concurrency, CAS, & Lock-Inversion Stress Suite
run_stage "8. Concurrency & CAS Stress Suite" \
  "${PYTHON_BIN}" -m pytest -c "${SDK_CFG}" -o "cache_dir=${PYTEST_CACHE_DIR}" -q \
  "${SDK_UNIT_DIR}/test_batch_concurrency_stress.py"

# Stage 9: Production Mypy & PEP 561 Strict Verification (SDK + RL)
run_stage "9. Production Mypy & PEP 561 (SDK+RL)" \
  bash -c "
    rc=0
    '${PYTHON_BIN}' -m mypy --cache-dir '${MYPY_CACHE_SDK}' --config-file '${SDK_CFG}' '${SDK_SRC_DIR}/k8s_agent_sandbox' || rc=1
    '${PYTHON_BIN}' -m mypy --cache-dir '${MYPY_CACHE_STRICT}' --strict -c 'from k8s_agent_sandbox import SandboxBatch, AsyncSandboxBatch, BatchGroup' || rc=1
    if [[ ${QUICK_MODE} -eq 0 && -d '${RL_DIR}/agent_sandbox_rl' ]]; then
      '${PYTHON_BIN}' -m mypy --cache-dir '${MYPY_CACHE_RL}' --ignore-missing-imports '${RL_DIR}/agent_sandbox_rl' || rc=1
    fi
    exit \${rc}
  "

fi  # PERF_ONLY

# Stages 10a/10b get a hard time limit, so a regression that hangs a gate fails the stage
# instead of stalling the run (the SDK suite takes about 3 minutes on 8 cores).
PERF_STAGE_TIMEOUT="${PERF_STAGE_TIMEOUT:-1800}"

# Stage 10a: SDK Performance & Complexity Suite (bytecode-count, request-count, memory gates)
run_stage "10a. SDK Performance & Complexity Suite" \
  timeout "${PERF_STAGE_TIMEOUT}" "${PYTHON_BIN}" -m pytest -c "${SDK_CFG}" -o "cache_dir=${PYTEST_CACHE_DIR}" -q \
  "${SDK_UNIT_DIR}/test_batch_performance_invariants.py"

# Stage 10b: RL Performance & Complexity Suite
if [[ -f "${RL_DIR}/tests/test_batch_performance_rl.py" ]]; then
  run_stage "10b. RL Performance & Complexity Suite" \
    timeout "${PERF_STAGE_TIMEOUT}" "${PYTHON_BIN}" -m pytest -o asyncio_mode=auto -o "cache_dir=${PYTEST_CACHE_DIR}" -q \
    "${RL_DIR}/tests/test_batch_performance_rl.py"
fi

echo ""
echo "============================================================================================================"
echo "MASTER BATCH VERIFICATION PIPELINE SUMMARY (commit $(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo unknown))"
echo "============================================================================================================"
printf "%-40s | %-6s | %-6s | %s\n" "PIPELINE STAGE" "STATUS" "TIME" "SUMMARY"
echo "------------------------------------------------------------------------------------------------------------"
for i in "${!STAGE_NAMES[@]}"; do
  printf "%-40s | %-6s | %-6s | %s\n" \
    "${STAGE_NAMES[$i]}" "${STAGE_STATUS[$i]}" "${STAGE_SECS[$i]}" "${STAGE_NOTES[$i]}"
done
echo "============================================================================================================"

exit "${OVERALL_RC}"
