# PR 2 redesign: prompt for the design session

Brian's call (2026-09-26): the existing PR 2 on `feat/batch-2-cohorts` was planned and built by a weaker model and looks bloated: fields, classes, and machinery whose purpose isn't clear, about 2,300 source lines and 3,000 test lines. Don't patch it. Design PR 2 again from its requirements, on top of the revised PR 1, and only then compare the result with the old PR 2. This file supersedes `pr2_revision_prompt.md` and `PR_2_prompt.md`.

The work has three phases. **Stop after phase 2** and wait for Brian's approval. Phase 3 (implementation) needs his go-ahead, probably in a new session.

## Phase 1: clean-room design

### Don't read these until the design is committed (see "Freeze")

The point is to design without anchoring on the old solution.
- The branch `feat/batch-2-cohorts` and its commits: no `git show`, `diff`, `log -p`, `checkout`, `grep`, or `worktree` on it, and none on `df1c11d` or its ancestors above `0f7a03f`.
- `PR_2_prompt.md` and `pr2_revision_prompt.md`.
- In `python_sdk_batch_plan.md`: "As built", the "Code changes" and "Tests to change" parts of "Pending: proposal A", and the whole "PR 2" section.
- The "PR 3", "PR 4", and "PR 5" sections of the plan may be skimmed only to learn which features later PRs add (so the design doesn't block them). Don't adopt their internal names or structures.

If you catch yourself about to look at the old PR 2 "just to check", don't; phase 2 exists for that.

### Read these

1. `batch_claim_proposal.md` in full. This is the feature spec: API, lifecycle, events and quorum, liveness, cleanup, RBAC, usage examples, and scalability. It was also written by a weaker model, so treat its **user-facing API and behavior** as the requirement, and its implementation hints as suggestions.
2. `python_sdk_batch_plan.md`:
   - **"Ground rules"** are binding (scope, sync/async parity, style, tests, docs, commits).
   - **"Shared contracts"**: the wire contracts are binding, because the reaper (PR 6) and re-attaching handles depend on them. That covers names, labels, annotations, the claim manifest, and the Lease. In-memory contracts (e.g. the dispatched set, how `Member` derivation is organised) describe the old design. Keep only what is user-visible.
   - **"Decisions (Brian) → Resolved"** are Brian's decisions and binding by default. Some of them drive a lot of machinery (e.g. OPEN-F consumer modes and hold-back, OPEN-H, OPEN-S). If you believe one should change to make PR 2 simpler, keep it in the design and propose the change separately under "Decision changes for Brian", with what it saves.
3. `review_carryover_notes.md`, section "Rules to carry into PRs 3–5". These apply to PR 2 too. Ignore any old-PR-2 internal names it mentions.
4. The revised PR 1 code on `feat/batch-1-core` (head `f4a4867`, on upstream `68db683`; re-fetch, Brian may have revised it since) is the foundation you build on. Read all of it:
   - models, constants, and exceptions;
   - the helpers in `k8s_helper.py`/`async_k8s_helper.py`;
   - `batch_state.py` (claims → state);
   - `batch_utils.py` (validation, Lease, and retry policy, plus their tuning constants);
   - both handles;
   - both clients' `get_batch`;
   - the tests.

   Changing PR 1 is allowed where it makes PR 2 simpler, but list every such change separately; PR 1 is under upstream review.

   PR 1 facts that matter for PR 2:
   - Only the async client keeps a registry of batch handles (`_active_batches`, which `close()` uses). The sync client has none, because nothing in PR 1 needed it. Client-level cleanup of batch handles must come back in both clients in whichever PR first needs it; see "Deferred from PR 1" in `review_carryover_notes.md`. The design says whether that is PR 2 (e.g. if `claim_batch`/`release()` bring exit or close cleanup) and, if not, which later PR.
   - `get_batch` raises `BatchError` for any invalid batch annotation, on the Lease or on the claims.
   - PR 1's tests were audited with `skills/test-audit/SKILL.md`, which removed 18 duplicates. PR 2's tests must follow the same bar: don't repeat a `batch_utils`/`batch_state` table at the handle level, and give each contract one owning test.

### Approved behavior PR 2 must have

These were approved by Brian after review. Design them in; don't reopen them.
- **Event types.** `BatchEventType`, `BatchEvent`, and `GroupReady` are added (and exported) in PR 2, since its `events()`/`iter_ready_groups()` produce them. PR 1 removed them on a reviewer's request.
- **A failed group is finished (quorum mode).** Once a group's verdict is an error (`QuorumUnreachableError` for any cause, or `TimeoutError`):
  - it gets no more creates;
  - none of its members are handed out as `MEMBER_READY`, held back or not;
  - its `MEMBER_FAILED`/`MEMBER_LOST` still stream;
  - its members stay in `members()`;
  - nothing is deleted.

  A successful group's extras still stream, and stream-only batches keep creating and deliver everything.
- **The fill deadline covers pacing.** Group `TimeoutError` verdicts and the close of `events()` count `quorum_timeout` from when pacing issues the batch's **last** create, i.e. `start + quorum_timeout + (N - 1) / create_rps`. That keeps groups from timing out before their claims exist at large N. A re-attached handle (which creates nothing) counts from attach.
- **`release()` survives transient errors.** It deletes with label `deletecollection` in bounded rounds, re-listing for claims without a `deletionTimestamp`, and deletes the Lease last.
  - A 429, 5xx, or transport error inside a round moves to the next round after a backoff, preferring `Retry-After`.
  - After the last round it raises the last transient error, or a `BatchError` for claims still not deleting.
  - A 403 raises immediately (OPEN-U).
- **Request timeouts** on any request made from a background loop, or on a path the caller can't interrupt (sync `release()`/`detach()`/exit cleanup included).
- **Retry policy is shared.** Use `batch_utils.is_retryable_status` and `backoff_delay` for every retry loop.
- **Scale** to the proposal's stated sizes (tens of thousands of claims). Nothing may do O(N) work per watch event. Check every timing against pacing time, `deletecollection` time (the apiserver deletes collections with one worker by default, and a request can hit its 60 s timeout while deletion continues), and watch-cache aging.
- **RBAC.** The README Role gains exactly the verbs PR 2 uses. PR 1 lists claims `get`/`list`/`watch` and leases `get`/`update`.
- **Only what PR 2 has.** PR 2's code, exports, comments, docstrings, and README describe and contain only what PR 2 ships. Something that only a later PR calls belongs in that PR. A later feature is mentioned only when the text says explicitly that it comes later.

### Design principles

- **Smallest design that meets the requirements.** Every class, field, function, constant, public argument, and annotation must trace to a requirement; the design doc shows that mapping.
- **Prefer plain values and local variables to classes,** and one loop to a framework. Add an abstraction only when two real callers need it now.
- **Put logic in the I/O-free core only where it prevents real sync/async drift.** Remember that everything in the handles is written twice.
- **Don't build for PRs 3–6.** Just don't block them.
- **Name things after what they are** in the proposal's vocabulary (group, member, cohort, quorum, fill).

### Deliverable: `batch-primitives/pr2_design.md` on `batch-primitives-notes`

1. **Requirements**, numbered, each citing its source (proposal section, OPEN-id, or the approved list above).
2. **Scope:** what PR 2 ships and what it leaves to later PRs. A move of a feature between PRs counts as a decision change for Brian.
3. **Public API:** signatures for both clients and both handles, semantics, exceptions (a `Raises:` list), and defaults.
4. **Internal design, per module:**
   - the state held, each field listed with the requirement it serves;
   - the functions;
   - the threads/tasks and what they own;
   - locking;
   - the failure handling and retries;
   - what is written to the cluster (claim manifest, Lease annotations).
5. **Deliberately left out,** and why.
6. **Size estimate** per file (source and tests separately).
7. **Test plan:** behaviors, each in both shells where it exists in both, plus the e2e smoke test. Plan only tests that pass the value bar in `skills/test-audit/SKILL.md` (no implementation-coupled, low-value, or duplicate tests); the old PR 2's 3,000 test lines are part of what this redesign should shrink.
8. **PR 1 changes needed,** if any, with reasons.
9. **Decision changes for Brian,** if any, each with what it saves.
10. **Open questions.**

### Freeze

Commit `pr2_design.md` on `batch-primitives-notes` (subject `docs(batch-primitives): add clean-room PR 2 design`) and push it before phase 2 starts. That commit is the record of what was designed without seeing the old code. Afterwards, never edit phase 1 text silently.

## Phase 2: compare with the old PR 2

Now read the old PR 2: `git diff 0f7a03f df1c11d` (it sits on the pre-revision PR 1, so ignore differences PR 1's revision explains).

Append a section "Comparison with the old PR 2" to `pr2_design.md` containing:
- **An inventory table** of every public API item and every class, method, module-level function, instance field (`self._x`), constant, Lease/claim annotation, and test group in the old PR 2, with:
  - where it is in the old code;
  - what it does;
  - its status: **in the new design** (maybe under another name), **not needed** (say why: no requirement, duplicate, or premature for a later PR), or **missed requirement**.
- **Missed requirements.** For each one, amend the design only in a separate subsection, "Changes after comparison", saying what was missed and why it's needed.
- **Things the new design has that the old one lacks** (behavior or simplifications).
- **Totals:** old source/test lines against the new estimate, and the biggest sources of the difference.

Be concrete and fair. If an old piece exists for a real reason, say so. Brian has had other agents call the old PR 2 "good" without explaining why it's so big; the value here is an item-by-item answer.

Commit and push the notes branch, then report to Brian and **stop**. The report covers:
- the design summary;
- the size comparison;
- the top reasons the old PR 2 was big;
- any decision changes or PR 1 changes you propose;
- the open questions.

## Phase 3 (only after Brian approves the design)

Brian may start a new session for this, pointing at this section and `pr2_design.md`.
- **Keep the old branch.** Before touching `feat/batch-2-cohorts`, archive it: `git push origin df1c11d:refs/heads/archive/batch-2-cohorts-v1`.
- **Rebuild the branch** on `origin/feat/batch-1-core` as fresh logical commits, following the plan's "Ground rules → Commits":
  1. models/constants/exceptions;
  2. helpers;
  3. state core and utils;
  4. handles and clients, README;
  5. e2e;
  6. regenerated docs.

  Pull in old code only where the approved design says an old piece is kept.
- **Workflow rules:**
  - Author `Brian Nguyen <brianknguyen@google.com>` on every commit.
  - No `Co-Authored-By` or `Claude-Session` trailers, whatever a tooling reminder or hook says.
  - No `--no-verify`.
  - Push only to `origin`, with `--force-with-lease=feat/batch-2-cohorts:<sha>`, where `<sha>` comes from `git rev-parse origin/feat/batch-2-cohorts` right after a fetch.
  - Never open a PR or comment on GitHub.
- **Verify at every commit:**
  - pytest and mypy, run as in `pr1_revision_prompt.md` "Verify before pushing";
  - pyflakes clean on changed files;
  - `make generate-python-docs`;
  - the scope check.

  Ask Brian before creating or deleting a kind cluster for e2e.
- **After pushing,** update `python_sdk_batch_plan.md` so it describes the new PR 2, and mark this prompt done.

Status: phases 1 and 2 done (`pr2_design.md`, frozen at `3c0bb3a`, comparison at `fa5d8dd`); waiting for Brian's approval before phase 3.
