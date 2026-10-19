"""Sub-threshold missing-power episodes in print logs (candidate "yellow" level, not an alarm).

Replays every print log with the live twin's code (live start: the k/k_fil the log was recorded with) and lists the
stretches where the 60 s mean of the missing power stays beyond +-LEVEL W for at least 60 s while printing.
The official alarm threshold is ENERGY_THRESHOLD (2.5 W); episodes that also raised an official GUC alarm are marked.
Analysis only: touches neither the model nor the alarms.

Usage (in dt/):   py benchmark/scan_subthreshold.py [--level 1.0] [--extra FOLDER]
  --extra   another folder of logs (e.g. logs copied from the Pi)
"""
import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import evaluate as ev  # noqa: E402

MIN_DUR = 60.0      # s - shortest episode listed
WINDOW = "60s"      # rolling mean of the missing power


def scan(path, m, level):
    df = ev.read_log(path)
    if len(df) < 200 or df.T1_real.dtype == object:     # short, or the corrupted 2809 string log
        return []
    k0, kf0, r0 = ev.live_start(df)
    n = len(df)
    t, T, has_T = df.t.values, df.T1_real.values, df.has_T.values
    col = lambda c: pd.to_numeric(df[c], errors="coerce").values if c in df else np.full(n, np.nan)  # noqa: E731
    tg, bp = col("target"), col("bed_pwm")
    pe = pd.to_numeric(df.pos_e, errors="coerce").ffill().values if "pos_e" in df else np.full(n, np.nan)
    ps = df["print_state"].fillna("").astype(str).values if "print_state" in df else None
    z = col("pos_z")
    tw = m.Twin(*m.initial_state(T[0], df.pwm[0], df.fan_state[0]), df.pwm[0], df.fan_state[0],
                df.extrude_velocity[0], k=k0, kfil=kf0, k_ref=r0)
    g = np.full(n, np.nan); alarm = np.zeros(n, bool); kfil = np.zeros(n)
    nan_none = lambda v: None if np.isnan(v) else v  # noqa: E731
    for i in range(n):
        r = tw.on_message(t[i], T[i] if has_T[i] else None, df.pwm.values[i], df.fan_state.values[i],
                          df.extrude_velocity.values[i], nan_none(tg[i]), nan_none(bp[i]), nan_none(pe[i]),
                          None if ps is None else ps[i])
        if r["eksik_guc"] is not None:
            g[i] = r["eksik_guc"]
        alarm[i] = any(c.startswith("GUC") for c in tw.faults.active)
        kfil[i] = tw.learner.kfil

    tm = ev.mono_time(t)
    roll = pd.Series(g, index=pd.to_datetime(tm, unit="s")).rolling(WINDOW, min_periods=40).mean().values
    printing = (ps == "printing") if ps is not None else (df.extrude_velocity.values > 0)
    rows = []
    for sign in (+1, -1):
        hit = (sign * roll >= level) & printing
        i = 0
        while i < n:
            if not hit[i]:
                i += 1
                continue
            j = i
            # short dips (< 20 s, still above 80 % of the level) do not split an episode
            while j + 1 < n and (hit[j + 1] or (tm[j + 1] - tm[j] < 20 and not np.isnan(roll[j + 1])
                                                and sign * roll[j + 1] >= 0.8 * level)):
                j += 1
            if tm[j] - tm[i] >= MIN_DUR:
                seg = slice(i, j + 1)
                rows.append(dict(
                    log=os.path.basename(path)[:58], t0_min=(tm[i] - tm[0]) / 60,
                    wall=pd.to_datetime(df.time.iloc[i], unit="s").tz_localize("UTC")
                         .tz_convert("Europe/Istanbul").strftime("%m-%d %H:%M"),
                    dur_s=tm[j] - tm[i], sign="+" if sign > 0 else "-",
                    peak=sign * float(np.nanmax(sign * roll[seg])), z0=z[i], z1=z[j],
                    official=bool(alarm[seg].any()), kfil=f"{kfil[i]:.2f}->{kfil[j]:.2f}"))
            i = j + 1
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--level", type=float, default=1.0, help="W, level of the 60 s mean (default 1.0)")
    ap.add_argument("--extra", help="another folder of logs")
    a = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(HERE, "loglar", "baskilar", "*.csv"))
                   + glob.glob(os.path.join(HERE, "loglar", "baskilar", "isimsiz", "*.csv"))
                   + glob.glob(os.path.join(HERE, "realtime_log_ws_onceki_*.csv"))
                   + (glob.glob(os.path.join(a.extra, "*.csv")) if a.extra else []))
    m = ev.load_model()
    seen, rows = set(), []
    for p in paths:
        try:
            first = pd.read_csv(p, nrows=1)
            key = round(float(first.time.iloc[0]), 1)     # copies of a session start at the same moment
        except Exception:  # noqa: BLE001 - not a twin log (e.g. *_geometri.csv)
            continue
        if key in seen:                      # the same session saved twice
            continue
        seen.add(key)
        rows += scan(p, m, a.level)
        print(f"ok {os.path.basename(p)}", file=sys.stderr, flush=True)

    out = pd.DataFrame(rows)
    pd.set_option("display.width", 250); pd.set_option("display.max_rows", 1000)
    print(out.round(2).to_string() if len(out) else "no episodes")
    if len(out):
        mid = out[(out.z0 >= 2) & ~out.official]
        print(f"\n{len(out)} episodes, {int(out.official.sum())} with an official alarm; "
              f"mid-print (Z >= 2 mm) without an alarm: {len(mid)}")


if __name__ == "__main__":
    main()
