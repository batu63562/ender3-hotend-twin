"""Experiment 18 G-code (K89): why do the smallest layers of a Benchy show ~+1 W missing power?

Two candidates: (1) local geometry / fan air: over a large layer the part-fan air hits the print and bounces back onto
the block (impinging jet), over a small column it passes by; (2) layer time: on a short layer the layer below is still
hot, less heat is conducted from the nozzle into the print. The print separates them:
    L   large block 40 x 40 mm (2 walls + sparse infill)      ~21 s/layer   large surface around the nozzle
    C4  4 columns 14 x 14 mm on a solid floor                   ~21 s/layer   small surface, same layer time as L
    C2  2 of the columns                                         ~10 s/layer   small surface, shorter
    C1  1 column                                                 ~5 s/layer    small surface, shortest (Benchy cabin ~7 s)
Same line, layer height and speed everywhere (same commanded flow), fan off / 50 % in alternating 4 min segments.
Data collection only: touches no model code.

Usage (in dt/):   py yazici/deney18_katman_boyutu.py
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ariza_deneyleri import CX, CY, FIL_AREA, GcodeWriter, e_per_mm, finish, start  # noqa: E402

W, H, W1 = 0.45, 0.20, 0.50       # mm - steel 0.4 nozzle (Pi printer.cfg), the user's 0.2 mm profile
V, V_FIRST, V_FLOOR = 25.0, 20.0, 40.0   # mm/s - V: every measured segment (same commanded flow)
FAN = 128                         # M106 S: 50 % = fan_max_speed of the user's Orca PETG profile
L_SIDE = 40.0                     # mm - block, centre line of the outer wall
INFILL = 9.0                      # mm - sparse infill line spacing in the block (~5 %): L layer time = C4's
COL = 16.0                        # mm - column side (outer wall centre line)
COL_OFF = 11.0                    # mm - column centres at (CX +- 11, CY +- 11): inside the block top, 6 mm apart
SEG = 240.0                       # s - one measured segment
BRIM = 3


def rect(cx, cy, half):
    return [(cx - half, cy - half), (cx + half, cy - half), (cx + half, cy + half), (cx - half, cy + half)]


def walls(g, cx, cy, half, z, e, v, extra=()):
    """Inner wall then outer wall (then brim loops) of a square centred at (cx, cy)."""
    halves = [half - W, half] + list(extra)
    for i, h in enumerate(halves):
        pts = rect(cx, cy, h)
        if i == 0:
            g.travel(*pts[0], z=z)
        else:
            g.extrude_to(*pts[0], e, v)
        g.polygon(pts, e, v)


def infill(g, half, spacing, z, e, v, horizontal):
    """Rectilinear infill inside the inner wall (overlapping it by half a line), zig-zag."""
    lim = half - W - W / 2
    n = max(1, int(2 * lim / spacing))
    step = 2 * lim / n
    for i in range(n + 1):
        c = -lim + i * step
        a, b = (-lim, lim) if i % 2 == 0 else (lim, -lim)
        p0 = (CX + a, CY + c) if horizontal else (CX + c, CY + a)
        p1 = (CX + b, CY + c) if horizontal else (CX + c, CY + b)
        if i == 0 or math.hypot(p0[0] - g.x, p0[1] - g.y) > 2 * step:
            g.travel(*p0, z=z)
        else:
            g.extrude_to(*p0, e, v)
        g.extrude_to(*p1, e, v)


COLUMNS = [(CX - COL_OFF, CY - COL_OFF), (CX + COL_OFF, CY - COL_OFF),
           (CX + COL_OFF, CY + COL_OFF), (CX - COL_OFF, CY + COL_OFF)]


class Builder:
    def __init__(self):
        self.g = GcodeWriter()
        self.z = 0.0
        self.n = 0
        self.table = []           # (name, fan, z0, z1, layers, s/layer)

    def layer(self, kind, ncol=4, fan_cmd=None, first=False):
        g = self.g
        self.z = round(self.z + H, 2)
        self.n += 1
        g.add(f";LAYER_CHANGE\n;Z:{self.z:.2f}")
        if fan_cmd is not None:
            g.add(fan_cmd)
        e = e_per_mm(W1 if first else W, H)
        if kind == "L":
            g.add(";TYPE:Outer wall")
            walls(g, CX, CY, L_SIDE / 2, self.z, e, V_FIRST if first else V,
                  extra=[L_SIDE / 2 + W1 * (i + 1) for i in range(BRIM)] if first else ())
            g.add(";TYPE:Bottom surface" if first else ";TYPE:Sparse infill")
            infill(g, L_SIDE / 2, W1 if first else INFILL, self.z, e, V_FIRST if first else V, self.n % 2 == 0)
        elif kind == "floor":
            g.add(";TYPE:Outer wall")
            walls(g, CX, CY, L_SIDE / 2, self.z, e, V)
            g.add(";TYPE:Internal solid infill")
            infill(g, L_SIDE / 2, W, self.z, e, V_FLOOR, self.n % 2 == 0)
        else:                                   # columns
            g.add(";TYPE:Outer wall")
            for cx, cy in COLUMNS[:ncol]:
                walls(g, cx, cy, COL / 2, self.z, e, V)

    def segment(self, name, kind, ncol, fan_on, seconds):
        g = self.g
        t0, z0 = g.t, round(self.z + H, 2)
        g.add(f"; ===== SEGMENT {name}: {'fan 50 %' if fan_on else 'fan off'} =====")
        g.add(f"M117 d18 {name} {'fan50' if fan_on else 'nofan'}")
        cmd = f"M106 S{FAN}" if fan_on else "M107"
        i = 0
        while g.t - t0 < seconds:
            self.layer(kind, ncol, fan_cmd=cmd if i == 0 else None)
            i += 1
        self.table.append((name, "50 %" if fan_on else "off", z0, self.z, i, (g.t - t0) / i))


def make(table="", minutes=0):
    b = Builder()
    g = b.g
    start(g, f"""
Experiment 18 - layer size: geometry (fan air) or layer time? (K89)
Why: two Benchys (29.09, 21.10) showed ~+1 W missing power at their smallest layers (cabin windows, z 28-33),
with the fan constant and no bridges there. Divided by the small filament heat of those layers it looks like a
clog to the "missing power / filament heat" ratio. Which physics is it?
  (1) fan air: over a large layer the air hits the print and bounces onto the block; over a small column it
      passes by -> the block is cooled less -> + missing power. Predicts: C4 > L with the fan on, C4 = L fan off.
  (2) layer time: the layer below is still hot on a short layer -> less heat into the print -> + missing power.
      Predicts: C1 > C2 > C4 (fan on and off), L = C4. deney13 already hinted at it (12.5 -> 26 s gave MORE heat
      into the print).
Geometry, bottom to top (centre of the bed):
  block {L_SIDE:.0f} x {L_SIDE:.0f} mm, 2 walls + ~5 % infill (first layer solid, brim 3) -> 3-layer solid floor -> 4 columns
  {COL:.0f} x {COL:.0f} mm (2 walls, hollow) at the block's corners -> then only 2 -> then only 1 column.
Same line ({W} x {H} mm), same speed ({V:.0f} mm/s, ~{e_per_mm(W, H) * FIL_AREA * V:.1f} mm3/s, filament
{e_per_mm(W, H) * V:.2f} mm/s) in every measured segment. PETG 245 C, bed 85 C, steel 0.4 nozzle.
Fan 50 % = the max of the Orca profile (60 % would add the K78 steel fan error). Generated by
yazici/deney18_katman_boyutu.py. ~{minutes:.0f} min printing + heat-up ~5 min.
Segments (z = layer heights; the analysis cuts the log by z):
{table}
Steps: start the print, nothing else. The LCD shows the segment.
The last segment (C1 without fan, 5 s layers) may turn soft/blobby: that is expected. Cancel if the column
leans or the nozzle starts dragging a blob; note the time.
If the twin pauses the print (active pause, GUC_AZALDI 150 s): Resume, and note the time.
The twin restores its learned k at the end (marker below, K79): no state backup needed.
""")
    # first layer (solid, brim) and two settling layers, all fan off
    b.layer("L", first=True)
    b.layer("L")
    b.layer("L")
    for i, fan_on in enumerate((False, True, False, True)):
        b.segment(f"L{i + 1}", "L", 4, fan_on, SEG)
    g.add("; ===== floor (not measured) =====")
    g.add("M117 d18 floor")
    for i in range(3):
        b.layer("floor", fan_cmd=f"M106 S{FAN}" if i == 0 else None)
    for i, fan_on in enumerate((False, True, False, True)):
        b.segment(f"C4_{i + 1}", "C", 4, fan_on, SEG)
    b.segment("C2", "C", 2, True, SEG)
    b.segment("C1", "C", 1, True, SEG)
    b.segment("C1_nofan", "C", 1, False, 90.0)
    finish(g)
    return b, g


def main():
    b, g = make()                                  # first pass: segment table and print time for the header
    table = "\n".join(f"  {n:9s} fan {f:5s} z {z0:5.2f} - {z1:5.2f} mm  {k:3d} layers  ~{s:4.1f} s/layer"
                      for n, f, z0, z1, k, s in b.table)
    b, g = make(table, g.t / 60)
    text = g.text()
    path = os.path.join(HERE, "deney18_katman_boyutu.gcode")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"deney18_katman_boyutu.gcode: {len(g.lines)} lines, ~{g.t / 60:.0f} min printing (+ heat-up ~5 min)")
    for n, f, z0, z1, k, s in b.table:
        print(f"  {n:9s} fan {f:5s} z {z0:5.2f} - {z1:5.2f}  {k:3d} layers  {s:4.1f} s/layer")


if __name__ == "__main__":
    main()
