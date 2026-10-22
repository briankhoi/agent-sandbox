# Task list (do in order; ask Brian before starting the next)

Read `project_instructions.md`, `working_rules.md` and `state.md` first. Each task says what else to read. "Needs OK" marks work that waits for Brian's approval before you start or before you act.

---
## Task 1. Restack onto the merged 5/N and get 6/N (fu5, #1984) ready for review

**Goal.** Upstream merged 5/N (fu4, #1983). Rebase the remaining stack onto the new upstream `feature/batch-claim`, prepare 6/N for review, and handle review comments Brian pastes.

**Read.** `state.md` (branch table), `working_rules.md` (restack, checks), `refs/pr_table.md` (fu5 description text), `refs/group_order_precheck_sync.md` (what the last sync pushed).

**Steps.**
1. `git fetch origin && git fetch upstream`. Confirm the real heads equal the `state.md` table (the group-order/precheck sync already ran). If a head differs, stop and tell Brian before touching anything.
2. Find the new base: `git rev-parse upstream/feature/batch-claim`. As of 22:30 UTC on 2026-10-21 the 5/N squash is `cc4a40bf` ("treat a terminating batch claim as lost (#1983)"), the upstream tip, with parent `4bb0db30` and tree `663a0951`. If upstream has moved, find that commit by its title. Check the trees match: `git rev-parse cc4a40bf^{tree} origin/feat/batch-1-fu4-terminating-claims^{tree}` (both `663a0951`). If they differ, stop and report (someone changed the code during merge).
3. Restack in order, old parent = the previous branch's pre-restack head: fu5, fu6, fu7, fu8, fu9, PR 2, PR 3, PR 4, PR 5, PR 6, transport, reaper, PR 7 (`git rebase --onto upstream/feature/batch-claim 2b8424a1 feat/batch-1-fu5-connect-checks`, then each next branch onto the rebased previous one, using the old previous head as the upstream argument). fu4's commits are dropped because they are merged. Do this on `review/restack-5n/<branch>` copies and push them, not on the real branches.
4. Prove it mechanical: for each branch, `git diff <old-head> <new-head>` must be empty (identical tree), and `git range-diff` shows only the base change. Run mypy and the author check on every commit, and the changed tests of fu5 only. If any tree differs or a rebase conflicts, stop and report; do not resolve silently.
5. Report to Brian with the compare links (`review/restack-5n/...` vs old heads should be empty diffs) and the sync recipe: a `git push --atomic origin --force-with-lease=...` command with one lease per real branch pinned to the heads you started from. **Run the sync only after Brian says OK** (open-PR branches).
6. **6/N ready for review** means: fu5 rebased, `connect()` checks `err()` then readiness before the cached connection (`sandbox_batch.py`, `async_sandbox_batch.py`), tests for both shells pass, lint, mypy and author check clean, docs regenerate with no diff, and Brian has a draft description (the fu5 text in `refs/pr_table.md`, "This PR is the sixth in a series..." with the literal "(6/N)" in the title; for any later PR, also set the ordinal word in the first sentence and the title's N to the table in `state.md`). Tell Brian what to check in one line.
7. **Review comments.** Brian will paste CodeRabbit or reviewer comments on #1984 (and later ones). For each: read the code and decide valid or invalid. Valid: fix it in the owning commit on the review copy, rebase dependents, give a compare link and one line. Invalid or already handled: give a paste-ready reply with the reason and `file:line`. Do not post anything.

**Done when.** The restacked review copies are pushed with empty diffs, mypy and author checks are green, Brian has the sync command and the 6/N brief, and each pasted comment has either a fix commit link or a paste-ready reply.

### Also in this task: PR 2 description line (Brian pastes it himself)
For the PR 2 description on #1824, Brian adds this line himself (do not edit GitHub): "Each group's first `min_ready` claims are created first, one group after another in group order, and the rest follow in the same order, so the first groups can be consumed while later groups are still being created."

---
## Task 2. Fold the async TLS EOF piece into PR 2 (needs the TLS PR decision)

**Problem.** PR 2's async `_is_transport_error` (`async_sandbox_batch.py`, near line 84 on `feat/batch-2-claim`) rejects every `aiohttp.ClientSSLError`, so a TLS EOF during create or release is treated as non-transient. The sync helper in the merged PR 1 code is being changed (separate branch `feat/batch-ssl-eof-retry`, `8937ccdb`) to treat `ssl.SSLEOFError` as transient and keep certificate and other TLS failures fatal.

**Read.** `git show origin/feat/batch-ssl-eof-retry` (the whole change; it also changes the watch loop's `except aiohttp.ClientSSLError` branch using `e.os_error` being `ssl.SSLEOFError`), and PR 2's two `_is_transport_error` helpers.

**Steps.**
1. Do this restack **after** Task 1's sync, as a change to the PR 2 commit that introduces the async helper (fixup + autosquash), on a `review/tls-eof/...` copy.
2. Change the async helper so a `ClientSSLError` whose `os_error` is `ssl.SSLEOFError` counts as transient, and every other `ClientSSLError` stays non-transient. Match the wording of the comments between the sync and async helpers (identical comments rule).
3. The sync helper in PR 2's file is the same function as in merged PR 1 code. Check whether the TLS PR is merged upstream yet (`git log upstream/feature/batch-claim --oneline | grep -i 'TLS EOF'`). If not merged, the sync side of PR 2 still rejects SSLEOFError, which breaks parity. If the TLS PR lands first, expect a textual conflict in the async watch loop's `ClientSSLError` block during the PR 2 restack (PR 2's file still has the old block, which the TLS branch rewrites); take the TLS wording. `feat/batch-ssl-eof-retry` sits on `4bb0db30` and need not be restacked unless Brian opens it; if he does, rebase it onto the upstream tip then. Ask Brian: either he opens the TLS PR first (`feat/batch-ssl-eof-retry`, base it on the current upstream tip, restack if needed) and PR 2 waits for it, or you also apply the sync change in PR 2's tree.
4. Add or extend a test in `test_async_sandbox_batch.py` for the create/release path (one owning test per contract; the sync side's coverage comes from the TLS PR). Mutation-check it: reverting the `os_error` check must fail it.
5. Rebase PRs above PR 2, run the checks, give Brian a compare link.

**Done when.** A review copy shows the async helper retrying SSLEOF only, tests pass, mutation check done, dependents rebased with empty-tree diffs apart from this change.

---
## Task 3. Design-doc updates (notes branch)

**Why.** The notes still describe the old create order ("interleaved across groups in proportion to their sizes"). Brian approved group-by-group on 2026-10-21 and it is built in PR 2.

**Read.** `batch_claim_proposal.md` line ~58, `review_carryover_notes.md` line ~90 (the "PR 2" row of "Approved review decisions"), `refs/work_plan.md` (the PR 2 row also says "interleaved"), `refs/benchmark_rerun_review.md` section 3A for the evidence, and the code in `feat/batch-2-claim` (the create-plan function, `fill_create_plan` in `batch_utils.py`) so the text matches what is built.

**Steps.** In the notes worktree (never a code branch), change minimally and keep Brian's wording elsewhere:
1. Proposal and carryover row: creates go group by group in the caller's group order, each group's first `min_ready` claims first, then the rest in the same order; ordinals stay contiguous. Record "approved by Brian 2026-10-21 18:28" in the carryover row (the cloud project's note says he approved moving it into PR 2 and fixing the precheck race; use the verbatim words given under Approval wording below).
2. Record in `review_carryover_notes.md` roadmap (and in `refs/work_plan.md`, editing that copy in place, since the cloud original is no longer maintained): the PR 7/PR 8 split (PR 7 = warm-aware pacing for the fill and `acquire()`; PR 8 = claim-list pagination, metadata-only re-list in `release()`, skip the deletecollection body); acquire pacing; Design C approval (2026-10-21 09:49) and the 300 s RL Lease default (2026-10-21 18:28); the group-order move into PR 2; the PR 6 precheck single-flight.
3. Author as Brian Nguyen, message prefix `notes:`, push to `origin batch-primitives-notes` (normal push, no force). Do not touch STALE files.

**Done when.** `grep -rn -i interleav batch-primitives/*.md` shows no remaining claim about the current create order (history in `pr2_design.md` and STALE files can stay), Brian has the commit link, and Brian has the before and after text of the proposal paragraph (`batch_claim_proposal.md` line ~58) so he can mirror it in his Google Doc by hand.

**Approval wording.** Brian's words on 2026-10-21 18:28, verbatim: "ok to move group order into PR 2 and keep other items in PR 7/8". Quote these in the carryover row; do not paraphrase.

---
## Task 4. Later queue (each item needs Brian's OK to start; work in this order)

For each item, bring Brian a short plan first. Items marked RL are not yours.

Write plans and run prompts as files under `batch-primitives/handoff/work/` on the notes branch (or paste them to Brian). Done criteria: 4.1 a review copy `review/pr8/...` with before and after memory numbers; 4.2 the two RSS curves plus drafted issue text; 4.3 an audited run prompt, then the results report; 4.4 a short plan file listing each `Any` and its replacement type; 4.5 a draft PR with runnable examples and the `detach(grace=...)` sentence.

| # | Item | Details | Needs OK |
| :-- | :-- | :-- | :-- |
| 4.1 | **PR 8 (was 7b)** | Claim-list pagination (`limit=500` with continue tokens, used by `get_batch` attach and the `release()` re-list; the first page's resourceVersion starts the watch), a metadata-only paginated re-list in `release()` (`PartialObjectMetadataList`; `limit=1` and watch were rejected), and skip parsing the deletecollection response (`_preload_content=False`; `delete_sandbox_claims_by_label` in `k8s_helper.py` and `async_k8s_helper.py` calls `delete_collection_namespaced_custom_object` without `_preload_content=False`, so the client parses and discards the whole deleted list; verify in both helpers). Measured: one list of 5,000 claims peaks at 69.4 MB; `release()` peaks at 69.5 MB. Branch off PR 7 (`feat/batch-7a-create-path`) as `feat/batch-8-...`. Evidence: `refs/benchmark_rerun_review.md` sections 3B, 6 and 7. Measure the memory effect before and after. | Yes (plan), then review copy |
| 4.2 | **Async memory probe** | Run the async baseline (per-claim, no batch code) with and without `MALLOC_ARENA_MAX=2` for about 10 minutes of the rolling scenario (`6v2-baseline-async`, see `refs/benchmark_rerun_prompt.md` and the harness on `origin/bench/batch-claim`, `test/benchmarks/batch-claim/bench.py`). If growth flattens it is allocator fragmentation, otherwise a leak. Then draft an upstream issue for Brian to file with the numbers (baseline grew 93 to 514 MB; Python-traced memory stayed flat at 11 MB). It does not block the series. | Yes (uses the kind cluster) |
| 4.3 | **SDK validation run** | After PR 7/8: about 45 minutes locally. 3s warm and RL shapes at the PR 7 defaults versus the baseline, 3 runs each; 3a2 cold; 6v2a rolling; 100 groups x 40 sandboxes. Then optionally a kwok run (`dev/ci/presubmits/test-e2e-scalability-kwok`, 100 simulated nodes, useful for warm scale 1,000 to 10,000 claims) and a GKE scale run (Brian's GKE project is unknown, so ask). Write the run prompt in the style of `refs/benchmark_rerun_prompt.md` (resumable, SHA-pinned, fair comparisons: same limiting mechanism on both arms, lead with unlimited vs unlimited). Have it self-checked by a fresh subagent before you run it. | Yes (cluster; GKE needs details) |
| 4.4 | **Type tightening** | A plan to replace loose types (`Any` where an exact type exists) in the batch modules, awaiting Brian's OK. The original plan text is not saved anywhere, so write a short new one: list the `Any` and untyped spots in `batch_*.py` and the two handles, what each would become, the mypy impact, and keep it out of the open PRs until approved. **Do soon, before transport is opened upstream (Brian, 2026-10-22).** Known hard mypy error (measured 2026-10-22, mypy 1.20.2 with `kubernetes_asyncio` 36.1.0): from transport's first commit ("check the Kubernetes API connection pool first", `8e17ca4d` on `review/restack-5n/feat/batch-transport-ltlfdm`) up to PR 7, `async_k8s_helper.py:105` `api_pool_size()` reads `self.custom_objects_api.api_client.configuration` and mypy reports `"CustomObjectsApi" has no attribute "api_client" [attr-defined]`. Every commit through PR 6 is clean. Cause: 36.1.0 ships `py.typed` (33.3.0 does not), and `dev/tools/test-unit` force-reinstalls an unpinned `kubernetes_asyncio` after the `<34` pin, so `make test-unit` likely hits this too (inferred, CI not run). Candidate fix, proposal only: read the typed `self._api_client` (`client.ApiClient \| None`, set before the APIs are built) in `api_pool_size()`, folded into the transport commit that adds it, on a review copy. The sync helper `k8s_helper.py:109` uses `cast(...)` on the same attribute; check it for parity. | Yes, plan only |
| 4.5 | **Examples and docs PR** | Runnable versions of the proposal's usage examples, the driver Role, the reaper manifests README (`examples/batch-reaper/` was deliberately not added; location and image are still an upstream discussion), and one line on calling `detach(grace=...)` on SIGTERM so a preempted driver can re-attach with `get_batch` (`adopt_expired` stays cut). After PR 6 and the reaper. Go SDK is a later separate effort (Brian's local agent; do not start without being asked). | Yes |
| 4.6 | **Small open items** | (a) The E402 nit in `test_async_sandbox_batch.py`: module-level imports (`import aiohttp`, `from kubernetes_asyncio ...`, `from k8s_agent_sandbox ...`) come after `pytest.importorskip("kubernetes_asyncio")`, which pycodestyle reports as E402 (it was reported at line 64 on an older head; find it with `pycodestyle --select=E402`). Ask Brian whether to fix it, and fold it into the PR whose commit added the line. (b) Delete stale `review/*`, `review/gofix*` and `review-diff/*` remote branches (**needs Brian's explicit OK**; list them first). (c) The PR 6 size-0 group gap: a re-attached handle (`get_batch`) forgets an unused `size=0` group. Not decided. The PR 6 independent audit (`refs/pr6_independent_audit.md`, "Where I agree and disagree") agreed with the earlier audit's item 2 and called it low impact, so documenting it is enough. The alternative was storing size-0 pools on the Lease. Ask Brian; if he agrees to document, the docstring change goes into PR 6 on a review copy (**needs OK**). (d) The `delete_batch` idea is in `review_carryover_notes.md` under "Ideas raised but not planned" (`client.delete_batch(batch_id, namespace)` doing the reaper's steps; the reaper review says to use the reaper's delete order). There was a decision card, and its outcome is not recorded. Ask Brian whether he wants it before doing anything (**needs OK**). (e) Opening the transport and reaper PRs upstream: Brian does this himself; you prepare title and description text from `refs/pr_table.md` and check the stack is restacked. (f) Local kubeconfig context was left at `kind-batch-bench`; check `kubectl config current-context` before any cluster work. | Mixed (see each) |

---
## Task 5. Re-commit the RL claim_mode branch with Brian as committer (small, any time)

**Why.** The 4 commits on `feat/rl-batch-claim-mode` (head `e606b9bd` on 2026-10-22) have author Brian Nguyen but committer `Claude <noreply@anthropic.com>`. The cloud project asked for this on 2026-10-22. The trees must stay identical.

**Steps.**
1. `git fetch origin`. Check that the head is still `e606b9bd` and that `git log --format='%h %an | %cn %s' -4 origin/feat/rl-batch-claim-mode` shows the 4 commits (`9bcee22c`, `496ea1e8`, `c1fcca9a`, `e606b9bd`) with committer Claude. If the head moved, stop and ask Brian.
2. The branch sits on `769b7f2e`, the pre-5/N-restack PR 7. Its tree equals today's PR 7 tree (`99697680`). Re-commit in place, keeping the same parent so the trees can't change (recipe checked in a cloud scratch clone: same 4 trees, Brian as committer): `git switch -c review/recommit/rl-batch-claim-mode origin/feat/rl-batch-claim-mode`, then `GIT_COMMITTER_NAME="Brian Nguyen" GIT_COMMITTER_EMAIL=brianknguyen@google.com git rebase --no-ff --committer-date-is-author-date 769b7f2e`.
3. Verify. For each of the 4 commits, old and new trees are equal (`git rev-parse <old>^{tree} <new>^{tree}`), `git diff origin/feat/rl-batch-claim-mode HEAD` is empty, and `git log --format='%an <%ae> | %cn <%ce>' 769b7f2e..HEAD` shows Brian on both sides. Messages carry no `Co-Authored-By` or `Claude-Session` trailers.
4. This branch has no open upstream PR as far as the notes know (Brian opens PRs himself, so ask if unsure). Push with `git push origin --force-with-lease=feat/rl-batch-claim-mode:e606b9bd HEAD:feat/rl-batch-claim-mode` and give Brian the new head.

**Done when.** The branch head has 4 commits with Brian as author and committer, identical trees, and Brian has the new SHA.

---
## Task 6. Rename `iter_ready_groups()` to `group_outcomes()` and `GroupReady` to `GroupOutcome` (approved)

**Approval.** Brian, 2026-10-22 00:45 UTC, verbatim (excerpt): "should we rename wait for wait for quorum, events, and iter ready groups to be more clear to users of what they do? i feel like its not immediately obvious, but man it would be a large effort". The recommendation is in the cloud project file `design/batch_api_naming.md`. Brian, 2026-10-22 11:56 UTC, verbatim: "i approve the naming call - can you add this as a task in the remote branch so the other agent can pick it up?"

**What was approved** (the recommendation he approved, nothing more):
- `iter_ready_groups()` becomes `group_outcomes()` on both handles, and the type `GroupReady` becomes `GroupOutcome`. The old name misled because failed groups are yielded too (`GroupReady(error=...)`, `batch_state.py` `_set_group_outcome` calls), and the PR 3 description said it "raises when it can't", which is wrong. "Outcome" is already the code's word (`pop_group_outcome`, `_set_group_outcome`, "as soon as its outcome is known").
- `wait_for_quorum()` and `events()` keep their names.
- Two docstring edits, both handles: the first line of `events()` becomes "Hands out each initial claim once, as it becomes Ready, fails, or is lost." The group method's "It fails with ``QuorumUnreachableError`` once ... or with ``BatchTimeoutError`` ..." becomes "A failed group is yielded with ``error`` set, not raised. The error is ``QuorumUnreachableError`` once ..., or ``BatchTimeoutError`` ...". This makes clear the iterator doesn't raise for a failed group.
- No aliases and no deprecation, because none of this is merged upstream (`upstream/feature/batch-claim` has none of the names; measured 2026-10-22).

**When.** After the pending stack work lands: the `review/fu6-lease/*` sync, the plan C TLS insertion and the renumbering (see `state.md`). The rename is head-independent, so run it on whatever the live heads are then. Ask Brian before starting.

**Read.** `state.md` (branch order), `working_rules.md` (review copies, checks), the script `handoff/work/rename_group_outcomes.py` (this task's only code change), and `refs/pr_table.md` rows for PR 3 to PR 7.

**Steps.**
1. **Check upstream first.** For #1988 (PR 3) and #1989 (PR 4), and also #1990 and #1991, whose docstrings mention the name, read every review comment: `gh api --paginate repos/kubernetes-sigs/agent-sandbox/pulls/<n>/comments`, `.../pulls/<n>/reviews` and `.../issues/<n>/comments`. Grep the bodies for `iter_ready_groups|GroupReady|wait_for_quorum|events\(|naming|rename`. If a reviewer asked for these names, approved them, or proposed others, stop and bring Brian the links before changing anything.
2. **Where the name lives** (measured 2026-10-22 at PR 7 `24a4e9a1`; recount on the live heads). 12 files: `k8s_agent_sandbox/{__init__,models,batch_state,sandbox_batch,async_sandbox_batch,sandbox_client,async_sandbox_client}.py`, `test/unit/test_sandbox_batch.py`, `test/unit/test_async_sandbox_batch.py`, the SDK `README.md` (a docs-site mount, `/docs/python-client/`), `docs/python_sdk_reference.md` and `test/e2e/clients/python/test_batch_e2e.py`. Commits that add or change the name or the two docstrings:
   - PR 3, all 5: "add batch event models and group outcome errors", "add batch fill accounting", "add events and iter_ready_groups", "use iter_ready_groups in the claim_batch e2e test", "regenerate Python SDK reference for events and iter_ready_groups".
   - PR 4, 3: "add batch-wide quorum to the batch state", "add wait_for_quorum to batch handles", "regenerate Python SDK reference for wait_for_quorum".
   - PR 5, 2: "add release_member, release_group and release_not_ready" and its docs regeneration.
   - PR 6, 3: "add acquire to batch handles", "allow size=0 batch groups", and the acquire docs regeneration.
   - Transport, reaper and PR 7 carry the renamed text only through their parents, plus README edits in the reaper and PR 7 commits. The script covers all of them.
   - The RL branch `feat/rl-batch-claim-mode` and `bench/rl-claim-mode` call none of the renamed names (measured). The bench calls only `events()`, which keeps its name.
3. **Make review copies**, one per real branch at its live head: `git branch review/rename/<branch> origin/<branch>` for PR 3, PR 4, PR 5, PR 6, transport, reaper and PR 7.
4. **One scripted pass, no rebase.** Fixups at PR 3 followed by rebases would conflict wherever a later commit rewrites the same docstring lines. Instead, rewrite every commit's tree in one `git filter-branch` over all seven copies at once, so shared commits are rewritten once and the copies stay stacked:
   ```
   BASE=$(git rev-parse origin/feat/batch-2-claim)   # PR 2 head, unchanged by this task
   SCRIPT=/abs/path/to/notes-worktree/batch-primitives/handoff/work/rename_group_outcomes.py
   FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f \
     --tree-filter "python3 -I $SCRIPT" \
     --msg-filter "sed -E 's/\biter_ready_groups\b/group_outcomes/g; s/\bGroupReady\b/GroupOutcome/g'" \
     -- $(for b in feat/batch-3-group-consumers feat/batch-4-group-quorum feat/batch-5-release-group-76a8m3 \
                   feat/batch-6-acquire-nbbp5j feat/batch-transport-ltlfdm feat/batch-reaper-ymtd6t \
                   feat/batch-7a-create-path; do echo "$BASE..review/rename/$b"; done)
   ```
   The script is idempotent. It also renames the escaped heading `iter\_ready\_groups` that pydoc-markdown writes into the reference, so the regenerated docs match. A cloud dry run on 2026-10-22 rewrote all 26 commits in 8 s. That run's script lacked the README path, which was added afterwards and checked at the PR 7 head. At the PR 7 head, 254 tests in `test_sandbox_batch.py`, `test_async_sandbox_batch.py` and `test_batch_state.py` passed, mypy was clean, `make generate-python-docs` matched the script's output byte for byte, and no old name was left. Committers become your local identity, so set it to Brian Nguyen first. Authors are kept.
5. **Verify** before showing Brian:
   - `git grep -n -E 'iter_ready_groups|GroupReady|iter\\_ready' review/rename/feat/batch-7a-create-path` is empty.
   - Each copy has the same commit count and subjects (renamed) as its real branch. `git range-diff origin/feat/batch-2-claim..origin/<b> origin/feat/batch-2-claim..review/rename/<b>` shows only the renames and the two docstring edits.
   - `git diff origin/<b> review/rename/<b> --stat` touches only the 12 files above.
   - Per commit, in detached worktrees: mypy with the repo config, pytest on the batch test files that exist at that commit, and pycodestyle on changed lines. `GroupOutcome` is 2 characters longer than `GroupReady`, so a few long lines grow by 2. The longest is a `batch_state.py` comment, which goes from 129 to 131 characters. These files already have lines over 115, so don't rewrap unless Brian asks. At each of the 5 "regenerate" commits, `make generate-python-docs` gives no diff. Then the author and trailer check.
   - No behaviour changes, so no mutation check is needed. Say so in the report.
6. **RL branch** (after Task 5): `git switch -c review/rename/rl-batch-claim-mode origin/feat/rl-batch-claim-mode && git rebase --onto review/rename/feat/batch-7a-create-path HEAD~4`. Its 4 commits touch only `examples/agent-sandbox-rl/`, and its old base `769b7f2e` has the same tree as PR 7 `24a4e9a1`, so this should apply cleanly. Check that `git diff review/rename/feat/batch-7a-create-path HEAD --stat` equals `git diff 769b7f2e origin/feat/rl-batch-claim-mode --stat`, and run `examples/agent-sandbox-rl/tests/test_batch_claims.py` (`MYPYPATH=clients/python/agentic-sandbox-client` for mypy; the RL lib uses 2-space indent). Do **not** rewrite `bench/rl-claim-mode`. It is a pinned benchmark record (the limits rerun prompt pins `96103717`) on an older SDK copy, and it needs no change.
7. **Show Brian** two-dot compare links that show only this change: `https://github.com/briankhoi/agent-sandbox/compare/<old PR 3 head>..<new PR 3 head>` and the same for PR 7. Add one line on what to check: the names, the two docstrings, and nothing else.
8. **Sync only after Brian's OK** (these are open-PR branches): one `git push --atomic origin --force-with-lease=<branch>:<live head> ... review/rename/<b>:<b>` covering all seven, plus the RL branch.
9. **PR text** (draft it, and change GitHub titles only under the renumbering rule in `state.md`): the #1988 title becomes "feat(python-sdk): add events and group_outcomes (k/N)" with k from `state.md`. In `refs/pr_table.md`, rename the PR 3 row and fix line ~90 to: "`group_outcomes()` yields each group once its outcome is known: its first `min_ready` Ready members, or an `error` (`QuorumUnreachableError` or `BatchTimeoutError`)." Rename the name wherever it appears in the PR 4 to PR 7 descriptions.
10. **Notes edits** (in the notes worktree, after the usual check that nobody else is editing the notes): rename in `batch_claim_proposal.md` (the 8 lines below), in the current-state lines of `review_carryover_notes.md` (~94, ~107, ~131, ~167), `state.md` (~42, ~51), `refs/work_plan.md` (~19) and `project_instructions.md` (~31). Leave the quoted, already-posted reply text in `review_carryover_notes.md` (~13, ~15, ~17, ~26) and the historical run prompts unchanged. Tell Brian that his own project instructions table ("PR 3 `events`, `iter_ready_groups`, fill accounting") needs the same one-word edit.
11. **Go design** (branch `go-batch-design-notes`, design and prototype only, no SDK code): rename `ReadyGroups` and `IterReadyGroups` to `GroupOutcomes`, and `GroupReady` to `GroupOutcome`. Rename the Python names too. Files: `go_batch_final_design.md` (17 hits), `python_sdk_alignment.md` (3), `pending_user_actions.md` (1), `golden-generators/gen_handle_traces.py` (2), `python-repros/check_all_size0.py` and `check_attach_no_groups.py` (2 each), and `prototype/batchproto/{batch_state.go,handle.go,handle_trace_test.go}`. The prototype's private `groupOutcomes` field and `popGroupOutcome` don't clash with the public names. Leave any text quoted from Brian unchanged. Run `go vet ./... && go test ./...` in `prototype/batchproto`, and regenerate the golden traces if the generator's output changes. Make one commit authored by Brian Nguyen with the prefix `notes(go):`, and push it with a plain push.

**Google Doc edits for Brian** (he mirrors the proposal by hand). The simplest way is Find and replace with "Match case" on: `iter_ready_groups` → `group_outcomes` (7 hits) and `GroupReady` → `GroupOutcome` (2 hits). They sit in these places in `batch_claim_proposal.md` (line numbers at notes `b026b0f8`):

| Line | Section | Before | After |
| :-- | :-- | :-- | :-- |
| 263 | Core Batch Methods | `` `iter_ready_groups`: A live channel yielding once per group, ... `` | `` `group_outcomes`: A live channel yielding once per group, ... `` |
| 307 | Python (types) | `class GroupReady:` | `class GroupOutcome:` |
| 308 | Python (types) | `"""One group's own quorum outcome, yielded by iter_ready_groups()."""` | `"""One group's own quorum outcome, yielded by group_outcomes()."""` |
| 371 | Python (handle) | `def iter_ready_groups(self) -> AsyncIterator[GroupReady]: ...` | `def group_outcomes(self) -> AsyncIterator[GroupOutcome]: ...` |
| 394 | Go | ``The `events` and `iter_ready_groups` iterators become receive-only channels`` | ``The `events` and `group_outcomes` iterators become receive-only channels`` |
| 434 | 2. Per-group quorum example | ``(via `iter_ready_groups`)``, and twice ``` `iter_ready_groups()` ``` later in the paragraph | ``(via `group_outcomes`)``, and twice ``` `group_outcomes()` ``` |
| 459 | 2. Per-group quorum example (code) | `async for group in batch.iter_ready_groups():` | `async for group in batch.group_outcomes():` |
| 611 | Rollout Waves: Per-Step Cohorts | ``Uses `batch.iter_ready_groups()` so each pool's cohort dispatches ...`` | ``Uses `batch.group_outcomes()` so each pool's cohort dispatches ...`` |

The proposal has no text for the two docstring edits, so nothing else changes there.

**Done when.**
- Step 1's upstream check is reported, with links if anything was found.
- The seven `review/rename/*` copies (plus the RL copy) are pushed and pass step 5's checks.
- Brian has the two compare links, then gives his OK, and the atomic sync has run.
- The pr_table, notes and Go design edits are pushed, with commit links.
- Brian has the Google Doc table above and the reminder about his project instructions table.
- `git grep -E 'iter_ready_groups|GroupReady'` on PR 7 and on `feat/rl-batch-claim-mode` is empty.

---
## Not yours (stays in the cloud project)
RL follow-ups: the RL integration prototype and its limits rerun; `fleet.prefetch` (only if a fill beats an acquire burst by more than 20%); a parallel precheck (only if prefetch ships); sandboxd exec as its own track; owner references on pools (only if leaks show up); the per-claim default acquire limit (mention at the TL meeting); the Lease-loss detection run. Changes to `examples/agent-sandbox-rl/`. If Brian pastes an SDK change request from that work, treat it as a normal task: owning PR, review copy, report.
