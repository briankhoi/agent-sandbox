# G6. Leak fix: apply the upstream AGENTS.md writing guidance, document the PR 2 sync/async split in code, and fix the issue texts

Self-contained prompt: paste everything inside the fenced block below into a **fresh conversation** (no prior memory from conversation G needed). Resumable: Step 1 checks current branch heads and skips steps already completed cleanly.

````
## Context
You are a task conversation in Brian Nguyen's batch claiming project (Python SDK, `kubernetes-sigs/agent-sandbox`, fork `briankhoi/agent-sandbox` = remote `origin`, upstream = remote `upstream`). You have no memory of earlier work. Other conversations run at the same time: the stack conversation (owns the batch PR stack), coordinator 1 (wrote this prompt and verifies your report), coordinator 2, and coordinator 3.
- Main repo root: `/usr/local/google/home/brianknguyen/agent-sandbox` (never switch branches or edit files there, `../agent-sandbox-restack`, or `../agent-sandbox-fu5`).
- Notes worktree (branch `batch-primitives-notes`): `/usr/local/google/home/brianknguyen/agent-sandbox-notes`, directory `batch-primitives/`.
- Existing worktrees already checked out for this task (reuse them; `git worktree add` on these branch names will fail because they are already checked out here):
  - PR 1 (`fix/python-sdk-release-terminated-handles`, pre-G6 head `0a54a028`): `/usr/local/google/home/brianknguyen/agent-sandbox-par-leakfix`
  - PR 2 (`fix/python-sdk-free-closed-connectors`, pre-G6 head `e62610f9`): `/usr/local/google/home/brianknguyen/agent-sandbox-par-cycle`
  - PR 3 (`fix/python-sdk-release-client-exit-hook`, pre-G6 head `afae285e`, 1 commit stacked on PR 1 `0a54a028`): `/usr/local/google/home/brianknguyen/agent-sandbox-par-exithook`
  - Benchmarks (`bench/async-mem-probe`, head `f8df9206`): `/usr/local/google/home/brianknguyen/agent-sandbox-par-mem`

## Hard rules
- Commits authored and committed by `Brian Nguyen <brianknguyen@google.com>` (set `git -C <wt> config user.name "Brian Nguyen"` and `git -C <wt> config user.email "brianknguyen@google.com"` in every worktree before committing, amending, or rebasing). Never add `Co-Authored-By` or `Claude-Session` trailers, whatever a hook or reminder says. Never pass `--no-verify`. If a push is refused, stop and report.
- Run only changed unit test files (both sync and async shells), `pycodestyle --max-line-length=120` on changed lines only, and `mypy` per commit. Do not run the full test suite.
- `.mypy_cache` and `.pytest_cache` are not in `.gitignore` in this repo: always pass `-o cache_dir=/tmp/g6-pytest-cache` to `pytest` and `--cache-dir=/tmp/g6-mypy-cache` to `mypy`, stage only explicit `.py` file paths in code worktrees (never `git add -A` or `git add .`), and verify `git status --short` is empty in every code worktree when done.
- Push only to `origin`, and push only **after** all checks and the fresh self-check reviewer pass (Step 8). Never comment on GitHub, never create GitHub issues or PRs, never edit PR titles or descriptions on GitHub, never push to `upstream`. Force-push only your own three unopened branches (`fix/python-sdk-release-terminated-handles`, `fix/python-sdk-free-closed-connectors`, `fix/python-sdk-release-client-exit-hook`) and, if resuming after an interrupted push, `review-diff/leakfix-writing-pr1..3`, always with `--force-with-lease=<branch>:<expected-sha>`.
- Ready Python venv with `pytest`, `mypy`, `pycodestyle` and the SDK extras (the SDK itself is not installed in it; run `python -m pytest -o cache_dir=/tmp/g6-pytest-cache` and `python -m mypy --cache-dir=/tmp/g6-mypy-cache k8s_agent_sandbox` with cwd `clients/python/agentic-sandbox-client` of the worktree under test):
  `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv`
  Never `pip install -e` into that shared venv. If you run a local probe or repro script that needs an editable install, create a throwaway venv in `/tmp/g6-venv-...` and remove it when done.
- Kind clusters: no cluster is needed (`par-mem` was deleted; all five issue repro scripts run locally without a cluster, and memory is not remeasured because no executable code changes). Never create, touch, or delete `kind-agent-sandbox` or any other cluster without asking Brian.
- Remote access: `origin` uses SSH and `ssh-agent` may hang or refuse to sign. Start with `timeout 60 git -C /usr/local/google/home/brianknguyen/agent-sandbox fetch origin` and `timeout 60 git -C /usr/local/google/home/brianknguyen/agent-sandbox fetch upstream`. If SSH to `origin` hangs or fails, use HTTPS with the logged-in `gh` token:
  `G="git -c credential.helper= -c credential.helper=!gh\ auth\ git-credential"`
  `$G -C <wt> fetch https://github.com/briankhoi/agent-sandbox.git '+refs/heads/*:refs/remotes/origin/*'`
  `$G -C <wt> push --force-with-lease=<branch>:<expected-sha> https://github.com/briankhoi/agent-sandbox.git <src>:refs/heads/<branch>`
  Verify every push with `git ls-remote`. Use absolute paths (each shell starts fresh).
- Notes worktree hygiene: other conversations push to `batch-primitives-notes` and leave untracked files in `/usr/local/google/home/brianknguyen/agent-sandbox-notes`. Run `git -C /usr/local/google/home/brianknguyen/agent-sandbox-notes pull --rebase` right before committing and again right before a normal `git push` (never force-push notes). Stage only your assigned files by explicit path, never `git add -A` or `git add .`.
- Base every claim on code and files you have read (`file:line`). Mark facts as measured, verified (read in code) or inferred.
- Design changes need Brian's approval; do not make changes no rule requires.
- Every subagent you spawn must use `Model="inherit"` (never `flash` or `flash_lite`).
- Minimal fixes (`batch-primitives/handoff/working_rules.md`): before reporting, check that no state, field, logic, or wider type was added, and include in your self-check reviewer brief: "Is there a smaller change with the same behavior? Does it add state, duplicate data or logic, or widen a type without need?" When you find your own mistake, say "I was wrong about X, because Y".

## Ownership
Edit only what this prompt assigns to you:
- Code branches on `origin`: `fix/python-sdk-release-terminated-handles`, `fix/python-sdk-free-closed-connectors`, `fix/python-sdk-release-client-exit-hook`, synthetic diff refs `review-diff/leakfix-writing-pr1`, `review-diff/leakfix-writing-pr2`, `review-diff/leakfix-writing-pr3`, and (only if you choose to commit probe outputs for Inputs items 1 or 2 below) `bench/async-mem-probe`. Never touch any `feat/batch-*` or `review/*` branch.
- Notes files on `batch-primitives-notes`: `batch-primitives/handoff/work/results/G6-writing.md`, `batch-primitives/handoff/work/results/issues-final.md`, and the three `SDK fix` leak rows in `batch-primitives/handoff/pr_board.md` (update the three head SHAs plus the stacked-on PR 1 SHA `0a54a028` -> `<new PR 1 head>` in row 39's `Branch` cell).

## Why
1. Brian (2026-10-27): "make sure each issue, and also docstring or comment in each of the 3 PRs we made uses the new upstream main agents.md writing guidance the one about like active voice and explaining why".
2. Brian (2026-10-28): "before i paste in g6, putting the reaso nfor this split in the commit message and PR is NOT sufficient. it also needs to be a docstring or comment somewhere."
- Read the standard first from `upstream/main` (`git -C /usr/local/google/home/brianknguyen/agent-sandbox show upstream/main:AGENTS.md`, section `## Writing` and the commit-message bullet under `## Pull requests and CI`; note that `/usr/local/google/home/brianknguyen/agent-sandbox/AGENTS.md` on disk is an older checkout without `## Writing`) plus `batch-primitives/handoff/working_rules.md` in `/usr/local/google/home/brianknguyen/agent-sandbox-notes`: active voice (say who does what), full sentences with one idea each (no chained colons, semicolons, or parenthetical asides), define a term before using it and use the codebase's existing names, say what the code does and why (not how it got that way), no em dashes, avoid colons in comments, and keep sync and async comments identical where both shells have the comment.

## Inputs
- `batch-primitives/handoff/work/results/writing-audit.md` (in `/usr/local/google/home/brianknguyen/agent-sandbox-notes`): every violation with location, current text, rule, and exact replacement across PR 1 code (5), PR 2 code (2), PR 3 code (5), the three commit messages (8), the three PR texts (21), and the five issues (36), with these adjustments:
  - In audit item `1.2` (`sandbox.py:239-240` and `async_sandbox.py:249-250`), write `Call _on_terminated before clearing claim_name, because that name is the client's key.` (using `_on_terminated` instead of `it`, since the preceding sentence's subject is `The client`).
  - PR 3 accuracy fix 1 (commit message and PR text): write "calls `atexit.register(...)`" (not "registers `atexit.register(...)`").
  - PR 3 accuracy fix 2 (commit message and PR text): the per-client sizes and units disagree between PR 3's commit message and PR text; use the measured values from `test/benchmarks/batch-claim/results/g4/client_retained.out` on `bench/async-mem-probe` (`main`: async used `9,600 B`, async never used `1,369 B`, sync `11,407 B`), one unit (`B`, matching `About 610 B` in `5.19` and Issue 4), the same in both. This overrides the stale `KiB`/`KB` figures in audit items `4.4` and `5.13` and in PR 3's `Measured` line (`G4-memleak-final.md:328`).
- **Additional PR 2 code comment (required by Brian, 2026-10-28):** in `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox.py` at line 199 (right above `self.connector._release_sandbox_callbacks()` in `Sandbox.close_connection()`), replace the 1-line comment:
  `# A closed handle never reconnects, so its connector can let go of it.`
  with this comment explaining in code why sync releases callbacks in `Sandbox.close_connection()` while async releases them inside `AsyncSandboxConnector.close()` (without editing `async_sandbox.py`):
  ```python
        # SandboxConnector.close() also resets live connections on errors and
        # during snapshot suspend and resume, so it keeps the sandbox callbacks.
        # close_connection() drops them here once this Sandbox closes for good.
        # AsyncSandboxConnector.close() is final and drops them itself.
  ```
  Keep the shared comment inside `_release_sandbox_callbacks()` (`connector.py` and `async_connector.py`) identical across both shells; only `connector.py` keeps the transport-lock comment below it (audit item 2.1).
- The independent repro check of `batch-primitives/handoff/work/results/issues-final.md` (all five scripts pass) found these five text fixes:
  1. Issue B: the bullet "With the GC off, a closed, dropped AsyncSandbox holds 0.69 MiB; clearing the callbacks ... 0.006 MiB" (`issues-final.md:152`) sits under the kind-cluster `results/g3/` heading (`test/benchmarks/batch-claim/results/g3/` on `bench/async-mem-probe`), where it does not belong (it came from the local stubbed `handle_cycle_edges.py` probe, not kind). Either move the bullet out from under the kind-cluster `g3` list with an honest local-probe source line (and if you run and commit `test/benchmarks/batch-claim/results/leakfix/handle_cycle_edges.py` output on `bench/async-mem-probe`, record code SHA, tree, SDK version, and Python version in that output file per `working_rules.md`), or drop the bullet.
  2. Issue A: "Measured, sync client: about 0.06 MiB per handle" has no committed result. Either run and commit `test/benchmarks/batch-claim/results/leakfix/handle_retained.py`'s output on `bench/async-mem-probe` (recording code SHA, tree, SDK version, and Python version) and cite it, or reword as a local probe, or drop it.
  3. Issue B: "With the cycle broken: 0.46 MiB per 100 cycles, ending below the start" (`issues-final.md:150`) is true for glibc bytes in use (`72` to `41 MiB`) but not RSS (`156` to `163`). Reword (following the Writing rules, no chained clauses): state `0.46 MiB` of RSS per 100 cycles, and state in its own sentence that glibc bytes in use ended below their start (`72` to `41 MiB`).
  4. Issue 3: "which happens on `close_connection()` or on a failed `get_sandbox()` lookup (...)" (the list lacked its conjunction; rewrite any parenthetical aside there to satisfy the Writing rules).
  5. In issue A's Verification prose (`issues-final.md:128`) and the Verification summary table (`issues-final.md:473`), write "about 0.25 MiB" for the per-cycle figure on PR 1 (run-to-run noise between 0.24 and 0.25); leave the literal terminal stdout at `issues-final.md:125` untouched. Also annotate the non-pasted preamble (`:12-14`) and Verification branch headers (`:123`, `:206`, `:360`, `:473-476`) with `<new sha> (prose-only amend of <old sha>)`.
- Current PR texts live in `batch-primitives/handoff/work/results/G4-memleak-final.md` ("### PR 1 (updated)", "### PR 2 (updated, one sentence added)", "### PR 3").

Apply the audit's replacements (with the adjustments above) and the `sandbox.py:199` comment as written unless one is technically wrong or violates a Writing rule; if you adjust one, state which and why. Do not make changes no rule requires. Do not rebase PR 1 or PR 2 onto newer `upstream/main` commits (keeping their bases at `7f40dc1a` and `c4ed9ba9` ensures the `review-diff` links below show only your prose edits).

## Steps
1. **Verify starting state** (resumability check): fetch `origin` and `upstream`. Confirm pre-G6 commits `0a54a028` (PR 1), `e62610f9` (PR 2), and `afae285e` (PR 3) exist in git objects, check the current state of `/usr/local/google/home/brianknguyen/agent-sandbox-par-leakfix`, `-cycle`, and `-exithook` (on a fresh run their `HEAD`s equal those three SHAs; if resuming after interruption, skip any step already completed cleanly), and set `user.name` and `user.email` to `Brian Nguyen <brianknguyen@google.com>` in each worktree.
2. **PR 1** (`fix/python-sdk-release-terminated-handles` in `agent-sandbox-par-leakfix`, pre-G6 head `0a54a028`): apply the 5 code comment replacements (audit section 1, with the `1.2` `_on_terminated` adjustment) and the 2 commit-message fixes (audit section 4, PR 1), keeping sync and async comments identical. Stage explicit `.py` paths and amend the single commit (`git rev-list --count 7f40dc1a..HEAD` must be `1`).
3. **PR 3** (`fix/python-sdk-release-client-exit-hook` in `agent-sandbox-par-exithook`, pre-G6 head `afae285e`, stacked on PR 1): rebase onto the amended PR 1 head with:
   `git -C /usr/local/google/home/brianknguyen/agent-sandbox-par-exithook rebase --onto <new PR 1 head> 0a54a028 fix/python-sdk-release-client-exit-hook`
   (a plain rebase would replay the old PR 1 commit). Check `git rev-list --count <new PR 1 head>..HEAD` is `1`. Apply PR 3's 5 code comment/docstring replacements (audit section 3) and its commit-message fixes (audit section 4, PR 3, plus the two accuracy fixes from Inputs). Stage explicit `.py` paths and amend the single commit.
4. **PR 2** (`fix/python-sdk-free-closed-connectors` in `agent-sandbox-par-cycle`, pre-G6 head `e62610f9`): apply audit items 2.1 and 2.2, the required `sandbox.py:199` comment replacement from Inputs, and the commit-message fix (audit item 4.3). Stage explicit `.py` paths and amend the single commit (`git rev-list --count c4ed9ba9..HEAD` must be `1`).
5. **Prove nothing but prose changed**, per PR:
   - For every `.py` file touched between pre-G6 head and new head (for PR 3, compare `<new PR 1>..<new PR 3>` against `0a54a028..afae285e` and also `afae285e` vs `<new PR 3>` — both must be AST-identical), parse old and new source with `ast.parse`, strip `Expr(Constant(str))` docstrings from Module/ClassDef/FunctionDef/AsyncFunctionDef bodies, and verify `ast.dump` is byte-for-byte equal (`#` comments never enter the AST). List every checked file and confirm zero AST differences.
   - Check `git diff <old head> <new head>`, author/committer (`Brian Nguyen <brianknguyen@google.com>`), zero trailers on all three commits, and empty `git status --short` in all worktrees.
   - Run `pycodestyle --max-line-length=120` on changed lines, `mypy --cache-dir=/tmp/g6-mypy-cache k8s_agent_sandbox`, and `pytest -o cache_dir=/tmp/g6-pytest-cache` on each PR's touched unit test files in both sync and async shells:
     - PR 1: `test_sandboxclient.py`, `test_async_sandboxclient.py`, `test_adoption_lifecycle.py`
     - PR 2: `test_sandbox.py`, `test_async_sandbox.py`
     - PR 3: `test_sandboxclient.py`, `test_async_sandboxclient.py`, `test_adoption_lifecycle.py`
   - Verify `git merge-tree --write-tree upstream/main <new head>` exits `0` for all three new heads against freshly fetched `upstream/main`.
6. **Texts** (before self-check and before pushing):
   - Draft `batch-primitives/handoff/work/results/G6-writing.md` including the final PR 1, PR 2, and PR 3 titles and descriptions (audit section 5 replacements plus PR 3's two accuracy fixes applied; PR 2 keeps the use-after-close paragraph split into short sentences per audit item 5.9).
   - Update `batch-primitives/handoff/work/results/issues-final.md` in place (audit section 6 replacements plus the five repro-check fixes from Inputs). Comment-only edits inside a repro script (such as audit item `6.25` in Issue 3's `repro_3.py`, whose `ast.dump` is unchanged) do not require rerunning the script; if any repro script's executable AST changes, rerun it in a throwaway `/tmp` venv on `c4ed9ba9` and its fix branch and update its Verification block.
7. **Self-check** (before pushing): spawn one fresh `inherit` subagent given only: (a) the `## Writing` and commit-message sections from `upstream/main:AGENTS.md` plus `working_rules.md` Style and Minimal-fixes rules, (b) the pre-G6 SHAs (`0a54a028`, `e62610f9`, `afae285e`) and local new branch heads in the shared git repo, and (c) the paths `results/G6-writing.md` and `results/issues-final.md`. Ask it: (1) does every changed or added sentence in comments, docstrings, commit messages, PR descriptions, and issues follow `## Writing` and `working_rules.md` Style (active voice, full sentences with one idea each, no chained colons/semicolons/parentheticals, terms defined before use, no em dashes, identical sync/async shared comments)? (2) does PR 2 now clearly explain in a code comment why `sandbox.py` calls `_release_sandbox_callbacks()` while `async_sandbox.py` does not? (3) did any edit alter meaning or technical accuracy, or touch anything other than prose in `.py` files? (4) "Is there a smaller change with the same behavior? Does it add state, duplicate data or logic, or widen a type without need?" Do not hand the subagent your own checklist results. If you amend any commit to resolve a finding, re-run Step 5 on the touched PR(s) (and re-rebase PR 3 if PR 1 changed).
8. **Push**:
   - Push each branch to `origin` with `--force-with-lease=<branch>:<old head>` (`0a54a028`, `e62610f9`, `afae285e`), and plain-push `bench/async-mem-probe` if you committed probe outputs there.
   - Build and push synthetic review-diff commits to `origin` for Brian:
     - `review-diff/leakfix-writing-pr1`: `git commit-tree <new PR 1>^{tree} -p 0a54a028 -m "review diff"`
     - `review-diff/leakfix-writing-pr2`: `git commit-tree <new PR 2>^{tree} -p e62610f9 -m "review diff"`
     - `review-diff/leakfix-writing-pr3`: `git commit-tree <new PR 3>^{tree} -p afae285e -m "review diff"`
   - Verify all pushed refs with `git ls-remote`, and record the final SHAs and `https://github.com/briankhoi/agent-sandbox/compare/<old head>..review-diff/leakfix-writing-prN` links in `results/G6-writing.md` (noting that `pr3`'s compare also includes PR 1's prose edits because PR 3 sits on PR 1, alongside `git range-diff 0a54a028..afae285e <new PR 1>..<new PR 3>` output) and in `pr_board.md`.

## Report
Finalize `batch-primitives/handoff/work/results/G6-writing.md` in `/usr/local/google/home/brianknguyen/agent-sandbox-notes`, ensure `batch-primitives/handoff/work/results/issues-final.md` and the three `SDK fix` rows in `batch-primitives/handoff/pr_board.md` (the three head SHAs plus the stacked-on PR 1 SHA in row 39) are up to date, run `git pull --rebase`, stage only those three files, commit as `Brian Nguyen <brianknguyen@google.com>` (prefix `notes:`, no trailers), `git pull --rebase`, plain push, and reply to Brian with only the link and a one-line summary.
Report contents (concise, 1-minute summary first):
1. Old -> new branch heads and the three `review-diff` compare links (plus `git range-diff` for PR 3).
2. Per-PR AST prose-only proof, test counts, pycodestyle, mypy, and `merge-tree` results.
3. Any audit replacements adjusted and why (with `file:line`), and how Inputs items 1-5 were resolved.
4. Self-check reviewer findings and how each was resolved.
5. Final PR 1, PR 2, and PR 3 titles and descriptions in fenced blocks ready for Brian to copy.
````
