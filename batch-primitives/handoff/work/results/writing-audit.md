# Writing audit: memleak PRs 1 to 3, commits, PR texts, issues

Read-only. Branch tips audited: PR 1 `0a54a028`, PR 2 `e62610f9`, PR 3 `afae285e`. Notes branch pulled at audit time.

## Rules cited (short keys)

- **R1** AGENTS.md Writing: "**Use active voice.** Say who does what". Pitfall list: "passive constructions with no actor".
- **R2** AGENTS.md Writing: "**Write full sentences, one idea each.** Don't chain clauses with colons, semicolons, and parenthetical asides."
- **R3** AGENTS.md Writing: "**Define a term before you use it,** and use the project's existing words ... Don't coin shorthand."
- **R4** AGENTS.md Writing: "**Say what the code does and why,** not how it got that way."
- **R5** AGENTS.md line 89: "Comments explain **why**, not **what**."
- **RC** AGENTS.md line 171: "Commit messages must stand alone ... Use the terminology already established in the code and docs; do not invent new names for existing concepts."
- **BS** working_rules.md Style: "no em dashes, avoid colons, comments explain why. Keep sync and async comments identical."
- **PyDoc** Google Python Style Guide 3.8.3 (linked from Writing): function docstrings are descriptive sentences, e.g. """Returns ...""" / """Fetches ...""".

No em dashes anywhere in the audited prose (checked by grep). All sync and async comment pairs are currently identical, and every replacement below keeps each pair identical.

Counting: one entry per rule-breaking sentence or comment block. Items marked (low) are passive voice with a named actor ("by the client"). R1 still covers them, but they are the least important.

---

## 1. PR 1 code (origin/fix/python-sdk-release-terminated-handles vs 7f40dc1a): 5 violations

Path prefix `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`.

**1.1 (low)** `sandbox.py:46-47` and `async_sandbox.py:47-48` (identical)
- Current: `# Set by the client that tracks this handle, so terminate() can drop the` / `# client's entry once the claim is gone.`
- Rule: R1.
- Replace with: `# The client that tracks this handle sets this hook, so terminate() can drop the` / `# client's entry once the claim is gone.`

**1.2** `sandbox.py:239-240` and `async_sandbox.py:249-250` (identical)
- Current: `# ... It` / `# runs before claim_name is cleared since that name is the client's key.`
- Rule: R1 (passive "is cleared" with no actor). R3 (the subject of the previous sentence is "The client", so "It" reads as the client, not the hook).
- Replace the second sentence with: `Call it before clearing claim_name, because that name is the client's key.` The first sentence stays as it is.

**1.3** `sandbox_client.py:348-349` and `async_sandbox_client.py:489-490` (identical)
- Current: `# Called by terminate() after a successful delete, while claim_name is still` / `# set, so the None check only narrows its type.`
- Rule: R2 (fragment with no subject). R1.
- Replace with: `# terminate() calls this after a successful delete, while claim_name is still` / `# set, so the None check only narrows its type.`

**1.4** `sandbox_client.py:350` and `async_sandbox_client.py:491` (identical)
- Current: `A newer handle may have replaced this one for the same claim, so only this exact handle is removed.`
- Rule: R1 (passive "is removed" with no actor).
- Replace with: `A newer handle may have replaced this one for the same claim, so remove only this exact handle.`

**1.5** `sandbox_client.py:351-352` and `async_sandbox_client.py:492-493` (identical)
- Current: `# Static and bound to the registry with a partial, so a tracked handle never` / `# keeps the client alive; an instance method here would bring that back.`
- Rule: R2 (fragment, and a semicolon joining two ideas). BS ("avoid colons", semicolon chaining). R3 ("a partial" is shorthand for `functools.partial`). R4 ("bring that back" describes history, how the code got this way).
- Replace with: `# This is static and bound to the registry with functools.partial, so a tracked` / `# handle never keeps the client alive. An instance method would keep it alive.`

Compliant: test comments `# With the collector off, only reference counting can free the client.` (test_sandboxclient.py:999 and test_async_sandboxclient.py:2166, identical, explain why gc is disabled). Test names read as sentences.

---

## 2. PR 2 code (origin/fix/python-sdk-free-closed-connectors vs c4ed9ba9): 2 violations

**2.1** `connector.py:831-832` (sync only, async has no lock, so parity is not affected)
- Current: `# Under the transport lock, so a connect() on another thread never sees a` / `# callback cleared between checking it and calling it.`
- Rule: R2 (fragment).
- Replace with: `# Hold the transport lock, so a connect() on another thread never sees a` / `# callback cleared between checking it and calling it.`

**2.2** `test/unit/test_sandbox.py:472`
- Current: `# Stands in for a connect() on another thread that is between checking a callback and calling it.`
- Rule: R2 (fragment with no subject).
- Replace with: `# Holding the lock stands in for a connect() on another thread that is between checking a callback and calling it.`

Compliant: the shared `_release_sandbox_callbacks` comment (`connector.py:828-830` and `async_connector.py:597-599`, identical, explains why), `sandbox.py:199` (`# A closed handle never reconnects, so its connector can let go of it.`), and both `# With the collector off, ...` test comments.

---

## 3. PR 3 code (origin/fix/python-sdk-release-client-exit-hook vs PR 1): 5 violations

**3.1** `sandbox_client.py:102` and `async_sandbox_client.py:197` (identical)
- Current: `# The exit hook holds these two containers, so they are changed in place and never rebound.`
- Rule: R1 (passive with no actor).
- Replace with: `# The exit hook holds these two containers, so the client changes them in place and never rebinds them.`

**3.2** `sandbox_client.py:108-109` and `async_sandbox_client.py:203-204` (identical)
- Current: `# Static and bound to the cleanup data with a partial, so the hook never keeps the` / `# client alive; registering a method of the client here would bring that back.`
- Rule: R2 (fragment and semicolon). BS. R3 ("a partial"). R4 ("bring that back").
- Replace with: `# Register a static method bound to the cleanup data with functools.partial, so the hook` / `# never keeps the client alive. A bound method of the client would keep it alive until exit.`

**3.3** `async_sandbox_client.py:205` (async only, because only async has an api_client)
- Current: `# The api_client is passed itself, not a copy, so its settings are read at cleanup time.`
- Rule: R1 (two passives with no actor).
- Replace with: `# Pass the api_client itself, not a copy, so exit cleanup reads its settings as they are at exit.`

**3.4** `sandbox_client.py:423`
- Current: `# Shared by delete_sandbox and exit cleanup, so the rule lives in one place.`
- Rule: R2 (fragment). R1. R3 ("the rule" is never defined. The reader has to infer "terminate, then pop only on success").
- Replace with: `# delete_sandbox and exit cleanup both call this, so they delete a tracked sandbox the same way.`

**3.5** `test/unit/test_async_sandboxclient.py:317`
- Current: `"""A client with cleanup on and the exit hook it registered."""`
- Rule: R2 (not a sentence). PyDoc (function docstrings describe what the function does, for example "Returns ...").
- Replace with: `"""Returns a client with cleanup on and the exit hook it registered."""`

Compliant: the docstring at test_async_sandboxclient.py:302, the `# With the collector off, ...` comments, and the existing `_atexit_cleanup` docstring (unchanged, Brian's wording).

---

## 4. Commit messages: 8 violations (PR 1: 2, PR 2: 1, PR 3: 5)

### PR 1 `0a54a028`

**4.1** Paragraph 1
- Current: `keep every handle they create or attach in _active_connection_sandboxes,` and later `bound to the registry dict`.
- Rule: R3 ("Define a term before you use it"). RC.
- Replace with: `keep every handle they create or attach in the _active_connection_sandboxes registry,`. "the registry dict" stays as it is.

**4.2** Paragraph 1
- Current: `Each kept handle holds its closed connector, about 0.7 MiB for the async client (a closed httpx client that keeps its SSL context and CA bundle), so a driver that creates and terminates sandboxes grows by that much per sandbox for as long as the client lives: 1,662 MiB of malloc'd memory after 2,400 create and terminate cycles on upstream main.`
- Rule: R2 (parenthetical aside plus a colon chaining four ideas).
- Replace with: `Each kept handle holds its closed connector. For the async client that is about 0.7 MiB, because the closed httpx client keeps its SSL context and CA bundle. A driver that creates and terminates sandboxes therefore grows by that much per sandbox for as long as the client lives. On upstream main, 2,400 create and terminate cycles left 1,662 MiB of malloc'd memory.`

Compliant: leads with the motivation, explains the static hook and why, and states the limit (paragraph 3).

### PR 2 `e62610f9`

**4.3 (low)** Paragraph 1
- Current: `so a closed handle that nobody references is freed only when the cyclic garbage collector runs.`
- Rule: R1.
- Replace with: `so only the cyclic garbage collector frees a closed handle that nobody references.`

Otherwise compliant. It leads with the cycle and its cost, and it explains why sync releases from close_connection() and why it takes the lock.

### PR 3 `afae285e`

**4.4** Paragraph 1
- Current: `A closed async client keeps about 8 KiB of Python objects that way (1.4 KiB if never used), a sync client about 9 KiB, and a program that creates a client per task grows by that much per task.`
- Rule: R2.
- Replace with: `A closed async client keeps about 8 KiB of Python objects that way, or 1.4 KiB if it was never used. A sync client keeps about 9 KiB. A program that creates a client per task grows by that much per task.`

**4.5** Paragraph 2, sentences 1 and 2
- Current: `Exit cleanup reads only the registry, the explicit-claims set and, in async, the injected ApiClient. ... the same pattern as the registry's terminate hook.`
- Rule: R3 and RC (the commit must stand alone. "the registry" is not defined in this message, and "terminate hook" is not the code's name).
- Replace with: `Exit cleanup reads only the _active_connection_sandboxes registry, the _explicit_claims set and, in async, the injected ApiClient. ... the same pattern as the _on_terminated hook.`

**4.6** Paragraph 2
- Current: `Exit behavior is unchanged: closed, reused, failed-close and never-closed clients are all cleaned up as before, and the ApiClient object itself is passed so its settings are still read at exit.`
- Rule: R2 (colon chain). R1 (three passives with no actor).
- Replace with: `Exit behavior is unchanged. Exit cleanup still covers closed, reused, failed-close and never-closed clients. The client passes the ApiClient object itself, so exit cleanup still reads its current settings.`

**4.7** Paragraph 2
- Current: `The registry and the set are never rebound, which the hook relies on.`
- Rule: R1.
- Replace with: `The client never rebinds the registry or the set, and the hook relies on that.`

**4.8** Paragraph 2
- Current: `..., so the rule lives in one place.`
- Rule: R3 ("the rule" is undefined).
- Replace with: `..., so both delete a tracked handle the same way.`

Advisory (not a Writing-rule violation, but worth fixing for accuracy): (a) `registers atexit.register(self._atexit_cleanup)` should say `calls atexit.register(...)` or `registers self._atexit_cleanup`. The same wording appears in the PR 3 text. (b) The commit says "about 8 KiB ... 1.4 KiB ... about 9 KiB", but the PR 3 text says "1.4 KB ... 9.6 KB ... 11.4 KB". The numbers and units (KiB or KB) differ.

---

## 5. PR descriptions (G4-memleak-final.md): 21 violations (PR 1: 6, PR 2: 5, PR 3: 10)

### PR 1 (updated)

**5.1** Problem
- Current: `... grows without bound: 1,662 MiB of malloc'd memory after 2,400 cycles on main.`
- Rule: R2.
- Replace with: `... grows without bound. On main, 2,400 cycles left 1,662 MiB of malloc'd memory.`

**5.2** Problem
- Current: `keep every handle they create or attach in `_active_connection_sandboxes`,`. Alternatives later says "Weak references in the registry".
- Rule: R3.
- Replace with: `keep every handle they create or attach in the `_active_connection_sandboxes` registry,`

**5.3** Change
- Current: `A failed delete keeps the entry, so `delete_all()`, the context managers and exit cleanup still retry it, and closed handles whose claim still exists stay tracked, as before. Both clients.`
- Rule: R2 (two ideas in one sentence, then a fragment).
- Replace with: `A failed delete keeps the entry, so `delete_all()`, the context managers and exit cleanup still retry it. Closed handles whose claim still exists stay tracked, as before. The change applies to both clients.`

**5.4** Notes, third bullet
- Current: `Small behavior differences: `client.delete_sandbox(name)` after `sandbox.terminate()` now sends one DELETE that returns 404 (treated as success) instead of none, and exit cleanup no longer sends a DELETE for every terminated handle.`
- Rule: R2 (colon, parenthetical, two ideas). R1 ("treated" has no actor).
- Replace with: `Two small behavior differences. `client.delete_sandbox(name)` after `sandbox.terminate()` now sends one DELETE that returns 404, which the client treats as success, instead of none. Exit cleanup no longer sends a DELETE for every terminated handle.`

**5.5** Alternatives considered (all four bullets use the `X: consequence` pattern)
- Rule: R2 (colon joins two clauses).
- Replace with:
  - `Pruning terminated handles on the next insert frees nothing after the last create.`
  - `A constructor argument would break subclasses with a fixed `__init__`.`
  - `A bound method of the client as the hook would pin the client while any handle is tracked.`
  - `Weak references in the registry would lose cleanup for handles the caller dropped.`

**5.6** Tests
- Current: list items end in `;`, then `Each is mutation-checked.`
- Rule: R2 (semicolons). R1 (passive with no actor). R3 ("mutation-checked" is never defined).
- Replace with: drop the trailing `;` from each list item, and replace the last line with `Each test fails when the change it covers is reverted.`

Compliant: Notes bullets 1 and 2, the static-hook paragraph, and Measured.

### PR 2 (updated, one sentence added)

**5.7 (low)** Problem
- Current: `A closed handle that nobody references is freed only by the cyclic garbage collector, which can't see the ~0.7 MiB of OpenSSL state each closed async handle holds.`
- Rule: R1.
- Replace with: `Only the cyclic garbage collector frees a closed handle that nobody references, and it can't see the ~0.7 MiB of OpenSSL state each closed async handle holds.`

**5.8** Problem
- Current: `After creating and terminating 100 sandboxes, 0.66 MiB per handle stays allocated as long as nothing else allocates.`
- Rule: R1 ("say who does what". The participle has no subject).
- Replace with: `After a program creates and terminates 100 sandboxes, 0.66 MiB per handle stays allocated as long as nothing else allocates.`

**5.9** The added paragraph
- Current: `Using a sandbox's connection after `close_connection()` or `terminate()` (for example through `sandbox.connector` or a `commands`/`files` object kept from before the close) is unsupported; after this change some connection modes raise or route without the pod IP instead of silently reconnecting, and a follow-up (#<issue 5>) will make the sync client refuse such use with a clear error, as the async client already does.`
- Rule: R2 (two parentheticals, a semicolon and three ideas in one sentence).
- Replace with: `Using a sandbox's connection after `close_connection()` or `terminate()` is unsupported. That includes calls through `sandbox.connector`, or through a `commands` or `files` object kept from before the close. After this change, some connection modes raise or route without the pod IP instead of silently reconnecting. A follow-up, #<issue 5>, will make the sync client refuse such use with a clear error, as the async client already does.`

**5.10** Alternatives considered
- Rule: R2.
- Replace with:
  - `Leaving it to the GC sets the peak by GC timing, and frees nothing if nothing else allocates.`
  - `Calling `gc.collect()` from the library causes global pauses.`
  - `With weak method references, a handle that only an executor still references would silently lose its callbacks.`
  - `Clearing inside the sync connector's `close()` breaks reconnects and snapshot resume.`

**5.11** Tests, first bullet
- Current: `Per client, with the GC disabled: a closed, dropped handle and its connector are freed. Parametrized over Direct, InCluster, SandboxdPodTunnel and SandboxdInCluster. Each cleared reference is mutation-checked separately.`
- Rule: R2 (colon, fragment). R1 (passives with no actor). R3 ("mutation-checked").
- Replace with: `For each client, with the GC disabled, reference counting frees a closed, dropped handle and its connector. The test covers the Direct, InCluster, SandboxdPodTunnel and SandboxdInCluster modes. Restoring any one cleared reference makes the test fail.`

Compliant: the Change bullets, "The fields were already optional." and Measured.

### PR 3

**5.12** Stacking line
- Current: `Stacked on #<PR 1>: its tests need the registry-bound terminate hook, so a client that still tracks handles can be freed.`
- Rule: R2 (fragment and colon). R1 ("can be freed"). R3 ("registry-bound terminate hook" is coined. The code calls it `_on_terminated`).
- Replace with: `This PR is stacked on #<PR 1>. Its tests free a client that still tracks handles, which works only with the registry-bound `_on_terminated` hook from #<PR 1>.`

**5.13** Problem, sentence 2
- Current: `A bound method keeps its object, so every client stays alive until exit, even after `close()`: 1.4 KB per unused and 9.6 KB per used async client, 11.4 KB per sync client (tracemalloc), and 200 of 200 closed clients alive after `gc.collect()`.`
- Rule: R2.
- Replace with: `A bound method keeps its object, so every client stays alive until exit, even after `close()`. tracemalloc measures 1.4 KB per unused async client, 9.6 KB per used async client and 11.4 KB per sync client. After `gc.collect()`, 200 of 200 closed clients are still alive.`

**5.14** Change, first paragraph
- Current: `Exit cleanup reads only the registry, the explicit-claims set and, for async, the injected ApiClient. Both cleanup methods become static and are registered bound to those objects with `functools.partial`, the same pattern as the terminate hook.`
- Rule: R3 ("the registry" is undefined in this text, and "terminate hook" is not the code's name). R1 ("are registered").
- Replace with: `Exit cleanup reads only the `_active_connection_sandboxes` registry, the `_explicit_claims` set and, for async, the injected ApiClient. Both cleanup methods become static, and each client registers its method bound to those objects with `functools.partial`, the same pattern as the `_on_terminated` hook.`

**5.15** Change, bullet 1
- Current: `Exit behavior is unchanged: closed, reused, failed-close and never-closed clients are all cleaned up as before.`
- Rule: R2. R1.
- Replace with: `Exit behavior is unchanged. Exit cleanup still covers closed, reused, failed-close and never-closed clients.`

**5.16** Change, bullet 2
- Current: `The ApiClient object itself is passed, so its settings are still read at exit.`
- Rule: R1.
- Replace with: `The client passes the ApiClient object itself, so exit cleanup still reads its current settings.`

**5.17** Change, bullet 3
- Current: `The registry and the set are never rebound after construction, which the hook relies on (commented at the assignments).`
- Rule: R1. R2 (parenthetical).
- Replace with: `The client never rebinds the registry or the set after construction, and the hook relies on that. A comment at the assignments says so.`

**5.18** Change, bullet 4
- Current: `..., so the rule lives in one place. Exit cleanup therefore no longer goes through a subclass's override of sync `delete_sandbox()`; no subclass in this repo overrides it.`
- Rule: R3 ("the rule"). R2 (semicolon).
- Replace with: `..., so both delete a tracked handle the same way. As a result, exit cleanup no longer goes through a subclass's override of sync `delete_sandbox()`. No subclass in this repo overrides it.`

**5.19** Remaining-cost line
- Current: `About 610 B per client remain (the atexit entry with an empty dict and set).`
- Rule: R2.
- Replace with: `About 610 B per client remain, which is the atexit entry with an empty dict and set.`

**5.20** Alternatives considered
- Rule: R2.
- Replace with:
  - `Unregistering in `close()` when nothing is tracked loses cleanup for clients reused after `close()`, unless a flag re-registers on every insert path.`
  - `A weak-reference hook loses cleanup for a client dropped while its sandboxes are in use.`
  - `Unregistering in sync `__exit__` breaks reuse after the `with` block.`
  - `Documenting "reuse one client" leaves the growth.`

**5.21** Tests, bullets 1 and 4
- Current: `Both clients: a closed and dropped client is freed by reference counting (GC disabled), with an empty registry and with tracked handles.` and `All mutation-checked; the real-process-exit tests pass unchanged.`
- Rule: R2. R1. R3 ("mutation-checked").
- Replace with: `For both clients, with the GC disabled, reference counting frees a closed and dropped client, both with an empty registry and with tracked handles.` and `Each test fails when the change it covers is reverted. The real-process-exit tests pass unchanged.`

Compliant: Tests bullets 2 and 3, and Measured. See also the advisory in section 4 (the "registers atexit.register" wording and the size mismatch with the commit).

---

## 6. Issues (issues-final.md, issue text only. The Verification blocks and the file preamble are excluded): 36 violations (A: 11, B: 8, 3: 6, 4: 4, 5: 7)

### Issue A

| # | Line | Current | Rule | Replacement |
| --- | --- | --- | --- | --- |
| 6.1 (low) | 30 | `I expected a terminated handle to be released by the client, as ...` | R1 | `I expected the client to release a terminated handle, as `client.delete_sandbox()` already does.` |
| 6.2 | 32 | `This causes **unbounded growth**: memory grows linearly ...` | R2 | `This causes **unbounded growth**. Memory grows linearly with the number of sandboxes terminated over the client's lifetime.` |
| 6.3 | 34 | `Measured (reproduction below, no cluster): 0.69 MiB of peak RSS per create/terminate cycle with the async client; after 200 cycles the client still holds 200 closed handles.` | R2 | `Measured with the reproduction below, without a cluster. The async client uses 0.69 MiB of peak RSS per create/terminate cycle, and after 200 cycles it still holds 200 closed handles.` |
| 6.4 | 35 | `Measured on a kind cluster (Python 3.12.15, glibc 2.41, 40 workers doing create / one command / terminate; results on the `bench/async-mem-probe` branch, ... and `results/g4/`):` | R2 | `Measured on a kind cluster with Python 3.12.15, glibc 2.41 and 40 workers, each doing create, one command and terminate. The results are in `test/benchmarks/batch-claim/results/memprobe/` and `results/g4/` on the `bench/async-mem-probe` branch.` |
| 6.5 | 36 | `2,400 cycles: RSS 93.8 → 1,752.4 MiB and still rising, 72.4 MiB per 100 cycles; glibc bytes in use 1,660 MiB at the end, about 0.68 MiB per cycle. Setting `MALLOC_ARENA_MAX=2` did not change the slope (94.5% of the default), so it is live memory, not fragmentation.` | R2 | `2,400 cycles: RSS rose from 93.8 to 1,752.4 MiB and was still rising, at 72.4 MiB per 100 cycles. glibc reported 1,660 MiB in use at the end, about 0.68 MiB per cycle. Setting `MALLOC_ARENA_MAX=2` left the slope at 94.5% of the default, so the growth is live memory, not fragmentation.` (The memray sentence stays as it is.) |
| 6.6 | 40 | `Inferred from the code, not measured: the async client's exit cleanup sends one DELETE per tracked key, so every terminated handle still tracked costs one redundant DELETE (answered with 404 and treated as success) at exit.` | R2, R1 | `Inferred from the code, not measured. The async client's exit cleanup sends one DELETE per tracked key, so every terminated handle still tracked costs one redundant DELETE at exit. The API server answers it with 404, which the client treats as success.` |
| 6.7 | 42 | `Workarounds: call ... which removes the entry; or call `client.list_active_sandboxes()` periodically, which drops closed handles (but see #<issue 3>: it also drops closed handles whose sandbox still exists).` | R2 | `There are two workarounds. Call `client.delete_sandbox(claim_name, namespace)` instead of `sandbox.terminate()`, which removes the entry. Or call `client.list_active_sandboxes()` periodically, which drops closed handles. That second workaround has its own problem, #<issue 3>, because it also drops closed handles whose sandbox still exists.` |
| 6.8 | 49 | `No cluster is needed: the three Kubernetes calls on this path (create claim, wait for ready, delete claim) are stubbed.` | R2, R1 | `No cluster is needed, because the script stubs the three Kubernetes calls on this path, which create the claim, wait for it to be ready and delete it.` |
| 6.9 | 84 | `Optional, real cluster: the shape of the loop that produced the measured cluster numbers above. It needs a cluster with the extensions installed and a `SandboxWarmPool` (here `python-sandbox-pool`); it was not re-run for this report.` | R2, R1 | `Optional, on a real cluster. This is the shape of the loop that produced the measured cluster numbers above. It needs a cluster with the extensions installed and a `SandboxWarmPool`, here `python-sandbox-pool`. I did not re-run it for this report.` |
| 6.10 | 109 | `Measured with this shape (40 workers, warm pool of 120): RSS grew about 72 MiB per 100 cycles on main (156 → 525 MiB over 600 cycles; 93.8 → 1,752.4 MiB over 2,400 cycles).` | R2 | `With this shape, 40 workers and a warm pool of 120, RSS grew about 72 MiB per 100 cycles on main. It went from 156 to 525 MiB over 600 cycles and from 93.8 to 1,752.4 MiB over 2,400 cycles.` |
| 6.11 | 114 | `The cluster numbers were measured with Python 3.12.15: the 2,400-cycle runs on the SDK at `7fd162d9` (same code on this path as main `840fdfde`), the 600-cycle and create-100 runs on main `7f40dc1a`.` | R2, R1 | `The cluster numbers come from Python 3.12.15. The 2,400-cycle runs used the SDK at `7fd162d9`, which has the same code on this path as main `840fdfde`. The 600-cycle and create-100 runs used main `7f40dc1a`.` |

Compliant: title, lines 26, 28, 37-39 (data), and 44.

### Issue B

| # | Line | Current | Rule | Replacement |
| --- | --- | --- | --- | --- |
| 6.12 (low) | 140 | `They were not freed until Python's cyclic garbage collector ran.` | R1 | `Python freed them only when its cyclic garbage collector ran.` |
| 6.13 | 142 | `Memory climbs to a GC-timed peak before it falls, ...` | R3 ("GC-timed" is coined. "GC" is not introduced in this paragraph, which says "cyclic collector") | `Memory climbs to a peak set by when the collector runs before it falls, ...` |
| 6.14 | 146 | `This causes **late freeing with high peaks**, not unbounded growth: the memory is eventually returned, but only when a collection happens to run.` | R2, R1 | `This causes **late freeing with high peaks**, not unbounded growth. Python returns the memory only when a collection happens to run.` |
| 6.15 | 148 | `Measured (reproduction below, no cluster): with the GC disabled, a closed, unreferenced `AsyncSandbox` is still alive; `gc.collect()` frees it.` | R2 | `Measured with the reproduction below, without a cluster. With the GC disabled, a closed, unreferenced `AsyncSandbox` is still alive, and `gc.collect()` frees it.` |
| 6.16 | 149 | `Measured on a kind cluster (Python 3.12.15, async client, with the fix for #<issue A> applied so the client no longer holds terminated handles; `bench/async-mem-probe` branch, `results/g3/`):` | R2 | `Measured on a kind cluster with Python 3.12.15 and the async client. The fix for #<issue A> was applied, so the client no longer holds terminated handles. The results are in `results/g3/` on the `bench/async-mem-probe` branch.` |
| 6.17 | 154 | `Not covered by breaking the cycle at close: handles dropped without being closed keep their live connector until the cyclic GC runs.` | R2 | `Breaking the cycle at close does not cover handles dropped without being closed. They keep their live connector until the cyclic GC runs.` (The last sentence stays as it is.) |
| 6.18 | 156 | `Related: #<issue A> (the client keeps every terminated handle), which causes the unbounded growth; this issue makes the remaining memory return late.` | R2 | `Related: #<issue A>, where the client keeps every terminated handle, causes the unbounded growth. This issue makes the remaining memory return late.` |
| 6.19 | 161 | `No cluster is needed; the Kubernetes calls are stubbed. `client.delete_sandbox()` is used so the client's own registry does not hold the handle (see #<issue A>).` | R2, R1 | `No cluster is needed, because the script stubs the Kubernetes calls. The script calls `client.delete_sandbox()` so that the client's own registry does not hold the handle (see #<issue A>).` |

Compliant: title, and lines 144 and 150-152 (data).

### Issue 3

| # | Line | Current | Rule | Replacement |
| --- | --- | --- | --- | --- |
| 6.20 | 225 | `... removes every tracked handle that is not active (the loop that pops `not obj.is_active`, in both ...). A handle becomes inactive when its connection is closed, which happens on `close_connection()`, on a failed `get_sandbox()` lookup (the client closes the handle locally and keeps tracking it so cleanup still reaches it). ... nothing deletes that claim: `delete_all()`, ...` | R2 (two parentheticals and a colon. The list "on X, on Y (...)" is also missing its conjunction) | `` `list_active_sandboxes()` removes every tracked handle that is not active. The loop that pops `not obj.is_active` does this in both `sandbox_client.py` and `async_sandbox_client.py`. A handle becomes inactive when its connection closes. That happens on `close_connection()` and on a failed `get_sandbox()` lookup, where the client closes the handle locally and keeps tracking it so cleanup still reaches it. The claim behind such a handle still exists in the cluster. Once `list_active_sandboxes()` has dropped it, nothing deletes that claim, because `delete_all()`, the context managers and the exit cleanup only walk the tracked handles.`` |
| 6.21 | 229 | `This causes **leaked cluster resources**: a program that closes a handle, lists, and exits leaves the SandboxClaim (and its sandbox pod) running. Measured with the stubbed reproduction below (the client never issues the DELETE); not run against a cluster.` | R2 | `This causes **leaked cluster resources**. A program that closes a handle, lists, and exits leaves the SandboxClaim and its sandbox pod running. I measured this with the stubbed reproduction below, where the client never issues the DELETE. I did not run it against a cluster.` |
| 6.22 | 231 | `Suggested direction: drop only handles whose claim was deleted (after a successful terminate) and keep reporting or tracking closed handles; or report only active handles without dropping the others.` | R2 | `One direction is to drop only handles whose claim a successful terminate deleted, and keep reporting or tracking closed handles. Another is to report only active handles without dropping the others.` |
| 6.23 | 233 | `Related: #<issue A> (the client keeps terminated handles; `list_active_sandboxes()` is one of its workarounds, which is how this path gets exercised).` | R2, R1 | `Related: #<issue A>, where the client keeps terminated handles. `list_active_sandboxes()` is one of its workarounds, which is how programs reach this path.` |
| 6.24 | 238 | `No cluster is needed: the Kubernetes calls are stubbed, and the delete stub records which claims the client asks to delete. The first run is the control without `list_active_sandboxes()`.` | R2, R1 | `No cluster is needed, because the script stubs the Kubernetes calls and its delete stub records which claims the client asks to delete. The first run is the control, without `list_active_sandboxes()`.` |
| 6.25 | 254 (repro script comment) | `# local close; the claim still exists` | R2, BS (comment chained with a semicolon) | `# Closes locally. The claim still exists.` |

Compliant: title, and lines 223 and 227.

### Issue 4

| # | Line | Current | Rule | Replacement |
| --- | --- | --- | --- | --- |
| 6.26 | 301 | `... when `cleanup=True`, which is its default; `SandboxClient` calls `atexit.register(self._delete_automatic_sandboxes)` when `cleanup=True` (default `False` there).` | R2 | `... when `cleanup=True`, which is its default. `SandboxClient` calls `atexit.register(self._delete_automatic_sandboxes)` when `cleanup=True`, which defaults to `False` there.` |
| 6.27 | 305 | `This causes **per-client retention**: memory grows with the number of clients created, not with sandboxes.` | R2 | `This causes **per-client retention**. Memory grows with the number of clients created, not with sandboxes.` |
| 6.28 | 308 | `Measured (Python 3.12.15, 200 clients per case, tracemalloc after `gc.collect()`; `bench/async-mem-probe` branch, `results/g4/client_retained.py`): async never used 1,369 B per client; async used then closed 9,600 B per client; sync `cleanup=True` 11,407 B per client.` | R2 | `Measured with Python 3.12.15, 200 clients per case, and tracemalloc after `gc.collect()`, using `results/g4/client_retained.py` on the `bench/async-mem-probe` branch. An async client that was never used keeps 1,369 B. An async client that was used and then closed keeps 9,600 B. A sync `cleanup=True` client keeps 11,407 B.` (The last sentence stays as it is.) |
| 6.29 | 310 | `Simply unregistering in `close()` is not enough: async `close()` keeps ..., a client can be used again after `close()`, and sync `__exit__` leaves the client usable. ... One way: register a function bound only to the data exit cleanup reads (the registry dict, the explicit-claims set and, for async, the injected `ApiClient`) instead of a bound method, as urllib3 does with ...` | R2, R1 | `Simply unregistering in `close()` is not enough, for three reasons. Async `close()` keeps handles whose close failed so a later call can retry. A caller can use a client again after `close()`. Sync `__exit__` leaves the client usable. Exit cleanup has to keep covering those cases and clients dropped without `close()`. One way is to register a function bound only to the data exit cleanup reads, instead of a bound method. That data is the registry dict, the explicit-claims set and, for async, the injected `ApiClient`. urllib3 does the same with `weakref.finalize(self, _close_pool_connections, pool)`.` |

Compliant: title, and lines 299, 303, 307 and 317.

### Issue 5

| # | Line | Current | Rule | Replacement |
| --- | --- | --- | --- | --- |
| 6.30 | 380 | `- a reset that the next request recovers from: `send_request` calls it on ..., strategies call ..., and the GKE snapshot extension calls ...;` | R2 | `- A reset that the next request recovers from. `send_request` calls it on port-forward and connection errors. Strategies call their own `close()` before restarting a port-forward. The GKE snapshot extension calls `connector.close()` on a live handle during suspend and resume.` (The line 379 lead-in colon before a list is fine.) |
| 6.31 | 383 | `` `AsyncSandboxConnector.close()` is final: later use raises. The difference follows the HTTP libraries (`requests.Session` keeps working after `close()`, `httpx` clients refuse requests after `close()`), not a deliberate sync/async design choice.`` | R2 | `` `AsyncSandboxConnector.close()` is final, and later use raises. The difference follows the HTTP libraries, not a deliberate sync/async design choice. `requests.Session` keeps working after `close()`, while `httpx` clients refuse requests after `close()`.`` |
| 6.32 | 385 | `I expected the same lifecycle on both: after the final close, using the connector raises a clear error.` | R2 | `I expected the same lifecycle on both. After the final close, using the connector should raise a clear error.` |
| 6.33 | 387 | `Code that keeps a sync connection after ... (through `sandbox.connector`, or a `commands`/`files` object obtained before the close) gets mode-dependent behavior instead of an error: depending on the connection mode it uses the pod IP, ... Inferred from the code, not measured: on a handle ...` | R2 | `Code that keeps a sync connection after `close_connection()` or `terminate()` gets mode-dependent behavior instead of an error. Such code holds `sandbox.connector`, or a `commands` or `files` object obtained before the close. Depending on the connection mode, it uses the pod IP, starts a new port-forward, or reconnects. Inferred from the code, not measured. On a handle that the client has already closed and stopped tracking, such a reconnect reopens a port-forward or session that nothing closes again.` |
| 6.34 | 389 | `Suggested direction: split a private reset ..., make the final close refuse later use as the async one does, and switch the internal callers (error recovery, snapshot suspend/resume) to the reset.` | R2 | `One direction is to split a private reset from the final `close()` in the sync connector and its strategies, and make the final close refuse later use as the async one does. The internal callers for error recovery and snapshot suspend and resume would then switch to the reset.` |
| 6.35 | 391 | `Related: #1785 (sync/async divergences in the Python SDK). #<issue B>: its fix drops a closed handle's callbacks, which changes ... but does not make close final.` | R2 | `Related: #1785, which tracks sync/async divergences in the Python SDK. The fix for #<issue B> drops a closed handle's callbacks. That changes what a reused sync connection does after close in some modes, but it does not make close final.` |
| 6.36 | 396 | `No cluster is needed: a local HTTP server stands in for the sandbox router, and the Kubernetes calls are stubbed.` | R2, R1 | `No cluster is needed. A local HTTP server stands in for the sandbox router, and the script stubs the Kubernetes calls.` |

Compliant: title, line 377, and the repro script comments.

---

## Counts

| Item | Violations |
| --- | --- |
| PR 1 code | 5 (1 low) |
| PR 2 code | 2 |
| PR 3 code | 5 |
| Commit messages | 8 (PR 1: 2, PR 2: 1 low, PR 3: 5) |
| PR texts | 21 (PR 1: 6, PR 2: 5 (1 low), PR 3: 10) |
| Issues | 36 (A: 11 (1 low), B: 8 (1 low), 3: 6, 4: 4, 5: 7) |

## Most important

1. The sentence added to the PR 2 text, "Using a sandbox's connection after ... is unsupported; ...", has two parentheticals, a semicolon and three ideas (5.9).
2. "the rule lives in one place" is an undefined term, in PR 3 code `sandbox_client.py:423`, the PR 3 commit and the PR 3 text (3.4, 4.8, 5.18).
3. "Static and bound ... with a partial ...; ... would bring that back" is a fragment, joins two ideas with a semicolon, uses shorthand and describes history. It appears in 4 code sites: the `_untrack` comment in PR 1, both files, and the atexit comment in PR 3, both files (1.5, 3.2).
4. "Exit behavior is unchanged: ... are all cleaned up ..., and the ApiClient object itself is passed so its settings are still read" is a colon chain of passives with no actor, in the PR 3 commit and the PR 3 text (4.6, 5.15, 5.16).
5. Issue 3 line 225 has stacked parentheticals and a colon, and its list "on X, on Y (...)" is missing a conjunction (6.20).

Plus an accuracy advisory outside the rules: the PR 3 commit and PR text say "registers atexit.register(...)", and their per-client sizes and units disagree (8 KiB / 9 KiB against 9.6 KB / 11.4 KB).
