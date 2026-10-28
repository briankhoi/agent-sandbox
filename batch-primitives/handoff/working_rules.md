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
- Lint: `pycodestyle` (or `ruff check --select E,W,F`) on the lines you changed only; do not reformat existing code. Module loggers only. There is no enforced Python formatter upstream, so match the file's style. Reviewers flag PEP8 nits (for example E301). Go changes would also need `make lint-go`; the Python stack has none. Upstream CI has no Python linter (presubmits: lint-go, lint-api, lint-olm). If `presubmit-agent-sandbox-lint-olm` is red with golangci-lint "typecheck" errors in `olm/hack/sync-k8s-manifests/main.go`, that is the base's tooling (fixed on upstream main by #1912), not our diff: report it, do not fix it in a Python PR.
- `mypy` with the repo's `pyproject.toml` config on every commit. The cloud container lacked mypy and showed four environment-only errors at upstream `4bb0db30` (reported by the restack thread, 2026-10-21 18:53); compare against the base before blaming your change.
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

## Minimal fixes (Brian, 2026-10-22, after a transport fix that widened a type for data the object already had)
- Before writing a fix, list the data already available where it runs. Use existing fields before adding state, fields or wider types.
- Never change production code to suit a test double. Fix the mock.
- Before reporting, ask what the smallest change with the same behavior is. Any new state or type change needs one sentence on why the existing data isn't enough.
- "Smallest" means the smallest resulting code, with each rule stated in exactly one place, not the smallest diff. After applying any change, including a reviewer's suggestion taken literally, re-read the whole function and module it touches and ask whether any condition is now expressed twice (Brian, 2026-10-22, after an SSLEOFError tuple entry duplicated the helper's EOF branch).
- Reviewer briefs always include: "Is there a smaller change with the same behavior? Does it add state, duplicate data or logic, or widen a type without need?"
- When you find your own mistake, say "I was wrong about X, because Y". Change a recommendation only on a new fact, and name it.
- Sync of open-PR branches: once Brian approves the review copies, run the atomic sync yourself; do not paste the command to him (Brian, 2026-10-22: "you can just ask if i approve of the review/changes, and if so then we can sync").

## "Review it" shorthand (Brian, 2026-10-22)
When Brian says to review a PR's comments: read them with `gh` yourself. For each comment, valid means fix it on review copies (owning commit, dependents restacked), run the checks, and show him the compare link before syncing; invalid means give a paste-ready reply with the reason and `file:line`. Never post on GitHub.
- **Stacked PRs carry the whole stack's diff.** Every open PR targets upstream `feature/batch-claim`, so until the PRs below it merge, its diff (and CodeRabbit's review) includes all lower PRs' changes, not just its own; upstream cannot show the incremental diff. CodeRabbit (and other reviewers) may therefore comment on code that belongs to a lower PR, or that a later commit already changed. For each such comment: (1) find the commit that owns the code (`git log -S`/`git blame` on the live stack) and say which PR that is; (2) check whether it is valid against the code and the libraries; (3) if valid, check whether it is already fixed anywhere in the stack (live heads, including higher PRs) and say where (`file:line`, commit); (4) if valid and not fixed, fix it in the owning commit on review copies as usual, even when that commit is in a lower PR (code already merged upstream follows the merged-code rule), and give Brian a reply for the thread that names the PR carrying the fix; (5) if invalid or already fixed, give Brian a paste-ready reply that says so, with the reason, the owning PR and `file:line`.

## Scope of the stack conversation (Brian, 2026-10-22 23:07)
- Keep restacking the whole stack after each upstream merge so it stays linear, including transport, reaper and PR 7, but only mechanically (identical trees). Make no other changes to any branch and start no new work (no Task 6 rename, no transport mypy fix, nothing from the queued work) unless Brian asks. The job is rebasing, getting the next open PR ready for review, and handling review comments on the open PRs. Fixes for review comments still go into their owning commits, and dependents are restacked.
- A separate thread fixes the transport mypy error on its own copy, so after each restack tell Brian the new transport head.
- For review comments and verification on future PRs, spawn `inherit` subagents (one to triage and fix, a fresh one to verify) to save the main context, and check their evidence before syncing.

## Never scope things out (Brian, 2026-10-23)
- Never scope a finding out. For every valid gap (from a reviewer, a verifier or yourself), either fold the fix into the owning PR or propose a separate PR, and put the choice to Brian. Never narrow a promise (a comment, docstring or contract) instead of fixing the gap without asking him.
- Verify a verifier note or handoff recommendation before treating it as a valid gap (Brian, 2026-10-28). Check all caller entrypoints, the method's documented contract, and standard library practice first, and recommend no change (while still reporting the finding) when the behavior is intended.
- For lifecycle or state fixes, write the rule the change promises in one sentence first, list every path that touches it (including concurrent interleavings), and give the verifier that list to check (lesson from #2049, where a scoped-out detach() path and an unchecked overlapping-cleanup race came back as CodeRabbit comments).

## "Explain" (Brian, 2026-10-22)
When Brian says "explain", be concise: what is happening, the issue, and the proposed fix (or say a fix is being worked out).

## Self-check (replaces the cloud's independent-review thread)
In the cloud, audits and reviews of a thread's work went to a fresh thread that formed its own findings first and was never shown the author's list. Locally, do the same with a fresh subagent (the Agent tool, general-purpose) when:
- you finish a non-trivial code change (a restack with conflicts, a behavior fix, anything touching more than one commit's logic), or
- you write a prompt for another agent, or a design recommendation.
Give the subagent only the question, the base and head, and the rules it must review against. Do not hand it your own findings. Compare its findings with yours, resolve differences by reading the code, and report both. Set the subagent's model explicitly: Sonnet for routine reviews, Opus only for design or non-trivial code review. Use one subagent per review, with a tight brief; skip this for mechanical work (a clean restack, a doc regen) where `git range-diff` showing an unchanged tree is the check.
For a restack, prove it mechanical: `git range-diff <old-base>..<old-head> <new-base>..<new-head>` and `git diff <old-head> <new-head>` tree compare.

## Faster review rounds (Brian, 2026-10-28: "let's make this next one faster please")
After a fix round took about an hour (full checks re-run in every round, and the whole stack restacked before Brian saw the design), Brian approved these rules ("yes"):
- Ask design-shape questions (for example "explicit flag or existing field?") before building.
- In a review round, build only on the owning commit. Run only the changed tests with their mutation checks, and mypy at that commit.
- Restack the upper branches only after Brian approves the diff. Run the full per-commit check (mypy on every commit, docs at every head, author check) once, right before syncing.
- Keep a fresh verifier for behavior and lifecycle changes, scoped to the rule and the paths that touch it. It doesn't re-run the builder's mechanical checks except as spot checks.
- Skip the verifier for comment-only changes and mechanical restacks.
