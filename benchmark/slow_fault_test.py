"""Synthetic slow-fault test (acceptance gate, criterion 5) [K17, K27, K54].

A slow fault is one the learner could take for the "new normal": k_fil drifts with it and the missing-power alarm never
fires. The test injects one into real prints: the logged heater power is lowered by a ramp (X W over R min), then held
lowered until the print ends - the same heat now reaches the hotend with less power, i.e. less heat goes into the print
(GUC_AZALDI direction: clog, filament not flowing). The log is replayed with the live twin's code from its live start
(the k/k_fil it was recorded with). Caught = a confirmed GUC_* event that starts after the injection and is not in
the clean replay of the same log; the delay is counted from the injection start.

Injection points per print (print_state == printing): when the part fan first turns on, and the print's midpoint;
a point is used only if the print runs at least ramp + TAIL_MIN after it. Faults: 3 W / 10 min, 4 W / 30 min.
Variants: "new" = the current code; "old" = printing threshold fixed at 230 C (the code before K54).
Analysis only: touches neither the model nor the alarms.

Usage (in dt/):   py benchmark/slow_fault_test.py [--set calibration|test] [--variants new,old] [--jobs N]
"""
import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import evaluate as ev  # noqa: E402

FAULTS = [(3.0, 10.0), (4.0, 30.0)]   # (W, ramp min)
TAIL_MIN = 5.0                          # min - the print must run at least ramp + this after the injection
MIN_PRINT_MIN = 10.0                    # min - shorter prints are skipped


def load(variant):
    m = ev.load_model()
    if variant == "old":
        m.hot_threshold = lambda target: 230.0
    return m


def prints(df):
    """(i0, i1) row ranges of contiguous print_state == printing; i1 exclusive."""
    if "print_state" not in df:
        return []
    on = (df.print_state.fillna("").astype(str).values == "printing").astype(int)
    edges = np.flatnonzero(np.diff(np.r_[0, on, 0]))
    t = df.t.values
    return [(a, b) for a, b in zip(edges[::2], edges[1::2]) if (t[b - 1] - t[a]) / 60 >= MIN_PRINT_MIN]


def injections(df, seg):
    """Injection rows of one print: fan-on and midpoint (None where there is no fan)."""
    a, b = seg
    t, fan = df.t.values, df.fan_state.values
    on = np.flatnonzero(fan[a:b] > 0)
    mid = a + int(np.searchsorted(t[a:b], (t[a] + t[b - 1]) / 2))
    return {"fan_on": a + int(on[0]) if len(on) else None, "mid": mid}


def guc_events(df, m, k0, kfil0, ref0):
    ev_list = []
    ev.replay(df, m, ev_list, k0=k0, kfil0=kfil0, k_ref0=ref0)
    return [(c, t0) for c, t0, _ in ev_list if c.startswith("GUC")]


def run_log(args):
    path, variants = args
    df = ev.read_log(path)
    if (np.diff(df.t.values) < 0).any():
        return []                       # clock went back: event times are ambiguous
    segs = prints(df)
    if not segs:
        return []
    k0, kfil0, ref0 = ev.live_start(df)
    t = df.t.values
    rows = []
    for variant in variants:
        m = load(variant)
        clean = guc_events(df, m, k0, kfil0, ref0)
        for si, seg in enumerate(segs):
            end = t[seg[1] - 1]
            for where, i in injections(df, seg).items():
                for watts, ramp in FAULTS:
                    row = dict(log=ev.short_name(path), print=si + 1, where=where, fault=f"{watts:.0f} W / {ramp:.0f} dk",
                               variant=variant)
                    if i is None or (end - t[i]) / 60 < ramp + TAIL_MIN:
                        continue
                    t0 = t[i]
                    sub = df[df.t <= end + 120].copy()          # the fault ends with the print; nothing after matters
                    tt = sub.t.values
                    frac = np.clip((tt - t0) / (ramp * 60), 0, 1) * ((tt >= t0) & (tt <= end))
                    sub["pwm"] = np.maximum(sub.pwm.values - frac * watts / m.P_MAX, 0.0)
                    hits = [s for c, s in guc_events(sub, m, k0, kfil0, ref0)
                            if t0 <= s <= end and not any(abs(s - s0) < 2 for _, s0 in clean)]
                    row["delay_min"] = (min(hits) - t0) / 60 if hits else np.nan
                    rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser(description="Synthetic slow-fault test (acceptance gate, criterion 5).")
    ap.add_argument("--set", choices=["calibration", "test"], default="calibration")
    ap.add_argument("--variants", default="new,old", help="comma list of new, old (old = fixed 230 C threshold)")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    a = ap.parse_args()
    variants = a.variants.split(",")
    paths = ev.set_paths(ev.SET_NAMES[a.set])
    with ProcessPoolExecutor(a.jobs) as pool:
        rows = [r for rs in pool.map(run_log, [(p, variants) for p in paths]) for r in rs]
    if not rows:
        print("print_state'li baski yok")
        return
    d = pd.DataFrame(rows)
    d["caught"] = d.delay_min.notna()
    d["result"] = [f"{x:.1f} dk" if x == x else "YUTULDU" for x in d.delay_min]
    wide = d.pivot_table(index=["log", "print", "where", "fault"], columns="variant", values="result", aggfunc="first")
    pd.set_option("display.width", 200)
    print(wide.to_string())
    print("\nOzet (yakalanan / senaryo, ortalama gecikme dk):")
    for (where, fault), g in d.groupby(["where", "fault"]):
        cells = []
        for v in variants:
            gv = g[g.variant == v]
            cells.append(f"{v}: {gv.caught.sum()}/{len(gv)}, {gv.delay_min.mean():.1f} dk")
        print(f"  {where:7s} {fault:10s} " + "   ".join(cells))
    if len(variants) == 2:
        p = d.pivot_table(index=["log", "print", "where", "fault"], columns="variant", values="caught", aggfunc="first")
        diff = p[p[variants[0]] != p[variants[1]]]
        print(f"\nSonucu degisen senaryo: {len(diff)}")
        if len(diff):
            print(diff.to_string())


if __name__ == "__main__":
    main()
