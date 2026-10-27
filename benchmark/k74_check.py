"""Nozzle change: adapt (A, the numbers in use) or recalibrate (B, the new nozzle's calibration print)? The
pre-registered test of KARARLAR K74, decided on the steel nozzle's prints AFTER its calibration print.

Test data (rule, not a hand pick): every log in loglar/baskilar whose first row is after the end of the steel
calibration print (CAL_LOG), except experiments, calibrations and maintenance sessions (file name), in time order.
Both models replay them as a CHAIN from k = 1, each learning its own k (a calibration defines k = 1, K59).
Criteria (K74 = K66's): B is accepted if ALL hold:
  1  TUMU RMSE at least 0.05 C better;   2  fan hold (tutma245fan) RMSE at most 0.10 C worse;
  3  GUC events outside fault labels do not increase;
  4  synthetic slow faults (slow_fault_test's injections, each model from its own chain state): in none of the four
     scenario groups fewer caught.
K74 asks for >= MIN_HOURS print hours; until then only --status (list + hours, no scores: pre-registration).

Usage (in dt/):   py benchmark/k74_check.py --status
                  py benchmark/k74_check.py [B.json] [--logs LOG ...] [--force]
--logs replays other logs instead (tool check on old data; not the K74 test).
"""
import argparse
import glob
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "benchmark"))
import evaluate as ev  # noqa: E402

CAL_LOG = os.path.join(ROOT, "loglar", "deneyler", "kalibrasyon_245_celik.csv")
B_DEFAULT = os.path.join(ROOT, "benchmark", "k74", "calibration_celik.json")
PRINT_DIR = os.path.join(ROOT, "loglar", "baskilar")
EXCLUDE = ("deney", "kalibrasyon", "calibration", "bakim")
MIN_HOURS = 4.0
LABEL_SLACK = 10.0          # s, as in report_v02 / calibration_check


def test_logs():
    t_cal = ev.read_log(CAL_LOG).time.iloc[-1]
    out = []
    for p in glob.glob(os.path.join(PRINT_DIR, "*.csv")):
        if any(x in os.path.basename(p).lower() for x in EXCLUDE):
            continue
        head = pd.read_csv(p, nrows=1)
        if "time" not in head:          # the oldest logs (before 28.09) have no wall clock: never steel
            continue
        t0 = pd.to_numeric(head.time, errors="coerce").iloc[0]
        if t0 > t_cal:
            out.append((t0, p))
    return [p for _, p in sorted(out)]


def print_hours(p):
    df = ev.read_log(p)
    on = df.print_state.fillna("").astype(str).values == "printing" if "print_state" in df else np.zeros(len(df), bool)
    dt = np.r_[0.0, np.diff(ev.mono_time(df.t.values))]
    return float(dt[on].sum() / 3600)


def labels():
    lab = pd.read_csv(os.path.join(ROOT, "benchmark", "ariza_ornekleri", "etiketler.csv"))
    lab["name"] = lab.dosya.map(os.path.basename)
    return lab[lab.beklenen != "-"]


def use(cal):
    """This worker's calibration file (None: the one in use); set on every job, workers are reused."""
    if cal:
        os.environ["TWIN_CALIBRATION"] = os.path.abspath(cal)
    else:
        os.environ.pop("TWIN_CALIBRATION", None)


def chain_job(args):
    """One model over the chain: pooled phase scores, GUC events outside fault labels, chain state per log."""
    cal, paths = args
    use(cal)
    sc = ev.pooled(ev.evaluate(None, paths, chain=True)).set_index("faz")
    m = ev.load_model()
    lab = labels()
    k, kfil, ref = 1.0, 1.0, 1.0
    starts, false_guc = [], []
    for p in paths:
        df = ev.read_log(p)
        starts.append((k, kfil, ref))
        evs, st = [], {}
        _, _, _, ks, _ = ev.replay(df, m, evs, k0=k, kfil0=kfil, state=st, k_ref0=ref)
        k, kfil, ref = float(ks[-1]), st["kfil"], st["k_ref"]
        mine = lab[lab.name == os.path.basename(p)]
        for code, ts, _ in evs:
            s = ts - df.t.iloc[0]
            if code.startswith("GUC") and not ((mine.baslangic_s - LABEL_SLACK <= s)
                                               & (s <= mine.bitis_s.fillna(np.inf))).any():
                false_guc.append((ev.short_name(p), code, round(s)))
    return sc, false_guc, starts


def slow_job(args):
    """slow_fault_test's injections into one log, the model starting from its own chain state."""
    import slow_fault_test as sf
    cal, path, (k0, kfil0, ref0) = args
    use(cal)
    m = ev.load_model()
    df = ev.read_log(path)
    if (np.diff(df.t.values) < 0).any():
        return []
    t, rows = df.t.values, []
    clean = sf.guc_events(df, m, k0, kfil0, ref0)
    for seg in sf.prints(df):
        end = t[seg[1] - 1]
        for where, i in sf.injections(df, seg).items():
            for watts, ramp in sf.FAULTS:
                if i is None or (end - t[i]) / 60 < ramp + sf.TAIL_MIN:
                    continue
                sub = df[df.t <= end + 120].copy()
                tt = sub.t.values
                frac = np.clip((tt - t[i]) / (ramp * 60), 0, 1) * ((tt >= t[i]) & (tt <= end))
                sub["pwm"] = np.maximum(sub.pwm.values - frac * watts / m.P_MAX, 0.0)
                hit = any(t[i] <= s <= end and not any(abs(s - s0) < 2 for _, s0 in clean)
                          for _, s in sf.guc_events(sub, m, k0, kfil0, ref0))
                rows.append((where, f"{watts:.0f} W / {ramp:.0f} min", hit))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("candidate", nargs="?", default=B_DEFAULT, help="model B's calibration file")
    ap.add_argument("--status", action="store_true", help="only the test logs and the print hours (no scores)")
    ap.add_argument("--logs", nargs="+", help="replay these instead (tool check, not the K74 test)")
    ap.add_argument("--force", action="store_true", help=f"score below {MIN_HOURS:g} print hours")
    a = ap.parse_args()
    paths = a.logs or test_logs()
    hours = [print_hours(p) for p in paths]
    print(f"{'tool check' if a.logs else 'K74 test data'}: {len(paths)} logs, {sum(hours):.2f} print hours")
    for p, h in zip(paths, hours):
        print(f"  {h:5.2f} h  {os.path.basename(p)}")
    if a.status:
        return
    if not a.logs and sum(hours) < MIN_HOURS and not a.force:
        sys.exit(f"{sum(hours):.2f} < {MIN_HOURS:g} print hours (K74): not scored yet")
    models = {"A (in use)": None, "B": a.candidate}
    with ProcessPoolExecutor(2) as pool:
        ch = dict(zip(models, pool.map(chain_job, [(c, paths) for c in models.values()])))
    with ProcessPoolExecutor(max(1, (os.cpu_count() or 2) - 1)) as pool:
        slow = {}
        for name, cal in models.items():
            jobs = [(cal, p, s) for p, s in zip(paths, ch[name][2])]
            slow[name] = pd.DataFrame([r for rs in pool.map(slow_job, jobs) for r in rs],
                                      columns=["where", "fault", "caught"])

    A, B = ch["A (in use)"][0], ch["B"][0]
    print("\nchain from k = 1, RMSE (bias) C: A -> B")
    for ph in A.index:
        if ph in B.index:
            print(f"  {ph:12s} {A.rmse[ph]:5.2f} ({A.bias[ph]:+5.2f}) -> {B.rmse[ph]:5.2f} ({B.bias[ph]:+5.2f})"
                  f"   d {B.rmse[ph] - A.rmse[ph]:+.2f}")
    for name in models:
        fg = ch[name][1]
        print(f"GUC outside fault labels, {name}: {len(fg)}  {fg}")
    print("slow faults caught / scenarios:")
    groups = {}
    for name, d in slow.items():
        for key, g in d.groupby(["where", "fault"]):
            groups.setdefault(key, {})[name] = (int(g.caught.sum()), len(g))
    for key, v in sorted(groups.items()):
        print(f"  {key[0]:7s} {key[1]:12s} " + "   ".join(f"{n}: {c}/{t}" for n, (c, t) in v.items()))

    fan = "tutma245fan"
    crit = [("1 TUMU at least 0.05 C better", B.rmse["TUMU"] <= A.rmse["TUMU"] - 0.05),
            ("2 fan hold at most 0.10 C worse", fan not in A.index or fan not in B.index
             or B.rmse[fan] <= A.rmse[fan] + 0.10),
            ("3 GUC outside labels not more", len(ch["B"][1]) <= len(ch["A (in use)"][1])),
            ("4 slow faults: no group fewer caught",
             all(v.get("B", (0, 0))[0] >= v.get("A (in use)", (0, 0))[0] for v in groups.values()))]
    print("\nK74 criteria" + (" (TOOL CHECK, not the K74 test)" if a.logs else "") + ":")
    for text, ok in crit:
        print(f"  {'PASS' if ok else 'FAIL'}  {text}")
    print("  -> B " + ("ACCEPTED (user approval next)" if all(ok for _, ok in crit) else "not accepted: A stays"))


if __name__ == "__main__":
    main()
