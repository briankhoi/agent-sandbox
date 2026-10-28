# G6 writing pass: comments, docstrings, commits, PR descriptions, and issues

Author: G6 task conversation, 2026-10-28. Audited against `upstream/main:AGENTS.md` (`## Coding conventions` line 89, `## Writing` lines 104-125, `## Pull requests and CI` line 171, fetched at `355803af`) and `batch-primitives/handoff/working_rules.md` (`## Style` and `## Minimal fixes`). Facts are marked measured, verified (read in code) or inferred.

## Summary (1 minute)

- All 77 writing-audit items across PR 1 code (5), PR 2 code (2), PR 3 code (5), the three commit messages (8), the three PR descriptions (21), and the five issues (36) are applied, together with Brian's required `sandbox.py:199-202` comment explaining the sync/async callback-release split, both PR 3 accuracy fixes (`calls atexit.register(...)` and unified `B` units from `results/g4/client_retained.out`), all five `issues-final.md` repro-check fixes, and the prose follow-ups caught by the independent self-check subagent.
- Zero executable code changed in any of the three PR branches (verified: docstring-stripped `ast.dump` is byte-for-byte identical across every touched `.py` file). All embedded Python snippets in `issues-final.md` also have unchanged ASTs (verified).
- All checks pass per PR (measured): `pycodestyle --max-line-length=120` on changed lines (0 issues), `mypy` (37 source files clean on each PR), touched unit tests in both sync and async shells (PR 1: 291 passed + 3 subtests; PR 2: 70 passed; PR 3: 295 passed + 3 subtests), and `git merge-tree --write-tree upstream/main <head>` (exit 0 on all three).
- Local probe outputs `handle_retained.out` and `handle_cycle_edges.out` (with code SHA, tree, SDK version, and Python version) are committed and pushed on `bench/async-mem-probe` (`f8df9206` -> `2ec456da`), and all three PR branches plus `review-diff/leakfix-writing-pr1..3` are pushed and verified on `origin` via `git ls-remote`.

## 1. Branch heads and compare links

| Item | Branch | Old head | New head | Base | Synthetic `review-diff` ref | Review diff / compare |
| --- | --- | --- | --- | --- | --- | --- |
| PR 1 | `fix/python-sdk-release-terminated-handles` | `0a54a028` | `9feeb8a9` | `7f40dc1a` | `review-diff/leakfix-writing-pr1` (`7c3b42f6`) | https://github.com/briankhoi/agent-sandbox/compare/0a54a028..review-diff/leakfix-writing-pr1 — check 5 comment edits across `sandbox.py`, `async_sandbox.py`, `sandbox_client.py`, `async_sandbox_client.py` |
| PR 2 | `fix/python-sdk-free-closed-connectors` | `e62610f9` | `fbbbb944` | `c4ed9ba9` | `review-diff/leakfix-writing-pr2` (`b8c5b732`) | https://github.com/briankhoi/agent-sandbox/compare/e62610f9..review-diff/leakfix-writing-pr2 — check `sandbox.py:199-202` sync/async split comment plus `connector.py` and `test_sandbox.py` comment edits |
| PR 3 | `fix/python-sdk-release-client-exit-hook` | `afae285e` | `368cedb6` | PR 1 `9feeb8a9` | `review-diff/leakfix-writing-pr3` (`f23f11b1`) | https://github.com/briankhoi/agent-sandbox/compare/afae285e..review-diff/leakfix-writing-pr3 — includes PR 1's prose edits because PR 3 sits on PR 1; PR 3-only `range-diff` below |
| Bench | `bench/async-mem-probe` | `f8df9206` | `2ec456da` | `f8df9206` | n/a (fast-forward) | https://github.com/briankhoi/agent-sandbox/compare/f8df9206..2ec456da — check `leakfix/handle_retained.out` and `leakfix/handle_cycle_edges.out` |

Full branch compares against bases:
- PR 1 vs base: https://github.com/briankhoi/agent-sandbox/compare/7f40dc1a..fix/python-sdk-release-terminated-handles
- PR 2 vs base: https://github.com/briankhoi/agent-sandbox/compare/c4ed9ba9..fix/python-sdk-free-closed-connectors
- PR 3 vs PR 1: https://github.com/briankhoi/agent-sandbox/compare/fix/python-sdk-release-terminated-handles..fix/python-sdk-release-client-exit-hook

PR 3 incremental `git range-diff 0a54a028..afae285e 9feeb8a9..368cedb6`:

```text
1:  afae285e ! 1:  368cedb6 fix(python-sdk): stop the exit hook from keeping closed clients alive
    @@ Metadata
      ## Commit message ##
         fix(python-sdk): stop the exit hook from keeping closed clients alive
     
    -    AsyncSandboxClient registers atexit.register(self._atexit_cleanup) by
    +    AsyncSandboxClient calls atexit.register(self._atexit_cleanup) by
         default, and SandboxClient registers self._delete_automatic_sandboxes
         when cleanup=True. A bound method keeps its object, so the atexit list
    -    holds every client until the program exits, even after close(). A
    -    closed async client keeps about 8 KiB of Python objects that way (1.4
    -    KiB if never used), a sync client about 9 KiB, and a program that
    -    creates a client per task grows by that much per task.
    +    holds every client until the program exits, even after close(). A closed
    +    async client keeps 9,600 B of Python objects that way, or 1,369 B if it
    +    was never used. A sync client keeps 11,407 B. A program that creates a
    +    client per task grows by that much per task.
     
    -    Exit cleanup reads only the registry, the explicit-claims set and, in
    -    async, the injected ApiClient. Both cleanup methods become static and
    -    are registered bound to those objects with functools.partial, the same
    -    pattern as the registry's terminate hook. Exit behavior is unchanged:
    -    closed, reused, failed-close and never-closed clients are all cleaned
    -    up as before, and the ApiClient object itself is passed so its settings
    -    are still read at exit. The registry and the set are never rebound,
    -    which the hook relies on. Sync __exit__ calls the same method, and
    -    delete_sandbox shares the tracked-handle branch with it, so the rule
    -    lives in one place.
    +    Exit cleanup reads only the _active_connection_sandboxes registry, the
    +    _explicit_claims set and, in async, the injected ApiClient. Both cleanup
    +    methods become static, and each client registers its method bound to
    +    those objects with functools.partial, the same pattern as the
    +    _on_terminated hook. Exit behavior is unchanged. Exit cleanup still
    +    covers closed, reused, failed-close and never-closed clients. The client
    +    passes the ApiClient object itself, so exit cleanup still reads its
    +    current settings. The client never rebinds the registry or the set, and
    +    the hook relies on that. Sync __exit__ calls the same method, and
    +    delete_sandbox shares the tracked-handle branch with it, so both delete
    +    a tracked handle the same way.
     
      ## clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_client.py ##
     @@ clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_client.py:
     -        # Held so atexit cleanup can read configuration/default_headers/cookie live at cleanup time
     -        self._injected_api_client = api_client
      
    -+        # The exit hook holds these two containers, so they are changed in place and never rebound.
    ++        # The exit hook holds these two containers, so the client changes them in place and never rebinds them.
              if cleanup:
    -+            # Static and bound to the cleanup data with a partial, so the hook never keeps the
    -+            # client alive; registering a method of the client here would bring that back.
    -+            # The api_client is passed itself, not a copy, so its settings are read at cleanup time.
    ++            # Register a static method bound to the cleanup data with functools.partial, so the hook
    ++            # never keeps the client alive. A bound method of the client would keep it alive until exit.
    ++            # Pass the api_client itself, not a copy, so exit cleanup reads its settings as they are at exit.
      ## clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_client.py ##
    -+        # The exit hook holds these two containers, so they are changed in place and never rebound.
    ++        # The exit hook holds these two containers, so the client changes them in place and never rebinds them.
              if cleanup:
    -+            # Static and bound to the cleanup data with a partial, so the hook never keeps the
    -+            # client alive; registering a method of the client here would bring that back.
    ++            # Register a static method bound to the cleanup data with functools.partial, so the hook
    ++            # never keeps the client alive. A bound method of the client would keep it alive until exit.
    -+        # Shared by delete_sandbox and exit cleanup, so the rule lives in one place.
    ++        # delete_sandbox and exit cleanup both call this, so they delete a tracked sandbox the same way.
      ## clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_async_sandboxclient.py ##
    -+        """A client with cleanup on and the exit hook it registered."""
    ++        """Returns a client with cleanup on and the exit hook it registered."""
```

## 2. Verification (measured)

### Prose-only AST proof

For every `.py` file modified between pre-G6 head and new head (zero non-`.py` files modified), old and new sources were parsed with `ast.parse`, `Expr(Constant(str))` docstrings were stripped from `Module`, `ClassDef`, `FunctionDef`, and `AsyncFunctionDef` bodies, and `ast.dump` was verified byte-for-byte equal:

- **PR 1 (`0a54a028` -> `9feeb8a9`)**:
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox.py`: equal (`20,776` bytes)
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_client.py`: equal (`53,912` bytes)
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox.py`: equal (`19,606` bytes)
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_client.py`: equal (`39,140` bytes)
- **PR 2 (`e62610f9` -> `fbbbb944`)**:
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/connector.py`: equal (`94,914` bytes)
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox.py`: equal (`19,070` bytes)
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_sandbox.py`: equal (`51,277` bytes)
- **PR 3 (`afae285e` -> `368cedb6`, and incremental `0a54a028..afae285e` vs `9feeb8a9..368cedb6`)**:
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox.py`: equal (`20,776` bytes)
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_client.py`: equal (`54,496` bytes)
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox.py`: equal (`19,606` bytes)
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_client.py`: equal (`41,702` bytes)
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_async_sandboxclient.py`: equal (`244,167` bytes)
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_sandboxclient.py` (incremental base/head): equal

### Lint, typecheck, unit tests, metadata, and merge-tree

| PR | Commit count | Author & committer / trailers | `pycodestyle` (changed lines, `<=120`) | `mypy` (`k8s_agent_sandbox`) | Unit tests (sync + async) | `merge-tree` vs `upstream/main` (`355803af`) |
| --- | ---: | --- | --- | --- | --- | --- |
| PR 1 (`9feeb8a9`) | 1 (`7f40dc1a..HEAD`) | `Brian Nguyen <brianknguyen@google.com>`, 0 trailers | 0 issues | 37 files, 0 issues | 291 passed, 3 subtests (`test_sandboxclient.py`, `test_async_sandboxclient.py`, `test_adoption_lifecycle.py`) | exit 0 (`6989c222836e`) |
| PR 2 (`fbbbb944`) | 1 (`c4ed9ba9..HEAD`) | `Brian Nguyen <brianknguyen@google.com>`, 0 trailers | 0 issues | 37 files, 0 issues | 70 passed (`test_sandbox.py`, `test_async_sandbox.py`) | exit 0 (`684a4f1b9af4`) |
| PR 3 (`368cedb6`) | 1 (`9feeb8a9..HEAD`) | `Brian Nguyen <brianknguyen@google.com>`, 0 trailers | 0 issues | 37 files, 0 issues | 295 passed, 3 subtests (`test_sandboxclient.py`, `test_async_sandboxclient.py`, `test_adoption_lifecycle.py`) | exit 0 (`be7952aa07f4`) |

All four code/bench worktrees (`-par-leakfix`, `-par-cycle`, `-par-exithook`, `-par-mem`) have empty `git status --short`.

## 3. Adjustments to audit replacements and resolution of Inputs 1–5

### Prompt-specified adjustments applied
- **Audit `1.2`** (`sandbox.py:239-240`, `async_sandbox.py:249-250`): used `_on_terminated` instead of `it` (`Call _on_terminated before clearing claim_name, because that name is the client's key.`), because the preceding sentence's subject is `The client`.
- **PR 2 sync/async split comment** (`sandbox.py:199-202`): replaced the 1-line comment above `self.connector._release_sandbox_callbacks()` with Brian's 4-line comment explaining why `SandboxConnector.close()` keeps callbacks for resets and snapshot suspend/resume while `Sandbox.close_connection()` and `AsyncSandboxConnector.close()` drop them. Kept the shared 3-line comment inside `_release_sandbox_callbacks()` identical between `connector.py:828-830` and `async_connector.py:597-599`.
- **PR 3 accuracy fixes 1 & 2** (commit message and PR 3 text): changed `registers atexit.register(...)` to `calls atexit.register(...)`, and unified per-client `tracemalloc` figures on `B` from `results/g4/client_retained.out` (`1,369 B` unused async, `9,600 B` used async, `11,407 B` sync `cleanup=True`, against `about 610 B` after PR 3).

### Audit replacements adjusted to satisfy Writing rules
- **Audit `4.5`** (PR 3 commit message, paragraph 2, sentence 2): the audit left `and are registered bound to those objects` in passive voice while fixing the identical sentence in `5.14`. Adjusted to match `5.14` (`Both cleanup methods become static, and each client registers its method bound to those objects with functools.partial, the same pattern as the _on_terminated hook.`) for R1 (active voice).
- **Audit `5.4`** (`G6-writing.md`, PR 1 Notes bullet 3): `Two small behavior differences.` is a verbless fragment (R2). Adjusted to `There are two small behavior differences.`
- **Audit `5.6`, `5.21`, and PR descriptions `Tests` / `Measured` / `_get_service_fqdn` lines** (`G6-writing.md`, PRs 1–3): rewrote verbless fragments, parentheticals, colons, undefined `GC` acronyms in prose, and actorless passives into full active-voice sentences with one idea each.
- **Audit section 6 (`issues-final.md`)**: turned participial lead-in fragments (`Measured with...`, `Inferred from the code, not measured.`, `Optional, on a real cluster.`) into full active-voice sentences (`I measured...`, `I inferred... and did not measure it.`, `The optional snippet below shows...`, `The script needs no cluster...`), eliminated parentheticals, semicolons, and clause-joining colons across all five issue bodies, replaced undefined `GC` with `cyclic garbage collector` / `cyclic collector`, and defined `_active_connection_sandboxes` and `_explicit_claims` in Issue 4 (`:310`).

### Resolution of Inputs items 1–5 (`issues-final.md`)
1. **Issue B local probe under `results/g3/` (`issues-final.md:152`)**: ran `handle_cycle_edges.py` in a throwaway venv `/tmp/g6-venv-probe` on upstream `c4ed9ba9` (`1.0.9.dev40+gc4ed9ba96`, Python 3.13.15), committed `test/benchmarks/batch-claim/results/leakfix/handle_cycle_edges.out` on `bench/async-mem-probe` (`2ec456da`) with code SHA, tree, SDK version, and Python version, and moved the bullet out of the kind-cluster `g3` list into its own top-level bullet (`issues-final.md:152`) citing `handle_cycle_edges.py` and `handle_cycle_edges.out` (`0.694 MiB` before `gc.collect()`, `0.006 MiB` with callbacks cleared). (Note on mistake during local setup: I was wrong about copying the shared venv with `cp -a` and running `/tmp/g6-venv-probe/bin/pip`, because `bin/pip`'s shebang pointed at the shared venv's Python binary; I immediately uninstalled the added packages from the shared venv, verified `import k8s_agent_sandbox` raises `ImportError`, and rebuilt `/tmp/g6-venv-probe` with `python3 -m venv` before removing `/tmp/g6-venv-probe`.)
2. **Issue A sync handle figure (`issues-final.md:39`)**: ran `handle_retained.py` in `/tmp/g6-venv-probe` on `c4ed9ba9`, committed `test/benchmarks/batch-claim/results/leakfix/handle_retained.out` on `bench/async-mem-probe` (`2ec456da`) with code SHA, tree, SDK version, and Python version, and rewrote `issues-final.md:39` as a cited local-probe bullet (`0.044 MiB` malloc'd, `0.036 MiB` in `tracemalloc` on Python 3.13.15).
3. **Issue B 600-cycle `0.46 MiB` RSS vs glibc bytes (`issues-final.md:150`)**: split into separate active-voice sentences stating that breaking the cycle cut RSS growth to `0.46 MiB` per 100 cycles and, in its own sentence, that glibc bytes in use fell from `72 MiB` at the start to `41 MiB` at the end.
4. **Issue 3 missing conjunction and parenthetical (`issues-final.md:225`)**: rewrote with `or` and no parentheticals (`That happens on close_connection() or on a failed get_sandbox() lookup, where the client closes the handle locally and keeps tracking it so cleanup still reaches it.`).
5. **Issue A `about 0.25 MiB` and SHA annotations (`issues-final.md:12-14, 123, 128, 206, 360, 473-476`)**: updated `:128` and `:473` to `about 0.25 MiB` while leaving literal stdout at `:125` untouched, and annotated `:12-14`, `:123`, `:206`, `:360`, and `:473-476` with `<new sha> (prose-only amend of <old sha>)`.

## 4. Self-check reviewer findings and resolutions

A fresh `inherit` subagent reviewed the three PR commits, `bench/async-mem-probe`, `G6-writing.md`, and `issues-final.md` without seeing our checklist:

1. **`.py` code comments, docstrings, sync/async parity, and PR 2 split comment — Passed with 0 issues.** Verified byte-for-byte shared-comment parity across sync and async shells, active voice, why-focus, zero AST differences, and clear explanation of the sync/async callback-release split at `sandbox.py:199-202`.
2. **Commit messages (`5cf420fa`, `789737a9`, `58f2f893`) — Resolved by amending (`9feeb8a9`, `fbbbb944`, `2ec456da`), rebasing PR 3 (`368cedb6`), and re-running Step 5:**
   - PR 1 commit message: changed `Sandbox.terminate()` to `terminate()` so it covers both `Sandbox` and `AsyncSandbox`, and aligned `1,662 MiB` (which was arena size) to `1,660 MiB of malloc'd memory in use` to match `results/memprobe/README.md:18` (`uordblks = 1,660 MB`, verified in `summary.json:41,53`) and `issues-final.md:36`.
   - PR 2 commit message: removed parenthetical `(get_pod_ip, get_pod_name, get_service_fqdn)`, replaced undefined `GC` in body with `when the collector runs`, and split the 38-word 3-clause sync `close_connection()` sentence into two sentences matching PR 2's description.
   - `bench/async-mem-probe` commit message: added a motivation body explaining why `handle_retained.out` and `handle_cycle_edges.out` were recorded.
3. **PR 1 description accuracy & style (`G6-writing.md`) — Resolved:**
   - I was wrong about PR 1's draft `Tests` lead-in (`Each test runs against the in-memory API for both clients:`), because bullet 4 (`test_tracked_handle_does_not_keep_its_client_alive` in `test_sandboxclient.py:992-1009` and `test_async_sandboxclient.py:2156-2174`, verified) uses `unittest.mock.patch` rather than `FakeCustomObjectsApi`. Fixed to `The tests cover both clients.` and scoped `Against the in-memory API` to the lifecycle bullets; also replaced `Notes:` with `**Notes.**`, aligned `1,660 MiB of malloc'd memory in use`, and specified `Async exit cleanup no longer sends a DELETE for every terminated handle.` (verified in `async_sandbox_client.py` vs `sandbox_client.py`).
   - Replaced undefined `GC` in PR 2 (`Alternatives considered`, `Tests`) and PR 3 (`Tests`, `Measured`) prose with `cyclic garbage collector` / `cyclic collector`.
4. **Remaining prose issues in `issues-final.md` (untouched lines inside the 5 issues plus `:128`, `:140`, `:142`, `:379-381`) — Resolved:**
   - Fixed `The about 0.25 MiB` at `:128` (`About 0.25 MiB per cycle remains...`), removed remaining parentheticals (`:26`, `:28`, `:48`, `:114`, `:140`, `:142`, `:160`, `:197`, `:223`, `:237`, `:265`, `:276`, `:299`, `:316`, `:351`, `:379`, `:395`, `:455`), split `:142` and `:377` into single-idea sentences, converted skipped fragment/passive bullets (`:37-38`, `:44`, `:136`, `:144`, `:151`, `:303`, `:307`, `:317`, `:379-381`) into full active-voice sentences, and replaced `Related:` labels with `See also ...` sentences. Verified zero em dashes, zero semicolons, and zero parenthetical asides remain in any of the five issue texts, and all 6 embedded Python ASTs remain identical.
5. **Minimal-fixes check on pre-existing PR 2 / PR 3 code — No executable change made (reported for record):**
   - The reviewer confirmed zero widened types across PRs 1–3, PR 1's `_on_terminated` is minimal, and PR 3 removes `_injected_api_client`. It noted two pre-existing executable-code simplifications outside G6's prose-only scope: (a) on `main`, `AsyncSandboxConnector._get_service_fqdn` (`async_connector.py:150`) is unread and `SandboxConnector._get_pod_name` / `_get_service_fqdn` (`connector.py:648-649`) are read only during `__init__`, which PR 2 (`e62610f9`) chose to clear rather than delete; (b) in PR 3 (`sandbox_client.py:425-429`), `_delete_tracked_sandbox` keeps its own `try/except` so failed generated-sandbox deletes at exit log `"Failed to delete sandbox ..."` rather than falling through to `_delete_automatic_sandboxes`'s `"Cleanup failed for ..."`. Both preserve existing attributes/log strings and changing either would alter executable ASTs without Brian's approval, so G6 left executable code untouched.

## 5. Final PR titles and descriptions

### PR 1

```markdown
Title: fix(python-sdk): drop terminated sandboxes from the client (#<issue A>)

Fixes #<issue A>

**Problem.** `SandboxClient` and `AsyncSandboxClient` keep every handle they create or attach in the `_active_connection_sandboxes` registry, and `terminate()` leaves it there. Each kept async handle holds about 0.7 MiB, so a driver that creates and terminates sandboxes grows without bound. On main, 2,400 cycles left 1,660 MiB of malloc'd memory in use.

**Change.** The client gives each handle it tracks a private `_on_terminated` hook. `terminate()` calls it after the delete succeeds and before it clears `claim_name`. The hook removes the entry only if it is still that same handle. A failed delete keeps the entry, so `delete_all()`, the context managers and exit cleanup still retry it. Closed handles whose claim still exists stay tracked, as before. The change applies to both clients.

The hook is the static `_untrack` bound to the registry dict with `functools.partial`, not a method of the client, so a tracked handle never keeps its client alive.

**Notes.**
- The pop after `terminate()` in `delete_sandbox()` stays on purpose. It covers subclasses whose `terminate()` doesn't call `super()`.
- The sync client takes no lock, as before. The identity-checked removal is safer than the existing unconditional pop. The async hook has no `await` between its check and its delete.
- There are two small behavior differences. `client.delete_sandbox(name)` after `sandbox.terminate()` now sends one DELETE that returns 404, which the client treats as success, instead of none. Async exit cleanup no longer sends a DELETE for every terminated handle.

**Alternatives considered.**
- Pruning terminated handles on the next insert frees nothing after the last create.
- A constructor argument would break subclasses with a fixed `__init__`.
- A bound method of the client as the hook would pin the client while any handle is tracked.
- Weak references in the registry would lose cleanup for handles the caller dropped.

**Tests.** The tests cover both clients.
- Against the in-memory API, `terminate()` stops tracking handles from `create_sandbox` and from `get_sandbox`.
- Against the in-memory API, a failed `terminate()` stays tracked, and exit cleanup deletes the claim.
- Against the in-memory API, terminating a replaced handle keeps its replacement.
- Reference counting frees a dropped client that still tracks a handle.

Reverting the change a test covers makes that test fail.

**Measured.** On a kind cluster, 40 async workers ran 600 create, command and terminate cycles. The RSS slope fell from 90.1 MiB per 100 cycles on main to 29.3 MiB with this change. #<issue B> accounts for the rest, and a separate PR fixes it.
```

### PR 2

```markdown
Title: fix(python-sdk): free closed sandbox handles without the cyclic GC (#<issue B>)

Fixes #<issue B>

**Problem.** A handle passes its bound methods to its connector and strategy, so handle and connector form a reference cycle. Only the cyclic garbage collector frees a closed handle that nobody references, and it can't see the ~0.7 MiB of OpenSSL state each closed async handle holds. With #<issue A> fixed, 600 cycles still grow RSS by 29.3 MiB per 100 cycles. After a program creates and terminates 100 sandboxes, 0.66 MiB per handle stays allocated as long as nothing else allocates.

**Change.** Each connector gets `_release_sandbox_callbacks()`, which clears the stored callbacks on the connector and its strategy.
- The async connector calls it at the end of its final `close()`.
- The sync handle calls it from `close_connection()`, right after `connector.close()`. The sync connector's `close()` also serves as a reset for reconnects and for snapshot suspend and resume, which must keep the callbacks.
- The sync release holds the transport lock, so a connect on another thread never sees a callback cleared between checking and calling it.

The fields were already optional. Nothing reads the async connector's `_get_service_fqdn`, but it still holds a bound method, so `_release_sandbox_callbacks()` clears it too.

Using a sandbox's connection after `close_connection()` or `terminate()` is unsupported. That includes calls through `sandbox.connector`, or through a `commands` or `files` object kept from before the close. After this change, some connection modes raise or route without the pod IP instead of silently reconnecting. A follow-up, #<issue 5>, will make the sync client refuse such use with a clear error, as the async client already does.

**Alternatives considered.**
- Leaving it to the cyclic collector sets the peak by when a collection runs, and frees nothing if nothing else allocates.
- Calling `gc.collect()` from the library causes global pauses.
- With weak method references, a handle that only an executor still references would silently lose its callbacks.
- Clearing inside the sync connector's `close()` breaks reconnects and snapshot resume.

**Tests.**
- For each client, with the cyclic collector disabled, reference counting frees a closed, dropped handle and its connector. The test covers the Direct, InCluster, SandboxdPodTunnel and SandboxdInCluster modes. Restoring any one cleared reference makes the test fail.
- A sync test checks that `_release_sandbox_callbacks()` waits for the transport lock.
- The reconnect and snapshot extension tests pass unchanged.

**Measured.** On a kind cluster, the async client ran with this change on top of #<PR 1>.
- Over 600 cycles, the RSS slope fell to 0.46 MiB per 100 cycles, down from 29.3 MiB on #<PR 1> alone and 90.1 MiB on main.
- After creating 100 sandboxes and terminating all of them, each handle retained 0.0001 MiB, down from 0.66 MiB without this change.
```

### PR 3

```markdown
Title: fix(python-sdk): stop the exit hook from keeping closed clients alive (#<issue 4>)

Fixes #<issue 4>

This PR is stacked on #<PR 1>. Its tests free a client that still tracks handles, which works only with the registry-bound `_on_terminated` hook from #<PR 1>.

**Problem.** `AsyncSandboxClient` calls `atexit.register(self._atexit_cleanup)` by default, and `SandboxClient` registers `self._delete_automatic_sandboxes` with `cleanup=True`. A bound method keeps its object, so every client stays alive until exit, even after `close()`. `tracemalloc` measures 1,369 B per unused async client, 9,600 B per used async client and 11,407 B per sync client. After `gc.collect()`, 200 of 200 closed clients are still alive.

**Change.** Exit cleanup reads only the `_active_connection_sandboxes` registry, the `_explicit_claims` set and, for async, the injected ApiClient. Both cleanup methods become static, and each client registers its method bound to those objects with `functools.partial`, the same pattern as the `_on_terminated` hook.
- Exit behavior is unchanged. Exit cleanup still covers closed, reused, failed-close and never-closed clients.
- The client passes the ApiClient object itself, so exit cleanup still reads its current settings.
- The client never rebinds the registry or the set after construction, and the hook relies on that. A comment at the assignments says so.
- Sync `__exit__` calls the same method, and `delete_sandbox()` shares the tracked-handle branch with it, so both delete a tracked handle the same way. As a result, exit cleanup no longer goes through a subclass's override of sync `delete_sandbox()`. No subclass in this repo overrides it.

About 610 B per client remain, which is the atexit entry with an empty dict and set.

**Alternatives considered.**
- Unregistering in `close()` when nothing is tracked loses cleanup for clients reused after `close()`, unless a flag re-registers on every insert path.
- A weak-reference hook loses cleanup for a client dropped while its sandboxes are in use.
- Unregistering in sync `__exit__` breaks reuse after the `with` block.
- Documenting "reuse one client" leaves the growth.

**Tests.**
- For both clients, with the cyclic garbage collector disabled, reference counting frees a closed and dropped client, both with an empty registry and with tracked handles. Its registered exit hook still deletes the tracked generated sandbox and only closes the explicit one.
- The async registration tests now check that the hook is bound to the client's registry, set and ApiClient.
- Existing exit-cleanup tests now fill the client's registry, or call the static method with their own, instead of replacing the dict.
- Reverting the change a test covers makes that test fail. The real-process-exit tests pass unchanged.

**Measured.** With Python 3.12 and the cyclic collector enabled, creating and closing 200 clients per case and calling `gc.collect()` before each reading left 0 of 200 clients alive and about 610 B per client. Main left 200 of 200 clients alive, keeping 1,369 B per unused async client, 9,600 B per used async client and 11,407 B per sync client.
```
