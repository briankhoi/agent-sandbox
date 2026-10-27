# G6. Leak fix: apply the upstream AGENTS.md writing guidance and fix the issue texts

Paste the block below into conversation G. Resumable: each step checks what already exists. All G4 rules still apply.

````
## Why
Brian (2026-10-27): "make sure each issue, and also docstring or comment in each of the 3 PRs we made uses the new upstream main agents.md writing guidance the one about like active voice and explaining why". The standard is the "## Writing" section of upstream main's `AGENTS.md` (#2036): active voice (say who does what), say what the code does and why (not how it got that way), write for someone new to the code, plus the commit-message bullet under "Pull requests and CI" and Brian's style rules (no em dashes, avoid colons in comments, identical sync and async comments).

## Inputs
- `batch-primitives/handoff/work/results/writing-audit.md` (notes branch): every violation with location, current text, rule and exact replacement, for PR 1 code (5), PR 2 code (2), PR 3 code (5), the three commit messages (8), the three PR texts (21) and the five issues (36). It also lists two accuracy fixes in PR 3's commit and text ("calls `atexit.register(...)`", not "registers"; the per-client sizes and units disagree between the commit and the PR text: use the measured values from `results/g4/client_retained.out` on `bench/async-mem-probe`, one unit, the same in both).
- The independent repro check of `results/issues-final.md` (all five scripts pass) found these text fixes:
  1. Issue B, the bullet "With the GC off, a closed, dropped AsyncSandbox holds 0.69 MiB; clearing the callbacks ... 0.006 MiB" sits under the kind-cluster `results/g3/` heading, but those numbers are not in g3. Either commit the probe's output under `results/leakfix/` on `bench/async-mem-probe` and cite it, or move the bullet out with an honest source line, or drop it.
  2. Issue A, "Measured, sync client: about 0.06 MiB per handle" has no committed result. Commit `leakfix/handle_retained.py`'s output and cite it, or reword as a local probe, or drop it.
  3. Issue B, "With the cycle broken: 0.46 MiB per 100 cycles, ending below the start" is true for glibc bytes in use (72 to 41 MiB) but not RSS (156 to 163). Reword: "0.46 MiB of RSS per 100 cycles; glibc bytes in use ended below their start (72 to 41 MiB)."
  4. Issue 3: "which happens on `close_connection()` or on a failed `get_sandbox()` lookup (...)" (the list lacks its conjunction).
  5. Write "about 0.25 MiB" for A's per-cycle figure on PR 1 (run-to-run noise between 0.24 and 0.25).

Apply the audit's replacements as written unless one is technically wrong; then say which and why. Do not make changes no rule requires.

## Steps
1. **PR 1** (`fix/python-sdk-release-terminated-handles`, `0a54a028`): apply the code comment and docstring replacements and the commit message fixes, amend the single commit. Keep sync and async comments identical.
2. **PR 3** (`fix/python-sdk-release-client-exit-hook`, `afae285e`, stacked on PR 1): rebase onto the amended PR 1, apply its code and commit-message fixes (including the two accuracy fixes), amend the single commit.
3. **PR 2** (`fix/python-sdk-free-closed-connectors`, `e62610f9`): apply its two code fixes and its commit-message fix, amend.
4. **Prove nothing but prose changed**, per PR: `git diff <old head> <new head>` touches only comments, docstrings and test comments or docstrings (list any other line and why); run the changed tests in both shells, mypy with the repo config, pycodestyle on changed lines; `git merge-tree --write-tree upstream/main <head>` clean. Memory is not remeasured (no code change).
5. **Push** each branch with `--force-with-lease=<branch>:<old head>` (your own unopened branches). Push `review-diff/leakfix-writing-pr1`, `-pr2`, `-pr3` commits (parent = old head's tree, tree = new head's tree, built with `git commit-tree`) so Brian sees each old-to-new change in one compare.
6. **Texts**: write the final PR 1, PR 2 and PR 3 descriptions (audit replacements applied; PR 2 keeps the use-after-close content, split into short sentences as the audit proposes) and update `results/issues-final.md` in place (audit replacements plus the five repro-check fixes; the repro scripts themselves change only if a fix requires it, and if one does, rerun it on main and its fix branch and update the Verification block).
7. **Self-check**: one fresh `inherit` subagent given the Writing section, the old and new heads and the two text files, asked "does every changed sentence follow the Writing section; did any change alter meaning or technical accuracy; did anything other than prose change in code?". Resolve findings.

## Report
`batch-primitives/handoff/work/results/G6-writing.md` (pull --rebase, add only it, `results/issues-final.md` and the pr_board leak rows, plain push); give Brian only the link and one line. Contents: new heads, the three review-diff compare links, the prose-only proof per PR, test and mypy results, audit items not applied and why, self-check result, and the final PR texts in fenced blocks.
````
