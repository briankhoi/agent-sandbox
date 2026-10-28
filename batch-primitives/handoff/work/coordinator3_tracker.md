# Coordinator 3 tracker (items H1 and H2)

Brief: `handoff/work/coordinator3_prompt.md`. Prompts in `handoff/work/prompts/` (`H*`), results in `handoff/work/results/` (`H*`). Link base: `https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/`.

| Item | Prompt | Status | Last result | Open decisions |
| :-- | :-- | :-- | :-- | :-- |
| H1. Merge `upstream/main` into `upstream/feature/batch-claim` (`merge/main-into-batch-claim-2`) | `prompts/H1-merge-main.md` | Verified `12e04eb7` (`7cfe8484` + `355803af`) via fresh `inherit` verifier `87a32434` (0 defects); **upstream moved** (`feature/batch-claim` -> `522dad4c` `#1824`; `main` -> `6a7ad5aa` `#2085`/`#2054`) so `12e04eb7` conflicts with `522dad4c` until rebuilt (`H1b`) | `results/H1-merge-main.md` (`12e04eb7`) | Rebuild `merge/main-into-batch-claim-2` onto `522dad4c` + `6a7ad5aa` now, or after `#2086`/`#2087` merge? |
| H2. Task 7: SDK `23/N` `pod_annotations` (`feat/batch-11-acquire-annotations`) + `review/c3/sdk-post-merge` + RL rebase/#2035 cleanup/batch annotations (`review/c3/feat/rl-batch-claim-mode`) | `prompts/H2-task7-acquire-annotations.md` | **Verified & accepted** (`97670d11`, `778b60e8`, `d9d5c258`) via fresh `inherit` verifier `a0a0e462` (0 blockers, 7/7 mutations caught, 0 mypy/lint/doc errors) | `results/H2-task7-acquire-annotations.md` (`97670d11`, `778b60e8`, `d9d5c258`) | Brian review of `23/N` (`97670d11`) & `review/c3/feat/rl-batch-claim-mode` (`d9d5c258`); sync to `feat/rl-batch-claim-mode` after approval / stack restack |

## Live heads at start (2026-10-28 03:57 UTC, verified)
- `upstream/main`: `355803af` (includes `#2035` `7f40dc1a`, `#2041` `e1e73e24`, `#1791`, `#1960`, `#1966`, `#1967`)
- `upstream/feature/batch-claim`: `7cfe8484` (`11/N` `#1996`)
- `origin/merge/main-into-batch-claim` (prior merge PR `#2005` `90abd59e`): `ef1349cc`
- SDK stack (`12/N`..`20/N`): `feat/batch-2-claim` `93c1711a`, `feat/batch-3-group-consumers` `73a41381`, `feat/batch-4-group-quorum` `cc0816b4`, `feat/batch-5-release-group-76a8m3` `87920a45`, `feat/batch-6-acquire-nbbp5j` `d8154c16`, `feat/batch-transport-ltlfdm` `92a22289`, `feat/batch-reaper-ymtd6t` `9c35c0e5`, `feat/batch-7a-create-path` `ecf7a158`, `feat/batch-8-list-memory` `1b061c53` (`21/N` `feat/batch-9-types` and `22/N` `feat/batch-10-deletecollection-retry` prompts ready under Coordinator 2, not yet pushed)
- `origin/feat/rl-batch-claim-mode`: `7252e27f` (4 commits `5c585caa..7252e27f` on `ff5c7652`; note `pr_board.md:35` still lists pre-review `3bee2847`)

## Sequencing & clean linear git structure
1. **H1 (`merge/main-into-batch-claim-2`)**: merges `upstream/main` (`355803af`) into `upstream/feature/batch-claim` (`7cfe8484`), resolves `Makefile`, `__init__.py`, and `models.py` conflicts, regenerates `docs/python_sdk_reference.md`, and (if Q1 Option A approved) forwards `file_config` in `SandboxBatch.connect()` / `AsyncSandboxBatch.connect()`. Merged upstream with `/label tide/merge-method-merge` (not squashed, not `(k/N)` numbered, matching `#2005`).
2. **H2 Part A (`feat/batch-11-acquire-annotations`, provisional `23/N`)**: branches **linearly** off the highest live SDK stack branch (`feat/batch-10-deletecollection-retry` if pushed, else `feat/batch-9-types`, else `feat/batch-8-list-memory` `1b061c53`). Zero merge commits inside the SDK stack.
3. **H2 simulated post-merge base (`review/c3/sdk-post-merge`)**: merges `merge/main-into-batch-claim-2` into `feat/batch-11-acquire-annotations`.
4. **H2 Part B (`review/c3/feat/rl-batch-claim-mode`)**: rebases the 4 RL commits (`5c585caa..7252e27f`) onto `review/c3/sdk-post-merge`, folds `#2035` Lease-workaround deletion into commit 1 (`5c585caa`), folds batch `pod_annotations`/`snapshot_uid` routing + `#1791` scoping into commit 2 (`35390caf`), and folds README updates into commit 4 (`7252e27f`). Never pushes directly to `feat/rl-batch-claim-mode` until Brian approves.
5. **Post-H1 upstream restack**: once H1 merges into `upstream/feature/batch-claim` and the stack conversation restacks `12/N..23/N`, `git rebase --onto feat/batch-11-acquire-annotations review/c3/sdk-post-merge review/c3/feat/rl-batch-claim-mode` moves the 4 RL commits onto the real stack with zero conflicts.

## Verification checklist (fresh `inherit` subagent, every prompt and every code result)
- Before handing any `H*.md` prompt to Brian: fresh `inherit` subagent verifies pinned SHAs, `file:line` references, API signatures, and `working_rules.md` compliance.
- Before accepting any `H*` result: fresh `inherit` subagent checks pushed SHAs/trees/author (`Brian Nguyen <brianknguyen@google.com>`, no trailers), minimal-fix rules (smallest resulting code, each rule stated once, no state/type widening without need, fix mock not prod), sync/async parity, comment/docstring style (`AGENTS.md` Writing), related surface area, test audit (`skills/test-audit/SKILL.md`, real helper boundary rather than mock-only check, mutation checks), lint (`pycodestyle`), `mypy` (`venv` 36.1.0 + `venv33` 33.3.0 for SDK; diff vs base for RL), and `make generate-python-docs` zero diff.

## Brian's answers (verbatim)
- 2026-10-28 04:21 (Q1): "1. option 1A" (forward `file_config` in `SandboxBatch.connect()` / `AsyncSandboxBatch.connect()` in a follow-up commit on `merge/main-into-batch-claim-2`).
- 2026-10-28 04:44 (Q2): "1. approve option 2A" (pass `ready_timeout=self.config.effective_claim_timeout()` to `BatchClaims`).
- 2026-10-28 04:45 (workflow): wait until decisions are finished before running prompt reviewers.
- 2026-10-28 04:52 (Q3): "for decision 3 the is alive thing, can you put it in a remote md file and ill get some opinions on what to do" -> `handoff/work/coordinator3_decision_is_alive.md`.
- 2026-10-28 05:15 (Q3 settled via `handoff/work/coordinator3_response_is_alive.md`): "Approve Option (a) (`if claim is None: handle.record_pod_identity()`), augmented with a 0-GET in-memory batch-watch check in `is_alive()` ... Do NOT use Option (b) or Option (d), and avoid adding a config knob (Option c)."
