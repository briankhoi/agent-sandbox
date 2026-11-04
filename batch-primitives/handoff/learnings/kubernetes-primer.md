<a id="sec-primer"></a>

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)

# Kubernetes primer: the concepts this feature taught us

The batch feature runs entirely in the client: it adds no new controller, so all of its safety comes from building blocks the Kubernetes API already provides. This section walks through those blocks in order, starting with "what is an object" and ending with the server-side bookkeeping we had to work around. Each card says what the concept is, where the batch uses it, the trap we hit, and a small snippet to try. The field-guide ids (like `LEASE-4`) point to the deeper edge cases.

| Concept | One-line meaning | Where the batch uses it | Field-guide entries |
| --- | --- | --- | --- |
| Custom resources and namespaces | Your own object types, served by the apiserver like built-in ones | Claims, warm pools, templates via `CustomObjectsApi` | [API-3](field-guide/api.md#api-3), [PERF-10](field-guide/perf.md#perf-10), [API-28](field-guide/api.md#api-28) |
| Names and UIDs | Name = reusable key; UID = identity that is never reused | `<id>-<ordinal>` claim names; UID checks on the Lease | [K8S-13](field-guide/k8s.md#k8s-13), [API-5](field-guide/api.md#api-5), [K8S-2](field-guide/k8s.md#k8s-2), [LEASE-17](field-guide/lease.md#lease-17) |
| Labels and selectors | Short indexed tags the server can filter on | `batch-id` and `batch-group` scope watch and release | [K8S-14](field-guide/k8s.md#k8s-14), [API-24](field-guide/api.md#api-24), [K8S-8](field-guide/k8s.md#k8s-8), [K8S-19](field-guide/k8s.md#k8s-19) |
| Annotations | Unindexed strings for small durable notes | Group sizes on claims; settings and next ordinal on the Lease | [LEASE-26](field-guide/lease.md#lease-26), [LEASE-30](field-guide/lease.md#lease-30), [LEASE-25](field-guide/lease.md#lease-25), [API-26](field-guide/api.md#api-26) |
| resourceVersion and 409 Conflict | Compare-and-swap for writes | Every Lease write is a PUT carrying the last-seen version | [LEASE-2](field-guide/lease.md#lease-2), [PERF-8](field-guide/perf.md#perf-8), [LEASE-28](field-guide/lease.md#lease-28), [K8S-6](field-guide/k8s.md#k8s-6) |
| Delete preconditions | "Delete only if it is still version X" | Final Lease delete; reaper delete | [K8S-1](field-guide/k8s.md#k8s-1), [LEASE-8](field-guide/lease.md#lease-8), [LEASE-18](field-guide/lease.md#lease-18) |
| List, then watch | Gap-free tracking of a set of objects | The batch watch loop | [WATCH-1](field-guide/watch.md#watch-1), [WATCH-2](field-guide/watch.md#watch-2), [RETRY-3](field-guide/retry.md#retry-3), [LIB-11](field-guide/lib.md#lib-11) |
| Bookmarks and 410 Gone | Keep a quiet watch fresh; re-list when history is gone | `allow_watch_bookmarks=True`; 410 branch re-lists | [K8S-10](field-guide/k8s.md#k8s-10), [WATCH-3](field-guide/watch.md#watch-3) |
| Paging (limit/continue) | Big lists in chunks from one snapshot | Claim lists and counts, reaper Lease list | [K8S-9](field-guide/k8s.md#k8s-9), [PERF-4](field-guide/perf.md#perf-4), [PERF-5](field-guide/perf.md#perf-5) |
| Consistent reads | A list with no resourceVersion reflects the latest state | "Are any claims left?" during release | [K8S-5](field-guide/k8s.md#k8s-5) |
| Lease object | Tiny "I'm alive and I hold X" record | `batch-<id>` liveness and ownership | [K8S-16](field-guide/k8s.md#k8s-16), [K8S-15](field-guide/k8s.md#k8s-15), [LEASE-1](field-guide/lease.md#lease-1), [LEASE-3](field-guide/lease.md#lease-3) |
| Leader election vs our Lease | Same fields, different promise | Three ordered staleness thresholds | [LEASE-4](field-guide/lease.md#lease-4), [LEASE-5](field-guide/lease.md#lease-5), [LEASE-9](field-guide/lease.md#lease-9), [LEASE-6](field-guide/lease.md#lease-6) |
| ownerReferences and GC | Dependents die when their owner (matched by UID) is gone | Every claim is owned by the batch Lease | [CLEAN-14](field-guide/clean.md#clean-14), [K8S-2](field-guide/k8s.md#k8s-2), [K8S-3](field-guide/k8s.md#k8s-3), [LEASE-7](field-guide/lease.md#lease-7) |
| Deletion lifecycle | A deleted object can linger as "terminating" | Terminating claim = LOST | [K8S-4](field-guide/k8s.md#k8s-4), [WATCH-7](field-guide/watch.md#watch-7) |
| deletecollection | One DELETE for every object matching a selector, not atomic | `release()` rounds | [RACE-7](field-guide/race.md#race-7), [RACE-8](field-guide/race.md#race-8), [RETRY-11](field-guide/retry.md#retry-11), [PERF-7](field-guide/perf.md#perf-7) |
| Status conditions | Controller reports as `{type, status, reason}` | Claim `Ready` condition, terminal reason allowlist | [K8S-11](field-guide/k8s.md#k8s-11), [K8S-12](field-guide/k8s.md#k8s-12) |
| Status mirroring | Controller copies useful facts onto the watched object | Pod IPs, service FQDN, warm pool `readyReplicas` | [WATCH-8](field-guide/watch.md#watch-8), [CLEAN-9](field-guide/clean.md#clean-9), [RETRY-15](field-guide/retry.md#retry-15) |
| RBAC | Who may do which verb where | Driver Role; reaper ClusterRole bound per namespace | [K8S-20](field-guide/k8s.md#k8s-20), [LEASE-10](field-guide/lease.md#lease-10) |
| APF, 429, Retry-After | Server-wide throttling and "wait this long" hints | Shared create backoff, warm credit cooldown | [RETRY-6](field-guide/retry.md#retry-6), [RETRY-16](field-guide/retry.md#retry-16), [LIB-7](field-guide/lib.md#lib-7), [LIB-17](field-guide/lib.md#lib-17) |
| Request timeouts | Server ends calls at 60s; the Python client has no default | 30s per call, 90s for collections, Lease-capped renewals | [RETRY-8](field-guide/retry.md#retry-8), [LIB-10](field-guide/lib.md#lib-10), [LIB-9](field-guide/lib.md#lib-9), [RETRY-9](field-guide/retry.md#retry-9) |
| CronJob and namespace discovery | Run a one-shot Pod on a schedule | The batch reaper | [CLEAN-23](field-guide/clean.md#clean-23), [LEASE-8](field-guide/lease.md#lease-8) |
| shutdownTime | Server-enforced expiry on a claim | Backstop on every batch claim | [LEASE-27](field-guide/lease.md#lease-27), [COUNT-12](field-guide/count.md#count-12) |
| managedFields | Server-side apply bookkeeping on every object | Dropped from listed claims to save memory | [PERF-6](field-guide/perf.md#perf-6) |

---

## Custom resources, CustomObjectsApi, and namespaces

A custom resource is your own object type added to the Kubernetes API. Once its CRD is installed, the apiserver stores, lists, and watches it like a built-in object. You name an object type by group, version, and plural, and a namespaced object also needs a namespace.

**How the batch uses it:** `SandboxClaim`, `SandboxWarmPool`, and `SandboxTemplate` have no typed Python classes, so the SDK calls `CustomObjectsApi` with the constants in `constants.py: CLAIM_API_GROUP, CLAIM_PLURAL_NAME, WARMPOOL_PLURAL_NAME, TEMPLATE_PLURAL_NAME`. A batch lives in one namespace: its claims, its Lease, and its warm pools. Before writing anything, `sandbox_batch.py: _precheck_pools` (16/N) GETs each warm pool and its template through `k8s_helper.py: K8sHelper.get_sandbox_warmpool / get_sandbox_template` (12/N).

**Gotcha:** responses are plain dicts, so every read is defensive, for example `(obj.get("metadata") or {}).get("name")` in `batch_state.py: reconstruct_groups`. Because the precheck runs before the Lease exists, a typo in a pool name fails fast and leaves nothing behind ([API-3](field-guide/api.md#api-3), [PERF-10](field-guide/perf.md#perf-10)).

**Try it:**
```python
api.list_namespaced_custom_object(
    group="extensions.agents.x-k8s.io", version="v1beta1",
    namespace="team-a", plural="sandboxclaims",
    label_selector="agents.x-k8s.io/batch-id=b1x2y3z4k5m6")
# kubectl get sandboxclaims -n team-a
```

**Read more:** [Custom Resources](https://kubernetes.io/docs/concepts/extend-kubernetes/api-extension/custom-resources/), [Namespaces](https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/), [Python CustomObjectsApi](https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/CustomObjectsApi.md)

## Object names (DNS-1123) and UIDs

Most resource types accept DNS-1123 subdomain names (up to 253 characters), and some, such as Namespaces and Services, require the stricter DNS-1123 label: lowercase letters, digits, and `-`, starting and ending with an alphanumeric, at most 63 characters. The apiserver also gives every object a UID that is never reused. If you delete an object and create a new one with the same name, the new one has a different UID.

**How the batch uses it:** claims are named `<batch-id>-<ordinal>`. `batch_utils.py: validate_batch_id` (1/N) caps the id at 52 characters, which leaves room for `-` plus up to 10 ordinal digits. `generate_batch_id` (12/N) returns `b` plus 11 random characters, so it always starts with a letter. The Lease is `batch-<id>` (`constants.py: BATCH_LEASE_NAME_PREFIX`).

**Gotcha:** `re.match` with `$` accepts a trailing newline, so `"b1\n"` passed until 12/N switched to `fullmatch` ([API-5](field-guide/api.md#api-5)). Also, "same name" does not mean "same object": `sandbox_batch.py: _take_batch_lease` (12/N) refuses to act if the Lease UID differs from the one this handle last wrote ([LEASE-17](field-guide/lease.md#lease-17), [K8S-2](field-guide/k8s.md#k8s-2)). Budget the 63 characters for every object a name flows into ([K8S-13](field-guide/k8s.md#k8s-13)).

**Try it:**
```python
validate_batch_id("train-step-7")  # ok -> claims train-step-7-0, train-step-7-1, ...
validate_batch_id("7-train")       # ValueError: must start with a letter
validate_batch_id("b1\n")          # ValueError (fullmatch)
```

**Read more:** [Object Names and IDs](https://kubernetes.io/docs/concepts/overview/working-with-objects/names/)

## Labels and label selectors

A label is a key/value tag the apiserver can filter on. One list, watch, or deletecollection call can ask for "every object with `batch-id=X`". Label values are at most 63 characters of alphanumerics, `-`, `_`, and `.`. A selector can also test only that a key exists.

**How the batch uses it:** `constants.py: BATCH_ID_LABEL` (1/N) scopes the whole batch for the watch, `release()`, and `get_batch`. `BATCH_GROUP_LABEL` (15/N) scopes one pool's claims for `release_group`, built by `batch_utils.py: group_label_selector`. The reaper finds batch Leases with the key-only selector `agents.x-k8s.io/batch-id` in `batch_reaper.py: find_stale_leases` (18/N).

**Gotcha:** warm pool names can be up to 253 characters, too long for a label value. `batch_utils.py: group_label_value` (15/N) stores the first 16 hex characters of a SHA-256 hash. A collision would make `release_group` delete another pool's claims, so 64 bits was chosen to make that practically impossible ([K8S-14](field-guide/k8s.md#k8s-14)). Callers can't set the two reserved keys, because that would hijack the delete scope (`validate_claim_batch_args`, 12/N, group key 15/N; [API-24](field-guide/api.md#api-24)).

**Try it:**
```bash
kubectl get sandboxclaims -l agents.x-k8s.io/batch-id=b1x2y3z4k5m6
kubectl get leases -l agents.x-k8s.io/batch-id      # key exists, any value
```

**Read more:** [Labels and Selectors](https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/)

## Annotations as durable small state

Annotations are key/value strings that the server stores but never filters on. They suit "notes to my future self" that are too long or too free-form for labels.

**How the batch uses it:** initial-fill claims carry `batch-group-size` and `batch-group-min-ready`, so `batch_state.py: reconstruct_groups` (1/N) can rebuild the groups after `get_batch`. The Lease carries the configured duration, quorum timeout, work budget, and create rate (`batch_utils.py: batch_lease_metadata`, 12/N), plus `batch-next-ordinal`, which `detach()` writes (16/N) so a re-attached handle never reuses a claim name.

**Gotcha:** anyone can edit an annotation. `parse_lease_metadata` and `reconstruct_groups` reject garbage and conflicting values with `BatchError` instead of guessing ([LEASE-25](field-guide/lease.md#lease-25), [API-26](field-guide/api.md#api-26)). The duration annotation exists because `detach(grace)` overwrites `spec.leaseDurationSeconds` with the grace ([LEASE-30](field-guide/lease.md#lease-30)). `acquire()` claims get the group label but not the group annotations, which keeps them out of fill reconstruction.

**Try it:**
```yaml
metadata:
  annotations:
    agents.x-k8s.io/batch-group-size: "8"
    agents.x-k8s.io/batch-group-min-ready: "6"
```

**Read more:** [Annotations](https://kubernetes.io/docs/concepts/overview/working-with-objects/annotations/)

## resourceVersion and optimistic concurrency (409 Conflict, PUT vs PATCH)

`resourceVersion` is an opaque string that changes on every write to an object. A PUT (replace) sends the whole object, including the version you last saw. If someone wrote first, the server answers 409 Conflict and changes nothing. That gives you compare-and-swap without locks. A merge patch without a `resourceVersion` has no such check, so the last writer silently wins.

**How the batch uses it:** `k8s_helper.py: K8sHelper.replace_batch_lease` (1/N) always uses PUT. Renewal sends back its own last successful write without reading first, and reads only on a 409 (`SandboxBatch._renew_once`, 3/N; [PERF-8](field-guide/perf.md#perf-8)). A 409 on attach means someone else holds it (`_attach` raises `BatchInUseError`, 1/N).

**Gotcha:** a renewal 409 can mean another holder took over, or that one of our own earlier writes landed after its response was lost. `_renew_once` re-reads and checks `holderIdentity` to tell these apart ([LEASE-28](field-guide/lease.md#lease-28)). 409 also means AlreadyExists on a create, a different case: on a retry it usually means attempt 1 landed (`batch_utils.py: create_error_outcome`, 12/N; [K8S-6](field-guide/k8s.md#k8s-6), [RACE-4](field-guide/race.md#race-4)).

**Try it:**
```python
lease = coord.read_namespaced_lease("batch-b1", ns)    # rv="1234"
lease.spec.renew_time = lease_now()
coord.replace_namespaced_lease("batch-b1", ns, lease)  # 200, rv="1240"
coord.replace_namespaced_lease("batch-b1", ns, lease)  # 409: rv 1234 is stale
```

**Read more:** [API concepts: resource versions](https://kubernetes.io/docs/reference/using-api/api-concepts/#resource-versions), [API conventions: concurrency control](https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md#concurrency-control-and-consistency)

## Preconditions on DELETE

A DELETE can carry `preconditions` with a `resourceVersion` or `uid`. If the object changed since you read it, the server returns 409 and keeps it. That turns "I looked, it was stale, now delete it" into one atomic step.

**How the batch uses it:** `K8sHelper.delete_batch_lease(resource_version=...)` (12/N) treats a 409 as "someone wrote it since, leave it". `release()` deletes the Lease only at the version its own last take wrote (`_delete_batch_objects`, 12/N; [LEASE-18](field-guide/lease.md#lease-18)). The reaper deletes with the listed version (`batch_reaper.py: _delete_lease_if_unchanged`, 18/N).

**Gotcha:** a live driver whose clock runs behind writes a `renewTime` that already looks stale to the reaper. The reaper waits one renew interval and then deletes with a precondition, so a live driver's renewal turns the delete into a 409 ([LEASE-8](field-guide/lease.md#lease-8), [K8S-1](field-guide/k8s.md#k8s-1)). The 18/N e2e test pauses a driver with SIGSTOP and checks nothing is reaped.

**Try it:**
```python
coord.delete_namespaced_lease("batch-b1", ns, body=client.V1DeleteOptions(
    preconditions=client.V1Preconditions(resource_version="1240")))
# 409 -> renewed or taken over since we read it; leave it alone
```

**Read more:** [DeleteOptions](https://kubernetes.io/docs/reference/kubernetes-api/common-definitions/delete-options/)

## List, then watch from the list's resourceVersion

A list returns objects plus a collection `resourceVersion`, meaning "this is the state as of version V". A watch from V streams every ADDED, MODIFIED, and DELETED change after V. Together they give a complete view with no gap. This pair is the core of client-go's informers.

**How the batch uses it:** `K8sHelper.list_sandbox_claim_objects` returns `(items, resourceVersion)` (1/N), and the watch starts there (`SandboxBatch._claim`, 12/N). `K8sHelper.watch_sandbox_claims` passes `timeout_seconds` so the server ends each stream cleanly, and `_watch_loop` resumes from the last version it saw.

**Gotcha:** the client read timeout (`_WATCH_REQUEST_TIMEOUT` = 35s) is longer than the server's `timeoutSeconds` (30s), so the server ends a healthy stream first and the client timeout only catches dead connections ([RETRY-8](field-guide/retry.md#retry-8)). A watch that ends at once with no events would make the loop spin, so 12/N counts it as a failure and backs off ([RETRY-3](field-guide/retry.md#retry-3)). Start the watch before anything writes ([WATCH-2](field-guide/watch.md#watch-2)), and turn off library auto-reconnects when you track position yourself ([LIB-11](field-guide/lib.md#lib-11)).

**Try it:**
```python
items, rv = helper.list_sandbox_claim_objects(ns, "agents.x-k8s.io/batch-id=b1")
for ev in helper.watch_sandbox_claims(ns, "agents.x-k8s.io/batch-id=b1", rv, 30):
    rv = ev["object"]["metadata"]["resourceVersion"]   # resume point
```

**Read more:** [API concepts: efficient detection of changes](https://kubernetes.io/docs/reference/using-api/api-concepts/#efficient-detection-of-changes), client-go [reflector.go](https://github.com/kubernetes/client-go/blob/master/tools/cache/reflector.go)

## Keeping a watch fresh: bookmarks and 410 Gone

The apiserver keeps only a window of recent history. If you try to resume a watch from a version older than that window, you get 410 Gone, and the only fix is a fresh list. A BOOKMARK event carries only a newer `resourceVersion`, so a quiet watch stays inside the window. The server sends bookmarks only if you ask with `allowWatchBookmarks=true`, and even then only on a best-effort basis.

**How the batch uses it:** `K8sHelper.watch_sandbox_claims` passes `allow_watch_bookmarks=True` (1/N), and `_watch_loop` updates its version and then skips the bookmark's object. On a 410, `_watch_loop` re-lists and calls `BatchState.resync_from_list` (1/N), and cached connections whose member changed are closed (`_pop_stale_connections`, 17/N).

**Gotcha:** an early revision handled BOOKMARK but never opted in, so a quiet batch would 410 and re-list every claim once its version aged out ([K8S-10](field-guide/k8s.md#k8s-10)). A 410 isn't transient: retrying the same version fails forever, so the loop checks 410 before the generic retry check. Deletions during the gap show up only as missing items, never as DELETED events, so the resync must mark them LOST itself ([WATCH-3](field-guide/watch.md#watch-3)).

**Try it:**
```text
t0 watch from rv=100 ... connection drops for 10 minutes
t1 reconnect rv=100 -> 410 Gone
t2 list -> items + rv=5000; resync marks missing claims LOST
t3 watch from rv=5000
```

**Read more:** [API concepts: watch bookmarks](https://kubernetes.io/docs/reference/using-api/api-concepts/#watch-bookmarks), [KEP-956](https://github.com/kubernetes/enhancements/tree/master/keps/sig-api-machinery/956-watch-bookmark)

## Paging large lists (limit and continue)

Pass `limit=N` and the server returns at most N items plus a `continue` token for the next page. Every page comes from the same snapshot, so the first page's `resourceVersion` describes the whole list. The token expires after a short time (5 minutes by default) and then fails with 410.

**How the batch uses it:** `k8s_helper.py: _claim_list_pages` (20/N) pages at `CLAIM_LIST_PAGE_SIZE` = 500 for both `list_sandbox_claim_objects` and `count_live_sandbox_claims`. The reaper pages Leases at 500 too (`batch_reaper.py: _LEASE_LIST_PAGE_SIZE`, 18/N).

**Gotcha:** 20/N measured an unpaged attach to a 5,000-claim batch at a 69.4 MB peak (about 23 MB of response bytes on top of 46 MB of parsed claims). A page can be empty while more follow, so only a missing token ends the loop ([PERF-4](field-guide/perf.md#perf-4)). On a 410 mid-list the code restarts once from scratch and drops the pages it gathered, because a watch start needs one consistent snapshot ([K8S-9](field-guide/k8s.md#k8s-9)). When you need only a count, fold it page by page ([PERF-5](field-guide/perf.md#perf-5)).

**Try it:**
```python
page = api.list_namespaced_custom_object(..., limit=500)
while page["metadata"].get("continue"):
    page = api.list_namespaced_custom_object(
        ..., limit=500, _continue=page["metadata"]["continue"])
```

**Read more:** [API concepts: retrieving large results sets in chunks](https://kubernetes.io/docs/reference/using-api/api-concepts/#retrieving-large-results-sets-in-chunks)

## Consistent reads vs the watch cache

A list with no `resourceVersion` must reflect the latest committed state. A list with `resourceVersion="0"` may be answered from the apiserver's in-memory cache, which is cheaper but can lag. Newer apiservers (beta and on by default since v1.31) can serve consistent reads from the cache once they prove it has caught up (KEP-2340), but the guarantee stays the same.

**How the batch uses it:** `release()` decides "no claims left, delete the Lease" from `count_live_sandbox_claims`, a consistent read (`_delete_claims_in_rounds`; a consistent re-list since 12/N, counted page by page since 20/N). The claim list (1/N) and the reaper's Lease list (18/N) also pass no `resourceVersion`.

**Gotcha:** a lagging cache could report "none left" while claims still exist, and the Lease would go too early ([K8S-5](field-guide/k8s.md#k8s-5)). The design notes say the reaper lists with `resourceVersion="0"`, but the code doesn't. Either would be safe for the reaper, since every delete carries a precondition.

**Try it:**
```python
api.list_namespaced_custom_object(..., label_selector=sel)                        # consistent
api.list_namespaced_custom_object(..., label_selector=sel, resource_version="0")  # may lag
```

**Read more:** [API concepts: semantics for get and list](https://kubernetes.io/docs/reference/using-api/api-concepts/#semantics-for-get-and-list), [KEP-2340](https://github.com/kubernetes/enhancements/tree/master/keps/sig-api-machinery/2340-Consistent-reads-from-cache)

## The coordination.k8s.io/v1 Lease

A Lease is a tiny object with `holderIdentity`, `leaseDurationSeconds`, `acquireTime`, `renewTime`, and `leaseTransitions`. Kubernetes enforces nothing about it; clients agree on a protocol. The holder renews `renewTime` regularly, and others treat the Lease as expired once `renewTime + leaseDurationSeconds` has passed. Kubernetes itself uses Leases for node heartbeats and leader election.

**How the batch uses it:** each batch has one Lease, `batch-<id>`. `SandboxBatch._claim` creates it with `acquireTime = renewTime = now` and `leaseTransitions = 0` (12/N). `_attach` bumps `leaseTransitions` (1/N). `batch_utils.py: generate_holder_identity` (1/N) makes the holder unique per handle ([LEASE-3](field-guide/lease.md#lease-3)).

**Gotcha:** `renewTime` and `acquireTime` are MicroTime, which the apiserver parses with exactly six fractional digits. Python's `isoformat()` drops the fraction when the microsecond is 0, so `batch_utils.py: lease_now` bumps it to 1 ([K8S-15](field-guide/k8s.md#k8s-15)). `leaseTransitions` also answers "did anyone else hold this since?", so `_take_batch_lease` (12/N) checks it even when the holder looks empty ([LEASE-17](field-guide/lease.md#lease-17)). Create the Lease first and delete it last ([LEASE-1](field-guide/lease.md#lease-1)).

**Try it:**
```yaml
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata: {name: batch-b1x2y3z4k5m6, labels: {agents.x-k8s.io/batch-id: b1x2y3z4k5m6}}
spec: {holderIdentity: host_4242_ab12cd34, leaseDurationSeconds: 60,
       renewTime: "2026-11-04T13:00:00.000001Z", leaseTransitions: 0}
```

**Read more:** [Leases](https://kubernetes.io/docs/concepts/architecture/leases/), [Lease v1 API reference](https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/lease-v1/)

## Leader election vs our batch Lease

In client-go leader election, any candidate may take a Lease once it has looked unchanged for `leaseDuration` on the candidate's own clock. Our batch uses the same fields for a different promise: "a live driver owns this batch". An expired batch is never adopted. Only a cleanly detached Lease (holder cleared) can be attached.

**How the batch uses it:** three ordered thresholds. `get_batch` refuses once `now > renewTime + D - 5s` (`batch_utils.py: is_lease_stale`, `CLOCK_SKEW_MARGIN` = 5, 1/N). The holder gives up D seconds after it *sent* its last successful renewal, on its monotonic clock (`_renew_loop`, 3/N). The reaper acts only after `renewTime + D + margin` (default 60) and then one more unchanged renew interval (`batch_reaper.py: is_lease_reapable`, 18/N).

**Gotcha:** measure expiry from when the renewal was sent, not when the response came back, or the holder can believe it owns a Lease others see as expired ([LEASE-5](field-guide/lease.md#lease-5)). Subtract the margin to adopt and add it to destroy ([LEASE-4](field-guide/lease.md#lease-4)). Every writer and the reaper share the renew interval D/3 ([LEASE-9](field-guide/lease.md#lease-9)). Don't add "keep alive on failure" paths that defeat expiry ([LEASE-6](field-guide/lease.md#lease-6)).

**Try it:**
```text
D=60, renew every 20s, reaper margin=60
t=0    last successful renewTime
t=55   get_batch refuses (0 + 60 - 5)
t=60   holder: BatchLeaseExpiredError, stops renewing
t=120  reaper: stale -> waits 20s -> deletes if rv unchanged (t >= 140)
```

**Read more:** client-go [leaderelection.go](https://github.com/kubernetes/client-go/blob/master/tools/leaderelection/leaderelection.go), [Leases: leader election](https://kubernetes.io/docs/concepts/architecture/leases/)

## ownerReferences and the garbage collector

`metadata.ownerReferences` on a dependent names its owner by apiVersion, kind, name, and UID. The garbage collector (GC) in kube-controller-manager deletes dependents whose owner is gone. With the default background cascade, the owner disappears at once and the GC cleans up after. With `--cascade=orphan`, the references are removed and the dependents stay. The match is by UID, not name.

**How the batch uses it:** every fill and `acquire()` claim gets `batch_utils.py: lease_owner_reference` (18/N) pointing at the batch Lease. So the claims go away once the Lease does, whether `release()`, the reaper, or a hand-run `kubectl delete` removed it ([CLEAN-14](field-guide/clean.md#clean-14), [K8S-3](field-guide/k8s.md#k8s-3)).

**Gotcha:** a reference to a UID that doesn't exist counts as a missing owner, so use the UID the apiserver returned. For the same reason, nothing may recreate a missing Lease: a new Lease has a new UID ([LEASE-7](field-guide/lease.md#lease-7), [K8S-2](field-guide/k8s.md#k8s-2)). The reference sets neither `controller` nor `blockOwnerDeletion`. Setting `blockOwnerDeletion` would need `update` permission on `leases/finalizers` under the OwnerReferencesPermissionEnforcement admission plugin, which the driver Role doesn't grant. UID matching is also why the reaper deletes only the Lease: a label-scoped claim delete could hit a newer batch reusing the id, and the GC never can.

**Try it:**
```bash
kubectl delete lease batch-b1x2y3z4k5m6                   # claims GC'd in the background
kubectl delete lease batch-b1x2y3z4k5m6 --cascade=orphan  # claims kept until shutdownTime
```

**Read more:** [Owners and Dependents](https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/), [Garbage Collection](https://kubernetes.io/docs/concepts/architecture/garbage-collection/), [Use Cascading Deletion](https://kubernetes.io/docs/tasks/administer-cluster/use-cascading-deletion/), [OwnerReferencesPermissionEnforcement](https://kubernetes.io/docs/reference/access-authn-authz/admission-controllers/#ownerreferencespermissionenforcement)

## Deletion lifecycle: deletionTimestamp and finalizers

A DELETE on an object with finalizers, or under a foreground cascade, doesn't remove it at once. The apiserver sets `metadata.deletionTimestamp` and the object is "terminating". It vanishes, with a DELETED watch event, only after its finalizers are removed. Meanwhile its status can be a stale snapshot.

**How the batch uses it:** `batch_state.py: derive_member` (5/N) makes a terminating claim LOST (unless it already FAILED) and clears its pod IPs and FQDN. `K8sHelper.count_live_sandbox_claims` (20/N) skips terminating claims, so `release()` doesn't wait on objects already on their way out.

**Gotcha:** the claim controller stops reconciling a claim once it's being deleted, so its `Ready` condition can stay True until it's gone. Before 5/N, `members()` kept reporting such claims Ready with old pod IPs ([K8S-4](field-guide/k8s.md#k8s-4)). Always check `deletionTimestamp` before trusting status ([WATCH-7](field-guide/watch.md#watch-7)).

**Try it:**
```yaml
metadata:
  name: b1-3
  deletionTimestamp: "2026-11-04T13:01:02Z"   # -> Member(state=LOST)
status:
  conditions: [{type: Ready, status: "True"}]  # stale, ignored
```

**Read more:** [Finalizers](https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/), [Garbage Collection: cascading deletion](https://kubernetes.io/docs/concepts/architecture/garbage-collection/#cascading-deletion)

## deletecollection: label-scoped, non-atomic, point-in-time

`deletecollection` is its own RBAC verb. One request deletes every object matching a selector at that moment, item by item on the server. It isn't atomic: a timeout can leave some deleted and some not. An object created a moment later with the same label survives. The response lists every deleted object, which can be huge.

**How the batch uses it:** `K8sHelper.delete_sandbox_claims_by_label` (12/N) is called in rounds by `sandbox_batch.py: _delete_claims_in_rounds`. Each round first re-takes the Lease, and after it a consistent count checks progress. The code gives up after `BATCH_RELEASE_MAX_IDLE_ROUNDS` = 3 rounds in a row with no progress (progress-aware since 12/N, moved into `_delete_claims_in_rounds` in 15/N; [RETRY-11](field-guide/retry.md#retry-11), [LEASE-18](field-guide/lease.md#lease-18)).

**Gotcha:** a request can time out while the server keeps deleting, hence the re-count. The response body was the 69.5 MB peak of `release()` at 5,000 claims, so 20/N drains it raw without parsing ([PERF-7](field-guide/perf.md#perf-7), [LIB-13](field-guide/lib.md#lib-13)). A concurrent `acquire()` create can land after the snapshot, so in-flight creates are quiesced or cleaned up ([RACE-7](field-guide/race.md#race-7), [RACE-8](field-guide/race.md#race-8)).

**Try it:**
```bash
kubectl delete sandboxclaims -n team-a -l agents.x-k8s.io/batch-id=b1
```

**Read more:** [Authorization: request verbs](https://kubernetes.io/docs/reference/access-authn-authz/authorization/#determine-the-request-verb), apiserver [store.go](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/apiserver/pkg/registry/generic/registry/store.go) `DeleteCollection` "NOT atomic" comment

## Status conditions: Ready, terminal vs transient reasons

Controllers report state in `status.conditions`, a list of `{type, status, reason, message}`. `status` is True, False, or Unknown, and `reason` is a machine-readable CamelCase word. Base decisions on type, status, and reason, never on the message text.

**How the batch uses it:** `batch_state.py: derive_member` (1/N) makes a member READY only when `Ready=True` *and* `status.sandbox.name` is set ([K8S-11](field-guide/k8s.md#k8s-11)). It's FAILED only when `Ready=False` with a reason in `constants.py: TERMINAL_CLAIM_READY_REASONS`, which mirrors the claim controller.

**Gotcha:** not every `Ready=False` is final. `WarmPoolNotFound`, `TemplateNotFound`, `SandboxNotReady`, and others are retried by the controller, and treating them as failures would break groups that would have recovered. Unknown reasons mean "keep waiting" within your own timeout ([K8S-12](field-guide/k8s.md#k8s-12)).

**Try it:**
```yaml
conditions:
- {type: Ready, status: "False", reason: PodFailed}        # -> FAILED
- {type: Ready, status: "False", reason: SandboxNotReady}  # -> PENDING
```

**Read more:** [API conventions: typical status properties](https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md#typical-status-properties)

## Status mirroring: pod IPs, service FQDN, warm pool readyReplicas

Status is the controller's report of what's true right now, and it can lag by a reconcile. When a controller copies useful facts onto the object you already watch, you save a list or watch per child object.

**How the batch uses it:** the claim's `status.sandbox.podIPs` and `serviceFQDN` come from the watch (`derive_member`, 1/N). `connect` dials the watched pod IP (`select_pod_ip`, 17/N). `batch_utils.py: warmpool_ready_replicas` and `WarmCredit` (19/N) let creates that a pool's ready sandboxes cover skip pacing.

**Gotcha:** pod IPs get reused, even within one batch. `batch_state.py: still_reaches` (17/N) checks state, sandbox name, and IP together, and the watch closes cached connections when any changes ([WATCH-8](field-guide/watch.md#watch-8), [CLEAN-9](field-guide/clean.md#clean-9)). `readyReplicas` is a snapshot another client may consume first; the worst case is that some creates go out unpaced, as per-claim creates would ([RETRY-15](field-guide/retry.md#retry-15)).

**Try it:**
```yaml
status:
  sandbox: {name: sb-abc, podIPs: ["10.0.3.7", "fd00::7"],
            serviceFQDN: sb-abc.team-a.svc.cluster.local}
```

**Read more:** [Kubernetes objects: spec and status](https://kubernetes.io/docs/concepts/overview/working-with-objects/#object-spec-and-status)

## RBAC: Role, ClusterRole, and their bindings

RBAC rules grant verbs on resources in API groups. A Role lives in one namespace. A ClusterRole is cluster-scoped, but what it grants depends on the binding: a ClusterRoleBinding grants it everywhere, while a RoleBinding grants it only in that binding's namespace.

**How the batch uses it:** the README's `sandbox-batch-driver` Role (1/N; claim `create` and `deletecollection` added in 12/N, claim `delete` in 15/N) grants claim verbs, `get` on pools and templates, and `create/get/update/delete` on Leases. The `sandbox-batch-reaper` ClusterRole (18/N) grants only `list/get/delete` on Leases, bound with a RoleBinding per namespace.

**Gotcha:** RBAC can't say "only Leases named `batch-*`", and deleting a batch Lease cascades to its claims. A ClusterRoleBinding would expose other components' Leases, such as leader election in kube-system. So the README says never to use one, and `batch_reaper.py: _batch_id_of` checks both the name and the label in code ([K8S-20](field-guide/k8s.md#k8s-20), [LEASE-10](field-guide/lease.md#lease-10)).

**Try it:**
```bash
kubectl create rolebinding reaper -n team-a \
  --clusterrole=sandbox-batch-reaper --serviceaccount=ops:reaper
```

**Read more:** [Using RBAC Authorization](https://kubernetes.io/docs/reference/access-authn-authz/rbac/), [RBAC good practices](https://kubernetes.io/docs/concepts/security/rbac-good-practices/)

## API Priority and Fairness, 429, and Retry-After

API Priority and Fairness (APF) queues requests by priority level. When a level overflows, the apiserver rejects with 429 Too Many Requests, often with a `Retry-After` header (HTTP allows seconds or an HTTP date). A 429 from APF reflects load on the whole priority level, which many clients share, not just your connection.

**How the batch uses it:** `batch_utils.py: is_retryable_status` (1/N) treats 429 and 5xx as transient. `retry_after_seconds` (13/N) caps the hint at 30s, and one `Retry-After` moves the shared create cursor for every worker ([RETRY-6](field-guide/retry.md#retry-6)). `WarmCredit` pauses warm credit during a cooldown (19/N; [RETRY-16](field-guide/retry.md#retry-16)).

**Gotcha:** `urllib3.Retry` honours `Retry-After` on 413, 429, and 503 even without `status_forcelist`, and can sleep up to 6 hours inside one SDK call (the default cap in urllib3 2.8.0), far past a 60s Lease. 22/N sets `respect_retry_after_header=False` on `k8s_helper.py: _CLAIM_COLLECTION_RETRIES` ([LIB-7](field-guide/lib.md#lib-7), [LIB-8](field-guide/lib.md#lib-8)). Parse the header as ASCII digits only, since `isdigit()` accepts `"²"` ([LIB-17](field-guide/lib.md#lib-17)).

**Try it:**
```text
HTTP/1.1 429 Too Many Requests
Retry-After: 3
retry_after_seconds(err)  # 3.0 -> every create worker waits
```

**Read more:** [API Priority and Fairness](https://kubernetes.io/docs/concepts/cluster-administration/flow-control/), [urllib3 Retry](https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html#urllib3.util.Retry), [KEP-1040](https://github.com/kubernetes/enhancements/tree/master/keps/sig-api-machinery/1040-priority-and-fairness)

## Request timeouts: the apiserver's 60s and your own

kube-apiserver ends non-watch requests after `--request-timeout` (default 1m0s) with a 504. Watches are long-running and use `timeoutSeconds` instead. The official sync Python client (`kubernetes`) has no default client timeout, so a hung connection can block forever unless you pass `_request_timeout`; check the async client's default for your version (unconfirmed).

**How the batch uses it:** `batch_utils.py: BATCH_REQUEST_TIMEOUT_SECONDS` = 30 for ordinary calls and `BATCH_COLLECTION_REQUEST_TIMEOUT_SECONDS` = 90 for long lists and deletecollections (2/N). `SandboxBatch._renew_request_timeout` (3/N) caps each renewal by the time left before the Lease expires ([RETRY-9](field-guide/retry.md#retry-9)).

**Gotcha:** if your timeout is shorter than the server's, you see a bare read timeout and lose the server's answer. Set it just above 60s to get a clean, retryable 504 ([LIB-10](field-guide/lib.md#lib-10), [RETRY-8](field-guide/retry.md#retry-8)). A urllib3 timeout bounds each attempt, not the whole call, so retries multiply it ([LIB-9](field-guide/lib.md#lib-9)).

**Try it:**
```python
helper.delete_sandbox_claims_by_label(ns, sel, _request_timeout=90)  # server 504s near 60s
helper.read_batch_lease(name, ns, _request_timeout=30)
```

**Read more:** [kube-apiserver flags](https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/)

## CronJob for the reaper, and finding your own namespace

A CronJob creates a Job on a schedule, and the Job runs a Pod to completion. `concurrencyPolicy: Forbid` skips a run while the last one is still going, and `activeDeadlineSeconds` kills a run that takes too long. Inside a Pod, the downward API can expose the namespace as an env var, and service-account Pods also have it in a mounted file.

**How the batch uses it:** `python -m k8s_agent_sandbox.batch_reaper` (18/N) runs once and exits non-zero if anything failed. `batch_reaper.py: _default_namespace` checks `POD_NAMESPACE`, then the service account namespace file, then `default`, the same order as client-go's in-cluster config.

**Gotcha:** before deleting, each run waits until each stale Lease's own renew interval has passed since the list (so up to the longest interval), and `activeDeadlineSeconds` must cover that. A run cut short is harmless, because every step is a list or a precondition delete ([CLEAN-23](field-guide/clean.md#clean-23), [LEASE-8](field-guide/lease.md#lease-8)). The repo ships no CronJob manifest; the one below is only an illustration.

**Try it:**
```yaml
# illustrative, not shipped in the repo
spec:
  schedule: "*/2 * * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      activeDeadlineSeconds: 300
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: reaper
            command: [python, -m, k8s_agent_sandbox.batch_reaper]
            env:
            - name: POD_NAMESPACE
              valueFrom: {fieldRef: {fieldPath: metadata.namespace}}
```

**Read more:** [CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/), [Job termination](https://kubernetes.io/docs/concepts/workloads/controllers/job/#job-termination-and-cleanup), [Downward API](https://kubernetes.io/docs/concepts/workloads/pods/downward-api/)

## shutdownTime and shutdownPolicy (the controller-side backstop)

`spec.lifecycle.shutdownTime` is an absolute expiry on a SandboxClaim. When it passes, the claim controller acts on `shutdownPolicy`: `Delete` removes the claim (and its Sandbox), `DeleteForeground` removes it with a foreground cascade, and `Retain` deletes the Sandbox but keeps the claim.

**How the batch uses it:** every batch claim gets `shutdownPolicy: Delete` from `utils.py: construct_sandbox_claim_lifecycle_spec`. The fill uses one `shutdownTime` = `ceil(N / create_rps) + quorum_timeout + work_budget + 600s` (`SandboxBatch._claim`, 12/N). An `acquire()` claim counts from its own create (16/N; [LEASE-27](field-guide/lease.md#lease-27)). Nothing extends it, so `work_budget` is a hard cap.

**Gotcha:** the CRD defaults `shutdownPolicy` to `Retain` (`extensions/api/v1beta1/sandboxclaim_types.go`). Without the explicit `Delete`, expired claims would linger with their batch labels. The pacing term matters: quorum can arrive up to `quorum_timeout` after the *last* paced create ([COUNT-12](field-guide/count.md#count-12)).

**Try it:**
```yaml
spec:
  warmPoolRef: {name: python-sandbox-pool}
  lifecycle: {shutdownTime: "2026-11-04T15:12:00Z", shutdownPolicy: Delete}
# N=1000, rps=50, quorum=600, work=3600 -> now + 20 + 600 + 3600 + 600 s
```

**Read more:** repo CRD types in `extensions/api/v1beta1/sandboxclaim_types.go`; the site's lifecycle page lives under `site/content/` (public URL unconfirmed)

## managedFields (server-side apply bookkeeping)

`metadata.managedFields` records which manager owns which fields, so server-side apply can detect conflicting writers. Every object carries it, and it can be a large share of a small object's JSON.

**How the batch uses it:** `K8sHelper.list_sandbox_claim_objects` (20/N) pops `managedFields` from each claim as its page arrives, since nothing in the SDK reads it.

**Gotcha:** per-object bloat multiplies by N when you cache a whole batch ([PERF-6](field-guide/perf.md#perf-6)). Only drop it from objects you treat as read-only; don't send a stripped object back in an update or apply.

**Try it:**
```bash
kubectl get sandboxclaim b1-0 -o yaml --show-managed-fields
```

**Read more:** [Server-Side Apply: field management](https://kubernetes.io/docs/reference/using-api/server-side-apply/#field-management)

---

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)
