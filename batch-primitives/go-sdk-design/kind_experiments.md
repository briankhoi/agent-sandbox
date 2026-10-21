# Kind experiments for U5 and U6 (pending user actions §4)

All runs used the local kind cluster `agent-sandbox` (context `kind-agent-sandbox`, kindest/node v1.36.1, one node, 8-CPU host shared with the probes). The agent-sandbox controller `v20261020-82d410e` was already installed, with `--extensions`. The work was done in four passes:

| pass | when (UTC) | what | raw logs and scripts (all under `~/.gemini/jetski/brain/`) |
|---|---|---|---|
| **L0** | 2026-10-20 23:10–23:49 | U6 webhook and proxy runs; first U5 netem sweep | `e3466a27-7bed-43ae-a6fa-854c8b2ea511/scratch/` (`u6/`, `u5/`, `prior/`) |
| **L1** | 2026-10-20 23:57 – 10-21 00:06 | Review of L0: server-side `?timeout=`, APF queueing, client-go through the proxy | `34f332af-613c-4c54-b829-99be604b9768/scratch/` (`x/X1*.txt`, `x/X2*.txt`, `x/G_*.txt`, `x/proxy.log`, `prior/`, `after/`) |
| **L2** | 2026-10-21 00:09–00:45 | U5 again, with an httptrace on every renewal PUT, n ≥ 482 per cell | `a6cb3ab2-fd21-4e18-aa8b-4ebdb3c62500/scratch/` (`u5t/`, `u5t_out/`, `prior/`, `after/`) |
| **L3** | 2026-10-21 00:56–01:00 | Review of L2: 2 more u5t bursts with socket queues (`ss -tni`) sampled at every hop | `fcca1b1b-ce62-45ac-8a1f-dd328d626e72/scratch/ssprobe/` (`run_ss.sh`, `ss_analyze.py`, `out/`, `prior/`, `after/`); review `fcca1b1b-…/u5_review_report.md` |

Paths below are relative to the pass's directory.

Nothing in the user's checkout was touched: `git status` is clean, on `main` at `82d410ef`. The only repo-level action was L0's `git fetch origin`, which the orchestrator asked for. Nothing was committed or pushed.

**Summary**

- **U6 (late-commit window).**
  - The window exists on a real apiserver, but it opens only when the client's cancellation doesn't reach the apiserver: a proxy or LB that swallows it, or a partition. Against a directly reachable apiserver, both SDKs' 30 s timeouts abort the create on the server, and nothing commits.
  - When it does open, a create commits at most **34 s after the apiserver dispatches it**.
    - That is (30, 34] s after send without APF queueing (measured).
    - With APF queueing it is later: **41 s** was measured after a 9 s queue wait (X2), and the source allows up to about 49 s.
    - That bound is per wire request. With decision B23 (`MaxRetries(0)` on the batch's claim calls, recommended), one SDK attempt is one HTTP request, so it is also the per-call bound. Only if B23 is rejected can client-go's hidden 429/5xx retries push one SDK call's commit to about 64 s after its first send, or about 79 s if the last retry is also queued (source only).
  - Lease ownerReferences close the window. GC deleted the late claim **19–28 ms after it committed**, although its owner Lease had been gone for 2–4 s. This confirms B2.
  - A server-side `?timeout=` on create POSTs (option C, proposed as decision **B24**) also closes it, without GC. It has costs (§1.6). Recommended: don't adopt it; at most an optional belt-and-braces, paired with B23.
- **U5 (B14), Go only.**
  - With ≤ 100 creates in flight, shared and isolated renewals are equal.
  - With 200 creates in flight, renewals on the shared transport are slower in **every** run: 18/18 runs across L0, L2 and L3 by p95, two-sided sign test p ≈ 7.6·10⁻⁶.
  - L2 traced runs, n = 482–1710 renewals per run: pooled p95 **544 vs 143 ms** at 0 ms added RTT and **496 vs 162 ms** at +50 ms. The worst shared renewal was 2.37 s, the worst isolated one 1.47 s.
  - **Mechanism (measured):** it is not a dial, not a wait for a stream slot, and not TCP loss.
    - The renewal gets a connection and writes its request in about 0.1 ms. It then waits for the response: from the end of its request to the first response byte, a median of **102 ms (0 ms) / 134 ms (+50 ms)** on the shared transport against **51 / 93 ms** isolated. Only the slowest 5 % of shared renewals wait about 0.5 s or more there (their median is 511–541 ms; the worst renewal took 2.37 s in total).
    - x/net's connection pool is first-fit (`getClientConn` returns the first connection that can reserve a stream), so renewals land on the connections already carrying a median of 69–92 creates, while the third connection sits nearly idle.
    - The apiserver's own handler histogram shows the time is spent **outside the request handler**. Socket sampling (L3, 2 runs) puts most of it in the **apiserver's kernel receive buffer on the shared connections** (mean backlog 6.4–8.1 KB, p95 ~19 KB; 0 at the median on the isolated connection): the apiserver's per-connection HTTP/2 reader falls behind. The rest of the tail is user-space time that `ss` can't see, and is unattributed.
    - kind puts a docker-proxy hop (a userland TCP relay) between client and apiserver, which real clusters don't have. Its queues were near-empty at 0 ms, so it isn't the cause (§2.1).
  - L0's "the gap scales with RTT via dials" is not supported.
  - **Recommendation: keep B14 = isolated.** Keep its rev 3.2 rationale (parity with Python sync) and add this as a measured Go-side reason (§3).

---

## 0. Cluster state and cleanup (all four passes)

**Prior state** (L0 `prior/`, L1 `prior/`, L2 `prior/`, L3 `prior/`, identical where they overlap):
- namespaces `agent-sandbox-system default kube-node-lease kube-public kube-system local-path-storage`;
- 4 agent-sandbox CRDs;
- no mutating or validating webhook configurations;
- 19 APF objects (the defaults);
- no SandboxClaims, Sandboxes, warm pools or templates in any namespace;
- `qdisc noqueue` on every interface of `agent-sandbox-control-plane`;
- the local docker and node image lists.

**L0 created and removed:**
- the MutatingWebhookConfiguration `u6-slow-claims`;
- namespaces `u6-webhook`, `u6-test`, `u6-gc`, and `batchprobe-h2` (which the h2fixed probe creates);
- images `u6-webhook:dev` and `dev2`, both on the node (`crictl rmi`) and locally (`docker rmi`);
- the netem qdiscs, deleted after each configuration;
- the local `u6-proxy` process.

Final check at 23:49: the namespace, CRD, webhook, qdisc and image lists were identical to `prior/` (`diff` empty).

**L1 created and removed:**
- the MutatingWebhookConfiguration `u6r2-slow`;
- the FlowSchema `u6-fs` and the PriorityLevelConfiguration `u6-plc`;
- namespaces `u6-test`, `u6-q1` and `u6-webhook` (with the SA, Roles and RoleBindings);
- image `u6-webhook:r2`, on the node and locally;
- the proxy process;
- the local copies of the admin key, SA token and TLS key.

The 9 lists in `after/` match `prior/`.

**L2 created and removed:**
- namespace `batchprobe-h2`, with 8 Leases `u5t-{shared,isolated}-{0..3}` and up to 22,724 SandboxClaims per run;
- the netem qdisc, deleted after each +50 ms run.

One incident: in the first attempt (kept as `u5t_out_aborted_deletecollection/`), the probe's single `DeleteCollection` hit the apiserver's 60 s request timeout with about 20,000 claims, because the server deletes them one at a time. That left 18,695 claims behind for about 15 minutes. They were removed with 100 parallel DELETEs in 34 s (`u5t cleanup`). The probe now cleans up that way after every run, and each run's log ends with `claims left after cleanup wait: 0`.

Final check at 00:45: `kubectl delete ns batchprobe-h2`, then `snap.sh after/`. Diffed against `prior/`, these are all identical:
- namespaces, CRDs, webhooks, APF objects, Deployments, cluster RBAC;
- agent-sandbox objects (none in any namespace);
- Leases, and pods with their restart counts;
- qdiscs, node images, local images;
- no probe processes left.

**L3 created and removed** (the review's socket-queue runs, §2.3):
- namespace `batchprobe-h2`, with the u5t Leases and up to 19,185 claims per run, removed by the probe's parallel DELETEs (`claims left: 0` after each run);
- the netem qdisc of the +50 ms run;
- the `ss` samplers on the host and inside the node.

Final check at about 01:00: `kubectl delete ns batchprobe-h2`, then `snap.sh` into `after/`. All 13 lists are identical to `prior/`, the qdisc is back to `noqueue`, and no probe or sampler processes are left.

---

## 1. U6 — the late-commit orphan window

### 1.1 Setup (L0; L1 reused it with image `u6-webhook:r2`)

- **Sleeping webhook** (`u6/webhook/main.go`): a FROM-scratch image loaded with `kind load docker-image`, with a self-signed cert whose SAN is `u6-webhook.u6-webhook.svc`.
  - On every SandboxClaim CREATE in namespace `u6-test` it sleeps, then allows the request without a patch.
  - It logs `START` with the apiserver's `?timeout=` parameter, then `DONE`, or `CANCELLED` if the apiserver cancelled the call.
- **Webhook configuration** (`u6/webhooks.yaml`): one MutatingWebhookConfiguration with two webhooks, `a` and `b`, run one after the other.
  - Each has `timeoutSeconds: 30` and `failurePolicy: Fail`, and a `namespaceSelector` limiting it to `u6-test`.
  - The sleep length comes from the service path `/sleep/<id>/<secs>`. Webhook service paths can't carry a query string, and each path segment has to be a lowercase DNS label.
- **Cancellation-losing proxy** (`u6/proxy/main.go`): a TLS-passthrough TCP proxy in front of the apiserver.
  - **hold** (`:46443`): a client close is never passed upstream, and the upstream connection stays open until 50 s after accept. This covers HTTP/1.1, i.e. Python/urllib3.
  - **freeze** (`:46444`): as hold, but in addition client→apiserver bytes are dropped once the connection is 5 s old, so client-go's HTTP/2 RST_STREAM never arrives. The proxy logged `dropped 98 client->upstream bytes` on each freeze run.
- **Go driver** (`u6/gomod/batchproto/u6_kind_test.go`, `//go:build kind`). This is a copy of the prototype package that drives the prototype's real `miniBatch.release` path: `stop()` waits up to `createTimeout + 1 s` for creators, then the deletecollection and list rounds run.
  - A wrapper `claimCreator` gives each claim a valid spec (`warmPoolRef: does-not-exist`) and a unique `batch-id` label.
  - With `U6_OWNERREF=1` it also adds an ownerReference to the batch Lease. The shape is Python's `lease_owner_reference`: apiVersion, kind, name and uid; no `controller`, no `blockOwnerDeletion`.
  - `U6_CREATE_HOST` sends the POST through the freeze proxy, while the release rounds use the normal connection.
  - L1 checked that the copy is identical to the worktree's non-test files (`diff -q` clean).
- **Python driver** (`u6/u6_python.py`, run by `u6/u6_py.sh`):
  - It calls `claim_batch([BatchGroup(warmpool="u6-pool", size=1)])`, then `release()` 2 s later, then watches for 25 s. `kubectl get -w --output-watch-events` on claims and Leases records host timestamps.
  - Stacks tested: `56105540` (`origin/feat/batch-transport-ltlfdm`, no ownerRefs) and `4fa72e04` (`origin/feat/batch-reaper-ymtd6t`, with `d913caa8` "make batch claims owned by their batch Lease"). The pre-push stack (`e07e3c70`; `600a880d` with `90b847ed`) gave the same results.
- **L1 clients:**
  - `curl --max-time 90`, which never cancels and so isolates the server-side bound (`x/run.sh`), as cluster-admin or as SA `u6-apf`.
  - The client-go probe `x/gp` (raw REST `Post()`, optionally `.Param("timeout", …)`), through the freeze proxy.

### 1.2 How long can a create take on the apiserver? 34 s after dispatch; later under APF queueing

**Source.** The create handler wraps its context in `context.WithTimeout(ctx, requestTimeoutUpperBound)`, with `requestTimeoutUpperBound = 34 * time.Second`. That is `k8s.io/apiserver/pkg/endpoints/handlers/rest.go:54` and `handlers/create.go:77-79` in the google3 mirror `//depot/google3/third_party/kubernetes_control_plane/versions/master/k8s_io/apiserver/…`; upstream it is `create.go:85-87`. The update, patch and delete handlers do the same. Webhooks get `min(timeoutSeconds, time left)`.

The handler starts only after API Priority and Fairness has dispatched the request:
- A queued request waits at most a quarter of its remaining deadline: 15 s with the default 60 s, or `timeout/4` when the client sets `?timeout=` (`server/filters/priority-and-fairness.go:396-426`, `getRequestWaitContext`).
- The global `WithRequestDeadline` caps every request at receipt + 60 s, or receipt + `?timeout=` (`request_deadline.go:81-96`).

**Measured without queueing** (L0, `kubectl create --request-timeout=90s`; kubectl doesn't cancel before 34 s):

| admission sleep (a + b) | result | client time | webhook log |
|---|---|---|---|
| 20 + 12 = 32 s | **committed** (`creationTimestamp 23:18:20`) | 32.16 s | b `DONE after=12.01s` |
| 20 + 13.5 = 33.5 s | **committed** | 33.68 s | b `DONE after=13.51s` |
| 20 + 13.9 = 33.9 s | **committed** | 34.04 s | b `DONE after=13.907s` |
| 20 + 20 = 40 s | **not committed**: `Error from server (Timeout): … context deadline exceeded` | 34.12 s | b `START … timeoutParam=14s` → `CANCELLED after=13.977s err=context canceled` |
| X1c (L1, curl, admin, no `?timeout`): 20 + 12 s | **201, committed** (`creationTimestamp 23:59:13`) | 32.07 s | b `timeoutParam=14s` |

L0 kept no raw logs for the first four rows; only its summary exists. L1 re-confirmed the 32 s commit and the 14 s webhook budget (X1c and X2, `x/X1c.txt`, `x/X1_webhook.txt`). The 33.5, 33.9 and 40 s rows were not re-run; they are consistent with source.

**Measured with APF queueing** (L1 X2, `x/x2.sh`, `x/X2.txt`, `x/X2_webhook.txt`):
- Setup: FlowSchema `u6-fs` → PriorityLevelConfiguration `u6-plc` with 3 seats (`nominalConcurrencyShares: 1`, no lending or borrowing, 1 queue). Four blockers created in `u6-q1` (webhook c sleeps 10 s); 1 s later the test create (webhooks a 20 s + b 12 s), with no `?timeout`. All requests run as SA `u6-apf`.
- The test create was sent at 23:59:53.432 and dispatched at 00:00:02.479 (webhook a `START`), i.e. **queued 9.05 s**.
- Webhook b got `timeoutParam=14s`, so the 34 s counts from dispatch, not from receipt.
- **201 at 41.05 s, committed at 00:00:34.** That is 41 s after send, on an unmodified apiserver.
- With the maximum 15 s queue wait and admission close to 34 s, the latest commit is about receipt + 49 s, still under the receipt + 60 s cap (source).

**Correction to L0:** L0 wrote "the two-20 s-webhooks setup can't commit at 40 s" and suggested moving `TestLateCommitWindowIsDocumented`'s commit to ≤ 34 s. **That is wrong under APF queueing:** X2 committed at 41 s. Keep the 40 s fixture. 34 s is the bound only for an unqueued request.

**Per wire request vs per SDK call (source only, not run).** The receipt + 60 s cap applies to each HTTP request. With the typed client (that is, without B23), client-go retries a POST up to 10 times on 429 or 5xx with Retry-After inside one `Create` call. The source:
- `rest/request.go:167`: `maxRetries: 10`;
- `rest/with_retry.go:148-193`, `IsNextRetry`;
- `with_retry.go:309-316`: `checkWait` accepts 429 and ≥ 500 when Retry-After is present;
- APF 429s carry `Retry-After` (`priority-and-fairness.go:377-380`).

**With B23** (recommended: the batch's creates go through `RESTClient()` with `MaxRetries(0)`), there are no hidden retries. One SDK attempt is one HTTP request, so the per-call bound is the per-request one: a commit at most 34 s after dispatch, i.e. (30, 34] s after send without queueing, about 41 s measured (X2) and at most about 49 s with APF queueing.

**Only if B23 is rejected:** if the cancellation is lost, the last hidden retry can go out just before the SDK's 30 s ctx expires. That retry can then commit up to 34 s after its own dispatch: about **64 s after the SDK's send without queueing, about 79 s if that retry is also queued**. The SDK sees only its own send time, not the retries. This matters for timing-based mitigations (B15) and for the documentation; GC via ownerRefs (B2) is unaffected either way.

### 1.3 Does the SDK's 30 s timeout stop the write? Yes, if the cancellation reaches the apiserver

client-go doesn't send its context deadline to the server. Only `rest.Config.Timeout` becomes `?timeout=` (`client-go@v0.37.0/rest/request.go:578-580`). So the server-side bound is the one in §1.2, and the client's 30 s takes effect only through cancellation:
- Go sends an HTTP/2 RST_STREAM, and urllib3 closes the socket on a read timeout.
- Go's `net/http` server cancels the request context on either one.
- The apiserver passes that context to the admission webhooks and to storage.

Webhooks at 20 + 12 = 32 s, direct connection (no proxy):

| run | client | result | evidence |
|---|---|---|---|
| G1 | Go prototype `release()` | **no orphan** | POST `context deadline exceeded` at 30.02 s; webhook b `CANCELLED after=10s err=context canceled`, exactly at the client's 30 s; release round 1 lists 0 items; namespace empty for 20 s |
| P1b | Python `56105540` `release()` | **no orphan** | `release() returned` at t+31.06 s; webhook b `CANCELLED after=9.998s`; the claim watch saw no event at all |
| P1 | Python `e07e3c70` (pre-push stack) | **no orphan** | webhook b `CANCELLED after=9.996s` |

**A commit that beats the client deadline but whose response arrives late** (C2: netem `delay 1000ms` on node egress, webhooks 20 + 9.4 = 29.4 s, Go, direct, connection pre-warmed):
- The POST was sent at 23:31:21.911.
- The apiserver committed at about 23:31:51.35 (webhook b `DONE` at 51.340).
- The client timed out at 23:31:51.911, while the response was still crossing the delayed link.
- Release's first deletecollection, sent right after, deleted the claim (`c2watch.txt`: `ADDED u6g052057-0` … `DELETED` at 23:31:52.93 host clock, about 1 s skew).
- **No orphan.** The deletion rounds start only after the creators return.

What remains when the cancellation does propagate is a storage write already in flight when it arrives. That window is sub-millisecond on kind and was not reproduced.

### 1.4 The orphan, reproduced: cancellation lost, commit in (30, 34] s

Webhooks at 20 + 12 = 32 s, create sent through the proxy:

| run | client | result | evidence |
|---|---|---|---|
| G2 | Go prototype, freeze proxy | **ORPHAN** | POST `context deadline exceeded` at t+30.03 s; release round 1 at t+30.04 s: `deletecollection … list: 0 items`; Lease deleted at t+30.05 s; webhook b `DONE` at 23:20:01.44; at t+32.18 s the watch shows `1 claims [u6g556509-0 created=23:20:01 deleting=false owners=0]`, still there after 25 s |
| G-ctl (L1) | client-go raw REST, freeze proxy, 30 s ctx | **late commit** | `x/G_ctl.txt`: sent 00:03:45.229, `context deadline exceeded` after 30.025 s; claim committed at 00:04:17 (≈ 32 s) |
| P2b | Python `56105540`, hold proxy | **ORPHAN** | `release() returned` at t+31.04 s; Lease `DELETED` at 23:45:37.621; claim `ADDED b15iv0q88r9z-0` at **23:45:39.601**, 2 s after the Lease was gone; still there 23 s later, removed by the driver's own cleanup at 23:46:02 |
| P2 | Python `e07e3c70`, hold proxy | **ORPHAN** | Lease deleted at 23:26:09.050; claim added at 23:26:11.032 |

Without ownerRefs, such a claim lives until its `shutdownTime`: quorum + budget + 600 s, or `ceil(N/rps)` + quorum + budget + 600 s for fill claims. L0 kept no raw logs for G1/G2. L1 checked the Python P2b/P3b logs, and they match the table exactly.

### 1.5 The fix: Lease ownerReferences (B2; Python `d913caa8`, formerly `90b847ed`)

Same setup as §1.4, with the claims owned by the batch Lease:

| run | client | result | evidence |
|---|---|---|---|
| G3 | Go prototype + ownerRef, freeze proxy | **no orphan** | `g3watch.txt`: `ADDED u6g299125-0 … ownerUID=24eaf1fe…` at 23:23:34.324 → `DELETED` at **23:23:34.352** (28 ms); the Lease had been deleted about 4 s earlier |
| P3b | Python `4fa72e04`, hold proxy | **no orphan** | Lease `DELETED` at 23:46:55.832; claim `ADDED bdw76va1lmvf-0 … ownerUID=5ba52a64…` at 23:46:57.806 → `DELETED` at **23:46:57.825** (19 ms) |
| P3 | Python `600a880d` (pre-push stack) | **no orphan** | Lease deleted at 23:27:29.131; claim added at 23:27:31.109 → deleted at 23:27:31.129 (20 ms) |

**What GC does with an ownerRef to a missing UID** (`TestU6GCDanglingOwner`, namespace `u6-gc`, no webhook, run twice; no raw log kept):
- an ownerRef to a Lease that was created and then deleted: the claim is deleted **130 / 134 ms** after create;
- an ownerRef that names a live Lease but carries a stale UID (the "Lease recreated" case): deleted **124 / 129 ms** after create;
- the control, an ownerRef to the live Lease with its real UID: **still present after 15 s**.

GC looks the owner up live; a missing owner and a UID mismatch both count as "owner gone". **So B2 closes F4 on a real apiserver:** a claim committed *after* its Lease was deleted is collected within about 0.1 s.

The flip side: a recreated Lease, with a new UID, makes GC delete every claim that points at the old UID. Commit `d913caa8`'s message already says that nothing recreates a Lease for existing claims.

Caveats:
- These claims never got a Sandbox, because the pool doesn't exist or can't schedule. With a real pool, the controller may adopt or create a Sandbox in the ~20 ms before GC. Sandboxes are owned by their claim, so GC's background cascade should remove them; this was not tested.
- GC must be running: kube-controller-manager's `garbagecollector` controller, which is on by default.

### 1.6 Option C: a server-side `?timeout=` on create POSTs (L1, measured; proposed as decision B24)

| run | what | result |
|---|---|---|
| X1 | curl, admin, `?timeout=25s`, admission 32 s, client never cancels | **504 at 25.01 s, not committed** (`x/X1.txt`). Webhook b got `timeoutParam=5s` and was `CANCELLED after=4.958s` |
| G-t25 | client-go raw REST through the freeze proxy (cancellation lost), `.Param("timeout","25s")` | `the server was unable to return a response in the time allotted` at 25.00 s; webhook b `CANCELLED after=4.978s`; **not committed** (`x/G_t25.txt`) |
| X2b | as X2 (APF queueing), test create with `?timeout=25s` | **429 at 6.26 s** (= 25 s / 4, the queue-wait limit). The webhook was never called; nothing committed (`x/X2b.txt`) |

- **What:** send `timeout=<createTimeout − margin>` on every create POST, e.g. 25 s with the 30 s client ctx.
  - The apiserver then bounds the whole request, APF wait included, at receipt + 25 s (`request_deadline.go:81-96`), **even when the client's cancellation is lost** (G-t25).
  - It covers every POST path (fill, `Acquire`, a late commit after `Detach`) and doesn't rely on GC or a Lease.
- **How in Go:**
  - Either `RESTClient().Post()…Param("timeout", "25s")`. `Param` survives because `URL()` overrides `timeout` only when `r.timeout != 0` (`request.go:571-581`).
  - Or a create-only clientset with `rest.Config.Timeout`. That also sets the client-side timeout to the same value, and it would break watches if the clientset were shared.
- **Costs:**
  - The APF queue-wait limit drops from 15 s to 6.25 s, so there are more 429s under contention (X2b).
  - Without B23, client-go's hidden 429 retries reuse the fixed parameter, so it bounds each request but not the call. With B23 (`MaxRetries(0)`), each SDK attempt is one request with its own parameter, and a hard per-call bound needs only a per-attempt `min(25 s, remaining − margin)`. So option C makes sense only paired with B23.
  - Python's `create_namespaced_custom_object` has no `timeout` query parameter. Python would need `api_client.call_api`, a parity cost.
  - An etcd write already in flight when the deadline hits remains as a residual.
  - It is one more wire difference to keep in sync across the two SDKs.
- **Recommendation (B24):** (a) don't adopt it; (b) is an optional belt-and-braces, paired with B23. **Not required.** B2's ownerRefs already close the Release case on a real apiserver (G3, P3b, the GC test). The case B2 doesn't close is a late commit after `Detach` (the Lease survives, so the next handle inherits the claim). Option C would close that, but it is a claim the next handle can see and free, not an orphan. Adopt it only if the inherited-claim case is judged worth a wire change in both SDKs.

---

## 2. U5 — Lease renewal latency, shared vs isolated transport (B14)

### 2.1 Common setup

- **Emulated RTT:** `docker exec agent-sandbox-control-plane tc qdisc replace dev eth0 root netem <args>`. netem on the node's eth0 egress delays every node→host packet, so each host↔apiserver round trip gets `<delay>` added; in-cluster traffic doesn't leave through eth0. Sanity check at `delay 1000ms`: `kubectl get --raw /readyz` took 3.08 s (TCP + TLS + request = 3 RTT). In L2, the pre-burst idle renewal p50 was 6–7 ms at 0 ms and 57–65 ms at +50 ms.
- **Transports** (both probes): one `rest.HTTPClientFor(cfg)`, shared through `NewForConfigAndClient` by the create clientset and the "shared" Lease clientset, plus a separate HTTP client for the "isolated" Lease clientset. Both transports run in the same process during the same burst, so each run is a paired comparison.
- **The client is cluster-admin (`system:masters`)**, which APF exempts. A real SDK user's renewals and creates fall in the same non-exempt priority level, so APF queueing would hit both transports alike; isolation doesn't help against it.
- **U5 is Go only.** It says nothing about Python. B14's rev 3.2 rationale is parity with Python **sync**, whose Lease client has its own `ApiClient`. The starvation concern (alignment A9) rests on Python **async/aiohttp** pool queueing (HTTP/1.1), which these runs don't cover.
- **kind adds a docker-proxy hop.** `docker port` maps `6443/tcp -> 127.0.0.1:42115`, served by `/usr/bin/docker-proxy … -host-port 42115 -container-ip 192.168.8.2 -container-port 6443`, a userland per-connection TCP relay. Every client↔apiserver byte in these runs passes through it, and a real cluster has no such hop. L3 measured its queues near-empty at 0 ms (§2.3), so it doesn't cause the shared-transport gap, but it is part of "the wire" here.

### 2.2 L0 sweep (h2fixed, unchanged; N = 3000 creates; 1 renewer per transport every 100 ms; 2 reps)

Probe: `brain/7187bf6d-…/scratch/h2fixed/h2fixed ~/.kube/config 3000 <conc>`, run by `u5/u5_run.sh`; raw output in `u5/out/*.txt`. Each cell is p50 / "p95" / max in ms, with n (renewal samples) in brackets. "p95" is h2fixed's `l[len*95/100]`. **At n = 20–22 that is the largest or second-largest sample, so these "p95" values are effectively maxima.**

| added RTT | conc | create rate | shared-transport dials | renew on **shared** | renew on **isolated** |
|---|---|---|---|---|---|
| 0 | 20 | 480/s, 409/s | 1, 1 | 24/64/120 (52); 35/78/104 (57) | 22/37/46 (55); 26/62/88 (61) |
| 0 | 200 | 733/s, 778/s | 94, 93 | 62/432/489 (21); 42/293/321 (23) | 75/370/398 (21); 43/110/170 (29) |
| 20 ms | 20 | 311/s, 297/s | 1, 1 | 48/85/174 (67); 45/78/110 (71) | 41/61/118 (71); 43/63/104 (73) |
| 20 ms | 200 | 873/s, 779/s | 103, 103 | 62/167/313 (22); 57/177/428 (25) | 52/90/159 (25); 46/96/103 (29) |
| 50 ms | 20 | 212/s, 193/s | 1, 1 | 75/101/132 (83); 81/122/134 (88) | 72/94/117 (84); 78/111/167 (90) |
| 50 ms | 200 | 674/s, 848/s | 101, 101 | 88/297/447 (22); 90/230/230 (20) | 81/135/185 (27); 84/138/153 (22) |
| 100 ms | 20 | 159/s, 163/s | 1, 1 | 115/140/205 (89); 112/135/212 (87) | 114/137/212 (90); 114/126/137 (88) |
| 100 ms | 200 | 661/s, 663/s | 103, 103 | 135/367/367 (20); 134/416/416 (20) | 126/167/196 (22); 131/169/193 (22) |
| 50 ms + 1 % loss | 20 | 199/s, 196/s | 1, 1 | 80/131/246 (84); 82/125/171 (85) | 66/105/385 (90); 70/92/429 (91) |
| 50 ms + 1 % loss | 200 | 675/s, 696/s | 103, 103 | 124/515/515 (20); 109/209/426 (22) | 91/132/150 (26); 81/150/189 (26) |

Readings that still hold:
- **conc 20 (≤ 100 in flight):** shared ≈ isolated at every RTT; one connection carried all creates.
- **conc 200:** the shared transport's "p95" was higher in **10/10** runs (sign test p ≈ 0.002).
- **Loss:** with 1 % loss at conc 20, the isolated connection had the worse max (385 and 429 ms vs 246 and 171 ms). A lost segment on an otherwise idle connection waits for the RTO, because no later segments trigger fast retransmit. So isolation doesn't protect against loss; this argues against the design's "TCP-level head-of-line blocking" rationale.

Readings withdrawn after L1 and L2:
- **The magnitude ("1.2–3.9×").** It came from one or two extreme samples per run.
- **"The gap grows with RTT."** That held only in absolute terms from 20 to 100 ms; the ratio was not monotonic. L2 (n ≥ 482) finds the gap no larger at +50 ms than at 0 ms.
- **"A renewal waits for a stream or for a dial; the gap tracks 2 RTT per dial."** L2's trace shows neither (§2.3).

### 2.3 L2: traced renewals (u5t), conc 200, 0 ms and +50 ms, 3 interleaved reps

**Probe `u5t/main.go`.** It is h2fixed with these deliberate changes:
- **httptrace on every renewal PUT:** GetConn, ConnectStart, TLSHandshakeDone, GotConn (Reused, conn identity), WroteHeaders, WroteRequest, GotFirstResponseByte, plus the end of `Update()`.
- **Each renewal records the number of create streams in flight on the connection it got**, counted per connection from the create's GotConn to its first response byte.
  - h2fixed counted to `Create()` returning, which inflated its peaks to 113–173.
  - Counted this way, **the peak per connection is exactly 100** in every run, the server's `MaxConcurrentStreams`.
- **Duration-based burst:** creates run at concurrency 200 for 30 s (14,849–22,724 creates per run, 493–750/s, 0 failures) instead of a fixed N.
- **4 renewers per transport** (one Lease each, as one `Client` holding 4 batches), each renewing **every 10 ms** after the previous PUT returns, instead of 1 renewer every 100 ms. Without this, a 30 s burst yields under 200 shared renewals at conc 200, because a shared renewal takes 100–500 ms.
  - A renewal counts only if it started after the first create was issued and finished before the last create returned.
  - A 2 s renew-only phase before the burst gives the idle baseline.
- **Cleanup by 100 parallel DELETEs** (§0).
- Every renewal is written to `u5t_out/<cell>_c200_r<rep>.csv`.
- The runner `u5t_run.sh` interleaves 0 ms and +50 ms within each rep and scrapes `apiserver_request_duration_seconds` (lease PUT, claim POST) before and after every run (`*.metrics_before/after`). Analysis: `u5t_analyze.py u5t_out` → `u5t_out/ANALYSIS.md`.
- Percentiles are nearest-rank (index `ceil(p·n) − 1`).

**Results (renewal PUT latency during the burst, ms):**

| added RTT | rep | transport | n | p50 | p95 | p99 | max | idle p50 before the burst |
|---|---|---|---|---|---|---|---|---|
| 0 | 1 | shared | 482 | 177 | 751 | 1021 | 1925 | 7 |
| 0 | 1 | isolated | 1260 | 66 | 196 | 385 | 865 | 7 |
| 0 | 2 | shared | 720 | 98 | 498 | 794 | 1095 | 7 |
| 0 | 2 | isolated | 1710 | 53 | 119 | 162 | 1468 | 6 |
| 0 | 3 | shared | 688 | 105 | 519 | 714 | 988 | 21 |
| 0 | 3 | isolated | 1689 | 53 | 120 | 180 | 382 | 20 |
| +50 ms | 1 | shared | 562 | 145 | 539 | 747 | 1247 | 57 |
| +50 ms | 1 | isolated | 1035 | 99 | 158 | 201 | 251 | 57 |
| +50 ms | 2 | shared | 530 | 152 | 545 | 1077 | 2369 | 64 |
| +50 ms | 2 | isolated | 1004 | 97 | 173 | 340 | 675 | 62 |
| +50 ms | 3 | shared | 650 | 139 | 416 | 532 | 706 | 65 |
| +50 ms | 3 | isolated | 1099 | 93 | 149 | 196 | 245 | 63 |

| added RTT | transport | n (pooled) | p50 | p95 (95 % bootstrap CI, rep-stratified) | p99 | max |
|---|---|---|---|---|---|---|
| 0 | shared | 1890 | 116 | **544** (518–582) | 864 | 1925 |
| 0 | isolated | 4659 | 56 | **143** (136–149) | 227 | 1468 |
| +50 ms | shared | 1742 | 144 | **496** (472–517) | 713 | 2369 |
| +50 ms | isolated | 3138 | 96 | **162** (156–166) | 224 | 675 |

No renewal or create errors in any run. The isolated transport dialed once per run. The shared transport dialed 66–109 times per run, yet the creates ran on 3 connections, e.g. `{1 100 9896} {2 100 9127} {3 4 663}` (id, peak create streams, creates).

**Sign tests (shared worse than isolated):**
- per run, L2: p50 **6/6**, p95 **6/6**, p99 **6/6** (two-sided p = 0.031 each);
- per 1 s window, maximum renewal in the window: **91/93** windows at 0 ms and **93/93** at +50 ms (p < 10⁻²⁴). The windows are serially correlated, so these p-values are optimistic;
- combined with L0's conc-200 runs: p95 shared > isolated in **16/16** runs (two-sided p ≈ 3·10⁻⁵); with L3's 2 runs, **18/18** (p ≈ 7.6·10⁻⁶).

The per-run p95 ratio (shared / isolated) is 2.8–4.3×, and the absolute p95 gap is about 400 ms at 0 ms and about 330 ms at +50 ms.

**Measured mechanism** (median ms per trace phase; "tail" = renewals ≥ the p95 of their own run):

| added RTT | transport | subset | n | GetConn→GotConn | GotConn→WroteHeaders | WroteRequest→first byte | first byte→return | total | renewals that dialed | creates on its connection at GotConn (median / max / count ≥ 100) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | shared | all | 1890 | 0.0 | 0.1 | 101.6 | 5.3 | 116 | 4 | 69 / 99 / 0 |
| 0 | shared | tail | 97 | 0.0 | 0.1 | 541.4 | 86.6 | 640 | 1 | 88 / 99 / 0 |
| 0 | isolated | all | 4659 | 0.0 | 0.1 | 50.9 | 1.8 | 56 | 0 | — |
| 0 | isolated | tail | 235 | 0.0 | 0.1 | 140.9 | 6.1 | 155 | 0 | — |
| +50 ms | shared | all | 1742 | 0.0 | 0.1 | 134.4 | 3.3 | 144 | 6 | 83 / 99 / 0 |
| +50 ms | shared | tail | 89 | 0.0 | 0.1 | 510.9 | 59.2 | 572 | 0 | 92 / 98 / 0 |
| +50 ms | isolated | all | 3138 | 0.0 | 0.1 | 92.6 | 0.7 | 96 | 0 | — |
| +50 ms | isolated | tail | 158 | 0.0 | 0.1 | 176.5 | 3.1 | 184 | 0 | — |

- **No wait for a connection, and no dial in the tail.** 4–6 of about 1800 shared renewals dialed, and none of the shared tail renewals did (except one at 0 ms). GetConn→GotConn is ≈ 0.
- **No wait for a stream slot.** GotConn→WroteHeaders is 0.1 ms median (the largest seen was 52 ms); that is the evidence. That every renewal found ≤ 99 create streams on its connection is close to true by construction: `canReserveLocked` admits a request only below 100 streams, and the probe counts a narrower window than x/net does.
  - This is what golang.org/x/net v0.59 http2 does in non-strict mode (Go 1.26 builds use `transport.go`, not the go1.27 wrapper). A request reserves a slot only on a connection below `maxConcurrentStreams` (`transport.go:666-720`, `idleStateLocked`/`canReserveLocked`; `client_conn_pool.go:53-98`). Otherwise net/http dials and retries (`net/http/transport.go:702-722`, `http2isNoCachedConnError`).
  - That is also the source of the 66–109 dials: their connections are mostly discarded by `addConnIfNeeded` (`client_conn_pool.go:151-158`).
  - So L1's inference that requests "were waiting for a stream slot" (from h2fixed's 113–173 peaks) is not supported for renewals. Those peaks were the counting artifact described above.
- **Renewals prefer the busiest connection.** x/net's `getClientConn` returns the **first** connection in `p.conns[addr]` that can reserve a stream (`client_conn_pool.go:66-79`), not the least loaded one. So renewals land on the first two connections, which carry a median of 69–92 creates at GotConn, while the third (peak 4 create streams) sits nearly idle. Sharing doesn't merely risk a busy connection; it prefers one.
- **Where the time goes: WroteRequest→first response byte, plus first byte→return.** The shared tail spends 510–540 ms (median) between finishing its request and the first response byte, against 140–180 ms for the isolated tail (different renewals, in the same runs). The first byte→return phase (the rest of the response after its HEADERS frame) adds another 60–90 ms on the shared tail and 3–6 ms on the isolated one.
- **That time is outside the apiserver's request handler.** `apiserver_request_duration_seconds` for lease PUTs (all PUTs during the run: both transports plus the cluster's own few Leases) against client-observed renewal times:

  | added RTT | rep | lease PUTs with handler > 0.2 s | client renewals > 0.2 s (shared, isolated) | handler > 0.4 s | client > 0.4 s (shared, isolated) |
  |---|---|---|---|---|---|
  | 0 | 1 | 45 | 269 (213, 56) | 16 | 84 (72, 12) |
  | 0 | 2 | 9 | 198 (190, 8) | 2 | 70 (69, 1) |
  | 0 | 3 | 6 | 209 (197, 12) | 0 | 68 (68, 0) |
  | +50 ms | 1 | 4 | 203 (192, 11) | 1 | 64 (64, 0) |
  | +50 ms | 2 | 18 | 229 (195, 34) | 2 | 76 (69, 7) |
  | +50 ms | 3 | 2 | 193 (184, 9) | 1 | 37 (37, 0) |

  A request's client time is at least its handler time. So in rep 1 at 0 ms, for example, at least 213 − 45 = 168 shared renewals took over 0.2 s while their handler took under 0.2 s; in the other runs, at least 172–188.
  - The excess is spent between the bytes on the wire and the handler. Each shared connection carries about 90 other streams; the isolated connection's renewals see the same apiserver in the same runs and don't wait.
  - **Where (L3, below): mostly a receive backlog in the apiserver's kernel buffer on the shared connections, measured at the socket level in 2 runs.** The rest of the tail is unattributed user-space time. L2 alone had only inferred the server side.
- **Not RTT-driven.** The shared−isolated gap was about 400 ms at 0 ms and about 330 ms at +50 ms, so it doesn't scale with RTT. It is load-driven: queuing behind other streams on the same connection. L0's dial explanation is refuted for these cells.
- **Not TCP head-of-line.** There was no loss in L2. L0's loss run already showed that isolation makes a loss tail *worse*.

**L3: where the shared connection's bytes queue (socket level).**
- **Method.** 2 more u5t bursts with L2's parameters (conc 200, 30 s, 4 renewers per transport every 10 ms), one at 0 ms and one at +50 ms. `ss -tni` was sampled every ~150 ms at every hop: the probe's client sockets, docker-proxy's accepted and upstream sockets, and the apiserver's sockets inside the node. Script `run_ss.sh`, analysis `ss_analyze.py`; raw data in `out/` (`{none,delay_50ms}_{host_ss,node_ss}.txt`, `*_c200.csv`, `*_probe.txt`).
- The shared transport dialed 105 and 109 times, and the creates again ran on 3 connections (`{1 100 9344} {2 100 8967} {3 4 874}` at 0 ms). 0 create failures, 0 claims left.

Latency (burst renewals, ms): shared was worse again in both runs, which makes 18/18 overall.

| run | shared n / p50 / p95 / p99 / max | isolated n / p50 / p95 / p99 / max |
|---|---|---|
| 0 ms | 597 / 154 / 457 / 585 / 792 | 1504 / 57 / 152 / 266 / 487 |
| +50 ms | 543 / 163 / 474 / 689 / 867 | 947 / 107 / 192 / 259 / 305 |

Socket queues during the burst (bytes, p50 / p95 / max; "heavy" = the 2 shared connections carrying ~100 creates each):

| hop | 0 ms heavy | 0 ms isolated | +50 ms heavy | +50 ms isolated |
|---|---|---|---|---|
| **apiserver socket Recv-Q** | **4282–4848 / 17398–19250 / ~21.4k** (mean 6.4–6.9k) | 0 / 728 / 1460 | **6079–7332 / 18555–19214 / 22–25k** (mean 7.2–8.1k) | 0 / 730–888 / ≤ 1491 |
| apiserver Send-Q | 0 / 123–286 / ≤ 6.1k | 0 / 184–325 | 4118 / ~40k / ≤ 78k (in flight on the netem'd link) | ≤ 3.3k |
| docker-proxy Recv-Q (both sides) | 0 / ≤ 297 / ≤ 24.7k | ~0 | 0 / ≤ 412 / ≤ 24.6k | ~0 |
| probe (client) Recv-Q | **0 / 0** / ≤ 44k | 0 / ≤ 66 | 0 / 3.6–4.1k / ≤ 57k | 0 / 0 |

- **On the shared connections, request bytes pile up in the apiserver's kernel receive buffer**, so the apiserver's per-connection HTTP/2 reader isn't keeping up. On the isolated connection that buffer is empty at the median.
- **At 0 ms the client's receive buffer and docker-proxy are empty at p95**, so neither the client's read loop nor the relay is where the time goes.
- **Size of the wait (Little's law):** the mean Recv-Q over ~85–95 KB/s of client→server bytes per connection gives about 70–90 ms on average, the same order as the median shared − isolated gap (97 ms at 0 ms, 56 ms at +50 ms).
- **The tail:** the p95 backlog (~19 KB ≈ 200 ms) explains part of the ~300 ms tail gap. The rest is user-space time that `ss` can't see (server dispatch and response scheduling, or goroutine scheduling under CPU saturation), and is **unattributed**.
- **Correlation (0 ms):** shared renewals in the slowest 5 % saw a higher server Recv-Q during their lifetime (median of the per-renewal maximum 18.9 KB, vs 13.6 KB for the rest). Isolated renewals saw 0 in both groups.
- **At +50 ms** some client-side read backlog appears too (p95 3.6–4.1 KB), but the server receive backlog is still the largest and most consistent.
- **Net:** **mostly a server-side receive backlog on the shared connections (measured at the socket level, 2 runs); the rest of the tail is unattributed.**
- **Caveats:** one run per RTT; ~150 ms sampling misses short peaks; the samplers add a little host CPU load; one 8-CPU host where the apiserver is CPU-starved; the docker-proxy hop (§2.1).

**Caveats:**
- **Closed-loop sampling.** Renewers sample less often while they are slow (coordinated omission), so the shared percentiles understate the time-weighted tail, if anything.
- **Correlated samples.** The 4 renewers per transport are correlated in time; the per-run sign test is the conservative one.
- **Residual load in rep 3 at 0 ms.** Its idle baseline was 20–21 ms, against 6–7 ms in reps 1 and 2, likely from the previous run's deletes. It affected both transports equally.
- **One host, one node.** The probes and kind share 8 CPUs, so apiserver CPU saturation is part of the setup. A real control plane with more headroom may show a smaller gap.
- **Only conc 200, only 0 and +50 ms, no loss.** The other L0 cells were not re-run with tracing.

> **Caveats on the L3 socket figures (from the final review):** the burst window used for the 0 ms heavy-connection Recv-Q p50 wasn't recorded. Re-running `ss_analyze.py` with the CSV burst window gives 4.6–4.9 KB rather than 4.3–4.8 KB (means 6.5–7.0 KB); p95/max and the +50 ms column reproduce exactly. In two cells the "isolated" column also includes the near-idle shared conn 3 (0 ms Send-Q 184; +50 ms Recv-Q p95 888, isolated alone 730). The means, the Little's-law estimate (independently re-checked: 86–95 KB/s on the heavy connections, so 6.4–8.1 KB is about 67–94 ms) and the per-renewal Recv-Q correlation (18.9 vs 13.6 KB, not re-derived) don't come from the listed scripts. None of this changes the conclusion.

### 2.4 B14 recommendation

**Keep the isolated Lease transport, one per `Client`.** The recommendation doesn't change. Its rev 3.2 rationale (parity with Python sync) stays, and these runs add a measured Go-side reason.
- **Added reason (measured, Go):** above 100 creates in flight, the shared transport keeps about 100 create streams on each of its busy connections, and x/net's first-fit pool puts renewals on exactly those connections. A renewal gets a stream at once, but its request bytes then queue in the apiserver's per-connection receive path, outside the handler (L3).
  - The shared renewal p95 was 2.8–4.3× the isolated one (about 330–400 ms more) in 6/6 traced L2 runs, and worse in 18/18 runs overall.
  - The worst shared renewal was 2.37 s, the worst isolated one 1.47 s.
  - That is the regime batches create in: `MaxInFlight` > 100.
- **Drop** "TCP-level head-of-line blocking", "the gap scales with RTT" and "waits for a dial or a stream slot". None is supported.
- **Margin:** at the default D = 60 the renew timeout is 20 s, so neither transport came close. At the minimum lease duration (D = 6, timeout 2 s), the shared transport's worst renewal (2.37 s) would have **exceeded** the timeout once in about 3600 burst renewals, and the isolated one wouldn't have. The decision is about margin, not correctness, but the margin is thinner than "343 ms worst" suggested.
- **Costs:** one idle connection per `Client`, and a slightly worse max tail on lossy links (RTO on an idle connection, L0).
- **What it doesn't fix:** APF queueing for a non-exempt user (same flow for both transports), and apiserver CPU contention, which also showed up on the isolated connection (0 ms, rep 1: p95 196 ms, max 1468 ms in rep 2).

### 2.5 What kind can't emulate, and what is untested

Needs non-kind infrastructure:
- **A cloud load balancer** in front of the apiserver: L4 vs L7, per-connection limits, idle timeouts, TLS terminated at the LB, and whether it forwards HTTP/2 RST_STREAM and client disconnects. The last point decides how often U6's "cancellation lost" happens in practice.
- **A multi-replica apiserver:** connections land on different replicas with different load, and `--goaway-chance` forces reconnects, i.e. fresh dials for the renewer. A managed control plane also has no docker-proxy hop (§2.1).
- **Exec-plugin auth** (e.g. `gke-gcloud-auth-plugin`): token fetch and refresh latency on new connections.
- **A shared, busy production apiserver** with non-exempt APF flows and real etcd latency: renewal queueing in APF (which isolation doesn't help) and the U6 bound under real queueing (41–49 s, §1.2).

Could be done on kind but was not:
- **Python async/aiohttp renewal starvation** (alignment A9): the starvation concern. B14's own rationale is parity with Python sync, plus the Go data above.
- The traced probe at +20/+100 ms and with loss; bidirectional netem with jitter.
- A packet capture or apiserver tracing to attribute the rest of the tail (L3 attributed the bulk to the apiserver's receive backlog, at the socket level).
- The hidden-retry extension of the U6 window (§1.2; relevant only if B23 is rejected), a Sandbox cascade under GC (§1.5), and the 33.5/33.9/40 s edge rows with raw logs.

---

## 3. For the orchestrator to fold into the design

Corrected after the L3 review (`brain/fcca1b1b-…/u5_review_report.md` §D). These items are folded into `go_batch_final_design.md`, `pending_user_actions.md` and `python_sdk_alignment.md` in the same round; this list records what was folded in, and why.

1. **B14** (design §10 row, §2.2 C6, §3.5, §7 Appendix C, §11; §9 E5 annotated as history).
   - **Rationale:** keep the rev 3.2 rationale (parity with Python sync, whose Lease client has its own `ApiClient`), and **add** the measured Go-side reason: above 100 creates in flight, x/net's first-fit pool puts renewals on a shared connection carrying ~70–90 creates, and their request bytes then queue in the apiserver's per-connection receive path (§2.3, L3).
   - **Data:** traced, conc 200, n = 482–1710 per run. Pooled p95 shared/isolated 544/143 ms (0 ms) and 496/162 ms (+50 ms). Worse on shared in 18/18 runs (L0, L2, L3; two-sided sign test p ≈ 7.6·10⁻⁶). Worst renewal 2.37 s shared vs 1.47 s isolated.
   - **Remove** "343 ms worst" (§2.2 C6, §3.5, §7 Appendix C, §10 B14; annotate §9 E5 rather than delete it) and the "TCP-level head-of-line blocking" argument (C6, §3.5 "No sharding", Appendix C, §11). L0's loss run showed that isolation makes a loss tail worse, not better.
   - **Add:** "Go only; Python async (A9) untested; kind adds a docker-proxy hop".
   - **Status:** stays *Tentatively accepted* (only the rationale changed), with a line saying the data now supports it.
2. **B15:** don't build it. The bound is +60 s per wire request (the global request deadline). Per SDK call it is the same **if B23 is adopted**, otherwise up to ~79 s. Delete the "+34 s" linger text (X2 committed at 41 s). Status: *Changed since you read it*, as before; its U6 condition is now met (item 4).
3. **F4 window text** (§1.6 "Stop" bullet, §3.4 stop step 3, §3.9, §9 E8 and E11, §11):
   - The window opens **only if the client's cancellation is lost** (a proxy or LB that doesn't propagate it, or a partition). Against a reachable apiserver, both SDKs' 30 s timeouts abort the create on the server (G1, P1b, P1).
   - When it opens, a create commits at most 34 s after APF dispatch: (30, 34] s after send with no APF queueing (measured); ~41 s measured with a 9 s queue wait (X2); ≤ ~49 s per the code (the 15 s maximum queue wait). Later only without B23 (~64–79 s per SDK call, source only).
   - A response that is only delayed is caught by the release rounds (C2). The orphan was reproduced with both SDKs (G2, G-ctl, P2b, P2).
   - Keep `TestLateCommitWindowIsDocumented`'s 40 s commit: a real apiserver produces it under APF queueing.
4. **B2 confirmed on kind:** GC deleted a claim committed after its owner Lease was gone 19–28 ms after the commit (G3 Go; P3b and P3 Python). A dangling or stale-UID ownerRef was collected in 124–134 ms, while the live-owner control survived (`TestU6GCDanglingOwner`). Remove "pending U6". The caveats stay: the Sandbox cascade was not tested, and a recreated Lease makes GC delete claims that point at the old UID.
5. **New decision B24** (B23 is taken): **a server-side `?timeout=` on create POSTs** (option C, §1.6; evidence X1, G-t25, X2b).
   - Options: (a) don't add it; (b) send `timeout = createTimeout − 5 s` on Go's create POSTs via `RESTClient().Post().Param("timeout", …)`, **paired with B23** (so each SDK attempt is one request with its own parameter), and ask Python to match via `api_client.call_api`.
   - Costs: the APF queue-wait limit drops from 15 s to timeout/4 (6.25 s at 25 s), so more 429s under contention (X2b); Python parity work (`create_namespaced_custom_object` has no `timeout` parameter); one more wire difference to keep in sync across the SDKs; an etcd write already in flight at the deadline remains.
   - **Recommendation: (a)**, with (b) optional. B2 already closes the Release case. The remaining case, a late commit after `Detach`, gives the next handle a claim it inherits and can free, not an orphan.
6. **Pending §4 and the design's U5/U6 rows:**
   - U5 and U6 are **done on kind** (this file).
   - Keep only what needs non-kind infrastructure, as optional confirmation: (i) whether a cloud LB or L7 proxy propagates client cancellation (RST_STREAM or disconnect), which decides how often F4 can open; (ii) renewal latency on a managed, multi-replica control plane (GOAWAY, exec-plugin auth, no docker-proxy hop); (iii) APF for a non-exempt SDK user on a busy shared apiserver.
   - Keep the **B13 FYI**, updated: the shared transport dialed 66–109 times per run while 3 connections carried the creates.
   - List separately, as doable on kind but not done: the Python async/aiohttp renewal case (A9).
7. **B22:** "client-go never queues for an HTTP/1.1 connection" stays. For HTTP/2, add that it doesn't wait for a stream slot either (GotConn→WroteHeaders 0.1 ms; it dials instead), and that the pool is **first-fit**, which is why a shared transport puts renewals on its busiest connection (B14).

---

## 4. Reproduction commands

```bash
L0=~/.gemini/jetski/brain/e3466a27-7bed-43ae-a6fa-854c8b2ea511/scratch
L1=~/.gemini/jetski/brain/34f332af-613c-4c54-b829-99be604b9768/scratch
L2=~/.gemini/jetski/brain/a6cb3ab2-fd21-4e18-aa8b-4ebdb3c62500/scratch

# --- U6 (L0) ---
# webhook: build, cert, load, deploy
cd $L0/u6/webhook && CGO_ENABLED=0 go build -o u6-webhook . && docker build -t u6-webhook:dev2 . \
  && kind load docker-image u6-webhook:dev2 --name agent-sandbox
kubectl apply -f $L0/u6/deploy.yaml
kubectl -n u6-webhook create secret tls u6-webhook-tls --cert=$L0/u6/webhook/tls.crt --key=$L0/u6/webhook/tls.key
kubectl apply -f $L0/u6/webhooks.yaml        # a=20 s; set b with:
kubectl patch mutatingwebhookconfiguration u6-slow-claims --type=json \
  -p='[{"op":"replace","path":"/webhooks/1/clientConfig/service/path","value":"/sleep/b/12"}]'
# 34 s edge (server-side only; vary b = 12 / 13.5 / 13.9 / 20)
kubectl create -f $L0/u6/claimA.yaml --request-timeout=90s
# cancellation-losing proxy
$L0/u6/proxy/u6-proxy -hold 50s -freeze 5s &
# Go prototype release path (G1 direct / G2 freeze / G3 freeze+ownerRef) and the GC test
cd $L0/u6/gomod
KUBECONFIG=~/.kube/config go test -tags kind -count=1 -v -run TestU6ReleaseLateCommit ./batchproto/
U6_CREATE_HOST=https://127.0.0.1:46444 KUBECONFIG=~/.kube/config go test -tags kind -count=1 -v -run TestU6ReleaseLateCommit ./batchproto/
U6_OWNERREF=1 U6_CREATE_HOST=https://127.0.0.1:46444 KUBECONFIG=~/.kube/config go test -tags kind -count=1 -v -run TestU6ReleaseLateCommit ./batchproto/
kubectl create ns u6-gc; KUBECONFIG=~/.kube/config go test -tags kind -count=1 -v -run TestU6GCDanglingOwner ./batchproto/
# Python (apply u6/pyfixtures.yaml first)
bash $L0/u6/u6_py.sh py-noref2 ~/.kube/config P1b          # 56105540, direct
bash $L0/u6/u6_py.sh py-noref2 $L0/u6/kubeconfig-hold P2b  # 56105540, via hold proxy
bash $L0/u6/u6_py.sh py-ref2   $L0/u6/kubeconfig-hold P3b  # 4fa72e04 (ownerRefs), via hold proxy
# C2 (late response, propagated cancel): b=9.4, then
docker exec agent-sandbox-control-plane tc qdisc add dev eth0 root netem delay 1000ms
#   ... run G1 ..., then: docker exec agent-sandbox-control-plane tc qdisc del dev eth0 root

# --- U6 review (L1): ?timeout=, APF queueing, client-go via proxy ---
# L1 deleted its local admin key, SA token and TLS key; re-extract admin.crt/admin.key/ca.crt from
# ~/.kube/config and sa.token (SA u6-apf) into $L1/x before using run.sh. The proxy must be running.
kubectl apply -f $L1/x/setup.yaml; kubectl apply -f $L1/x/mwc.yaml   # webhook r2, SA u6-apf, u6-fs/u6-plc, a/b/c webhooks
bash $L1/x/run.sh X1c admin u6-test x1c-none                         # no ?timeout: 201 at ~32 s
bash $L1/x/run.sh X1  admin u6-test x1-t25 25s                       # ?timeout=25s: 504 at 25 s, nothing committed
bash $L1/x/x2.sh X2                                                  # APF-queued: 201 at ~41 s
bash $L1/x/x2.sh X2b 25s                                             # APF-queued + ?timeout=25s: 429 at ~6.25 s
$L1/x/gp/gp ~/.kube/config https://127.0.0.1:46444 g-ctl            # client-go via freeze proxy: late commit
$L1/x/gp/gp ~/.kube/config https://127.0.0.1:46444 g-t25 25s        # same + Param("timeout","25s"): no commit
#   usage: gp <kubeconfig> <host> <name> [serverTimeout]

# --- U5 L0 sweep ---
for nm in none "delay 20ms" "delay 50ms" "delay 100ms" "delay 50ms loss 1%"; do for c in 20 200; do
  bash $L0/u5/u5_run.sh $L0/u5/out "$nm" $c 2; done; done

# --- U5 L2 traced runs ---
bash $L2/snap.sh $L2/prior                       # snapshot first
cd $L2/u5t && go build -o u5t .                  # go.work in $L2 uses the same module copy as h2fixed
bash $L2/u5t_run.sh $L2/u5t_out 3 30 10 200 4    # reps=3, burst 30 s, renew every 10 ms, conc 200, 4 renewers/transport
python3 $L2/u5t_analyze.py $L2/u5t_out > $L2/u5t_out/ANALYSIS.md
$L2/u5t/u5t cleanup ~/.kube/config               # only if a run left claims behind
kubectl delete ns batchprobe-h2; bash $L2/snap.sh $L2/after
for f in $L2/prior/*.txt; do diff -q $f $L2/after/$(basename $f); done

# --- U5 L3 socket-queue runs (the review) ---
L3=~/.gemini/jetski/brain/fcca1b1b-ce62-45ac-8a1f-dd328d626e72/scratch/ssprobe
bash $L2/snap.sh $L3/prior
bash $L3/run_ss.sh $L3/out none; bash $L3/run_ss.sh $L3/out "delay 50ms"   # uses $L2/u5t/u5t
python3 $L3/ss_analyze.py $L3/out none <burst_start_s> <burst_end_s>       # window from the run's *_c200.csv
kubectl delete ns batchprobe-h2; bash $L2/snap.sh $L3/after
```

`$L0/u6/gomod` is a scratch module (`module u6exp`) holding a copy of the prototype's non-test files. Its `go.mod` has `replace sigs.k8s.io/agent-sandbox => <notes-wt>`, so it builds against the notes worktree without writing into it. `$L2/go.work` uses `brain/6402564a-…/scratch/um`, the module copy h2fixed was built against: client-go v0.37.1, golang.org/x/net v0.59.0, Go 1.26.8.
