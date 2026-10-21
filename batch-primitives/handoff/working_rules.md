# How Brian wants work done

Distilled from the cloud project's memory. Brian is token-limited, so be economical: scripts do the waiting, you read summaries, and you spawn subagents only when a rule below calls for one.

## Reporting
- Lead with a summary he can read in a minute. Then ranked findings, each with a recommendation and its reason. Decisions you need from him go last. Be concise and direct.
- Every review ask carries a GitHub compare or commit link that shows only that change, plus one line on what to check. Example: `https://github.com/briankhoi/agent-sandbox/compare/<old>..<new>`. When commits were folded or rebased, make the old-to-new diff linkable by pushing a synthetic commit to `origin` as `review-diff/<item>` (parent = the pre-fix tree's commit, tree = post-fix tree, built with `git commit-tree`), and link its compare page.
- Mark facts as measured, verified (read in code, with `file:line`) or inferred. Never present an inference as a fact. A fact you cannot establish becomes a question for Brian.
- When you summarize from a longer source, check the result item by item against the source and say so.
- When a file records Brian's answers, quote his words. Label an agent's proposal as a proposal even if he did not object.

## Design recommendations
- Every recommendation lists alternatives considered, including simpler ones, each with cost (speed, RBAC, code size, failure modes) and why rejected.
- Once Brian approves something, do not change the recommendation unless you can name a new fact. A question from him is not pushback: answer with facts and the trade-off already on record.
- Design changes need his approval. Propose, then wait. Delegated implementation choices (best practice) you may settle yourself; say what you chose.

## Review comments (CodeRabbit, upstream reviewers, Brian's manager, Brian)
- Never assume a comment is right. Check it against the code and say whether the stack already handles it (`file:line`) and which PR owns it.
- Valid comments: fold the fix into the commit that owns the code, rebase the PRs above it, and tell Brian what changed with a compare link. Do not ask first (standing authorization, 2026-10-19).
- Invalid or already-fixed comments: give Brian a paste-ready reply with the technical reason. Never post it yourself.
- Brian pastes upstream comments to you. You never comment on GitHub.

## Where fixes go
- A fix goes into the PR that owns the code, not on top of the stack. Code already merged upstream gets its own small upstream PR branch off the current upstream base.
- Open-PR branches (almost the whole stack) are never edited directly. Work on `review/<area>/<branch>` copies, push those, and wait for Brian to review. Only after his OK sync to the real branch, atomically, with `git push --atomic origin --force-with-lease=<branch>:<expected-sha> <new-sha>:refs/heads/<branch> ...` (one lease per branch). If a lease fails or a push is refused, stop and report.
- During a review round by Brian: amend edits into the owning commit (`git commit --fixup=<sha>` then `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash <base>`), no new commits. Do not rebase dependents or run the full check until he says the round is done.
- Mechanical restack after an upstream merge: rebase each branch in order onto the new base (`git rebase --onto <newbase> <old-parent-head> <branch>`), also restack pending review copies and side branches. Only a conflicting restack needs thought; report it.
- Deleting remote branches: ask Brian first.

## Checks per commit (before any push)
- Lint: `pycodestyle` (or `ruff check --select E,W,F`) on the lines you changed only; do not reformat existing code. Module loggers only. There is no enforced Python formatter upstream, so match the file's style. Reviewers flag PEP8 nits (for example E301). Go changes would also need `make lint-go`; the Python stack has none.
- `mypy` with the repo's `pyproject.toml` config on every commit. The cloud container lacked mypy and showed four environment-only errors at upstream `4bb0db30`; compare against the base before blaming your change.
- Author check: every commit is `Brian Nguyen <brianknguyen@google.com>`, no trailers (`git log --format='%an <%ae>%n%B' base..HEAD`).
- Tests: no full suite. Run only the new or changed tests (`pytest` on the specific tests, both shells), on the commit that changed source. Mutation-check each new or changed test: break the code it protects and see it fail for the intended reason.
- Test audit: at the end of an implementation, apply `batch-primitives/skills/test-audit/SKILL.md` (one owning test per contract at the strongest boundary, no near-duplicates, no tests that only check a mock). It is read-only; report and wait.
- Docs: after changing public docstrings, `make generate-python-docs` (or the Makefile target that regenerates `docs/python_sdk_reference.md`) must leave no diff at each PR.
- Full per-commit check only when Brian says the work is done: pytest and mypy per commit in detached worktrees, regenerated docs with no diff, the author/trailer check, and `git merge-tree --write-tree upstream/<base> HEAD` against current upstream (fetch first). The base for the batch stack is upstream `feature/batch-claim`; for standalone fixes it can be upstream `main`.
- Restore `package-lock.json` if `make test-unit` changed it. Do not commit `.venv/`, binaries, `VERSION`, or kubeconfigs.

## Style
- Comments and README: no em dashes, avoid colons, comments explain why. Keep sync and async comments identical. Sync/async parity in surface, docstrings and tests.
- A PR describes and contains only itself. No mention of later features unless the text says they come later. No drive-by fixes. No code whose only caller lands in a later PR.
- Docstrings say "Raises" and list what is raised; batch docstrings say `except BatchError` catches every runtime failure.
- Types: exact types long term, `Any` only at true boundaries.
- DRY, minimal state. Leave Brian's wording alone except where the described code changes, then change it minimally.
- PR titles use "(k/N)" with a literal N, for example `fix(python-sdk): ... (6/N)`. Brian opens the PRs and posts descriptions himself; draft them from `refs/pr_table.md`.

## Prompts and benchmarks
- Fair comparisons only: the same limiting mechanism on both arms. Lead with unlimited versus unlimited (batch defaults versus per-claim as shipped); any limit is the same driver semaphore on both arms.
- A prompt you write for another agent (including a benchmark run) must be complete the first time, resumable after interruption, and work from a fresh chat. It requires that every API call, flag, script and path is verified against the code at the tested SHA, and it records code SHA, tree and SDK version in the results.
- Benchmarks and e2e use kind. Ask Brian before creating or deleting a cluster, and say which cluster you will use.

## Self-check (replaces the cloud's independent-review thread)
In the cloud, audits and reviews of a thread's work went to a fresh thread that formed its own findings first and was never shown the author's list. Locally, do the same with a fresh subagent (the Agent tool, general-purpose) when:
- you finish a non-trivial code change (a restack with conflicts, a behavior fix, anything touching more than one commit's logic), or
- you write a prompt for another agent, or a design recommendation.
Give the subagent only the question, the base and head, and the rules it must review against. Do not hand it your own findings. Compare its findings with yours, resolve differences by reading the code, and report both. Use one subagent per review, with a tight brief; skip this for mechanical work (a clean restack, a doc regen) where `git range-diff` showing an unchanged tree is the check.
For a restack, prove it mechanical: `git range-diff <old-base>..<old-head> <new-base>..<new-head>` and `git diff <old-head> <new-head>` tree compare.
