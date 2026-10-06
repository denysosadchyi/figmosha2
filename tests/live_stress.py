"""Live stress test against real Figma files — run by hand, not by pytest.

Every connected file gets N concurrent agents doing a read-modify-write on a
counter kept in the document's plugin data (invisible, removed afterwards):
read, pause, write + 1. Through the bridge's queue each file must end at
exactly N. A control run with "parallel" (no queue) shows the race is real.

    python tests/live_stress.py            # 20 agents per file
    python tests/live_stress.py 50         # 50 agents per file

Needs the bridge running and the plugin open in at least one file. Nothing but
the plugin-data key "figmosha-stress" is touched, and it is cleared at the end.
"""
import json
import random
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BRIDGE = "http://127.0.0.1:8787"
KEY = "figmosha-stress"
RMW = (f"const v = +(figma.root.getPluginData('{KEY}') || 0);"
       # A pause so concurrent scripts genuinely overlap. Figma slows timers in
       # background tabs to ~1s, so this run takes longer there; that's Figma.
       "await new Promise(r => setTimeout(r, 30));"
       f"figma.root.setPluginData('{KEY}', String(v + 1)); return v + 1")
READ = f"return +(figma.root.getPluginData('{KEY}') || 0)"
RESET = f"figma.root.setPluginData('{KEY}', ''); return 0"


def post(body):
    req = urllib.request.Request(f"{BRIDGE}/exec", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def run(docs, n, parallel):
    for d in docs:
        post({"code": RESET, "target": d})
    jobs = [(d, i) for i in range(n) for d in docs]
    random.shuffle(jobs)
    t0 = time.time()
    with ThreadPoolExecutor(64) as pool:
        results = list(pool.map(lambda j: post({
            "code": RMW, "target": j[0], "agent": f"stress-{j[1]}",
            "timeout": 120, "queue_timeout": 300, "parallel": parallel}), jobs))
    failed = [r for r in results if r[0] != 200]
    counts = [post({"code": READ, "target": d})[1].get("value") for d in docs]
    for d in docs:
        post({"code": RESET, "target": d})
    return time.time() - t0, failed, counts


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    with urllib.request.urlopen(f"{BRIDGE}/targets") as r:
        files = json.load(r)["files"]
    docs = list(dict.fromkeys(f["doc"] for f in files if f.get("doc")))
    if not docs:
        sys.exit("no connected file reports a document id — re-run the plugin")
    print(f"{len(docs)} file(s), {n} agents each")

    took, failed, counts = run(docs, n, parallel=False)
    ok = not failed and counts == [n] * len(docs)
    print(f"queue:    {took:5.1f}s  counters {counts}  failures {len(failed)}  "
          f"{'PASS' if ok else 'FAIL'}")

    took, _, counts = run(docs, n, parallel=True)
    raced = any(c < n for c in counts)
    print(f"parallel: {took:5.1f}s  counters {counts}  "
          f"{'race reproduced, as expected' if raced else 'no race seen (too fast to overlap)'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
