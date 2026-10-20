"""v0.2 alarm report: false-alarm rate per print hour, detection delay of labelled faults, shadow pauses.

Replays every archived log with the live twin's code (live start: the k/k_fil the log was recorded with) and collects
the confirmed missing-power alarms (GUC_*). Each alarm is matched against the fault labels
(benchmark/ariza_ornekleri/etiketler.csv):
  true    overlaps a labelled fault (beklenen != "-")
  false   overlaps a "no fault" label (beklenen == "-") or no label at all (listed, to be checked by hand)
A pause is what the live twin would request: GUC_AZALDI lasting PAUSE_AFTER s while printing (logs recorded before
print_state was logged count as printing). Logs, sets (benchmark/setler.csv) and labels are matched by the first
timestamp of the log, so a copy under another name is the same session. Analysis only: touches neither the model
nor the alarms. KARARLAR [K28] is the first, hand-made version of this report.

Usage (in dt/):   py benchmark/report_v02.py [--extra FOLDER] [--list] [--since YYYY-MM-DD]
  --extra   another folder of logs (e.g. new logs copied from the Pi)
  --list    print every alarm, not only the false and unlabelled ones
  --since   only sessions that start on or after this date (e.g. 2026-10-18: the current system, after the k_fil
            anchor; older logs replay with the k their twin had then, which was often far off [K37])
"""
import argparse
import glob
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import evaluate as ev  # noqa: E402

LABEL_FILE = os.path.join(HERE, "benchmark", "ariza_ornekleri", "etiketler.csv")
SET_FILE = os.path.join(HERE, "benchmark", "setler.csv")
LABEL_SLACK = 10.0      # s - an alarm starting this long before a fault label still belongs to it


def span(path):
    """(first, last) wall time and the first log time t of a log (None if not a twin log). A copy of a session,
    whole or cut, lies inside the original's wall-time span; labels are kept in log time t (eventtime), which
    a copy shares with its original."""
    try:
        d = pd.read_csv(path, usecols=["time"], low_memory=False)
        w = pd.to_numeric(d.time, errors="coerce").dropna()
        t0 = float(ev.read_log(path).t.iloc[0])
        return float(w.iloc[0]), float(w.iloc[-1]), t0
    except Exception:  # noqa: BLE001 - e.g. *_geometri.csv
        return None


def collect(extra):
    """Unique sessions: list of dict(path, set, labels); labels as (start_t, end_t, expected, observed) in log time.
    Copies are grouped by overlapping wall-time spans; the longest member is replayed (closest to the live run)."""
    pats = ["loglar/baskilar/*.csv", "loglar/baskilar/isimsiz/*.csv", "loglar/deneyler/*.csv",
            "benchmark/*.csv", "benchmark/ariza_ornekleri/*.csv"]
    paths = sorted(p for pat in pats for p in glob.glob(os.path.join(HERE, pat))
                   if not p.endswith(("etiketler.csv", "setler.csv")))
    if extra:
        paths += sorted(glob.glob(os.path.join(extra, "*.csv")))
    spans = {}
    for p in paths:
        s = span(p)
        if s is not None:
            spans[os.path.normcase(os.path.abspath(p))] = s
    groups = []                                       # [paths], members overlapping in wall time
    for p, (a, b, _) in sorted(spans.items(), key=lambda kv: kv[1][1] - kv[1][0], reverse=True):
        for g in groups:
            ga, gb, _ = spans[g[0]]
            if a < gb and b > ga and min(b, gb) - max(a, ga) >= 0.5 * (b - a):
                g.append(p)
                break
        else:
            groups.append([p])
    key = lambda p: os.path.normcase(os.path.abspath(p))  # noqa: E731
    set_of = {key(os.path.join(HERE, r.dosya)): r.set for r in pd.read_csv(SET_FILE).itertuples()}
    lab_of = {}
    for r in pd.read_csv(LABEL_FILE).itertuples():
        p = key(os.path.join(os.path.dirname(LABEL_FILE), r.dosya))
        if p in spans:
            t0 = spans[p][2]
            end = t0 + r.bitis_s if pd.notna(r.bitis_s) else 1e12
            lab_of.setdefault(p, []).append((t0 + r.baslangic_s, end, r.beklenen, r.gozlenen, r.set))
    out = []
    for g in groups:
        labels = [lb for p in g for lb in lab_of.get(p, [])]
        sets = [lb[4] for lb in labels] + [set_of[p] for p in g if p in set_of]
        out.append(dict(path=g[0], set=sets[0] if sets else "-", labels=labels))
    return out


def replay_log(path, m):
    df = ev.read_log(path)
    if len(df) < 200 or df.T1_real.dtype == object:     # short, or the corrupted 2809 string log
        return None
    k0, kf0, r0 = ev.live_start(df)
    events = []
    ev.replay(df, m, events, k0=k0, kfil0=kf0, k_ref0=r0)
    t = df.t.values
    tm = ev.mono_time(t)
    dt = np.r_[0.0, np.diff(tm)]
    has_ps = "print_state" in df and df.print_state.notna().any()
    printing = (df.print_state.fillna("").astype(str).values == "printing") if has_ps else np.zeros(len(df), bool)
    return dict(t0=t[0], t_end=t[-1], hours=tm[-1] / 3600 - tm[0] / 3600, print_h=dt[printing].sum() / 3600,
                has_ps=has_ps, t=t, printing=printing, events=events)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--extra", help="another folder of logs")
    ap.add_argument("--list", action="store_true", help="print every alarm")
    ap.add_argument("--since", help="only sessions starting on or after this date (YYYY-MM-DD, local time)")
    a = ap.parse_args()

    m = ev.load_model()
    confirm = m.ENERGY_CONFIRM
    sessions = collect(a.extra)
    if a.since:
        t_since = time.mktime(time.strptime(a.since, "%Y-%m-%d"))
        sessions = [s for s in sessions if span(s["path"])[0] >= t_since]
        print(f"sessions since {a.since}: {len(sessions)}", file=sys.stderr)
    logs, alarms, faults = [], [], []
    for s in sessions:
        r = replay_log(s["path"], m)
        if r is None:
            continue
        name = os.path.basename(s["path"])[:52]
        logs.append(dict(log=name, set=s["set"], hours=r["hours"], print_h=r["print_h"], has_ps=r["has_ps"]))
        fault_iv = s["labels"]                        # (start_t, end_t, expected, observed, set) in log time
        tm = ev.mono_time(r["t"])
        for code, t0, t1 in r["events"]:
            if not code.startswith("GUC"):
                continue
            end = r["t_end"] if t1 is None else t1
            hit = [f for f in fault_iv if t0 < f[1] and end > f[0] - LABEL_SLACK]
            if any(f[2] != "-" for f in hit):
                kind = "true"
            elif hit:
                kind = "false (labelled no fault)"
            else:
                kind = "false? (unlabelled)"
            # live rule: pause PAUSE_AFTER s after the start of a GUC_AZALDI that is still active, while printing
            t_pause = t0 + m.PAUSE_AFTER
            i_pause = min(np.searchsorted(r["t"], t_pause), len(r["t"]) - 1)
            pause = (code == "GUC_AZALDI" and end - t0 >= m.PAUSE_AFTER
                     and (not r["has_ps"] or r["printing"][i_pause]))
            hit_i = np.flatnonzero(r["t"] == t0)       # event time is a message time (right row if the clock went back)
            i0 = hit_i[0] if len(hit_i) else min(np.searchsorted(r["t"], t0), len(tm) - 1)
            alarms.append(dict(log=name, set=s["set"], code=code, start_s=tm[i0] - tm[0], dur_s=end - t0,
                               kind=kind, pause=pause, t_abs=t0))
        for f in fault_iv:
            if f[2] == "-":
                continue
            mine = [e for e in alarms if e["log"] == name and e["kind"] == "true"
                    and f[0] - LABEL_SLACK <= e["t_abs"] < f[1]]
            first = min(mine, key=lambda e: e["t_abs"]) if mine else None
            p = [e for e in mine if e["pause"]]
            faults.append(dict(log=name, set=f[4], observed=f[3][:40], expected=f[2],
                               alarm_delay_s=(first["t_abs"] + confirm - f[0]) if first else np.nan,
                               pause_delay_s=(min(e["t_abs"] for e in p) + m.PAUSE_AFTER - f[0]) if p else np.nan))
        print(f"ok {name}", file=sys.stderr, flush=True)

    L, A, F = pd.DataFrame(logs), pd.DataFrame(alarms), pd.DataFrame(faults)
    pd.set_option("display.width", 250); pd.set_option("display.max_rows", 1000)
    false = A[A.kind != "true"] if len(A) else A
    print(f"\n{len(L)} logs, {L.hours.sum():.1f} h; with print_state {int(L.has_ps.sum())} logs, "
          f"{L.print_h.sum():.1f} print hours\n")
    rows = []
    for name, sel in (("calibration (ayar)", L.set == "ayar"), ("test (sinav)", L.set == "sinav"),
                      ("no set", L.set == "-"), ("all", L.set.notna())):
        logs_s = set(L[sel].log)
        a_s = A[A.log.isin(logs_s)] if len(A) else A
        f_s = a_s[a_s.kind != "true"] if len(a_s) else a_s
        ph = L[sel].print_h.sum()
        f_print = f_s[[L.set_index("log").has_ps.get(lg, False) for lg in f_s.log]] if len(f_s) else f_s
        rows.append(dict(set=name, logs=int(sel.sum()), print_h=ph, alarms=len(a_s),
                         true=int((a_s.kind == "true").sum()) if len(a_s) else 0, false=len(f_s),
                         false_per_print_h=(len(f_print) / ph) if ph > 0 else np.nan,
                         pauses_true=int((a_s.pause & (a_s.kind == "true")).sum()) if len(a_s) else 0,
                         pauses_false=int((a_s.pause & (a_s.kind != "true")).sum()) if len(a_s) else 0))
    print(pd.DataFrame(rows).round(3).to_string(index=False))
    print("  false_per_print_h counts only false alarms in logs that have print_state (print hours are known).")

    print("\nLABELLED FAULTS (delay from the label start; alarm = start + ENERGY_CONFIRM, pause = start + PAUSE_AFTER)")
    print(F.round(0).to_string(index=False) if len(F) else "  none")
    if len(F):
        print(f"  caught {int(F.alarm_delay_s.notna().sum())}/{len(F)}, paused {int(F.pause_delay_s.notna().sum())}/"
              f"{len(F)}; alarm delay median {F.alarm_delay_s.median():.0f} s")

    show = A if a.list else false
    print(f"\n{'ALL ALARMS' if a.list else 'FALSE AND UNLABELLED ALARMS'} (check the unlabelled ones by hand)")
    print(show.drop(columns="t_abs").round(0).to_string(index=False) if len(show) else "  none")
    if len(A):
        az = A[A.code == "GUC_AZALDI"].dur_s
        print(f"\nGUC_AZALDI events {len(az)}: duration median {az.median():.0f} s, 90 % {az.quantile(.9):.0f} s, "
              f"95 % {az.quantile(.95):.0f} s; longest false {false.dur_s.max() if len(false) else 0:.0f} s "
              f"(pause after {m.PAUSE_AFTER:.0f} s)")


if __name__ == "__main__":
    main()
