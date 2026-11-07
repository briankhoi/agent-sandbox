# Self-Contained Prompt for New Conversation: Automated Batch Verification Pipelines & Deep Bug Sweep

Copy everything below the horizontal rule (from `# Mission` to the end of the file) into a new conversation.

---

# Mission

You are working for Brian Nguyen (`brianknguyen`, GitHub `briankhoi`) to build and execute the **Automated Batch Verification & Chaos Pipeline Suite** across the `agent-sandbox` batch claiming stack and `agent-sandbox-rl`.

Your primary objective is twofold:
1. **Build Reusable, Deterministic Verification Pipelines:**
   Instead of forcing human reviewers and AI agents to manually re-read thousands of lines of code to catch subtle races and regressions, you will construct an extensive set of permanent, executable verification pipelines (TLA+ model runners, static AST/lock auditors, in-code fault-injection suites, stateful Hypothesis fuzzers, and sync/async differential test harnesses). These pipelines must be runnable with single commands so developers and future agents can run fast, deterministic sanity checks after applying fixes.
2. **Execute an Exhaustive Bug Sweep & Generate the Master Results Report:**
   You will run these verification pipelines across the current live batch codebase, systematically catalog all invariant violations, race conditions, edge cases, and platform bugs, and generate a comprehensive findings document:
   `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/deliverables/VERIFICATION_SWEEP_RESULTS.md`
   Brian and his primary PR stack conversation will then use this report to spawn builder and verifier subagent pairs to fix each finding methodically.

---

## 0. Mandatory Operating Rules & Workspace Isolation

Another active conversation owns the batch-claim PR stack, and `/usr/local/google/home/brianknguyen/agent-sandbox-restack` plus `/usr/local/google/home/brianknguyen/agent-sandbox-notes` are linked git worktrees sharing `/usr/local/google/home/brianknguyen/agent-sandbox/.git`. Follow these rules strictly:

1. **Do NOT touch `/usr/local/google/home/brianknguyen/agent-sandbox-restack`.** Do not `cd` into it, run commands in it, or edit files in it.
2. **Do NOT switch branches, commit, rebase, or push in `/usr/local/google/home/brianknguyen/agent-sandbox`.** Read code directly from immutable git refs or SHAs using `git -C /usr/local/google/home/brianknguyen/agent-sandbox show <sha>:<path>`.
3. **Do NOT fix the production bugs in this verification session!**
   Your job in this conversation is to build the verification pipelines, test the harness with negative controls, run the sweep, catalog all findings, and output the results report. Fixing the production code belongs to builder and verifier subagents on the PR stack.
4. **Keep Janet's and Aditya's bugs as unfixed negative controls first!**
   A verification tool that starts green proves nothing about its bug-detection power. You must run your new tools against the unfixed code first and prove that:
   - `BatchHandle.tla` and `test_batch_fault_injection.py` fail and reproduce Janet's bug (Watch 403 stops Lease renewal).
   - The BSD socket simulation fails and reproduces Aditya's Darwin `ConnectTimeoutError`.
   Only after the tools prove they catch these bugs do you document the reproduction traces in the results report.
5. **Where files go:**
   - Permanent tooling scripts and test files live in the repository structure (e.g. `dev/tools/`, `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/`, `handoff/work/tla/`).
   - The master findings report and progress tracking live in `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/deliverables/` (`VERIFICATION_SWEEP_RESULTS.md` and `VERIFICATION_PROGRESS.md`).
   - Also copy the final markdown deliverables into your conversation's artifact directory so Brian can inspect them in the UI.
6. **Style & hygiene rules (`working_rules.md`):**
   - **Zero em dashes (`\u2014`) anywhere** in code, tests, docs, or messages. Verify with `grep -rn $'\xe2\x80\x94'` before completing (`0` matches required).
   - **Use active voice everywhere.**
   - **Zero fabricated claims or numbers.** Record exact commit SHAs, git tree SHAs, and reproducible test commands for every finding.

---

## 1. Verified Source-of-Truth & Tooling Map

Before writing or running tests, inspect these sources:

### A. Strategy Specification & Working Rules
- **Full Verification Strategy:** `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/comprehensive_verification_strategy.md` (commit `e72f7347`). This document contains the full Master Invariant Oracle (`INV-OWN-*`, `INV-AUTH-*`, `INV-ACCT-*`, `INV-CONN-*`, `INV-LEAK-*`), the 12-cell fault-injection matrix, and specification definitions.
- **Working Rules:** `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/working_rules.md`.
- **Existing TLA+ Specification & Run Scripts:**
  - `handoff/work/tla/BatchLease.tla` (cross-process Lease protocol).
  - `handoff/work/tla/run.sh`, `runall.sh`, and `mkcfg.sh`.
  - `tla2tools.jar`: located at `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/0afb7fb0-e332-4e55-b005-158951e911bb/scratch/tla/tla2tools.jar` (or copy into `handoff/work/tla/tla2tools.jar`).
- **Existing In-Process BFS Lock/Signal Checker:**
  - `handoff/work/verify_delete_lifecycle_model.py` and `verify_cancel5_exercise.py`.

### B. Target Codebase to Audit & Sweep
- **Sole Source of Truth for Batch Stack:** `origin/feat/rl-batch-claim-mode` (`e84e964ce2d87987ec2443a5b5edccc84a374289` on `origin`, stacked on `24/N` `#2194`, `23/N` `#2189`, and `22/N` `#2188`).
- **Core SDK Modules under Audit:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_batch.py`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/batch_state.py`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/k8s_helper.py` & `async_k8s_helper.py`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/batch_reaper.py`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_client.py` & `async_sandbox_client.py`
- **Downstream RL Integration under Audit:**
  - `examples/agent-sandbox-rl/agent_sandbox_rl/fleet.py` & `async_fleet.py`
  - `examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py`
  - `examples/agent-sandbox-rl/agent_sandbox_rl/handles.py`
  - `examples/agent-sandbox-rl/agent_sandbox_rl/recycle.py`

---

## 2. Step-by-Step Implementation & Execution Plan

Follow this phased plan sequentially, updating `deliverables/VERIFICATION_PROGRESS.md` after each step.

### Step 1: Baseline Negative-Control Proof (Test the Harness First)

Before building the full suite, prove that your new verification methods catch the two known review comments on PR #2188:
1. **Reproduce Janet's Bug in TLA+ (`BatchHandle.tla`):**
   - Write a minimal `handoff/work/tla/BatchHandle.tla` modeling `renew_loop`, `watch_loop`, and `activeSandboxes`.
   - Include the current code's logic: when `watch_loop` encounters a fatal error, it sets shared `stateError := "WATCH_FAILED"`, and `renew_loop` checks `if detached \/ stateError /= "NONE": return STOPPED`.
   - Assert invariant `ActiveWorkloadProtected`:
     ```tla
     (activeSandboxes > 0 /\ now < leaseExpiresAt /\ ~detached) => renewState /= "STOPPED"
     ```
   - Run TLC via `run.sh` and verify that TLC **violates the invariant** and prints a minimal counterexample trace showing Lease renewal terminating while sandboxes are active.
2. **Reproduce Janet's Bug Live in Python:**
   - In `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py`, implement `test_watch_fatal_error_does_not_halt_lease_renewal`.
   - Run the test against the current code and verify that it **fails live**, proving that `_renew_once()` returns `False` and `_renew_loop` terminates after a watch 403.
3. **Reproduce Aditya's Bug Live:**
   - In `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_k8s_helper.py`, implement a test case simulating BSD/macOS socket behavior where connecting to a bound, non-listening port drops packets until connect timeout.
   - Verify that it **fails live** with `ConnectTimeoutError` under the current `assertRaises(urllib3.exceptions.NewConnectionError)` check.
4. Record all three negative-control failure logs in `deliverables/VERIFICATION_SWEEP_RESULTS.md`.

---

### Step 2: Build the Reusable Verification Pipelines (The Tooling Suite)

Construct the following permanent, executable pipelines so any developer or agent can run them on demand:

#### Pipeline 1: Modular TLA+ Formal Verification Suite
Implement the specifications detailed in Section 5 of `comprehensive_verification_strategy.md`:
1. `handoff/work/tla/BatchHandle.tla`: Subsystem composition and authority separation (renew, watch, create, acquire, active sandboxes).
2. `handoff/work/tla/BatchWatch.tla`: K8s watch event stream, `410 Gone` paged relists, readiness flaps, and `_connected` cache eviction.
3. `handoff/work/tla/BatchConsumer.tla`: Consumer modes (`wait_for_quorum`, `group_outcomes`, `events`), dynamic ordinals, and quorum deadlocks.
4. `handoff/work/tla/BatchAcquireRelease.tla`: Concurrent initial fill, dynamic `acquire()`, and `release_*` methods.
5. `handoff/work/tla/RLBatchLifecycle.tla`: `agent-sandbox-rl` multi-cluster batch claimer, Lease rotation, and rollback coordination.
6. Create an automated runner `handoff/work/tla/runall_subsystems.sh` that checks all specs with TLC and prints a clean pass/fail summary.

#### Pipeline 2: Static AST Guardrail & Call-Graph Auditor
Create `dev/tools/verify_batch_static.py` (executable via `python dev/tools/verify_batch_static.py`) that scans the Python AST and call graph for structural anti-patterns:
- **Transitive `await`-under-lock scanner:** Flags any `async with self._lock:` or `_changed:` block executing an `await` directly or transitively.
- **Lock-hierarchy checker:** Verifies `_lifecycle_lock -> client._batches_lock -> _lock` order is never inverted.
- **Request timeout auditor:** Flags any outbound Kubernetes API call that omits `_request_timeout` or passes an unbounded timeout on a Lease-fenced path.
- **Unretried client auditor:** Verifies all batch custom-object calls route through `_batch_custom_objects_api` rather than `custom_objects_api`.
- **`BaseException` rollback auditor:** Verifies `try ... except Exception:` blocks do not drop cleanup or leave futures unresolved under `asyncio.CancelledError` or `KeyboardInterrupt`.
- **Sync/Async AST parity checker:** Flags unapproved structural branch divergence between twin methods in `sandbox_batch.py` and `async_sandbox_batch.py`.

#### Pipeline 3: In-Code Subsystem Chaos & Fault-Injection Suite
Create `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py` covering the 12-cell fault matrix from Section 8 of `comprehensive_verification_strategy.md`:
- Watch faults: 403 Forbidden, 410 Gone during deletions, TLS EOF, slow chunk delivery.
- Renewal faults: 409 conflict with matching holder vs mismatching holder, 500/503 server error, expired certificate.
- Pacing & creation faults: 429 rate limit cooldown, warm-pool check 404, template deleted mid-creation.
- Dynamic acquire faults: timeout during precheck, concurrent slot exhaustion, member failure mid-acquire.
- Teardown faults: client exit during in-flight creates, failed connection closes during `release()`.

#### Pipeline 4: Stateful Property-Based Fuzzer (`Hypothesis`)
Create `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_state_fuzz.py`:
- Use `hypothesis.stateful.RuleBasedStateMachine` to generate arbitrary interleaved sequences of:
  `upsert_claim`, `mark_lost`, `resync_from_list`, `expire_fill`, `set_mode`, `acquire`, `release_member`, and `note_deleted`.
- Compare `BatchState`'s incremental $O(1)$ counters against a naive $O(N)$ full-scan reference model at every step (`INV-ACCT-1`).

#### Pipeline 5: Lockstep Dual-Runtime Differential Fuzzer
Create `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_differential.py`:
- Feed identical generated event streams and fault sequences to `SandboxBatch` and `AsyncSandboxBatch` simultaneously.
- Assert that member states, public property values, error codes, and connection cache operations remain 100% identical between sync and async implementations at every step.

#### Pipeline 6: Concurrency & Lock-Inversion Stress Suite
Create `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_concurrency_stress.py`:
- 50 concurrent worker threads calling `acquire()` while another thread repeatedly releases ready members.
- Concurrent `detach()` vs `get_batch()` re-attachment.
- Rapid `create_loop` cancellation under heavy in-flight load.

#### Pipeline 7: Master Verification Pipeline Runner
Create a single orchestrator script `dev/tools/run_batch_verification.sh` that executes:
`[Static AST Auditor] -> [TLA+ Suite] -> [Fault Injection] -> [Differential Fuzzer] -> [Concurrency Stress]`
and outputs a unified green/red status table.

---

### Step 3: Run the Full Verification Sweep Across the Codebase

Run the entire verification suite against the baseline codebase (`origin/feat/rl-batch-claim-mode`).
Record every single failure, including:
1. Every TLA+ invariant violation (record the spec, configuration, invariant name, and counterexample trace).
2. Every static AST violation (record the file, line, function, and anti-pattern).
3. Every fault-injection failure (record the injected fault and the unintended side effect).
4. Every state counter drift or sync/async divergence from fuzzing.
5. Every lock contention, starvation, or race from the concurrency suite.

---

### Step 4: Deliverables & Master Results Report

Generate `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/deliverables/VERIFICATION_SWEEP_RESULTS.md` containing:
1. **Executive Summary & Pipeline Coverage:** High-level summary of tests executed and overall health of the batch stack.
2. **Negative-Control Proof:** Full logs showing the live reproduction of Janet's bug and Aditya's bug on the unfixed baseline.
3. **Ranked Catalog of Discovered Bugs & Vulnerabilities:**
   Group all findings by severity:
   - **P0 (Critical / Data Loss / Split-Brain / Premature Deletion):** Must be fixed immediately.
   - **P1 (High / Invariant Violation / Hung Waiter / Leaked Task):** High priority.
   - **P2 (Medium / Flake / Minor Race / Suboptimal Recovery):** Polish and hardening.
   For each finding, provide:
   - Title and ID (e.g. `FINDING-AUTH-1`).
   - Invariant Violated (`INV-*` from `comprehensive_verification_strategy.md`).
   - Subsystem & Exact File/Line.
   - Deterministic Reproduction Command (using the new reusable pipelines).
   - Root Cause Analysis.
   - Recommended Surgical Fix.
4. **Operational Runbook for Reusable Pipelines:**
   Clear instructions on how developers and future agents can run each pipeline in 1 command as a regression test.

Also copy `VERIFICATION_SWEEP_RESULTS.md` into your conversation's artifact directory for easy reading.

---

## 3. Mandatory Independent Verifier Sign-Off

Before finishing, spawn an un-anchored `inherit` subagent (`invoke_subagent` with `TypeName="self"`, `Model="inherit"`) to audit your work:
- Verify that every pipeline script and test file executes cleanly and deterministically.
- Verify that Janet's and Aditya's bugs were genuinely reproduced as negative controls.
- Verify that every finding in `VERIFICATION_SWEEP_RESULTS.md` has an exact reproduction command, valid `file:line` citation, and concrete fix proposal.
- Verify that zero em dashes (`\u2014`) exist across all deliverables, that active voice is used, and that git working trees remain untouched.
