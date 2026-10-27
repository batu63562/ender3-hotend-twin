"""Heat-into-the-print test tower: generate the G-code, and afterwards measure the heat each section put into the print.

The calibration print (calibration_print.py) measures the hotend without filament. What goes into the print while
printing is learned online as k_fil, from a fixed anchor at every print start (K27), because it depends on the print
(speed, fan, geometry) more than on the material. This tower prints one simple object whose sections each change ONE
setting around a centre point (volumetric flow, part fan, layer height), every section long enough for the energy
balance to settle, so the heat into the print can be related to the slicer settings (ROADMAP v0.4, K73).

  generate  A hollow square tower (WALLS concentric perimeters, constant speed within a section, no infill), the
            sections from the centre point with one setting changed at a time, the centre repeated to see drift.
            Before each analysed section a tare: the nozzle waits beside the tower top at the section's fan, without
            extruding. Line width and filament diameter come from printer.cfg. The G-code needs no slicer; it writes
            <out>_sections.csv (Z range and settings of each section) next to it.
  analyze   A twin log of the print + that sections file: per section the heat into the print = heater power while
            printing - heater power in the tare just before (W), the filament heat the model expects
            (K_FIL x flow x (T1 - T_amb), W) and their ratio = the section's k_fil. The tare cancels the loss: the
            result does not depend on k, which a model-based balance needs to ~0.01 (K77).
            --synthetic runs the twin's own plan simulation (gcode_plan.Sim, k_fil known) instead of a log: the
            analysis must return that k_fil. This checks the tool, not the printer.

Usage (in dt/):   py yazici/heat_tower.py generate [--temp 245] [--bed 85] [--center 100,100] [--out FILE]
                  py yazici/heat_tower.py analyze LOG --sections FILE_sections.csv
                  py yazici/heat_tower.py analyze --synthetic GCODE --sections FILE_sections.csv
"""
import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

CENTRE = (4.0, 60, 0.20)     # volumetric flow (mm^3/s), part fan (%), layer height (mm)
FLOWS = (2.0, 6.0)           # this printer melts ~6 mm^3/s cleanly at 245 C; ~8 is past its limit (K62)
FANS = (0, 30, 100)          # threshold ~19 % (K03); fan 0 = filament heating only (k_fil fixed at 1.0 in the twin)
HEIGHTS = (0.15, 0.30)
SETTLE = 40.0                # s at the start of each section left out (fan steps: the nozzle side stores/gives heat)
TAIL = 10.0                  # s at the end left out: the twin's net feed is a 10 s average (EXT_AVG_WINDOW)
TARE = 80.0                  # s - tare before each analysed section; the last TARE - SETTLE s of it are averaged
TARE_LIFT = 1.1              # mm above the tower top (not a multiple of any layer height: no print row at this Z)
TARE_OFFSET = 3.0            # mm outside the first corner, so ooze falls beside the tower
RETRACT = 2.0                # mm of filament pulled back during the tare (1 mm let PETG ooze over 80 s)
PRIME = 1.0                  # mm of filament pushed out in the air after the tare: the oozed blob drops beside the tower
WIPE = 10.0                  # mm fast move away from the tower and back after the prime, to break the string
WIDTH_PER_NOZZLE = 1.125     # line width / nozzle diameter (0.4 -> 0.45, 0.6 -> 0.68)


def cfg_diameters(cfg):
    """(nozzle, filament) diameter in mm from the [extruder] section of a Klipper printer.cfg."""
    import gcode_plan as gp
    ex = gp.read_cfg(cfg)[0].get("extruder", {})
    return float(ex.get("nozzle_diameter", 0.4)), float(ex.get("filament_diameter", 1.75))


def sections(section_s):
    """A lead-in at the centre settings (not analysed: the fan switches on for the first time and the nozzle side
    gives back the heat it stored), the centre, then one setting changed at a time, the centre repeated after each
    group (drift check)."""
    f0, fan0, h0 = CENTRE
    out = [("lead-in", f0, fan0, h0), ("centre", f0, fan0, h0)]
    out += [(f"flow {f:g}", f, fan0, h0) for f in FLOWS] + [("centre", f0, fan0, h0)]
    out += [(f"fan {p}", f0, p, h0) for p in FANS] + [("centre", f0, fan0, h0)]
    out += [(f"layer {h:g}", f0, fan0, h) for h in HEIGHTS] + [("centre", f0, fan0, h0)]
    return [dict(name=n, flow=f, fan=p, h=h, seconds=section_s) for n, f, p, h in out]


def generate(a):
    cx, cy = (float(v) for v in a.center.split(","))
    nozzle, filament = cfg_diameters(a.cfg)
    nozzle = a.nozzle or nozzle
    a.filament = a.filament or filament
    area = math.pi * a.filament ** 2 / 4
    w = a.width or round(WIDTH_PER_NOZZLE * nozzle, 2)
    print(f"nozzle {nozzle:g} mm, line width {w:g} mm, filament {a.filament:g} mm")
    perim = [4 * (a.size - 2 * i * w) for i in range(a.walls)]        # mm per layer, concentric squares
    layer_len = sum(perim)
    out = a.out or os.path.join(HERE, f"heat_tower_{a.temp}.gcode")
    g, rows, z, e, t = [], [], 0.0, 0.0, 0.0

    def square(half, speed_mm_s):
        nonlocal e, t
        pts = [(cx - half, cy - half), (cx + half, cy - half), (cx + half, cy + half), (cx - half, cy + half),
               (cx - half, cy - half)]
        g.append(f"G0 X{pts[0][0]:.3f} Y{pts[0][1]:.3f} F6000")
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            seg = math.hypot(x1 - x0, y1 - y0)
            e += seg * w * h_now / area
            g.append(f"G1 X{x1:.3f} Y{y1:.3f} E{e:.5f} F{speed_mm_s * 60:.0f}")
            t += seg / speed_mm_s

    g += [f"; Heat-into-the-print test tower, generated by yazici/heat_tower.py: {a.size:g} mm square, {a.walls} walls of"
          f" {w:g} mm (nozzle {nozzle:g} mm), {a.temp} C, bed {a.bed} C, filament {a.filament:g} mm.",
          "; Each section changes one setting around the centre (flow 4 mm3/s, fan 60 %, layer 0.2 mm) and starts with",
          f"; a {TARE:.0f} s tare beside the tower top at its fan. Sections and their Z ranges are in the _sections.csv",
          "; next to this file. ;Z: and ;TYPE: comments follow Orca's style.",
          f"; filament_type = {a.material}",
          "SET_IDLE_TIMEOUT TIMEOUT=7200", "M107"]
    if a.start:
        g.append(a.start)
    else:
        g += [f"M140 S{a.bed}", "M104 S150", "G28", "G90", f"M190 S{a.bed}"]
    g += ["G90", "M82", f"M109 S{a.temp}"]
    # prime line beside the tower (not with --start: the start macro primes), then two base layers (fan off, slow)
    # that are not analysed
    g += ["G92 E0"]
    if not a.start:
        g += [f"G1 X{cx - a.size:.1f} Y{cy - a.size / 2 - 10:.1f} Z0.3 F6000",
              f"G1 X{cx + a.size:.1f} E{2 * a.size * w * 0.3 / area:.4f} F1200", "G92 E0"]
    e = 0.0
    for i in range(2):
        z = round(z + 0.2, 3)
        h_now = 0.2
        g += [f";Z:{z:g}", ";TYPE:Outer wall", f"G1 Z{z:.3f} F600"]
        if i == 0:      # brim on the first layer, inside out like Orca's (the walls alone hold the bed poorly)
            for k in range(1, a.brim + 1):
                square(a.size / 2 + k * w, 20.0)
        for k in range(a.walls):
            square(a.size / 2 - k * w, 20.0)
    z_rows = []
    for s in sections(a.section):
        h_now = s["h"]
        speed = s["flow"] / (w * h_now)
        layer_t = layer_len / speed
        n_layers = max(2, math.ceil(s["seconds"] / layer_t))
        z0 = z
        g += [f"; --- section {s['name']}: flow {s['flow']:g} mm3/s, fan {s['fan']} %, layer {h_now:g} mm, "
              f"{speed:.0f} mm/s, {n_layers} layers of {layer_t:.0f} s ---",
              f"M117 {s['name']}"[:28], f"M106 S{round(s['fan'] * 2.55)}" if s["fan"] > 0 else "M107"]
        if s["name"] != "lead-in":
            # tare: same fan, same temperature, no extrusion; the print's heat is the step from here to printing
            half = a.size / 2
            e -= RETRACT
            g += [f"G1 E{e:.5f} F1800", f"G1 Z{z + TARE_LIFT:.3f} F600",
                  f"G0 X{cx - half - TARE_OFFSET:.3f} Y{cy - half - TARE_OFFSET:.3f} F6000",
                  f"G4 P{TARE * 1000:.0f}          ; tare"]
            # prime in the air beside the tower and wipe, then back to the corner (slow feed: stays below the
            # analysis' flow threshold, so the section still starts at its first printed line)
            e += RETRACT + PRIME
            g += [f"G1 E{e:.5f} F120          ; prime",
                  f"G0 X{cx - half - TARE_OFFSET - WIPE:.3f} F9000", f"G0 X{cx - half - TARE_OFFSET:.3f} F9000",
                  f"G0 X{cx - half:.3f} Y{cy - half:.3f} F6000", f"G1 Z{z + h_now:.3f} F600"]
            t += TARE + 3 + (RETRACT + PRIME) / 2
        t_start = t
        for _ in range(n_layers):
            z = round(z + h_now, 3)
            g += [f";Z:{z:g}", ";TYPE:Outer wall", f"G1 Z{z:.3f} F600"]
            for k in range(a.walls):
                square(a.size / 2 - k * w, speed)
        z_rows.append(dict(name=s["name"], z_from=round(z0 + 1e-3, 3), z_to=z, flow=s["flow"], fan=s["fan"], h=h_now,
                           speed=round(speed, 1), layers=n_layers, seconds=round(t - t_start)))
    g += ["M107", "G91", "G1 Z10 F600", "G90", "TURN_OFF_HEATERS", "G1 X0 Y220 F3000", "M84", "M117 tower done"]
    g.insert(4, f"; Estimated duration ~{(t + 180) / 60:.0f} min, height {z:g} mm, filament ~{total_e(g, area):.1f} g")
    with open(out, "w", newline="\n") as f:
        f.write("\n".join(g) + "\n")
    sec_path = os.path.splitext(out)[0] + "_sections.csv"
    pd.DataFrame(z_rows).to_csv(sec_path, index=False)
    print(f"{os.path.relpath(out)}: {len(z_rows)} sections, height {z:g} mm, ~{(t + 180) / 60:.0f} min")
    print(f"{os.path.relpath(sec_path)}")


def total_e(lines, area, density=1.27):
    """Filament mass (g) from the absolute E values (each G92 E0 starts a new run)."""
    tot, last = 0.0, 0.0
    for ln in lines:
        if ln.startswith("G92 E0"):
            tot += last
            last = 0.0
        elif ln.startswith("G1") and " E" in ln:
            last = float(ln.split(" E")[1].split()[0])
    tot += last
    return tot * area * 1e-3 * density


def analyse(df, sec, m, k):
    """df: t (s), T1, target, pwm, bed_pwm, fan (0-1), flow (mm/s of filament, net), z. One row per section."""
    out = []
    area = math.pi * m.FILAMENT_DIAMETER ** 2 / 4
    for s in sec.itertuples():
        if s.name == "lead-in":
            continue
        # rows of this section's layers while it is extruding at about its own rate (not the travel after the print)
        # z_from is the previous section's top + 0.001: no tolerance below it, or that top layer counts here too.
        # ext_now > 0: the tare is lifted into this section's Z range and the 10 s net feed is still high there
        d = df[(df.z >= s.z_from - 5e-4) & (df.z <= s.z_to + 1e-3) & (df.flow >= 0.5 * s.flow / area)
               & (df.ext_now > 0) & (df.target > 0)]
        if d.empty:
            out.append(dict(name=s.name, n=0))
            continue
        t_first = d.t.iloc[0]
        # tare: the end of the wait before this section, after SETTLE s of it, while nothing is fed
        tare = df[(df.t >= t_first - TARE + SETTLE) & (df.t <= t_first - 3) & (df.flow < 1e-3) & (df.ext_now == 0)
                  & (df.target > 0)]
        d = d[(d.t >= t_first + SETTLE) & (d.t <= d.t.iloc[-1] - TAIL)]
        if d.empty:                                 # section cut short (print stopped)
            out.append(dict(name=s.name, n=0))
            continue

        def balance(r):
            """Mean heater power (W) and model loss scaled by k (W) of rows r."""
            T1, fan = r.T1.values, r.fan.values
            dT2 = np.array([m.steady_T2_rise(T, f) for T, f in zip(T1, fan)])
            loss = k * (m.natural_loss(T1) + np.array([m.fan_conductance(f) for f in fan]) * dT2) + m.HA2 * dT2
            return float(np.mean(r.pwm.values * m.P_MAX * (1 - m.BED_SAG * r.bed_pwm.values))), float(np.mean(loss))

        P_in, loss = balance(d)
        P_fil = float(np.mean(m.K_FIL * m.FLOW_RATIO * d.flow.values * (d.T1.values - m.T_AMB)))
        P_model = P_in - loss                       # model-based: needs k to ~0.01 (K77)
        row = dict(name=s.name, flow=s.flow, fan=s.fan, h=s.h, n=len(d), s=round(d.t.iloc[-1] - t_first - SETTLE),
                   T1=round(float(d.T1.mean()), 1), P_in_W=round(P_in, 2))
        if len(tare) >= 20:
            P_tare, loss_tare = balance(tare)
            # the small temperature difference between tare and print is corrected with the model loss
            P_print = P_in - P_tare - (loss - loss_tare)
            row.update(tare_n=len(tare), P_tare_W=round(P_tare, 2), heat_into_print_W=round(P_print, 2))
        else:
            P_print = float("nan")
            row.update(tare_n=len(tare), P_tare_W=float("nan"), heat_into_print_W=float("nan"))
        row.update(filament_heat_W=round(P_fil, 2),
                   k_fil=round(P_print / P_fil, 3) if P_fil > 0.05 else float("nan"),
                   k_fil_model=round(P_model / P_fil, 3) if P_fil > 0.05 else float("nan"))
        out.append(row)
    return pd.DataFrame(out)


def frame_from_log(path, m):
    import evaluate as ev
    df = ev.read_log(path)
    t = ev.mono_time(df.t.values)
    num = lambda c, d=0.0: pd.to_numeric(df[c], errors="coerce").ffill().fillna(d).values if c in df else np.full(len(df), d)
    ext = pd.Series(num("extrude_velocity"), index=pd.to_datetime(t, unit="s"))
    flow = np.maximum(ext.rolling(f"{m.EXT_AVG_WINDOW:.0f}s").mean().values, 0.0)   # the twin's net feed
    return pd.DataFrame(dict(t=t, T1=num("T1_real", np.nan), target=num("target"), pwm=num("pwm"), bed_pwm=num("bed_pwm"),
                             fan=num("fan_state"), flow=flow, ext_now=num("extrude_velocity"),
                             z=num("pos_z", np.nan))).dropna()


def frame_from_sim(gcode, cfg, kfil):
    import evaluate as ev
    import gcode_plan as gp
    mc, m = gp.Machine(cfg), ev.load_model()
    sim = gp.Sim(mc, m, 1.0, kfil=kfil)
    gp.run(gp.parse(gcode, mc), sim)
    f = sim.frame()
    return pd.DataFrame(dict(t=f.t, T1=f.T1, target=f.target, pwm=f.pwm, bed_pwm=f.bed_pwm, fan=f.fan,
                             flow=f.akis / mc.fil_area, ext_now=f.akis_anlik / mc.fil_area, z=f.katman_z)), m


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--temp", type=int, default=245, help="print temperature (C)")
    gen.add_argument("--bed", type=int, default=85, help="bed temperature (C)")
    gen.add_argument("--center", default="100,100", help="tower centre X,Y (bed centre of the Orca profile, 200 x 200)")
    gen.add_argument("--size", type=float, default=40.0, help="outer square side (mm)")
    gen.add_argument("--walls", type=int, default=4, help="concentric perimeters per layer")
    gen.add_argument("--brim", type=int, default=10, help="brim loops on the first layer (0 = none)")
    gen.add_argument("--cfg", default=os.path.join(HERE, "printer.cfg"),
                     help="your printer.cfg: nozzle and filament diameter are read from its [extruder]")
    gen.add_argument("--nozzle", type=float, help="nozzle diameter (mm) instead of printer.cfg's")
    gen.add_argument("--width", type=float, help=f"line width (mm); default {WIDTH_PER_NOZZLE:g} x nozzle diameter")
    gen.add_argument("--filament", type=float, help="filament diameter (mm) instead of printer.cfg's")
    gen.add_argument("--material", default="PETG", help="written as '; filament_type' (the twin logs it)")
    gen.add_argument("--section", type=float, default=180.0, help="minimum length of a section (s); ~130 s of it is "
                     "analysed, the print-window noise (R_PRINT, ~0.4 W over 300 s) then stays well below the 1-4 W measured")
    gen.add_argument("--start", help="your start macro instead of the built-in start (e.g. 'START_GCODE BED_TEMP=80 "
                                     "EXTRUDER_TEMP=245'); it must leave the nozzle at temperature and homed")
    gen.add_argument("--out", help="output file (default yazici/heat_tower_<temp>.gcode)")
    an = sub.add_parser("analyze")
    an.add_argument("log", nargs="?", help="twin log of the tower print")
    an.add_argument("--sections", required=True, help="the _sections.csv written with the G-code")
    an.add_argument("--k", type=float, help="loss factor of the model-based column k_fil_model and the small tare "
                                             "temperature correction (default: the log's k at the start of the first section)")
    an.add_argument("--synthetic", metavar="GCODE", help="analyse the plan simulation of this G-code instead of a log")
    an.add_argument("--cfg", default=os.path.join(HERE, "printer.cfg"), help="printer.cfg for --synthetic")
    an.add_argument("--kfil", type=float, default=1.33, help="k_fil of the --synthetic run")
    a = ap.parse_args()
    if a.cmd == "generate":
        return generate(a)
    sec = pd.read_csv(a.sections)
    if a.synthetic:
        df, m = frame_from_sim(a.synthetic, a.cfg, a.kfil)
        k = 1.0 if a.k is None else a.k
        print(f"synthetic: plan simulation with k 1.0, k_fil {a.kfil} (the analysis must return it)")
    else:
        import evaluate as ev
        m = ev.load_model()
        df = frame_from_log(a.log, m)
        if a.k is not None:
            k = a.k
        else:
            log = ev.read_log(a.log)
            kl = pd.to_numeric(log.k_loss, errors="coerce")
            zs = pd.to_numeric(log.pos_z, errors="coerce")
            k = float(kl[zs < sec.z_from.min()].dropna().iloc[-1])
        print(f"log: {os.path.basename(a.log)}, k {k:.3f}")
    r = analyse(df, sec, m, k)
    with pd.option_context("display.width", 160, "display.max_columns", 20):
        print(r.to_string(index=False))


if __name__ == "__main__":
    main()
