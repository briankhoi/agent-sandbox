"""Tiny BFS model of learnings CANCEL-5: acquire()'s cancel handler awaits _changed before
drop_acquired. Checks three variants: today (no task awaits while holding the lock), the
regression (one does), and the hardening (apply the state transition first). Run it with
`python3 verify_cancel5_exercise.py`; the regression fails in 3 steps."""

from collections import deque
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class S:
    lock: str | None = None  # who holds _changed's lock
    acquiring: bool = True  # claim name still in _acquiring / _acquire_waiters
    a: str = "WAITING"  # acquire task: WAITING -> HANDLER_WANT_LOCK -> DONE | ESCAPED
    cancels: int = 0  # cancels delivered to A so far (environment bound: 2)
    b: str = "IDLE"  # some other task that touches _changed


def transitions(s, await_under_lock, transition_first):
    # Environment: deliver a cancel to A at any await (bounded at 2).
    if s.cancels < 2 and s.a in ("WAITING", "HANDLER_WANT_LOCK"):
        if s.a == "WAITING":
            if transition_first:
                # Hardened: core transition before any await, then a best-effort wakeup.
                yield (
                    "A:cancel1_transition_first",
                    replace(s, cancels=s.cancels + 1, acquiring=False, a="DONE"),
                )
            elif s.lock is None:
                # Lock free: acquire takes the no-yield fast path, so the whole handler is one step.
                yield (
                    "A:cancel1_handler_runs_atomically",
                    replace(s, cancels=s.cancels + 1, acquiring=False, a="DONE"),
                )
            else:
                yield (
                    "A:cancel1_handler_blocks_on_lock",
                    replace(s, cancels=s.cancels + 1, a="HANDLER_WANT_LOCK"),
                )
        elif s.lock is not None:
            # Second cancel can land only if the acquire really suspends (someone holds the lock).
            # A free asyncio.Lock is taken on the no-yield fast path, so no cancel can land there.
            yield "A:cancel2_escapes", replace(s, cancels=s.cancels + 1, a="ESCAPED")
    if s.a == "HANDLER_WANT_LOCK" and s.lock is None:
        # Acquire + drop_acquired + notify_all is await-free once the lock is ours: one atomic step.
        yield "A:lock_drop_notify", replace(s, acquiring=False, a="DONE")
    # Task B: with await_under_lock it holds the lock across a suspension point.
    if s.b == "IDLE" and s.lock is None:
        if await_under_lock:
            yield "B:lock_then_await", replace(s, lock="B", b="HOLDING")
        else:
            yield "B:lock_work_unlock", replace(s, b="DONE")
    if s.b == "HOLDING":
        yield "B:resume_unlock", replace(s, lock=None, b="DONE")


def check(await_under_lock, transition_first=False):
    init = S()
    seen = {init: None}
    label = {}
    q = deque([init])
    while q:
        s = q.popleft()
        if s.a in ("DONE", "ESCAPED") and s.acquiring:
            trace, cur = [], s
            while seen[cur] is not None:
                trace.append(label[cur])
                cur = seen[cur]
            return len(seen), list(reversed(trace))
        for lbl, n in transitions(s, await_under_lock, transition_first):
            if n not in seen:
                seen[n] = s
                label[n] = lbl
                q.append(n)
    return len(seen), None


if __name__ == "__main__":
    today_states, today_trace = check(False)
    reg_states, reg_trace = check(True)
    hard_states, hard_trace = check(True, transition_first=True)
    print("today (no await under lock):", (today_states, today_trace))
    print("regression (await under lock):", (reg_states, reg_trace))
    print("hardened (transition first) + await under lock:", (hard_states, hard_trace))
    assert today_trace is None, (
        f"Expected 0 leaks when no task awaits under lock, got {today_trace}"
    )
    assert reg_trace is not None, "Expected negative-control regression (await under lock) to leak"
    assert hard_trace is None, (
        f"Expected hardened transition-first to have 0 leaks, got {hard_trace}"
    )
