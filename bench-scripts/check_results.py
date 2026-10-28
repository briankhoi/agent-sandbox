# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Validates gke_scale_bench.py JSON result files in a results directory.

Checks every JSON file for:
  - unhandled exceptions
  - non-zero step errors
  - dirty git worktree provenance
  - step started count matching expected n
  - valid server-side kube-apiserver /metrics deltas (apiserver.valid == True)
  - steady.ready_last populated on multi-step runs
  - batch watch count strictly below per-claim watch count

Usage:
    python bench-scripts/check_results.py <results-dir> [expected_n]
"""

import glob
import json
import os
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python check_results.py <results-dir> [expected_n]", file=sys.stderr)
        return 2
    d = sys.argv[1]
    expect_n = int(sys.argv[2]) if len(sys.argv) > 2 else None
    bad: list[str] = []
    watches: dict[str | None, tuple[int, object]] = {}
    for f in sorted(glob.glob(os.path.join(d, "*.json"))):
        with open(f, encoding="utf-8") as fh:
            r = json.load(fh)
        lab = r.get("label", os.path.basename(f))
        if "exception" in r:
            bad.append(f"{lab}: exception {r['exception'][:200]}")
            continue
        if r.get("errors"):
            bad.append(f"{lab}: errors={r['errors']}")
        if r.get("provenance", {}).get("dirty"):
            bad.append(f"{lab}: dirty")
        steps = r.get("steps", [])
        for i, s in enumerate(steps, 1):
            n = expect_n or s.get("n")
            if s.get("started") != n:
                bad.append(f"{lab} step{i}: started {s.get('started')} != {n}")
            a = s.get("apiserver", {})
            if not a.get("valid"):
                bad.append(f"{lab} step{i}: apiserver invalid: {a.get('reason')}")
        if r.get("steady", {}).get("ready_last") is None and len(steps) > 1:
            bad.append(f"{lab}: no steady.ready_last")
        watches[r.get("variant")] = (sum(s.get("watches", 0) for s in steps), r.get("settings"))
        print(
            f"{lab}: steps={len(steps)} errors={r.get('errors')} "
            f"ready_last={r.get('steady', {}).get('ready_last')} "
            f"valid={[s.get('apiserver', {}).get('valid') for s in steps]} "
            f"watches={[s.get('watches') for s in steps]}"
        )
    pc, bu = watches.get("per-claim"), watches.get("batch-unbounded")
    if pc and bu:
        print(f"watches per-claim={pc[0]} batch-unbounded={bu[0]} settings={bu[1]}")
        if not bu[0] < pc[0]:
            bad.append("batch-unbounded watches not < per-claim")
    if bad:
        print("PROBLEMS:", *bad, sep="\n  ")
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
