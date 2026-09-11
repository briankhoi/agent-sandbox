Project Description
Include the language and/or technologies your intern will need to know/learn
Inclu


agent-sandbox (sigs.k8s.io/agent-sandbox, a Kubernetes SIG Apps subproject) provides the Sandbox CRD plus extension APIs (SandboxClaim, SandboxTemplate, SandboxWarmPool). 

An RL rollout or batch-eval harness wants N environments as a batch: claim 200 sandboxes, wait for a quorum to be ready, run the wave, release them together. Today users fire N independent SandboxClaims and hand-roll the fan-out, polling, quorum, retry, and cleanup logic — in every harness, in every language (our own stress tooling and the merged RL example SDK both reimplement it).


The workaround's failure modes are real and observed: a driver that dies at claim 137/200 leaks 137 claims until someone notices the quota burn; N clients polling readiness individually means N watch/poll connections instead of one; and our scale campaigns measured a concrete SDK defect — the claim path opens ~3 TCP connections per claim with no reuse, capping a single client process at low-thousands of claims. Naive parallel blasts are also exactly the burst pattern behind the warm-pool over-creation churn (fixed).

Batching and quorum belong in official client primitives, not a fourth extension CRD layer on the controller. 

This project builds those primitives. 

Deliverables:
Paced batch claiming — claim_batch(n) in the Python SDK with sync and async context managers (per the repo's sync/async parity convention), client-side pacing, and connection reuse (fixing the measured 3-connections-per-claim defect).
Quorum via a single watch — one label-filtered Watch stream aggregating readiness for the whole batch (a min_ready threshold unblocks the caller), replacing N individual polling/watch connections.
Crash-safe atomic cleanup — context-manager exit issues a label-selected DeleteCollection; a standalone orphan-reaper utility (keyed on a batch label + TTL, stateless, safe to run as a cron) cleans up after dead drivers.
RL example migration — migrate examples/agent-sandbox-rl to the new primitives, retiring its custom fleet plumbing. This is the acceptance test: if the official primitives can't replace that example's hand-rolled code, they're not done.

Boundary: a server-side batch object is an explicit non-goal. The benchmark milestone measures where client-side batching tops out (batch size, quorum-detection latency, reaper lag after driver death); that data — not opinion — is what would reopen the server-side question later. Boundary vs SandboxWarmPool stays as before: pool = supply-side pre-warming, batch claiming = demand-side; they compose (a batch prefers adopting from the pool).
Objectives:
claim_batch with sync/async parity, pacing, and connection reuse in the Python SDK, shipped through upstream review.
Single-watch quorum aggregation and crash-safe cleanup (DeleteCollection on exit + orphan reaper), with failure-injection tests (kill the driver mid-batch; verify the reaper converges).
Benchmark on kind: time-to-quorum and cleanup-completeness for batches of 50–500 via the new primitives vs the hand-rolled fan-out baseline; migrate examples/agent-sandbox-rl as the end-to-end demo.



