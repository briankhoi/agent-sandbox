### Reaper Contract

The reaper is a stateless process that cleans up batches whose driver is gone. It relies on the Lease contract the SDKs already implement, and doesn't change it. It reads the Lease's `renewTime` and `leaseDurationSeconds` (below, *D*). It writes the Lease only to fence a batch it is about to delete, and never touches the batch annotations. Until a reaper is deployed, each claim's `shutdownTime` is the only crash cleanup.

#### Namespace-Scoped Authorization

The reaper runs under its own ServiceAccount. It gets access one namespace at a time through RoleBindings, never a ClusterRoleBinding. The RoleBindings can all reference one shared ClusterRole, which then grants its permissions only inside each bound namespace. The reaper takes an explicit list of namespaces, defaulting to its own, and never lists across the cluster. If it gets a 403 in one namespace, it logs it and moves on to the next.

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: sandbox-batch-reaper
rules:
- apiGroups: ["coordination.k8s.io"]
  resources: ["leases"]
  verbs: ["list", "get", "update", "delete"]
- apiGroups: ["extensions.agents.x-k8s.io"]
  resources: ["sandboxclaims"]
  verbs: ["list", "deletecollection"]
```

RBAC can't restrict `deletecollection` by label, so the reaper enforces its scope in code. It only deletes claims with the selector `agents.x-k8s.io/batch-id=<id>`, for an id that passes batch-id validation, and never with an empty selector. It only touches Leases named `batch-<id>` whose label matches that id.

#### Staleness and Clock Skew

The reaper considers a Lease stale when `now > renewTime + D + REAPER_SKEW_MARGIN`, with `now` taken from the reaper's own clock. The margin absorbs clock skew between the driver and the reaper. It is configurable, 30–60 s by default, and never below `CLOCK_SKEW_MARGIN`. The margin only ever adds waiting, because reaping early destroys running work while waiting only holds idle quota.

This puts three thresholds in a deliberate order:
1. `GetBatch` stops adopting a Lease at `renewTime + D − CLOCK_SKEW_MARGIN`.
2. A driver that can't renew declares itself expired (`BatchLeaseExpiredError`) *D* after its last successful renewal, measured on its own monotonic clock.
3. The reaper acts only at `renewTime + D + REAPER_SKEW_MARGIN`.

As long as real skew stays below the margin, the driver has given up before the reaper deletes anything. A detached batch needs no special handling: `Detach(grace)` writes `renewTime = now` and `leaseDurationSeconds = grace`, so the reaper waits `grace` plus the margin. Worst-case crash cleanup is `D + margin + CronJob period`, plus deletion time, which grows with batch size because `deletecollection` deletes claims one at a time.

#### Sweeping Unleased Claims

`ClaimBatch` creates the Lease before any claim, and `Release` deletes it last. So a claim with a batch-id label but no Lease never belongs to a batch that is still being created. Such claims come only from:
- a Lease deleted by hand;
- a create whose response timed out and that landed after `Release`;
- a driver that kept creating after losing its Lease.

The reaper sweeps them with the same `deletecollection` rounds, under two safeguards. First, it lists claims before Leases and re-reads the specific Lease just before deleting, so a batch created between the two lists isn't mistaken for an orphan. Second, it only sweeps a batch whose newest claim is older than `unleased_grace`, for example 10 minutes. In the reverse case, a stale Lease with no claims left (a driver that died partway through `Release`), the reaper deletes only the Lease.

#### Execution and Takeover

On each run the reaper lists, per namespace, the batch claims and Leases, and handles each stale Lease in three steps:

1. **Fence.** Update the Lease with `holderIdentity = reaper/<pod>`, `renewTime = now` and `leaseDurationSeconds = REAPER_HOLD`, conditioned on the resourceVersion it observed. A conflict means someone wrote the Lease in the meantime, so the batch is skipped for this run.
2. **Delete claims.** Issue label-scoped `deletecollection` rounds, re-listing with a consistent read until no claim is left that isn't already being deleted. These are the same steps `Release` uses.
3. **Delete the Lease last**, conditioned on the fence's resourceVersion.

The fence is what keeps everyone else safe:
- A driver that wakes up after the fence reads a foreign holder, reports `BatchInUseError`, and stops renewing.
- `GetBatch` raises `BatchInUseError` while the reaper holds the Lease and `BatchLeaseExpiredError` afterwards, so it can never adopt a batch that is being reaped.
- If two reapers overlap, only one fence write succeeds.

If a reaper dies partway through a batch, its own hold goes stale after `REAPER_HOLD`, and the next run fences again and carries on. Every step is a delete, so repeating it is harmless.

#### Coexistence with Renewal

A Lease has exactly one holder at a time. Every write to it is a read-modify-replace on the resourceVersion, so no writer can silently overwrite another. The writers are driver renewal, `Detach`, a `GetBatch` takeover and the reaper's fence.

When a renewal and a fence race, one of them gets a conflict. If the renewal wins, the reaper skips the batch; if the fence wins, the driver stops. Several processes renewing the same Lease is not supported; a handoff goes through `Detach(grace)` followed by `GetBatch`.

One overlap is expected and safe. `Release` stops renewing before it deletes, so releasing a large batch can take long enough for the Lease to go stale and be reaped at the same time. Both sides only delete, and `Release` treats an already-deleted Lease as success.

#### Deployment and Artifacts

The reaper ships as a Python module in the SDK, run as `python -m k8s_agent_sandbox.batch_reaper`. That way it shares the SDK's staleness check, batch-id validation and `Release`'s delete rounds instead of reimplementing the contract. The alternative is a Go binary under `cmd/`, which costs a new image and changes to release tooling. Either way it needs a published image, because installing the package on every CronJob run is too slow and fragile for production. Until that image exists, the example uses a stock Python image with a pinned install.

The artifacts live in `examples/batch-reaper/`:
- the CronJob, ServiceAccount, ClusterRole and a RoleBinding template;
- a README.

The CronJob runs every 1–2 minutes with `concurrencyPolicy: Forbid` and an `activeDeadlineSeconds` below `REAPER_HOLD`. The reaper takes `--namespaces`, `--skew-margin`, `--unleased-grace` and `--dry-run`. It logs one line per reaped batch and exits non-zero on errors, so failed runs show up as failed Jobs.

Unit tests cover the decision table with a fake clock. A kind e2e test checks three things: a SIGKILLed driver's batch is reaped after `D + margin`, a detach grace is honored, and a live batch is never touched.
