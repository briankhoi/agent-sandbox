# Stack conversation handoff prompt (post-12/N merge, 13/N #1988, and 3 carried-over decisions)

Paste everything in the fenced block into a fresh conversation. Written 2026-10-28 18:31 UTC (self-checked against `upstream/feature/batch-claim` `522dad4c`, `origin/feat/batch-*` heads `73a41381` through `97670d11`, and `batch-primitives-notes` `6b1a1407`). Live state on the branches and `state.md` wins over anything here.

````
You are taking over the **stack conversation** for Brian Nguyen's batch claiming project: the Python SDK in `kubernetes-sigs/agent-sandbox`, built as a linear stack of PRs on his fork `briankhoi/agent-sandbox` (remote `origin`; upstream remote `upstream`). You have no memory of earlier work.

Other conversations run in parallel. Do not do their work, and do not touch their non-stack branches:
- **Coordinator 1**: owns items A, D, E/G, F (`work/parallel_tracker.md`), including open main-targeting SDK leak fixes `fix/python-sdk-*` (`#2085` to `#2087`) and `review/leakfix/*`.
- **Coordinator 2**: built `20/N` (`feat/batch-8-list-memory`), `21/N` (`feat/batch-9-types`), and `22/N` (`feat/batch-10-deletecollection-retry`) (`work/coordinator2_tracker.md`). Those three branches are now linear on the SDK stack, so you restack and sync them mechanically with the stack.
- **Coordinator 3**: owns `merge/main-into-batch-claim-2` (`12e04eb7`, branch-sync merge PR from `upstream/main` into `feature/batch-claim`), `23/N` `feat/batch-11-acquire-annotations` (`97670d11`, now linear on top of `22/N`), and `review/c3/*` (`review/c3/sdk-post-merge`, `review/c3/feat/rl-batch-claim-mode`, `work/coordinator3_tracker.md`). Never touch `merge/main-into-batch-claim-2` or `review/c3/*`. You DO restack `23/N` `feat/batch-11-acquire-annotations` mechanically as the top of the linear SDK stack, and after every stack sync you tell Brian the new `17/N` (`feat/batch-transport-ltlfdm`) and `23/N` (`feat/batch-11-acquire-annotations`) heads so C2/C3 can update dependent branches if needed.
- Never touch `bench/*`, `feat/rl-sandbox-client-race`, or `feat/rl-batch-claim-mode`.

## Setup (repo root `/usr/local/google/home/brianknguyen/agent-sandbox`)
```sh
timeout 60 git -C /usr/local/google/home/brianknguyen/agent-sandbox fetch origin && timeout 60 git -C /usr/local/google/home/brianknguyen/agent-sandbox fetch upstream
git -C /usr/local/google/home/brianknguyen/agent-sandbox-notes pull --rebase   # notes worktree, branch batch-primitives-notes
```
SSH to `origin` may print `sign_and_send_pubkey: ... agent refused operation`, or fail if the agent has no keys loaded. If SSH fails, use HTTPS with the logged-in `gh` credential helper:
`git -C <worktree> -c credential.helper= -c 'credential.helper=!gh auth git-credential' push https://github.com/briankhoi/agent-sandbox.git ...`
Wrap network git commands in `timeout 90`, and verify every push with `git ls-remote`.

Read these files from `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/` in this order before acting:
1. `working_rules.md` (read all of it; it is binding, except in Jetski `Model="inherit"` overrides the legacy Claude model names on line 83)
2. `project_instructions.md`
3. `state.md` (last entries)
4. `learnings.md` (especially sections 6 and 7)
5. `pr_board.md` (rows `12/N` through `RL`)
6. `../skills/test-audit/SKILL.md`

## How Brian wants you to talk to him (most important)
- **Be concise.** Lead with a two-line summary he can read in seconds, then ranked findings (each with a recommendation and reason). Put decisions you need from him last, each restated in plain words with options, costs, and a clear recommendation.
- **"Explain" means** what is happening, the issue, and the proposed fix. Never write "item 2" or "Thread 13" without restating what it is. For a concurrency issue, tell a two-line story with handles or callers A and B.
- **Answer questions plainly first.** A question is not pushback. Change a recommendation only on a new fact, and name the fact. When wrong, say "I was wrong about X, because Y".
- **Never scope things out, and verify before proposing changes.** Check every reviewer or verifier claim against all caller entrypoints, the documented contract, and standard Python client practice (`working_rules.md:71-74`). When behavior is intended, recommend no code change and draft a technical reply (or docstring clarification if warranted). For every genuine gap, fold into the owning PR or propose a separate PR and let Brian choose. Never narrow a promise in a comment or docstring without asking.
- **Compare links must show only the change under review.** A GitHub three-dot compare between two stacked branches diffs from their shared base and shows the whole PR. Build a synthetic commit (`git commit-tree <new>^{tree} -p <old> -m "review-diff: ..."`), push it to `origin` as `review-diff/<topic>`, verify `git diff --shortstat <old> <new>` first, and link `https://github.com/briankhoi/agent-sandbox/compare/<old>...review-diff/<topic>` with one line on what to check.
- **Writing rules (`working_rules.md:40-46` and `work/results/writing-audit.md`):** active voice, full sentences with one idea each, no chained colons or semicolons, no em dashes, avoid colons in comments, comments explain why, and sync and async comments/docstrings/tests stay identical.

## Hard rules
- Commits are authored and committed by `Brian Nguyen <brianknguyen@google.com>`, with zero `Co-Authored-By` or `Claude-Session` trailers. Never use `--no-verify`.
- Push only to `origin`. Never comment on GitHub, never open or edit PRs on GitHub, and never push to `upstream`. Draft replies and PR descriptions for Brian instead.
- Never switch branches in `/usr/local/google/home/brianknguyen/agent-sandbox` (main worktree). Do stack rebase/build work in `/usr/local/google/home/brianknguyen/agent-sandbox-restack` (or a sibling detached worktree outside the main repo). Always branch `review/<topic>/<branch>` from `origin/<branch>` rather than checking out local `<branch>` (for example, local `feat/batch-8-list-memory` is checked out in `../agent-sandbox-par-pr8`).
- Open-PR and live stack branches are never edited directly. Build and verify on `review/<topic>/<branch>` copies first. After Brian approves, run the atomic sync yourself (do not paste the push command to him):
  `timeout 90 git push --atomic origin --force-with-lease=<branch>:<expected-live-sha> <new-sha>:refs/heads/<branch> ...` (one lease per branch; check `git ls-remote` right before syncing so you never overwrite another coordinator's push; if any lease fails, stop and report).
- Fold fixes into the owning commit (`git commit --fixup=<sha>` and `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash <base>`), then restack dependents with `git rebase --onto`. Never use `--update-refs` (it moves other conversations' refs).
- Checks before pushing code changes:
  - Run only the new or changed `pytest` tests in both sync and async shells (`"$VENV/bin/python" -m pytest ...` from `clients/python/agentic-sandbox-client`), and mutation-check each new or changed test.
  - `"$VENV/bin/pycodestyle" --max-line-length=120` on changed lines plus whole-file `E301,E302,E303,E304,E305,E306` check on touched files.
  - `mypy` with zero errors on both venvs (`venv` has `kubernetes_asyncio` 36.1.0: `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv`; `venv33` has `kubernetes_asyncio` 33.3.0: `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/00bb279e-f865-4b5f-8655-867e51b8b486/scratch/venv33`). Per-commit script (`venv` 36.1.0): `bash /usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/mypy_per_commit.sh <base> <top>`, plus run `"$VENV33/bin/python" -m mypy k8s_agent_sandbox` separately, and clean up any temporary worktree via `git -C /usr/local/google/home/brianknguyen/agent-sandbox worktree remove /usr/local/google/home/brianknguyen/agent-sandbox-mypy`.
  - `make generate-python-docs` with zero uncommitted diff on every PR that changes public docstrings.
  - Author and zero-trailer check across `<base>..<top>`.
- Subagents **always** use `Model="inherit"`. Use one `inherit` subagent to triage/build and a fresh `inherit` subagent to verify (giving the verifier the rule and every path touching it, never your own conclusions).
- Notes worktree (`/usr/local/google/home/brianknguyen/agent-sandbox-notes`): edit only files you own (`handoff/state.md`, `handoff/tasks.md`, stack rows of `handoff/pr_board.md`, `handoff/learnings.md`, `handoff/working_rules.md` when Brian sets a rule, and your own `handoff/work/*` files). Always `git pull --rebase` right before committing and pushing, and stage explicit paths (`git add <file>`, never `git add -A`).
- Ask Brian before creating/deleting kind clusters or deleting remote branches.

---

## Verified live state (2026-10-28 18:31 UTC; re-verify on startup)
- **Upstream `feature/batch-claim` tip:** `522dad4cbdeb32b5cb31cf55c999481121046db6` (`feat(batch,python-sdk): add claim_batch and release (12/N) (#1824)`, squash-merged 2026-10-28 17:57 UTC).
- **Verified squash tree equality:** `522dad4c^{tree}` == `origin/feat/batch-2-claim^{tree}` (`93c1711a`) == `0fbee353daff8fd6ef7c3268ae91cb441e123597`. Zero edits happened during squash merge.
- **Merged so far:** `1/N` through `12/N` (`#1824`).
- **Lowest open PR now:** `13/N` (`#1988`, `feat/batch-3-group-consumers`).
- **11 linear SDK stack branches on `origin` (currently stacked on pre-squash `12/N` `93c1711a`):**
  1. `13/N` `feat/batch-3-group-consumers` (open `#1988`): `73a41381` (5 commits in `93c1711a..73a41381`)
  2. `14/N` `feat/batch-4-group-quorum` (open `#1989`): `cc0816b4`
  3. `15/N` `feat/batch-5-release-group-76a8m3` (open `#1990`): `87920a45`
  4. `16/N` `feat/batch-6-acquire-nbbp5j` (open `#1991`): `d8154c16`
  5. `17/N` `feat/batch-transport-ltlfdm` (ready, not opened): `92a22289`
  6. `18/N` `feat/batch-reaper-ymtd6t` (ready, not opened): `9c35c0e5`
  7. `19/N` `feat/batch-7a-create-path` (ready, not opened): `ecf7a158`
  8. `20/N` `feat/batch-8-list-memory` (ready, not opened, C2): `1b061c53`
  9. `21/N` `feat/batch-9-types` (ready, not opened, C2): `9785b2f8`
  10. `22/N` `feat/batch-10-deletecollection-retry` (ready, not opened, C2): `d7e78189`
  11. `23/N` `feat/batch-11-acquire-annotations` (ready, not opened, C3): `97670d11`

---

## Immediate agenda (execute 1 and 2 in parallel right when you start)

### 1. Review `vicentefb`'s comments on merged `12/N` (`#1824`)
`12/N` (`#1824`) was unexpectedly merged by someone else while Brian's manager `vicentefb` had 3 review comments on it (1 resolved, **2 unresolved**). Read `#1824` via `gh api graphql` and verify all 3 against `522dad4c` (sync and async):
- **Resolved comment (`batch_utils.py:215`, comment `4403723958`):** `size=0` groups. Brian already replied (`4403902998`) that `size=0` lands in `#1991` (`16/N`) with `acquire()`, and resolved the thread. Confirm nothing left to do.
- **Unresolved comment 1 (`sandbox_client.py:576`, thread `PRRT_kwDOPcbYcc6xPE83`, comment `4403739189`):**
  > *"Releasing an attached batch on exit is a footgun: an inspection tool that get_batch()es, reads members(), and exits without detach() deletes someone else's batch, and the async client defaults to cleanup=True. I'd release only batches this client claimed and detach attached ones on exit. If you keep this, the get_batch docstring needs the warning, not just the README."*
  - Check `sandbox_client.py:442-475,574-590`, `async_sandbox_client.py` (`get_batch`, `_stop_batches`, `_release_batches`, `__aexit__`), `README.md`, and `SandboxClient`/`AsyncSandboxClient` single-sandbox `get_sandbox` exit precedents (`_explicit_claims`, `cleanup=True` on `AsyncSandboxClient.__aexit__` vs `SandboxClient.__exit__`).
  - Note: in `AsyncSandboxClient`, check what `close()` vs `__aexit__` actually does to batches today (`_stop_batches` vs `_release_batches`) so your analysis states exact facts for both sync and async.
  - Evaluate whether the critique is valid, list the concrete options with trade-offs (e.g. tracking claimed vs attached handles so exit cleanup releases claimed handles and detaches/stops attached handles, vs keeping current behavior and adding the explicit warning to `get_batch` docstrings, vs how sync `__exit__` and async `__aexit__` align), and give a clear recommendation.
- **Unresolved comment 2 (`sandbox_batch.py:880`, thread `PRRT_kwDOPcbYcc6xPFyv`, comment `4403744773`):**
  > *"Reserving at now + retry_delay advances the shared cursor, so one 429 with Retry-After: 30 stalls every worker for 30 s. For an apiserver throttle that may be what you want (APF is telling the whole client to slow down); for a transport blip it isn't, though that backoff is short. Say which is intended; if not the stall, don't let a retry move the cursor past now + interval."*
  - Check `522dad4c:sandbox_batch.py:872-919` and `522dad4c:async_sandbox_batch.py:860-909` (`_reserve_create_slot`, `_create_one`, `batch_utils.retry_delay`), plus `16/N` `acquire()` (`d8154c16:sandbox_batch.py:648-654`, `d8154c16:async_sandbox_batch.py:664-670`, where `self._next_create_at >= deadline` also reads `_next_create_at`) and `19/N` warm-pool pacing skip (`ecf7a158`).
  - Evaluate both cases `vicentefb` distinguishes (429/503 `Retry-After` apiserver throttle vs per-connection transport blip), give options + costs (e.g. sleeping `retry_delay` locally on transport error before reserving a normal `1/create_rps` slot while letting 429/503 `Retry-After` push shared `_next_create_at` or vice versa, vs keeping as-is with an explicit comment and reply), and recommend one.
- **Where any approved `#1824` fix lands:** Brian specifically noted that if either comment warrants a code/docstring fix that he approves, it can probably land in `13/N` (`#1988`) (saving an extra PR + renumbering round, at the minor cost of a small commit in `#1988` outside `events`/`group_outcomes`, whereas `working_rules.md:24`'s default is a small standalone follow-up PR off `522dad4c`). Present the verdict + fix/reply options to Brian and confirm whether he wants any approved `#1824` fix placed as a dedicated commit in `13/N` (`#1988`) or in a standalone follow-up PR.

### 2. Rebase `13/N`..`23/N` onto `upstream/feature/batch-claim` (`522dad4c`) AND triage CodeRabbit comments on `13/N` (`#1988`) in parallel
- **Mechanical restack (`review/restack-13n/<branch>`):**
  - Rebase all 11 linear branches (`13/N` `origin/feat/batch-3-group-consumers` `73a41381` through `23/N` `origin/feat/batch-11-acquire-annotations` `97670d11`, old base `93c1711a`) onto `522dad4c` as `review/restack-13n/<branch>` in `../agent-sandbox-restack` (branching from `origin/<branch>` without checking out local `<branch>`).
  - Prove it is 100% mechanical: `git diff <old-head> <new-head>` must be empty for every branch `13/N`..`23/N`, `git range-diff 93c1711a..97670d11 522dad4c..review/restack-13n/feat/batch-11-acquire-annotations` must show only `=`, and all commits must have `Brian Nguyen <brianknguyen@google.com>` with zero trailers. Push `review/restack-13n/*` to `origin`.
- **Triage CodeRabbit comments on `#1988` (`https://github.com/kubernetes-sigs/agent-sandbox/pull/1988`):**
  - Note: CodeRabbit's existing reviews on `#1988` ran on 2026-10-21 against old commit `aed55b42` (before `10/N` `#2049`, `11/N` `#1996`, `12/N` `#1824`, and the `group_outcomes` rename). Read all inline threads AND review bodies on `#1988` with `gh api`:
    1. Inline thread `PRRT_kwDOPcbYcc6ukWK6` (comment `4331497752`, `sandbox_batch.py`): adding `urllib3.exceptions.ConnectTimeoutError` (superclass of `NewConnectionError`) to `_TRANSPORT_ERRORS` (`522dad4c:sandbox_batch.py:71-79`), since `coordination_v1_api` runs with `retries=False` (`k8s_helper.py`) so connect timeouts/errors on Lease calls are not wrapped in `MaxRetryError`. (Note: `22/N` `d7e78189` uses `urllib3.Retry(3, read=False)` for `_claim_collection_api`, so only `coordination_v1_api` uses `retries=False`.)
    2. Inline thread `PRRT_kwDOPcbYcc6upmS0` (comment `4333750286`, `batch_utils.py`): `validate_claim_batch_args` (`522dad4c:batch_utils.py:245`) accepts `create_rps=float("inf")`. Check `16/N` (`d8154c16:batch_utils.py:220-230,275`, commit `29fd217a`), which already introduces `positive_number` and switches `create_rps` validation to `positive_number("create_rps", create_rps, BATCH_DEFAULT_CREATE_RPS)`!
    3. Inline thread `PRRT_kwDOPcbYcc6u5Pfp` (comment `4340353778`, `async_sandbox_client.py`): registering attached handle in `get_batch` without a cancellable wait. Check `522dad4c:async_sandbox_client.py:630-639` / `73a41381:async_sandbox_client.py:631-640`, where `_register_batch` was already made synchronous in `10/N` (`#2049`, `74cd8fa5`).
    4. Outside-diff-range note in review `5604234416` (`async_sandbox_client.py:241-242`): keeping batches eligible for release after `close()`. Check against `10/N` `#2049`, `12/N` `#1824`, and `AsyncSandboxClient.close()`'s documented contract.
    5. Outside-diff-range note in review `5606950275` (`2026-10-21T22:21:29Z`, `async_sandbox_client.py:237-248`): waking `_changed` waiters in `_stop_batches()`. Check `73a41381:async_sandbox_client.py:253-255`, where `9/N` (`#1986`, `b03ba91b`) added `batch._detached = True` and `13/N` itself (`9c73ac3d`) already added `batch._changed.notify_all()`.
  - Also check `13/N`'s own 5 commits (`93c1711a..73a41381`) and `#1988`'s GitHub PR title/body (note: `#1988`'s PR body still mentions `iter_ready_groups()` instead of `group_outcomes()`, so prepare an updated PR body for Brian to paste). Once `13/N` is synced onto `522dad4c`, CodeRabbit will also review `13/N`'s own 5-commit diff cleanly.

### 3. Resolve the 3 carried-over stack decisions with Brian
Keep these 3 pending decisions on the docket so Brian can resolve them with you (either right after `13/N` + `#1824` triage or alongside it):
1. **Decision A (`16/N` `#1991`, `d8154c16`): `acquire()` cleanup in `_drop_acquired()` (`sandbox_batch.py:655-667,759-771`, `async_sandbox_batch.py:671-683,780-792`):**
   - What happens: `acquire()` sets `may_exist = True` before waiting for its `create_rps` pacing slot (`sandbox_batch.py:655-658`, `async_sandbox_batch.py:671-674`), and `_drop_acquired()` calls `_delete_claim(claim_name)` (`sandbox_batch.py:769`, `async_sandbox_batch.py:790`) without holding `_lifecycle_lock`.
   - Issue: (a) if `acquire()` is cancelled or stopped during the pacing wait before any create was sent, `_drop_acquired` sends an unnecessary HTTP DELETE (harmless 404); (b) if `acquire()` fails after creating a claim and caller B calls `detach()` while `_drop_acquired` is mid-DELETE, `detach()` can hand off the Lease before `_drop_acquired`'s DELETE finishes.
   - Proposed fix (recommended: yes, fold into `16/N` `#1991`): initialize `may_exist = False` before the pacing wait (setting `may_exist = True` right before `_create_one`), and wrap `_delete_claim(claim_name)` in `_drop_acquired()` with `_lifecycle_lock`.
2. **Decision B (`15/N` `#1990`, `87920a45`): concurrent `release_member()` fan-out (`sandbox_batch.py:618-619`, `async_sandbox_batch.py:634-635`):**
   - What happens: `15/N` currently wraps `release_member()`'s `_delete_claim(member.claim_name)` in exclusive `_lifecycle_lock` so `detach()` cannot free the Lease mid-delete.
   - Issue: exclusive `_lifecycle_lock` serializes concurrent `release_member()` calls on the same batch (~5-20 ms per HTTP DELETE, so 64 concurrent `release_member()` calls take ~0.3-1.3 s instead of ~20 ms in parallel). `release_group()` and `release_not_ready()` already fan out up to 20 deletes internally, so only concurrent `release_member()` callers are affected.
   - Question + proposed fix: ask Brian whether RL or callers invoke `release_member()` concurrently across many sandboxes at once. If yes, replace exclusive `_lifecycle_lock` on member deletes in `15/N` with a ~15-line shared active-delete counter (`release_member` increments under lock, runs HTTP DELETE in parallel, decrements/notifies in `finally`; `detach()`/`release()` waits for counter == 0). If no, leave `15/N` as-is.
3. **Decision C: Delete superseded remote review branches on `origin`:**
   - Ask Brian's OK to delete the 16 superseded remote branches `review/cr1824b/*` (8 branches) and `review/d1824/*` (8 branches) on `origin` (plus `review/d1824b/*`, `review/d1824c/*`, `review/d1824d/*` once no longer needed).

### 4. Ongoing mission after `13/N` (`#1988`) settles
You own the SDK PR stack end-to-end. Once `13/N` (`#1988`), `#1824`'s follow-ups, and the 3 decisions are settled and synced:
- Continue upward through `14/N` (`#1989`), `15/N` (`#1990`), `16/N` (`#1991`), and help Brian open and shepherd `17/N` through `23/N`.
- Whenever an upstream PR merges into `feature/batch-claim` (including Coordinator 3's `merge/main-into-batch-claim-2`), restack the remaining stack onto the new `upstream/feature/batch-claim` tip and report the new heads (especially `17/N` and `23/N`).

## Your first turn
1. Run Setup, read the 6 handoff files, and verify live SHAs (`upstream/feature/batch-claim` and `origin/feat/batch-*`).
2. In parallel:
   - Spawn one read-only `inherit` subagent to verify `vicentefb`'s 3 `#1824` comments and `#1988`'s 5 CodeRabbit items against `522dad4c`, `73a41381`, and `16/N` (`d8154c16`).
   - Mechanically rebase `13/N`..`23/N` onto `522dad4c` as `review/restack-13n/<branch>` in `../agent-sandbox-restack`, verify empty tree diffs and clean `range-diff`, and push `review/restack-13n/*` to `origin`.
3. Present Brian a concise, easy-to-scan kickoff response with:
   - Two-line summary (confirming `review/restack-13n/*` is pushed with identical trees and ready to sync, plus the headline on `#1824` and `#1988` comments).
   - Verdict, options/trade-offs, and recommendation (or paste-ready reply) for `vicentefb`'s 2 unresolved `#1824` comments and `#1988`'s CodeRabbit comments, plus updated `#1988` PR body (`group_outcomes`).
   - Clear list of decisions at the bottom: (1) approve syncing the mechanical restack `13/N`..`23/N` onto `522dad4c` now (or folding approved `#1824`/`#1988` fixes first and syncing once), (2) choices on `#1824` / `#1988` items and whether approved `#1824` fixes go in `13/N` or a small standalone PR, and (3) Decisions A, B, and C carried forward.
````
