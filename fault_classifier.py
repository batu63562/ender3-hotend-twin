"""
Fault classification (OFFLINE - does not touch the live script).

The live twin finds a fault as a "direction" (e.g. GUC_AZALDI). This tool takes one fault event from the
log and looks for WHICH PHYSICAL TERM broke:

    A  fan path         duct blocked / turned, fan slowed          -> deviation scales with the fan loss
    B  no filament flow clog, filament ran out, gear grinding      -> deviation scales with the filament load
    C  insulation/env.  sock fell off, draft, blob, enclosure warm -> deviation scales with all air losses
    D  thermistor       physically impossible reading rate
    E  heater           heater at 100 % but the temperature falls

Method - energy balance (while the temperature is held, the PID power covers the losses):
    eksik_guc (missing power) = loss expected by the model - (measured power - rate of stored energy)
    (+ = LESS heat leaving than expected, - = MORE heat leaving than expected)
This energy signal is used as the "is there a meaningful deviation" gate and for the split in windows
where the fan changes. Single-term hypotheses are tested by TWIN REPLAY instead: the twin is replayed from
its state at the event separately for each hypothesis (the thermal lag is inside the model); the day's loss
factor and, when possible, the filament coefficient are measured in place from the reference before the
event (see replay_hypotheses). Hypotheses:
    A: deviation = a * fan_loss              (a = fraction of fan cooling lost; <0 extra cooling)
    B: deviation = b * filament_load         (b = fraction of filament not flowing)
    C: deviation = c * (block_loss + fan)    (c = relative change of all air losses)
A hypothesis is "consistent" if its ratio is within physical bounds and the fit is good. If two hypotheses
have the same shape in the data (e.g. A and C when the fan never changed) both are reported: the data
cannot make that distinction.

If the fan changed in the window (and there is no extrusion), A/C are separated directly: the deviation is
split into SEPARATE block and fan terms,
    deviation = -(x * block_loss + y * fan_loss)
C if x (block loss change) is outside its normal band; A if x is normal and y (fan cooling change) is
outside its band. Bands from normal fan-on benchmark logs (X_BAND, Y_BAND).

Usage:
    python fault_classifier.py LOG.csv                    # classify the events the twin found
    python fault_classifier.py LOG.csv --window 330 480   # classify a given window (s)
    python fault_classifier.py --labels                   # benchmark/ariza_ornekleri/etiketler.csv
"""
import argparse
import os

import numpy as np
import pandas as pd

import evaluate as d

HERE = os.path.dirname(os.path.abspath(__file__))
LABEL_FILE = os.path.join(HERE, "benchmark", "ariza_ornekleri", "etiketler.csv")

GRID_DT = 0.5          # s - logs are resampled evenly at this interval
SMOOTH = 20.0          # s - power/loss signals are averaged over this window (PID noise + lag)
QUASI_RATE = 0.3       # C/s - the energy balance is unreliable for faster changes (ramps)
T_VALID = 150.0        # C - no analysis below this
REF_LEN = 180.0        # s - reference window before the event
REF_MIN = 30.0         # s - the reference needs at least this much valid data
ANALYSIS_LEN = 300.0   # s - at most this much is examined from the event start
MIN_SHIFT = 1.2        # W - if the deviation (mean, or the part a hypothesis explains) is below this:
                       #     "no meaningful power difference". In normal benchmark windows |mean|
                       #     is at most 0.91 W, 95 % below 0.67 W.
MIN_TERM = 0.3         # W - a hypothesis cannot be tested if its term's mean is below this
                       #     (e.g. A with the fan off, B without extrusion)
RMS_TIE = 1.25         # hypotheses within this multiple of the best fit's rms are also "consistent"
BOUNDS = {"A": (-1.5, 1.15), "B": (-0.3, 1.15), "C": (-0.6, 0.6)}
RES_RATE_MAX = 8.0     # C/s - D: a reading separating from the model this fast is not physical (jump).
                       #     In normal logs the residual changes at most 2.9 C/s (klipper), 5.8 C/s (old http).
                       #     The reading's own rate cannot be used: 10 C/s is normal in a cold heat-up.
SAT_PWM = 0.95         # E: heater counts as saturated
FAN_SPREAD = 2.0       # W - if the fan loss changes at least this much in the window (e.g. fan 0 -> 50 %,
                       #     4.4 W at 245 C) the block and fan terms are solved separately
# normal bands for the two-term split (3 fan-on benchmark logs, 19 windows, 2026-09-25):
# block |x| <= 2.4 %, fan y -18..0 % (mean -8 %: the model overestimates fan cooling by ~8 %).
# One day of data -> kept wide; the block band equals the live k alarm (K_ALARM_BAND).
X_BAND = 0.08
Y_BAND = (-0.22, 0.12)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
def bed_scale(df, m):
    """Cartridge power factor 1 - BED_SAG * bed_pwm for every row (1 without bed data)."""
    if "bed_pwm" not in df:
        return np.ones(len(df))
    bp = pd.to_numeric(df.bed_pwm, errors="coerce").ffill().fillna(0.0).values
    return 1.0 - m.BED_SAG * bp


def signals(df, m):
    """Resamples the log onto an even time grid and computes the energy balance terms."""
    t = (df.t - df.t.iloc[0]).values
    hasT = df.has_T.values
    g = np.arange(0.0, t[-1], GRID_DT)
    idx = np.searchsorted(t, g, side="right") - 1          # zero-order hold
    pwm, fan, ext = df.pwm.values[idx], df.fan_state.values[idx], df.extrude_velocity.values[idx]
    T = np.interp(g, t[hasT], df.T1_real.values[hasT])

    s = pd.DataFrame({"t": g, "T": T, "pwm": pwm, "fan": fan})
    n2 = max(1, int(round(m.EXT_AVG_WINDOW / GRID_DT)))     # same net flow average as the twin
    s["flow"] = np.clip(pd.Series(ext).rolling(n2, min_periods=1).mean().values, 0, None)
    s["P"] = pwm * m.P_MAX * bed_scale(df, m)[idx]     # cartridge power drops while the bed is on (as in the twin)
    # T2 (nozzle side) and T3 (cartridge) are not measured: computed with the model's own equations, driven by
    # the REAL T1 and the measured power. Without the heat they store in the balance, there was a ~2 W fake
    # deviation when the fan switched on (T2 releases heat) and after heat-up (T3 releases heat).
    fanc = np.array([m.fan_conductance(f) for f in fan])     # the model's own fan rule (threshold included)
    T1s, T2s, T3s = m.initial_state(T[0], pwm[0], fan[0])
    T2 = np.empty(len(g)); T3 = np.empty(len(g))
    for i in range(len(g)):
        T2[i], T3[i] = T2s, T3s
        # T1 is taken constant over the step -> exact exponential solution for both nodes (unconditionally stable)
        G2 = 1 / m.R_HB + m.HA2 + fanc[i]
        T2_inf = (T[i] / m.R_HB + (m.HA2 + fanc[i]) * m.T_AMB) / G2
        T2s = T2_inf + (T2s - T2_inf) * np.exp(-GRID_DT * G2 / m.C2)
        T3_inf = T[i] + s.P.values[i] * m.R31
        T3s = T3_inf + (T3s - T3_inf) * np.exp(-GRID_DT / (m.R31 * m.C3))
    s["L_blok"] = m.HA1 * (T - m.T_AMB) + m.CRAD * ((T + 273.15) ** 4 - m.T_AMB_K ** 4)
    s["L_hs"] = m.HA2 * (T2 - m.T_AMB)
    s["L_fan"] = fanc * (T2 - m.T_AMB)
    s["L_fil"] = m.K_FIL * s.flow * (T - m.T_AMB)
    s["store"] = (m.C1 * np.gradient(T, g) + m.C2 * np.gradient(T2, g) + m.C3 * np.gradient(T3, g))

    n = max(1, int(round(SMOOTH / GRID_DT)))
    sm = s.rolling(n, center=True, min_periods=n // 2).mean()
    sm["t"] = g
    sm["dTdt"] = np.gradient(sm["T"].values, g)
    # fast derivative (2 s) for rule E: an intermittent power loss disappeared in the 20 s average
    n_fast = max(1, int(round(2.0 / GRID_DT)))
    sm["dTdt_fast"] = np.gradient(pd.Series(T).rolling(n_fast, center=True, min_periods=1).mean().values, g)
    # lowest pwm over the last 3 s: single-sample 100 % spikes are normal for the PID during a hold (fan
    # switching on etc.); a fault means UNINTERRUPTED saturation
    sm["pwm_min3"] = pd.Series(pwm).rolling(max(1, int(round(3.0 / GRID_DT))), min_periods=1).min().values
    # highest pwm over the last 15 s: when the PID cuts power at the end of a heat-up, heat stored in the
    # cartridge raises the temperature for a few more seconds (normal). After 15 s fully off that heat is gone
    # (R31*C3 ~2.5 s).
    sm["pwm_max15"] = pd.Series(pwm).rolling(max(1, int(round(15.0 / GRID_DT))), min_periods=1).max().values
    sm["gap"] = (sm.L_blok + sm.L_hs + sm.L_fan + sm.L_fil) - (sm.P - sm.store)
    # points near a data gap are invalid
    near_gap = np.zeros(len(g), bool)
    for i in np.where(np.diff(t) > m.GAP_THRESHOLD)[0]:
        near_gap |= (g > t[i] - SMOOTH) & (g < t[i + 1] + SMOOTH)
    sm["valid"] = (sm["T"] > T_VALID) & (sm.dTdt.abs() < QUASI_RATE) & ~near_gap & sm.gap.notna()
    # raw signals (for rules D/E, not smoothed)
    sm["T_raw"], sm["pwm_raw"] = T, pwm
    return sm, t, hasT


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def fan_varies(seg):
    """Does the fan loss change enough in the window (can the block and fan terms be solved separately)."""
    lf = seg.L_fan.values
    return np.percentile(lf, 90) - np.percentile(lf, 10) >= FAN_SPREAD


def fit_two_term(dg, seg):
    """deviation = -(x * block_loss + y * fan_loss):  x = relative change in block loss, y = in fan cooling
    (+ = MORE loss in reality). Returns: x, y, fit rms (W)."""
    X = -np.column_stack([seg.L_blok.values, seg.L_fan.values])
    (x, y), *_ = np.linalg.lstsq(X, dg, rcond=None)
    rms = float(np.sqrt(np.mean((dg - X @ [x, y]) ** 2)))
    return float(x), float(y), rms


def fit_hypotheses(dg, terms):
    """dg: deviation from the reference (W). terms: {hypothesis: regressor array}. One-parameter LS."""
    out = {}
    for h, x in terms.items():
        if np.mean(np.abs(x)) < MIN_TERM:
            out[h] = dict(ratio=np.nan, rms=np.nan, fits=False, reason=f"terim ~0 ({np.mean(x):.2f} W): test edilemez")
            continue
        a = float(np.dot(x, dg) / np.dot(x, x))
        rms = float(np.sqrt(np.mean((dg - a * x) ** 2)))
        lo, hi = BOUNDS[h]
        ok = lo <= a <= hi
        out[h] = dict(ratio=a, rms=rms, fits=ok,
                      reason="" if ok else f"oran {a:+.2f} fiziksel sinir [{lo}, {hi}] disinda")
    return out


# ---------------------------------------------------------------------------
# Hypothesis test by twin replay
# ---------------------------------------------------------------------------
# The energy window (20 s average) captures the PID and thermal lag only roughly: after a short extrusion the
# power difference lasted beyond the end of the extrusion and the filament test was fooled (experiment 1).
# Here the twin is replayed from its state at the event with the measured power/fan/flow, separately for each
# hypothesis; the lag is inside the model, so it is accounted for automatically.
# The filament load depends on context (free extrusion into the air drew 1.33 times the in-print K_FIL heat -
# experiment 1). So if the reference period has both extruding and non-extruding parts, the filament
# coefficient is also measured in place and the B ratio is given relative to it.
REPLAY_EPS = 0.05      # Gauss-Newton finite-difference step (factor)
REPLAY_TIE_C = 0.3     # C - hypotheses this close to the best fit are also "consistent"
REF_EXT_MIN = 20.0     # s - if the reference has at least this much extruding AND non-extruding time,
                       #     the filament coefficient is measured in place
K_FIL_PRIOR = 0.15     # relative uncertainty of K_FIL when it cannot be measured in place: calibrated in a print
                       # (one print, one material); measured 1.33 times in free extrusion (another context)
B_MAX = 1.05           # filament test: B is rejected if, even after the uncertainty (2 sigma), the fraction of
                       # filament not flowing exceeds this (no more than the filament load can disappear)


def replay_inputs(df, m):
    """Same input order as Twin: interval (t[i-1], t[i]] advances with the previous message's pwm/fan and the
    signed average flow of the last 2 s (clipped below 0)."""
    t = (df.t - df.t.iloc[0]).values
    ext = df.extrude_velocity.values
    ext_sm = np.zeros(len(t))
    hist = []
    for i in range(1, len(t)):
        hist.append((t[i], t[i] - t[i - 1], ext[i - 1]))
        while hist and hist[0][0] < t[i] - m.EXT_AVG_WINDOW:
            hist.pop(0)
        span = sum(h[1] for h in hist)
        ext_sm[i] = max(sum(h[1] * h[2] for h in hist) / span, 0.0) if span > 0 else max(ext[i - 1], 0.0)
    return dict(t=t, T=df.T1_real.values, hasT=df.has_T.values, pwm=df.pwm.values * bed_scale(df, m),
                fan=df.fan_state.values, ext_sm=ext_sm)


WARMUP = 30.0          # s - the replay starts this much before the start (so T2/T3 settle), then all three are
                       #     shifted to the real T1. The PID power swings 37-65 % within 5 s, so setting T3 from
                       #     the power at that moment gave a ~15 C wrong start and a fake transient.


def _real_T1(R, i):
    last = (R["t"] >= R["t"][i] - 1.0) & (R["t"] <= R["t"][i]) & R["hasT"]
    return float(R["T"][last].mean()) if last.any() else float(R["T"][i])


def _sim(ctx, i0, i1, kn, kf, kfil, base=None):
    """Replay the twin from row i0 to i1 with factors (kn: block loss, kf: fan cooling, kfil: filament).
    Start: replayed WARMUP seconds earlier with the 'base' factors (same factors if not given); at i0 the
    three nodes are shifted together to the real T1 (internal temperature differences = heat flows are kept).
    Returns: real - twin at the temperature measurements in (i0, i1]."""
    m, R = ctx["m"], ctx["rp"]
    t, T, hasT, pwm, fan, ext = R["t"], R["T"], R["hasT"], R["pwm"], R["fan"], R["ext_sm"]
    iw = int(np.searchsorted(t, t[i0] - WARMUP))
    T1 = _real_T1(R, iw)
    T1, T2, T3 = m.initial_state(T1, float(pwm[max(0, iw - 20):iw + 1].mean()), fan[iw])

    def f(T1, T2, T3, u, fc, q, kn, kfil):
        q31 = (T3 - T1) / m.R31; qhb = (T1 - T2) / m.R_HB
        d1 = (q31 - qhb - kn * m.natural_loss(T1) - kfil * m.K_FIL * q * (T1 - m.T_AMB)) / m.C1
        d2 = (qhb - m.HA2 * (T2 - m.T_AMB) - fc * (T2 - m.T_AMB)) / m.C2
        return d1, d2, (u * m.P_MAX - q31) / m.C3

    bn, bf, bfil = base if base is not None else (kn, kf, kfil)
    out = []
    for i in range(iw + 1, i1 + 1):
        if i == i0 + 1:                      # event start: shift to the real temperature
            off = _real_T1(R, i0) - T1
            T1, T2, T3 = T1 + off, T2 + off, T3 + off
        on = i > i0
        cn, cf, cfil = (kn, kf, kfil) if on else (bn, bf, bfil)
        dt = t[i] - t[i - 1]
        u, fc, q = pwm[i - 1], cf * m.fan_conductance(fan[i - 1]), ext[i]
        n = max(1, int(np.ceil(dt / 0.25))); h = dt / n     # model stability limit ~2.7 s
        for _ in range(n):
            a = f(T1, T2, T3, u, fc, q, cn, cfil)
            b = f(T1 + h/2*a[0], T2 + h/2*a[1], T3 + h/2*a[2], u, fc, q, cn, cfil)
            c = f(T1 + h/2*b[0], T2 + h/2*b[1], T3 + h/2*b[2], u, fc, q, cn, cfil)
            e = f(T1 + h*c[0], T2 + h*c[1], T3 + h*c[2], u, fc, q, cn, cfil)
            T1 += h/6*(a[0] + 2*b[0] + 2*c[0] + e[0]); T2 += h/6*(a[1] + 2*b[1] + 2*c[1] + e[1])
            T3 += h/6*(a[2] + 2*b[2] + 2*c[2] + e[2])
        if on and hasT[i]:
            out.append(T[i] - T1)
    return np.array(out)


def _fit(ctx, i0, i1, to_mult, p0, base=None, cov=False):
    """Gauss-Newton (2 iterations): to_mult(p) -> (kn, kf, kfil). Returns: p, residual rms (C)[, covariance].
    Covariance: residual samples are 0.25 s apart and strongly correlated; ~one independent sample per 10 s."""
    p = np.array(p0, float)
    for _ in range(2):
        r = _sim(ctx, i0, i1, *to_mult(p), base=base)
        J = np.column_stack([(_sim(ctx, i0, i1, *to_mult(p + REPLAY_EPS * e), base=base) - r) / REPLAY_EPS
                             for e in np.eye(len(p))])
        p = p + np.linalg.lstsq(J, -r, rcond=None)[0]
    r = _sim(ctx, i0, i1, *to_mult(p), base=base)
    rms = float(np.sqrt(np.mean(r ** 2)))
    if not cov:
        return p, rms
    n_eff = max((ctx["rp"]["t"][i1] - ctx["rp"]["t"][i0]) / 10.0, 1.0)
    return p, rms, rms ** 2 * np.linalg.pinv(J.T @ J) * len(r) / n_eff


def replay_baseline(ctx, t_start):
    """The day's loss factor (kb) and, when possible, the filament coefficient (kfil) from the reference period.
    Returns: kb, kfil, relative uncertainty of kfil (1 sigma), note."""
    R = ctx["rp"]; t = R["t"]
    i0 = int(np.searchsorted(t, max(t_start - REF_LEN, 0.0)))
    i1 = int(np.searchsorted(t, t_start))
    sm = ctx["sm"]
    ref = (sm.t >= t[i0]) & (sm.t < t_start)
    if i1 - i0 < 20 or sm.valid[ref].sum() * GRID_DT < REF_MIN or sm.valid[ref].mean() < 0.7:
        return 1.0, 1.0, K_FIL_PRIOR, "referans yok/yetersiz: gunluk carpan 1, filaman katsayisi model (K_FIL)"
    dts = np.diff(t[i0:i1 + 1]); ex = R["ext_sm"][i0 + 1:i1 + 1] > 0.1
    t_ext, t_no = dts[ex].sum(), dts[~ex].sum()
    if t_ext >= REF_EXT_MIN and t_no >= REF_EXT_MIN:
        (kb, kfil), rms, cv = _fit(ctx, i0, i1, lambda p: (p[0], p[0], p[1]), [1.0, 1.0], cov=True)
        sig = float(np.sqrt(cv[1, 1]) / max(abs(kfil), 0.05))
        return kb, kfil, sig, (f"referans {t_start - t[i0]:.0f} s (ekstruzyonlu {t_ext:.0f} s + ekstruzyonsuz {t_no:.0f} s): "
                               f"gunluk kayip carpani {kb:.3f}, filaman yuku yerinde olculdu = K_FIL x {kfil:.2f} "
                               f"(+-{sig*100:.0f}%), uyum {rms:.2f} C")
    (kb,), rms = _fit(ctx, i0, i1, lambda p: (p[0], p[0], 1.0), [1.0])
    why = "hep ekstruzyon var" if t_ext >= REF_EXT_MIN else "ekstruzyon yok"
    return kb, 1.0, K_FIL_PRIOR, (f"referans {t_start - t[i0]:.0f} s: gunluk kayip carpani {kb:.3f}, uyum {rms:.2f} C; "
                                  f"filaman katsayisi ayrica olculemedi ({why}) -> model K_FIL (+-{K_FIL_PRIOR*100:.0f}%)")


def replay_hypotheses(ctx, t_start, t_end, seg):
    """Tests the A/B/C hypotheses by twin replay. Ratios mean the same as in the energy method:
    a = fan cooling lost, b = filament not flowing, c = decrease of the air losses (all relative)."""
    R = ctx["rp"]; t = R["t"]
    kb, kf0, sig, note = replay_baseline(ctx, t_start)
    i0 = int(np.searchsorted(t, t_start)); i1 = int(np.searchsorted(t, t_end, side="right")) - 1
    maps = {"A": lambda p: (kb, kb * (1 - p[0]), kf0),
            "B": lambda p: (kb, kb, kf0 * (1 - p[0])),
            "C": lambda p: (kb * (1 - p[0]), kb * (1 - p[0]), kf0)}
    terms = {"A": seg.L_fan.values, "B": seg.L_fil.values, "C": (seg.L_blok + seg.L_fan).values}
    base_rms = float(np.sqrt(np.mean(_sim(ctx, i0, i1, kb, kb, kf0) ** 2)))
    out = {}
    for h in ("A", "B", "C"):
        if np.mean(np.abs(terms[h])) < MIN_TERM:
            out[h] = dict(ratio=np.nan, rms=np.nan, fits=False, reason=f"terim ~0 ({np.mean(terms[h]):.2f} W): test edilemez")
            continue
        (p,), rms = _fit(ctx, i0, i1, maps[h], [0.0], base=(kb, kb, kf0))
        if h == "B":
            # filament coefficient is uncertain: if the real load is 2 sigma larger, the required ratio shrinks by that much
            p_min = p / (1 + 2 * sig) if p > 0 else p * (1 + 2 * sig)
            ok = BOUNDS["B"][0] <= p_min <= B_MAX
            why = (f"filaman yukunun {p:.2f} kati kaybolmali; K_FIL belirsizligi (+-{sig*100:.0f}%, 2 sigma) "
                   f"ile bile {p_min:.2f} > {B_MAX}") if not ok else (
                   f"(K_FIL +-{sig*100:.0f}% ile en az {p_min:.2f})" if p > B_MAX else "")
            out[h] = dict(ratio=float(p), rms=rms, fits=ok, reason=why)
            continue
        lo, hi = BOUNDS[h]
        ok = lo <= p <= hi
        out[h] = dict(ratio=float(p), rms=rms, fits=ok,
                      reason="" if ok else f"oran {p:+.2f} fiziksel sinir [{lo}, {hi}] disinda")
    return out, note + f"; arizasiz varsayimla ikiz-gercek farki {base_rms:.1f} C", kf0


def rule_checks(sm, w, ctx):
    """Rules D (thermistor) and E (heater), on raw data."""
    notes = []
    t0, t1 = sm.t[w].min(), sm.t[w].max()
    # D: change of the residual over ~1 s (does the reading separate from the model at a non-physical rate)
    t, sel = ctx["t"], ctx["hasT"] & ~ctx["synced"]
    tt, rr = t[sel], ctx["res"][sel]
    win = (tt >= t0) & (tt <= t1)
    tt, rr = tt[win], rr[win]
    if len(tt) > 2:
        j = np.clip(np.searchsorted(tt, tt + 1.0), 0, len(tt) - 1)   # measurement ~1 s later
        span = tt[j] - tt
        ok = (span >= 0.5) & (span < 3.0)
        rate = np.where(ok, (rr[j] - rr) / np.where(ok, span, 1), 0)
        rmax = float(np.max(np.abs(rate))) if ok.any() else 0.0
        if rmax > RES_RATE_MAX:
            notes.append(("D", f"okuma modelden {rmax:.1f} C/s hizla ayrisiyor (normalde < 3 C/s): "
                               "termistor/konnektor sicramasi"))
    # E: heater saturated and temperature falling (>= 10 s)
    seg = sm[w]
    # PID gives an uninterrupted 100 % for 3 s while the temperature falls: does not happen in normal operation
    # (at 100 % during heat-up the temperature RISES). Total time is counted, so intermittent cases (loose crimp)
    # are caught too.
    sat_fall = (seg.pwm_min3 >= SAT_PWM) & (seg.dTdt_fast < -0.5) & (seg["T"] > 100)
    if sat_fall.sum() * GRID_DT >= 5:
        notes.append(("E", f"isitici %{SAT_PWM*100:.0f}+ iken sicaklik toplam {sat_fall.sum()*GRID_DT:.0f} s "
                           "dusuyor: kartusa guc gelmiyor (kopuk/gevsek kablo ya da krimp, bozuk kartus)"))
    # temperature rising while the heater is off
    heat_off_rise = (seg.pwm_max15 <= 0.02) & (seg.dTdt_fast > 0.3) & (seg["T"] > 40)
    if heat_off_rise.sum() * GRID_DT >= 10:
        notes.append(("E", "isitici kapaliyken sicaklik yukseliyor: MOSFET kontrolsuz ya da termistor hatali"))
    return notes


def prepare(df, m):
    """Everything needed for one log is computed once: energy signals + the twin's replay."""
    sm, t, hasT = signals(df, m)
    events = []
    _, res, _, _, synced = d.replay(df, m, events)
    return dict(df=df, m=m, sm=sm, t=t, hasT=hasT, res=res, synced=synced, events=events,
                rp=replay_inputs(df, m))


def classify_window(ctx, t_start, t_end):
    """Classify the window [t_start, t_end] (seconds from log start). Returns: result dict."""
    sm = ctx["sm"]
    rep = {"pencere": (t_start, t_end), "category": [], "notes": [], "hipotezler": {}}

    # reference: valid data before the event
    ref = sm.valid & (sm.t >= t_start - REF_LEN) & (sm.t < t_start - SMOOTH / 2)
    if ref.sum() * GRID_DT >= REF_MIN:
        g0 = float(sm.gap[ref].mean()); noise = float(sm.gap[ref].std())
        rep["notes"].append(f"referans: olaydan onceki {ref.sum()*GRID_DT:.0f} s, eksik guc {g0:+.2f} W (gunluk oynama), "
                             f"gurultu {noise:.2f} W")
    else:
        g0, noise = 0.0, np.nan
        rep["notes"].append("referans penceresi yok (olay log basinda): model esas alindi, g0 = 0")

    w_all = (sm.t >= t_start) & (sm.t <= t_end)
    w = w_all & sm.valid
    rep["kurallar"] = rule_checks(sm, w_all, ctx)
    for cat, txt in rep["kurallar"]:
        rep["category"].append(cat); rep["notes"].append(f"[{cat}] {txt}")
    if rep["kurallar"]:
        rep["birincil"] = rep["kurallar"][0][0]     # a direct rule (D/E) takes precedence over the energy analysis

    if w.sum() * GRID_DT < 15:
        rep["notes"].append(f"pencerede sicaklik tutulan gecerli veri {w.sum()*GRID_DT:.0f} s: enerji analizi yapilamadi")
        if not rep["category"]:
            rep["category"] = ["?"]
        return rep

    seg = sm[w]
    dg = seg.gap.values - g0
    rep["sapma"] = float(dg.mean())
    rep["terimler"] = {k: float(seg[k].mean()) for k in ("L_blok", "L_fan", "L_fil")}
    rep["notes"].append(f"pencerede {w.sum()*GRID_DT:.0f} s gecerli veri: ortalama sapma {dg.mean():+.2f} W  "
                         f"(model: blok {seg.L_blok.mean():.1f} W, fan {seg.L_fan.mean():.2f} W, "
                         f"filaman {seg.L_fil.mean():.2f} W)")
    terms = {"A": seg.L_fan.values, "B": seg.L_fil.values, "C": (seg.L_blok + seg.L_fan).values}
    hyp = fit_hypotheses(dg, terms)
    rep["hipotezler"] = hyp
    feas = {h: r for h, r in hyp.items() if r["fits"]}
    # significance: mean deviation or the part a hypothesis explains (a short extrusion event is diluted in the
    # window but moves together with the filament term, so it is caught here)
    explained = max([abs(r["ratio"]) * np.sqrt(np.mean(terms[h] ** 2)) for h, r in feas.items()], default=0.0)
    if abs(dg.mean()) < MIN_SHIFT and explained < MIN_SHIFT:
        rep["notes"].append(f"anlamli guc farki yok (sapma ve aciklanan kisim < {MIN_SHIFT} W): isi dengesi normal")
        rep["hipotezler"] = {}
        if not rep["category"]:
            rep["category"] = ["-"]
        return rep
    if fan_varies(seg) and seg.L_fil.mean() < MIN_TERM:
        # the fan changed in the window: block and fan terms solved separately, A/C decided from the data
        x, y, rms = fit_two_term(dg, seg)
        rep["ayristirma"] = dict(x=x, y=y, rms=rms)
        rep["notes"].append(f"fan degisti ({seg.fan.min()*100:.0f}-{seg.fan.max()*100:.0f}%): blok ve fan ayri cozuldu -> "
                             f"blok kaybi {x*100:+.0f}% (normal +-{X_BAND*100:.0f}%), fan sogutmasi {y*100:+.0f}% "
                             f"(normal {Y_BAND[0]*100:+.0f}..{Y_BAND[1]*100:+.0f}%), uyum rms {rms:.2f} W")
        if abs(x) > X_BAND:
            cat = "C"
            rep["notes"].append(f"blok kaybi normal bandin disinda: {'corap/cereyan (daha fazla kayip)' if x > 0 else 'yalitim/ortam isindi (daha az kayip)'}"
                                 + ("; fan sogutmasi da degisti (corapsiz blok fan havasini dogrudan alir)"
                                    if not Y_BAND[0] <= y <= Y_BAND[1] else ""))
        elif not Y_BAND[0] <= y <= Y_BAND[1]:
            cat = "A"
            rep["notes"].append("blok kaybi normal, sadece fan sogutmasi degisti: "
                                 + ("hava nozzle'a az ulasiyor (kanal tikali/fan yavas)" if y < 0
                                    else "fazladan sogutma (kanal bloga donmus?)"))
        else:
            cat = "-"
            rep["notes"].append("iki oran da normal bantta")
        rep["category"].append(cat)
        if not rep.get("birincil"):
            rep["birincil"] = cat
    else:
        # single-term hypotheses by twin replay (the energy window above is only for significance)
        hyp, note, _ = replay_hypotheses(ctx, t_start, t_end, seg)
        rep["hipotezler"] = hyp; rep["hipotez_birim"] = "C"
        rep["notes"].append(note)
        feas = {h: r for h, r in hyp.items() if r["fits"]}
        if feas:
            best = min(r["rms"] for r in feas.values())
            tie = max(best * RMS_TIE, best + REPLAY_TIE_C)
            chosen = sorted(h for h, r in feas.items() if r["rms"] <= tie)
            ranked = sorted(chosen, key=lambda h: feas[h]["rms"])
            # with a constant fan A and C give physically the same shape; the rms difference between them comes
            # from the model's internal structure (loss from T1 or T2), not from the data -> both are kept
            ac_tied = "A" in feas and "C" in feas and ranked[0] in ("A", "C")
            if ac_tied:
                chosen = sorted(set(chosen) | {"A", "C"})
            rep["category"] += chosen
            # if the two best fits are practically equal, no "most likely" is named
            if not rep.get("birincil") and not ac_tied and (
                    len(ranked) == 1 or feas[ranked[1]]["rms"] - feas[ranked[0]]["rms"] > 0.1):
                rep["birincil"] = ranked[0]
            if "A" in chosen and "C" in chosen:
                f = seg.fan.values
                rep["notes"].append(f"A ile C ayrilamiyor: pencerede fan {f.min()*100:.0f}-{f.max()*100:.0f}% "
                                     "(fan degismeden 'fan sogutmasi kayboldu' ile 'blok yalitildi' ayni gorunur)")
        else:
            rep["category"].append("?")
            rep["notes"].append("hicbir tek terim sapmayi fiziksel sinirlar icinde aciklamiyor (birlesik ariza?)")

    # onset speed: time for the deviation to rise from 10 % to 90 %
    s = pd.Series(sm.gap.values - g0, index=sm.t).loc[t_start - 60: t_end]
    tgt = dg.mean()
    if abs(tgt) >= MIN_SHIFT and len(s):
        frac = (s / tgt).values
        tt = s.index.values
        i90 = np.argmax(frac >= 0.9) if (frac >= 0.9).any() else None
        if i90 is not None:
            before = np.where(frac[:i90] <= 0.1)[0]
            if len(before):
                rise = tt[i90] - tt[before[-1]]
                rep["baslangic_s"] = float(rise)
                rep["notes"].append(f"sapma {rise:.0f} s icinde olustu ({'ani' if rise < 60 else 'yavas'}; "
                                     f"{SMOOTH:.0f} s yumusatma yuzunden ani bir adim da ~{0.8*SMOOTH:.0f} s gorunur)")
    return rep


CAT_TEXT = {
    "A": "fan yolu (kanal tikali/dondu, fan yavasladi, topak havayi kesiyor)",
    "B": "filaman akmiyor (tikanma, filaman bitti, disli kaziyor)",
    "C": "yalitim/cevre (corap, cereyan, topak blogu sardi, ortam; termistorun yuvasindan kaymasi da "
         "ayni imzayi verir)",
    "D": "termistor",
    "E": "isitici",
    "-": "anlamli sapma yok",
    "?": "belirsiz",
}


def category_line(rep):
    cats = sorted(set(rep["category"]))
    first = rep.get("birincil")
    if first and len(cats) > 1:
        return f"en olasi {first}; {', '.join(c for c in cats if c != first)} de disarida birakilamiyor"
    return " veya ".join(cats)


def format_report(rep, title):
    a, b = rep["pencere"]
    lines = [f"\n=== {title}  [{a:.0f} - {b:.0f} s]"]
    lines += [f"  {n}" for n in rep["notes"]]
    if "ayristirma" in rep and rep.get("hipotezler"):
        lines.append("  (tek terimli uyumlar, sadece bilgi icin - karar yukaridaki ayristirmadan)")
    for h, r in rep.get("hipotezler", {}).items():
        if np.isnan(r["ratio"]):
            lines.append(f"    {h}: {r['reason']}")
        else:
            flag = "uyumlu" if r["fits"] else "ELENDI"
            unit = rep.get("hipotez_birim", "W")
            lines.append(f"    {h}: oran {r['ratio']:+.2f}  uyum rms {r['rms']:.2f} {unit}  -> {flag} {r['reason']}")
    lines.append(f"  KATEGORI: {category_line(rep)}")
    lines += [f"    {c} = {CAT_TEXT.get(c, c)}" for c in sorted(set(rep["category"]))]
    return "\n".join(lines)


def print_report(rep, title):
    print(format_report(rep, title))


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def fault_events(ctx, merge=60.0):
    """Faults confirmed by the twin (same code as the live script), close ones merged.
    Returns: [(start_s, end_s, codes)] relative to the log start."""
    df = ctx["df"]
    t0 = df.t.iloc[0]; t_last = df.t.iloc[-1] - t0
    ev = sorted((a - t0, (b - t0) if b is not None else t_last, c) for c, a, b in ctx["events"])
    out = []
    for a, b, c in ev:
        if out and a <= out[-1][1] + merge:
            out[-1] = (out[-1][0], max(out[-1][1], b), out[-1][2] | {c})
        else:
            out.append((a, b, {c}))
    return out


def find_onset(sm, t_conf, g0_guess_end):
    """Backwards from the confirmation: the moment the deviation started rising (at most 120 s before)."""
    ref = sm.valid & (sm.t >= g0_guess_end - REF_LEN) & (sm.t < g0_guess_end)
    g0 = sm.gap[ref].mean() if ref.sum() * GRID_DT >= REF_MIN else 0.0
    after = sm.valid & (sm.t >= t_conf) & (sm.t < t_conf + 60)
    if not after.any():
        return t_conf
    level = sm.gap[after].mean() - g0
    s = sm[(sm.t >= t_conf - 120) & (sm.t <= t_conf)]
    below = s.t[(s.gap - g0) / level < 0.3] if abs(level) > MIN_SHIFT else pd.Series(dtype=float)
    return float(below.max()) if len(below) else t_conf


def classify_log(path, m=None, window=None):
    m = m or d.load_model()
    ctx = prepare(d.read_log(path), m)
    reps = []
    if window:
        rep = classify_window(ctx, window[0], window[1])
        print_report(rep, f"{os.path.basename(path)}  (elle verilen pencere)")
        return [rep]
    evs = fault_events(ctx)
    if not evs:
        print(f"{os.path.basename(path)}: ikiz onaylanmis ariza bulmadi")
    for a, b, codes in evs:
        onset = find_onset(ctx["sm"], a, a - 60)
        end = min(b, onset + ANALYSIS_LEN)
        rep = classify_window(ctx, onset, end)
        rep["codes"] = codes
        print_report(rep, f"{os.path.basename(path)}  ikiz: {', '.join(sorted(codes))} (onay {a:.0f} s, "
                          f"sapma baslangici ~{onset:.0f} s)")
        reps.append(rep)
    return reps


def classify_live(rows, m, t_confirm):
    """From the live script: rows = last rows in memory (same fields as the CSV), m = running model
    module, t_confirm = moment the fault was confirmed (model clock). Returns: (category line, full report)."""
    df = d.prepare_log(pd.DataFrame(rows))
    ctx = prepare(df, m)
    t0 = df.t.iloc[0]
    a = t_confirm - t0
    onset = find_onset(ctx["sm"], a, a - 60)
    end = min(ctx["t"][-1], onset + ANALYSIS_LEN)
    rep = classify_window(ctx, onset, end)
    title = (f"canli: onaydan {ctx['t'][-1] - a:.0f} s sonra, sapma baslangici onaydan "
             f"{a - onset:.0f} s once (pencere saniyeleri arabellek basina gore)")
    expl = "; ".join(f"{c} = {CAT_TEXT.get(c, c)}" for c in sorted(set(rep["category"])))
    return f"{category_line(rep)}\n      {expl}", format_report(rep, title)


def evaluate_labels():
    """etiketler.csv: dosya, baslangic_s, bitis_s, gozlenen, beklenen (e.g. 'A|C'), set.
    set: 'ayar' = thresholds were tuned looking at these examples (optimistic score), 'sinav' = examples added
    after 2026-10-09, never used for any threshold. Decisions are made on the test score."""
    lab = pd.read_csv(LABEL_FILE)
    m = d.load_model()
    rows = []
    for r in lab.itertuples():
        path = os.path.join(os.path.dirname(LABEL_FILE), r.dosya)
        end = r.bitis_s if pd.notna(r.bitis_s) else 1e9
        if (np.diff(d.read_log(path).t.values) < 0).any():
            # the clock went back in this log (Klipper restart): the even time grid cannot be built [K53]
            rows.append((r.dosya[:40], r.gozlenen, str(r.beklenen), "-", "ATLANDI (saat geri)", getattr(r, "set", "ayar")))
            continue
        rep = classify_log(path, m, (r.baslangic_s, end))[0]
        got = set(rep["category"]); exp = set(str(r.beklenen).split("|"))
        rows.append((r.dosya[:40], r.gozlenen, "|".join(sorted(exp)), "|".join(sorted(got)),
                     "DOGRU" if got == exp else ("KISMEN" if got & exp else "YANLIS"),
                     getattr(r, "set", "ayar")))
    print("\nETIKETLI ORNEKLER")
    for row in rows:
        print(f"  [{row[5]:5s}] {row[0]:42s} {row[1]:32s} beklenen {row[2]:6s} bulunan {row[3]:6s} {row[4]}")
    for s in ("ayar", "sinav"):
        res = [row[4] for row in rows if row[5] == s]
        if res:
            print(f"  {s}: {res.count('DOGRU')}/{len(res)} dogru, {res.count('KISMEN')} kismen, {res.count('YANLIS')} yanlis"
                  + ("  (esikler bu orneklere gore ayarlandi: iyimser)" if s == "ayar" else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Physical fault category for the twin's alarms (diagnostic only).")
    ap.add_argument("log", nargs="?", help="session log: classify the alarms the twin found")
    ap.add_argument("--window", nargs=2, type=float, metavar=("START", "END"),
                    help="classify this window instead (seconds from log start)")
    ap.add_argument("--labels", action="store_true",
                    help="score against benchmark/ariza_ornekleri/etiketler.csv")
    a = ap.parse_args()
    if a.labels:
        evaluate_labels()
    elif a.log:
        classify_log(a.log, window=a.window)
    else:
        ap.print_help()
