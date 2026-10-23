"""Is a fitted calibration file better than the numbers in use? The pre-registered test of KARARLAR K66.

Both models replay the post-maintenance calibration logs (ayar, from 2026-10-14, time order) as a CHAIN, both
starting at k = 1 and learning their own k: a new calibration defines k = 1, so the k the live twin recorded under the
old numbers means nothing for it [K59]. Also: the September core set (fresh k = 1), GUC events in the chain, the alarm
report over the whole archive (`report_v02.py`) and the synthetic slow-fault test (`slow_fault_test.py`), each with
the candidate file switched in through the TWIN_CALIBRATION environment variable (realtime_websocket reads it).
--compare OTHER.json reports how far two calibrations of the same printer differ (reproducibility).
Analysis only: never touches calibration.json.

Usage (in dt/):   py benchmark/calibration_check.py CANDIDATE.json [--compare OTHER.json] [--skip-slow]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import evaluate as ev  # noqa: E402

SINCE = "2026-10-14"                 # first day after the 13.10 maintenance (current hardware state)
LABEL_FILE = os.path.join(HERE, "benchmark", "ariza_ornekleri", "etiketler.csv")
LABEL_SLACK = 10.0                   # s, as in report_v02


def use(cal):
    """Switch the twin's calibration file for this process (None: the file in use)."""
    if cal:
        os.environ["TWIN_CALIBRATION"] = os.path.abspath(cal)
    else:
        os.environ.pop("TWIN_CALIBRATION", None)


def chain_paths():
    since = time.mktime(time.strptime(SINCE, "%Y-%m-%d"))
    ps = [(ev.read_log(p).time.iloc[0], p) for p in ev.set_paths("ayar")]
    return [p for t0, p in sorted(ps) if t0 >= since]


def labels():
    lab = pd.read_csv(LABEL_FILE)
    lab["name"] = lab.dosya.map(os.path.basename)
    return lab


def chain_job(cal):
    """Chain replay: phase scores and the GUC events not inside a fault label."""
    use(cal)
    paths = chain_paths()
    sc = ev.evaluate(None, paths, chain=True)
    m = ev.load_model()
    lab = labels()
    k, kfil, ref = 1.0, 1.0, 1.0
    false_guc = []
    for p in paths:
        df = ev.read_log(p)
        evs, st = [], {}
        _, _, _, ks, _ = ev.replay(df, m, evs, k0=k, kfil0=kfil, state=st, k_ref0=ref)
        k, kfil, ref = float(ks[-1]), st["kfil"], st["k_ref"]
        t0 = df.t.iloc[0]
        mine = lab[(lab.name == os.path.basename(p)) & (lab.beklenen != "-")]
        for code, ts, _ in evs:
            if not code.startswith("GUC"):
                continue
            s = ts - t0
            if not ((mine.baslangic_s - LABEL_SLACK <= s) & (s <= mine.bitis_s)).any():
                false_guc.append((ev.short_name(p), code, round(s)))
    return ev.pooled(sc).set_index("faz"), false_guc


def september_job(cal):
    use(cal)
    return ev.pooled(ev.evaluate(None)).set_index("faz")


def run_tool(cal, args):
    env = dict(os.environ)
    if cal:
        env["TWIN_CALIBRATION"] = os.path.abspath(cal)
    else:
        env.pop("TWIN_CALIBRATION", None)
    r = subprocess.run([sys.executable] + args, cwd=HERE, env=env, capture_output=True, text=True)
    return r.stdout


def report_summary(out):
    m = re.search(r"caught (\d+)/(\d+), paused (\d+)/\d+", out)
    fp = [ln for ln in out.splitlines() if ln.strip().startswith("all ")]
    return (m.group(0) if m else "?"), (fp[0].split()[-3] if fp else "?")   # all: ... false_per_print_h, pauses


def slow_summary(out):
    return [ln.strip() for ln in out.splitlines() if re.match(r"\s+(fan_on|mid)\s", ln) and " new: " in ln]


def compare(a, b):
    pa, pb = json.load(open(a))["params"], json.load(open(b))["params"]
    ua = json.load(open(a)).get("uncertainty_log_factor", {})
    ub = json.load(open(b)).get("uncertainty_log_factor", {})
    print(f"\nreproducibility: {os.path.basename(a)} vs {os.path.basename(b)} (log ratio; 2 se combined where known)")
    for n in pa:
        if n in pb and pa[n] and pb[n]:
            r = np.log(pa[n] / pb[n])
            se = np.hypot(ua.get(n) or 0, ub.get(n) or 0)
            flag = "" if not se else ("  ok" if abs(r) <= 2 * se else "  DIFFERS")
            print(f"  {n:11s} {pb[n]:11.4g} -> {pa[n]:11.4g}  x{np.exp(r):.3f}" + (f"  (2se x{np.exp(2 * se):.3f})" if se else "") + flag)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("candidate", help="fitted calibration file (calibrate.py --staged --write)")
    ap.add_argument("--compare", help="another calibration of the same printer (reproducibility)")
    ap.add_argument("--skip-slow", action="store_true", help="skip the synthetic slow-fault test (slowest part)")
    a = ap.parse_args()
    if not os.path.isfile(a.candidate):
        sys.exit(f"not found: {a.candidate}")
    cands = {"current": None, "candidate": a.candidate}
    with ProcessPoolExecutor(4) as pool:
        ch = dict(zip(cands, pool.map(chain_job, cands.values())))
        sep = dict(zip(cands, pool.map(september_job, cands.values())))

    def table(title, rows):
        print(f"\n{title}: RMSE (bias) C, current -> candidate")
        for ph in rows["current"].index:
            c, n = rows["current"], rows["candidate"]
            print(f"  {ph:12s} {c.rmse[ph]:5.2f} ({c.bias[ph]:+5.2f}) -> {n.rmse[ph]:5.2f} ({n.bias[ph]:+5.2f})"
                  + (f"   d {n.rmse[ph] - c.rmse[ph]:+.2f}" if ph in n.index else ""))
    table(f"calibration logs since {SINCE}, chain from k = 1", {k: v[0] for k, v in ch.items()})
    table("September core set, fresh k = 1", sep)
    for k, (_, fg) in ch.items():
        print(f"\nGUC outside fault labels in the chain, {k}: {len(fg)}  {fg}")

    print("\nalarm report over the whole archive (report_v02.py):")
    for k, cal in cands.items():
        caught, fph = report_summary(run_tool(cal, ["benchmark/report_v02.py"]))
        print(f"  {k:9s} {caught}; false alarms per print hour (all) {fph}")
    if not a.skip_slow:
        print("\nsynthetic slow faults (slow_fault_test.py, calibration set):")
        for k, cal in cands.items():
            print(f"  {k}:")
            for ln in slow_summary(run_tool(cal, ["benchmark/slow_fault_test.py", "--variants", "new"])):
                print("    " + ln)
    if a.compare:
        compare(a.candidate, a.compare)
    print("\ncriteria: KARARLAR K66")


if __name__ == "__main__":
    main()
