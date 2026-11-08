"""Retro scan of the pause rule [K89]: current (GUC_AZALDI PAUSE_AFTER s uninterrupted) vs cumulative
(GUC_AZALDI on-time within the last W s >= X s, while printing). Analysis only: touches neither model nor alarms.

Two GUC_AZALDI timelines per session: the replay with today's code (report_v02: live start, but k_ref = k, so an
IDLE_K_REVERT at print start [K75] is not reproduced) and the live one from the CSV (anomali + ariza), used only for
logs recorded with the current model version. A live pause sets print_state to paused at the trigger instant, so a
real pause can show as "-" or just past the label end.

Usage (in dt/):
  py benchmark/pause_rule_scan.py collect PI_LOG_FOLDER OUT.pkl    # slow: replays every session
  py benchmark/pause_rule_scan.py rules OUT.pkl                    # fast: compares the rules
"""
import os
import pickle
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "benchmark"))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import evaluate as ev  # noqa: E402
import report_v02 as rv  # noqa: E402

SLACK = rv.LABEL_SLACK


def collect(extra, out):
    m = ev.load_model()
    sessions = rv.collect(extra)
    res = []
    for s in sessions:
        p = s["path"]
        try:
            df = ev.read_log(p)
        except Exception as e:  # noqa: BLE001
            print("skip", p, e, file=sys.stderr)
            continue
        if len(df) < 200 or df.T1_real.dtype == object:
            continue
        r = rv.replay_log(p, m)
        if r is None:
            continue
        t = r["t"]
        tm = ev.mono_time(t)
        n = len(t)
        # replay timeline: GUC_AZALDI on per row
        rep_on = np.zeros(n, bool)
        for code, t0, t1 in r["events"]:
            if code == "GUC_AZALDI":
                end = r["t_end"] if t1 is None else t1
                rep_on |= (t >= t0) & (t < end)
        # live timeline
        live_on = None
        if "ariza" in df and "anomali" in df:
            live_on = (df["anomali"].astype(str).eq("True")
                       & df["ariza"].fillna("").astype(str).str.contains("GUC_AZALDI")).values
        cv = ""
        if "code_version" in df:
            v = df["code_version"].dropna().astype(str)
            cv = v.iloc[0] if len(v) else ""
        res.append(dict(path=p, set=s["set"], labels=s["labels"], t=t, tm=tm, printing=r["printing"],
                        has_ps=r["has_ps"], rep_on=rep_on, live_on=live_on, cv=cv,
                        wall0=float(pd.to_numeric(df["time"], errors="coerce").dropna().iloc[0])))
        print("ok", os.path.basename(p), file=sys.stderr, flush=True)
    with open(out, "wb") as f:
        pickle.dump(res, f)


def triggers(on, tm, printing, has_ps, rule):
    """Times (index) where the rule requests a pause; one request per episode."""
    pr = printing if has_ps else np.ones(len(tm), bool)
    dt = np.r_[0.0, np.diff(tm)]
    dt = np.clip(dt, 0, 2.0)                    # a data gap does not count as alarm time
    on = on & pr
    out = []
    if rule[0] == "cont":
        X = rule[1]
        t_start, flagged = None, False
        for i in range(len(tm)):
            if on[i]:
                if t_start is None:
                    t_start, flagged = tm[i], False
                if not flagged and tm[i] - t_start >= X:
                    out.append(i); flagged = True
            else:
                t_start = None
        return out
    _, W, X = rule
    c = np.cumsum(dt * on)                       # on-time integral
    j, flagged = 0, False
    for i in range(len(tm)):
        while tm[j] < tm[i] - W:
            j += 1
        acc = c[i] - c[j]
        if not flagged and acc >= X and pr[i]:
            out.append(i); flagged = True
        elif flagged and acc == 0:
            flagged = False                     # episode over: W s without any alarm
    return out


def rules(pkl):
    res = pickle.load(open(pkl, "rb"))
    grid = [("cont", 150)] + [("cum", W, X) for W in (300, 600) for X in (120, 150, 180)]
    name = lambda r: "cont150" if r[0] == "cont" else f"cum{r[1]}/{r[2]}"  # noqa: E731
    for src in ("rep_on", "live_on"):
        rows, faults = [], []
        print_h = 0.0
        for s in res:
            on = s[src]
            if on is None:
                continue
            if src == "live_on" and "gecikenkutle-v4" not in s["cv"]:
                continue                        # live alarms only from logs recorded with the current model
            tm, t = s["tm"], s["t"]
            pr = s["printing"] if s["has_ps"] else np.ones(len(tm), bool)
            print_h += np.clip(np.r_[0, np.diff(tm)], 0, 2)[pr].sum() / 3600
            fl = [f for f in s["labels"] if f[2] != "-"]
            for f in fl:
                faults.append(dict(log=os.path.basename(s["path"])[:44], exp=f[2], obs=f[3][:30], key=(s["path"], f[0])))
            for rule in grid:
                for i in triggers(on, tm, s["printing"], s["has_ps"], rule):
                    hit = [f for f in s["labels"] if f[0] - SLACK <= t[i] <= f[1]]
                    kind = "true" if any(f[2] != "-" for f in hit) else ("false(label-)" if hit else "false?")
                    fk = next(((s["path"], f[0]) for f in hit if f[2] != "-"), None)
                    f0 = next((f[0] for f in hit if f[2] != "-"), None)
                    rows.append(dict(rule=name(rule), log=os.path.basename(s["path"])[:44], kind=kind,
                                     t_min=round((tm[i] - tm[0]) / 60, 1), fault=fk,
                                     delay_s=(t[i] - f0) if f0 is not None else np.nan,
                                     wall=pd.to_datetime(s["wall0"] + tm[i] - tm[0], unit="s")
                                     .tz_localize("UTC").tz_convert("Europe/Istanbul").strftime("%m-%d %H:%M")))
        R = pd.DataFrame(rows)
        F = pd.DataFrame(faults)
        print(f"\n===== source {src}: {print_h:.1f} print hours, {len(F)} labelled faults")
        summ = []
        for rule in grid:
            nm = name(rule)
            r = R[R.rule == nm] if len(R) else R
            paused = set(r[r.kind == "true"].fault) if len(r) else set()
            summ.append(dict(rule=nm, faults_paused=f"{len(paused)}/{len(F)}",
                             delay_med_s=r[r.kind == "true"].groupby("fault").delay_s.min().median() if len(r) else np.nan,
                             false_labelled=int((r.kind == "false(label-)").sum()) if len(r) else 0,
                             false_unlabelled=int((r.kind == "false?").sum()) if len(r) else 0))
        pd.set_option("display.width", 250); pd.set_option("display.max_rows", 500)
        print(pd.DataFrame(summ).round(0).to_string(index=False))
        # faults: which rules paused each
        if len(F):
            for nm in [name(g) for g in grid]:
                r = R[(R.rule == nm) & (R.kind == "true")] if len(R) else R
                F[nm] = [("%.0f" % r[r.fault == k].delay_s.min()) if (len(r) and (r.fault == k).any()) else "-"
                         for k in F.key]
            print(F.drop(columns="key").to_string(index=False))
        if len(R):
            print("\nfalse pauses (check by hand):")
            fx = R[R.kind != "true"].drop(columns=["fault", "delay_s"])
            print(fx.sort_values(["log", "t_min", "rule"]).to_string(index=False) if len(fx) else "  none")
            # new true pauses vs cont150
            print("\ntrue pauses gained/lost vs cont150:")
            base = set(R[(R.rule == "cont150") & (R.kind == "true")].fault)
            for nm in [name(g) for g in grid][1:]:
                got = set(R[(R.rule == nm) & (R.kind == "true")].fault)
                print(f"  {nm:12s} +{[os.path.basename(k[0])[:34] for k in got - base]}  -{[os.path.basename(k[0])[:34] for k in base - got]}")


if __name__ == "__main__":
    if sys.argv[1] == "collect":
        collect(sys.argv[2], sys.argv[3])
    else:
        rules(sys.argv[2])
