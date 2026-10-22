# Task 4.6 small items (D), 2026-10-22 23:10 UTC

Read-only pass. Nothing was pushed, deleted, posted or changed in a cluster. Live heads after `git fetch origin && git fetch upstream` at 23:06 match `state.md` exactly (fu7 `7392b76a`, fu8 `9bc3ded8`, fu9 `9ad5b1e0`, PR 2 `b98257e6`, PR 3 `e05c037e`, PR 4 `5f4c1433`, PR 5 `b0a74253`, PR 6 `dd816aa9`, transport `3755e0ae`, reaper `4dd8b21b`, PR 7 `7ed1ce5c`; upstream `feature/batch-claim` `c56f0791`).

## Summary

- **(a) E402** is merged code, not ours to fold. It is on upstream `feature/batch-claim` (9 hits), added by PR 1 (1/N, #1742, squash `3a1607bc`), and the same `importorskip`-then-import pattern is in three pre-existing upstream async test files (16 more hits, from #510 on `main`). Recommend: leave it.
- **(b) Branches:** 177 `review/*` + `review-diff/*` on origin. 128 keep, **49 deletion candidates** (all tips dated 2026-10-13 to 2026-10-20: the old `review/batch-*`, `review/feat/*`, `review/gofix/*`, `review/gofix2/*`, and five 10-19 `review-diff/*`). No `review/types/*`, `review/pr8/*`, `review/rename/*` or `review-diff/tls-eof-pr2` exists yet.
- **(c) Size-0 gap** confirmed at PR 6, with two effects beyond `groups` that the audit did not name: `release_group(pool)` raises `ValueError` on the re-attached handle (a no-op on the original), and the first `acquire(pool)` re-runs the pool precheck. Recommend: document, one sentence in PR 6.
- **(d) `delete_batch`:** no decision recorded anywhere in the notes. The two records disagree on the delete order (details below). Recommend: don't build it now; the reaper PR's ownerReference already makes `kubectl delete lease batch-<id>` delete the claims.
- **(e) Stack** is linear and restacked (each branch's first new commit has the branch below as its parent, no merges). Drafts below; the reaper text in `pr_table.md` omits the one-renew-interval wait, so the draft adds it. Transport must wait for B's mypy fix.
- **New, not in the brief:** upstream PR **#1990 (PR 5, 14/N) targets `main`**, not `feature/batch-claim` (measured with `gh pr view`, 35 commits); every other open PR targets `feature/batch-claim`.
- **(f)** `kubectl config current-context` = `kind-agent-sandbox` (not `kind-batch-bench` as `tasks.md` says). `kind get clusters` = `agent-sandbox`, `par-mem` (no `batch-bench`).

## Findings, ranked

### 1. #1990 targets `main` (measured, new)
`gh pr view 1990` reports `baseRefName: main`, head `b0a74253`, 35 commits; #1991 reports `feature/batch-claim`, 29 commits. `state.md` and the working rules say every open PR targets `feature/batch-claim`. Recommendation: Brian retargets #1990 to `feature/batch-claim` in the GitHub UI (base change only, no push). Reason: against `main` it shows every merged batch commit too, and a merge would land on the wrong branch.

### 2. (e) Transport and reaper PR text, stack check
**Numbering:** 16/N and 17/N are inferred from stack order in `state.md` and `pr_table.md`; Brian must confirm before opening.

**Stack (measured, `git merge-base --is-ancestor`, and the parent of each branch's first new commit is the branch below):** upstream `c56f0791` -> fu7 -> fu8 -> fu9 -> PR 2 -> PR 3 -> PR 4 -> PR 5 -> PR 6 `dd816aa9` -> transport `3755e0ae` -> reaper `4dd8b21b` -> PR 7 `7ed1ce5c`, all direct, 0 merge commits. Transport is 2 commits (`ce3d4d6e` pool check, `3755e0ae` pod IP), reaper 3 (`715a3348` ownerReference, `8f980625` reaper, `4dd8b21b` e2e tests). All authored `Brian Nguyen <brianknguyen@google.com>`, no trailers (measured).

**Blocker:** the known mypy error (`async_k8s_helper.py:105`, `"CustomObjectsApi" has no attribute "api_client"`, from transport's pool commit up) is being planned by coordinator 2 (B, `coordinator2_decisions.md` B1, awaiting Brian). It must land, and reaper and PR 7 be restacked, before transport is opened. Note B's files name the pool commit `df4dbeb2` (pre-8/N head); after the 22:51 restack it is `ce3d4d6e` (inferred same change, from the identical-tree restack in `state.md`).

**Self-contained check (verified):** the added lines of transport contain no reaper, ownerReference or warm-pacing references, and the reaper's contain no pacing or pagination references (grep of `git diff` per PR). Neither draft mentions a later feature. Title scope: six of seven open PRs use `feat(batch,python-sdk)`/`fix(batch,python-sdk)` (#1991 uses `feat(python-sdk)`), so the drafts use `batch,python-sdk`; Brian may prefer otherwise.

Transport draft (title):

    feat(batch,python-sdk): connect batch members through their watched pod IP (16/N)

Transport draft (first section; the rest is the repo PR template as on #1991):

    This PR is the sixteenth in a series of PRs to implement a batch. It makes fan-out cheaper and safer.

    - `connect()` uses the pod IP the watch already knows, so it skips one Sandbox GET per member.
    - A cached connection closes when its member is lost or failed, or the watch reports another sandbox or pod IP for it.
    - `claim_batch` fails early when the client's Kubernetes API connection pool is smaller than `max_in_flight + 2`. `SandboxClient` and `AsyncSandboxClient` take `pool_size`.

    As previous PRs have not been merged yet, this PR contains all previous, un-merged commits as well, and this PR's actual diff only consists of the last two commits.

(Changed from `pr_table.md`: "moves to another IP" becomes "the watch reports another sandbox or pod IP", matching the docstring in the transport diff and the `sandbox_id` check in `still_reaches` from plan C; "connection pool" names the Kubernetes API pool. Verified against `git diff dd816aa9 3755e0ae`.)

Reaper draft (title):

    feat(batch,python-sdk): add the batch reaper (17/N)

Reaper draft (first section):

    This PR is the seventeenth in a series of PRs to implement a batch. It cleans up batches whose driver died.

    - Batch claims carry an ownerReference to their Lease, so the garbage collector deletes them once the Lease is gone. Deleting a batch Lease by hand now deletes its claims too.
    - `python -m k8s_agent_sandbox.batch_reaper` runs once over `--namespaces` and picks Leases whose `renewTime` is older than their duration plus `--skew-margin` (60 s by default). After one renew interval it deletes each picked Lease only if it hasn't changed (a resourceVersion precondition), so a live driver whose clock runs behind is never reaped. It never touches claims, so its RBAC is `list`, `get` and `delete` on Leases, granted per namespace with RoleBindings. `--dry-run` only logs.
    - e2e tests cover the reaper and Lease ownership.

    As previous PRs have not been merged yet, this PR contains all previous, un-merged commits as well, and this PR's actual diff only consists of the last three commits.

(Changed from `pr_table.md`: adds the one-renew-interval wait and RoleBinding scope, both in the reaper's README and module docstring, `batch_reaper.py:15-27` at `4dd8b21b`; adds the hand-delete cascade from the README; adds the e2e commit.)

Recommendation: confirm 16/N and 17/N, land B's fix first, then open with these texts. Reason: numbers are inferred, and transport fails mypy today.

### 3. (c) PR 6 size-0 group gap
Verified at PR 6 `dd816aa9`:
- `batch_state.py:72-127` `reconstruct_groups` builds the group list only from pools seen on listed claims (`pool_order` filled per claim, `:91`; groups built from it, `:111`). A `size=0` group no `acquire()` has used has no claims, so the re-attached handle drops it. A size-0 group that has acquired claims comes back as `size=0` (`:125-126`). Called by `get_batch` at `sandbox_batch.py:351` and `async_sandbox_batch.py:351`.
- Effects on the re-attached handle, both shells:
  1. `groups` omits the pool (`sandbox_batch.py:189-191`, `async_sandbox_batch.py:191-193`).
  2. `release_group(pool)` raises `ValueError("... has no group or members for warm pool ...")` because `has_pool` (`batch_state.py:367-373`) is false (`sandbox_batch.py:812-813`, `async_sandbox_batch.py:836-837`). On the original handle it is a no-op.
  3. The first `acquire(pool)` re-runs the pool precheck (warm pool and template GETs), since `_checked_pools` is seeded from `state.groups` (`sandbox_batch.py:181`, `async_sandbox_batch.py:183`; `_check_pool` at `:675` / `:692`).
  Fill and quorum are unaffected, since size-0 groups are already skipped (`batch_state.py:197-199`).
- Existing doc: `get_batch` already says "A group whose claims are all gone is unknown to the new handle, so it isn't in ``groups`` and ``iter_ready_groups()`` doesn't yield it." (`sandbox_client.py:440-441`, `async_sandbox_client.py:589-590`, added by PR 3 commit `55276b8f`). An unused size-0 group never had claims, so it is covered only implicitly, and the `release_group` effect is not mentioned.
- Audit's view (`refs/pr6_independent_audit.md:75`, quoted): "**Agree** with its audit item 2 (a re-attached handle drops an unused size-0 group) and its audit item 3 (templates aren't cached across calls). Both are low impact, and documenting is enough."

Options:
- **A. Document (recommended).** In PR 6's commit, extend the existing sentence in both `get_batch` docstrings, for example: "A group whose claims are all gone, including a ``size`` 0 group that ``acquire()`` hasn't used, is unknown to the new handle, so it isn't in ``groups``, ``iter_ready_groups()`` doesn't yield it, and ``release_group()`` on it raises ``ValueError``." Cost: two docstrings plus the docs regen, on a review copy. Note `release_group`'s `ValueError` already applies to an all-gone group today, so the sentence describes existing behavior.
- **B. Store size-0 pools on the Lease.** A new annotation written in `claim_batch`'s Lease create (no extra request) and parsed in `get_batch`. Cost: a new annotation format and its validation in both shells, tests, and an ordering rule (size-0 groups have no ordinals, so `reconstruct_groups`' lowest-ordinal order can't place them). It also runs against the 2026-10-13 choice to keep group data on claims, not the Lease (no `batch-size` annotation, "Per-claim").
- **C. Let `release_group` accept unknown pools as a no-op.** Smaller than B, but changes approved PR 5 behavior for every unknown pool, which also hides typos. Rejected.

Recommendation: A. Reason: the only costs are one precheck GET pair and a `ValueError` on a call that would free nothing; B adds state and a format for that.

### 4. (d) `delete_batch`
Carryover entry (`review_carryover_notes.md:175`, quoted): "**An SDK path to clean up a batch that can't be re-attached.** After a crash, `get_batch` raises, so the SDK has no way to delete a stale batch before the reaper or `shutdownTime` does; today that takes `kubectl delete sandboxclaims -l agents.x-k8s.io/batch-id=<id>` plus deleting the Lease. A `client.delete_batch(batch_id, namespace)` doing exactly the reaper's steps (label `deletecollection`, bounded re-list, then Lease delete, never adopting the Lease) wouldn't race the reaper, since both only delete. Worth raising with Brian when the reaper PR is planned, since the two would share code."

Recorded decision: none found (grep of the whole notes branch, and `git log -S delete_batch`). Order history (verified in `git log -L` of `reaper_contract.md:158`): on 2026-10-08 the contract said "it uses the same steps and order as the reaper: precondition delete of the Lease, then the claims" (this is the "reaper's delete order" `tasks.md` refers to). Option B (`5c094941`, 2026-10-19) changed it to: "If built, it uses `Release`'s steps (claims, then the Lease), since its caller is waiting for the sandboxes." The reaper now deletes only Leases, so "the reaper's steps" in the carryover entry is out of date.

What it would do: a client method (both shells) that calls the existing `_delete_batch_objects(helper, namespace, batch_id, lease_name)` (`sandbox_batch.py:1481`, `async_sandbox_batch.py:1507` at PR 7), that is, `Release`'s label `deletecollection` rounds then the Lease delete. Cost: small code (two methods, docstrings, tests, docs regen) but a new PR, and one open safety question: unlike `release()` it holds no Lease, so without a check it deletes a live batch held by another driver. A guard (read the Lease, refuse unless stale or unheld) is more code and a new rule.

Alternatives: (1) do nothing; after the reaper PR, `kubectl delete lease batch-<id>` deletes the claims through the ownerReference (reaper README in `4dd8b21b`), or wait for the reaper. (2) Mention that one-liner in the docs PR (4.5). Recommendation: don't build it; add the one-liner to the docs PR. Reason: the reaper PR already gives a one-command cleanup, and `delete_batch` would add a new API with a live-batch hazard.

### 5. (a) E402 in `test_async_sandbox_batch.py`
Measured with `pycodestyle --select=E402` on the file at each head: 9 hits everywhere, from upstream `feature/batch-claim` through PR 7. Lines: 29, 30, 32-35, 39, 45, 52 at upstream and fu7 to fu9; 30-31, 33-36, 40, 46, 53 at PR 2; 31-32, 34-37, 41, 52, 65 at PR 3 to PR 7 (the "line 64" of the older head). The count never grows: the stack only adds names inside existing import statements.
- Cause: `pytest.importorskip("kubernetes_asyncio")` at line 27 (upstream), added by **PR 1 (1/N, #1742, `3a1607bc`)**; blame of the import block is `3a1607bc` except line 32 (`31a47728`, 3/N #1925) and line 52 (`4bb0db30`, 4/N #1982). All merged, so a fix would be its own small upstream PR, not a fold.
- The same pattern is upstream's own style: `test_async_k8s_helper.py` (6), `test_async_sandbox.py` (2), `test_async_sandboxclient.py` (8) hit E402 identically on upstream `main` (from #510, #531, #999). Other batch test files (`test_sandbox_batch.py`, `test_batch_state.py`, `test_batch_utils.py`, `test_batch_reaper.py`) have no `importorskip` and no E402.
- Upstream has no Python lint in CI.

Recommendation: leave it. Reason: it matches the established pattern of the neighboring files, the working rules say match the file's style, and it is merged. If Brian wants it gone: a one-commit upstream PR off `feature/batch-claim` adding `# noqa: E402` to the 9 lines (or moving the imports into a `try`), ideally for all four async test files at once.

### 6. (b) Remote review branches
Measured: `git for-each-ref` on `refs/remotes/origin/review/` and `review-diff/` after the fetch (177; `git ls-remote` agrees, 177). Rule: keep if the tip commit is dated 2026-10-21 or later (UTC), or the prefix is used by a live process. Tip date is a proxy for creation date (git does not record branch creation), so this is inferred. Every live-process prefix happened to be dated 10-21 or later anyway.

Keep, 128:

| Prefix | Count | Tip dates |
| :-- | --: | :-- |
| `review/fix3/*` | 13 | 10-22 (superseded per `state.md`, kept by the named-in-`state.md` rule) |
| `review/fu3-comments/*` | 15 | 10-21 |
| `review/fu6-lease/*` | 12 | 10-22 |
| `review/group-order/*` | 8 | 10-21 |
| `review/notes-stale-fix` | 1 | 10-22 |
| `review/planc/*` | 12 | 10-22 (superseded per `state.md`) |
| `review/restack-5n/*`, `-6n/*`, `-8n/*` | 13, 12, 11 | 10-22 |
| `review/tls-eof/*` | 8 | 10-22 (superseded per `state.md`, kept by the brief) |
| `review/vicente/*` | 12 | 10-22 |
| `review-diff/*` | 11 | 10-21 to 10-22 |

If Brian wants a second round later, `planc`, `fix3`, `tls-eof`, `fu3-comments` (fu3 merged), `group-order` and `restack-5n/6n` look superseded (inferred from `state.md`), but they are inside the keep rule, so they are not listed below.

Deletion candidates, 49 (none named in `state.md`, `tasks.md` or `work/`; checked by grep):

| Branch | Tip | Tip date (UTC) | Subject |
| :-- | :-- | :-- | :-- |
| `review/batch-1-core` | `81bc0eca` | 2026-10-13 07:48 | docs(python-sdk): regenerate Python SDK reference for batch handles |
| `review/batch-2-claim` | `2a30e8ff` | 2026-10-13 07:48 | docs(python-sdk): regenerate Python SDK reference for claim_batch |
| `review/batch-3-group-consumers` | `6f999e4c` | 2026-10-13 07:48 | docs(python-sdk): regenerate Python SDK reference for events and iter_ready_groups |
| `review/batch-4-group-quorum` | `121fb82d` | 2026-10-13 07:48 | docs(python-sdk): regenerate Python SDK reference for wait_for_quorum |
| `review-diff/fu2-manager-items` | `ce1b7648` | 2026-10-19 13:07 | review diff: fu2 manager items 1 and 4 |
| `review-diff/fu3-janet-j1-j3` | `a49162a5` | 2026-10-19 13:07 | review diff: fu3 Janet J1/J2 and J3 |
| `review-diff/fu5-janet-j5` | `c8de74fb` | 2026-10-19 13:07 | review diff: fu5 Janet J5 and CodeRabbit C1 |
| `review-diff/pr1-rebase-edits` | `6050072d` | 2026-10-19 13:07 | review diff: PR 1 rebase edits |
| `review-diff/pr2-atexit-carry` | `51944fbf` | 2026-10-19 13:07 | review diff: PR 2 atexit carry |
| `review/feat/batch-3-group-consumers-x40gnl` | `cfcf247c` | 2026-10-19 16:59 | docs(python-sdk): regenerate Python SDK reference for events and iter_ready_groups |
| `review/feat/batch-4-group-quorum-ow91j5` | `706781b5` | 2026-10-19 16:59 | docs(python-sdk): regenerate Python SDK reference for wait_for_quorum |
| `review/feat/batch-1-fu1-watch-retries` | `cc4e8873` | 2026-10-19 19:05 | fix(python-sdk): keep the batch watch alive across network failures |
| `review/feat/batch-1-fu2-lease-renewal` | `2d71b265` | 2026-10-19 19:05 | docs(python-sdk): regenerate Python SDK reference for lease_degraded |
| `review/feat/batch-1-fu3-member-state` | `08aaa75a` | 2026-10-19 19:05 | docs(python-sdk): regenerate Python SDK reference for MemberState |
| `review/feat/batch-1-fu4-terminating-claims` | `4f822524` | 2026-10-19 19:05 | fix(python-sdk): treat a terminating batch claim as lost |
| `review/feat/batch-1-fu5-connect-checks` | `728a159a` | 2026-10-19 19:05 | docs(python-sdk): regenerate Python SDK reference for connect |
| `review/feat/batch-1-fu6-group-order` | `c8b58cef` | 2026-10-19 19:05 | fix(python-sdk): keep a batch's group order when re-attaching |
| `review/feat/batch-1-fu7-client-cleanup` | `9cfd4ea3` | 2026-10-19 19:05 | fix(python-sdk): close cached batch connections on client cleanup and keep newer handles registered |
| `review/feat/batch-1-fu8-task-names` | `b3b29ecc` | 2026-10-19 19:05 | refactor(python-sdk): start async batch tasks with create_task and name them |
| `review/feat/batch-1-fu9-concurrent-detach` | `4cbc6c1d` | 2026-10-19 19:07 | fix(python-sdk): let concurrent detach() calls on a batch handle both succeed |
| `review/feat/batch-2-claim` | `a242cf84` | 2026-10-19 19:07 | docs(python-sdk): regenerate Python SDK reference for claim_batch |
| `review/feat/batch-3-group-consumers` | `1f14246e` | 2026-10-19 19:07 | docs(python-sdk): regenerate Python SDK reference for events and iter_ready_groups |
| `review/feat/batch-4-group-quorum` | `4ea07cff` | 2026-10-19 19:07 | docs(python-sdk): regenerate Python SDK reference for wait_for_quorum |
| `review/feat/batch-5-release-group-76a8m3` | `2d2a4e78` | 2026-10-19 19:07 | docs(python-sdk): regenerate Python SDK reference for the release methods |
| `review/feat/batch-6-acquire-nbbp5j` | `81ade44a` | 2026-10-19 19:07 | docs(python-sdk): regenerate Python SDK reference for acquire |
| `review/feat/batch-reaper-ymtd6t` | `05104311` | 2026-10-19 19:07 | test(python-sdk): add batch reaper and Lease ownership e2e tests |
| `review/feat/batch-transport-ltlfdm` | `d606a076` | 2026-10-19 19:07 | feat(python-sdk): connect batch members through their watched pod IP |
| `review/gofix/feat/batch-2-claim` | `671fd987` | 2026-10-20 22:13 | docs(python-sdk): regenerate Python SDK reference for claim_batch |
| `review/gofix/feat/batch-3-group-consumers` | `916fe5ed` | 2026-10-20 22:13 | docs(python-sdk): regenerate Python SDK reference for events and iter_ready_groups |
| `review/gofix/feat/batch-4-group-quorum` | `2bf1a035` | 2026-10-20 22:13 | docs(python-sdk): regenerate Python SDK reference for wait_for_quorum |
| `review/gofix/feat/batch-5-release-group-76a8m3` | `84701185` | 2026-10-20 22:13 | docs(python-sdk): regenerate Python SDK reference for the release methods |
| `review/gofix/feat/batch-6-acquire-nbbp5j` | `95175c6c` | 2026-10-20 22:18 | docs(python-sdk): regenerate Python SDK reference for acquire |
| `review/gofix/feat/batch-reaper-ymtd6t` | `e478341f` | 2026-10-20 22:18 | test(python-sdk): add batch reaper and Lease ownership e2e tests |
| `review/gofix/feat/batch-transport-ltlfdm` | `c0429f87` | 2026-10-20 22:18 | feat(python-sdk): connect batch members through their watched pod IP |
| `review/gofix2/feat/batch-1-fu2-lease-renewal` | `625d4a58` | 2026-10-20 23:11 | docs(python-sdk): regenerate Python SDK reference for lease_degraded |
| `review/gofix2/feat/batch-1-fu3-member-state` | `781e830c` | 2026-10-20 23:11 | docs(python-sdk): regenerate Python SDK reference for MemberState |
| `review/gofix2/feat/batch-1-fu4-terminating-claims` | `ccd23c3b` | 2026-10-20 23:11 | fix(python-sdk): treat a terminating batch claim as lost |
| `review/gofix2/feat/batch-1-fu5-connect-checks` | `20f3fb62` | 2026-10-20 23:11 | docs(python-sdk): regenerate Python SDK reference for connect |
| `review/gofix2/feat/batch-1-fu6-group-order` | `e6ba6c53` | 2026-10-20 23:11 | fix(python-sdk): keep a batch's group order when re-attaching |
| `review/gofix2/feat/batch-1-fu7-client-cleanup` | `46e10298` | 2026-10-20 23:11 | fix(python-sdk): close cached batch connections on client cleanup and keep newer handles registered |
| `review/gofix2/feat/batch-1-fu8-task-names` | `dd4f158c` | 2026-10-20 23:11 | refactor(python-sdk): start async batch tasks with create_task and name them |
| `review/gofix2/feat/batch-1-fu9-concurrent-detach` | `a40b83f4` | 2026-10-20 23:11 | fix(python-sdk): let concurrent detach() calls on a batch handle both succeed |
| `review/gofix2/feat/batch-2-claim` | `1e12903a` | 2026-10-20 23:11 | docs(python-sdk): regenerate Python SDK reference for claim_batch |
| `review/gofix2/feat/batch-3-group-consumers` | `5250d4d6` | 2026-10-20 23:11 | docs(python-sdk): regenerate Python SDK reference for events and iter_ready_groups |
| `review/gofix2/feat/batch-4-group-quorum` | `fe5f6f21` | 2026-10-20 23:12 | docs(python-sdk): regenerate Python SDK reference for wait_for_quorum |
| `review/gofix2/feat/batch-5-release-group-76a8m3` | `f6bb08e2` | 2026-10-20 23:12 | docs(python-sdk): regenerate Python SDK reference for the release methods |
| `review/gofix2/feat/batch-6-acquire-nbbp5j` | `69ee5643` | 2026-10-20 23:12 | docs(python-sdk): regenerate Python SDK reference for acquire |
| `review/gofix2/feat/batch-reaper-ymtd6t` | `4fa72e04` | 2026-10-20 23:12 | test(python-sdk): add batch reaper and Lease ownership e2e tests |
| `review/gofix2/feat/batch-transport-ltlfdm` | `56105540` | 2026-10-20 23:12 | feat(python-sdk): connect batch members through their watched pod IP |

### 7. (f) Local cluster state (measured, read-only)
`kubectl config current-context` returns `kind-agent-sandbox`. `kind get clusters` returns `agent-sandbox` and `par-mem`. `tasks.md` 4.6(f) expects `kind-batch-bench`; that cluster is not in the list. Recommendation: update `tasks.md` (stack conversation's file) and confirm with Brian which cluster any future benchmark uses. Reason: the recorded context is stale.

## Decisions for Brian

1. **#1990 base:** retarget PR 5 (#1990) from `main` to `feature/batch-claim`? Recommend yes.
2. **Numbering:** confirm transport = 16/N and reaper = 17/N (inferred from stack order), and the title scope `feat(batch,python-sdk)`.
3. **Texts:** approve the transport and reaper drafts above (they differ from `pr_table.md` where noted). Open transport only after B's mypy fix lands and reaper and PR 7 are restacked.
4. **(c) size-0 gap:** document it (option A, one sentence in both `get_batch` docstrings in PR 6, on a review copy) or store size-0 pools on the Lease (B)? Recommend A.
5. **(d) `delete_batch`:** build it (and if so with `Release`'s order, claims then Lease, plus a live-Lease guard), or skip it and add the `kubectl delete lease batch-<id>` line to the docs PR? Recommend skip plus docs line.
6. **(a) E402:** leave it (merged, matches upstream's own async tests), or a separate small upstream PR with `# noqa: E402` across the four async test files? Recommend leave it.
7. **(b) Branches:** OK to delete the 49 candidates listed above? Nothing is deleted until you say so.
