"""
Baski oncesi tahmin (golgeden ikize 1b): G-code -> zaman cizelgesi -> kapali dongu ikiz.

1) G-code okunur (Orca ciktisi + printer.cfg makrolari: START_GCODE, END_PRINT).
2) Klipper'in hiz planlayicisi (toolhead.py lookahead: kose hizi, ivme, minimum_cruise_ratio,
   ekstruder kose limiti) taklit edilir: her hareketin suresi -> filaman akisi zamana yayilir.
3) Hotend ve tabla, Klipper PID'leriyle kapali dongude simule edilir (kapali_dongu.KlipperPID):
   M109/M190 beklemeleri de simulasyonda gercekten beklenir (isinma suresi tahmini).
4) Cikti: zaman serisi (sicaklik, isitici gucu, akis, fan, katman) + rapor.
   Zaman serisi tahminler/tahmin_<ad>.csv'ye yazilir (Unity Print modulu okur, bkz. UNITY_ARAYUZ.md).

Dogrulama (G-code'un gercek logu varsa): simulasyon logdaki bir senkron aninda (ornegin 2. katmandaki
M104 S240) logun o anki durumundan baslatilir; katman zamanlari, akis ve isitici gucu karsilastirilir.

Kullanim:
    python gcode_tahmin.py yazici/supporto_mac_0.3mm_petg.gcode                 # baski oncesi rapor
    python gcode_tahmin.py G.gcode --log loglar/baskilar/0910_dunun_aynisi.csv  # dogrulama
"""
import argparse
import math
import os
import re
from collections import deque

import numpy as np
import pandas as pd

import degerlendir as d
import kapali_dongu as kd
import tabla_model as tm

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CFG = os.path.join(HERE, "yazici", "printer.cfg")
STEP = kd.REPORT_TIME          # s - termal simulasyon / PID adimi
HOME_TIME = 15.0               # s - G28 suresi (yaklasik; uc eksen, iki kademeli)
FILAMENT_AREA = math.pi * 1.75 ** 2 / 4


# ---------------------------------------------------------------------------
# printer.cfg
# ---------------------------------------------------------------------------
def read_cfg(path):
    """Bolumler -> {anahtar: deger}; SAVE_CONFIG (#*#) blogu ustune yazar; makrolarin gcode govdesi."""
    sections, macros, cur, in_gcode = {}, {}, None, False
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        saved = line.startswith("#*#")
        if saved:
            line = line[3:].strip()
        elif line.strip().startswith("#") or not line.strip():
            continue
        m = re.match(r"^\[(.+)\]\s*$", line.strip())
        if m:
            cur, in_gcode = m.group(1), False
            sections.setdefault(cur, {})
            continue
        if in_gcode and (raw.startswith((" ", "\t"))):
            macros.setdefault(cur.replace("gcode_macro ", ""), []).append(line.strip())
            continue
        in_gcode = False
        if cur and ":" in line or cur and "=" in line:
            k, v = re.split(r"[:=]", line, maxsplit=1)
            k, v = k.strip().lower(), v.strip()
            if k == "gcode" and cur.startswith("gcode_macro"):
                in_gcode = True
                continue
            sections[cur][k] = v
    return sections, macros


class Machine:
    def __init__(self, cfg_path=DEFAULT_CFG):
        s, self.macros = read_cfg(cfg_path)
        pr, ex = s["printer"], s["extruder"]
        f = lambda sec, k, dflt: float(s.get(sec, {}).get(k, dflt))
        self.max_v, self.max_a = float(pr["max_velocity"]), float(pr["max_accel"])
        self.max_z_v, self.max_z_a = f("printer", "max_z_velocity", self.max_v), f("printer", "max_z_accel", self.max_a)
        self.scv = f("printer", "square_corner_velocity", 5.0)
        self.min_cruise_ratio = f("printer", "minimum_cruise_ratio", 0.5)
        self.accel_to_decel = self.max_a * (1.0 - self.min_cruise_ratio)
        self.junction_dev = self.scv ** 2 * (math.sqrt(2.0) - 1.0) / self.max_a
        nd, fd = f("extruder", "nozzle_diameter", 0.4), f("extruder", "filament_diameter", 1.75)
        max_xs = f("extruder", "max_extrude_cross_section", 4.0 * nd * nd)
        ratio = max_xs / (math.pi * (fd * .5) ** 2)
        self.max_e_v = f("extruder", "max_extrude_only_velocity", self.max_v * ratio)
        self.max_e_a = f("extruder", "max_extrude_only_accel", self.max_a * ratio)
        self.instant_corner_v = f("extruder", "instantaneous_corner_velocity", 1.0)
        hb = s.get("heater_bed", {})
        self.pid_hotend = tuple(float(ex[k]) for k in ("pid_kp", "pid_ki", "pid_kd"))
        self.pid_bed = tuple(float(hb[k]) for k in ("pid_kp", "pid_ki", "pid_kd"))

    def expand(self, name, params):
        """Makroyu duz G-code satirlarina acar (sadece {PARAM} yerine koyma; Jinja 'set' satirlari atlanir)."""
        out = []
        for line in self.macros.get(name, []):
            if line.startswith("{%"):
                continue
            out.append(re.sub(r"\{(\w+)\}", lambda mm: params.get(mm.group(1), "0"), line))
        return out


# ---------------------------------------------------------------------------
# G-code -> islem listesi
# ---------------------------------------------------------------------------
class Move:
    __slots__ = ("start", "end", "axes_d", "move_d", "axes_r", "accel", "kin", "max_cruise_v2", "min_move_t",
                 "delta_v2", "smooth_delta_v2", "max_start_v2", "max_smoothed_v2", "junction_dev", "e",
                 "start_v", "cruise_v", "end_v", "accel_t", "cruise_t", "decel_t", "type")

    def __init__(self, mc, start, end, speed, ftype):
        self.start, self.end, self.type = start, end, ftype
        self.axes_d = [end[i] - start[i] for i in range(4)]
        self.e = self.axes_d[3]
        move_d = math.sqrt(sum(x * x for x in self.axes_d[:3]))
        velocity = min(speed, mc.max_v)
        self.kin = move_d >= 1e-9
        self.accel = mc.max_a
        if not self.kin:                            # sadece ekstruder (geri cekme / geri itme)
            move_d = abs(self.axes_d[3])
            self.accel = 99999999.9
        self.move_d = move_d
        inv = 1.0 / move_d if move_d else 0.0
        self.axes_r = [x * inv for x in self.axes_d]
        self.junction_dev = mc.junction_dev
        self.max_start_v2 = 0.0
        self.max_cruise_v2 = velocity ** 2
        self.min_move_t = move_d / velocity if velocity else 0.0
        self.delta_v2 = 2.0 * move_d * self.accel
        self.max_smoothed_v2 = 0.0
        self.smooth_delta_v2 = 2.0 * move_d * mc.accel_to_decel
        # kinematik / ekstruder limitleri (cartesian.check_move, extruder.check_move)
        if self.kin and abs(self.axes_d[2]) > 1e-9:
            zr = move_d / abs(self.axes_d[2])
            self.limit(mc.max_z_v * zr, mc.max_z_a * zr)
        if not self.kin:
            self.limit(mc.max_e_v, mc.max_e_a)

    def limit(self, speed, accel):
        if speed ** 2 < self.max_cruise_v2:
            self.max_cruise_v2 = speed ** 2
        self.accel = min(self.accel, accel)
        self.delta_v2 = 2.0 * self.move_d * self.accel
        self.smooth_delta_v2 = min(self.smooth_delta_v2, self.delta_v2)

    def calc_junction(self, prev, mc):
        if not self.kin or not prev.kin:
            return
        dr = self.axes_r[3] - prev.axes_r[3]
        extruder_v2 = (mc.instant_corner_v / abs(dr)) ** 2 if dr else self.max_cruise_v2
        max_start_v2 = min(extruder_v2, self.max_cruise_v2, prev.max_cruise_v2, prev.max_start_v2 + prev.delta_v2)
        cos_t = -(self.axes_r[0] * prev.axes_r[0] + self.axes_r[1] * prev.axes_r[1] + self.axes_r[2] * prev.axes_r[2])
        sin_d2 = math.sqrt(max(0.5 * (1.0 - cos_t), 0.0))
        cos_d2 = math.sqrt(max(0.5 * (1.0 + cos_t), 0.0))
        if 1.0 - sin_d2 > 0.0 and cos_d2 > 0.0:
            R_jd = sin_d2 / (1.0 - sin_d2)
            q = 0.25 * sin_d2 / cos_d2
            max_start_v2 = min(max_start_v2, R_jd * self.junction_dev * self.accel,
                               R_jd * prev.junction_dev * prev.accel, self.delta_v2 * q, prev.delta_v2 * q)
        self.max_start_v2 = max_start_v2
        self.max_smoothed_v2 = min(max_start_v2, prev.max_smoothed_v2 + prev.smooth_delta_v2)

    def set_junction(self, start_v2, cruise_v2, end_v2):
        h = 0.5 / self.accel
        accel_d = (cruise_v2 - start_v2) * h
        decel_d = (cruise_v2 - end_v2) * h
        cruise_d = self.move_d - accel_d - decel_d
        sv, cv, ev = math.sqrt(start_v2), math.sqrt(cruise_v2), math.sqrt(end_v2)
        self.start_v, self.cruise_v, self.end_v = sv, cv, ev
        self.accel_t = accel_d / ((sv + cv) * 0.5) if accel_d > 0 else 0.0
        self.cruise_t = cruise_d / cv if cv > 0 else 0.0
        self.decel_t = decel_d / ((ev + cv) * 0.5) if decel_d > 0 else 0.0

    @property
    def duration(self):
        return self.accel_t + self.cruise_t + self.decel_t


def flush(queue):
    """toolhead.py LookAheadQueue.flush (lazy=False): sondan basa tepe hiz ve kavsak hizlari."""
    delayed = []
    next_end_v2 = next_smoothed_v2 = peak_cruise_v2 = 0.0
    for i in range(len(queue) - 1, -1, -1):
        mv = queue[i]
        reachable_start_v2 = next_end_v2 + mv.delta_v2
        start_v2 = min(mv.max_start_v2, reachable_start_v2)
        reachable_smoothed_v2 = next_smoothed_v2 + mv.smooth_delta_v2
        smoothed_v2 = min(mv.max_smoothed_v2, reachable_smoothed_v2)
        if smoothed_v2 < reachable_smoothed_v2:
            if smoothed_v2 + mv.smooth_delta_v2 > next_smoothed_v2 or delayed:
                peak_cruise_v2 = min(mv.max_cruise_v2, (smoothed_v2 + reachable_smoothed_v2) * .5)
                if delayed:
                    mc_v2 = peak_cruise_v2
                    for m, ms_v2, me_v2 in delayed:
                        mc_v2 = min(mc_v2, ms_v2)
                        m.set_junction(min(ms_v2, mc_v2), mc_v2, min(me_v2, mc_v2))
                    del delayed[:]
            cruise_v2 = min((start_v2 + reachable_start_v2) * .5, mv.max_cruise_v2, peak_cruise_v2)
            mv.set_junction(min(start_v2, cruise_v2), cruise_v2, min(next_end_v2, cruise_v2))
        else:
            delayed.append((mv, start_v2, next_end_v2))
        next_end_v2 = start_v2
        next_smoothed_v2 = smoothed_v2
    queue.clear()


def parse(gcode_path, mc, abs_e=True):
    """G-code -> islem listesi: ('move', Move) | ('temp', 'hotend'|'bed', deger, bekle) | ('fan', 0-1)
    | ('dwell', s) | ('layer', z) | ('type', ad). Hareket sureleri planlayicidan doldurulur.
    abs_e: baski basindaki ekstruzyon modu. Klipper modu baskilar arasinda sifirlamaz; Orca'nin M83'u
    START_GCODE'dan SONRA geldigi icin makrodaki prime cizgisi onceki baskidan kalan modda yurur
    (14.10: goreli modda E28 = +28 mm, plan mutlak varsayip 14 mm eksik sayiyordu)."""
    ops, queue, prev = [], [], None
    pos = [0.0, 0.0, 0.0, 0.0]; base_e = 0.0
    abs_xyz = True
    speed = 25.0
    ftype = "Custom"

    def do_flush():
        nonlocal prev
        flush(queue)
        prev = None

    def lines():
        for raw in open(gcode_path, encoding="utf-8", errors="replace"):
            s = raw.strip()
            name = s.split(None, 1)[0].upper() if s and not s.startswith(";") else ""
            if name in ("START_GCODE", "END_PRINT", "CANCEL_PRINT"):
                params = dict(re.findall(r"(\w+)=([^\s]+)", s))
                for ln in mc.expand(name, params):
                    yield ln
            else:
                yield s

    for s in lines():
        if s.startswith(";"):
            if s.startswith(";Z:"):
                ops.append(("layer", float(s[3:])))
            elif s.startswith(";TYPE:"):
                ftype = s[6:].strip()
                ops.append(("type", ftype))
            continue
        code = s.split(";", 1)[0].strip()
        if not code:
            continue
        parts = code.split()
        cmd = parts[0].upper()
        args = {}
        for p in parts[1:]:
            if len(p) > 1 and p[0].isalpha():
                try:
                    args[p[0].upper()] = float(p[1:])
                except ValueError:
                    pass
        if cmd in ("G0", "G1"):
            if "F" in args:
                speed = args["F"] / 60.0
            new = list(pos)
            for i, ax in enumerate("XYZ"):
                if ax in args:
                    new[i] = args[ax] if abs_xyz else pos[i] + args[ax]
            if "E" in args:
                new[3] = (base_e + args["E"]) if (abs_xyz and abs_e) else pos[3] + args["E"]
            if new == pos:
                continue
            mv = Move(mc, tuple(pos), tuple(new), speed, ftype)
            if prev is not None:
                mv.calc_junction(prev, mc)
            queue.append(mv); ops.append(("move", mv)); prev = mv
            pos = new
        elif cmd == "G92":
            if "E" in args:
                base_e = pos[3] - args["E"]
            for i, ax in enumerate("XYZ"):
                if ax in args:
                    pos[i] = args[ax]
        elif cmd == "G90":
            abs_xyz = True
        elif cmd == "G91":
            abs_xyz = False
        elif cmd == "M82":
            abs_e = True
        elif cmd == "M83":
            abs_e = False
        elif cmd == "G28":
            do_flush(); ops.append(("dwell", HOME_TIME)); pos[:3] = [0.0, 0.0, 0.0]
        elif cmd == "G4":
            do_flush(); ops.append(("dwell", args.get("P", 0.0) / 1000.0 + args.get("S", 0.0)))
        elif cmd in ("M104", "M109"):
            if cmd == "M109":
                do_flush()
            ops.append(("temp", "hotend", args.get("S", 0.0), cmd == "M109"))
        elif cmd in ("M140", "M190"):
            if cmd == "M190":
                do_flush()
            ops.append(("temp", "bed", args.get("S", 0.0), cmd == "M190"))
        elif cmd == "M106":
            ops.append(("fan", min(1.0, args.get("S", 255.0) / 255.0)))
        elif cmd == "M107":
            ops.append(("fan", 0.0))
        elif cmd == "TURN_OFF_HEATERS":
            ops.append(("temp", "hotend", 0.0, False)); ops.append(("temp", "bed", 0.0, False))
    do_flush()
    return ops


# ---------------------------------------------------------------------------
# Kapali dongu simulasyonu (hotend + tabla)
# ---------------------------------------------------------------------------
class Sim:
    def __init__(self, mc, m, k=1.0, T_h=25.0, T_b=25.0, pwm_h=0.0, pwm_b=0.0, tgt_h=0.0, tgt_b=0.0, fan=0.0,
                 kfil=1.0):
        self.mc, self.m, self.k, self.kfil = mc, m, k, kfil
        self.T1, self.T2, self.T3 = m.initial_state(T_h, pwm_h, fan)
        self.Tal, self.Tcam = T_b, tm.initial_glass(T_b, pwm_b)
        self.pid_h = kd.KlipperPID(T_h, pwm_h, tgt_h, mc.pid_hotend)
        self.pid_b = kd.KlipperPID(T_b, pwm_b, tgt_b, mc.pid_bed)
        self.sm_h, self.sm_b = T_h, T_b                      # Klipper smoothed_temp (smooth_time 1 s)
        self.tgt_h, self.tgt_b, self.fan = tgt_h, tgt_b, fan
        self.t = 0.0
        self.ext_hist = deque()                              # (t, dt, hiz) - ikizle ayni 10 s net akis
        self.act_hist = deque()                              # (t, hareketli) - tabla aktivitesi (30 s)
        self.rows = []
        self.layer, self.ftype = 0.0, ""

    def step(self, e_rate, moving):
        """STEP saniye: PID'ler olcer, isiticilar suruler, modeller ilerler."""
        m, dt = self.m, STEP
        pwm_h = self.pid_h.update(self.t, self.T1, self.tgt_h)
        pwm_b = self.pid_b.update(self.t, self.Tal, self.tgt_b)
        self.ext_hist.append((self.t, dt, e_rate))
        while self.ext_hist and self.ext_hist[0][0] < self.t - m.EXT_AVG_WINDOW:
            self.ext_hist.popleft()
        span = sum(h[1] for h in self.ext_hist)
        net = max(sum(h[1] * h[2] for h in self.ext_hist) / span, 0.0) if span else 0.0
        self.act_hist.append((self.t, 1.0 if moving else 0.0))
        while self.act_hist and self.act_hist[0][0] < self.t - tm.ACT_WINDOW:
            self.act_hist.popleft()
        act = sum(a for _, a in self.act_hist) / len(self.act_hist)
        self.rows.append((self.t, self.T1, pwm_h, self.Tal, pwm_b, self.tgt_h, self.tgt_b, self.fan,
                          e_rate * FILAMENT_AREA, net * FILAMENT_AREA, self.layer, self.ftype))
        scale = 1.0 - m.BED_SAG * pwm_b
        self.T1, self.T2, self.T3 = m.model_step(self.T1, self.T2, self.T3, pwm_h * scale, self.fan, net, dt, self.k,
                                                 k_fil=self.kfil)
        self.Tal, self.Tcam = tm.step(self.Tal, self.Tcam, pwm_b, dt, 1.0 + tm.BED_FAN * self.fan + tm.BED_ACT * act)
        a = min(dt / kd.SMOOTH_TIME, 1.0)
        self.sm_h += (self.T1 - self.sm_h) * a
        self.sm_b += (self.Tal - self.sm_b) * a
        self.t += dt

    def frame(self):
        return pd.DataFrame(self.rows, columns=["t", "T1", "pwm", "bed_T", "bed_pwm", "target", "bed_target", "fan",
                                                "akis_anlik", "akis", "katman_z", "tip"])


def run(ops, sim, start_op=0, max_wait=3600.0):
    """Islemleri sirayla oynatir. Hareketler zaman ekseninde birikir; termal simulasyon hareketlerin
    arkasindan STEP adimlarla ilerler. Bekleme (M109/M190) simulasyonun kendisiyle cozulur.
    Donus: katman baslangic zamanlari [(z, t)]."""
    clock = sim.t                    # hareket saati (planlayicinin ilerledigi an)
    segs = deque()                   # (t0, t1, e) - ekstruzyon araliklari
    layers = []

    def advance(t_end):
        while sim.t + STEP <= t_end + 1e-9:
            t0, t1 = sim.t, sim.t + STEP
            e = 0.0; moving = False
            while segs and segs[0][1] <= t0:
                segs.popleft()
            for a, b, ev, kin in segs:
                if a >= t1:
                    break
                ov = min(b, t1) - max(a, t0)
                if ov > 0:
                    if b > a:
                        e += ev * ov / (b - a)
                    moving = moving or kin
            sim.step(e / STEP, moving)

    for op in ops[start_op:]:
        kind = op[0]
        if kind == "move":
            mv = op[1]
            dur = mv.duration
            if dur > 0:
                segs.append((clock, clock + dur, mv.e, mv.kin))
                clock += dur
            continue
        advance(clock)               # komut, oncesindeki hareketler bittiginde gecerli olur
        if kind == "layer":
            sim.layer = op[1]; layers.append((op[1], clock))
        elif kind == "type":
            sim.ftype = op[1]
        elif kind == "fan":
            sim.fan = op[1]
        elif kind == "dwell":
            clock += op[1]; advance(clock)
        elif kind == "temp":
            _, heater, val, wait = op
            if heater == "hotend":
                sim.tgt_h = val
            else:
                sim.tgt_b = val
            if wait and val > 0:
                pid, smooth = (sim.pid_h, "sm_h") if heater == "hotend" else (sim.pid_b, "sm_b")
                t_lim = sim.t + max_wait
                sim.step(0.0, False)
                while pid.busy(getattr(sim, smooth), val) and sim.t < t_lim:
                    sim.step(0.0, False)
                clock = max(clock, sim.t)
    advance(clock + 30.0)
    return layers


# ---------------------------------------------------------------------------
# Rapor ve dogrulama
# ---------------------------------------------------------------------------
def report(sim_df, layers, mc, m):
    df = sim_df
    P = df.pwm * m.P_MAX
    P_avail = m.P_MAX * (1.0 - m.BED_SAG * df.bed_pwm)
    n = int(10.0 / STEP)
    Pa = P.rolling(n, min_periods=1).mean()
    pr = (df.target >= 230) & (df.katman_z > 0)
    t_first = layers[0][1] if layers else 0.0
    t_end = df.t[pr].max() if pr.any() else df.t.iloc[-1]
    heat_b = df.t[(df.bed_target > 0)].min() if (df.bed_target > 0).any() else np.nan
    print(f"\nTAHMIN (k = {sim_df.attrs.get('k', 1):.3f})")
    print(f"  baslangic -> ilk katman: {t_first/60:.1f} dk (tabla + nozzle isinmasi, START_GCODE)")
    print(f"  baski (ilk katman -> son): {(t_end - t_first)/60:.1f} dk   toplam {t_end/60:.1f} dk")
    if pr.any():
        # hedef degisiminden sonraki 120 s haric (245 -> 240 gecisi "asim" sayilmasin)
        t_chg = df.t.where(df.target.diff().fillna(0) != 0).ffill().fillna(-1e9)
        dip = (df.target - df.T1)[pr & (df.t - t_chg > 120)]
        print(f"  baskida isitici: ort {P[pr].mean():.1f} W, 10 s ort. en yuksek {Pa[pr].max():.1f} W "
              f"(mevcut {P_avail[pr].mean():.1f} W) -> en dusuk pay %{100*(1 - (Pa[pr]/P_avail[pr]).max()):.0f}")
        print(f"  akis: ort {df.akis[pr & (df.akis > 0.5)].mean():.1f}, en yuksek (10 s) {df.akis[pr].max():.1f} mm3/s")
        print(f"  nozzle sicakligi: hedefin en fazla {dip.max():.1f} C altina dusuyor, en fazla {-dip.min():.1f} C ustune cikiyor")
        top = df[pr].assign(Pa=Pa[pr]).sort_values("Pa", ascending=False)
        print("  en zorlu anlar (10 s ortalama guc):")
        shown = []
        for r in top.itertuples():
            if any(abs(r.t - s) < 120 for s in shown):
                continue
            shown.append(r.t)
            print(f"    {r.t/60:6.1f}. dk  Z {r.katman_z:5.2f}  {r.tip:22s} akis {r.akis:4.1f} mm3/s  guc {r.Pa:4.1f} W  fan %{r.fan*100:.0f}")
            if len(shown) == 3:
                break


TAHMIN_DIR = os.path.join(HERE, "tahminler")
TAHMIN_COLS = ["t", "katman_z", "tip", "akis_anlik", "akis", "T1", "pwm", "target",
               "bed_T", "bed_pwm", "bed_target", "fan"]


def save_csv(df, path, t0=None):
    """Tahmin zaman serisi -> CSV (Unity Print modulu okur; sutunlar UNITY_ARAYUZ.md'de).
    t0 verilirse (dogrulama: logla senkron) 'time' = unix zamani (logun 'time' sutunuyla ayni saat) eklenir. Zaman bukulmez:
    takvim sapmasi (plan vs gercek katman zamani) Unity'de residual olarak gorunsun diye."""
    out = df[TAHMIN_COLS].copy()
    if t0 is not None:
        out.insert(0, "time", out.t + t0)
    out["tip"] = out.tip.fillna("").astype(str).str.replace(",", " ")   # Unity CSV'yi virgulle boler
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"   # Unity canli modda klasoru tarar: yarim yazilmis dosyayi asla gormesin
    out.to_csv(tmp, index=False, float_format="%.4f")
    os.replace(tmp, path)
    print(f"\ntahmin zaman serisi: {os.path.relpath(path, HERE)}  ({len(out)} satir, {out.t.iloc[-1]/60:.1f} dk)")


YOL_TOL = 0.05   # mm: ardisik segmentler birlestirilirken izin verilen sapma (gorsel; geometri bundan iyi okunmaz)


def save_toolpath(ops, path, start_op=0):
    """Unity'nin G-code geometrisi icin ekstruzyon segmentleri (bkz. UNITY_ARAYUZ.md, yol_*.bin).
    Her segment: x0 y0 x1 y1 z e (float32, little-endian). e = planin baslangicindan beri kumulatif filaman
    (kosu maksimumu), planin CSV'sindeki E ekseniyle ayni: Unity gercek ilerlemeyi (pos_e) buna esler.
    Ayni Z'de uc uca ve dogrultusu YOL_TOL icinde kalan ardisik segmentler birlestirilir (egriler yuzlerce minik
    segmentten olusuyor; 850 bin -> 545 bin)."""
    import struct
    out, run, e_cum, e_max = [], None, 0.0, 0.0

    def close():
        if run:
            out.append((run[0][0], run[0][1], run[1][0], run[1][1], run[0][2], run[2]))

    for op in ops[start_op:]:
        if op[0] != "move":
            continue
        m = op[1]
        e_cum += m.e
        e_max = max(e_max, e_cum)
        if not (m.kin and m.e > 0):
            continue
        s, e = m.start, m.end
        if run and abs(run[0][2] - s[2]) < 1e-6 and math.dist(run[1][:2], s[:2]) < 1e-6:
            a = run[0]
            dx, dy = e[0] - a[0], e[1] - a[1]
            L = math.hypot(dx, dy) or 1e-9
            if all(abs((p[0] - a[0]) * dy - (p[1] - a[1]) * dx) / L <= YOL_TOL for p in run[3] + [s]):
                run = (a, e, e_max, run[3] + [s])
                continue
        close()
        run = (s, e, e_max, [])
    close()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(b"YOL1" + struct.pack("<i", len(out)))
        f.write(b"".join(struct.pack("<6f", *s) for s in out))
    os.replace(tmp, path)
    return len(out)


def toolpath_path(plan_csv):
    """tahminler/tahmin_X.csv -> tahminler/yol_X.bin"""
    d, b = os.path.split(plan_csv)
    return os.path.join(d, "yol_" + b[len("tahmin_"):-len(".csv")] + ".bin")


def plan_for_live(gcode, cfg, t0, out_path, T_h=25.0, T_b=25.0, pwm_h=0.0, pwm_b=0.0,
                  tgt_h=0.0, tgt_b=0.0, fan=0.0, abs_e=True):
    """Canli ikiz (realtime_websocket.py) baski basladiginda arka planda cagirir.
    Simulasyon, baskinin basladigi anda OLCULEN durumdan baslar (sicak makinede soguk baslangic
    isinmayi dakikalarca fazla tahmin ederdi). t0: baskinin basladigi an (PC unix saati) -> CSV 'time'.
    Donus: tek satirlik ozet."""
    mc, m = Machine(cfg), d.load_model()
    k, kfil = m.load_k()
    sim = Sim(mc, m, k, T_h=T_h, T_b=T_b, pwm_h=pwm_h, pwm_b=pwm_b, tgt_h=tgt_h, tgt_b=tgt_b, fan=fan, kfil=kfil)
    ops = parse(gcode, mc, abs_e=abs_e)
    layers = run(ops, sim)
    df = sim.frame()
    save_csv(df, out_path, t0=t0)
    save_toolpath(ops, toolpath_path(out_path))
    end = df.t[df.akis_anlik > 0].max() if (df.akis_anlik > 0).any() else df.t.iloc[-1]
    first = layers[0][1] if layers else 0.0
    return (f"{os.path.basename(out_path)} · ilk katmana {first / 60:.1f} dk, baski bitisi {end / 60:.1f} dk "
            f"(k {k:.3f}, baslangic nozzle {T_h:.0f} C / tabla {T_b:.0f} C)")


def layer_times_log(log, sync_t):
    """Logda katman baslangiclari: Z'nin yeni bir degere cikip orada kaldigi ilk an (senkrondan sonra)."""
    z = log.pos_z.values; t = log.t.values
    out = {}
    for i in range(1, len(z)):
        if t[i] < sync_t - 1 or np.isnan(z[i]):
            continue
        zr = round(z[i], 2)
        if zr not in out and (i + 8 < len(z)) and np.all(np.round(z[i:i + 8], 2) == zr):
            out[zr] = t[i]
    return out


def validate(gcode, log_path, mc, m, k=None):
    log = d.read_log(log_path)
    for c in ("pos_z", "target", "bed_pwm", "bed_T", "bed_target"):
        log[c] = pd.to_numeric(log[c], errors="coerce")
    ops = parse(gcode, mc)
    # senkron: G-code'daki ilk "M104 S240" (2. katman) ile logda hedefin 240'a indigi an
    i_op = next(i for i, o in enumerate(ops) if o[0] == "temp" and o[1] == "hotend" and o[2] == 240.0)
    tg = log.target.ffill().values
    j = next(i for i in range(1, len(log)) if tg[i] == 240.0 and tg[i - 1] != 240.0)
    r0 = log.iloc[j]
    if k is None:
        k = float(pd.to_numeric(log.k_loss, errors="coerce").iloc[max(j - 1, 0)])
    # k_fil 2026-10-09'dan itibaren loglaniyor; eski loglarda 1
    kfil = float(pd.to_numeric(log.k_fil, errors="coerce").iloc[max(j - 1, 0)]) if "k_fil" in log else 1.0
    if np.isnan(kfil):
        kfil = 1.0
    fan0 = next((o[1] for o in reversed(ops[:i_op]) if o[0] == "fan"), 0.0)
    sim = Sim(mc, m, k, T_h=r0.T1_real, T_b=r0.bed_T, pwm_h=r0.pwm, pwm_b=r0.bed_pwm,
              tgt_h=245.0, tgt_b=r0.bed_target, fan=fan0, kfil=kfil)
    sim.layer = next(o[1] for o in reversed(ops[:i_op]) if o[0] == "layer")
    layers = run(ops, sim, start_op=i_op)
    s = sim.frame()
    s["t_log"] = s.t + r0.t
    # katman zamanlari
    # Z ofseti (SET_GCODE_OFFSET / Z ofset ayari): log Z'si G-code'dan sabit bir miktar farkli olabilir.
    # Ofset, logdaki kararli Z degerlerinin en yakin G-code katmanina farklarinin medyani.
    lt = layer_times_log(log, r0.t)
    gz = np.array([z for z, _ in layers])
    diffs = [zl - gz[np.argmin(np.abs(gz - zl))] for zl in lt if zl < gz.max() + 1]
    z_off = float(np.median(diffs)) if diffs else 0.0
    pairs = []
    for z, t_sim in layers:
        hit = [tl for zl, tl in lt.items() if abs(zl - z_off - z) < 0.01]
        if hit:
            pairs.append((z, t_sim + r0.t, min(hit)))
    print(f"\nDOGRULAMA: {os.path.basename(log_path)}  (senkron: 2. katman M104 S240, k = {k:.3f})")
    if pairs:
        e = np.array([ts - tl for _, ts, tl in pairs])
        dur_s = np.diff([p[1] for p in pairs]); dur_l = np.diff([p[2] for p in pairs])
        print(f"  katman zamani: {len(pairs)} katman, son katmanda sapma {e[-1]:+.0f} s "
              f"(gercek sure {(pairs[-1][2]-pairs[0][2])/60:.1f} dk, tahmin {(pairs[-1][1]-pairs[0][1])/60:.1f} dk, "
              f"%{100*e[-1]/(pairs[-1][2]-pairs[0][2]):+.1f}); katman suresi hatasi ort %{100*np.mean(dur_s/dur_l-1):+.1f}, "
              f"ort mutlak %{100*np.mean(np.abs(dur_s/dur_l-1)):.1f}")
        # zaman bukme: simulasyon zamanini katman baslangiclarina gore log zamanina esle
        warp = lambda ts: np.interp(ts, [p[1] for p in pairs], [p[2] for p in pairs])
        s["t_warp"] = warp(s.t_log)
    else:
        s["t_warp"] = s.t_log
    # karsilastirma: 10 s ortalamalar, ortak zaman izgarasinda
    n = int(10.0 / STEP)
    s["P"] = (s.pwm * m.P_MAX).rolling(n, min_periods=n).mean()
    s["Q"] = s.akis.rolling(n, min_periods=n).mean()
    lg = log[log.t >= r0.t].copy()
    ser = lambda col: pd.Series(col.values, index=pd.to_datetime(lg.t.values, unit="s"))
    lg["P"] = ser(pd.to_numeric(lg.pwm, errors="coerce").ffill() * m.P_MAX).rolling("10s").mean().values
    lg["Q"] = ser(lg.extrude_velocity * FILAMENT_AREA).rolling("10s").mean().values
    hold = (lg.target >= 230) & ((lg.T1_real - lg.target).abs() < 3)
    lg = lg[hold & (lg.t < s.t_warp.max())]
    for name, col in (("akis (mm3/s)", "Q"), ("isitici gucu (W)", "P")):
        sim_v = np.interp(lg.t, s.t_warp, s[col].bfill())
        err = sim_v - lg[col].values
        ok = ~np.isnan(err)
        cor = np.corrcoef(sim_v[ok], lg[col].values[ok])[0, 1]
        print(f"  {name:18s} gercek ort {np.nanmean(lg[col]):6.2f}  tahmin ort {np.nanmean(sim_v):6.2f}  "
              f"fark {np.nanmean(err):+5.2f}  rms {np.sqrt(np.nanmean(err**2)):5.2f}  korelasyon {cor:.2f}")
    T_sim = np.interp(lg.t, s.t_warp, s.T1)
    print(f"  nozzle sicakligi: rms {np.sqrt(np.mean((T_sim - lg.T1_real.values)**2)):.2f} C")
    # log.t Klipper saati (eventtime); Unity PC saatini ('time', unix) kullanir: senkron anindaki fark
    s.attrs["unix_offset"] = float(r0.time - r0.t)
    return s, lg


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("gcode")
    ap.add_argument("--cfg", default=DEFAULT_CFG)
    ap.add_argument("--log", help="bu G-code'un gercek logu: dogrulama")
    ap.add_argument("--k", type=float, help="kayip carpani (verilmezse twin_state.json'daki ogrenilmis k)")
    ap.add_argument("--csv", help="tahmin zaman serisinin dosyasi (varsayilan: tahminler/tahmin_<log ya da gcode adi>.csv)")
    ap.add_argument("--yol", help="sadece Unity takim yolunu bu dosyaya yaz (mevcut bir canli plan icin, simulasyonsuz)")
    ap.add_argument("--goreli-e", action="store_true", help="baski basinda Klipper goreli ekstruzyon modundaydi (M83)")
    a = ap.parse_args()
    if a.yol:
        n = save_toolpath(parse(a.gcode, Machine(a.cfg), abs_e=not a.goreli_e), a.yol)
        print(f"takim yolu: {a.yol} ({n} segment)")
        raise SystemExit
    mc, m = Machine(a.cfg), d.load_model()
    name = os.path.splitext(os.path.basename(a.log or a.gcode))[0]
    csv_path = a.csv or os.path.join(TAHMIN_DIR, f"tahmin_{name}.csv")
    if a.log:
        s, _ = validate(a.gcode, a.log, mc, m, a.k)
        save_csv(s, csv_path, t0=(s.t_log - s.t).iloc[0] + s.attrs["unix_offset"])
    else:
        k, kfil = m.load_k()
        k = a.k if a.k is not None else k
        sim = Sim(mc, m, k, kfil=kfil)
        layers = run(parse(a.gcode, mc), sim)
        df = sim.frame(); df.attrs["k"] = k
        report(df, layers, mc, m)
        save_csv(df, csv_path)
