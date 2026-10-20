"""Structure candidate S3d (KARARLAR K40): heat loss from the hot heater cartridge, driven by T3 (radiative EPS3,
optional linear G3A),
with a fair scoring set-up: the k learner and the missing-power monitor take the cartridge loss out of the heater
power (steady estimate T3 ~ T1 + (P - Q_cart) * R31), so it is not counted twice; logs replayed from their live
start get their recorded k scaled by K_MAP (k means "block loss only" in this structure).
Experimental and in memory only (patches realtime_websocket after every evaluate.load_model). Touches no file.

Usage (in dt/):  [set K_MAP=1.16]  py benchmark/structure_cartridge_loss.py LOG ... [calibrate.py options]
K40 run: K_MAP=1.16, --params C_ALL,R31,R_HB,CRAD,HA2_FAN,CAP_T_COEF,EPS3 --score
"""
import math
import os
import sys

DT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DT)
os.chdir(DT)
import evaluate as ev  # noqa: E402
import realtime_websocket as rw  # noqa: E402

EXTRA = {"EPS3": 3.0e-11, "G3A": 0.0}   # radiation from the cartridge; no linear term
K_MAP = float(os.environ.get("K_MAP", "1.16"))


def q_cart_T3(m, T3):
    T3K = T3 + 273.15
    return m.G3A * (T3 - m.T_AMB) + m.EPS3 * (T3K ** 4 - m.T_AMB_K ** 4)


def q_cart_steady(m, T1, P):
    """Cartridge loss in steady state at block temperature T1 and heater power P (two fixed-point passes)."""
    q = 0.0
    for _ in range(3):
        q = q_cart_T3(m, T1 + max(P - q, 0.0) * m.R31)
    return q


def model_step_s3b(T1, T2, T3, pwm, fan, extrude_v, dt, k_loss=1.0, max_substep=None, k_fil=1.0):
    m = rw
    if max_substep is None:
        max_substep = m.max_stable_substep(fan, extrude_v)

    def deriv(T1, T2, T3):
        Q_filament = k_fil * m.K_FIL * max(extrude_v, 0.0) * (T1 - m.T_AMB)
        Q_conv1 = k_loss * m.natural_loss(T1) + Q_filament
        Q_heater_to_1 = (T3 - T1) / m.R31
        Q_hb = (T1 - T2) / m.R_HB
        dT1 = (Q_heater_to_1 - Q_hb - Q_conv1) / (m.C1 * m.cap_scale(T1))
        Q_c2 = m.HA2 * (T2 - m.T_AMB)
        Q_fan = k_loss * m.fan_conductance(fan) * (T2 - m.T_AMB)
        dT2 = (Q_hb - Q_c2 - Q_fan) / (m.C2 * m.cap_scale(T2))
        dT3 = (pwm * m.P_MAX - Q_heater_to_1 - q_cart_T3(m, T3)) / (m.C3 * m.cap_scale(T3))
        return dT1, dT2, dT3

    n_sub = max(1, math.ceil(dt / max_substep))
    h = dt / n_sub
    for _ in range(n_sub):
        k1 = deriv(T1, T2, T3)
        k2 = deriv(T1 + h / 2 * k1[0], T2 + h / 2 * k1[1], T3 + h / 2 * k1[2])
        k3 = deriv(T1 + h / 2 * k2[0], T2 + h / 2 * k2[1], T3 + h / 2 * k2[2])
        k4 = deriv(T1 + h * k3[0], T2 + h * k3[1], T3 + h * k3[2])
        T1 += h / 6 * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0])
        T2 += h / 6 * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1])
        T3 += h / 6 * (k1[2] + 2 * k2[2] + 2 * k3[2] + k4[2])
    return T1, T2, T3


def initial_state_s3b(T1, pwm, fan):
    T1, T2, T3 = _orig["initial_state"](T1, pwm, fan)
    if pwm and pwm > 0:                     # hot start: T3 carries the loss through R31 plus its own loss
        T3 = T1 + (T3 - T1) + 0.0
    return T1, T2, T3


_orig = {}


def install(m):
    for k, v in EXTRA.items():
        if not hasattr(m, k):
            setattr(m, k, v)
    m.model_step = model_step_s3b
    L, E = m.KLossLearner, m.EnergyMonitor
    if not getattr(L, "_s3b", False):
        u, up, eu = L.update, L.update_print, E.update

        def update(self, now, dt, T1_real, pwm, fan, extruding):
            P = pwm * m.P_MAX
            return u(self, now, dt, T1_real, (P - q_cart_steady(m, T1_real, P)) / m.P_MAX, fan, extruding)

        def update_print(self, now, dt, T1_real, pwm, fan, ext_v, energy_ok, blocked):
            P = pwm * m.P_MAX
            return up(self, now, dt, T1_real, (P - q_cart_steady(m, T1_real, P)) / m.P_MAX, fan, ext_v,
                      energy_ok, blocked)

        def e_update(self, now, dt, T1_prev, T1_now, pwm, fan, ext, power_scale=1.0, k=1.0, kfil=1.0):
            P = pwm * m.P_MAX * power_scale
            pe = (P - q_cart_steady(m, T1_now, P)) / (m.P_MAX * power_scale)
            return eu(self, now, dt, T1_prev, T1_now, pe, fan, ext, power_scale, k, kfil)

        L.update, L.update_print, E.update, L._s3b = update, update_print, e_update, True


install(rw)
_orig_load, _orig_live = ev.load_model, ev.live_start


def load_model(overrides=None):
    m = _orig_load({k: v for k, v in (overrides or {}).items() if k not in EXTRA})
    install(m)
    for k, v in (overrides or {}).items():
        if k in EXTRA:
            setattr(m, k, v)
    return m


def live_start(df):
    k, kf, r = _orig_live(df)
    return k * K_MAP, kf, r * K_MAP


ev.load_model, ev.live_start = load_model, live_start
_orig_eval = ev.evaluate


def evaluate(overrides=None, paths=None, chain=False, live=False):
    """A fresh start (k = 1 in the current structure) is k = K_MAP here: replay fresh logs as a live start whose
    recorded k is 1 (live_start scales it)."""
    if not live and not chain:
        saved = ev.live_start
        ev.live_start = lambda df: (K_MAP, 1.0, K_MAP)
        try:
            return _orig_eval(overrides, paths, chain=False, live=True)
        finally:
            ev.live_start = saved
    return _orig_eval(overrides, paths, chain, live)


ev.evaluate = evaluate
import calibrate  # noqa: E402

if __name__ == "__main__":
    sys.argv = ["calibrate.py"] + sys.argv[1:]
    calibrate.main()
