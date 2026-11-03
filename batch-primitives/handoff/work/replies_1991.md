# Paste-ready replies for 16/N (#1991) (drafts, not posted)

> Note: Commit hashes below (`9ba5a6c6` and `810d3773`) match the synced live `16/N` branch (`feat/batch-6-acquire-nbbp5j` `692d7229` on `113fae28`).

## `tomergee` review (`5689392219`, items 1 to 7)

Combined reply for the review comment:

> Thanks for the thorough review and for catching the Python 3.14 `_callbacks` failure!
>
> 1. Fixed in `810d3773` (`test_async_sandbox_batch.py:2920-2924`, `:2963-2964`). Replaced both `Future._callbacks` checks with `warmpool in batch._prechecks` / `"pool-b" in checked`, `await asyncio.sleep(0)` so the concurrent tasks reach `await asyncio.wait_for(asyncio.shield(check), ...)`, and `not task.done()` assertions.
> 2. Fixed in `810d3773` (`sandbox_batch.py:799-809`, `async_sandbox_batch.py:805-817`, `test_sandbox_batch.py:3025-3081`, `test_async_sandbox_batch.py:2974-3027`). In both sync and async `_check_pool`, `except TimeoutError:` only sees `check.cancelled()` when the waiting `acquire()`'s own deadline has already expired right as the checking `acquire()` cancelled `check`, so both now check `if not check.done() or check.cancelled(): raise BatchTimeoutError(...) from None` (avoiding both a leaked `CancelledError` in sync and starting a fresh precheck past `deadline`), and `_check_pool` also checks `time.monotonic() >= deadline` at the top of the loop.
> 3. Fixed in `810d3773` (`sandbox_batch.py:718-755`, `async_sandbox_batch.py:724-761`, `test_sandbox_batch.py:3217-3240`, `test_async_sandbox_batch.py:3166-3191`). Initialized `may_exist = False` before the pacing wait and flipped it via `on_send` right before each `create_sandbox_claim` attempt (keeping `may_exist = sent > 1 or _is_transient_error(e)` if a retry fails after an earlier attempt was sent), so stopping or interrupting `acquire()` during pacing sends neither create nor delete.
> 4. Fixed in `9ba5a6c6` (`exceptions.py:155-156`, `test_batch_state.py:923-939`). Built the `TerminalMemberError` detail from whichever of `reason` and `message` are non-empty.
> 5. Fixed in `810d3773` (`clients/python/agentic-sandbox-client/README.md:783-785`). Aligned the README's failed-`acquire()` cleanup wording with `acquire()`'s docstring.
> 6. Fixed in `810d3773` (`sandbox_batch.py:709, 725, 796`, `async_sandbox_batch.py:715, 731, 802`, `test_sandbox_batch.py:3199-3206, 3237-3240`, `test_async_sandbox_batch.py:3148-3155, 3188-3191`). Added `self._check_healthy()` at the top of `_check_pool` so detached, released, or `err()`-stopped handles raise `BatchError` before reading warmpool or template objects, and deduplicated `acquire()`'s health checks into `self._check_healthy()`.
> 7. Kept as-is. `get_batch()` cannot re-attach after a crash without `detach()`: `_attach` (`sandbox_batch.py:364-377`, `async_sandbox_batch.py:360-373`) raises `BatchInUseError` while `lease.spec.holder_identity` is set on a live Lease and `BatchLeaseExpiredError` once the Lease goes stale (`async_sandbox_client.py:231-232, 614`). Only `detach()` clears `holder_identity`, and it writes `BATCH_NEXT_ORDINAL_ANNOTATION` in the same `replace_batch_lease` call (`sandbox_batch.py:1187-1208`, `async_sandbox_batch.py:1170-1185`), so a re-attached handle always reads `next_ordinal` from the Lease.

## CodeRabbit thread 1 (`4331503515`, `async_sandbox_batch.py:728`, `acquire()` after `close()` + `may_exist`)

> `AsyncSandboxClient._stop_batches` (`async_sandbox_client.py:257-260`) already marks the handle detached, sets `_stop_creating`, and wakes all waiters before stopping background tasks, so `_wait_acquired` raises `BatchError` at once when `close()` runs. Fixed the `may_exist` initialization in `810d3773` (`sandbox_batch.py:718-755`, `async_sandbox_batch.py:724-761`) so `may_exist` starts as `False` and flips to `True` via `on_send` right before `create_sandbox_claim` is sent.

## CodeRabbit thread 2 (`4331503521`, `exceptions.py:155`, `TerminalMemberError` detail formatting)

> Fixed in `9ba5a6c6` (`exceptions.py:155-156`, covered in `test_batch_state.py:923-939`). `TerminalMemberError` now joins only the non-empty parts of `member.reason` and `member.message` and omits the parenthetical when both are empty.

## CodeRabbit thread 3 (`4331503539`, `docs/python_sdk_reference.md` / `README.md`, failed-`acquire()` cleanup wording)

> `acquire()`'s docstring and generated `docs/python_sdk_reference.md:1003-1006` already state when the claim is deleted and how `release_not_ready()` and `release()` retry a failed delete; aligned `clients/python/agentic-sandbox-client/README.md:783-785` to match in `810d3773`.

## `vicentefb` comment on #1990 (`6591402900`, undeclared pools in `release_group()`)

> In `16/N` (#1991), `acquire()` stamps `BATCH_GROUP_LABEL` on every acquired claim even when its warm pool isn't one of `claim_batch()`'s declared groups (`sandbox_batch.py:745`, `async_sandbox_batch.py:751`), so `release_group(warmpool)` releases acquired members from undeclared pools too (`sandbox_batch.py:910-913`, `test_sandbox_batch.py:2929-2930`). Checking this also caught a retry edge case where a second `release_group(warmpool)` call after a failed `deletecollection` on an undeclared pool raised `ValueError` once the first call dropped its members; `BatchState.has_pool` (`batch_state.py:391` in `9ba5a6c6`) now checks `_fill_stopped_pools` as well so retrying `release_group(warmpool)` works for undeclared pools too.
