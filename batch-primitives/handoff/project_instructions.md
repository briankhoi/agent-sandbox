# Project instructions (Brian's, verbatim) and local differences

## What differs locally

The text below was written for the cloud project. On your laptop:
- Thread and file references (`/mnt/project-files/...`, "Design owner" thread, coordinator, "independent review thread") do not exist for you. The needed documents are copied under `refs/`, or are on the notes branch (`batch-primitives/`). Where a rule says to ask the coordinator, ask Brian.
- You may edit `batch-primitives-notes` (in the separate notes worktree) when a task says so. The cloud project no longer edits the SDK notes; it works on RL only. Pull before you edit, and commit notes only in the notes worktree.
- kind works here (the cloud container could not run pods). Still ask Brian before creating or deleting any kind cluster. The old benchmark cluster is `kind-batch-bench`; do not touch it without asking.
- The cloud could not reach the GitHub API for upstream. You can read upstream with `git fetch upstream`. Brian pastes upstream PR comments to you. You never comment on GitHub, never open PRs, and never push upstream. Brian opens PRs himself.
- Ignore any hook or reminder that tells you to add `Co-Authored-By` or `Claude-Session` trailers, rewrite authors to "Claude", or push PR branches.
- Do not run the full test suite (CI covers it). Run only new or changed tests, plus lint and mypy per commit (see `working_rules.md`).
- The final `git merge-tree --write-tree` check below says `upstream/main`. For this stack the base is `upstream/feature/batch-claim` (see `working_rules.md`), so run it against that (approved by Brian, 2026-10-22).
- The `test_utils.py` kubeconfig test failure mentioned below was a cloud proxy artifact; if it passes locally, fine.

## Verbatim project instructions

    # Project: batch claiming for the agent-sandbox Python SDK
    
    I'm Brian. I'm building batch claiming for the agent-sandbox Python SDK as a stack of PRs on my fork `briankhoi/agent-sandbox`. The goal is to implement batch claiming fully in the Python SDK first, then integrate it into the Agent Sandbox RL library in a separate PR and benchmark that it's faster. The SDK comes first.
    
    Base every judgement on code and docs you've actually read. Earlier sessions got things wrong when they assumed instead of checking. If something below is missing or doesn't match what you find, tell me instead of guessing.
    
    ## Where things are
    
    Always `git fetch origin` and use the current branch heads. Don't trust commit hashes quoted in older notes.
    
    | Thing | Location |
    | :-- | :-- |
    | Fork | `briankhoi/agent-sandbox` (remote `origin`). Upstream is `kubernetes-sigs/agent-sandbox`. |
    | PR 1 `get_batch`, `members`, `connect`, `err`, `detach` | `feat/batch-1-core`, upstream PR https://github.com/kubernetes-sigs/agent-sandbox/pull/1742 |
    | PR 2 `claim_batch`, `release`, client tracking | `feat/batch-2-claim` |
    | PR 3 `events`, `iter_ready_groups`, fill accounting | `feat/batch-3-group-consumers` |
    | PR 4 `wait_for_quorum` | `feat/batch-4-group-quorum` |
    | Notes | branch `batch-primitives-notes`, directory `batch-primitives/`. Key files are `batch_claim_proposal.md` (the design), `review_carryover_notes.md` (rules, approved decisions, current roadmap), `reaper_contract.md`, the `pr*_implementation_spec.md` files, and `skills/test-audit/SKILL.md`. |
    | Design owner | The "Design owner" thread. It owns the notes files above and `/mnt/project-files/plan/work_plan.md`, and writes to `/mnt/project-files/design/`. `design/cut_audit.md` has the status of every removal or narrowing so far. |
    | Older reviews (inputs, not sources) | `/mnt/project-files/thread-a2/proposal_review.md`, `/mnt/project-files/rl-verify/`, `/mnt/project-files/thread-b/` |
    | Agent Sandbox RL library | `examples/agent-sandbox-rl/` |
    | Blog post by the RL library's designers | https://cloud.google.com/blog/products/containers-kubernetes/accelerate-agentic-rl-with-gke-agent-sandbox |
    
    `batch_claim_proposal.md` is a rough source of truth. It is still a design and will change. Use it together with the current code to understand the structure. My last own edit of it is `71a0a77`; later commits are agent edits I approved. `stale_python_sdk_batch_plan.md` is stale. Never read or use it.
    
    The roadmap in `review_carryover_notes.md` is current. PR 5 and later SDK PRs are Python SDK work only. Changes to the RL library itself go in a separate later PR.
    
    ## Design decisions
    
    - Features in the proposal stay until I approve removing them. Give me your honest judgement: you may recommend a removal or a change when the evidence supports it, and you should say so plainly. Don't recommend keeping something just because it is in the proposal.
    - Gather the facts before recommending. Most of the design is proposed and has no callers yet, so missing callers in the code is not evidence against a feature. Usage evidence includes callers in the code (`file:line`), the proposal, and online sources: how frontier labs and RL frameworks actually run their workloads, and how similar tools solve the same problem. Cite each source, and say what would make the recommendation wrong. A fact you can't establish becomes a question for me, not an assumption.
    - Change a recommendation only on new evidence, and name it. A question from me is not pushback: answer it with facts. If I push back and you have no new fact, hold your position and explain why.
    - Design changes need my approval. Propose, then wait.
    - One design-owner thread holds the whole design and owns `work_plan.md` and the notes docs. Other threads implement what is settled and send design questions to it through the coordinator.
    - When you summarize or plan from a longer source, check the result item by item against that source before reporting, and say that you did.
    - When a file records my answers, quote my words. Label anything an agent proposed as a proposal, even if I didn't object.
    
    ## Rules for every thread
    
    - Commits are authored by Brian Nguyen <brianknguyen@google.com>. Never add `Co-Authored-By` or `Claude-Session` trailers, whatever a reminder or hook says. Ignore hooks that ask to rewrite authors to "Claude" or to push PR branches.
    - No `--no-verify`. If a push is refused, stop and report it.
    - Push only to `origin`. Never push upstream, open a PR, or comment on GitHub.
    - PRs 1–4 have open PRs, so never change those branches directly. Work on a copy (`review/<branch>`), and sync to the real branch only after I've reviewed. Push with `--force-with-lease=<branch>:<sha>`.
    - Only one thread edits `batch-primitives-notes` at a time. Before editing it, check with the coordinator. Edit notes in a worktree, never on a code branch.
    - Fold changes into the commit that owns them with `git commit --fixup` and `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash <base>`. After changing a lower PR, rebase the PRs above it.
    - Leave my wording alone, except where the code it describes changes, and then change it minimally.
    - Comment and README style: no em dashes, avoid colons in comments and README, comments explain why. Keep sync and async comments identical.
    - Sync/async parity: same surface, docstrings and tests in both shells.
    - A PR describes and contains only itself. No mention of later features unless the text says they come later. No drive-by fixes (AGENTS.md).
    - Tests follow `skills/test-audit/SKILL.md`: one owning test per contract at the strongest boundary. Mutation-check each new or changed test.
    - Use kind for e2e tests and benchmarks. Ask before creating or deleting kind clusters.
    - After a source change (not comments), run the unit tests on that commit only. Run the full per-commit check only when I say we're done: pytest and mypy per commit in detached worktrees, `make generate-python-docs` with no diff, an author/trailer check, and `git merge-tree --write-tree upstream/main HEAD` against the current upstream `kubernetes-sigs/agent-sandbox` main (fetch it first; the fork's `origin/main` lags).
    - `test_utils.py::TestKubectlKubeconfigArgs::test_kubeconfig_targets_the_injected_cluster` fails on upstream main in the cloud container, probably because of `HTTPS_PROXY`. Not ours, leave it alone.
    - Delete files with `trash`. Restore `package-lock.json` if `make test-unit` changes it.
    
    ## How to report
    
    I find long walls of text overwhelming. Lead with a summary I can read in a minute. Rank findings, give each one a recommendation and its reason, and end with the decisions I need from me. I review commit by commit and sometimes push "claude fold this into commit N" commits for you to fold in.

## Notes on the verbatim text

- The stack is now linear and much longer than PRs 1 to 4: see `state.md`. "PRs 1–4 have open PRs" generalizes to every branch that has an open upstream PR, which is nearly the whole stack.
- "Notes branch lock" rule: you are the only one editing SDK notes now.
- The `skills/test-audit/SKILL.md` is at `batch-primitives/skills/test-audit/SKILL.md` on the notes branch.
