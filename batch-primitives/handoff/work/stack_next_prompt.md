# Starting prompt: stack conversation, from 9/N on (paste everything below the line)

---

You are taking over the **stack conversation** for Brian's batch claiming project: the Python SDK in `kubernetes-sigs/agent-sandbox`, built as a linear stack of PRs on his fork `briankhoi/agent-sandbox` (remote `origin`; upstream is `upstream`). You have no memory of earlier work. A separate **parallel conversation** handles Task 3, Task 4 items, Task 5 and parts of Task 6 through prompt files; do not do its work.

## Setup (repo root `/usr/local/google/home/brianknguyen/agent-sandbox`)
```sh
git fetch origin && git fetch upstream
git -C ../agent-sandbox-notes pull --ff-only     # notes worktree, branch batch-primitives-notes
```
Read, from `../agent-sandbox-notes/batch-primitives/handoff/`, in order: `project_instructions.md`, `working_rules.md` (read all of it; the "Minimal fixes" and "Review it" sections are new and binding), `state.md`, `tasks.md`, `refs/pr_table.md`. They override your defaults. `state.md` and live heads beat anything below.

## Hard rules (short form; the files have the rest)
- Commits authored and committed by `Brian Nguyen <brianknguyen@google.com>`, no `Co-Authored-By` or `Claude-Session` trailers, never `--no-verify`.
- Push only to `origin`. Never comment on GitHub, never open a PR (Brian does). The one GitHub write you may do: renumbering open PRs (title "(X/N)" and the ordinal word in the description's first sentence) with `gh`, after Brian opens a PR that shifts the series.
- Open-PR branches are never edited directly. Work on `review/<topic>/<branch>` copies, push them, show Brian compare links, and after his OK run the atomic sync yourself (`git push --atomic origin --force-with-lease=<branch>:<live sha> <new sha>:refs/heads/<branch> ...`, one lease per branch). Do not paste the command to him. A purely mechanical restack after an upstream merge, with identical trees proven, may be synced once Brian has asked for the rebase.
- Run only new or changed tests (plus mutation checks for new or changed tests), lint on changed lines, mypy on every commit you create or rewrite. No full suite.
- Ask before creating or deleting kind clusters or remote branches.
- Base every claim on code you read (`file:line`), mark facts verified, measured or inferred. Report short: summary, ranked findings each with a recommendation, decisions for Brian last.
- Subagents always use the `inherit` model.
- Edit only the notes files you own (`handoff/state.md`, `handoff/tasks.md`, `handoff/refs/pr_table.md`, plus `working_rules.md` when Brian sets a rule); `git pull --rebase` right before committing notes because the parallel conversation pushes too. Update `state.md` after every sync.

## Where things stand (2026-10-22 22:10 UTC)
- Upstream `feature/batch-claim` tip: `d07e8941` (7/N, #1985, merged). Merged so far: 1/N to 7/N.
- **8/N #2033** (`feat/batch-ssl-eof-retry`, TLS EOF retry), head `0e586d47`, open, Vicente gave `/lgtm` with two optional notes. The first is fixed (both `_is_transport_error` helpers unwrap a TLS error and `ssl.SSLEOFError` is in `_TRANSPORT_ERRORS`). Brian has a paste-ready reply and a description sentence about unbounded EOF retries to add himself.
- Open and renumbered: 9/N fu7 #1986, 10/N fu9 #1996 (contains fu8), 11/N PR 2 #1824, 12/N PR 3 #1988, 13/N PR 4 #1989, 14/N PR 5 #1990, 15/N PR 6 #1991. Not opened: transport, reaper, PR 7 (16/N to 18/N inferred). Heads are in `state.md`.

## Your next task: get 9/N (fu7, #1986) ready once 8/N merges
1. Wait for Brian to say 8/N merged (or check `git log upstream/feature/batch-claim`). Find the squash commit by its title, and confirm its tree equals the tree of `origin/feat/batch-ssl-eof-retry` (`git rev-parse <squash>^{tree} origin/feat/batch-ssl-eof-retry^{tree}`). If they differ, stop and tell Brian.
2. Restack fu7 and everything above onto the squash: delete stale local `review/*` refs first (see lesson 6), create `review/restack-8n/<branch>` copies, `git rebase --onto <squash> <old TLS head> --update-refs` from the top copy. Prove it mechanical: every branch tree identical to its old head, per-commit trees identical, `git range-diff` all `=`, authors clean. Push the copies, then sync atomically (Brian asked for this rebase pattern).
3. 9/N ready means: #1986 shows only fu7's files; fu7's tests pass in both shells (mutation-check them); lint, mypy per commit and the author check are clean; `make generate-python-docs` leaves no diff; the description in `refs/pr_table.md` ("fu7 (9/N)", "This PR is the ninth...") matches the code. Give Brian one compare link and one line on what to check.
4. Then handle review comments when Brian says "review it" for a PR (rule in `working_rules.md`): read every comment with `gh` (use the GraphQL `reviewThreads` query to see unresolved threads), decide valid or invalid against the code. Valid: fix in the owning commit on review copies, restack dependents, run checks plus an independent `inherit` reviewer whose brief includes "Is there a smaller change with the same behavior? Does it add state, duplicate a rule, or widen a type?", show the compare, sync after OK. Invalid or not this PR's code: paste-ready reply with the reason and `file:line`.

Other queued work (each needs Brian's OK to start): Task 6 rename (`iter_ready_groups` to `group_outcomes`, steps 2 to 10 in `tasks.md`, now unblocked since plan C and the renumbering are done; first get the parallel conversation's step 1 result from Brian); the transport mypy error (4.4) before transport is opened; opening transport, reaper, PR 7 (Brian opens; you prepare text from `refs/pr_table.md`).

## Lessons from the previous session (Brian corrected each of these; do not repeat them)
1. **Minimal fixes.** Before writing a fix, list the data already available where it runs; use it before adding state, fields or wider types. Never change production code to suit a test double, fix the mock. "Smallest" means the smallest resulting code with each rule stated once, not the smallest diff. After any change, including a reviewer's suggestion taken literally, re-read the whole function and module for a condition now expressed twice. (A transport fix stored a name the cached `Sandbox` already had as `sandbox_id` and widened a tuple type to suit a `MagicMock`; a literal `ssl.SSLEOFError` tuple entry duplicated the helper's EOF branch.)
2. **Recommendations.** List alternatives with costs, pick one, and hold it unless a new fact appears; name the fact when you change. If you were wrong, say "I was wrong about X, because Y". Do not flip-flop under a question; a question is not pushback.
3. **Answer the question asked, plainly, first.** When Brian asks "what is X / why", explain in simple terms with the evidence before proposing anything.
4. **Check who owns the code a comment is about.** Comments can sit on outdated heads (CodeRabbit reviewed a transient #1984 head that included upstream `main` files) or on code merged in an earlier PR. Read the library source to verify claims (`kubernetes`, `urllib3`, `aiohttp` in the venv below).
5. **Shallow vs deep copy:** a shallow copy is fine when you only rebind attributes on the copy and the class holds settings, not resources (sync `kubernetes` `Configuration` for the Lease client). It was wrong in #1509 because that converted an async `kubernetes_asyncio` config into a sync one (different classes, async refresh hook).
6. **`git rebase --update-refs` moves every local branch that points at the rebased commits**, including old review copies. Delete stale local `review/*` branches (they stay on origin) before restacking, and switch the restack worktree off a branch before deleting it.
7. **Semantic conflicts.** When a commit moves below others, later commits may delete symbols it uses (PR 2 deletes `_watch_backoff_delay` in favour of `batch_utils.retry_delay`). After any non-mechanical restack, grep for removed symbols and run mypy on every commit.
8. **Pushes and SSH.** Brian's SSH agent sometimes prints `sign_and_send_pubkey: ... agent refused operation`; pushes still go through. Wrap network git commands in `timeout`, then verify with `git fetch` or `git ls-remote` that the refs landed.
9. **Renumbering** is done with a script that asserts exactly one "(X/N)" in the title and one "This PR is the <ordinal> in a series" in the body, patches both, and reads them back.

## Tools already set up
- Venv with mypy, pytest, pycodestyle and the SDK extras: `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv` (run from `clients/python/agentic-sandbox-client`; the SDK is imported from the working directory). It has `kubernetes_asyncio` 36.1.0, which `make test-unit` also installs; with it, mypy reports one known error from transport up (`async_k8s_helper.py:105`, tracked in 4.4).
- Scripts in that `scratch/` directory: `mypy_per_commit.sh <base> <head>` (detached worktree `../agent-sandbox-mypy`, remove it after), `resolve_imports.py` (merges conflict blocks that contain only `import x` lines), `renumber.py` (edit its PLAN before reuse).
- Worktrees: `../agent-sandbox-restack` (restacks), `../agent-sandbox-fu5` (detached checks), `../agent-sandbox-notes` (notes). Keep scratch files out of the repo.
