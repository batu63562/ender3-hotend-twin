"""Run a heater-only G-code on a virtual printer: Klipper's PID (closed_loop.KlipperPID, gains from the
SAVE_CONFIG block of yazici/printer.cfg) drives the twin's hotend model, and the result is written as a twin log
(time, eventtime, T1_real, pwm, fan_state, target, ...). Used to check a calibration G-code before printing it:
its duration, and with `calibrate.py LOG --synthetic` whether the data it produces can tell the model parameters
apart.

Understood: M104/M109 S, M106 S/M107, TEMPERATURE_WAIT SENSOR=extruder MINIMUM/MAXIMUM, G4 P/S, TURN_OFF_HEATERS.
Everything else (homing, moves, M117 ...) takes no time here. No extrusion, bed off.

Usage (in dt/):   py yazici/virtual_print.py GCODE [--out LOG.csv] [--room 23]
"""
import argparse
import os
import re
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import closed_loop as cl  # noqa: E402
import realtime_websocket as rw  # noqa: E402

WAIT_MAX = 1800.0   # s - a wait that never ends is cut here (and reported)


def pid_gains(cfg):
    """pid_kp/ki/kd of [extruder] from the SAVE_CONFIG block (falls back to closed_loop's values)."""
    text = open(cfg, encoding="utf-8").read()
    m = re.search(r"#\*# \[extruder\](.*?)(?:#\*# \[|\Z)", text, re.S)
    if not m:
        return cl.PID_KP, cl.PID_KI, cl.PID_KD
    vals = dict(re.findall(r"#\*# pid_(k[pid]) = ([0-9.]+)", m.group(1)))
    return float(vals["kp"]), float(vals["ki"]), float(vals["kd"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("gcode")
    ap.add_argument("--out", help="log to write (default: <gcode>_virtual.csv next to it)")
    ap.add_argument("--room", type=float, default=float(rw.T_AMB), help="starting nozzle temperature (C)")
    ap.add_argument("--cfg", default=os.path.join(HERE, "printer.cfg"))
    a = ap.parse_args()

    gains = pid_gains(a.cfg)
    pid = cl.KlipperPID(a.room, 0.0, 0.0, gains=gains)
    dt = cl.REPORT_TIME
    T1 = T2 = T3 = a.room
    t, target, fan = 0.0, 0.0, 0.0
    rows, cut = [], []

    def step():
        nonlocal T1, T2, T3, t
        pwm = pid.update(t, T1, target)
        rows.append((t, round(T1, 2), pwm, fan, target))
        T1, T2, T3 = rw.model_step(T1, T2, T3, pwm, fan, 0.0, dt, 1.0)
        t += dt

    def run_until(cond, what):
        t0 = t
        while not cond():
            step()
            if t - t0 > WAIT_MAX:
                cut.append(f"{what} at t={t0:.0f} s")
                break

    for raw in open(a.gcode, encoding="utf-8"):
        line = raw.split(";")[0].strip()
        if not line:
            continue
        w = line.split()
        cmd = w[0].upper()
        arg = {p[0].upper(): p[1:] for p in w[1:] if len(p) > 1}
        if cmd in ("M104", "M109"):
            target = float(arg.get("S", 0))
            if cmd == "M109" and target > 0:
                run_until(lambda: not pid.busy(T1, target), f"M109 S{target:g}")
        elif cmd == "M106":
            fan = min(1.0, float(arg.get("S", 255)) / 255.0)
        elif cmd == "M107":
            fan = 0.0
        elif cmd == "TURN_OFF_HEATERS":
            target = 0.0
        elif cmd == "TEMPERATURE_WAIT":
            kv = dict(p.split("=", 1) for p in w[1:] if "=" in p)
            if "MAXIMUM" in kv:
                run_until(lambda: T1 <= float(kv["MAXIMUM"]), line)
            if "MINIMUM" in kv:
                run_until(lambda: T1 >= float(kv["MINIMUM"]), line)
        elif cmd == "G4":
            dur = float(arg["P"]) / 1000 if "P" in arg else float(arg.get("S", 0))
            end = t + dur
            run_until(lambda: t >= end, line)

    d = pd.DataFrame(rows, columns=["eventtime", "T1_real", "pwm", "fan_state", "target"])
    d["time"] = 1.8e9 + d.eventtime
    d["T_yeni"], d["extrude_velocity"], d["bed_pwm"], d["bed_target"] = 1, 0.0, 0.0, 0.0
    out = a.out or os.path.splitext(a.gcode)[0] + "_virtual.csv"
    d.to_csv(out, index=False)
    print(f"{os.path.relpath(out)}: {t / 60:.1f} min, {len(d)} rows, PID {gains}, max power used "
          f"{d.pwm.max():.2f}" + (f"; CUT waits: {cut}" if cut else ""))


if __name__ == "__main__":
    main()
