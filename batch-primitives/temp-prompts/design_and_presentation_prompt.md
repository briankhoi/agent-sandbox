# Self-Contained Prompt for New Conversation: Batch Claim Design Doc (2 Versions) + 10-Minute Internship Presentation

Copy everything below the horizontal rule (from `# Mission` to the end of the file) into a new conversation.

---

# Mission

You are working for Brian Nguyen (`brianknguyen`, GitHub `briankhoi`) on two deliverables covering his internship and open-source work in `kubernetes-sigs/agent-sandbox`:

1. **Deliverable 1 (Batch Claim Design Doc in Two Side-by-Side Versions):**
   Audit `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/batch_claim_proposal.md` against the shipped batch-claim codebase, RL integration, GKE scale results, Lease/reaper safety invariants, and Go SDK alignment notes. Then write **two complete, standalone design-doc versions** so Brian can compare them side by side:
   - **Version A (`batch_claim_proposal_same_structure.md`):** Keeps the exact section and subsection hierarchy of `batch_claim_proposal.md`, fixes every stale API, invariant, example, diagram edge, and number, and removes implementation-only minutiae.
   - **Version B (`batch_claim_proposal_restructured.md`):** Reorganizes the document from first principles so an engineer with **zero prior context** can pick it up and understand the problem, mental model, API contracts, safety guarantees, ecosystem fit, scalability, and trade-offs effortlessly.
   - **Audit Companion (`proposal_audit_changelog.md`):** Concise mapping of every stale claim fixed, every implementation detail removed, and how Version A and Version B map to each other structurally.

2. **Deliverable 2 (10-Minute High-Level Internship Presentation):**
   Generate a polished, high-level **10-minute presentation** (`internship_presentation_10min.md`, plus a clean self-contained HTML slide preview `internship_presentation_10min.html`) covering Brian's whole project and internship across `kubernetes-sigs/agent-sandbox`. Brian will edit the deck later as a reference, so write it at real final-presentation quality with crisp slide visuals, concise slide copy, timed speaker notes (~10 minutes total), and verified provenance citations for every metric and PR.

---

## 0. Mandatory Workspace & Git Safety Rules (Another Agent Owns the PR Stack)

Another active conversation owns the batch-claim PR stack, and `/usr/local/google/home/brianknguyen/agent-sandbox-restack` plus `/usr/local/google/home/brianknguyen/agent-sandbox-notes` are linked git worktrees sharing `/usr/local/google/home/brianknguyen/agent-sandbox/.git`. Follow these isolation rules strictly:

1. **Never touch `/usr/local/google/home/brianknguyen/agent-sandbox-restack`.** Do not `cd` into it, run `git` commands in it, or edit files in it.
2. **Never switch branches, commit, rebase, push, run `git worktree add`, or run `git fetch` in `/usr/local/google/home/brianknguyen/agent-sandbox` or `/usr/local/google/home/brianknguyen/agent-sandbox-notes`.** Mutating shared `refs/remotes/origin/*` or `.git/worktrees/` races with `--force-with-lease` pushes in the active stack conversation, and every target SHA listed below is already fetched locally. Read code and benchmark files directly from immutable git refs or SHAs with `git -C /usr/local/google/home/brianknguyen/agent-sandbox show <sha>:<path>` (and only run a targeted `git fetch` with Brian's HTTPS credential helper `-c 'credential.helper=!gh auth git-credential' -c 'url.https://github.com/.insteadOf=git@github.com:'` if `git cat-file -e <sha>` ever reports a missing object).
3. **Never overwrite existing tracked files in `/usr/local/google/home/brianknguyen/agent-sandbox-notes`.** Do **not** overwrite `batch_claim_proposal.md`, `reaper_contract.md`, `handoff/state.md`, `handoff/pr_board.md`, `handoff/learnings.md`, or `handoff/working_rules.md`, and do **not** commit or push to `agent-sandbox-notes` unless Brian explicitly asks afterward.
4. **Never read files prefixed with `STALE_` or `stale_`** in `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/` (`STALE_PR_2_prompt.md`, `STALE_pr2_redesign_prompt.md`, `STALE_pr2_revision_prompt.md`, `stale_python_sdk_batch_plan.md`). They describe superseded designs.
5. **Write final deliverables + `PROGRESS.md` to `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/deliverables/`** (this directory will remain untracked `??` in `agent-sandbox-notes`; keep any temporary scratch scripts in your own conversation `<artifact-dir>/scratch/`). Also copy the final markdown deliverables into your conversation's artifact directory for easy viewing. Maintain `deliverables/PROGRESS.md` after each phase so the task resumes cleanly after any interruption:
   - Phase 0: Preflight SHA/tree/SDK-version verification & resume check (`deliverables/PROGRESS.md`)
   - Phase 1: Code, PR, Lease/reaper protocol, Go design, and GKE benchmark audit (`deliverables/proposal_audit_changelog.md`)
   - Phase 2: Design Doc Version A (`deliverables/batch_claim_proposal_same_structure.md`) + Version B (`deliverables/batch_claim_proposal_restructured.md`)
   - Phase 3: 10-minute presentation (`deliverables/internship_presentation_10min.md` + `deliverables/internship_presentation_10min.html`)
   - Phase 4: Independent `inherit` verifier subagent audit & sign-off
6. **Never comment on, open, edit, or close any GitHub PR or issue.** Read-only `gh pr list` / `gh pr view` queries are fine.
7. **Writing, provenance, comparison, and TLA+ rules (read `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/working_rules.md` first):**
   - **Zero em dashes (`\u2014`) anywhere** in design docs, slides, speaker notes, changelogs, or chat messages. Verify with `grep -rn $'\xe2\x80\x94'` before finishing (`0` matches required). Avoid colons in prose where natural active-voice phrasing works better.
   - **Use active voice everywhere.** Say who does what ("The batch handle renews the Lease every `lease_duration // 3` seconds", not "The Lease is renewed").
   - **Do NOT mention TLA+ or TLC in either Design Doc version or on any presentation slide.** Brian wants every slide and design section to be 100% defensible from distributed-systems and Kubernetes first principles without inviting TLA+ language/syntax questions in Q&A. Explain the Lease-UID claim selector fencing (`agents.x-k8s.io/batch-lease-uid`) and the full `leaseDurationSeconds` reaper confirmation wait purely through the concrete multi-process race scenarios and Kubernetes `Lease` / `ownerReferences` invariants. (You may read `handoff/learnings/tla.md` as background to understand those exact race traces, and in Slide 5's Q&A backup note only, you may include one optional sentence noting that Brian used AI-assisted state-space model checking to surface candidate multi-process race traces and then verified and reproduced them directly in Python code and tests.)
   - **Provenance & zero fabricated numbers (`working_rules.md:8, 50`):** Every metric, rollout count, memory figure, latency, API signature, or PR claim must trace to a verified file and commit SHA. Record the **commit SHA, git tree SHA (`git rev-parse <sha>^{tree}`), and SDK version (`git describe --tags --always <sha>`)** in `deliverables/proposal_audit_changelog.md` and `deliverables/PROGRESS.md`. Mark facts in your audit notes and slide provenance blocks as measured, verified (`file:line` at `<sha>`), or inferred.
   - **Fair benchmark comparisons (`working_rules.md:49`):** Whenever presenting batch vs per-claim comparisons in the scalability section or slides, preserve arm parity (the same limiting mechanism on both arms, leading with unlimited vs unlimited: batch defaults vs per-claim as shipped).

---

## 1. Verified Source-of-Truth Map

Before writing anything, check `deliverables/PROGRESS.md` (if resuming) and inspect these verified sources.

### A. Design & Handoff Notes (`/usr/local/google/home/brianknguyen/agent-sandbox-notes`)
- Working rules: `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/working_rules.md`
- Current design doc baseline: `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/batch_claim_proposal.md`
- Reaper contract companion: `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/reaper_contract.md`
- PR stack board (`1/N` through `25/N`): `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/pr_board.md`
  - *Important staleness check:* `pr_board.md` still has pre-merge/pre-open labels on a few rows (`24/N` is now OPEN upstream as PR `#2194`, and standalone PRs `#2035`, `#2086`, and `#2087` are now MERGED on `upstream/main`). Always cross-check live status with `gh pr list --repo kubernetes-sigs/agent-sandbox --author briankhoi --state all --limit 100`.
- Live stack state & history: `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/state.md`
- Engineering learnings index & deep dives:
  - `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/learnings.md`
  - `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/learnings/optimizations.md` (connection, watch, memory, and pacing optimizations)
  - `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/learnings/gke-benchmarking.md` (GKE scale findings)
  - `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/learnings/verification.md` and `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/learnings/tla.md` *(background reference for exact multi-process race scenarios only; remember not to cite TLA+/TLC in the design docs or on slides)*
- Python SDK memory-leak audit & kind measurements (`#2085`, `#2086`, `#2087`):
  - `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/work/results/G4-memleak-final.md`
- Go SDK Batch Design (rev 3.2, branch `origin/go-batch-design-notes` @ `af1dd651bb59ffd03c5f65bc20d5c2f05d14dd4c`):
  - Read via `git -C /usr/local/google/home/brianknguyen/agent-sandbox-notes show af1dd651bb59ffd03c5f65bc20d5c2f05d14dd4c:batch-primitives/go-sdk-design/go_batch_final_design.md` (plus `python_sdk_alignment.md` and `kind_experiments.md` in the same directory).
  - *Important lineage caveat:* `af1dd651` was frozen before Python PRs `19/N` through `23/N` landed (warm-pool credit `#2166`, paged list `#2175`, `agents.x-k8s.io/batch-lease-uid` selector fencing + full `leaseDurationSeconds` reaper confirmation wait + unretried batch custom-object calls `#2188`, and `acquire(..., pod_annotations=...)` `#2189`). When writing the Go wire-contract and Go API sections in Versions A and B, align them with the `19/N`-`23/N` wire/API contract on `origin/feat/rl-batch-claim-mode` rather than copying pre-`22/N` Lease or reaper rules.

### B. Live Code & Benchmark Refs (`/usr/local/google/home/brianknguyen/agent-sandbox`, `origin` = `briankhoi/agent-sandbox`, `upstream` = `kubernetes-sigs/agent-sandbox`)
1. **Sole Source of Truth for Python SDK Batch Code + Base `25/N` (`origin/feat/rl-batch-claim-mode`, verified at `31a703cdb197497edbe27990bd1a67151bc5ce96`, tree `6529613ce2f4b842ca7b78db14a7a4b5a0b05f2f`, SDK version `v1.0.9-79-g31a703cd`):**
   - Stacked linearly on `24/N` (`#2194` `51ba4e78`), `23/N` (`#2189` `8a435b0f`), `22/N` (`#2188` `d9df3a42`), and `upstream/feature/batch-claim` (`76d750e2aa3b7c117c6af503fcbcb81772ce11d8`). Run `git -C /usr/local/google/home/brianknguyen/agent-sandbox rev-parse origin/feat/rl-batch-claim-mode` at startup and use its current SHA + tree SHA if a `CHANGELOG.md` update landed on `25/N`.
   - Core Python SDK files under `clients/python/agentic-sandbox-client/`:
     - `README.md` (Batch Claims user documentation and Error Handling section)
     - `k8s_agent_sandbox/__init__.py` (public exports)
     - `k8s_agent_sandbox/models.py` (`BatchGroup`, `Member`, `MemberState`, `BatchEvent`, `BatchEventType`, `GroupOutcome`)
     - `k8s_agent_sandbox/exceptions.py` (`BatchError` -> `SandboxError`, plus `BatchNotFoundError`, `BatchLeaseExpiredError`, `BatchInUseError`, `BatchExistsError`, `QuorumUnreachableError`, `BatchTimeoutError`, `TerminalMemberError`)
     - `k8s_agent_sandbox/sandbox_client.py` & `k8s_agent_sandbox/async_sandbox_client.py` (`claim_batch`, `get_batch`, client exit-cleanup semantics)
     - `k8s_agent_sandbox/sandbox_batch.py` & `k8s_agent_sandbox/async_sandbox_batch.py` (`SandboxBatch` / `AsyncSandboxBatch` public surface and docstrings)
     - `k8s_agent_sandbox/batch_reaper.py` (`reap`, `find_stale_leases`, `is_lease_reapable`, `main` CLI contract)
     - `k8s_agent_sandbox/constants.py` & `k8s_agent_sandbox/batch_utils.py` (`BATCH_LEASE_NAME_PREFIX = "batch-"`, reserved `agents.x-k8s.io/batch-*` labels including `batch-lease-uid`, ownerReferences)
   - Base RL integration files under `examples/agent-sandbox-rl/`:
     - `agent_sandbox_rl/batch_claims.py`, `agent_sandbox_rl/fleet.py`, `agent_sandbox_rl/async_fleet.py`, `agent_sandbox_rl/config.py`, `README.md`
2. **Multi-Cluster RL Batch Hardening Commits (`origin/feat/rl-batch-hardening` @ `4018f0ddfa381baf91b41a28c555ac9d0d25903e`):**
   - *Important lineage caveat:* `4018f0dd` sits on an older `25/N` base (`8b63a7f7`, branched at `18/N` `a97eeae7`) before `22/N` and `23/N` were restacked. Do **not** read Python SDK files from `4018f0dd`; inspect **only** the 9 RL hardening commits `8b63a7f7..4018f0dd` (`git -C /usr/local/google/home/brianknguyen/agent-sandbox log -p 8b63a7f7..4018f0dd`) for multi-cluster parallel `acquire_batch`/`release_all` rollback, auto-sized cluster connection pools, 4096 async thread pool ceiling, `work_budget` hints, snapshot trigger cleanup, and watchdog/Lease RBAC handling.
3. **1,000-Sandbox GKE Benchmark Suite & Audited Results (`origin/bench/rl-gke` @ `a8dfcb8deef8b521413170fcf213b2b5b992f886`):**
   - Read via `git -C /usr/local/google/home/brianknguyen/agent-sandbox show a8dfcb8deef8b521413170fcf213b2b5b992f886:<path>`:
     - `examples/agent-sandbox-rl/bench-results/gke/campaign-v2/EXECUTIVE_SUMMARY.md`
     - `examples/agent-sandbox-rl/bench-results/gke/campaign-v2/README.md`
     - `examples/agent-sandbox-rl/bench-results/gke/EXECUTIVE_SUMMARY.md`
     - `examples/agent-sandbox-rl/bench-results/gke/GKE_APF_WARMPOOL_DEEP_DIVE.md`
     - `examples/agent-sandbox-rl/bench-results/gke/PROVENANCE.md`
4. **Upstream PR Portfolio (`36` merged/open PRs = `25` on `feature/batch-claim` + `11` standalone merged PRs on `main`, plus `2` closed superseded PRs `#1987` and `#1407`):**
   - **Batch Claim Stack on `feature/batch-claim` (`25` PRs: `22` merged + `3` open `#2188`, `#2189`, `#2194`, plus `25/N` branch):** `#1742` (1/N), `#1924` (2/N), `#1925` (3/N), `#1982` (4/N), `#1983` (5/N), `#1984` (6/N), `#1985` (7/N), `#2005` (merge main 1), `#2033` (8/N), `#1986` (9/N), `#2049` (10/N), `#1996` (11/N), `#1824` (12/N), `#1988` (13/N, superseding closed `#1987`), `#1989` (14/N), `#1990` (15/N), `#1991` (16/N), `#2153` (17/N), `#2154` (18/N), `#2166` (19/N), `#2175` (20/N), `#2176` (21/N), `#2188` (22/N open), `#2189` (23/N open), `#2194` (24/N open).
   - **Standalone Merged Upstream PRs on `main` (`11` merged PRs; inspect `upstream/main` @ `7ea8b7370a0b895a63255d1d4dcf60c8936bf363`, `G4-memleak-final.md`, and `gh pr view <num> --repo kubernetes-sigs/agent-sandbox`):**
     - **Python SDK memory-leak elimination series (`#2085`, `#2086`, `#2087`):** Untracked terminated handles (`#2085`), broke handle-to-connector reference cycle (`#2086`), and switched `atexit` to weak/registry-only tracking (`#2087`). Distinguish the two measured workloads cleanly (`G4-memleak-final.md` + commit messages): (a) RSS growth slope over `600` async create/command/terminate cycles on kind (40 workers) dropped `~196x` from **`90.1 MiB` to `0.46 MiB` per 100 cycles**, and (b) `2,400` create/terminate cycles dropped from **`1,660 MiB` of retained heap** on `main` to **`+0 MiB` retained** for short-lived handles (or a flat `+44-45 MiB` live set with a 64-handle sliding window).
     - **Go controller Kubernetes Events (`#1532` + `#1826`):** Emitted `Sandbox` lifecycle transition Events (`Provisioning`, `Ready`, `Expired`, etc.) gated on both `Status` and `Reason`.
     - **Go controller & SDK legacy pod-name annotation removal (`#1417` + `#1467`):** Removed redundant `pod-name` metadata patch in the controller (`#1417`, `~2.2x` faster average `Sandbox` reconcile latency: **`14.53ms -> 6.51ms`**, verified in `gh pr view 1417`) and hardened empty annotation handling across SDKs (`#1467`).
     - **`agent-sandbox-rl` concurrent client build fix (`#2035`):** Built `Cluster.sandbox_client` once under lock (`160 -> 1` kubeconfig loads and **`4.22s -> 1.37s`** cold-build wall time under 160 concurrent workers).
     - **Cross-SDK & Python SDK reliability (`#1917`, `#1509`, `#1512`):** Python + TypeScript SDK terminal `SandboxClaim` Ready failure reasons parity (`#1917`), pre-configured `ApiClient` injection for multi-cluster/custom auth (`#1509`, co-authored completion of `#1069`/`#1088`), and synchronous `ApiClient` for reliable `atexit` sandbox cleanup (`#1512`).

---

## 2. Detailed Instructions for Deliverable 1: Batch Claim Design Doc (2 Side-by-Side Versions)

### Design-Doc Boundary Rule (Crucial)
`batch_claim_proposal.md` is Brian's **design document**, not an implementation walkthrough or code dump.
- **What belongs in the design doc:**
  - Problem statement, motivation, and architectural trade-offs (client-side SDK composition vs server-side CRD, 1 watch vs N watches, Lease + `ownerReferences` + reaper vs client-only cleanup).
  - Conceptual model: Batch, `BatchGroup` (`warmpool`, `size`, `min_ready`), `Member` (`MemberState`: `PENDING`, `READY`, `FAILED`, `LOST`), `BatchEvent` (`BatchEventType`: `MEMBER_READY`, `MEMBER_LOST`, `MEMBER_FAILED`), `GroupOutcome` (`warmpool`, `members`, `error`), Lease ownership (`batch-<batch_id>`), Kubernetes label schema (`agents.x-k8s.io/batch-id`, `agents.x-k8s.io/batch-group`, `agents.x-k8s.io/batch-lease-uid`), and RBAC permissions.
  - Public SDK surface and behavioral contracts (Python sync & async `claim_batch` / `get_batch` / `SandboxBatch` methods, plus Go SDK `ClaimBatch` / `GetBatch` / `Batch` aligned to the `19/N`-`23/N` wire/API contract).
  - Lifecycle & consumption patterns: paced creation (`create_rps=50`, `max_in_flight=20`, `quorum_timeout=600`, `work_budget=3600`, `lease_duration=60`) with warm-pool credit (`SandboxWarmPool.status.readyReplicas`), watch-driven cold-claim credit exhaustion, and `429` cooldown; the three consumption modes (`wait_for_quorum`, `group_outcomes`, `events`); dynamic membership (`acquire(warmpool, timeout=None, *, pod_annotations=None)`, `release_member`, `release_group`, `release_not_ready`); `connect()` pod-IP fast path and connection pool sizing (`pool_size >= max_in_flight + 2`); liveness (`lease_degraded()`, `err()`, renewal every `lease_duration // 3`); process-exit backstop (`claim_batch` releases on exit vs `get_batch` detaches on exit unless explicitly released); cross-process handoff (`detach(grace=None)` + `get_batch`); and Lease-UID selector fencing (`batch-lease-uid` preventing late `deletecollection` calls from deleting a newer batch reusing `batch_id`) + reaper confirmation wait (full `leaseDurationSeconds` + `resourceVersion` delete precondition preventing split-brain under clock skew and slow renewals).
  - Error model (matching `README.md` and `working_rules.md:43`): every batch-specific runtime failure (`BatchTimeoutError`, `QuorumUnreachableError`, `TerminalMemberError`, `BatchExistsError`, `BatchNotFoundError`, `BatchInUseError`, `BatchLeaseExpiredError`) subclasses `BatchError` (which subclasses `SandboxError`), so `except BatchError` catches batch runtime failures ergonomically while specific subclasses enable targeted recovery; `SandboxError` additionally covers warm-pool/template precheck (`SandboxWarmPoolNotFoundError`, `SandboxTemplateNotFoundError`) and `connect()` (`SandboxNotReadyError`); invalid arguments raise `ValueError`; and direct Kubernetes API or network errors during setup, teardown, or checking an unchecked pool propagate `ApiException` or transport errors unchanged.
  - Idiomatic usage examples (Python, Go, `agent-sandbox-rl` `claim_mode="batch"`), measured scalability math with fair arm parity (kind + 1,000-sandbox GKE Campaign 2 highlights), and alternatives considered.
- **What MUST NOT clutter the design doc:**
  - Private class, attribute, or helper names (`_UnretriedApiClient`, `_BatchState`, `_pending_deletes`, `_checking_acquires`, `_take_batch_lease`, `_renew_once`, etc.).
  - Internal lock hierarchies, Condition variable wakeup choreography, `urllib3`/`aiohttp` retry-object internals, pagination token mechanics, TLA+/TLC tool mentions, or PR-by-PR restack history.
  - Superseded or cut APIs presented as shipped features (`replace()` was cut in favor of `release_member` + `acquire`; `wait_for_quorum()` has **no** per-call `timeout` parameter and uses `quorum_timeout`; `LEASE_DEGRADED` is **not** a `BatchEventType`, it is polled via `lease_degraded()`; `iter_ready_groups` / `GroupReady` were renamed to `group_outcomes` / `GroupOutcome`; `adopt_expired` was cut).

### Step 1: Write `deliverables/proposal_audit_changelog.md`
Read `batch_claim_proposal.md` line-by-line alongside the live Python code on `origin/feat/rl-batch-claim-mode`, `reaper_contract.md`, `go_batch_final_design.md` (`af1dd651`), and `origin/bench/rl-gke` (`a8dfcb8d`). Record:
1. Exact commit SHAs, git tree SHAs, and SDK version inspected.
2. Every stale signature, type name, label, lifecycle rule, diagram edge, RL snippet, Go snippet, or scalability claim in `batch_claim_proposal.md`, paired with the verified shipped behavior (`file:line`).
3. Every implementation-specific detail stripped so the doc reads cleanly as a design specification.
4. A concise structural comparison table showing how Version A and Version B map to each other.

### Step 2: Write Version A (`deliverables/batch_claim_proposal_same_structure.md`)
Preserve the **exact top-level and subsection (`##`, `###`, and `####`) hierarchy** of `batch_claim_proposal.md` so Brian can compare it section-for-section with his existing Google Doc:
- `# Sandbox Batch Claim API for SDK`
- `## Summary`
- `## Motivation`
- `## Proposal`
- `## Design`
  - `### Batch Model`
  - `### Batch Lifecycle` (`#### Paced Creation`, `#### Membership`, `#### Events and Quorum`, `#### Liveness`, `#### Shutdown Backstop`, `#### Cleanup`, `#### Reaper`)
  - `### Batch Lifecycle Diagram`
  - `### RBAC`
- `## SDK API Additions`
  - `### Core Batch Methods`
  - `### Python`
  - `### Go`
- `## SDK Usage`
  - `### Python` (`#### 1. Fixed cohort example`, `#### 2. Per-group quorum example`, `#### 3. Pure streaming dispatch example`, `#### 4. Pipelined rolling queue example`)
  - `### Go`
  - `### Agent Sandbox RL` (`#### Rollout Waves: Per-Step Cohorts`, `#### Held Pools: Reuse Across Steps`, `#### Warm Pools Under a Batch`, `#### Upgrading Existing Evaluation Executors`, updated to show shipped `FleetConfig(claim_mode="batch")` alongside the underlying batch cohort patterns)
  - `### Scalability` (`#### Batch Claim Improvements`, `#### Transport & Connection Scaling`, `#### Batch Scaling`, `#### Client-side vs. Server-side Scaling`)
- `## Alternatives`

Within that exact skeleton, tighten prose for zero-context readability, fix every outdated detail, update all Python/Go/RL snippets to match real types and method signatures, update the Mermaid lifecycle diagram, and update the scalability tables with verified numbers.

### Step 3: Write Version B (`deliverables/batch_claim_proposal_restructured.md`)
Design the cleanest, sharpest possible organization for a reader who has **never seen `agent-sandbox` batch claims before**. Choose the section structure, ordering, tables, and Mermaid diagrams so every concept lands before it is used. Make sure Version B:
- Opens with an executive summary and a 60-second architectural mental model (why per-claim breaks at fleet scale, what a Batch is in Kubernetes, and how 1 Lease + N Claims + 1 Watch solve creation, consumption, and crash-safe cleanup).
- Clearly separates **Core Concepts & Data Model**, **Consumption & Dynamic Membership Patterns**, **Creation Pacing & Connection Scaling**, **Distributed Ownership, Incarnation Fencing & Crash Recovery (Lease + GC + Reaper)**, **Public SDK Surface (Python & Go) & Error Model**, **RBAC & Operational Footprint**, **Ecosystem Integration (`agent-sandbox-rl`) & Measured Scale (kind + 1,000-Sandbox GKE)**, and **Design Alternatives & Trade-Offs**.
- Uses concise comparison tables and clean Mermaid diagrams (`flowchart`, `sequenceDiagram`, or `stateDiagram-v2` with quoted labels where needed) rather than walls of dense prose.
- Stays strictly at the **design** level (zero private implementation internals and zero TLA+ mentions) while losing none of the essential design rigor of Version A.

---

## 3. Detailed Instructions for Deliverable 2: 10-Minute Internship Presentation

Create `deliverables/internship_presentation_10min.md` (and a companion self-contained browser slide deck `deliverables/internship_presentation_10min.html`).

### Presentation Goals & Constraints
- **Target Duration:** Strictly **10 minutes** (~9 core slides at ~60-75 seconds per slide, plus 2-3 optional Appendix / Q&A backup slides that do not count toward the 10-minute budget).
- **Audience:** Engineering leadership, Kubernetes / infrastructure engineers, and AI/RL platform engineers who know Kubernetes basics (`Pod`, `Watch`, `Controller`) and ML/agent evaluation concepts, but do **not** know the internals of `agent-sandbox`.
- **Tone & Defensibility:** High-level, technical, crisp, metric-driven, and 100% defensible from Kubernetes and distributed-systems first principles. Do **not** mention TLA+ or TLC on any slide or in the main speaker script.

### Recommended 10-Minute Narrative Arc (Adapt Only If It Makes the Story Even Clearer)
1. **Slide 1 (~0:45) - Title & Executive Summary:** Scaling Stateful Agent Sandboxes on Kubernetes: Batch Orchestration, RL Fleet Scale, and Upstream SDK/Controller Reliability (`kubernetes-sigs/agent-sandbox` internship overview + headline numbers: `36` merged/open upstream PRs = `25` batch stack + `11` standalone merged PRs, 1,000-sandbox GKE scale, `148,544` RL rollouts).
2. **Slide 2 (~1:00) - Motivation & Bottleneck:** Why AI agent evaluation & RL training need hundreds to thousands of isolated sandboxes simultaneously, and why 1-by-1 `SandboxClaim` orchestration breaks at scale (`O(N)` watches, create bursts / APF saturation, `O(N)` `Sandbox` GETs, connection pool stalls, orphaned pods on driver crash).
3. **Slide 3 (~1:15) - Core Architecture (`SandboxBatch` in the Python SDK):** Turning `N` independent polling/watch loops into **1 shared Lease (`batch-<id>`) + 1 label-scoped Watch + paced creation**. Highlight warm-pool credit (`readyReplicas` fast path + cold-claim watch cutover) and the pod-IP `connect()` fast path eliminating per-sandbox `Sandbox` GETs.
4. **Slide 4 (~1:10) - Flexible Consumption for Diverse Agent & RL Workloads:** How one batch state machine supports synchronous RL cohorts (`wait_for_quorum`), multi-pool pipelined cohorts (`group_outcomes`), pure async streaming (`events`), dynamic replacement/elasticity (`acquire(..., pod_annotations=...)` + `release_*`), and cross-process handoff (`detach` / `get_batch`).
5. **Slide 5 (~1:15) - Crash-Safe Distributed Ownership & Incarnation Fencing:** How Kubernetes `Lease` + `ownerReferences` garbage collection + optional Reaper CronJob guarantee zero leaked sandboxes on `SIGKILL` without split-brain kills. Walk through the two concrete distributed races solved in `#2188`: scoping `deletecollection` by Lease incarnation UID (`agents.x-k8s.io/batch-lease-uid`) so a stalled handle cannot delete a newer batch reusing `batch_id`, and waiting one full `leaseDurationSeconds` with a `resourceVersion` precondition in the reaper to tolerate clock skew and slow renewals. *(In a separate Q&A tip note below the speaker script, include 1 optional sentence Brian can use if asked how those races were discovered: using AI-assisted state-space exploration to generate candidate multi-process race traces and then verifying/reproducing them directly in Python code and tests.)*
6. **Slide 6 (~1:15) - `agent-sandbox-rl` Integration:** Wiring `claim_mode="batch"` into the multi-cluster RL fleet orchestrator (`25/N` + RL hardening): parallel multi-cluster cohort acquisition, automatic connection/thread-pool sizing, rotation of recycled batch claims before `shutdownTime` expiry, atomic rollback on partial step failure, and seamless support for SWE-bench, R2E-Gym, OpenHands, and Tunix GRPO adapters.
7. **Slide 7 (~1:20) - 1,000-Sandbox GKE Scale Validation (Campaign 2):** Fair arm-parity findings from running `148,544` rollouts on GKE across 500 to 1,000 concurrent sandboxes (`origin/bench/rl-gke`): warm vs cold throughput, 10,000-rollout soak reliability, 1,000-sandbox `deletecollection` cleanup speed, Secondary Boot Disk vs Image Streaming (GCFS) findings, and Kubernetes API Priority & Fairness (APF) discoveries.
8. **Slide 8 (~1:10) - Broader Upstream Impact Across `kubernetes-sigs/agent-sandbox`:** Dedicated highlight slide for Brian's 11 merged standalone PRs outside the batch stack:
   - **~196x Python SDK Memory-Leak Reduction (`#2085`, `#2086`, `#2087`):** `90.1 MiB -> 0.46 MiB` RSS slope per 100 cycles over 600 kind cycles (and `1,660 MiB -> +0 MiB` retained heap over 2,400 create/terminate cycles).
   - **Go Controller Observability & Performance (`#1532`, `#1826`, `#1417`, `#1467`):** Kubernetes `Event` emission for `Sandbox` transitions (`#1532`, `#1826`) and `~2.2x` faster average `Sandbox` reconcile latency (`14.53ms -> 6.51ms`) by removing legacy `pod-name` annotation writes (`#1417`, `#1467`).
   - **RL & Cross-SDK Reliability (`#2035`, `#1917`, `#1509`, `#1512`):** Lock-safe singleflight `Cluster.sandbox_client` build (`4.22s -> 1.37s`, `160 -> 1` kubeconfig loads at 160 workers, `#2035`), Python + TypeScript terminal Claim failure parity (`#1917`), multi-cluster `ApiClient` injection (`#1509`), and sync `atexit` cleanup (`#1512`).
9. **Slide 9 (~0:50) - Key Takeaways & Future Roadmap:** Design lessons (client-side composition before new CRDs, incarnation fencing + K8s GC for crash-safe client SDKs, end-to-end 1,000-sandbox testing uncovering infra bottlenecks like APF/GCFS), Go SDK batch implementation (`go_batch_final_design.md` aligned with `19/N`-`23/N`), and criteria for future server-side `SandboxBatchClaim` CRD.
10. **Optional Backup Slides (Appendix for Q&A, 0:00 in main talk):**
    - Backup A: Batch PR Stack Overview (`1/N`-`25/N`) & Testing Strategy (unit + deterministic interleaving tests + kind + 1,000-sandbox GKE).
    - Backup B: Lease Incarnation Reuse Race Diagram (step-by-step sequence showing how `batch-lease-uid` blocks late `deletecollection` across `batch_id` reuse).
    - Backup C: Detailed GKE Campaign 2 Benchmark Matrix & Arm-Parity Notes.

### Format Required for Every Slide in `internship_presentation_10min.md`
For each slide, include:
1. **Slide Header & Time Budget** (for example `## Slide 5: Crash-Safe Ownership & Incarnation Fencing (1:15 | Cumulative 5:25)`).
2. **On-Slide Headline** (one takeaway sentence at the top of the slide, assertion-evidence style).
3. **Visual / Diagram Layout** (a concrete Mermaid diagram, clean comparison table, or chart layout with exact verified data points).
4. **On-Slide Bullet Points** (3 to 4 crisp, scannable bullets max; no paragraph walls).
5. **Speaker Notes (Talk Track)** (~130-165 words per minute of budgeted time, natural spoken tone, active voice, smooth transition to next slide).
6. **Verified Provenance Block** (exact PR numbers with `https://github.com/kubernetes-sigs/agent-sandbox/pull/<N>` links, `file:line`, and commit SHA backing every claim/metric on that slide).

Also generate `deliverables/internship_presentation_10min.html`: a clean, modern, keyboard-navigable (`Left`/`Right` arrow keys, slide counter, press `S` to toggle speaker notes) single-file HTML/CSS/JS slide deck rendering the exact slides and visuals so Brian can flip through the presentation visually in Chrome.

---

## 4. Mandatory Independent Subagent Verification Before Completion

Per `working_rules.md` ("Self-check", lines 80-83), once you finish drafting all deliverables in `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/deliverables/`, spawn **one fresh `inherit` subagent** (`invoke_subagent` with `TypeName="self"`, `Model="inherit"`) as an un-anchored adversarial verifier.

Give the verifier **only**:
1. Brian's goals (design-focused update of `batch_claim_proposal.md` in same-structure + restructured versions with zero TLA+ mentions, plus accurate 10-minute high-level internship presentation keeping TLA+ off slides and highlighting standalone upstream PRs).
2. The paths to your generated files in `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/deliverables/` and the baseline `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/batch_claim_proposal.md`.
3. The live repo SHAs (`origin/feat/rl-batch-claim-mode`, `4018f0ddfa381baf91b41a28c555ac9d0d25903e`, `a8dfcb8deef8b521413170fcf213b2b5b992f886`, `7ea8b7370a0b895a63255d1d4dcf60c8936bf363`, and `af1dd651bb59ffd03c5f65bc20d5c2f05d14dd4c`) and `working_rules.md`.

Instruct the verifier to independently check:
- **Accuracy against live code & benchmarks:** Every method name, parameter name/default, return type, exception type, Kubernetes label/annotation, Lease/reaper rule, PR number/status, and benchmark/memory/latency metric matches the repository at the recorded SHAs (`0` fabricated or stale claims).
- **Design vs implementation boundary & zero TLA+ on slides/docs:** Neither design doc leaks private SDK helper names, lock names, implementation clutter, or TLA+/TLC references, and no presentation slide mentions TLA+/TLC, while every caller-visible contract and distributed-systems invariant stays crystal clear.
- **Structural fidelity:** Version A preserves `batch_claim_proposal.md`'s exact `##` / `###` / `####` section hierarchy; Version B is genuinely clear and self-contained for a zero-context reader.
- **Presentation pacing & quality:** Core slides fit cleanly within 10 minutes (~1,300-1,500 spoken words across core speaker notes), preserve benchmark arm parity, highlight batch + RL/GKE + standalone upstream PRs, and render cleanly in `internship_presentation_10min.html`.
- **Style & safety hygiene:** Zero em dashes (`\u2014`) across all deliverables, active voice throughout, and `git status` in `/usr/local/google/home/brianknguyen/agent-sandbox` and `/usr/local/google/home/brianknguyen/agent-sandbox-restack` is untouched (with only `batch-primitives/deliverables/` untracked in `agent-sandbox-notes`).

Fold every valid finding from the verifier into the deliverables, copy the final markdown deliverables into your conversation's artifact directory so Brian can view them directly in the UI as well as in `deliverables/`, and finish with a concise executive summary linking all deliverables and highlighting the key structural trade-offs between Version A and Version B.
