### Reaper Contract

The reaper is a stateless process that cleans up batches whose driver is gone. It relies on the Lease contract the SDKs already implement, plus the driver-side changes under "Driver Side" below. It reads the Lease's `renewTime`, `leaseDurationSeconds` (below, *D*) and resourceVersion. It never writes a Lease, only deletes one, and never touches the batch annotations. Until a reaper is deployed, each claim's `shutdownTime` is the only crash cleanup.

Decided on 2026-10-08: delete-first rather than a fence, a confirmation step implemented as a resourceVersion precondition, the driver expiry fix in PR 1, and a reaper branch based on PR 2.

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
  verbs: ["list", "get", "delete"]
- apiGroups: ["extensions.agents.x-k8s.io"]
  resources: ["sandboxclaims"]
  verbs: ["list", "deletecollection"]
```

RBAC can't restrict `deletecollection` by label, so the reaper enforces its scope in code. It only deletes claims with the selector `agents.x-k8s.io/batch-id=<id>`, for an id that passes batch-id validation, and never with an empty selector. It only touches Leases named `batch-<id>` whose label matches that id. It needs no `watch` (it only lists) and no `update` (it never writes a Lease).

#### Driver Side

The reaper's safety argument needs the driver to have given up before the reaper acts. Two changes to the renewal loop make that hold for every *D*. They land in PR 1, in both shells, because they complete PR 1's own Lease contract whether or not a reaper is deployed.

- **Renewal cadence is part of the contract.** A driver renews at least every `RenewInterval = max(1, D // 3)`, and stamps `renewTime` from its own clock before sending the renewal. The reaper's confirmation step (below) assumes this cadence, so the Go SDK and any other client must keep it.
- **Expiry is measured from when the last successful renewal was sent.** Today `_last_renew_success` is taken after the replace returns (`sandbox_batch.py:785`, async `:797`), which can be up to `RenewInterval` after the `renewTime` the server stored. Take the monotonic time just before stamping `renewTime` instead.
- **Expiry fires at a deadline, not at the end of a failed attempt.** Today expiry is checked only after a failed attempt (`:775`). Attempts are `RenewInterval` apart and each can take `RenewInterval` to time out, so expiry can fire about `5D/3` after the stored `renewTime`. Instead, the loop waits `min(RenewInterval, deadline − now)` and bounds each request by the time left, so expiry fires at `last_sent + D`.
- **On expiry the driver stops renewing.** It records `BatchLeaseExpiredError` and returns from the renewal loop. Today it keeps renewing, so a renewal that later succeeds keeps the Lease fresh while `err()` says expired, and the batch is neither usable nor reapable.
- **Creators already stop once `err()` is set** (PR 2, `_create_worker`). No change there.
- **Degraded stays degraded.** Once renewal stops, the degraded state stays true, since only a successful renewal clears it. A test owns this once `lease_degraded()` exists (PR 3).

#### Staleness and Clock Skew

The reaper considers a Lease stale when `now > renewTime + D + REAPER_SKEW_MARGIN`, with `now` taken from the reaper's own clock. The margin absorbs clock skew between the driver and the reaper. It is configurable, 30–60 s by default, and never below `CLOCK_SKEW_MARGIN`. The margin only ever adds waiting, because reaping early destroys running work while waiting only holds idle quota.

A stale Lease is deleted only if its resourceVersion is unchanged since the reaper read it at least one `RenewInterval` earlier (see Execution). This is the same idea as client-go leader election's `observedTime`. It is the only protection against a healthy driver whose clock runs behind by more than the margin, since that driver writes a `renewTime` that already looks stale but keeps changing the resourceVersion every `RenewInterval`.

This puts three thresholds in a deliberate order:
1. `GetBatch` stops adopting a Lease at `renewTime + D − CLOCK_SKEW_MARGIN`.
2. A driver that can't renew declares itself expired (`BatchLeaseExpiredError`) and stops renewing *D* after it sent its last successful renewal, measured on its own monotonic clock.
3. The reaper deletes only after `renewTime + D + REAPER_SKEW_MARGIN`, and only if the Lease hasn't changed for one `RenewInterval`.

As long as real skew stays below the margin, the driver has given up before the reaper deletes anything. A detached batch needs no special handling: `Detach(grace)` writes `renewTime = now` and `leaseDurationSeconds = grace`, so the reaper waits `grace` plus the margin. Worst-case crash cleanup is `D + margin + RenewInterval + CronJob period`, plus deletion time, which grows with batch size because `deletecollection` deletes claims one at a time.

#### Execution

Each run, per namespace:

1. **Discover.** List the batch claims, then the batch Leases (see "Cost per Run" for how). Claims go first because `ClaimBatch` creates its Lease before any claim, so any claim the first list sees has a Lease the second list also sees, unless it really is gone.
2. **Pick candidates.** A Lease is a candidate when it is stale by the test above. Record the resourceVersion read in step 1.
3. **Confirm.** Wait until at least one `RenewInterval` (from that Lease's *D*) has passed since step 1. Candidates in a namespace share one wait, the longest one needed.
4. **Delete the Lease first**, with a resourceVersion precondition set to the value from step 1. A 409 means someone wrote the Lease in the meantime (a renewal, `Detach`, or a takeover), so the batch is skipped for this run. A 404 means someone else (`Release`, another reaper) deleted it, so go on to step 5.
5. **Delete the claims.** Issue label-scoped `deletecollection` rounds, re-listing with a consistent read until no claim is left that isn't already being deleted. These are the same steps `Release` uses, so the reaper calls `_delete_batch_objects` (PR 2), whose final Lease delete treats the already-deleted Lease as success.

Once the Lease is gone, everyone else sees the right error:
- A driver that wakes up reads no Lease, reports `BatchLeaseExpiredError`, and stops renewing.
- `GetBatch` raises `BatchLeaseExpiredError` (Lease missing while claims exist), so it can never adopt a batch that is being reaped.
- If two reapers overlap, only one precondition delete succeeds. The other gets a 404 or 409, and both only delete claims afterwards.
- A `ClaimBatch` reusing the same id creates a new Lease, then finds the old claims (terminating ones included) and raises `BatchExistsError`, deleting its own Lease again.

If a reaper dies between steps 4 and 5, the claims are left without a Lease, and the next run sweeps them as unleased claims (below). Every step is a delete, so repeating it is harmless.

#### Sweeping Unleased Claims

`ClaimBatch` creates the Lease before any claim, and `Release` deletes it last. So a claim with a batch-id label but no Lease never belongs to a batch that is still being created. Such claims come only from:
- a reaper that died after deleting the Lease;
- a Lease deleted by hand;
- a create whose response timed out and that landed after `Release` or after the reaper;
- a driver that kept creating after losing its Lease.

The reaper sweeps them with the same `deletecollection` rounds, under two safeguards. First, it lists claims before Leases and re-reads the specific Lease just before deleting, so a batch created between the two lists isn't mistaken for an orphan. Second, it only sweeps a batch whose newest claim is older than `unleased_grace`, for example 10 minutes. The age is the newest claim's `creationTimestamp`, not the time since the Lease went away, so a batch left behind by a dead reaper is usually swept on the next run. Only a batch whose last create was within `unleased_grace` waits longer.

In the reverse case, a stale Lease with no claims left (a driver that died partway through `Release`), the reaper deletes only the Lease, with the same confirmation and precondition.

#### Coexistence with Renewal

A Lease has exactly one holder at a time. Every write to it is a read-modify-replace on the resourceVersion, so no writer can silently overwrite another. The writers are driver renewal, `Detach` and a `GetBatch` takeover. The reaper is not a writer. Its only Lease operation is a delete conditioned on a resourceVersion, so any write between its read and its delete makes the delete fail and the batch is skipped.

Several processes renewing the same Lease is not supported; a handoff goes through `Detach(grace)` followed by `GetBatch`.

One overlap is expected and safe. `Release` stops renewing before it deletes, so releasing a large batch can take long enough for the Lease to go stale and be reaped at the same time. Both sides only delete, and `Release` treats an already-deleted Lease as success.

#### Cost per Run

Each run lists the batch Leases (O(B)) and every batch claim (O(N)) in each bound namespace. It keeps no watch or cache between runs. The claim list is the expensive part, since the unleased sweep needs every claim with a batch-id label, and at tens of thousands of claims it would be a large read every minute or two. Three things keep it cheap:

- **Discovery reads from the apiserver's watch cache.** The discovery lists pass `resourceVersion="0"`, so the apiserver answers from its in-memory cache instead of reading etcd. A slightly old answer is harmless here: a claim the cache hasn't seen yet is picked up next run, and every delete is guarded by the precondition, the Lease re-read, or `unleased_grace`.
- **Discovery is paged** (`limit` and `continue`), so no single response holds every claim. Whether a paged list can also be served from the cache depends on the apiserver version, so measure this on the target cluster.
- **Consistent reads only where a decision needs them.** The "no claims left" check while deleting one batch uses a consistent read (no resourceVersion), as `Release` does, and it is scoped to that batch's label.

The reaper only needs each claim's labels, `creationTimestamp` and `deletionTimestamp`, so a metadata-only list (`PartialObjectMetadataList`) would cut the payload further. Measure before building it, since the Python client needs a raw `call_api` for it.

`_delete_batch_objects` is not paginated yet. Thread A's review recommends paginating its lists in PR 7 (performance), after measuring. If the reaper lands before that, it inherits the unpaginated per-batch lists (one batch at a time, so bounded by batch size, not by N).

#### Deployment and Artifacts

The reaper ships as a Python module in the SDK, run as `python -m k8s_agent_sandbox.batch_reaper`. That way it shares the SDK's staleness check, batch-id validation and `Release`'s delete rounds instead of reimplementing the contract. The alternative is a Go binary under `cmd/`, which costs a new image and changes to release tooling. Either way it needs a published image, because installing the package on every CronJob run is too slow and fragile for production. Until that image exists, the example uses a stock Python image with a pinned install. Where it lives and how the image is published need an upstream discussion first (AGENTS.md: no new top-level directories without one).

The artifacts live in `examples/batch-reaper/`:
- the CronJob, ServiceAccount, ClusterRole and a RoleBinding template;
- a README.

The CronJob runs every 1–2 minutes with `concurrencyPolicy: Forbid`. A run lasts at least the longest `RenewInterval` among its candidates (the confirmation wait), plus deletion time, so `activeDeadlineSeconds` must cover that. A run that is cut short is harmless, since every step is a delete and the next run starts over. The reaper takes `--namespaces`, `--skew-margin`, `--unleased-grace` and `--dry-run`. It logs one line per reaped batch and exits non-zero on errors, so failed runs show up as failed Jobs.

The reaper PR is a branch based on PR 2 (`feat/batch-2-claim`), not stacked on PR 4, because it needs only PR 1's staleness check and batch-id validation and PR 2's `_delete_batch_objects`. It doesn't renumber the stack, and it can land whenever upstream agrees on its location. The driver-side changes above are not part of it; they go into PR 1.

#### Tests

Unit tests cover the decision table with a fake clock: fresh, stale by clock but renewed during the confirmation wait (409, skipped), stale and unchanged (deleted), detached within grace, stale with no claims, unleased claims younger and older than `unleased_grace`, and an invalid batch id (never deleted). The driver-side changes each get one owning test per shell: expiry fires at `last_sent + D` when every renewal hangs, and renewal stops after expiry.

A kind e2e test checks three things: a SIGKILLed driver's batch is reaped after `D + margin + RenewInterval`, a detach grace is honored, and a live batch is never touched.

#### Proposal Changes

Edits to `batch_claim_proposal.md` so it matches this contract. Applied to the proposal on 2026-10-08.

1. **Liveness.** Append after "We use the Lease in conjunction with a new stateless reaper…":

   > The reaper runs every one to two minutes. In each namespace it is bound to, it lists the batch Leases and batch claims, and deletes the Lease and then the claims of any batch whose Lease has gone stale (Cleanup, path 2). It also deletes batch claims that have no Lease at all, once the newest of them is old enough that it can't belong to a batch that is still being created. Without the reaper, a driver that dies without running any code (`SIGKILL`, OOM kill, node preemption) never calls `Release`, and its claims hold their sandboxes until the Shutdown Backstop below.
   >
   > The reaper waits rather than risk deleting live work. The driver renews the Lease every `RenewInterval`, a third of `LeaseDuration`. If no renewal succeeds for a full `LeaseDuration`, counted from when the last successful one was sent, the Lease is expired: the driver stops renewing and stops starting new claim creates, and `err()` reports `BatchLeaseExpiredError`. The reaper treats a Lease as stale only after `renewTime + LeaseDuration` plus its own clock-skew margin, so a driver that has lost its Lease has already stopped by the time the reaper acts, and `GetBatch` no longer re-attaches the batch. The reaper also deletes a Lease only if it hasn't changed since the reaper read it at least one `RenewInterval` earlier, so a healthy driver whose clock runs behind is never mistaken for a dead one. Because the reaper deletes the Lease before the claims, a driver or `GetBatch` that looks afterwards finds no Lease and reports `BatchLeaseExpiredError`. A detached batch needs no special case, because `Detach` sets the Lease to go stale once its grace period ends. `Release` stops renewing before it deletes and never writes the Lease, so a reaper that runs during a slow `Release` doesn't interfere with it.

   Line 97 changes under thread A's recommendation 2 (`lease_degraded()` instead of a `LeaseDegraded` event). That edit owns line 97, so this one doesn't touch it.
2. **Cleanup path 2 (`:112`).** "the reaper issues the same label-scoped `deletecollection` to delete the claims, then delete the Lease" becomes "the reaper deletes the Lease, then issues the same label-scoped `deletecollection` to delete the claims".
3. **`get_batch` signature (`:271`).** Remove `adopt_expired`. It would let `GetBatch` adopt a batch the reaper is deleting, and the code never had it.
4. **Diagram (`:141`).** "LeaseDuration + poll period" becomes "LeaseDuration + margin + RenewInterval + poll period".
5. **Reaper RBAC (`:170-184`).** Lease verbs become `get`, `list`, `delete` (no `watch`, no `update`). Add one sentence that the ClusterRole is granted with a RoleBinding in each covered namespace, never a ClusterRoleBinding, because RBAC can't limit `deletecollection` to batch claims.
6. **Scalability (`:681-682`).** Replace the reaper's "cache memory and watch event volume" bullet with "Each reaper run lists the batch Leases (O(B)) and the batch claims (O(N)) in each bound namespace, paged and from the apiserver's watch cache; it keeps no watch or cache between runs."

Everything else that mentions the reaper (`:18`, `:43`, `:103`, `:511`, `:691`, `:697-704`) is still accurate.

#### Open Questions

- **Location and image** (see Deployment). Raise upstream before the reaper PR.
- **Per-run namespaces.** Agent Sandbox RL's `run_isolation="namespace"` creates a namespace per run, which a reaper bound to a fixed `--namespaces` list doesn't cover unless each run also creates a RoleBinding for the reaper. Creating one needs `bind`, or the reaper's own permissions, which is worth raising upstream. The RL library's own run-scoped reaper (`agent_sandbox_rl/reaper.py`) also cleans up the warm pools, templates and namespaces a run creates, which this reaper never touches.
- **`client.delete_batch(batch_id, namespace)`** (carryover notes, "Ideas raised but not planned"). If built, it uses the same steps and order as the reaper: precondition delete of the Lease, then the claims.
