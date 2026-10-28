"""Bottleneck gate (plan.md section 7). Usage: python gate.py <warmup-results-dir>"""
import glob
import json
import os
import re
import sys

d = sys.argv[1]
ok = True
create_ms = []
for f in sorted(glob.glob(os.path.join(d, "w*.json"))):
    r = json.load(open(f))
    if "exception" in r:
        print(f, "EXCEPTION", r["exception"][:200]); ok = False; continue
    for i, s in enumerate(r["steps"], 1):
        a = s.get("apiserver", {})
        if i == 1:
            continue  # steady steps only
        req = a.get("apf_requests") or a.get("requests_all") or 0
        wait = a.get("apf_wait_s")
        rej, t429 = a.get("apf_rejected", 0), a.get("http_429_all", 0)
        per = (wait / req * 1000) if (wait is not None and req) else None
        cm = (a.get("mean_latency_ms") or {}).get("POST sandboxclaims")
        if cm:
            create_ms.append(cm)
        print(f"{os.path.basename(f)} step{i}: ready_last={s['acquired_s'].get('last', s['acquired_s'].get('max'))} "
              f"apf_wait={wait} requests_all={req} wait/req={per if per is None else round(per, 1)} ms "
              f"rejected={rej} 429={t429} create_ms={cm} valid={a.get('valid')}")
        if per is None or per > 10 or rej or t429:
            ok = False
    st = r.get("steady", {})
    print(f"  steady: ready_last={st.get('ready_last')} apf_wait_s={st.get('apiserver_apf_wait_s')} "
          f"requests_all={st.get('apiserver_requests_all')} create_ms={st.get('apiserver_create_ms')}")


def load(p):
    out = {}
    for line in open(p):
        m = re.match(r'rest_client_request_duration_seconds_(sum|count)\{.*verb="(\w+)"\} (\S+)', line)
        if m:
            out[(m.group(1), m.group(2))] = float(m.group(3))
    return out


b, a = load(os.path.join(d, "ctrl-before.txt")), load(os.path.join(d, "ctrl-after.txt"))
s = sum(a.get(("sum", v), 0) - b.get(("sum", v), 0) for v in ("PATCH", "POST", "PUT"))
c = sum(a.get(("count", v), 0) - b.get(("count", v), 0) for v in ("PATCH", "POST", "PUT"))
client_ms = s / c * 1000 if c else None
server_ms = sum(create_ms) / len(create_ms) if create_ms else None
print(f"controller client write latency {client_ms and round(client_ms, 1)} ms over {int(c)} writes; "
      f"server create latency {server_ms and round(server_ms, 1)} ms")
if client_ms is None or server_ms is None or client_ms > 2 * server_ms:
    ok = False
print("GATE PASS" if ok else "GATE FAIL")
