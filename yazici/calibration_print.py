"""Generate the hotend calibration G-code (v0.3: "calibration print = system identification").

One ~1 h job that collects everything the model identification needs, without extrusion, the bed off and the
nozzle 50 mm above the bed centre. It generalizes the experiments that showed what the data must contain
(KARARLAR K36, K39, K40): deney9 (steps up and down, holds), deney14a/b (heat-ups at two rates, free cooling) and
deney15 (part-fan steps at the levels used in prints).

  1  cold start           wait below 40 C (fan helps), then fan off
  2  full-power ramp      room -> TEMP at the heater limit (rate lever 1)
  3  hold TEMP            loss at the print temperature
  4  free cooling         TEMP -> MID with the heater off (heat capacity without the heater)
  5  slow ramp            MID -> TEMP by raising the target 1 C at a time at SLOW_RATE C/s; the PID follows below its
                          limit, so the same temperatures are crossed at a different rate and power (rate lever 2:
                          replaces the max_power change of deney14b, no printer.cfg edit needed)
  6  fan steps at TEMP    part fan at FAN_LEVELS, then back to 0 (fan curve: threshold, exponent, amplitude)
  7  small steps          TEMP - 10 -> TEMP (the PID does not saturate: heater-to-thermistor lag)
  8  holds going down     at each of HOLDS (loss shape over temperature); at the first one also fan 0/40/100
  9  full-power steps up  from the lowest hold back to TEMP through the other holds: the cartridge far above the
                          block at several temperatures (separates cartridge from block loss, K40)
 10  free cooling         to 60 C with the heater off (the long tail towards room temperature)

The twin records it like a print (its own log) and learns from it: after a hardware change that is the point; on
unchanged hardware back up its state (twin_state.json) before and restore it afterwards (K79: not marked).
Position, park point and temperature limit come from printer.cfg ([stepper_x/y/z], [extruder] max_temp).

Usage (in dt/):   py yazici/calibration_print.py [--cfg printer.cfg] [--temp 245] [--mid 150] [--holds 215,185,150]
                  [--fan-levels 15,25,40,60,100] [--slow-rate 1.0] [--hold 150] [--center X,Y] [--out FILE]
"""
import argparse
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
LIFT = 50.0          # mm above the bed during the job: no contact, the bed's warm air does not reach the nozzle
PARK_BACK = 15.0     # mm in front of the Y end for the final park (Ender: X0 Y220; at the end the frame is hit, K32)
TEMP_MARGIN = 5      # C below max_temp: the PID overshoots ~1 C (255 C print: 256.1 C) and Klipper shuts down above it
HEAT_RATE = 2.5      # C/s - rough full-power heat-up rate near print temperature (Ender 3, 40 W), for the estimate
COOL_TIME = {245: 0, 215: 25, 185: 60, 150: 110, 100: 210, 60: 420}   # s from 245 C, free cooling (deney14 A2)


def cool_s(t_from, t_to):
    """Rough free-cooling time between two temperatures (interpolated from COOL_TIME)."""
    pts = sorted(COOL_TIME.items(), reverse=True)

    def t_at(T):
        for (T1, s1), (T2, s2) in zip(pts, pts[1:]):
            if T2 <= T <= T1:
                return s1 + (s2 - s1) * (T1 - T) / (T1 - T2)
        return pts[-1][1] if T < pts[-1][0] else 0.0
    return max(0.0, t_at(t_to) - t_at(t_from))


def cfg_limits(cfg):
    """(x_min, x_max, y_min, y_max, z_max, extruder max_temp) from a Klipper printer.cfg."""
    import gcode_plan as gp
    sec = gp.read_cfg(cfg)[0]

    def axis(name):
        st = sec.get(f"stepper_{name}", {})
        return float(st.get("position_min", 0.0)), float(st["position_max"])
    (x0, x1), (y0, y1), (_, z1) = axis("x"), axis("y"), axis("z")
    return x0, x1, y0, y1, z1, float(sec.get("extruder", {})["max_temp"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--temp", type=int, default=245, help="print temperature (C)")
    ap.add_argument("--mid", type=int, default=150, help="free cooling down to / slow ramp from (C)")
    ap.add_argument("--holds", default="215,185,150", help="hold temperatures going down (C)")
    ap.add_argument("--fan-levels", default="15,25,40,60,100", help="part-fan steps at --temp (%%)")
    ap.add_argument("--slow-rate", type=float, default=1.0, help="slow ramp rate (C/s)")
    ap.add_argument("--hold", type=int, default=150, help="length of each hold (s)")
    ap.add_argument("--cfg", default=os.path.join(HERE, "printer.cfg"), help="the printer's printer.cfg")
    ap.add_argument("--center", help="nozzle X,Y during the job (default: middle of the X/Y travel, whole mm)")
    ap.add_argument("--out", help="output file (default yazici/calibration_<temp>.gcode)")
    a = ap.parse_args()

    T, MID, H = a.temp, a.mid, a.hold
    holds = [int(x) for x in a.holds.split(",") if x.strip()]
    fans = [int(x) for x in a.fan_levels.split(",") if x.strip()]
    x0, x1, y0, y1, z1, max_temp = cfg_limits(a.cfg)
    if T > max_temp - TEMP_MARGIN:
        sys.exit(f"--temp {T} is too close to max_temp {max_temp:g} in {a.cfg} (at most {max_temp - TEMP_MARGIN:g})")
    if z1 < LIFT + 10:
        sys.exit(f"Z travel {z1:g} mm is too short for the {LIFT:g} mm lift")
    if a.center:
        cx, cy = (float(v) for v in a.center.split(","))
    else:
        cx, cy = math.floor((x0 + x1) / 2), math.floor((y0 + y1) / 2)
    if not (x0 <= cx <= x1 and y0 <= cy <= y1):
        sys.exit(f"--center {cx:g},{cy:g} is outside the travel X {x0:g}..{x1:g} Y {y0:g}..{y1:g}")
    park = f"G1 X{x0:g} Y{max(y0, y1 - PARK_BACK):g} F3000"
    out = a.out or os.path.join(HERE, f"calibration_{T}.gcode")
    g, est = [], 0.0

    def fan(pct):
        g.append(f"M106 S{round(pct * 2.55)}" if pct > 0 else "M106 S0")

    def label(text):
        g.append(f"M117 cal {text}"[:28])

    def hold(s, note=""):
        nonlocal est
        g.append(f"G4 P{s * 1000:d}" + (f"          ; {note}" if note else ""))
        est += s

    g += [f"; Hotend calibration print, generated by yazici/calibration_print.py (temp {T}, mid {MID}, holds {holds},",
          f"; fan {fans} %, slow ramp {a.slow_rate} C/s, holds {H} s). No extrusion, bed off, nozzle {LIFT:g} mm above the",
          "; bed centre. The twin learns from it (not experiment-marked, K79): on unchanged hardware back up and restore",
          "; its twin_state.json.",
          "SET_IDLE_TIMEOUT TIMEOUT=7200", "M140 S0", "M104 S0", "G28", "G90", f"G1 Z{LIFT:g} F600",
          f"G1 X{cx:g} Y{cy:g} F6000"]
    est += 60

    g.append("; --- 1 cold start ---")
    label("cold start")
    fan(100)
    g.append("TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=40")
    fan(0)
    hold(60, "calm after the fan")

    g.append("; --- 2 full-power ramp + 3 hold ---")
    label(f"ramp {T} full power")
    g.append(f"M109 S{T}")
    est += (T - 30) / HEAT_RATE
    hold(H + 30, f"hold {T}, fan 0")

    g.append("; --- 4 free cooling ---")
    label(f"free cooling to {MID}")
    g.append("M104 S0")
    g.append(f"TEMPERATURE_WAIT SENSOR=extruder MAXIMUM={MID}")
    est += cool_s(T, MID)

    g.append(f"; --- 5 slow ramp {MID} -> {T} at {a.slow_rate} C/s (target raised 1 C at a time) ---")
    label(f"slow ramp {a.slow_rate}C/s")
    step_ms = round(1000 / a.slow_rate)
    for t in range(MID + 1, T + 1):
        g.append(f"M104 S{t}")
        g.append(f"G4 P{step_ms}")
    est += (T - MID) / a.slow_rate
    g.append(f"M109 S{T}")
    hold(H, f"hold {T} after the slow ramp")

    g.append("; --- 6 fan steps ---")
    for p in fans:
        label(f"{T} fan {p}")
        fan(p)
        hold(H, f"fan {p} %")
    label(f"{T} fan 0 again")
    fan(0)
    hold(H, "fan 0 again (repeatability)")

    g.append("; --- 7 small steps (PID below its limit) ---")
    label(f"step {T - 10}")
    g.append(f"M109 S{T - 10}")
    est += cool_s(T, T - 10)
    hold(90)
    label(f"step {T}")
    g.append(f"M109 S{T}")
    est += 10
    hold(90)

    g.append("; --- 8 holds going down ---")
    prev = T
    for i, th in enumerate(holds):
        label(f"hold {th}")
        g.append(f"M109 S{th}")
        est += cool_s(prev, th)
        hold(H, f"hold {th}, fan 0")
        if i == 0:
            for p in (40, 100):
                label(f"{th} fan {p}")
                fan(p)
                hold(H, f"hold {th}, fan {p} %")
            fan(0)
            hold(60, "fan off, settle")
        prev = th

    g.append("; --- 9 full-power steps up from the lowest hold (the cartridge far above the block at several "
             "temperatures: separates heat lost from the cartridge from heat lost from the block, K40) ---")
    for th in sorted(set(holds + [T])):
        if th <= prev:
            continue
        label(f"step up {th}")
        g.append(f"M109 S{th}")
        est += (th - prev) / HEAT_RATE
        hold(90, f"hold {th} after a full-power step")
        prev = th

    g.append("; --- 10 free cooling to 60 C ---")
    label("free cooling")
    g += ["TURN_OFF_HEATERS", "M106 S0", "TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=60"]
    est += cool_s(prev, 60)
    g += [park, "M84", "M117 cal done"]
    g.insert(4, f"; Estimated duration ~{est / 60:.0f} min.")

    with open(out, "w", newline="\n") as f:
        f.write("\n".join(g) + "\n")
    print(f"{os.path.relpath(out)}: {len(g)} lines, ~{est / 60:.0f} min")


if __name__ == "__main__":
    main()
