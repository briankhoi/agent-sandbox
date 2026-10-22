"""Top allocation sites at memray's high-water mark (an aggregated capture), by retained bytes."""
import sys
from collections import defaultdict

from memray import FileReader

path = sys.argv[1]
r = FileReader(path)
meta = r.metadata
print(f"capture {path}: peak {meta.peak_memory / 2**20:.1f} MB, total allocations {meta.total_allocations}, "
      f"python {meta.python_allocator}, native traces {meta.has_native_traces}, pid {meta.pid}")


def frames(rec, n):
    try:
        st = rec.hybrid_stack_trace(max_stacks=n) if meta.has_native_traces else rec.stack_trace(max_stacks=n)
    except Exception:
        st = rec.stack_trace(max_stacks=n)
    return [f"{fn} {fname.rsplit('/site-packages/', 1)[-1]}:{line}" for fn, fname, line in st]


for title, recs in (("high-water mark", r.get_high_watermark_allocation_records(merge_threads=True)),
                    ("leaked at exit", r.get_leaked_allocation_records(merge_threads=True))):
    recs = list(recs)
    total = sum(x.size for x in recs)
    print(f"\n== {title}: {total / 2**20:.1f} MB in {len(recs)} records ==")
    # By the innermost Python frame, which names the caller that owns the memory.
    by_py = defaultdict(lambda: [0, 0])
    for x in recs:
        py = [f for f in frames(x, 60) if ".py:" in f]
        key = " <- ".join(py[:4]) or "(no python frame)"
        by_py[key][0] += x.size
        by_py[key][1] += x.n_allocations
    print("-- top 10 by innermost Python frames --")
    for k, (size, n) in sorted(by_py.items(), key=lambda kv: -kv[1][0])[:10]:
        print(f"{size / 2**20:9.1f} MB {n:9d} allocs  {k}")
    print("-- top 10 native+python stacks --")
    for x in sorted(recs, key=lambda x: -x.size)[:10]:
        print(f"{x.size / 2**20:9.1f} MB {x.n_allocations:9d} allocs {getattr(x.allocator, 'name', x.allocator)}")
        for f in frames(x, 14):
            print(f"      {f}")
