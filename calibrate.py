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

Analysis only: writes nothing, touches neither the live twin nor its parameters.
Needs scipy (not needed by the live twin).

Usage (in dt/):   py calibrate.py LOG [LOG ...] [--params C1,C3,R31,...] [--rate 1.0] [--synthetic]
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import evaluate as ev  # noqa: E402
import realtime_websocket as rw  # noqa: E402

DEFAULT_PARAMS = ["C1", "C2", "C3", "R31", "R_HB", "HA1", "CRAD", "HA2", "HA2_FAN", "CAP_T_COEF"]
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
        setattr(rw, n, b * np.exp(xi))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="+", help="calibration logs without extrusion")
    ap.add_argument("--params", default=",".join(DEFAULT_PARAMS), help="comma-separated model constants to fit")
    ap.add_argument("--rate", type=float, default=1.0, help="resampling step in s (default 1)")
    ap.add_argument("--synthetic", action="store_true", help="fit the current model's own output (tool check)")
    a = ap.parse_args()

    names = [n.strip() for n in a.params.split(",") if n.strip()]
    for n in names:
        if not hasattr(rw, n):
            sys.exit(f"unknown parameter: {n}")
    base = np.array([float(getattr(rw, n)) for n in names])
    data = [(os.path.basename(p), load(p, a.rate)) for p in a.logs]
    nP = len(names)

    if a.synthetic:
        for _, d in data:
            d["T"] = np.round(simulate(d), 2)          # thermistor reported with 0.01 C steps
        x0 = np.log(np.random.default_rng(0).uniform(0.75, 1.3, nP))
    else:
        x0 = np.zeros(nP)
    x0 = np.r_[x0, np.zeros(len(data))]                 # per-log log k, starts at k = 1

    def residuals(x):
        set_params(names, base, x[:nP])
        return np.concatenate([simulate(d, np.exp(xk)) - d["T"].values for (_, d), xk in zip(data, x[nP:])])

    rms = lambda r: float(np.sqrt(np.mean(r ** 2)))   # noqa: E731
    r_start = residuals(np.zeros(nP + len(data)))
    print(f"{sum(len(d) for _, d in data)} samples from {len(data)} logs; rms with current values, k = 1: "
          f"{rms(r_start):.3f} C")
    lo = np.r_[np.full(nP, -BOUND), np.full(len(data), -K_BOUND)]
    fit = least_squares(residuals, x0, bounds=(lo, -lo), x_scale=0.1, diff_step=1e-3, verbose=0)
    r_fit = fit.fun
    set_params(names, base, fit.x[:nP])
    k_logs = np.exp(fit.x[nP:])

    print(f"rms after fit {rms(r_fit):.3f} C   ({fit.nfev} model runs, {fit.message})\n")
    if nP == 0:                                          # --params "": only the per-log k (baseline for a comparison)
        for (name, d), k, i in zip(data, k_logs, np.cumsum([0] + [len(d) for _, d in data])):
            print(f"  {name:45s} {rms(r_start[i:i + len(d)]):6.3f} -> {rms(r_fit[i:i + len(d)]):6.3f} C   k {k:.3f}")
        return
    # identifiability of the physical parameters: covariance of the log-factors from the Jacobian
    J = fit.jac[:, :nP]
    dof = max(1, len(r_fit) - len(fit.x))
    s2 = max(float(r_fit @ r_fit) / dof, 1e-6)
    # a parameter the residual does not react to (e.g. the fan term in a fan-off log) carries no information
    sens = np.sqrt((J ** 2).mean(axis=0))                 # C per unit log-factor
    info = sens > 1e-3 * max(sens.max(), 1e-12)
    cov = np.full((nP, nP), np.nan)
    Ji = J[:, info]
    cov[np.ix_(info, info)] = np.linalg.pinv(Ji.T @ Ji) * s2
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


if __name__ == "__main__":
    main()
