"""B4 golden fixtures: where Python (rev 3.2: 4fa72e04; first 8a72827f) rejects a batch's shutdownTime.

claim_batch computes construct_sandbox_claim_lifecycle_spec(ceil(N/rps) + quorum + budget + 600)
(sandbox_batch.py:204-209) and acquire computes construct_sandbox_claim_lifecycle_spec(quorum + budget
+ 600) (:627-629). The bound is datetime's: now + seconds must stay <= 9999-12-31T23:59:59.999999 UTC
(utils.py:91-96 turns the OverflowError into ValueError), and ceil(N/rps) itself raises OverflowError
when N/rps is inf. `now` is frozen so the fixture is reproducible; the Go test passes the same instant.

Run from clients/python/agentic-sandbox-client of the TOP checkout:
    PYTHONPATH=$PWD python gen_shutdown_bound.py > shutdown_bound.json
"""
import json
import math
import sys
from datetime import datetime, timezone

from k8s_agent_sandbox import batch_utils, utils

FIXED = datetime(2026, 10, 20, 8, 0, 0, 123456, tzinfo=timezone.utc)


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED if tz is not None else FIXED.replace(tzinfo=None)


utils.datetime = _Frozen
MARGIN = batch_utils.BATCH_SHUTDOWN_MARGIN_SECONDS
MAX_UNIX = int(datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())
LIMIT = MAX_UNIX - int(FIXED.timestamp())  # whole seconds left before datetime.max


def run(fn):
    try:
        return {"ok": True, "shutdown_time": fn()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error_type": type(e).__name__, "error": str(e)}


def fill(n, rps, q, b):
    # The exact expression of sandbox_batch.py:204-209.
    return utils.construct_sandbox_claim_lifecycle_spec(math.ceil(n / rps) + q + b + MARGIN)["shutdownTime"]


def acquire(q, b):
    return utils.construct_sandbox_claim_lifecycle_spec(q + b + MARGIN)["shutdownTime"]


fill_cases = [
    (1, 50.0, 600, 3600),
    (3, 0.1, 1, 1),
    (0, 5e-324, 1, 1),            # all-size-0: ceil(0/rps) = 0
    (0, 1e-300, 600, 3600),
    (1, 1e-9, 600, 3600),         # ~32 years
    (1, 1e-10, 600, 3600),        # ~317 years: Python accepts; revision 2 Go rejected (B4)
    (1, 4e-12, 600, 3600),        # ~7922 years: still before 9999 -> Python accepts (B4)
    (1, 3e-12, 600, 3600),        # ~10563 years: past 9999 -> ValueError
    (7, 2.9e-11, 600, 3600),
    (1, 1e-300, 1, 1),            # timedelta OverflowError -> ValueError
    (1, 5e-324, 1, 1),            # 1/5e-324 = inf -> math.ceil raises OverflowError
    (LIMIT - 1 - 600 - 3600 - MARGIN, 1.0, 600, 3600),  # exactly one second inside the bound
    (LIMIT - 600 - 3600 - MARGIN, 1.0, 600, 3600),      # exactly at the bound (whole seconds)
    (LIMIT + 1 - 600 - 3600 - MARGIN, 1.0, 600, 3600),  # one second past it
]
acquire_cases = [
    (600, 3600),
    (1, LIMIT - 1 - MARGIN),
    (1, LIMIT - MARGIN),
    (1, LIMIT + 1 - MARGIN),
    (1, 9_000_000_000_000_000_000),  # fits int64; a Lease can carry it (Go parses it)
]

out = {
    "generator": {"python_commit": "4fa72e043e1c5f36273bb0d8e8a6b411e13db749", "python": sys.version.split()[0]},
    "now": FIXED.isoformat(),
    "now_unix": int(FIXED.timestamp()),
    "limit_seconds": LIMIT,
    "fill": [{"size": n, "create_rps": r, "quorum_timeout": q, "work_budget": b, "result": run(lambda: fill(n, r, q, b))}
             for n, r, q, b in fill_cases],
    "acquire": [{"quorum_timeout": q, "work_budget": b, "result": run(lambda: acquire(q, b))} for q, b in acquire_cases],
}
json.dump(out, sys.stdout, indent=1)
