"""Generates golden parity fixtures for the Go batch port from the Python SDK (rev 3.2: re-generated at 4fa72e04,
the end of the fork stack; first generated at 8a72827f).

Run from clients/python/agentic-sandbox-client of the TOP checkout:
    python gen_fixtures.py > batch_golden.json
Every value is computed by calling the Python functions; nothing is hand-written except inputs.
"""
import json
import math
import sys
from datetime import UTC, datetime, timedelta

import kubernetes
import pydantic

from k8s_agent_sandbox import batch_state, batch_utils
from k8s_agent_sandbox.models import BatchGroup


def err_or(fn):
    try:
        return {"ok": True, "value": fn()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error_type": type(e).__name__, "error": str(e)}


out = {
    "generator": {
        "python_commit": "4fa72e043e1c5f36273bb0d8e8a6b411e13db749",
        "python": sys.version.split()[0],
        "kubernetes": kubernetes.__version__,
        "pydantic": pydantic.VERSION,
    }
}

# ---- fill plan -------------------------------------------------------------
plan_inputs = [
    ("b", [("p4", 4, 2), ("p2", 2, 1), ("p0", 0, 0), ("p3", 3, 3)]),
    ("b", [("a", 1, 1)]),
    ("bx", [("a", 5, 0), ("b", 5, 5)]),
    ("bx", [("a", 3, None), ("b", 7, 2), ("c", 2, 1)]),
    ("q", [("a", 6, 3), ("b", 4, 2)]),          # equal fractions -> ties broken by group index then k
    ("q", [("a", 0, 0)]),
    ("big", [("x", 10, 7), ("y", 3, 1), ("z", 1, 0)]),
]
fill_plan = []
for bid, gs in plan_inputs:
    groups = [BatchGroup(warmpool=p, size=s, min_ready=m) for p, s, m in gs]
    plan = batch_utils.fill_create_plan(bid, groups)
    fill_plan.append({
        "batch_id": bid,
        "groups": [{"warmpool": g.warmpool, "size": g.size, "min_ready": g.min_ready} for g in groups],
        "min_ready_given": [m is not None for _, _, m in gs],
        "plan": [[name, g.warmpool] for name, g in plan],
    })
out["fill_plan"] = fill_plan

# ---- group label hash --------------------------------------------------------
pools = ["python-sandbox-pool", "a", "", "pool-with-a-very-long-name-" + "x" * 80, "ünïcødé-pool", "UPPER", "p0"]
out["group_label"] = [{"warmpool": p, "value": batch_utils.group_label_value(p),
                       "selector": batch_utils.group_label_selector("bid", p)} for p in pools]

# ---- claim_batch validation + Lease encode ----------------------------------
def args_case(name, groups, labels=None, batch_id="bfixture", create_rps=None, max_in_flight=None,
              work_budget=None, quorum_timeout=None, lease_duration=None):
    def run():
        gs = [BatchGroup(**g) if isinstance(g, dict) else g for g in groups]
        a = batch_utils.validate_claim_batch_args(gs, labels, batch_id, create_rps, max_in_flight,
                                                  work_budget, quorum_timeout, lease_duration)
        lbl, ann = batch_utils.batch_lease_metadata(a)
        return {"create_rps": a.create_rps, "max_in_flight": a.max_in_flight, "work_budget": a.work_budget,
                "quorum_timeout": a.quorum_timeout, "lease_duration": a.lease_duration,
                "lease_labels": lbl, "lease_annotations": ann}
    return {"name": name,
            "input": {"groups": groups if all(isinstance(g, dict) for g in groups) else "<non-BatchGroup>",
                      "labels": labels, "batch_id": batch_id, "create_rps": create_rps,
                      "max_in_flight": max_in_flight, "work_budget": work_budget,
                      "quorum_timeout": quorum_timeout, "lease_duration": lease_duration},
            "result": err_or(run)}

G = [{"warmpool": "a", "size": 2}]
cases = [
    args_case("defaults", G),
    args_case("labels", G, labels={"team": "x", "example.com/app": "y", "empty": ""}),
    args_case("reserved batch-id label", G, labels={"agents.x-k8s.io/batch-id": "x"}),
    args_case("reserved batch-group label", G, labels={"agents.x-k8s.io/batch-group": "x"}),
    args_case("created-by label allowed by validate (overwritten on Lease)", G, labels={"agents.x-k8s.io/created-by": "me"}),
    args_case("empty groups", []),
    args_case("duplicate pool", [{"warmpool": "a", "size": 1}, {"warmpool": "a", "size": 2}]),
    args_case("bad batch id", G, batch_id="1abc"),
    args_case("batch id 52", G, batch_id="a" * 52),
    args_case("batch id 53", G, batch_id="a" * 53),
    args_case("lease 5", G, lease_duration=5),
    args_case("lease 6", G, lease_duration=6),
    args_case("lease float", G, lease_duration=6.0),
    args_case("work budget float", G, work_budget=1.5),
    args_case("quorum 0", G, quorum_timeout=0),
    args_case("max_in_flight 0", G, max_in_flight=0),
    args_case("rps 0", G, create_rps=0),
    args_case("rps -1", G, create_rps=-1),
    args_case("rps inf", G, create_rps=math.inf),
    args_case("rps nan", G, create_rps=math.nan),
    args_case("rps bool", G, create_rps=True),
    # re-review F5: an all-size-0 (acquire-only) batch is valid, and with a tiny rps nothing overflows in Python
    args_case("all groups size 0", [{"warmpool": "a", "size": 0}, {"warmpool": "b", "size": 0, "min_ready": 0}]),
    args_case("all groups size 0, rps 5e-324", [{"warmpool": "a", "size": 0}], create_rps=5e-324),
]
for v in [50, 1e6, 1234567, 33.333333333, 0.5, 1 / 3, 100.0, 1e-5, 0.0001, 12345.6789, 1e16, 123456,
          999999.5, 9999995, 0.1, 2.5e-7, 7, 1e21, 1e-300, 5e-324]:
    cases.append(args_case(f"rps {v!r}", G, create_rps=v))
out["claim_batch_args"] = cases

# Python accepts these label strings; the server may not. The Go port should use apimachinery's
# validators, which match the apiserver. Record Python's verdicts so the divergence is explicit.
label_cases = ["a", "A", "a_b", "-a", "a-", "a.b", "x" * 63, "x" * 64, "abc\n", "a b"]
key_cases = ["a/b", "Example.com/a", "a..b/c", "a.-b/c", "/a", "a/", "x" * 254 + "/a", "a/b/c"]
out["label_validation"] = (
    [{"kind": "value", "s": s, **err_or(lambda s=s: batch_utils.validate_labels({"k": s}))} for s in label_cases]
    + [{"kind": "key", "s": s, **err_or(lambda s=s: batch_utils.validate_labels({s: "v"}))} for s in key_cases]
)

# ---- Lease decode -------------------------------------------------------------
A = {
    "agents.x-k8s.io/batch-lease-duration": "60",
    "agents.x-k8s.io/batch-work-budget": "3600",
    "agents.x-k8s.io/batch-quorum-timeout": "600",
    "agents.x-k8s.io/batch-create-rps": "50",
}
def dec(name, labels, annotations):
    def run():
        s = batch_utils.parse_lease_metadata(labels, annotations)
        return s._asdict()
    return {"name": name, "labels": labels, "annotations": annotations, "result": err_or(run)}

decode = [
    dec("full", {"team": "x", "agents.x-k8s.io/batch-id": "b", "agents.x-k8s.io/created-by": "python-client"}, A),
    dec("go-written", {"team": "x", "agents.x-k8s.io/batch-id": "b", "agents.x-k8s.io/created-by": "go-client"},
        {**A, "agents.x-k8s.io/batch-next-ordinal": "17"}),
    dec("none", None, None),
    dec("empty", {}, {}),
]
for key, vals in {
    "agents.x-k8s.io/batch-create-rps": ["1e+06", "1.23457e+06", "33.3333", "0", "-1", "nan", "inf", "Infinity",
                                          "abc", " 50 ", "1_0", "0x10", "1e400", "5e-324", "", ".5", "5.",
                                          # re-review F6 (hex floats: Go ParseFloat accepts, Python rejects) and F5 (tiny rps)
                                          "0x1p4", "0x1.8p1", "0X1P4", "-0x1p-2", "1e1_0", "1e-10", "1e-300", "+.5"],
    "agents.x-k8s.io/batch-lease-duration": ["5", "6", "abc", "6.0", " 7 ", "+8", "1_0", "-6", ""],
    "agents.x-k8s.io/batch-work-budget": ["0", "1", "1.5", " 2", "٣", "99999999999999999999"],
    "agents.x-k8s.io/batch-quorum-timeout": ["0", "1", "x"],
    "agents.x-k8s.io/batch-next-ordinal": ["0", "-1", "12", "1e3"],
}.items():
    for v in vals:
        decode.append(dec(f"{key.split('/')[1]}={v!r}", {}, {**A, key: v}))
out["lease_decode"] = decode

# ---- deriveMember ---------------------------------------------------------------
def claim(name="b-0", pool="p", conds=None, sandbox=None, deleting=False, annotations=None, gen=None, obs=None):
    md = {"name": name}
    if annotations is not None:
        md["annotations"] = annotations
    if deleting:
        md["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    if gen is not None:
        md["generation"] = gen
    c = {"metadata": md, "spec": {"warmPoolRef": {"name": pool}}}
    status = {}
    if conds is not None:
        status["conditions"] = conds
    if sandbox is not None:
        status["sandbox"] = sandbox
    if status:
        c["status"] = status
    return c

def cond(status, reason="R", message="m", typ="Ready", obs=None):
    c = {"type": typ, "status": status, "reason": reason, "message": message,
         "lastTransitionTime": "2026-01-01T00:00:00Z"}
    if obs is not None:
        c["observedGeneration"] = obs
    return c

SB = {"name": "sb-1", "podIPs": ["10.0.0.1", "fd00::1"], "serviceFQDN": "sb-1.ns.svc.cluster.local"}
dm_inputs = {
    "empty": {},
    "no status": claim(),
    "ready": claim(conds=[cond("True", "Ready")], sandbox=SB),
    "ready no sandbox name": claim(conds=[cond("True", "Ready")], sandbox={"podIPs": ["10.0.0.1"]}),
    "ready empty sandbox name": claim(conds=[cond("True", "Ready")], sandbox={"name": ""}),
    "ready lowercase true": claim(conds=[cond("true", "Ready")], sandbox=SB),
    "unknown": claim(conds=[cond("Unknown", "SandboxNotReady")], sandbox=SB),
    "not ready transient": claim(conds=[cond("False", "SandboxNotReady")], sandbox=SB),
    "TemplateNotFound": claim(conds=[cond("False", "TemplateNotFound")]),
    "WarmPoolNotFound": claim(conds=[cond("False", "WarmPoolNotFound")]),
    "stale observedGeneration ready": claim(conds=[cond("True", "Ready", obs=1)], sandbox=SB, gen=2),
    "ready podIPs null": claim(conds=[cond("True", "Ready")], sandbox={"name": "sb", "podIPs": None}),
    "two Ready conditions": claim(conds=[cond("False", "PodFailed"), cond("True", "Ready")], sandbox=SB),
    "other condition only": claim(conds=[cond("True", "x", typ="Other")], sandbox=SB),
    "deleting ready": claim(conds=[cond("True", "Ready")], sandbox=SB, deleting=True),
    "deleting failed": claim(conds=[cond("False", "PodFailed")], sandbox=SB, deleting=True),
    "deleting pending": claim(deleting=True),
}
for r in sorted(batch_state.TERMINAL_CLAIM_READY_REASONS if hasattr(batch_state, "TERMINAL_CLAIM_READY_REASONS") else []):
    dm_inputs[f"terminal {r}"] = claim(conds=[cond("False", r)], sandbox=SB)
from k8s_agent_sandbox.constants import TERMINAL_CLAIM_READY_REASONS
for r in sorted(TERMINAL_CLAIM_READY_REASONS):
    dm_inputs[f"terminal {r}"] = claim(conds=[cond("False", r)], sandbox=SB)
    dm_inputs[f"terminal {r} but status True"] = claim(conds=[cond("True", r)], sandbox=SB)
out["terminal_reasons"] = sorted(TERMINAL_CLAIM_READY_REASONS)

def member_json(m):
    return {"claim_name": m.claim_name, "sandbox_name": m.sandbox_name or "", "warmpool": m.warmpool,
            "pod_ips": list(m.pod_ips), "service_fqdn": m.service_fqdn or "", "state": m.state.value,
            "reason": m.reason or "", "message": m.message or ""}

out["derive_member"] = [{"name": k, "claim": v, "member": member_json(batch_state.derive_member(v))}
                        for k, v in dm_inputs.items()]

# ---- reconstructGroups -------------------------------------------------------------
def fc(name, pool, size=None, mr=None):
    ann = {}
    if size is not None:
        ann["agents.x-k8s.io/batch-group-size"] = size
    if mr is not None:
        ann["agents.x-k8s.io/batch-group-min-ready"] = mr
    return claim(name=name, pool=pool, annotations=ann)

rg_inputs = {
    "order by lowest ordinal": ("b", [fc("b-5", "z", "2", "1"), fc("b-0", "y", "3", "3"), fc("b-6", "z", "2", "1"),
                                      fc("b-1", "y", "3", "3")]),
    "acquire-only pool -> size 0": ("b", [fc("b-0", "a", "1", "1"), fc("b-9", "acq")]),
    "pool with no ordinal claims sorts last, then by name": ("b", [fc("weird", "w2"), fc("odd", "w1"), fc("b-3", "a", "1", "0")]),
    "only one annotation": ("b", [fc("b-0", "a", "1", None)]),
    "conflicting": ("b", [fc("b-0", "a", "2", "1"), fc("b-1", "a", "2", "2")]),
    "non-integer": ("b", [fc("b-0", "a", "x", "1")]),
    "min_ready > size": ("b", [fc("b-0", "a", "1", "2")]),
    "negative size": ("b", [fc("b-0", "a", "-1", "0")]),
    "same pair different string form": ("b", [fc("b-0", "a", "2", "1"), fc("b-1", "a", "02", "1")]),
    "whitespace int": ("b", [fc("b-0", "a", " 2", "1")]),
    "fill claims missing (reattach)": ("b", [fc("b-2", "a", "4", "2")]),
    "empty": ("b", []),
    "ordinal prefix mismatch": ("b", [fc("bb-0", "x", "1", "1"), fc("b-1", "y", "1", "1")]),
    "leading zero ordinal": ("b", [fc("b-007", "x", "1", "1"), fc("b-3", "y", "1", "1")]),
}
out["reconstruct_groups"] = [
    {"name": k, "batch_id": bid, "claims": cl,
     "result": err_or(lambda bid=bid, cl=cl: [{"warmpool": g.warmpool, "size": g.size, "min_ready": g.min_ready}
                                              for g in batch_state.reconstruct_groups(bid, cl)])}
    for k, (bid, cl) in rg_inputs.items()
]

# ---- ordinals, ids, staleness, retry ----------------------------------------------
out["parse_ordinal"] = [{"batch_id": b, "name": n, "ordinal": batch_state.parse_ordinal(b, n)}
                        for b, n in [("b", "b-0"), ("b", "b-12"), ("b", "b-"), ("b", "b-x"), ("b", "bb-1"),
                                     ("b-1", "b-1-2"), ("b", "b-1-2"), ("b", "b-007"), ("b", "b- 1"), ("b", "b-+1")]]
out["validate_batch_id"] = [{"id": i, **err_or(lambda i=i: batch_utils.validate_batch_id(i))}
                            for i in ["a", "a1", "a-b", "a-", "-a", "1a", "A", "a_b", "a" * 52, "a" * 53, "", "a\n"]]
base = datetime(2026, 1, 1, tzinfo=UTC)
out["is_lease_stale"] = [{"renew": base.isoformat(), "duration": d, "now": (base + timedelta(seconds=s)).isoformat(),
                          "stale": batch_utils.is_lease_stale(base, d, base + timedelta(seconds=s))}
                         for d, s in [(60, 54), (60, 55), (60, 55.000001), (60, 56), (6, 1), (6, 1.5), (60, -10)]]
out["create_error_outcome"] = [{"status": st, "attempt": a, "outcome": batch_utils.create_error_outcome(st, a).value}
                               for st in [None, 409, 429, 500, 503, 504, 599, 600, 400, 403, 404, 410, 422]
                               for a in [1, 2, 3]]
out["is_retryable_status"] = [{"status": s, "retryable": batch_utils.is_retryable_status(s)}
                              for s in [None, 200, 409, 410, 429, 499, 500, 503, 599, 600]]

class _E(Exception):
    def __init__(self, h):
        self.headers = h
ra = []
# rev 3.2: whether a header was honoured is observed, not re-derived. retry_delay falls back to the
# module-level backoff_delay, so a sentinel from a stubbed backoff_delay means "not honoured". (The old
# `h.strip().isdigit()` re-implemented Python's rule before 924e8016 and mislabelled "٣" and "²" once
# Python started requiring ASCII digits.)
_NOT_HONOURED = -1.0
_real_backoff_delay = batch_utils.backoff_delay
batch_utils.backoff_delay = lambda *a, **k: _NOT_HONOURED
try:
    for h in ["5", " 7 ", "0", "100", "30", "31", "1.5", "-1", "+3", "Wed, 21 Oct 2015 07:28:00 GMT", "", "٣", "²"]:
        try:
            d = batch_utils.retry_delay(1, _E({"Retry-After": h}))
            used = d != _NOT_HONOURED
            ra.append({"header": h, "honoured": used, "delay": d if used else None})
        except Exception as e:  # noqa: BLE001
            ra.append({"header": h, "honoured": False, "delay": None, "python_raises": f"{type(e).__name__}: {e}"})
finally:
    batch_utils.backoff_delay = _real_backoff_delay
out["retry_after"] = ra
out["backoff"] = [{"attempt": a, "rand": r, "delay": batch_utils.backoff_delay(a, 0.5, 30.0, lambda r=r: r)}
                  for a in [1, 2, 3, 6, 7, 8, 50, 10**6] for r in [0.0, 0.5, 0.999999]]

# ---- shutdownTime seconds ------------------------------------------------------------
def fill_secs(size, rps, q, b):
    return math.ceil(size / rps) + q + b + batch_utils.BATCH_SHUTDOWN_MARGIN_SECONDS
out["shutdown_seconds"] = [{"size": n, "create_rps": r, "quorum_timeout": q, "work_budget": b,
                            "fill_seconds": fill_secs(n, r, q, b),
                            "acquire_seconds": q + b + batch_utils.BATCH_SHUTDOWN_MARGIN_SECONDS}
                           for n, r, q, b in [(1, 50.0, 600, 3600), (100, 50.0, 600, 3600), (101, 50.0, 600, 3600),
                                              (3, 0.1, 1, 1), (10, 3.0, 1, 1), (7, 1 / 3, 1, 1), (1000, 33.3333, 600, 3600),
                                              (0, 50.0, 600, 3600), (49, 0.7, 10, 10), (1, 1e-300, 1, 1),
                                              (0, 5e-324, 1, 1), (0, 1e-300, 600, 3600)]]
out["shutdown_time_format"] = "%Y-%m-%dT%H:%M:%SZ (seconds truncated, UTC); from utils.construct_sandbox_claim_lifecycle_spec"
out["constants"] = {k: getattr(batch_utils, k) for k in dir(batch_utils) if k.startswith(("BATCH_", "CLOCK_"))}

def _sanitize(v):
    if isinstance(v, float) and not math.isfinite(v):
        return repr(v)
    if isinstance(v, dict):
        return {k: _sanitize(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_sanitize(x) for x in v]
    return v

json.dump(_sanitize(out), sys.stdout, indent=1, sort_keys=False, ensure_ascii=False, allow_nan=False)
