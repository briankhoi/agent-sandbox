# Paste-ready replies for 15/N (#1990) (drafts, not posted)

## `tomergee` review (`5689391778`, items 1 to 4)

Combined reply for the review comment:

> Thanks for the thorough review.
>
> 1. Fixed in `17bb034f`. `_delete_released_create` (`sandbox_batch.py:1217-1233`, `async_sandbox_batch.py:1192-1210`) now checks `self._state.get_member(claim_name) is not None` under `self._lock` and returns without releasing or deleting the claim. `BatchState.get_member` (`batch_state.py:399-401`) already exposes `_members`, and any fill claim still in `_members` after `release_not_ready()` arrived on the watch beforehand and `release_not_ready()` kept it (`upsert_claim` releases late arrivals without adding them to `_members`, and `release_group` pops all fill members of the group). Added `test_release_not_ready_keeps_handed_out_member_when_create_retries_late` in both `test_sandbox_batch.py` and `test_async_sandbox_batch.py`.
> 2. Fixed in `17bb034f`. Factored `_check_lease_held_locked` and `_check_healthy` (`sandbox_batch.py:428-443`, `async_sandbox_batch.py:444-459`) so `connect`, `release_member`, `release_not_ready`, and `release_group` check `_detached`, `_lease_released`, and `self._state.error()` under the batch lock before dropping planned creates or mutating `BatchState`, while `_delete_claim` and `release_group`'s `hold=` still run `_check_lease_held` before each delete attempt and `deletecollection` round. Extended `test_release_methods_delete_nothing_once_err_is_set` in both unit test files to assert `members()` stays unchanged and `__cause__` chains the stopping error.
> 3. Keeping `"was never handed out"` so the public docstrings stay in caller terms rather than exposing the internal event queue. `member.state is not MemberState.READY` already keeps any Ready member, and keeping a member whose `MEMBER_READY` event sits in the `events()` queue avoids deleting a member that `events()` has already committed to yielding even if it turned not Ready afterwards.
> 4. Checked the rest of the repository for `deletecollection` and `sandboxclaims` RBAC rules. `clients/python/agentic-sandbox-client/README.md:818` is the only batch driver `Role` example in the repo.

## CodeRabbit thread 1 (`4340393709`, `async_sandbox_batch.py:1201`, `_delete_released_create` deletes a kept member)

> Fixed in `17bb034f`. `_delete_released_create` in both `sandbox_batch.py:1224-1227` and `async_sandbox_batch.py:1199-1202` now checks `self._state.get_member(claim_name) is not None` under `self._lock` and returns without releasing or deleting the claim, with regression tests in `test_sandbox_batch.py` and `test_async_sandbox_batch.py`.

## CodeRabbit thread 2 (`4340393744`, `async_sandbox_client.py:242`, keep unreleased batches after `close()`)

> Outside this PR's delta (from #1824, #1986, and #2049), and `close()` intentionally leaves batches in place. `close()` shuts the client down, and its docstring (`async_sandbox_client.py:231-232`) states that it stops batch Lease renewal without releasing anything. It treats single sandboxes the same way: closing a sandbox connection removes it from the registry so `atexit` does not delete it either. Callers that want batches deleted use `async with`, `delete_all()`, or `batch.release()` before `close()`, and claims left behind still end at their `shutdownTime`.

## CodeRabbit thread 3 (`4340393748`, `async_sandbox_client.py:615`, register attached batch without cancellation point)

> Outside this PR's delta, and #2049 (`74cd8fa5`) already fixed this upstream (now `async_sandbox_client.py:642-648`). `_register_batch` is synchronous and takes no lock, and `get_batch` calls it right after `_attach` returns with no `await` in between, matching `claim_batch`.

## CodeRabbit thread 4 (`4340393759`, `test_batch_state.py:15`, module docstring)

> Outside this PR's delta (`test_batch_state.py` landed in #1742, `3a1607bc`). `pyproject.toml:29-31` excludes `k8s_agent_sandbox.test*` from the published package, and omitting module docstrings in unit test modules matches the rest of `k8s_agent_sandbox/test/unit/` (such as `test_sandbox.py`, `test_sandboxclient.py`, and `test_utils.py`).

## CodeRabbit thread 5 (`4340393769`, `test_batch_utils.py:15`, module docstring)

> Outside this PR's delta (`test_batch_utils.py` landed in #1742, `3a1607bc`). `pyproject.toml:29-31` excludes `k8s_agent_sandbox.test*` from the published package, and omitting module docstrings in unit test modules matches the rest of `k8s_agent_sandbox/test/unit/`.

## CodeRabbit thread 6 (`4348297847`, `k8s_helper.py:71`, keep Lease credentials current after creation)

> Outside this PR's delta (`_api_client_without_retries` landed in #1925, `31a47728`, and #1985, `d07e8941`, at `k8s_helper.py:53-72`). #1985 builds `no_retries` from `copy.copy(api_client.configuration)` (`k8s_helper.py:65`), so the Lease client shares `api_key`, `api_key_prefix`, and `refresh_api_key_hook` with the injected `Configuration`, which `kubernetes.client` reads on each request when preparing and refreshing bearer tokens.
