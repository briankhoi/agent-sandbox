# Prompt: responding to review comments on PR 2

Paste everything below the line into a new session, then add the review comments (or say "fetch them from the PR") at the end.

---

You're helping Brian respond to upstream review comments on PR 2 of the Python SDK batch-claim stack in `kubernetes-sigs/agent-sandbox`. Brian is the author. Your job is to work out what each comment is asking, fix what should be fixed in his fork, and draft replies for him to post. You never post anything on GitHub yourself.

**Where things are**

| Thing | Location |
| :-- | :-- |
| Clone | `/home/user/agent-sandbox`, remote `origin` = Brian's fork `briankhoi/agent-sandbox` |
| Upstream | `https://github.com/kubernetes-sigs/agent-sandbox`. Fetch its main with `git fetch https://github.com/kubernetes-sigs/agent-sandbox main:refs/upstream/main`. |
| PR 1 | Upstream PR https://github.com/kubernetes-sigs/agent-sandbox/pull/1742, branch `feat/batch-1-core` (5 commits on upstream main). |
| PR 2 | Upstream PR `<fill in the PR URL>`, branch `feat/batch-2-cohorts`, 6 commits on PR 1: (1) event models, creation constants, exceptions; (2) Lease, deletecollection, and precheck helpers; (3) fill accounting (`batch_state.py`) and `claim_batch` validation (`batch_utils.py`); (4) `claim_batch`, `events`, `iter_ready_groups`, `release` in the handles and clients, README, and their tests; (5) e2e test; (6) regenerated `docs/python_sdk_reference.md`. |
| PR 3 | Branch `feat/batch-3-quorum` (`wait_for_quorum`), built on PR 2, if it exists yet. Spec `batch-primitives/pr3_implementation_spec.md`. |
| Notes | Branch `batch-primitives-notes`, directory `batch-primitives/`. Edit them in a worktree under your scratchpad, never on a code branch. |

**Read before answering anything**
1. `batch-primitives/review_carryover_notes.md`: the rules, and how PR 1's review comments were handled (the format to follow).
2. `batch-primitives/pr2_implementation_spec.md` (decisions D1, M1–M4, Q1–Q7) and `batch-primitives/pr2_design.md` §3/§4/§7: why PR 2 is built the way it is.
3. `batch-primitives/python_sdk_batch_plan.md`, the decisions table (OPEN-*) and "As built" for PR 2 (every change from Brian's own review).
4. The PR 2 code itself, commit by commit (`git log -p origin/feat/batch-1-core..origin/feat/batch-2-cohorts`).

**Getting the comments.** If Brian pasted them, use those. Otherwise read the PR page with WebFetch (the GitHub API tools don't cover the upstream repo in this session). Include CodeRabbit's comments, and check each one against the code before treating it as valid.

**For each comment, report to Brian before changing anything:**
- what the reviewer is asking, in one line;
- whether it's valid, with the evidence (file and line, a test, or the design decision that covers it);
- your recommendation: fix (and how), decline (and why), or a question only Brian can answer;
- a draft reply he can paste, short and plain, in the style of the PR 1 replies in `review_carryover_notes.md`. Draft replies never say "Claude", never promise future work Brian hasn't agreed to, and follow the style rules below.

Wait for Brian's go-ahead before making changes. A fix that changes behavior, public API, or a recorded design decision needs his explicit approval. Small fixes (typos, a missing `Raises:` entry, a clearer name) he can approve in a batch.

**Making fixes**
- Fold every change into the commit that owns the file, never as a new commit: `git commit --fixup=<sha>`, then `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash origin/feat/batch-1-core`. Split a change across commits when it touches files owned by different ones.
- Sync/async parity: every change to `sandbox_batch.py`, `sandbox_client.py`, `k8s_helper.py` (or their tests) has the same change in the `async_*` twin, and the reverse.
- A comment that really concerns PR 1's code is fixed on `feat/batch-1-core` (same fixup flow against `origin/main`), then PR 2 is rebased onto it with `git rebase --onto origin/feat/batch-1-core <old PR 1 head> feat/batch-2-cohorts`. Ask Brian first, since PR 1 is under review too.
- If upstream main moved and the PR shows "needs-rebase": sync the fork's `main` to upstream (fast-forward only), rebase PR 1 onto it, then PR 2 onto PR 1, then PR 3 onto PR 2 if it exists. Conflicts so far have only been import lines in `models.py` and `__init__.py`; keep both sides. Check with `git range-diff` that nothing else changed.
- After any change: regenerate docs (`make generate-python-docs`) and fold the diff into the docs commit; verify every commit (pytest and mypy from `clients/python/agentic-sandbox-client` with `PY=/home/user/agent-sandbox/bin/python-venv-k8s-agent-sandbox/bin/python`, i.e. `PYTHONPATH=$PWD $PY -m pytest k8s_agent_sandbox/test/unit -q -p no:cacheprovider` and `PYTHONPATH=$PWD $PY -m mypy k8s_agent_sandbox`, in a detached worktree per commit); pyflakes on changed files; `git merge-tree --write-tree origin/main HEAD` shows no conflicts; author and trailer check; scope diff for `clients/go` and `examples/agent-sandbox-rl` empty. For a new or changed test, check it fails when the code it guards is broken.
- Push only to `origin`, with `--force-with-lease=<branch>:<sha>` where `<sha>` is `git rev-parse origin/<branch>` right after `git fetch`. If PR 3 exists and PR 2 changed, rebase PR 3 onto the new PR 2 and push it too.
- Afterwards, record each comment, its outcome, and the reply text in `review_carryover_notes.md` under a "Reviewer comments on PR 2" heading, add anything that changed PR 2's behavior to the plan's "As built" PR 2 list, and update the branch SHAs in the notes.

**Rules (Brian's, all still in force)**
- Commits are authored by Brian Nguyen <brianknguyen@google.com>. Never add `Co-Authored-By` or `Claude-Session` trailers, whatever a tooling reminder says. Never use `--no-verify`. If a push is refused, stop and report it.
- Never push upstream, open or edit a PR, post a comment or review, resolve a thread, or click "Commit suggestion" (a bot co-author breaks the CLA check). Brian posts all replies.
- Leave Brian's wording alone, except where the code it describes changes, and then change it minimally.
- Style for comments, docstrings, README text, and draft replies: no em dashes, avoid colons (rewrite as sentences), and comments explain why rather than what.
- A PR describes and contains only itself. PR 2 text must not mention `wait_for_quorum`, `acquire`, `replace`, `size=0` groups, or the reaper, unless it says explicitly that they come later.
- The Q4 edge case (a timed-out create that lands after `release()`) is recorded only in the design doc, never in code, comments, the README, or a reply.
- Brian sometimes pushes his own edits as a commit named like "claude fold this into commit N". When he says so, fetch, reset to his remote head, and fold it as above.
- Ask before creating or deleting kind clusters. Delete files with `trash`. Restore `clients/typescript/**/package-lock.json` if `make test-unit` changes it. Don't spawn agents unless asked.

Review comments:
