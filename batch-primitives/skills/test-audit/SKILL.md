---
name: test-audit
description: "Audit the tests in a PR (given as a number, URL, or branch) for low-value, implementation-coupled, or duplicative tests and report which should be removed, then wait for confirmation. Use when the user says /test-audit <PR> or asks whether a PR's tests are worth keeping."
---

# Test Audit (PR)

Given a PR, decide which of its added or changed tests fail the value bar below, and report the removal candidates. This skill is read-only: never edit, delete, commit, or push. After reporting, stop and wait for Brian to confirm which candidates to remove.

Adapted from https://github.com/openclaw/openclaw/blob/main/.agents/skills/test-audit/SKILL.md (authoring gate, junk patterns, retention bar, candidate evidence). Optimize for confidence, not deletion count: a few high-confidence candidates beat a large speculative list, and "no candidates" is a valid result.

## Input

The argument is a PR number, URL, or branch. If it is a bare number, use the current repo's remote. If nothing is given, ask which PR.

## Steps

1. Fetch the PR: `gh pr view <pr> --json title,body,baseRefName,headRefName,files` and `gh pr diff <pr>`. Identify test files and test cases the PR adds or changes. Ignore tests the PR does not touch.
2. Check whether the PR branch is checked out locally or available as a worktree under `.claude/worktrees/`. If not, read files via `gh api` at the head ref rather than switching branches.
3. Read root and scoped `AGENTS.md` / `CLAUDE.md` files for the touched directories first.
4. For each added or changed test, read the complete test and its production owner: entry point, callers, callees, sibling implementations, overlapping tests, and relevant history (`git log`, `gh pr view` discussion). When a test claims dependency-backed behavior, inspect the dependency source or types directly.
5. Apply the authoring gate and junk patterns. Then apply the retention bar to anything that matches, since a match alone is not enough to recommend removal.
6. Report using the format below and stop.

## Authoring gate

A test earns its place only if all four have answers:

1. What observable behavior, invariant, or independent contract does it protect?
2. What credible regression makes it fail?
3. Why does existing coverage not already catch that failure? Each contract has one primary test owner at the strongest boundary; another layer needs its own distinct risk, such as a transport or lifecycle failure the owner cannot reach. Prefer extending a table-driven case or shared fixture over a near-duplicate test.
4. Does it need a production seam (export, flag, wrapper, injection hook) that no production caller needs? If yes, the test belongs at the real boundary instead.

A test that would break under behavior-preserving refactoring asserts implementation, not behavior.

Bug regression tests must fail on the pre-fix code for the intended reason and pass after the fix. A regression test that never demonstrably failed proves the mock, not the fix. One regression at the owner boundary covers the bug; do not replay the same scenario at every layer it crosses. When a PR claims to fix a bug, verify the regression test fails without the fix if that is feasible without editing the checkout (e.g. in a scratch worktree), otherwise say it was not verified.

## Junk patterns

- assertion-free coverage probes;
- self-comparisons and identity copiers;
- copied fixtures, inventories, manifests, or export lists;
- exact source, import, or string greps;
- private predicate or call-shape tests duplicated at real boundaries;
- duplicate invocations of the same contract;
- provider-local replays of shared helpers;
- tests whose only purpose is preserving test-only exports, globals, or wrappers;
- dead production code whose only callers are tests;
- expected values produced by the helper or renderer under test;
- mocks that implement the asserted behavior, or one identical mock standing in for different APIs;
- fixtures that supply the receipt, admission, or callback ordering the owner should produce, or persistence asserted against a store the path never writes;
- capability tests that restate declared flags instead of exercising the delivery or acknowledgement the flag promises;
- negative controls that pass for an unrelated reason, such as a denial from a different guard or a rejection the production path never reaches;
- names or fixtures that promise more than the input exercises.

## Retention bar

Keep a test when it independently enforces a public API, SDK, protocol, config, migration, storage, security, platform, default, prompt-byte, generated cross-language, package, release, or architecture contract. Also keep:

- call ordering when order is observable behavior;
- regressions with a credible failure mode;
- source inspection when it is the cheapest independent guard: it fails when the contract changes (the user-facing key, byte, or path) and survives an identifier-only refactor;
- a test that fails on the baseline: treat it as a possible product bug, not a deletion candidate.

Static or slow is not a deletion reason. A test that resembles implementation may still be the independent contract; prove otherwise before recommending removal.

## Report format

Lead with the verdict: how many of the PR's tests you recommend removing, out of how many you reviewed. Then one entry per removal candidate, with every field below. A candidate missing a field is not ready, so either close the gap or leave it out.

- **Test**: exact name and `file:line`
- **Junk pattern**: which one it matches
- **Detectable failure**: what failure it can actually detect
- **Non-test callers**: callers of the covered production or support seam
- **Stronger proof**: the remaining owner-boundary test that covers it, or why none is needed
- **History**: why the test or seam exists
- **Unlocked deletion**: production or test-support code that can go with it
- **Risk and validation**: risk, plus the focused command that would confirm removal is safe

Then list tests you reviewed and are keeping despite resembling a junk pattern, with the retention reason in one line each. Separate what you verified (read the code, ran a command) from what you inferred.

End by asking which candidates Brian wants removed. Do not remove anything, and do not offer to commit or push.
