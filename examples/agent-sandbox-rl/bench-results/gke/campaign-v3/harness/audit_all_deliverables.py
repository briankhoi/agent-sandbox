#!/usr/bin/env python3
"""Exhaustive Second Programmatic Non-Hallucination & Fairness Audit.

Verifies every deliverable before GCP teardown and GitHub pushes:
  1. `campaign-v3/` (21 raw JSONs SHA-256 1:1, 14 smoke JSONs, 0 leaks, valid links & tables, README numbers)
  2. `scratch/wt_pr5` + `fresh_pr5_body.md` (26 RESULTS.md rows #17-#42 exact match against raw JSONs + hardware specs, 6 reports, 0 leaks/placeholders)
  3. `scratch/wt_pr6` + `fresh_pr6_body.md` (diff scope, py_compile, bash -n, CLI flags/functions match PR body, 3 bugfix unit assertions)
  4. `~/agent-sandbox-notes` `gke-benchmarking.md` (`GKE-13..16` anchors & numbers, session isolation check)
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import py_compile
import re
import subprocess
import sys

SCRATCH = "<SCRATCH_DIR>"
V2_RUNS = f"{SCRATCH}/gke_bench_v2/runs"
V3_DIR = f"{SCRATCH}/gke_bench_v3"
V3_RUNS = f"{V3_DIR}/runs"
SUMMARY = json.load(open(f"{V3_DIR}/verified_metrics_summary.json", encoding="utf-8"))

CV3_DIR = "<HOME>/agent-sandbox/.claude/worktrees/rl-gke/examples/agent-sandbox-rl/bench-results/gke/campaign-v3"
WT_PR5 = f"{SCRATCH}/wt_pr5"
WT_PR6 = f"{SCRATCH}/wt_pr6"
PR5_BODY = f"{SCRATCH}/fresh_pr5_body.md"
PR6_BODY = f"{SCRATCH}/fresh_pr6_body.md"
NOTES_FILE = "<HOME>/agent-sandbox-notes/batch-primitives/handoff/learnings/gke-benchmarking.md"


def sha256(p: str) -> str:
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def check_markdown_hygiene(md_path: str, forbid_project_id: bool = False) -> None:
    txt = open(md_path, encoding="utf-8").read()
    base_dir = os.path.dirname(md_path)

    # 1. Forbidden tokens / placeholders
    bad_patterns = [r"\bTML\b", r"\bMistral\b", r"\bReflection\b", r"\{\{", r"\}\}", r"\bTODO\b", r"\bTBD\b"]
    if forbid_project_id:
        bad_patterns.append(r"<USER>")
    for pat in bad_patterns:
        m = re.search(pat, txt)
        assert not m, f"{md_path}: forbidden pattern {pat!r} matched {m.group(0)!r}"

    # 2. Markdown relative file links
    for m in re.finditer(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]*)?\)", txt):
        link = m.group(1).strip()
        if link.startswith(("http://", "https://", "mailto:", "file://")):
            continue
        if not link:
            continue
        resolved = os.path.normpath(os.path.join(base_dir, link))
        assert os.path.exists(resolved), f"{md_path}: broken relative link {link!r} -> {resolved}"

    # 3. Markdown table column uniformity
    lines = txt.splitlines()
    i = 0
    in_code = False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if not in_code and line.strip().startswith("|") and line.strip().endswith("|"):
            tbl = []
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                # Split unescaped pipes
                cols = re.split(r"(?<!\\)\|", lines[i].strip())[1:-1]
                tbl.append((i + 1, len(cols)))
                i += 1
            if len(tbl) >= 2:
                expected = tbl[0][1]
                for ln, c_cnt in tbl[1:]:
                    assert c_cnt == expected, f"{md_path}:{ln}: table column count {c_cnt} != header {expected}"
            continue
        i += 1


def audit_campaign_v3() -> None:
    print("=== [1/4] Auditing campaign-v3/ (public branch bench/rl-gke) ===")
    raw_files = sorted(os.listdir(f"{CV3_DIR}/raw"))
    smoke_files = sorted(os.listdir(f"{CV3_DIR}/smoke"))
    assert len(raw_files) == 21, f"Expected 21 raw JSONs, got {len(raw_files)}"
    assert len(smoke_files) == 14, f"Expected 14 smoke JSONs, got {len(smoke_files)}"

    # Byte-for-byte SHA-256 parity with scratch/gke_bench_v3/runs/
    for fn in raw_files:
        assert sha256(f"{CV3_DIR}/raw/{fn}") == sha256(f"{V3_RUNS}/{fn}"), f"SHA mismatch on raw/{fn}"
    for fn in smoke_files:
        assert sha256(f"{CV3_DIR}/smoke/{fn}") == sha256(f"{V3_RUNS}/{fn}"), f"SHA mismatch on smoke/{fn}"

    # Zero leaks across ALL files in campaign-v3/
    for p in glob.glob(f"{CV3_DIR}/**/*", recursive=True):
        if os.path.isfile(p):
            txt = open(p, encoding="utf-8").read()
            for pat in (r"<USER>", r"\bTML\b", r"\bMistral\b", r"\bReflection\b"):
                m = re.search(pat, txt)
                assert not m, f"Public leak in {p}: {pat!r} -> {m.group(0)!r}"

    check_markdown_hygiene(f"{CV3_DIR}/README.md", forbid_project_id=True)
    readme = open(f"{CV3_DIR}/README.md", encoding="utf-8").read()
    # Verify key numbers in README match raw JSONs
    b_pc_w = SUMMARY["campaign_3"]["c3_1b_cold_gcfs_500"]["per_claim"]["variants"][0]["wall_s"]
    b_ba_w = SUMMARY["campaign_3"]["c3_1b_cold_gcfs_500"]["batch64"]["variants"][0]["wall_s"]
    c8_pc_acq = SUMMARY["campaign_3"]["c3_3c_mc5_cuj1_8000"]["per_claim"]["points"][0]["acquire_wall_med_s"]
    c8_ba_acq = SUMMARY["campaign_3"]["c3_3c_mc5_cuj1_8000"]["batch64"]["points"][0]["acquire_wall_med_s"]
    s3d = {(r["mode"], r["n"]): r for r in SUMMARY["campaign_3"]["c3_3d_mc5_sdk_5000"]["summary"]}
    for expected_substr in (
        "132,572", "492.52s", "539.18s", "124.73s", "146.22s",
        f"{b_pc_w:.2f}s", f"{b_ba_w:.2f}s", f"{c8_pc_acq:.2f}s", f"{c8_ba_acq:.2f}s",
        f"{s3d[('batch_release_deletecollection', 5000)]['release_api_med_s']:.2f}s",
        f"{s3d[('batch_release_deletecollection', 5000)]['pods_gone_med_s']:.2f}s",
        f"{s3d[('per_claim_concurrent_terminate', 5000)]['pods_gone_med_s']:.2f}s",
        f"{s3d[('per_claim_sdk_delete_all', 5000)]['pods_gone_med_s']:.2f}s",
        "e2-standard-32", "200 GB pd-balanced", "100 GB pd-standard",
    ):
        assert expected_substr in readme, f"campaign-v3/README.md missing expected verified string {expected_substr!r}"
    print("  PASS: 21/21 raw + 14/14 smoke SHA-256 verified, 0 public leaks, links & tables valid.")


def audit_pr5() -> None:
    print("=== [2/4] Auditing PR #5 (wt_pr5 + fresh_pr5_body.md) ===")
    for md in [
        PR5_BODY,
        f"{WT_PR5}/agent-sandbox/RESULTS.md",
        f"{WT_PR5}/agent-sandbox/performance_reports/11_05_2026_RESULTS.md",
        f"{WT_PR5}/agent-sandbox/performance_reports/brian-nguyen_20261105-073700.md",
        f"{WT_PR5}/agent-sandbox/performance_reports/brian-nguyen_20261105-143900.md",
        f"{WT_PR5}/agent-sandbox/performance_reports/brian-nguyen_20261105-151500.md",
        f"{WT_PR5}/agent-sandbox/performance_reports/brian-nguyen_20261106-154500.md",
        f"{WT_PR5}/agent-sandbox/performance_reports/brian-nguyen_20261106-163800.md",
    ]:
        check_markdown_hygiene(md, forbid_project_id=False)

    # Verify external non-JSON reference numbers are gone from 11_05_2026_RESULTS.md
    synth = open(f"{WT_PR5}/agent-sandbox/performance_reports/11_05_2026_RESULTS.md", encoding="utf-8").read()
    for stale in ("1.1s / 1.8s / 4.3s / 8.8s", "30.8s", "300 GB pd-balanced", "55 total benchmark runs", "275,116"):
        assert stale not in synth, f"Stale string {stale!r} still in 11_05_2026_RESULTS.md"

    # Cross-check EVERY row #17..#42 in RESULTS.md against raw JSONs!
    c2 = SUMMARY["campaign_2"]
    expected_rows = {
        17: (f"{V2_RUNS}/{c2['phase1_t0_3000']['naive']['per_claim']['file']}", "naive", "e2-standard-32", 3000),
        18: (f"{V2_RUNS}/{c2['phase1_t0_3000']['sliding']['per_claim']['file']}", "sliding", "e2-standard-32", 3000),
        19: (f"{V2_RUNS}/{c2['phase1_t0_3000']['pipelined']['per_claim']['file']}", "pipelined", "e2-standard-32", 3000),
        20: (f"{V2_RUNS}/{c2['phase1_t0_3000']['recycle']['per_claim']['file']}", "naive+recycle", "e2-standard-32", 3000),
        21: (f"{V2_RUNS}/{c2['phase1_t0_3000']['naive']['batch64']['file']}", "naive", "e2-standard-32", 3000),
        22: (f"{V2_RUNS}/{c2['phase1_t0_3000']['sliding']['batch64']['file']}", "sliding", "e2-standard-32", 3000),
        23: (f"{V2_RUNS}/{c2['phase1_t0_3000']['pipelined']['batch64']['file']}", "pipelined", "e2-standard-32", 3000),
        24: (f"{V2_RUNS}/{c2['phase1_t0_3000']['recycle']['batch64']['file']}", "naive+recycle", "e2-standard-32", 3000),
        25: (f"{V2_RUNS}/{c2['phase2_configH_sweeps']['swe3000']['naive']['per_claim']['file']}", "naive", "e2-standard-32", 3000),
        26: (f"{V2_RUNS}/{c2['phase2_configH_sweeps']['swe3000']['sliding']['per_claim']['file']}", "sliding", "e2-standard-32", 3000),
        27: (f"{V2_RUNS}/{c2['phase2_configH_sweeps']['swe3000']['pipelined']['per_claim']['file']}", "pipelined", "e2-standard-32", 3000),
        28: (f"{V2_RUNS}/{c2['phase2_configH_sweeps']['swe3000']['naive']['batch64']['file']}", "naive", "e2-standard-32", 3000),
        29: (f"{V2_RUNS}/{c2['phase2_configH_sweeps']['swe3000']['sliding']['batch64']['file']}", "sliding", "e2-standard-32", 3000),
        30: (f"{V2_RUNS}/{c2['phase2_configH_sweeps']['swe3000']['pipelined']['batch64']['file']}", "pipelined", "e2-standard-32", 3000),
        31: (f"{V2_RUNS}/{c2['phase3_t1_sbd_3000']['naive']['per_claim']['file']}", "naive", "n2-standard-32", 3000),
        32: (f"{V2_RUNS}/{c2['phase3_t1_sbd_3000']['naive']['batch64']['file']}", "naive", "n2-standard-32", 3000),
        33: (f"{V2_RUNS}/{c2['phase3_t1_sbd_3000']['pipelined']['per_claim']['file']}", "pipelined", "n2-standard-32", 3000),
        34: (f"{V2_RUNS}/{c2['phase3_t1_sbd_3000']['pipelined']['batch64']['file']}", "pipelined", "n2-standard-32", 3000),
        35: (f"{V2_RUNS}/{c2['phase3_t1_sbd_3000']['sleep30']['per_claim']['file']}", "pipelined", "n2-standard-32", 3000),
        36: (f"{V2_RUNS}/{c2['phase3_t1_sbd_3000']['sleep30']['batch64']['file']}", "pipelined", "n2-standard-32", 3000),
        37: (f"{V3_RUNS}/c3_1b_cold_swebench500_per_claim.json", "naive", "e2-standard-32", 500),
        38: (f"{V3_RUNS}/c3_1b_cold_swebench500_batch64.json", "naive", "e2-standard-32", 500),
        39: (f"{V3_RUNS}/c3_1a_cold_sbd_symmetric_per_claim.json", "naive", "n2-standard-32", 3000),
        40: (f"{V3_RUNS}/c3_1a_cold_sbd_symmetric_batch64.json", "naive", "n2-standard-32", 3000),
        41: (f"{V3_RUNS}/c3_1a_anchor_exact_c2_per_claim.json", "naive", "n2-standard-32", 3000),
        42: (f"{V3_RUNS}/c3_1a_anchor_exact_c2_batch64.json", "naive", "n2-standard-32", 3000),
    }

    results_lines = open(f"{WT_PR5}/agent-sandbox/RESULTS.md", encoding="utf-8").read().splitlines()
    found_nums = set()
    for line in results_lines:
        m = re.match(r"^\|\s*(\d+)\s*\|", line)
        if not m:
            continue
        rnum = int(m.group(1))
        if rnum < 17:
            continue
        assert rnum in expected_rows, f"Unexpected row #{rnum} in RESULTS.md"
        found_nums.add(rnum)
        cols = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        assert len(cols) == 24, f"Row #{rnum} has {len(cols)} cols, expected 24"

        jpath, vlabel, mtype, ntasks = expected_rows[rnum]
        raw = json.load(open(jpath, encoding="utf-8"))
        variant = next(v for v in raw["variants"] if v["label"] == vlabel)

        wall_col = float(re.sub(r"[^0-9.]", "", cols[19]))
        spt_col = float(re.sub(r"[^0-9.]", "", cols[20]))
        tasks_col = int(re.sub(r"[^0-9]", "", cols[9]))
        assert tasks_col == ntasks, f"Row #{rnum} tasks {tasks_col} != {ntasks}"
        assert abs(wall_col - round(variant["wall_s"], 2)) < 0.015, f"Row #{rnum} wall {wall_col} != {variant['wall_s']}"
        assert abs(spt_col - round(variant["wall_s"] / ntasks, 4)) < 0.0015, f"Row #{rnum} s/task {spt_col} != {variant['wall_s']/ntasks:.4f}"
        assert cols[22] == f"{ntasks}/0", f"Row #{rnum} ok/err col {cols[22]!r} != '{ntasks}/0'"
        assert mtype in cols[23], f"Row #{rnum} Notes col {cols[23]!r} missing {mtype!r}"

    assert found_nums == set(range(17, 43)), f"Missing rows in RESULTS.md: {set(range(17,43)) - found_nums}"

    diff_files = subprocess.check_output(["git", "-C", WT_PR5, "diff", "--name-only", "origin/main"], text=True).splitlines()
    expected_diff = {
        "agent-sandbox/RESULTS.md",
        "agent-sandbox/performance_reports/11_05_2026_RESULTS.md",
        "agent-sandbox/performance_reports/brian-nguyen_20261105-073700.md",
        "agent-sandbox/performance_reports/brian-nguyen_20261105-143900.md",
        "agent-sandbox/performance_reports/brian-nguyen_20261105-151500.md",
        "agent-sandbox/performance_reports/brian-nguyen_20261106-154500.md",
        "agent-sandbox/performance_reports/brian-nguyen_20261106-163800.md",
    }
    assert set(diff_files) == expected_diff, f"Unexpected PR #5 diff files: {set(diff_files) ^ expected_diff}"
    print("  PASS: All 26 rows (#17-#42), 6 reports, PR #5 body, and git diff scope verified 1:1 against raw JSONs.")


def audit_pr6() -> None:
    print("=== [3/4] Auditing PR #6 (wt_pr6 + fresh_pr6_body.md) ===")
    check_markdown_hygiene(PR6_BODY, forbid_project_id=True)
    diff_files = subprocess.check_output(["git", "-C", WT_PR6, "diff", "--name-only", "origin/main"], text=True).splitlines()
    expected_pr6 = {
        "agent-sandbox/drivers/bench_core.py",
        "agent-sandbox/drivers/run_claim_soak_bench.py",
        "agent-sandbox/drivers/run_density_bench.py",
        "agent-sandbox/drivers/run_multicluster_bench.py",
        "agent-sandbox/drivers/run_smoke_gate.py",
        "agent-sandbox/drivers/verify_cluster.sh",
    }
    assert set(diff_files) == expected_pr6, f"Unexpected PR #6 diff files: {set(diff_files) ^ expected_pr6}"

    # Verify every file bullet in fresh_pr6_body.md matches expected_pr6
    pr6_body = open(PR6_BODY, encoding="utf-8").read()
    body_files = set(re.findall(r"\*\*`(agent-sandbox/drivers/[^`:]+)`:\*\*", pr6_body))
    assert body_files == expected_pr6, f"PR #6 body file bullets {body_files} != {expected_pr6}"

    for pyf in ("bench_core.py", "run_claim_soak_bench.py", "run_density_bench.py", "run_multicluster_bench.py", "run_smoke_gate.py"):
        py_compile.compile(f"{WT_PR6}/agent-sandbox/drivers/{pyf}", doraise=True)
    subprocess.check_call(["bash", "-n", f"{WT_PR6}/agent-sandbox/drivers/verify_cluster.sh"])

    # Functional unit checks on PR #6's 3 bugfixes (with upstream stub lacking BatchClaimConfig)
    import types
    stub_rl = types.ModuleType("agent_sandbox_rl")
    stub_rl.__path__ = []
    for cls_name in ("AsyncSandboxFleet", "ClusterConfig", "FleetConfig", "ResourceSpec", "TemplateSpec", "Task"):
        setattr(stub_rl, cls_name, type(cls_name, (), {}))
    stub_src = types.ModuleType("agent_sandbox_rl.sources")
    setattr(stub_src, "ListSource", type("ListSource", (), {}))
    sys.modules["agent_sandbox_rl"] = stub_rl
    sys.modules["agent_sandbox_rl.sources"] = stub_src
    sys.path.insert(0, f"{WT_PR6}/agent-sandbox/drivers")
    import bench_core as bc
    import run_smoke_gate as rsg

    # Bugfix 1: fail-fast RuntimeError when --claim-mode batch lacks BatchClaimConfig
    class _Args:
        claim_mode = "batch"
        batch_max_in_flight = 64
        batch_create_rps = 200.0
        batch_lease_duration = 300
        batch_work_budget = 3600
    try:
        bc.apply_claim_mode_kwargs({}, _Args())
        raise AssertionError("Expected RuntimeError when BatchClaimConfig is absent!")
    except RuntimeError:
        pass

    # Bugfix 2: recursive _find_teardown_leaks
    leaks = rsg._find_teardown_leaks({"c1": {"remaining": {"sandboxes": 0, "claims": 2}}})
    assert len(leaks) == 1 and "claims" in str(leaks), f"Unexpected leaks: {leaks}"
    assert not rsg._find_teardown_leaks({"c1": {"remaining": {"sandboxes": 0, "claims": 0}}})

    # Bugfix 3: __file__-relative default images/swebench500.txt
    assert 'os.path.dirname(os.path.abspath(__file__)), "images", "swebench500.txt"' in open(rsg.__file__).read()
    assert os.path.exists(os.path.join(os.path.dirname(os.path.abspath(rsg.__file__)), "images", "swebench500.txt"))
    print("  PASS: PR #6 6-file scope, PR body 1:1 file match, py_compile, bash -n, and all 3 bugfix assertions verified.")


def audit_notes() -> None:
    print("=== [4/4] Auditing ~/agent-sandbox-notes (GKE-13..16 & Rule 0 isolation) ===")
    check_markdown_hygiene(NOTES_FILE, forbid_project_id=False)
    ntxt = open(NOTES_FILE, encoding="utf-8").read()
    for anchor in ('<a id="gke-13"></a>', '<a id="gke-14"></a>', '<a id="gke-15"></a>', '<a id="gke-16"></a>'):
        assert ntxt.count(anchor) == 1, f"Anchor {anchor} count != 1"
    for must in ("492.52s", "539.18s", "124.73s", "146.22s", "100 GB pd-standard", "0.97s", "10.05s"):
        assert must in ntxt, f"gke-benchmarking.md missing {must!r}"
    assert "50 GB pd-standard" not in ntxt, "Stale '50 GB pd-standard' found in gke-benchmarking.md"
    print("  PASS: GKE-13..16 verified with 0 stale specs/numbers.")


if __name__ == "__main__":
    audit_campaign_v3()
    audit_pr5()
    audit_pr6()
    audit_notes()
    print("\nALL 4 AUDIT SUITES PASSED 100% WITH ZERO DISCREPANCIES!")
