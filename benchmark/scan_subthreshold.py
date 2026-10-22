"""Sub-threshold missing-power episodes in print logs (candidate "yellow" level, not an alarm).

Replays every print log with the live twin's code (live start: the k/k_fil the log was recorded with) and lists the
stretches where the 60 s mean of the missing power stays beyond +-LEVEL W for at least 60 s while printing.
The official alarm threshold is ENERGY_THRESHOLD (2.5 W); episodes that also raised an official GUC alarm are marked.
Analysis only: touches neither the model nor the alarms.

--yellow applies the pre-registered yellow rule [K62] instead (frozen 2026-10-22; judged only on prints after that
date): PETG, an episode of the 60 s mean >= YELLOW_ARM W lasting >= YELLOW_DUR s that has reached YELLOW_PEAK W,
not counting the first YELLOW_GRACE s after a print's first extrusion; an episode where a GUC alarm came first is
not a yellow (GUC already said it). Prints the yellow events and the false-yellow rate per print hour.

Usage (in dt/):   py benchmark/scan_subthreshold.py [--level 1.0] [--extra FOLDER] [--yellow] [--since YYYY-MM-DD]
  --extra   another folder of logs (e.g. logs copied from the Pi)
  --since   only logs that start on or after this date (local time)
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

# Pre-registered yellow rule [K62] - do not tune: the numbers are judged on prints after 2026-10-22.
YELLOW_ARM = 1.0     # W - the episode runs while the 60 s mean stays at or above this (short dips allowed, below)
YELLOW_PEAK = 1.5    # W - and is a yellow once the 60 s mean has reached this
YELLOW_DUR = 70.0    # s - ... and the episode has lasted this long
YELLOW_GRACE = 90.0  # s - after a print's first extrusion: the first-layer start transient [K37, K49]
YELLOW_MATERIALS = ("PETG",)   # TPU does not separate (healthy 1.9-2.4 W, K49)
DIP_S, DIP_FRAC = 20.0, 0.8    # dips shorter than 20 s and above 80 % of the level do not split an episode


def replay(path, m):
    """Per-sample arrays of one log replayed with the live twin (None for short or corrupted logs)."""
    df = ev.read_log(path)
    if len(df) < 200 or df.T1_real.dtype == object:     # short, or the corrupted 2809 string log
        return None
    k0, kf0, r0 = ev.live_start(df)
    n = len(df)
    t, T, has_T = df.t.values, df.T1_real.values, df.has_T.values
    col = lambda c: pd.to_numeric(df[c], errors="coerce").values if c in df else np.full(n, np.nan)  # noqa: E731
    tg, bp = col("target"), col("bed_pwm")
    pe = pd.to_numeric(df.pos_e, errors="coerce").ffill().values if "pos_e" in df else np.full(n, np.nan)
    ps = df["print_state"].fillna("").astype(str).values if "print_state" in df else None
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
    mat = df["malzeme"].dropna().astype(str).unique().tolist() if "malzeme" in df else []
    if not mat:     # before 16.10 there is no malzeme column: PETG unless the file name says TPU
        mat = ["TPU" if "tpu" in os.path.basename(path).lower() else "PETG"]
    return dict(df=df, tm=tm, roll=roll, printing=printing, alarm=alarm, kfil=kfil, z=col("pos_z"),
                ext=df.extrude_velocity.values, ps=ps, mat=mat)


def runs(cond, roll, tm, level):
    """(i, j) index spans where cond holds; short dips above DIP_FRAC * level are bridged."""
    n, i = len(cond), 0
    while i < n:
        if not cond[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and (cond[j + 1] or (tm[j + 1] - tm[j] < DIP_S and not np.isnan(roll[j + 1])
                                            and roll[j + 1] >= DIP_FRAC * level)):
            j += 1
        yield i, j
        i = j + 1


def wall(df, i):
    return (pd.to_datetime(df.time.iloc[i], unit="s").tz_localize("UTC").tz_convert("Europe/Istanbul")
            .strftime("%m-%d %H:%M"))


def scan(path, m, level, rep=None):
    rep = rep or replay(path, m)
    if rep is None:
        return []
    df, tm, roll, printing = rep["df"], rep["tm"], rep["roll"], rep["printing"]
    rows = []
    for sign in (+1, -1):
        for i, j in runs((sign * roll >= level) & printing, sign * roll, tm, level):
            if tm[j] - tm[i] >= MIN_DUR:
                seg = slice(i, j + 1)
                rows.append(dict(
                    log=os.path.basename(path)[:58], t0_min=(tm[i] - tm[0]) / 60, wall=wall(df, i),
                    dur_s=tm[j] - tm[i], sign="+" if sign > 0 else "-",
                    peak=sign * float(np.nanmax(sign * roll[seg])), z0=rep["z"][i], z1=rep["z"][j],
                    official=bool(rep["alarm"][seg].any()),
                    kfil=f"{rep['kfil'][i]:.2f}->{rep['kfil'][j]:.2f}"))
    return rows


def after_grace(rep):
    """True where the current print has extruded for YELLOW_GRACE s (a resume from pause does not restart it)."""
    tm, pr, ext, ps = rep["tm"], rep["printing"], rep["ext"], rep["ps"]
    ok = np.zeros(len(tm), bool)
    t_first, prev = None, ""
    for i in range(len(tm)):
        state = ps[i] if ps is not None else ("printing" if pr[i] else "")
        if state == "printing" and prev not in ("printing", "paused"):
            t_first = None                                   # a new print starts
        if pr[i] and t_first is None and ext[i] > 0:
            t_first = tm[i]
        ok[i] = pr[i] and t_first is not None and tm[i] - t_first >= YELLOW_GRACE
        if state:
            prev = state
    return ok


def yellow(path, rep):
    """Yellow events of one replayed log [K62]: list of dicts; the event's time is the moment it would be raised."""
    if rep is None or not set(rep["mat"]) & set(YELLOW_MATERIALS):
        return []
    df, tm, roll, alarm = rep["df"], rep["tm"], rep["roll"], rep["alarm"]
    rows = []
    for i, j in runs((roll >= YELLOW_ARM) & after_grace(rep), roll, tm, YELLOW_ARM):
        peak = np.fmax.accumulate(np.nan_to_num(roll[i:j + 1], nan=-np.inf))
        hit = np.where((tm[i:j + 1] - tm[i] >= YELLOW_DUR) & (peak >= YELLOW_PEAK))[0]
        if not len(hit):
            continue
        k = i + hit[0]
        if alarm[i:k + 1].any():          # GUC came first: nothing to add
            continue
        rows.append(dict(log=os.path.basename(path)[:58], wall=wall(df, k), dur_s=round(tm[j] - tm[i]),
                         peak=round(float(np.nanmax(roll[i:j + 1])), 2), z=f"{rep['z'][i]:.1f}-{rep['z'][j]:.1f}",
                         t_s=round(float(df.t.iloc[i]) - float(df.t.iloc[0]))))
    return rows


def print_hours(rep):
    return float(np.sum(np.diff(rep["tm"])[rep["printing"][1:]])) / 3600


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--level", type=float, default=1.0, help="W, level of the 60 s mean (default 1.0)")
    ap.add_argument("--extra", help="another folder of logs")
    ap.add_argument("--yellow", action="store_true", help="apply the pre-registered yellow rule [K62]")
    ap.add_argument("--since", help="only logs starting on or after this date, YYYY-MM-DD (local time)")
    a = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(HERE, "loglar", "baskilar", "*.csv"))
                   + glob.glob(os.path.join(HERE, "loglar", "baskilar", "isimsiz", "*.csv"))
                   + glob.glob(os.path.join(HERE, "realtime_log_ws_onceki_*.csv"))
                   + (glob.glob(os.path.join(a.extra, "*.csv")) if a.extra else []))
    since = pd.Timestamp(a.since, tz="Europe/Istanbul").timestamp() if a.since else None
    key = lambda p: os.path.normcase(os.path.abspath(p))  # noqa: E731
    set_of = {key(os.path.join(HERE, r.dosya)): r.set
              for r in pd.read_csv(os.path.join(HERE, "benchmark", "setler.csv")).itertuples()}
    m = ev.load_model()
    seen, rows, hours = set(), [], []
    for p in paths:
        try:
            first = pd.read_csv(p, nrows=1)
            start = round(float(first.time.iloc[0]), 1)     # copies of a session start at the same moment
        except Exception:  # noqa: BLE001 - not a twin log (e.g. *_geometri.csv)
            continue
        if start in seen or (since is not None and start < since):     # the same session saved twice
            continue
        seen.add(start)
        if a.yellow:
            rep = replay(p, m)
            if rep is not None and set(rep["mat"]) & set(YELLOW_MATERIALS):
                hours.append(dict(set=set_of.get(key(p), "-"), h=print_hours(rep)))
                rows += [dict(r, set=set_of.get(key(p), "-")) for r in yellow(p, rep)]
        else:
            rows += scan(p, m, a.level)
        print(f"ok {os.path.basename(p)}", file=sys.stderr, flush=True)

    out = pd.DataFrame(rows)
    pd.set_option("display.width", 250); pd.set_option("display.max_rows", 1000)
    print(out.round(2).to_string() if len(out) else "no episodes")
    if a.yellow:
        h = pd.DataFrame(hours, columns=["set", "h"]).groupby("set").h.sum()
        n = out.groupby("set").size() if len(out) else pd.Series(dtype=int)
        print("\nyellow events per set (label each one real or healthy by hand; false-yellow rate = healthy / hours):")
        for s in h.index:
            print(f"  {s:6s} {int(n.get(s, 0)):3d} yellow in {h[s]:.1f} PETG print hours")
    elif len(out):
        mid = out[(out.z0 >= 2) & ~out.official]
        print(f"\n{len(out)} episodes, {int(out.official.sum())} with an official alarm; "
              f"mid-print (Z >= 2 mm) without an alarm: {len(mid)}")


if __name__ == "__main__":
    main()
