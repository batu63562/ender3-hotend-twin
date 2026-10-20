"""
Benchmark evaluation.

Replays the model in realtime_websocket.py over the logs in benchmark/ EXACTLY LIKE THE LIVE SCRIPT
(same model_step / alarm logic / k_loss learning, same loop logic) and prints a phase-by-phase
score table for each log.

Phases are determined from REAL data only (pwm, fan, T1_real), independent of the model:
    isinma      heat-up: heater at 100 % (pwm >= 0.95 averaged over the last 3 s)
    varis       arrival: the first 30 s after a heat-up ends (overshoot / settling)
    tutma245    hold around 245 C (T > 235, T range < 3 C over the last 30 s), fan off
    tutma245fan the same, fan on
    tutma_diger holds at other temperatures (150, 180, 220 ...)
    soguma      cool-down: heater off for 15 s, T >= 35 C
    bosta       idle: heater off for 15 s, T < 35 C
    gecis       everything else (target changes etc.)

Residual sign is the same as in the live script: residual = T1_real - T1_model
(+ = reality hotter than the model, - = model hotter than reality).

Usage:
    python evaluate.py                                  # score table (September runs)
    python evaluate.py --save baseline                  # benchmark/skorlar/baseline.csv
    python evaluate.py --compare baseline               # difference from a saved score
    python evaluate.py --set test                       # the test logs in benchmark/setler.csv
    python evaluate.py --set test --live-start          # each log with the k it had when recorded (README)

Sets (benchmark/setler.csv): 'ayar' (calibration) = logs looked at while tuning parameters/thresholds,
'sinav' (test) = logs never used for tuning, frozen for the score table only (since 2026-10-09).
Rule: a model change is never tuned by looking at the test logs; new prints alternate between the sets.
A warning is printed if a test file has changed (fingerprint mismatch).
From another script (parameter trial):
    from evaluate import evaluate, print_scorecard
    print_scorecard(evaluate(overrides={"HA1": 0.04}))
"""
import argparse
import glob
import importlib
import os

import numpy as np
import pandas as pd

import realtime_websocket as rw

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH_DIR = os.path.join(HERE, "benchmark")
SCORE_DIR = os.path.join(BENCH_DIR, "skorlar")
SET_FILE = os.path.join(BENCH_DIR, "setler.csv")
PHASES = ["bosta", "isinma", "varis", "tutma245", "tutma245fan", "tutma_diger", "soguma", "gecis"]


# ---------------------------------------------------------------------------
# Reading logs and labelling phases
# ---------------------------------------------------------------------------
def read_log(path):
    return prepare_log(pd.read_csv(path))


def prepare_log(df):
    """Prepares the table coming from a CSV or from the live script's in-memory rows."""
    # a power cut can leave a NUL-filled block at the end of the file: a row without any time is dropped
    has_time = pd.to_numeric(df.time, errors="coerce").notna()
    if "eventtime" in df:
        has_time |= pd.to_numeric(df.eventtime, errors="coerce").notna()
    df = df[has_time]
    if "eventtime" in df:
        df["eventtime"] = pd.to_numeric(df.eventtime, errors="coerce")
    klipper = "eventtime" in df and df.eventtime.notna().all()
    df["t"] = df.eventtime if klipper else df.time
    df["clock"] = "klipper" if klipper else "pc"
    df["extrude_velocity"] = df.extrude_velocity.fillna(0.0)
    df["fan_state"] = df.fan_state.fillna(0.0)
    df["pwm"] = df.pwm.fillna(0.0)
    # new logs record every message; rows with T_yeni=1 carry a new temperature.
    # in old logs every row was a temperature message.
    df["has_T"] = (df.T_yeni == 1) if "T_yeni" in df else True
    return df.reset_index(drop=True)


def mono_time(t):
    """If Klipper restarts, eventtime goes backwards (10.10 cancellations log); monotonic time for rolling
    windows: backward steps count as 0. For monotonic logs t itself (identical result)."""
    t = np.asarray(t, dtype=float)
    d = np.diff(t, prepend=t[0])
    if (d >= 0).all():
        return t
    return t[0] + np.cumsum(np.maximum(d, 0.0))


def label_phases(df):
    """Phase label for every sample (from real data only)."""
    tm = mono_time(df.t.values)
    s = df.set_index(pd.to_datetime(tm, unit="s"))
    pwm_mean3 = s.pwm.rolling("3s").mean().values
    pwm_max15 = s.pwm.rolling("15s").max().values
    T_range30 = (s.T1_real.rolling("30s").max() - s.T1_real.rolling("30s").min()).values
    # the latest REAL heat-up moment (100 % for 3 s). Single-sample pwm=1 spikes are normal for the PID
    # during a hold; a single pwm>=0.99 is not used so that they do not count as heat-up.
    heat_t = pd.Series(tm).where(pd.Series(pwm_mean3 >= 0.95)).ffill().values
    since_sat = tm - np.nan_to_num(heat_t, nan=-1e12)

    T, fan = df.T1_real.values, df.fan_state.values
    ph = np.full(len(df), "gecis", dtype=object)
    hold = (T_range30 < 3.0) & (T > 100)
    ph[hold & (T <= 235)] = "tutma_diger"
    ph[hold & (T > 235) & (fan == 0)] = "tutma245"
    ph[hold & (T > 235) & (fan > 0)] = "tutma245fan"
    ph[since_sat < 30.0] = "varis"
    off = pwm_max15 <= 0.01
    ph[off & (T >= 35)] = "soguma"
    ph[off & (T < 35)] = "bosta"
    ph[pwm_mean3 >= 0.95] = "isinma"
    return ph


# ---------------------------------------------------------------------------
# Replaying the live run: the SAME Twin class as the live script is used
# ---------------------------------------------------------------------------
def replay(df, m, events=None, k0=1.0, kfil0=1.0, state=None, k_ref0=None):
    """m: the realtime_websocket module (its parameters may be overridden).
    events: if a list is given, confirmed fault events (code, start, end|None) are appended.
    state: if a dict is given, the final filament factor and loss-alarm reference are written (state["kfil"],
    state["k_ref"], row by row state["k_refs"]); for chaining. k_ref0: inherited reference (None: k0).
    Row 0 is the first message of the live script: the model starts fresh here (no saved state).
    The logged pwm/fan/extrusion are the values valid AFTER that message; the Twin applies them to the
    next interval - the same order as the live run."""
    t, T, has_T = df.t.values, df.T1_real.values, df.has_T.values
    pwm, fan, ext = df.pwm.values, df.fan_state.values, df.extrude_velocity.values
    n = len(df)
    T1m = np.empty(n); res = np.zeros(n); anom = np.zeros(n, bool); ks = np.ones(n); synced = np.zeros(n, bool)
    refs = np.ones(n)

    # k0: the loss factor the live script inherited from the previous session (default: fresh start)
    twin = m.Twin(*m.initial_state(T[0], pwm[0], fan[0]), pwm[0], fan[0], ext[0], k=k0, kfil=kfil0, k_ref=k_ref0)
    # target temperature (logged since 2026-09-28) only affects the early warning
    tg = pd.to_numeric(df.target, errors="coerce").values if "target" in df else np.full(n, np.nan)
    # bed heater power (logged since 2026-09-28): supply voltage of the nozzle cartridge
    bp = pd.to_numeric(df.bed_pwm, errors="coerce").values if "bed_pwm" in df else np.full(n, np.nan)
    # extruder position (logged since 2026-09-29): last known value as in the live script;
    # flow is computed from it, otherwise from the instantaneous speed
    pe = pd.to_numeric(df.pos_e, errors="coerce").ffill().values if "pos_e" in df else np.full(n, np.nan)
    # material (logged since 2026-10-16): the live twin reads it from the G-code shortly after the print starts;
    # a change restarts k_fil from the anchor (Twin.set_material)
    mz = df["malzeme"].fillna("").astype(str).values if "malzeme" in df else None
    # print state (logged since 2026-10-11): a new print restarts k_fil from the anchor, as in the live twin
    ps = df["print_state"].fillna("").astype(str).values if "print_state" in df else None
    if mz is not None:
        twin.material = mz[0] or None
    for i in range(n):
        if mz is not None and mz[i] and mz[i] != twin.material:
            twin.set_material(mz[i])
        r = twin.on_message(t[i], T[i] if has_T[i] else None, pwm[i], fan[i], ext[i],
                            None if np.isnan(tg[i]) else tg[i], None if np.isnan(bp[i]) else bp[i],
                            None if np.isnan(pe[i]) else pe[i], None if ps is None else ps[i])
        T1m[i] = twin.T1_model; ks[i] = twin.learner.k; refs[i] = twin.k_ref
        synced[i] = r["senkron"]; anom[i] = r["anomali"]
        res[i] = r["residual"] if r["residual"] is not None else 0.0
    if events is not None:
        events.extend(twin.faults.events + [(c, t0, None) for c, t0 in twin.faults.active.items()])
    if state is not None:
        state["kfil"], state["k_ref"], state["k_refs"] = twin.learner.kfil, twin.k_ref, refs
    return T1m, res, anom, ks, synced


def load_model(overrides=None):
    m = importlib.reload(rw)
    for k, v in (overrides or {}).items():
        if not hasattr(m, k):
            raise KeyError(f"realtime_websocket.py'de '{k}' yok")
        setattr(m, k, v)
    m.T_AMB_K = m.T_AMB + 273.15
    return m


def short_name(path):
    p = os.path.basename(path).replace(".csv", "").split("_")
    if len(p[0]) == 10 and len(p) >= 3:          # 2026-09-25_1443_klipper_...
        return f"{p[0][5:]} {p[1]} {p[2]}"
    return " ".join(p)[:22]                       # 0810_sonunda_basarili, deney7_...


def set_paths(name):
    """Log paths of one set in benchmark/setler.csv ('hepsi' = all). Test files are fingerprinted:
    a warning is printed if a frozen log has changed since."""
    import hashlib
    s = pd.read_csv(SET_FILE)
    if name != "hepsi":
        s = s[s.set == name]
    paths = []
    for r in s.itertuples():
        p = os.path.join(HERE, r.dosya)
        if not os.path.exists(p):
            print(f"!! setteki log yok: {r.dosya}")
            continue
        if r.set == "sinav":
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for b in iter(lambda: f.read(1 << 20), b""):
                    h.update(b)
            if h.hexdigest()[:16] != r.sha256_16:
                print(f"!! sinav logu degismis (parmak izi tutmuyor): {r.dosya}")
        paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def live_start(df):
    """Learned state when the log was recorded: k_loss / k_fil of the first row (1.0 without the column).
    The loss-alarm reference is not logged; it is taken equal to k (no alarm at session start)."""
    def first(col):
        v = pd.to_numeric(df[col], errors="coerce").dropna() if col in df else pd.Series(dtype=float)
        return float(v.iloc[0]) if len(v) else 1.0
    k = first("k_loss")
    return k, first("k_fil"), k


def evaluate(overrides=None, paths=None, chain=False, live=False):
    """Score DataFrame in long format: log, clock, faz, n, sure_s, rmse, bias, max, anomali.
    chain=True: the logs are consecutive sessions in the given order, each inherits the previous one's final k
    (the live script's memory); live=True: each log starts with the k/k_fil it had when recorded
    ("live start"; acceptance gate and README scores); both False: each log starts fresh with k=1."""
    m = load_model(overrides)
    paths = paths or sorted(p for p in glob.glob(os.path.join(BENCH_DIR, "*.csv")) if p != SET_FILE)
    rows = []
    k_carry, kfil_carry, ref_carry = 1.0, 1.0, 1.0
    for p in paths:
        df = read_log(p)
        events, st = [], {}
        k_start, kfil_start, ref_start = (k_carry, kfil_carry, ref_carry) if chain else \
            live_start(df) if live else (1.0, 1.0, 1.0)
        _, res, anom, ks, synced = replay(df, m, events, k0=k_start, kfil0=kfil_start, state=st, k_ref0=ref_start)
        k_carry, kfil_carry, ref_carry = float(ks[-1]), st["kfil"], st["k_ref"]
        kanom = abs(ks - st["k_refs"]) > m.K_ALARM_BAND   # learned loss outside the band around the slow reference
        fault_start = np.zeros(len(df), bool)          # rows where a confirmed fault started
        for _, t0, _ in events:
            hit = np.flatnonzero(df.t.values == t0)    # event time is a message time (right row even if the clock went back)
            fault_start[hit[0] if len(hit) else min(np.searchsorted(df.t.values, t0), len(df) - 1)] = True
        ph = label_phases(df)
        dt = np.r_[0.0, np.diff(mono_time(df.t.values))]
        valid = ~synced & df.has_T.values        # error only on rows with a new temperature
        valid[0] = False
        for phase in PHASES + ["TUMU"]:
            in_phase = (ph == phase) if phase != "TUMU" else np.ones(len(df), bool)
            mask = valid & in_phase
            if not mask.any():
                continue
            r = res[mask]
            rows.append(dict(log=short_name(p), clock=df.clock[0], faz=phase, n=int(mask.sum()),
                             sure_s=float(dt[in_phase & ~synced].sum()), rmse=float(np.sqrt(np.mean(r ** 2))),
                             bias=float(r.mean()), max=float(np.abs(r).max()),
                             anomali=int(anom[mask].sum()), k_anomali=int(kanom[mask].sum()),
                             ariza=int(fault_start[in_phase].sum()), k_son=float(ks[-1]), kfil_son=kfil_carry))
    return pd.DataFrame(rows)


def pooled(sc, clock=None):
    """Logs pooled, per phase (RMSEs combined weighted by sample count)."""
    s = sc if clock is None else sc[sc.clock == clock]
    out = []
    for phase in PHASES + ["TUMU"]:
        g = s[s.faz == phase]
        if len(g):
            out.append(dict(faz=phase, n=int(g.n.sum()), sure_s=g.sure_s.sum(),
                            rmse=float(np.sqrt((g.rmse ** 2 * g.n).sum() / g.n.sum())),
                            bias=float((g.bias * g.n).sum() / g.n.sum()), max=g["max"].max(),
                            anomali=int(g.anomali.sum()),
                            k_anomali=int(g.k_anomali.sum()) if "k_anomali" in g else 0,
                            ariza=int(g.ariza.sum()) if "ariza" in g else 0))
    return pd.DataFrame(out)


def print_scorecard(sc):
    cols = [p for p in PHASES + ["TUMU"] if p in set(sc.faz)]
    print("RMSE (bias) [C] - residual = gercek - model;  '-' = bu fazda veri yok\n")
    head = f"{'log':22s}{'saat':8s}" + "".join(f"{c:>15s}" for c in cols)
    print(head); print("-" * len(head))
    for log, g in sc.groupby("log", sort=False):
        cells = {r.faz: f"{r.rmse:5.2f} ({r.bias:+5.1f})" for r in g.itertuples()}
        print(f"{log:22s}{g.clock.iloc[0]:8s}" + "".join(f"{cells.get(c, '-'):>15s}" for c in cols))

    for title, clock in (("KLIPPER ZAMANLI LOGLAR (karar icin esas)", "klipper"), ("TUM LOGLAR", None)):
        p = pooled(sc, clock)
        if not len(p):
            continue
        print(f"\n{title}")
        print(f"  {'faz':12s}{'sure':>8s}{'RMSE':>8s}{'bias':>8s}{'max':>8s}{'anomali':>9s}{'k_anom':>8s}{'ariza':>7s}")
        for r in p.itertuples():
            print(f"  {r.faz:12s}{r.sure_s:7.0f}s{r.rmse:8.2f}{r.bias:+8.2f}{r.max:8.2f}{r.anomali:9d}"
                  f"{r.k_anomali:8d}{r.ariza:7d}")


def print_comparison(sc, ref, ref_name):
    print(f"\nFARK: simdiki - '{ref_name}'  (RMSE, negatif = iyilesme)\n")
    cols = [p for p in PHASES + ["TUMU"] if p in set(sc.faz)]
    merged = sc.merge(ref, on=["log", "faz"], suffixes=("", "_ref"), how="left")
    head = f"{'log':22s}" + "".join(f"{c:>13s}" for c in cols)
    print(head); print("-" * len(head))
    for log, g in merged.groupby("log", sort=False):
        cells = {r.faz: (f"{r.rmse - r.rmse_ref:+6.2f}" if pd.notna(r.rmse_ref) else "yeni") for r in g.itertuples()}
        print(f"{log:22s}" + "".join(f"{cells.get(c, '-'):>13s}" for c in cols))
    for clock, title in (("klipper", "klipper"), (None, "tum")):
        a, b = pooled(sc, clock), pooled(ref, clock)
        if len(a) and len(b):
            j = a.merge(b, on="faz", suffixes=("", "_ref"))
            print(f"  birlesik ({title}): " + "  ".join(f"{r.faz} {r.rmse - r.rmse_ref:+.2f}" for r in j.itertuples()))


# Set names on the command line -> values in setler.csv (the data contract stays Turkish)
SET_NAMES = {"calibration": "ayar", "test": "sinav", "all": "hepsi"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Replay logs through the live twin code and print phase scores.")
    ap.add_argument("--save", metavar="NAME", help="save the scores as benchmark/skorlar/NAME.csv")
    ap.add_argument("--compare", metavar="NAME", help="compare with benchmark/skorlar/NAME.csv")
    ap.add_argument("--set", choices=list(SET_NAMES),
                    help="log set from benchmark/setler.csv (default: only the September runs benchmark/*.csv)")
    ap.add_argument("--chain", action="store_true",
                    help="treat logs as consecutive sessions: each starts with the k learned by the previous one")
    ap.add_argument("--live-start", action="store_true",
                    help="each log starts with the k / k_fil the live twin had when it was recorded (README scores)")
    a = ap.parse_args()
    if a.chain and a.live_start:
        ap.error("--chain and --live-start cannot be combined")

    sc = evaluate(paths=set_paths(SET_NAMES[a.set]) if a.set else None, chain=a.chain, live=a.live_start)
    print_scorecard(sc)
    if a.compare:
        print_comparison(sc, pd.read_csv(os.path.join(SCORE_DIR, a.compare + ".csv")), a.compare)
    if a.save:
        os.makedirs(SCORE_DIR, exist_ok=True)
        sc.to_csv(os.path.join(SCORE_DIR, a.save + ".csv"), index=False)
        print(f"\nKaydedildi: benchmark/skorlar/{a.save}.csv")
