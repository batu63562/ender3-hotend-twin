"""Induced-fault experiment G-codes (v0.2 "induced faults on purpose"). Data collection only: touches no model code.

Writes into yazici/:
    deney10_mandal_acik.gcode     B: extruder latch opened mid-print (filament stops, stepper keeps turning)
    deney11_fan_kapali.gcode      A: part-fan (5015 blower) intake covered, at fan 25 % and at fan 60 %
    deney12_cereyan.gcode         C: desk fan blowing on the hotend (draft, more heat loss)
    deney13_cikinti_lale.gcode    yellow: "tulip" whose overhang angle grows with Z (0 -> 70 deg)

Fault windows are framed by PAUSE / RESUME: the head parks, the LCD (M117) says what to do, the user applies or
removes the fault and presses Resume. The paused -> printing transition in the twin's CSV (print_state) is then
the exact label boundary, no stopwatch needed. The twin treats a resume as the same print (k_fil is not
re-anchored, the log is not rotated). The window after the fault is removed has the same pause/resume procedure
without a fault, so it doubles as the control.

Detectability depends on flow: the filament term is K_FIL * flow * (T - T_amb) ~ 1.19 W per mm/s of filament at
245 C (x1.33 with the fan on). A full flow stop crosses the 2.5 W threshold only above ~1.6 mm/s of filament
(~3.8 mm3/s), so the carrier prints at ~7 mm3/s (0.6 nozzle, 0.65 x 0.3 mm lines, 40 mm/s).

Usage (in dt/):   py yazici/ariza_deneyleri.py
"""
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))

HOT, BED = 245, 85             # C - the user's PETG profile
W, H = 0.65, 0.30              # mm - line width (0.6 nozzle), layer height
W1 = 0.70                      # mm - first layer line width
FIL_AREA = math.pi / 4 * 1.75 ** 2
V_PRINT, V_FIRST, V_TRAVEL = 40.0, 20.0, 150.0   # mm/s
FAN_LOW, FAN_HIGH = 64, 153    # M106 S: 25 % (the user's PETG profile) and 60 %
CX = CY = 117.5                # bed centre (235 x 235)
SIDE = 70.0                    # mm - carrier tube, centre line of the outer wall
BRIM = 3                       # first-layer brim loops


def e_per_mm(w, h):
    """Filament (mm) per mm of line: Orca's rounded-rectangle cross-section / filament area."""
    return ((w - h) * h + math.pi * (h / 2) ** 2) / FIL_AREA


class GcodeWriter:
    def __init__(self):
        self.lines, self.t, self.z = [], 0.0, 0.0     # t: estimated print time (s), excluding heat-up/pauses
        self.x, self.y = 0.0, 0.0

    def add(self, s=""):
        self.lines.append(s)

    def travel(self, x, y, z=None):
        """Travel without extrusion: Z up before / down after the XY move, a 0.6 mm hop on long moves."""
        z = self.z if z is None else z
        d = math.hypot(x - self.x, y - self.y)
        hop = 0.6 if d > 5.0 else 0.0
        if max(z, self.z) + hop > self.z + 1e-6:
            self.add(f"G1 Z{max(z, self.z) + hop:.2f} F600")
        self.add(f"G1 X{x:.3f} Y{y:.3f} F{V_TRAVEL * 60:.0f}")
        if abs(max(z, self.z) + hop - z) > 1e-6:
            self.add(f"G1 Z{z:.2f} F600")
        self.t += d / V_TRAVEL + 0.1 + (0.2 if hop else 0.0)
        self.x, self.y, self.z = x, y, z

    def extrude_to(self, x, y, e_mm, v):
        d = math.hypot(x - self.x, y - self.y)
        self.add(f"G1 X{x:.3f} Y{y:.3f} E{d * e_mm:.5f} F{v * 60:.0f}")
        self.t += d / v
        self.x, self.y = x, y

    def polygon(self, pts, e_mm, v):
        """Closed loop through pts (starts at pts[0], the caller has travelled there)."""
        for x, y in pts[1:] + pts[:1]:
            self.extrude_to(x, y, e_mm, v)

    def text(self):
        return "\n".join(self.lines) + "\n"


def square(half):
    return [(CX - half, CY - half), (CX + half, CY - half), (CX + half, CY + half), (CX - half, CY + half)]


def start(g, header):
    for ln in header.strip("\n").splitlines():
        g.add("; " + ln if ln else ";")
    g.add("; filament_type = PETG")
    g.add("SET_IDLE_TIMEOUT TIMEOUT=7200   ; pauses wait for the user")
    g.add(f"START_GCODE BED_TEMP={BED} EXTRUDER_TEMP={HOT}")
    g.add("M83                             ; relative extrusion")
    g.add("M107")
    g.x, g.y, g.z = 10.4, 20.0, 1.0      # where START_GCODE leaves the head


def tube_layer(g, z, fan=None, first=False):
    """One layer of the carrier tube: inner wall then outer wall (plus brim on the first layer)."""
    g.add(f";LAYER_CHANGE\n;Z:{z:.2f}")
    if fan is not None:
        g.add(f"M106 S{fan}")
    w, v = (W1, V_FIRST) if first else (W, V_PRINT)
    e = e_per_mm(w, H)
    halves = [SIDE / 2 - w, SIDE / 2] + ([SIDE / 2 + w * (i + 1) for i in range(BRIM)] if first else [])
    g.add(";TYPE:Outer wall")
    for i, half in enumerate(halves):
        pts = square(half)
        if i == 0:
            g.travel(*pts[0], z=z)
        else:
            g.extrude_to(*pts[0], e, v)      # step out to the next loop (one line width)
        g.polygon(pts, e, v)


def layer_time():
    g = GcodeWriter()
    g.x, g.y = square(SIDE / 2 - W)[0]
    tube_layer(g, 1.0)
    return g.t


def print_for(g, seconds, fan=None):
    """Normal tube layers for about `seconds`; returns the number of layers."""
    n = max(1, round(seconds / layer_time()))
    for i in range(n):
        tube_layer(g, g.z_next, fan=fan if i == 0 else None)
        g.z_next = round(g.z_next + H, 2)
    return n


def retrace_for(g, seconds):
    """Fault window of the latch experiment: the NEXT layer is traced again and again at the same Z. With the
    latch open no plastic comes out, so no gap forms in the tube (the nozzle stays 0.3 mm above the last real
    layer, no contact). The extruder stepper still turns: the twin sees the commanded flow, as in a real fault."""
    n = max(1, round(seconds / layer_time()))
    for _ in range(n):
        tube_layer(g, g.z_next)
    return n                       # Z is NOT advanced: the next normal layer is printed at the same z_next


def pause(g, lcd, note, fan_restore=None):
    """fan_restore: part fan stops while paused (taping next to a running blower is hard, 20.10) and is set back
    to this M106 value after Resume."""
    g.add(f"; ===== PAUSE: {note} =====")
    g.add(f"M117 {lcd}")
    if fan_restore is not None:
        g.add("M107")
    # Park where END_PRINT parks (X0 Y220, safe on this printer). Mainsail's default park is max - 5 = X230 Y230;
    # in deney10/11/11b (20.10) every Resume came back shifted to the left -> the carriage likely hit the frame there.
    g.add("PAUSE X=0 Y=220")
    if fan_restore is not None:
        g.add(f"M106 S{fan_restore}")


def finish(g):
    g.add("M117 Done")
    g.add("M107")
    g.add("END_PRINT")


def carrier_start(g):
    """Layer 1-2 without fan (as in the user's profile), then fan 25 %."""
    g.z_next = H
    tube_layer(g, g.z_next, first=True)
    g.z_next = round(g.z_next + H, 2)
    tube_layer(g, g.z_next)
    g.z_next = round(g.z_next + H, 2)


COMMON = f"""
Carrier: square tube {SIDE:.0f} x {SIDE:.0f} mm, 2 walls, {W} x {H} mm lines at {V_PRINT:.0f} mm/s
(~{e_per_mm(W, H) * FIL_AREA * V_PRINT:.1f} mm3/s, filament {e_per_mm(W, H) * V_PRINT:.2f} mm/s), PETG {HOT} C, bed {BED} C,
fan off on layers 1-2. One layer ~{layer_time():.0f} s. Generated by yazici/ariza_deneyleri.py.
The twin runs on the Pi as a service: nothing to start. Watch the phone page (http://192.168.0.25:8090).
At every PAUSE the head parks and the LCD says what to do. Do it, then press Resume in Mainsail.
Write down only anything unusual (e.g. "latch was only half open for the first ~10 s").
"""


def make_latch():
    g = GcodeWriter()
    start(g, f"""
Experiment 10 - extruder latch open mid-print (category B, fast fault)
Question: is a real flow stop in a FAN-ON print caught by GUC_AZALDI, and does the shadow pause fire
(GUC_AZALDI 150 s uninterrupted)? The September runs (deney1/1b) were not prints and were at 1.65 mm/s:
GUC only 29-35 s, never a pause.
Expected: missing power ~ +4.6 W (1.33 x 1.19 W/(mm/s) x 2.9 mm/s) -> GUC_AZALDI ~25 s after Resume,
shadow pause (CSV duraklat) ~3 min after Resume. After the latch is closed: back to +-0.5 W, no alarm.
Physics: the filament that should leave the nozzle carries heat (heating + contact into the print); with
the latch open the stepper still turns (pos_e grows) but nothing moves, so less heat leaves than expected.
{COMMON}
Steps:
 1. Start the print. ~6 min normal printing (k_fil settles from the 1.33 anchor).
 2. PAUSE 1 (LCD "B OPEN latch>Resume"): open the extruder latch fully, press Resume.
    Fault window 4 min: the head traces the same layer again 0.3 mm above the part (no gap forms).
    If plastic still comes out after Resume, the latch is not open: open it now (or Cancel).
 3. PAUSE 2 (LCD "B CLOSE latch>Resume"): close the latch, press Resume. 4 min normal = control.
Safety: none special (latch open = idler not pressing, the gear spins freely, no grinding).
Labels: fault = Resume 1 -> Pause 2 (B); control = Resume 2 -> end (-). Taken from print_state.
""")
    carrier_start(g)
    print_for(g, 360 - 2 * layer_time(), fan=FAN_LOW)
    pause(g, "B OPEN latch>Resume", "open the extruder latch, then Resume")
    g.add("M117 B fault: latch OPEN")
    retrace_for(g, 240)
    pause(g, "B CLOSE latch>Resume", "close the extruder latch, then Resume")
    g.add("M117 B control")
    print_for(g, 240)
    finish(g)
    return g


def make_fan():
    g = GcodeWriter()
    start(g, f"""
Experiment 11 - part-fan intake covered (category A: fan air does not reach the nozzle)
Question: is a dead part fan visible at the user's real fan setting (25 %), and at 60 %?
Expected: fan 25 % -> ~+2.7 W (fan loss ~1.6 W + the fan share of k_fil, 0.33 x 1.19 x 2.9 ~ 1.1 W):
BORDERLINE, maybe only sub-threshold / KAYIP_AZALDI_FAN (the September foil run gave KAYIP only).
Fan 60 % -> ~+5 W (fan loss ~4 W + 1.1 W): GUC_AZALDI and a shadow pause.
Physics: the blower cools the block's lower part (T2) and the fresh print; covered, both stop.
{COMMON}
Steps:
 1. Start the print. ~6 min normal at fan 25 %.
 2. PAUSE 1 (LCD "A1 COVER fan>Resume"): cover the INTAKE of the 5015 part-cooling blower (the round hole
    on its face) with paper + tape, airtight. NEVER the heatsink fan (heat creep jams an all-metal hotend).
    Press Resume. 4 min fault.
 3. PAUSE 2 (LCD "A1 UNCOVER>Resume"): remove the cover, Resume. 3 min control, then the G-code sets fan 60 %
    and prints 4 min to settle (k_fil re-learns for the new fan).
 4. PAUSE 3 (LCD "A2 COVER fan>Resume"): cover again, Resume. 4 min fault at 60 %.
 5. PAUSE 4 (LCD "A2 UNCOVER>Resume"): remove, Resume. 3 min control.
Safety: low (overhang/bridging quality only; a blower does not overheat with a blocked intake).
Labels: Resume 1 -> Pause 2 (A, fan 25 %), Resume 3 -> Pause 4 (A, fan 60 %), the windows after (-).
""")
    carrier_start(g)
    print_for(g, 360 - 2 * layer_time(), fan=FAN_LOW)
    pause(g, "A1 COVER fan>Resume", "cover the part-fan intake (25 %), then Resume")
    g.add("M117 A1 fault: fan 25% covered")
    print_for(g, 240)
    pause(g, "A1 UNCOVER>Resume", "remove the cover, then Resume")
    g.add("M117 A1 control")
    print_for(g, 180)
    g.add("M117 fan 60%, settling")
    print_for(g, 240, fan=FAN_HIGH)
    pause(g, "A2 COVER fan>Resume", "cover the part-fan intake (60 %), then Resume")
    g.add("M117 A2 fault: fan 60% covered")
    print_for(g, 240)
    pause(g, "A2 UNCOVER>Resume", "remove the cover, then Resume")
    g.add("M117 A2 control")
    print_for(g, 180)
    finish(g)
    return g


def make_fan60():
    g = GcodeWriter()
    start(g, f"""
Experiment 11b - both duct outlets covered at fan 60 % (category A), repeat of deney11's A2 window
Why: in deney11 (20.10) the intake faces the duct (not reachable), covers were put on/taken off at the wrong
pauses next to a running blower, and the part came loose. Fan 25 % stayed within +-1 W with one or both outlets
covered: at the user's normal fan a duct fault is sub-threshold.
Expected at 60 %: ~+5 W (fan loss ~4 W + the fan share of k_fil ~1.1 W) -> GUC_AZALDI and a shadow pause.
The part fan STOPS during each pause (easier taping) and comes back to 60 % after Resume.
{COMMON}
Steps:
 0. Before starting: cut two pieces of aluminium foil for the two duct outlets. Do NOT touch the part.
 1. Start the print. Fan 60 % from layer 3, ~6 min normal.
 2. PAUSE 1 (LCD "A COVER outlets>Resume"): cover BOTH duct outlets with the foil (foil must not touch the
    heater block or the sock), press Resume. 4 min fault.
 3. PAUSE 2 (LCD "A UNCOVER>Resume"): remove the foil, Resume. 4 min control.
Labels: Resume 1 -> Pause 2 (A, fan 60 %), Resume 2 -> end (-).
""")
    carrier_start(g)
    print_for(g, 360 - 2 * layer_time(), fan=FAN_HIGH)
    pause(g, "A COVER outlets>Resume", "cover both duct outlets, then Resume", fan_restore=FAN_HIGH)
    g.add("M117 A fault: outlets covered")
    print_for(g, 240)
    pause(g, "A UNCOVER>Resume", "remove the foil, then Resume", fan_restore=FAN_HIGH)
    g.add("M117 A control")
    print_for(g, 240)
    finish(g)
    return g


def make_draft():
    g = GcodeWriter()
    start(g, f"""
Experiment 12 - draft on the hotend (category C, more heat loss: GUC_ARTTI)
Question: the first GUC_ARTTI in a print. GUC_ARTTI never pauses; this measures size and delay only.
Expected: unknown, ~1-5 W more loss (the sock covers most of the block; the draft reaches the nozzle tip,
the heater block edges and the fresh print). First measurement of this path.
Physics: forced convection raises the block's loss to air (the k term), the PID has to give more power.
{COMMON}
Steps:
 1. Start the print. ~6 min normal at fan 25 %.
 2. PAUSE 1 (LCD "C FAN ON>Resume"): place a desk fan ~50 cm in front of the printer at nozzle height,
    lowest speed, aimed at the hotend; switch it on, press Resume. 4 min fault.
    Write down: distance, speed step, direction (front/side).
 3. PAUSE 2 (LCD "C FAN OFF>Resume"): switch it off, Resume. 4 min control.
Safety: low (the part may warp a little; the bed PID compensates, the bed twin will see it too).
Labels: Resume 1 -> Pause 2 (C), Resume 2 -> end (-).
""")
    carrier_start(g)
    print_for(g, 360 - 2 * layer_time(), fan=FAN_LOW)
    pause(g, "C FAN ON>Resume", "switch the desk fan on, then Resume")
    g.add("M117 C fault: draft")
    print_for(g, 240)
    pause(g, "C FAN OFF>Resume", "switch the desk fan off, then Resume")
    g.add("M117 C control")
    print_for(g, 240)
    finish(g)
    return g


# --- overhang tulip ---------------------------------------------------------------------------------------------
TULIP_CX = (72.0, 163.0)       # two tulips side by side (longer layer time = closer to a real print)
TULIP_R0 = 20.0                # mm - radius of the vertical part (centre line of the outer wall)
TULIP_ZB = 6.0                 # mm - vertical reference part
TULIP_H = 24.0                 # mm - the angle rises linearly over this height
TULIP_MAX = 70.0               # deg


def tulip_angle(z):
    return TULIP_MAX * min(max((z - TULIP_ZB) / TULIP_H, 0.0), 1.0)


def tulip_radius(z):
    """r(z) with local overhang angle theta(z) = theta_max * s: dr/dz = tan(theta) integrates to -ln(cos)."""
    th = math.radians(tulip_angle(z))
    if th <= 0:
        return TULIP_R0
    return TULIP_R0 + TULIP_H / math.radians(TULIP_MAX) * -math.log(math.cos(th))


def circle(cx, r):
    n = max(24, math.ceil(2 * math.pi * r / 1.0))
    return [(cx + r * math.cos(2 * math.pi * i / n), CY + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def make_tulip():
    g = GcodeWriter()
    zs = [round(H * (i + 1), 2) for i in range(round((TULIP_ZB + TULIP_H) / H))]
    table = "\n".join(f"  Z {z:5.1f} mm  angle {tulip_angle(z):4.1f} deg  radius {tulip_radius(z):5.1f} mm"
                      for z in zs if abs((z * 10) % 30) < 1e-6)
    start(g, f"""
Experiment 13 - overhang tulip (yellow level candidate, sub-threshold)
Question: does losing contact with the layer below show as missing power, and from which angle?
(Pencil Topper 19.10: printing into air from Z 15.3 gave +1.5 W, below the 2.5 W alarm.)
Expected: vertical part ~0 W; as the overhang angle grows, + missing power rising towards ~+1...1.5 W
where the line no longer rests on the layer below (PETG at fan 25 %: probably from ~50-60 deg).
No official alarm expected. The k_fil learner (open in 1-2.5 W) may swallow part of it: the analysis
also replays with k_fil frozen.
Physics: k_fil > 1 is heat conducted from the nozzle into the print; without support the line hangs in
air, that path disappears, less heat leaves than expected.
Geometry: two tulips (centres x = {TULIP_CX[0]:.0f} and {TULIP_CX[1]:.0f}), 2 walls, solid 2-layer base; vertical up to
Z {TULIP_ZB:.0f} mm, then the overhang angle rises linearly to {TULIP_MAX:.0f} deg at Z {TULIP_ZB + TULIP_H:.0f} mm. Constant speed
{V_PRINT:.0f} mm/s and constant fan 25 % on purpose (no overhang slow-down, no overhang fan): only the contact changes.
Z -> angle:
{table}
Steps: start the print, nothing else. The LCD shows the current angle every 5 deg.
Note the angle/height where the edge first curls or sags (and later measure it with a ruler).
Cancel when the edge curls up more than ~1 mm (the nozzle can catch it); note the time.
Safety: low; cancel early rather than late.
""")
    e1, e = e_per_mm(W1, H), e_per_mm(W, H)
    last_shown = -5
    for li, z in enumerate(zs):
        g.add(f";LAYER_CHANGE\n;Z:{z:.2f}")
        if li == 2:
            g.add(f"M106 S{FAN_LOW}")
        ang = tulip_angle(z)
        if ang >= last_shown + 5:
            last_shown = int(ang // 5) * 5
            g.add(f"M117 Y angle {last_shown} deg")
        for cx in TULIP_CX:
            r = tulip_radius(z)
            if li < 2:                                   # solid base: concentric rings, outside in
                w, v, ee = (W1, V_FIRST, e1) if li == 0 else (W, V_PRINT * 0.75, e)
                g.add(";TYPE:Bottom surface")
                ring_r = r
                first = True
                while ring_r > w / 2:
                    pts = circle(cx, ring_r)
                    if first:
                        g.travel(*pts[0], z=z)
                        first = False
                    else:
                        g.extrude_to(*pts[0], ee, v)
                    g.polygon(pts, ee, v)
                    ring_r -= w
            else:                                        # walls: inner then outer
                g.add(";TYPE:Outer wall")
                inner, outer = circle(cx, r - W), circle(cx, r)
                g.travel(*inner[0], z=z)
                g.polygon(inner, e, V_PRINT)
                g.extrude_to(*outer[0], e, V_PRINT)
                g.polygon(outer, e, V_PRINT)
    finish(g)
    return g


def main():
    for name, fn in (("deney10_mandal_acik", make_latch), ("deney11_fan_kapali", make_fan), ("deney11b_fan60_kanal_kapali", make_fan60),
                     ("deney12_cereyan", make_draft), ("deney13_cikinti_lale", make_tulip)):
        g = fn()
        path = os.path.join(HERE, name + ".gcode")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(g.text())
        print(f"{name}.gcode: {len(g.lines)} lines, ~{g.t / 60:.0f} min printing (+ heat-up ~5 min + pauses)")


if __name__ == "__main__":
    main()
