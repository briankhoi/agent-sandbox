# Batch Verification Sweep Remediation & Verification Plan

Status: Completed across `review/r-sweep/*` (`24/N` and `25/N` pushed to `origin`; verified against `VERIFICATION_SWEEP_RESULTS.md` at `afba05c0`)

## 1. Locked User-Approved Scope Split (`28 DO` / `14 SKIP` across `F-01..F-41`)

- **DO (`28` items focused on batch correctness and batch-scale performance):**
  - **Batch SDK (`feat/batch-12-lease-watch-fencing` `24/N` & `feat/batch-13-client-handle-hardening` `25/N`):** `F-01`, `F-02`, `F-03`, `F-04`, `F-08`, `F-09`, `F-12`, `F-13a`, `F-15` (`a`+`b`), `F-16`, `F-17` (`a`+`b`), `F-18`, `F-23` (`a`+`b`), `F-24`, `F-25`, `F-26`, `F-27`, `F-29`, `F-30` (SDK `batch.get_member`), `F-31`, `F-32`, `F-36`, `F-37`.
  - **RL Batch Integration (folded in-place into `review/r-sweep/feat/rl-batch-claim-mode` + `review/r-sweep/feat/rl-batch-hardening`, `27/N`):** `F-05`, `F-06` (5 rollback sites in `fleet.py`), `F-07`, `F-10` (`a`+`c`, plus `vars(self.sandbox)` snapshot check), `F-11` (`a`+`b`), `F-13b`, `F-19a`, `F-20a`, `F-21` (`a`+`b`), `F-30` (RL `BatchClaim.still_reaches` + `BatchWatchdog._poll_once`), `F-33` (`SandboxFleet._handles` `dict[int, SandboxHandle]`).
- **SKIP (`14` items/sub-items, zero production edits in batch PRs):**
  - **Already fixed on `26/N` (`bb443e71` / `review/r-sweep/feat/rl-batch-hardening`):** `F-10b`, `F-14a`, `F-19b`, `F-20b`, `F-21c`, `F-34` (plus all 7 verified intended contracts in Section 3.13).
  - **Out of batch scope (`upstream/main` / `per_claim`-only / monotonic tombstone invariant):** `F-14b`, `F-22` (`0` `mypy` errors enforced on touched RL batch modules), `F-28`, `F-35`, `F-38`, `F-39`, `F-40`, `F-41`.

## 2. Stack & Commit Plan

1. **Phase A (`23/N`, `review/r-sweep/feat/batch-11-acquire-annotations`):** Mechanical rebase onto `upstream/feature/batch-claim`.
2. **Phase B (`New 24/N`, `feat/batch-12-lease-watch-fencing`):** 8 atomic commits (`F-02`; `F-01+F-16`; `F-03+F-12`; `F-04`; `F-27+F-29`; `F-24+F-25+F-26+F-36`; `F-18`; `F-30+F-31+F-32+F-37`).
3. **Phase C (`New 25/N`, `feat/batch-13-client-handle-hardening`):** 4 atomic commits (`F-09+F-23b`; `F-08+F-15+F-23a`; `F-17`; `F-13a`).
4. **Phase D (`Renumbered 26/N`, `review/r-sweep/merge/main-into-batch-claim-2`):** Merge `upstream/main` into `New 25/N` and regenerate docs.
5. **Phase E (`Folded RL 27/N`, `review/r-sweep/feat/rl-batch-claim-mode` + `review/r-sweep/feat/rl-batch-hardening`):** Rebase onto `Renumbered 26/N` and fold approved RL batch fixes into their owning commits.
