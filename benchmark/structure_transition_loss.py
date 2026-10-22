"""Candidate (KARARLAR K60 note): phenomenological transition loss Q_tr = beta * |dT1/dt| on the block node.

A known limitation, not a physical model: while the temperature changes the real hotend loses ~5 % more than in a
hold, in both directions (K60), cause unknown (needs a second sensor). The term is zero in a steady hold, so the
learner, the missing-power monitor, GUC and KAYIP see no change: it only moves the model temperature in transients.
It is solved implicitly: C1 dT1/dt = F - beta*|dT1/dt|  ->  dT1/dt = F / (C1 + beta) when heating (F > 0) and
F / (C1 - beta) when cooling, i.e. a larger effective block capacity on the way up and a smaller one on the way down.
BETA_DOWN (default = BETA) gives the two directions separate values; HEATER_ONLY=1 applies it only while pwm > 0;
TR_TAU > 0 uses the model T1 rate low-passed over TR_TAU s instead (the raw rate is not zero in a hold, K61).
Result: rejected (KARARLAR K61).
Experimental and in memory only (patches realtime_websocket after every evaluate.load_model). Touches no file.

Usage (in dt/):  set BETA=0.3 & py benchmark/structure_transition_loss.py evaluate.py [evaluate options]
                 (or import it before evaluate / calibrate in a script)
"""
import importlib
import math
import os
import runpy
import sys

DT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DT)
os.chdir(DT)
import evaluate as ev  # noqa: E402
import realtime_websocket as rw  # noqa: E402

BETA = float(os.environ.get("BETA", "0.0"))                     # J/K - heating side
BETA_DOWN = float(os.environ.get("BETA_DOWN", str(BETA)))       # J/K - cooling side
HEATER_ONLY = os.environ.get("HEATER_ONLY", "0") == "1"
DOWN_FLOOR = 0.25                                               # effective cooling capacity never below 25 % of C1


def make_step(m):
    def model_step(T1, T2, T3, pwm, fan, extrude_v, dt, k_loss=1.0, max_substep=None, k_fil=1.0):
        bu, bd = (0.0, 0.0) if m.HEATER_ONLY and pwm <= 0 else (m.BETA, m.BETA_DOWN)
        if m.TR_TAU > 0:
            return smoothed_step(T1, T2, T3, pwm, fan, extrude_v, dt, k_loss, max_substep, k_fil)
        if max_substep is None:
            max_substep = m.max_stable_substep(fan, extrude_v)
            c_min = m.C1 * (m.cap_scale(0.0) if m.CAP_T_COEF > 0 else 1.0)
            max_substep *= max(c_min - bd, DOWN_FLOOR * c_min) / c_min   # faster T1 mode while cooling

        def deriv(T1, T2, T3):
            Q_filament = k_fil * m.K_FIL * max(extrude_v, 0.0) * (T1 - m.T_AMB)
            Q_conv1 = k_loss * m.natural_loss(T1) + Q_filament
            Q_heater_to_1 = (T3 - T1) / m.R31
            Q_hb = (T1 - T2) / m.R_HB
            F = Q_heater_to_1 - Q_hb - Q_conv1
            C = m.C1 * m.cap_scale(T1)
            dT1 = F / (C + bu) if F > 0 else F / max(C - bd, DOWN_FLOOR * C)
            Q_c2 = m.HA2 * (T2 - m.T_AMB)
            Q_fan = k_loss * m.fan_conductance(fan) * (T2 - m.T_AMB)
            dT2 = (Q_hb - Q_c2 - Q_fan) / (m.C2 * m.cap_scale(T2))
            dT3 = (pwm * m.P_MAX - Q_heater_to_1) / (m.C3 * m.cap_scale(T3))
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

    def smoothed_step(T1, T2, T3, pwm, fan, extrude_v, dt, k_loss, max_substep, k_fil):
        """TR_TAU > 0: the rate is the model T1 rate low-passed over TR_TAU s, applied explicitly as a block loss
        (beta_up while the smoothed rate rises, beta_down while it falls). The PID's on/off ripple in a hold moves
        the model T1 at ~0.8 K/s back and forth; the low-pass averages it out. One rate state per module: valid for
        one replay at a time (reset by initial_state)."""
        r = m._tr_rate
        q = (m.BETA if r > 0 else m.BETA_DOWN) * abs(r)
        if m.HEATER_ONLY and pwm <= 0:
            q = 0.0
        T1n, T2n, T3n = m._step0(T1, T2, T3, pwm, fan, extrude_v, dt, k_loss, max_substep, k_fil, q)
        if dt > 0:
            m._tr_rate += ((T1n - T1) / dt - r) * (1.0 - math.exp(-dt / m.TR_TAU))
        return T1n, T2n, T3n
    return model_step


def make_step0(m):
    """The current model_step with an extra constant block loss q (W) over the step."""
    def step0(T1, T2, T3, pwm, fan, extrude_v, dt, k_loss, max_substep, k_fil, q):
        if max_substep is None:
            max_substep = m.max_stable_substep(fan, extrude_v)

        def deriv(T1, T2, T3):
            Q_conv1 = k_loss * m.natural_loss(T1) + k_fil * m.K_FIL * max(extrude_v, 0.0) * (T1 - m.T_AMB) + q
            Q_heater_to_1 = (T3 - T1) / m.R31
            Q_hb = (T1 - T2) / m.R_HB
            dT1 = (Q_heater_to_1 - Q_hb - Q_conv1) / (m.C1 * m.cap_scale(T1))
            dT2 = (Q_hb - m.HA2 * (T2 - m.T_AMB) - k_loss * m.fan_conductance(fan) * (T2 - m.T_AMB)) / (
                m.C2 * m.cap_scale(T2))
            dT3 = (pwm * m.P_MAX - Q_heater_to_1) / (m.C3 * m.cap_scale(T3))
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
    return step0


TR_TAU = float(os.environ.get("TR_TAU", "0"))   # s - 0: instantaneous implicit rate; > 0: low-passed rate


def install(m):
    """Puts the candidate into the module; BETA / BETA_DOWN / TR_TAU can then be set as evaluate.py overrides."""
    m.BETA, m.BETA_DOWN, m.HEATER_ONLY, m.TR_TAU, m._tr_rate = BETA, BETA_DOWN, HEATER_ONLY, TR_TAU, 0.0
    m._step0 = make_step0(m)
    m.model_step = make_step(m)
    init = m.initial_state

    def initial_state(T1, pwm, fan):
        m._tr_rate = 0.0
        return init(T1, pwm, fan)
    m.initial_state = initial_state


# evaluate.load_model reloads realtime_websocket (also from fresh copies run by runpy): reinstall after each reload
_reload = importlib.reload


def reload(mod):
    mod = _reload(mod)
    if mod.__name__ == "realtime_websocket":
        install(mod)
    return mod


importlib.reload = reload
install(rw)

if __name__ == "__main__":
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_path(os.path.join(DT, script), run_name="__main__")
