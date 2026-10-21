"""
Heated-bed digital twin - a model file INDEPENDENT of the nozzle twin.

Structure (2 nodes):
    [heater pad + aluminium plate, T_al]  --R_AC-->  [glass surface (carbon glass), T_cam]
       ^ the thermistor measures here                   ^ the surface the print sees, not measured (estimated)
    Losses: aluminium underside/edges H_AL*(T_al - T_amb); radiation from the glass top (eps*area*sigma*(T^4-Ta^4))
            + H_CAM*(T_cam - T_amb) (came out ~0 in the experiment; convection is lumped on the aluminium side).

Parameters: open-loop fit to the bed experiment (2026-09-29, 60/85/100 C steps + 38 min cool-down, nozzle off,
bed not moving). Physical check: C_AL ~ 450 g aluminium + pad (~400 J/K expected),
C_CAM ~ 550 g glass (~460-550 J/K expected).

While printing, the bed loses MORE heat than in the experiment (the bed moves along Y all the time and
the part fan blows on it; hold bias +3.3 / +7.4 C in two prints). A bed loss factor (BedLossLearner),
like k_loss on the nozzle, learns this slowly while the temperature is being held.

Usage (offline evaluation):
    python bed_model.py            # scores on recorded logs, with and without learning
"""
import math
from collections import deque

T_AMB = 25.0
P_BED = 220.0          # W - Ender 3 bed heater nominal (24 V); its own voltage drop is absorbed in the parameters
C_AL = 422.8           # J/K - heater pad + aluminium
C_GLASS = 582.3          # J/K - glass
R_AC = 0.1369          # K/W - aluminium -> glass (tau = R_AC*C_CAM ~ 1.3 min)
H_AL = 1.249           # W/K - aluminium underside/edge loss (linear; most of the bed's convection)
H_GLASS = 0.0            # W/K - linear loss from the glass top (~0 in the experiment)
EPS_A = 0.9 * 0.235 * 0.235   # glass top surface: emissivity * area (m^2)
SIG = 5.670e-8
# Extra loss while printing (bed v1, 2026-09-29; from the hold windows of 6 logs):
#   loss factor = 1 + BED_FAN*fan + BED_ACT*activity
# fan: part fan (0-1) air hits the print/glass (~18 % extra loss at full fan).
# activity: fraction of the last ACT_WINDOW seconds the head was moving (the bed moves along Y).
# Both reset when the print ends: cool-down uses the loss of the static experiment.
BED_FAN = 0.18
BED_ACT = 0.035
ACT_WINDOW = 30.0      # s
ACT_SPEED = 5.0        # mm/s - head faster than this counts as "moving"
MAX_SUBSTEP = 1.0      # s
GAP_THRESHOLD = 10.0   # s - re-seat on the measurement after a longer data gap

# bed loss factor learning (only while the temperature is HELD)
LEARN_WINDOW = 60.0    # s - energy balance window
LEARN_TAU = 300.0      # s - learning time constant
LEARN_T_RANGE = 1.0    # C - bed temperature range in the window below this counts as a "hold"
LEARN_TMIN = 45.0      # C - at low temperature the loss is small and the ratio is noisy
K_MIN, K_MAX = 0.7, 1.6


def rad(T):
    return SIG * EPS_A * ((T + 273.15) ** 4 - (T_AMB + 273.15) ** 4)


def losses(T_al, T_glass):
    """Losses to air (W): aluminium side, glass side."""
    return H_AL * (T_al - T_AMB), H_GLASS * (T_glass - T_AMB) + rad(T_glass)


def step(T_al, T_glass, pwm, dt, k=1.0):
    """Explicit Euler with small substeps (fastest mode ~R_AC*C_AL ~ 1 min; 1 s steps are comfortably stable)."""
    n = max(1, math.ceil(dt / MAX_SUBSTEP)); h = dt / n
    for _ in range(n):
        q = (T_al - T_glass) / R_AC
        l_al, l_glass = losses(T_al, T_glass)
        T_al += h * (pwm * P_BED - q - k * l_al) / C_AL
        T_glass += h * (q - k * l_glass) / C_GLASS
    return T_al, T_glass


def initial_glass(T_al, pwm):
    """Glass at start: steady state if the bed is heating (heat out of the glass = heat in), else T_al."""
    if not pwm or pwm <= 0 or T_al < 35:
        return T_al
    Tg = T_al
    for _ in range(50):                      # q = (T_al - Tg)/R = glass_loss(Tg) -> fixed point
        Tg = T_al - R_AC * (H_GLASS * (Tg - T_AMB) + rad(Tg))
    return Tg


class BedLossLearner:
    """Loss factor k from the energy balance while the bed is held: k = (supplied - stored) / model loss."""

    def __init__(self):
        self.k = 1.0
        self.t0 = None

    def update(self, now, dt, T_al, T_glass, pwm, mult=1.0):
        """mult: known extra loss factor (fan, activity); k learns only the difference BEYOND it."""
        # heater on or not: judged from the window's AVERAGE power (below). 2026-10-14: before, every sample
        # had to have pwm > 0.02; the bed PID output drops to 0 and back to 1 every minute (Kd 948), so almost
        # every window was discarded and k stayed at 1.00 for 23 min of printing (needed ~1.20, residual -9 C).
        # With the average condition: 12 logs hold rms 2.76 -> 1.40 (bias -1.17 -> -0.30), heat-up the same,
        # cool-down 1.80 -> 1.63.
        ok = T_al > LEARN_TMIN
        if self.t0 is None:
            self.t0, self.T_first, self.T_min, self.T_max = now, T_al, T_al, T_al
            self.Tc_first = T_glass
            self.E_in = self.E_loss = 0.0
            self.ok = ok
            return self.k
        l_al, l_glass = losses(T_al, T_glass)
        self.E_in += pwm * P_BED * dt
        self.E_loss += (l_al + l_glass) * mult * dt
        self.T_min, self.T_max = min(self.T_min, T_al), max(self.T_max, T_al)
        self.ok = self.ok and ok
        span = now - self.t0
        if span >= LEARN_WINDOW:
            if (self.ok and self.E_in > 0.02 * P_BED * span and self.T_max - self.T_min < LEARN_T_RANGE
                    and self.E_loss > 0):
                stored = C_AL * (T_al - self.T_first) + C_GLASS * (T_glass - self.Tc_first)
                k_win = (self.E_in - stored) / self.E_loss
                self.k += min(1.0, span / LEARN_TAU) * (k_win - self.k)
                self.k = min(K_MAX, max(K_MIN, self.k))
            self.t0 = None
            self.update(now, 0.0, T_al, T_glass, pwm, mult)
        return self.k


class BedTwin:
    """Message handler (same layout as the nozzle Twin): advances the elapsed interval with the bed power at
    the START of the interval, then takes the new inputs, computes the residual if a new temperature came."""

    def __init__(self, T_al, pwm=0.0, learn=True, extras=True):
        self.T_model = T_al
        self.T_glass = initial_glass(T_al, pwm)
        self.pwm = pwm or 0.0
        self.fan = 0.0
        self.moving = False
        self.T_real = T_al
        self.prev = None
        self.learn = learn
        self.extras = extras               # False: no fan/activity effect (static experiment model)
        self.learner = BedLossLearner()
        self.act_hist = deque()            # (interval end, dt, moving or not)
        self.act = 0.0

    def mult(self):
        return 1.0 + BED_FAN * self.fan + BED_ACT * self.act if self.extras else 1.0

    def on_message(self, now, T_real=None, pwm=None, fan=None, head_v=None, ext=None):
        """Without head_v (head speed) activity is taken from extrusion (ext)."""
        out = {"residual": None, "resynced": False}
        if self.prev is not None:
            dt = now - self.prev
            if dt > GAP_THRESHOLD or dt < 0:
                T0 = T_real if T_real is not None else self.T_real
                self.T_model, self.T_glass = T0, initial_glass(T0, self.pwm)
                self.learner.t0 = None
                self.act_hist.clear()
                out["resynced"] = True
            else:
                self.act_hist.append((now, dt, 1.0 if self.moving else 0.0))
                while self.act_hist and self.act_hist[0][0] < now - ACT_WINDOW:
                    self.act_hist.popleft()
                span = sum(h[1] for h in self.act_hist)
                self.act = sum(h[1] * h[2] for h in self.act_hist) / span if span > 0 else 0.0
                k = self.learner.k if self.learn else 1.0
                self.T_model, self.T_glass = step(self.T_model, self.T_glass, self.pwm, dt, k * self.mult())
                if self.learn:
                    # learning uses the MEASURED aluminium temperature and the model's glass estimate
                    self.learner.update(now, dt, self.T_real, self.T_glass, self.pwm, self.mult())
        self.prev = now
        if pwm is not None:
            self.pwm = pwm
        if fan is not None:
            self.fan = fan
        if head_v is not None:
            self.moving = head_v > ACT_SPEED
        elif ext is not None:
            self.moving = abs(ext) > 0.1
        if T_real is not None:
            self.T_real = T_real
            if not out["resynced"]:
                out["residual"] = T_real - self.T_model
        return out


# ---------------------------------------------------------------------------
# Offline evaluation
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import numpy as np
    import pandas as pd

    HERE = os.path.dirname(os.path.abspath(__file__))
    L = os.path.join(HERE, "loglar")
    LOGS = [("tabla deneyi (egitim)", os.path.join(L, "deneyler", "tabla_deney1.csv")),
            ("28.09 string", os.path.join(L, "baskilar", "2809_string_temiz.csv")),
            ("28.09 aksam", os.path.join(L, "baskilar", "2809_aksam_240C.csv")),
            ("29.09 benchy1", os.path.join(L, "baskilar", "2909_benchy1.csv")),
            ("29.09 geri cekme", os.path.join(L, "baskilar", "2909_geri_cekme_testi.csv")),
            ("29.09 benchy2", os.path.join(L, "baskilar", "2909_benchy2.csv")),
            ("29.09 huni", os.path.join(L, "baskilar", "2909_huni.csv")),
            ("29.09 topak/havada", os.path.join(L, "baskilar", "2909_nozzle_filament_havada_baski.csv")),
            ("29.09 aksam uzun", os.path.join(L, "baskilar", "2909_aksam_uzun.csv"))]

    def replay(path, learn, extras):
        d = pd.read_csv(path, low_memory=False)
        for c in ("eventtime", "bed_T", "bed_pwm", "fan_state", "head_v", "extrude_velocity"):
            if c in d:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        d = d.dropna(subset=["eventtime"]).reset_index(drop=True)
        # start where the bed power is first known (old script copies did not see an unchanged power:
        # at the start of 29.09 benchy1 the power column is empty while the bed heats at 100 %)
        d = d.iloc[int(d.bed_pwm.first_valid_index()):].reset_index(drop=True)
        T = d.bed_T.ffill().bfill().values; u = d.bed_pwm.ffill().fillna(0).values; t = d.eventtime.values
        fan = d.fan_state.values; ext = d.extrude_velocity.values
        hv = d.head_v.values if "head_v" in d else np.full(len(d), np.nan)
        newT = np.r_[True, np.diff(T) != 0]                       # rows where the bed temperature changed
        tw = BedTwin(T[0], u[0], learn, extras)
        res = np.full(len(d), np.nan); ks = np.ones(len(d))
        for i in range(len(d)):
            r = tw.on_message(t[i], T[i] if newT[i] else None, u[i],
                              None if np.isnan(fan[i]) else fan[i], None if np.isnan(hv[i]) else hv[i],
                              None if np.isnan(ext[i]) else ext[i])
            if r["residual"] is not None:
                res[i] = r["residual"]
            ks[i] = tw.learner.k
        return T, u, res, ks

    VARIANTS = [("hareketsiz model", False, False), ("+ fan/aktivite", False, True), ("+ fan/akt. + ogrenme", True, True)]
    print(f"{'':22s} {'':22s} {'RMSE':>5s} {'isinma':>7s} {'tutma (bias)':>14s} {'soguma':>7s} {'son k':>6s}")
    for name, p in LOGS:
        for vname, learn, extras in VARIANTS:
            T, u, res, ks = replay(p, learn, extras)
            m = {"isinma": u > 0.95, "tutma": (u > 0.05) & (u < 0.95), "soguma": (u == 0) & (T > 30)}
            f = lambda w: np.sqrt(np.nanmean(res[w] ** 2)) if w.any() else float("nan")
            print(f"{name:22s} {vname:22s} {np.sqrt(np.nanmean(res**2)):5.2f} {f(m['isinma']):7.2f} "
                  f"{f(m['tutma']):6.2f} ({np.nanmean(res[m['tutma']]):+5.2f}) {f(m['soguma']):7.2f} {ks[-1]:6.3f}")
        print()
