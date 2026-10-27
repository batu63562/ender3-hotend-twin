"""Fit the hotend model's thermal parameters to calibration logs (v0.3 groundwork: system identification).

The live twin's own model (realtime_websocket.model_step) is run open-loop on the logged heater power and fan
of each log, and the chosen parameters are fitted so that the model's T1 follows the measured temperature
(least squares on the T1 residual). Only logs without extrusion fit this (holds, steps, free cooling: deney9,
deney14a/b); the filament term is not fitted here. Each log gets its own k (the session-to-session loss level the
live twin learns, e.g. 0.97 vs 1.08 on different days), fitted together with the shared physical parameters;
without it the open-loop error of a wrong k piles up over a 40 min log and is pushed into the physical values.

Besides the fitted values it reports how well each parameter is pinned down by the data (identifiability): the
approximate standard error from the Jacobian, the parameters the data carry no information about, and the pairs
the data cannot tell apart (|r| > 0.9).

--synthetic replaces the measured temperature by the current model's own output (k = 1, plus thermistor
rounding) and starts the fit from perturbed values: the fit must find the current values again. This checks the
tool, not the printer.

--write PATH saves the result as a calibration file (the format the twin reads as calibration.json): every
calibrated constant, the fitted ones replaced, the others as currently in effect (code <- profile.toml <-
calibration.json), so `--params ""` reproduces today's values exactly. It never replaces an existing file unless
--force is given, and it never writes the live calibration.json by itself: a fitted file is a model change and
goes through the acceptance gate before it is copied there.
Otherwise analysis only: touches neither the live twin nor its parameters.
Needs scipy (not needed by the live twin).

--staged calibrates a hotend from its own calibration print (yazici/calibration_print.py) in two stages, k = 1:
losses and part-fan curve from the steady holds, then capacities and resistances from the trajectory (see staged()).
Checked on virtual prints of the calibration G-code (yazici/virtual_print.py): recovers the model's own values exactly.

Usage (in dt/):   py calibrate.py LOG [LOG ...] [--params C1,C3,R31,...] [--rate 1.0] [--synthetic]
                                [--write PATH [--force]]
                  py calibrate.py CALIBRATION_LOG --staged --write calibration_new.json
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import evaluate as ev  # noqa: E402
import realtime_websocket as rw  # noqa: E402

ALL_PARAMS = ["C1", "C2", "C3", "R31", "R_HB", "HA1", "CRAD", "HA2", "HA2_FAN", "CAP_T_COEF"]
# the set the data can pin down (K39): one factor for all capacities, R31 with C3 fixed (= the cartridge time
# constant), R_HB, the loss shape over T (CRAD; the level is each log's k), the fan term, C(T)
DEFAULT_PARAMS = ["C_ALL", "R31", "R_HB", "CRAD", "HA2_FAN", "CAP_T_COEF"]
BASE_C = {c: float(getattr(rw, c)) for c in ("C1", "C2", "C3")}
SIGMA_DATA = 1.0        # C - typical open-loop model error, scales the data term against the prior
CORR_TIME = 60.0        # s - open-loop errors are correlated over about this long: one independent value per window
BOUND = np.log(4.0)     # each factor may move between /4 and x4 of its current value
K_BOUND = np.log(1.3)   # per-log k between 1/1.3 and 1.3


def load(path, rate):
    """Inputs and measured T1 of one log, resampled to `rate` s steps (heater power averaged over each step,
    so the heat input is kept). Rows without a time are dropped by evaluate.read_log."""
    df = ev.read_log(path)
    t = ev.mono_time(df.t.values)
    bp = pd.to_numeric(df.bed_pwm, errors="coerce").fillna(0).values if "bed_pwm" in df else np.zeros(len(df))
    s = pd.DataFrame(dict(T=df.T1_real.values, pwm=df.pwm.values * (1 - rw.BED_SAG * bp),
                          fan=df.fan_state.values), index=pd.to_datetime(t, unit="s"))
    if "extrude_velocity" in df and (df.extrude_velocity.abs() > rw.EXT_EPS).mean() > 0.01:
        print(f"!! {os.path.basename(path)}: extrusion in the log, the filament term is not fitted here")
    r = s.resample(f"{int(rate * 1000)}ms")
    out = pd.DataFrame(dict(T=r["T"].last(), pwm=r["pwm"].mean(), fan=r["fan"].last())).dropna()
    out["t"] = (out.index - out.index[0]).total_seconds()
    return out.reset_index(drop=True)


def simulate(d, k=1.0):
    """Open-loop model T1 on the logged inputs (the power of step i heats over [t_i, t_i+1]); k: the log's loss level."""
    T1, T2, T3 = rw.initial_state(d["T"].iloc[0], d.pwm.iloc[0], d.fan.iloc[0])
    t, pwm, fan = d.t.values, d.pwm.values, d.fan.values
    out = np.empty(len(d))
    out[0] = T1
    for i in range(1, len(d)):
        T1, T2, T3 = rw.model_step(T1, T2, T3, pwm[i - 1], fan[i - 1], 0.0, t[i] - t[i - 1], k)
        out[i] = T1
    return out


def set_params(names, base, x):
    for n, b, xi in zip(names, base, x):
        if n == "C_ALL":                       # pseudo parameter: all three heat capacities by one factor
            for c, bc in BASE_C.items():
                setattr(rw, c, bc * np.exp(xi))
        else:
            setattr(rw, n, b * np.exp(xi))


def overrides(names, base, x):
    """The fitted values as evaluate.py overrides (C_ALL expanded)."""
    out = {}
    for n, b, xi in zip(names, base, x):
        if n == "C_ALL":
            out.update({c: bc * float(np.exp(xi)) for c, bc in BASE_C.items()})
        else:
            out[n] = b * float(np.exp(xi))
    return out


def write_calibration(path, names, se, info, data, k_logs, fit_rms, args):
    """Calibration file in the twin's format (PROFILE_DESIGN.md). rw already holds the fitted values."""
    out = {
        "calibration_version": 1,
        "date": time.strftime("%Y-%m-%d"),
        "source": "calibrate.py " + " ".join(f"--{k}={v}" for k, v in (("params", args.params), ("rate", args.rate),
                                                                     ("prior", args.prior))),
        "source_logs": [n for n, _ in data],
        "code_version": rw.source_version(),
        "params": {n: float(getattr(rw, n)) for n in rw.CALIBRATED},
        "fitted": names,
        "uncertainty_log_factor": {n: (float(e) if ok else None) for n, e, ok in zip(names, se, info)},
        "fit_rms_c": round(fit_rms, 4),
        "k_logs": {n: round(float(k), 4) for (n, _), k in zip(data, k_logs)},
    }
    if args.synthetic:
        out["source"] += " --synthetic (tool check, not a printer calibration)"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print(f"\nwritten: {path}  (not live: score it through the acceptance gate, then copy to calibration.json)")


STAGED_DYNAMIC = ["C_ALL", "R31", "R_HB", "CAP_T_COEF"]   # stage 2: storage and internal resistances
# Sanity checks of a staged calibration: they warn, they do not stop it (another hotend may really differ).
HOLD_SPREAD_W = 0.5     # W - same temperature and fan, different power: the hotend changed during the print
                        # (27.10: 18.8 vs 17.2 W an hour apart, plastic on the tip?, K78); in-hold noise ~0.2 W
HOLD_RESIDUAL_W = 0.5   # W - a hold the fitted loss curve does not explain
TRAJ_RMS_C = 3.0        # C - stage 2 misfit above this: the 3-node structure may not suit the hotend (26.10: 5.2)
# factor ranges relative to the profile (the author's Ender, brass and steel nozzle: loss x0.92-1.02, C x0.87-1.03,
# R31 x1.3-1.8, R_HB x1.1-1.4, HA2_FAN x0.67-1.0); outside: check the profile, or the hotend is very different
FACTOR_RANGE = {"loss": (0.7, 1.4), "HA2_FAN": (0.4, 2.5), "C_ALL": (0.7, 1.4), "R31": (0.5, 2.5),
                "R_HB": (0.6, 1.7), "CAP_T_COEF": (0.3, 3.0)}
POWER_HINT = 0.10       # loss and C both off by > ~10 % the same way: heater_power_w is suspect (+-20 % test: both caught)


def holds(path, min_len=140.0, tail=90.0):
    """Steady holds of one log as (T, fan, heater W): a constant target and fan for >= min_len s, the mean heater
    power of the last `tail` s (PID ripple averages out), T within 3 C and its mean within 0.5 C of the target.
    In a hold the heater power IS the loss: calorimetry, no dynamics involved."""
    df = ev.read_log(path)
    t = ev.mono_time(df.t.values)
    tg = pd.to_numeric(df.target, errors="coerce").ffill().values
    fan, T, has_T = df.fan_state.values, df.T1_real.values, df.has_T.values
    bp = pd.to_numeric(df.bed_pwm, errors="coerce").fillna(0).values if "bed_pwm" in df else np.zeros(len(df))
    P = df.pwm.values * rw.P_MAX * (1 - rw.BED_SAG * bp)
    seg = np.r_[0, np.cumsum((np.diff(tg) != 0) | (np.diff(fan) != 0))]
    out = []
    for g in np.unique(seg):
        idx = np.flatnonzero(seg == g)
        if tg[idx[0]] <= 0 or t[idx[-1]] - t[idx[0]] < min_len:
            continue
        w = idx[t[idx] >= t[idx[-1]] - tail]
        Tw = T[w][has_T[w]]
        if len(Tw) < 10 or Tw.max() - Tw.min() > 3.0 or abs(Tw.mean() - tg[idx[0]]) > 0.5:
            continue
        dt = np.diff(t[w])
        out.append((float(Tw.mean()), float(fan[idx[0]]), float(np.sum(P[w][:-1] * dt) / dt.sum())))
    return out


def steady_power(T, fan, s, ha2_fan, f0):
    """Heater power of a steady hold with k = 1, the block loss scaled by s and the given part-fan curve."""
    fc = ha2_fan * max((fan - f0) / (1.0 - f0), 0.0) ** rw.FAN_EXP
    dT2 = (T - rw.T_AMB) / (1 + rw.R_HB * (rw.HA2 + fc))
    return s * rw.natural_loss(T) + fc * dT2 + rw.HA2 * dT2


def staged(a):
    """Calibration of a new hotend in two stages [K59]: (1) losses and part-fan curve from the steady holds
    (calorimetry), (2) heat capacities and internal resistances from the whole trajectory with those losses fixed.
    k is 1 throughout: the calibration print DEFINES the reference loss level, so the learned k means the same thing
    as on the day of calibration. Fitting the losses on the trajectory instead lets them trade heat-up against the
    holds (K59)."""
    hl = [(os.path.basename(p), h) for p in a.logs for h in holds(p)]
    off = sorted({round(T) for _, (T, f, _) in hl if f <= rw.FAN_F0})
    on = [h for _, h in hl if h[1] > rw.FAN_F0 + 0.05]
    print(f"stage 1, holds: {len(hl)} ({len(hl) - len(on)} fan below threshold at {off} C, {len(on)} fan on)")
    warn = []
    groups = {}
    for n, (T, f, P) in hl:
        groups.setdefault((round(T), round(f, 2)), []).append(P)
    for (T, f), Ps in sorted(groups.items()):
        if len(Ps) > 1 and max(Ps) - min(Ps) > HOLD_SPREAD_W:
            warn.append(f"holds at {T} C fan {f:.2f} differ by {max(Ps) - min(Ps):.2f} W ({', '.join(f'{P:.2f}' for P in Ps)}"
                        " W, in print order): the hotend changed during the print (plastic on the tip, a sock or "
                        "nozzle settling); clean and check the tip, then print it again")
    if len(off) < 2:
        sys.exit("need fan-off holds at >= 2 temperatures (the calibration G-code has them; was the print complete?)")
    fit_fan = len({round(f, 2) for _, f, _ in on}) >= 2
    x0 = [0.0] + ([np.log(rw.HA2_FAN), rw.FAN_F0] if fit_fan else [])

    def res1(x):
        s = np.exp(x[0])
        hf, f0 = (np.exp(x[1]), x[2]) if fit_fan else (rw.HA2_FAN, rw.FAN_F0)
        return np.array([steady_power(T, f, s, hf, f0) - P for _, (T, f, P) in hl])
    r_before = res1(np.array(x0))
    f1 = least_squares(res1, x0, bounds=([-BOUND] + ([np.log(rw.HA2_FAN) - BOUND, 0.0] if fit_fan else []),
                                         [BOUND] + ([np.log(rw.HA2_FAN) + BOUND, 0.6] if fit_fan else [])))
    s = float(np.exp(f1.x[0]))
    rw.HA1, rw.CRAD = rw.HA1 * s, rw.CRAD * s
    if fit_fan:
        rw.HA2_FAN, rw.FAN_F0 = float(np.exp(f1.x[1])), float(f1.x[2])
    rms = lambda r: float(np.sqrt(np.mean(r ** 2)))   # noqa: E731
    print(f"  heater power rms {rms(r_before):.2f} -> {rms(f1.fun):.2f} W; block loss x{s:.3f}"
          + (f", part fan HA2_FAN {rw.HA2_FAN:.4f} threshold {rw.FAN_F0:.3f}" if fit_fan else ", fan curve kept"
             " (fan-on holds at < 2 levels)"))
    # a fan level next to the threshold is not checked: the model's sharp threshold is a known simplification there
    # (21.10: fan 15 % with the threshold at 16 %, -0.70 W)
    off_curve = [abs(r) > HOLD_RESIDUAL_W and abs(f - rw.FAN_F0) > 0.05 for (_, (_, f, _)), r in zip(hl, f1.fun)]
    for (n, (T, f, P)), r, bad in zip(hl, f1.fun, off_curve):
        print(f"    {n:30s} {T:5.0f} C fan {f:4.2f}: {P:5.2f} W, model {r:+5.2f}" + ("  !!" if bad else ""))
    if any(off_curve):
        warn.append(f"{sum(off_curve)} hold(s) off the fitted loss curve by > {HOLD_RESIDUAL_W:g} W (marked !!)")
    factors = {"loss": s}
    if fit_fan:
        factors["HA2_FAN"] = rw.HA2_FAN / float(np.exp(x0[1]))

    names = list(STAGED_DYNAMIC)
    base = np.array([1.0 if n == "C_ALL" else float(getattr(rw, n)) for n in names])
    data = [(os.path.basename(p), load(p, a.rate)) for p in a.logs]
    prior = a.prior or 0.3
    n_samples = sum(len(d) for _, d in data)
    w = np.sqrt(sum(d.t.iloc[-1] for _, d in data) / CORR_TIME / n_samples) / SIGMA_DATA

    def dres(x):
        set_params(names, base, x)
        return np.concatenate([simulate(d, 1.0) - d["T"].values for _, d in data])
    r0 = dres(np.zeros(len(names)))
    f2 = least_squares(lambda x: np.r_[dres(x) * w, x / prior], np.zeros(len(names)), bounds=(-BOUND, BOUND),
                       x_scale=0.1, diff_step=1e-3)
    r2 = dres(f2.x)
    cov = np.linalg.pinv(f2.jac.T @ f2.jac)
    se = np.sqrt(np.abs(np.diag(cov)))
    print(f"\nstage 2, trajectory (k = 1, prior +-{prior:.2f}): T1 rms {rms(r0):.2f} -> {rms(r2):.2f} C")
    for n, b, xi, e in zip(names, base, f2.x, se):
        print(f"  {n:11s} {b:11.4g} -> {b * np.exp(xi):11.4g}  x{np.exp(xi):.3f} +-{e:.3f}"
              + ("  at bound" if abs(abs(xi) - BOUND) < 1e-3 else ""))
        factors[n] = float(np.exp(xi))
    if rms(r2) > TRAJ_RMS_C:
        warn.append(f"stage 2 misfit {rms(r2):.2f} C > {TRAJ_RMS_C:g} C: the model does not follow this hotend well "
                    "(a transient during the print, or a hotend unlike cartridge + block + heatsink)")
    for n, (lo, hi) in FACTOR_RANGE.items():
        if n in factors and not lo <= factors[n] <= hi:
            warn.append(f"{n} x{factors[n]:.2f} is outside x{lo:g}-x{hi:g} of the profile")
    ls, lc = np.log(factors["loss"]), np.log(factors["C_ALL"])
    if abs(ls) > POWER_HINT and abs(lc) > POWER_HINT and ls * lc > 0:
        warn.append(f"loss x{factors['loss']:.2f} and heat capacity x{factors['C_ALL']:.2f} are off together: both scale "
                    f"with the heater power, so heater_power_w ({rw.P_MAX:g} W) is probably wrong by about "
                    f"x{np.exp((ls + lc) / 2):.2f} (cartridge rating, supply voltage)")
    print(f"\nchecks: {len(warn)} warning(s)" + "".join(f"\n  !! {w_}" for w_ in warn))
    if a.write:
        a.params = "staged:" + ",".join(["loss_scale"] + (["HA2_FAN", "FAN_F0"] if fit_fan else []) + names)
        write_calibration(a.write, ["HA1", "CRAD"] + (["HA2_FAN", "FAN_F0"] if fit_fan else []) + names,
                          [None] * (4 if fit_fan else 2) + list(se), [False] * (4 if fit_fan else 2) + [True] * len(names),
                          data, [1.0] * len(data), rms(r2), a)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="+", help="calibration logs without extrusion")
    ap.add_argument("--params", default=",".join(DEFAULT_PARAMS), help="comma-separated model constants to fit")
    ap.add_argument("--rate", type=float, default=None,
                    help="resampling step in s (default 1; --staged 0.3, Klipper's report interval: the cartridge "
                         "time constant is ~2.5 s, 1 s steps bias R31 by ~30 %% on a virtual print)")
    ap.add_argument("--synthetic", action="store_true", help="fit the current model's own output (tool check)")
    ap.add_argument("--prior", type=float, default=0.0,
                    help="profile uncertainty of each parameter as a log-factor (e.g. 0.3 = about +-35 %%); "
                         "0 = no prior (free fit)")
    ap.add_argument("--score", action="store_true",
                    help="afterwards score phase by phase, current vs fitted: the September core set and the "
                         "calibration-set logs of the same hardware state (evaluate.py)")
    ap.add_argument("--score-since", default="2026-10-14",
                    help="first day of the current hardware state for --score (default 2026-10-14, after the "
                         "13.10 maintenance)")
    ap.add_argument("--write", metavar="PATH", help="save the result as a calibration file (see above)")
    ap.add_argument("--force", action="store_true", help="--write may replace an existing file")
    ap.add_argument("--staged", action="store_true",
                    help="calibrate a hotend from its calibration print: losses + fan curve from the holds, then "
                         "capacities and resistances from the trajectory, k = 1 (see staged())")
    a = ap.parse_args()
    missing = [p for p in a.logs if not os.path.isfile(p)]
    if missing:
        sys.exit("log not found: " + ", ".join(missing))
    if a.write and os.path.exists(a.write) and not a.force:
        sys.exit(f"{a.write} exists; use --force to replace it")
    if a.staged:
        a.rate = a.rate or 0.3
        return staged(a)
    a.rate = a.rate or 1.0

    names = [n.strip() for n in a.params.split(",") if n.strip()]
    for n in names:
        if n != "C_ALL" and not hasattr(rw, n):
            sys.exit(f"unknown parameter: {n}")
    base = np.array([1.0 if n == "C_ALL" else float(getattr(rw, n)) for n in names])
    data = [(os.path.basename(p), load(p, a.rate)) for p in a.logs]
    nP = len(names)

    if a.synthetic:
        for _, d in data:
            d["T"] = np.round(simulate(d), 2)          # thermistor reported with 0.01 C steps
        x0 = np.log(np.random.default_rng(0).uniform(0.75, 1.3, nP))
    else:
        x0 = np.zeros(nP)
    x0 = np.r_[x0, np.zeros(len(data))]                 # per-log log k, starts at k = 1

    def data_residuals(x):
        set_params(names, base, x[:nP])
        return np.concatenate([simulate(d, np.exp(xk)) - d["T"].values for (_, d), xk in zip(data, x[nP:])])

    n_samples = sum(len(d) for _, d in data)
    n_eff = sum(d.t.iloc[-1] for _, d in data) / CORR_TIME
    w = np.sqrt(n_eff / n_samples) / SIGMA_DATA          # data weight: n_eff independent values of SIGMA_DATA

    def residuals(x):
        r = data_residuals(x) * w
        return np.r_[r, x[:nP] / a.prior] if a.prior > 0 else r

    rms = lambda r: float(np.sqrt(np.mean(r ** 2)))   # noqa: E731
    r_start = data_residuals(np.zeros(nP + len(data)))
    print(f"{n_samples} samples from {len(data)} logs (~{n_eff:.0f} independent); rms with current values, k = 1: "
          f"{rms(r_start):.3f} C" + (f"; prior +-{a.prior:.2f} (log-factor)" if a.prior > 0 else "; no prior"))
    lo = np.r_[np.full(nP, -BOUND), np.full(len(data), -K_BOUND)]
    fit = least_squares(residuals, x0, bounds=(lo, -lo), x_scale=0.1, diff_step=1e-3, verbose=0)
    r_fit = data_residuals(fit.x)
    set_params(names, base, fit.x[:nP])
    k_logs = np.exp(fit.x[nP:])

    print(f"rms after fit {rms(r_fit):.3f} C   ({fit.nfev} model runs, {fit.message})\n")
    if nP == 0:                                          # --params "": only the per-log k (baseline for a comparison)
        for (name, d), k, i in zip(data, k_logs, np.cumsum([0] + [len(d) for _, d in data])):
            print(f"  {name:45s} {rms(r_start[i:i + len(d)]):6.3f} -> {rms(r_fit[i:i + len(d)]):6.3f} C   k {k:.3f}")
        if a.write:
            write_calibration(a.write, names, [], [], data, k_logs, rms(r_fit), a)
        return
    # identifiability of the physical parameters: covariance of the log-factors from the Jacobian of the weighted
    # system (data as n_eff independent values of SIGMA_DATA, plus the prior rows), the per-log k marginalised
    J = fit.jac
    # a parameter the residual does not react to (e.g. the fan term in a fan-off log) carries no information
    sens = np.sqrt(((J[:n_samples, :nP] / w) ** 2).mean(axis=0))     # C per unit log-factor
    info = np.r_[sens > 1e-3 * max(sens.max(), 1e-12), np.ones(len(data), bool)]
    cov_all = np.full((len(fit.x), len(fit.x)), np.nan)
    Ji = J[:, info]
    cov_all[np.ix_(info, info)] = np.linalg.pinv(Ji.T @ Ji)
    cov, info = cov_all[:nP, :nP], info[:nP]
    se = np.sqrt(np.abs(np.diag(cov)))
    corr = cov / np.outer(se, se)

    print(f"{'param':12s}{'current':>12s}{'fitted':>12s}{'factor':>9s}{'+- (1 se)':>11s}")
    for n, b, xi, e, ok in zip(names, base, fit.x[:nP], se, info):
        flag = "  at bound" if abs(abs(xi) - BOUND) < 1e-3 else ""
        unc = ("x" + format(np.exp(e), ".3f")) if ok else "no info"
        print(f"{n:12s}{b:12.5g}{b * np.exp(xi):12.5g}{np.exp(xi):9.3f}{unc:>11s}{flag}")
    pairs = [(names[i], names[j], corr[i, j]) for i in range(nP) for j in range(i + 1, nP)
             if info[i] and info[j] and abs(corr[i, j]) > 0.9]
    print("\nnot separable by these data (|r| > 0.9): "
          + (", ".join(f"{p}~{q} {c:+.2f}" for p, q, c in pairs) if pairs else "none"))
    print("\nper log rms (current values, k = 1 -> fitted) and the log's fitted k:")
    i = 0
    for (name, d), k in zip(data, k_logs):
        n = len(d)
        print(f"  {name:45s} {rms(r_start[i:i + n]):6.3f} -> {rms(r_fit[i:i + n]):6.3f} C   k {k:.3f}")
        i += n
    if a.write:
        write_calibration(a.write, names, se, info, data, k_logs, rms(r_fit), a)

    if a.score:
        # does it carry over? (1) September core set (evaluate.py default, fresh k) - the structure test of the
        # acceptance gate, but from an older hardware state (before the 13.10 maintenance and duct re-mount);
        # (2) calibration-set prints of the same hardware state as the calibration logs (from --score-since,
        # live start: each log with the k / k_fil it was recorded with)
        ov = overrides(names, base, fit.x[:nP])
        since = time.mktime(time.strptime(a.score_since, "%Y-%m-%d"))
        same = [p for p in ev.set_paths("ayar") if os.path.basename(p) not in {n for n, _ in data}
                and ev.read_log(p).time.iloc[0] >= since]
        for title, kw in (("September core set (older hardware state)", {}),
                          (f"calibration-set logs since {a.score_since} ({len(same)}, live start)",
                           dict(paths=same, live=True))):
            cur, new = ev.pooled(ev.evaluate(**kw)).set_index("faz"), ev.pooled(ev.evaluate(ov, **kw)).set_index("faz")
            print(f"\n{title}, RMSE (bias) C: current -> fitted")
            for ph in ev.PHASES + ["TUMU"]:
                if ph in cur.index:
                    print(f"  {ph:12s} {cur.rmse[ph]:5.2f} ({cur.bias[ph]:+5.2f}) -> "
                          f"{new.rmse[ph]:5.2f} ({new.bias[ph]:+5.2f})")


if __name__ == "__main__":
    main()
