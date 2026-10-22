"""
Closed-loop simulation: Klipper's PID is put inside the twin, so the heater power is no longer an INPUT but
PREDICTED by the twin. The core of the pre-print prediction (G-code -> temperature/power).

Shadow (live twin):   real pwm -> [hotend model] -> T_model   (residual = T_real - T_model)
Closed loop (this):   target -> [Klipper PID] -> pwm_pred -> [hotend model] -> T_pred
                                    ^-------------- T_pred (feedback) ----------'
                      filament flow and fan enter the model as disturbances.

Test (no G-code needed): a log's target temperature, fan, filament flow and bed power are fed to this loop;
the heater power and temperature the twin predicts are compared with the REAL pwm and temperature in the
log. If the twin also predicts the power correctly, it really "imitates" the printer.

PID: an exact copy of klippy/extras/heaters.py ControlPID (classic positional form):
    Kp, Ki, Kd = pid_kp/255, pid_ki/255, pid_kd/255
    derivative: smoothed with min_deriv_time (= smooth_time, default 1 s)
    integral clamped to [0, max_power/Ki]; not updated while the output is saturated (anti-windup)
    heater off while target <= 0.
Temperature readings and PID updates every REPORT_TIME (0.3 s) - Klipper's ADC report interval.

Usage:
    python closed_loop.py --set calibration   # benchmark/setler.csv (PID changed 26.09: logs before 28.09 skipped)
    python closed_loop.py --set test
    python closed_loop.py LOG.csv
"""
import argparse
import os

import numpy as np
import pandas as pd

import evaluate as d

# Hotend PID history from the SAVE_CONFIG backups on the Pi (printer-YYYYMMDD_HHMMSS.cfg keeps the content BEFORE a
# save at that time) [K43]: (from, (Kp, Ki, Kd)). The first set is assumed from PID_SINCE (backups show it from
# 2026-10-07 22:33); before this table only 40.262/12.201/33.216 was used for every log.
PID_HISTORY = [
    (pd.Timestamp("2026-09-28").timestamp(), (40.837, 13.612, 30.628)),
    (pd.Timestamp("2026-10-08 17:50:33", tz="Europe/Istanbul").timestamp(), (40.262, 12.201, 33.216)),
    (pd.Timestamp("2026-10-13 11:06:51", tz="Europe/Istanbul").timestamp(), (38.530, 12.232, 30.342)),   # after the maintenance
]
PID_KP, PID_KI, PID_KD = PID_HISTORY[-1][1]   # current gains (default)


def pid_gains_at(t):
    """Hotend PID gains in use at wall time t (s)."""
    gains = PID_HISTORY[0][1]
    for since, g in PID_HISTORY:
        if t >= since:
            gains = g
    return gains
PID_PARAM_BASE = 255.0
SMOOTH_TIME = 1.0          # [extruder] smooth_time (not in printer.cfg -> Klipper default)
MAX_POWER = 1.0
REPORT_TIME = 0.3          # s - Klipper ADC temperature report / PID update interval
PID_SINCE = pd.Timestamp("2026-09-28").timestamp()   # first day this PID was certainly in use
AVG = 10.0                 # s - power is compared with this average (energy, not the PID's instant ripple)


# Derived from Klipper (klippy/extras/heaters.py, ControlPID), Copyright (C) Kevin O'Connor, GPLv3 [K58].
class KlipperPID:
    """Exact copy of klippy/extras/heaters.py ControlPID."""

    PID_SETTLE_DELTA, PID_SETTLE_SLOPE = 1.0, 0.1    # M109/M190 wait condition (heaters.py)

    def __init__(self, T0, pwm0=0.0, target0=0.0, gains=(PID_KP, PID_KI, PID_KD)):
        self.Kp, self.Ki, self.Kd = (g / PID_PARAM_BASE for g in gains)
        self.integ_max = MAX_POWER / self.Ki
        self.prev_T, self.prev_t, self.prev_deriv = T0, None, 0.0
        # when starting mid-log, the integral is set so that it matches the real power at that moment
        err = target0 - T0
        self.prev_integ = min(self.integ_max, max(0.0, (pwm0 - self.Kp * err) / self.Ki)) if target0 > 0 else 0.0

    def update(self, t, T, target):
        dt = REPORT_TIME if self.prev_t is None else t - self.prev_t
        dT = T - self.prev_T
        if dt >= SMOOTH_TIME:
            deriv = dT / dt
        else:
            deriv = (self.prev_deriv * (SMOOTH_TIME - dt) + dT) / SMOOTH_TIME
        err = target - T
        integ = min(self.integ_max, max(0.0, self.prev_integ + err * dt))
        co = self.Kp * err + self.Ki * integ - self.Kd * deriv
        out = min(MAX_POWER, max(0.0, co))
        self.prev_T, self.prev_t, self.prev_deriv = T, t, deriv
        if co == out:
            self.prev_integ = integ
        return out if target > 0 else 0.0

    def busy(self, smoothed_T, target):
        """Klipper ControlPID.check_busy: M109/M190 wait until this is False."""
        return abs(target - smoothed_T) > self.PID_SETTLE_DELTA or abs(self.prev_deriv) > self.PID_SETTLE_SLOPE


def inputs(df):
    """Inputs from the log on the REPORT_TIME grid (zero-order hold): target, fan, net flow, bed."""
    t = df.t.values
    grid = np.arange(t[0], t[-1], REPORT_TIME)
    idx = np.clip(np.searchsorted(t, grid, side="right") - 1, 0, len(t) - 1)
    tg = pd.to_numeric(df.target, errors="coerce").ffill().fillna(0.0).values
    bp = pd.to_numeric(df.bed_pwm, errors="coerce").ffill().fillna(0.0).values if "bed_pwm" in df else np.zeros(len(df))
    # same net flow as the twin: time average of the signed speed over the last 10 s, clipped below 0
    ev = pd.Series(df.extrude_velocity.values, index=pd.to_datetime(t, unit="s"))
    ext = np.maximum(ev.rolling(f"{d.rw.EXT_AVG_WINDOW:.0f}s").mean().values, 0.0)
    return grid, tg[idx], df.fan_state.values[idx], ext[idx], bp[idx], idx


def simulate(df, m, k=1.0, kfil=1.0):
    """Closed loop: returns (grid, T_pred, pwm_pred, row index in the log)."""
    grid, tg, fan, ext, bp, idx = inputs(df)
    T0 = float(df.T1_real.iloc[0])
    T1, T2, T3 = m.initial_state(T0, float(df.pwm.iloc[0]), float(df.fan_state.iloc[0]))
    pid = KlipperPID(T0, float(df.pwm.iloc[0]), float(tg[0]), gains=pid_gains_at(float(df.time.iloc[0])))
    Ts, Ps = np.empty(len(grid)), np.empty(len(grid))
    for i, t in enumerate(grid):
        pwm = pid.update(t, T1, tg[i])          # thermistor = the model's T1 node
        Ts[i], Ps[i] = T1, pwm
        scale = 1.0 - m.BED_SAG * bp[i]
        T1, T2, T3 = m.model_step(T1, T2, T3, pwm * scale, fan[i], ext[i], REPORT_TIME, k, k_fil=kfil)
    return grid, Ts, Ps, idx


def score(df, grid, Ts, Ps, idx):
    """Comparison with reality: power (W, 10 s average) and temperature while printing; heat-up time."""
    P_real = pd.to_numeric(df.pwm, errors="coerce").ffill().values[idx] * d.rw.P_MAX
    T_real = df.T1_real.ffill().values[idx]
    tg = pd.to_numeric(df.target, errors="coerce").ffill().fillna(0.0).values[idx]
    n = max(1, int(AVG / REPORT_TIME))
    avg = lambda x: pd.Series(x).rolling(n, min_periods=n).mean().values
    Pr, Pp = avg(P_real), avg(Ps * d.rw.P_MAX)
    hold = (tg >= 230) & (np.abs(T_real - tg) < 3) & (grid - grid[0] > 300)
    hold &= ~np.isnan(Pr)
    out = dict(hold_min=hold.sum() * REPORT_TIME / 60)
    if hold.any():
        e = Pp[hold] - Pr[hold]
        out.update(power_bias=e.mean(), power_rmse=np.sqrt(np.mean(e ** 2)), P_real_mean=Pr[hold].mean(),
                   T_rmse=np.sqrt(np.mean((Ts[hold] - T_real[hold]) ** 2)),
                   sat_real_pct=np.mean(P_real[hold] >= 0.99 * d.rw.P_MAX) * 100,
                   sat_pred_pct=np.mean(Ps[hold] >= 0.99) * 100)
    # heat-up: time from the target rising above 230 to reaching target - 2 C (real and predicted)
    up = np.flatnonzero((tg[1:] >= 230) & (tg[:-1] < 230))
    if len(up):
        i0 = up[0] + 1
        def reach(T):
            j = np.flatnonzero(T[i0:] >= tg[i0] - 2.0)
            return (grid[i0 + j[0]] - grid[i0]) if len(j) else np.nan
        if T_real[i0] < tg[i0] - 20:
            out.update(heat_real_s=reach(T_real), heat_pred_s=reach(Ts))
    return out


def run(paths, chain_k=True):
    m = d.load_model()
    rows = []
    k_prev, kfil_prev = 1.0, 1.0
    for p in paths:
        df = d.read_log(p)
        if df.time.iloc[0] < PID_SINCE:
            continue
        # k, k_fil: what the live twin would know BEFORE this print = learned at the end of the previous log
        # (from the shadow replay). The k learned from this log's own measurements is not used (that would leak).
        k_use, kfil_use = (k_prev, kfil_prev) if chain_k else (1.0, 1.0)
        grid, Ts, Ps, idx = simulate(df, m, k_use, kfil_use)
        r = dict(log=d.short_name(p), k=k_use, **score(df, grid, Ts, Ps, idx))
        rows.append(r)
        if chain_k:
            st = {}
            _, _, _, ks, _ = d.replay(df, m, k0=k_prev, kfil0=kfil_prev, state=st)
            k_prev, kfil_prev = float(ks[-1]), st["kfil"]
        print(f"  {r['log']:22s} k {k_use:.3f}  baski {r.get('hold_min', 0):5.1f} dk  "
              + (f"guc gercek {r['P_real_mean']:5.2f} W  tahmin hatasi {r['power_bias']:+5.2f} W (rms {r['power_rmse']:4.2f})  "
                 f"T rms {r['T_rmse']:4.2f} C  doyum %{r['sat_real_pct']:.1f}/%{r['sat_pred_pct']:.1f}" if 'power_bias' in r else "")
              + (f"  isinma {r['heat_real_s']:.0f}/{r['heat_pred_s']:.0f} s" if 'heat_real_s' in r else ""))
    return pd.DataFrame(rows)


def summarize(sc, title):
    s = sc.dropna(subset=["power_bias"])
    w = s.hold_min
    print(f"\n{title}: {len(s)} log, {w.sum():.0f} dk baski")
    print(f"  guc tahmini: bias {np.average(s.power_bias, weights=w):+.2f} W, rms {np.sqrt(np.average(s.power_rmse**2, weights=w)):.2f} W "
          f"(gercek ort {np.average(s.P_real_mean, weights=w):.1f} W)")
    print(f"  sicaklik tahmini (baskida): rms {np.sqrt(np.average(s.T_rmse**2, weights=w)):.2f} C")
    h = sc.dropna(subset=["heat_real_s", "heat_pred_s"])
    if len(h):
        print(f"  isinma suresi: gercek ort {h.heat_real_s.mean():.0f} s, tahmin ort {h.heat_pred_s.mean():.0f} s, "
              f"ort mutlak fark {np.mean(np.abs(h.heat_pred_s - h.heat_real_s)):.0f} s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Closed-loop (PID) validation: simulate the heater from the target.")
    ap.add_argument("log", nargs="?", help="one log (default: the test set)")
    ap.add_argument("--set", choices=list(d.SET_NAMES), help="log set from benchmark/setler.csv")
    ap.add_argument("--k1", action="store_true", help="fixed k = 1 (no k carried over from the previous session)")
    a = ap.parse_args()
    paths = [a.log] if a.log else d.set_paths(d.SET_NAMES[a.set or "test"])
    sc = run(paths, chain_k=not a.k1)
    summarize(sc, a.set or os.path.basename(a.log or ""))
