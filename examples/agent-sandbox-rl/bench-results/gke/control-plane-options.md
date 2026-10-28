# Control-plane bottleneck: what we found and what to do (00:06Z, 1.1 h of cluster time used)

## What the diagnostic showed
GKE sizes the control plane automatically: small at creation (based on how many
nodes the default pool had), then larger as node count and load grow. Ours started
with a 2-node default pool, so it began on the smallest size. It has been resized 3
times: 23:11, 23:21, and 23:52-00:01Z, the last one triggered by our benchmark load.

| w160, steady state | API server seats | per-claim Ready last | batch-unbounded Ready last | APF queue wait per step (per-claim / batch) | run wall time |
|---|---:|---:|---:|---:|---:|
| before the 3rd resize (suite run 1) | 122 | 15.18 s | 13.99 s | 446 s / 374 s | 155 s |
| after the 3rd resize (diagnostic, stock settings) | 243 | **4.69 s** | **3.99 s** | 147 s / 40 s | 81-92 s |
| kind (earlier result, context only) | – | 1.37 s | 1.27 s (default) | – | – |

Doubling the API server's capacity cut Ready time about 3x on both arms, so the
control plane was the bottleneck. It also brought out a batch effect that the
queueing had hidden: batch's queue wait is now **3.7x lower** than per-claim's,
because it sends 200 requests per step instead of 640. One run each, so not a verdict.

GKE won't let us remove the limit ourselves. Its admission webhook rejects putting
the controller in the `exempt` priority level, and it caps custom priority levels at
30 shares. There is no public flag for control-plane size.

## Options

| | What | Representative of | Extra time / cost | Risks |
|---|---|---|---|---|
| **A. Continue on this cluster, pre-warmed** (recommended) | Supersede the 8 runs from the 122-seat plane ("pre-resize control plane"). Run two throwaway w1280 bursts so GKE grows the plane for our peak load now, wait out any resize, then restart the suite. Rows as in plan.md §6 but with **3 steps everywhere** (runs are ~2x faster now). The sampler logs control-plane seats every minute. | A GKE cluster that has grown to its load, as a production cluster has | ~15-25 min warm-up; total ≈ 8.1 h (≈1.9 h slack) | GKE may still resize during w1280 (a few minutes of API unavailability on a zonal cluster). Runs that overlap a resize get rerun once (prompt's rule). |
| ~~B. GKE on-call pre-scales the control plane~~ | **Not available: Brian has no GKE on-call access.** (Internal playbook go/gke-playbooks/procedures/default-master-size.md describes it, for reference.) | | | |
| C. Recreate the cluster pre-sized | Create a new cluster whose default pool starts with 101+ small nodes, so GKE picks a bigger initial control plane (e.g. 16 vCPU instead of 2), then shrink the default pool. Self-serve. | A large customer with a pre-scaled control plane | ~1 h rebuild (cluster, pools, controller, driver, 1,280-pod setup); ~$1 for the temporary nodes | Uses internal sizing behaviour; GKE may still adjust later. Loses ~1 h of the 10 h budget. |
| D. Self-managed kops on GCE | The repo's `test/benchmarks/scenarios/benchmarks-kops-gcp*` on n2-standard-8: we pick the control-plane VM and API server flags (e.g. 3000/1000 inflight). | An unconstrained, tuned API server. Not GKE. | ~1-1.5 h setup + runs; ~$8/h while up. Only fits in 10 h if run **in parallel** with A (lead rows only). | Most moving parts (kops, state bucket, DNS). Answers a different question ("when the API server isn't the limit"). |

## Recommendation
**A now. Optionally D in parallel** if you want the unconstrained-API-server answer
tonight. C is the self-serve way to get a known, larger control-plane size from the
start, at the cost of about 1 h of rebuild.

Your concern that benchmarking GKE is meaningless if the API server strains this
easily applies to the *first* runs (an under-sized, still-growing plane). It
doesn't apply to a plane that has grown to the load, which is what A measures and
what production clusters run on. The report will say which control-plane size every
run saw.
