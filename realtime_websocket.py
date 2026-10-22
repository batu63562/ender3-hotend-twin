import asyncio
import json
import time
import csv
import math
import os
import shutil
import tomllib
from collections import deque

import websockets

import bed_model   # bed twin (separate model file; does not touch the nozzle model in this file)

# All files are written to the script's folder (whichever folder VS Code runs it from)
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "realtime_log_ws.csv")
STATE_FILE = os.path.join(HERE, "twin_state.json")
STATE_MAX_AGE = 1800   # 30 minutes - an older file counts as "stale"
STATE_MAX_T1_DIFF = 5.0  # C - do not inherit if the saved T1 differs from the current T1 by more than this

# Printer address from config.toml (copied from config.example.toml; not in git). The module can be imported
# without it: evaluate.py and the plan tools do not connect to the printer, only the live twin needs it (main).
CONFIG_FILE = os.path.join(HERE, "config.toml")


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


CONFIG = load_config()
PRINTER_HOST = str(CONFIG.get("printer_host", "")).strip()
WS_URL = f"ws://{PRINTER_HOST}/websocket"

# Hotend model, 3-node RC network:
#   [heater cartridge T3] --R31--> [block near thermistor T1] --R_HB--> [nozzle/heat-break top T2]
# T1 loses to air (convection + radiation, scaled by k) and gives heat to the print (filament + contact, k_fil);
# T2 loses through the heat break to the heatsink (HA2) and to the part fan (HA2_FAN, scaled by k).
# Parameters fitted to the September calibration logs within physical bounds (benchmark/). The heatsink fan
# is not read from Klipper; it always runs, so its effect is inside HA2.
MODEL_VERSION = "3dugum-gecikenkutle-v4"   # twin_state.json from another version is not inherited [K01]
T_AMB = 25      # room temperature (C); fixed assumption - the real room is 21-25 C, the difference goes into k
T_AMB_K = T_AMB + 273.15
P_MAX = 40      # W - full power of the heater cartridge
BED_SAG = 0.042  # the 24 V rail sags while the bed is on: real power = pwm * P_MAX * (1 - BED_SAG * bed_pwm) [K02]

C3 = 0.8303     # J/K - heater cartridge
R31 = 3.0       # K/W - cartridge -> block (in a 245 C hold the cartridge is ~55 C hotter)
C1 = 2.0        # J/K - block near the thermistor
HA1 = 0.03624   # W/K - convection from the block to air
CRAD = 1.5259e-10
R_HB = 1.805    # K/W - block -> rest of the hot metal
C2 = 4.034      # J/K - rest of the hot metal (nozzle, far side of the block, top of the heat break)
HA2 = 0.004435  # W/K - loss from the rest of the metal outwards (heat break -> heatsink)
HA2_FAN = 0.0293  # W/K - extra conductance from T2 by the part cooling fan at full speed [K03]
FAN_EXP = 0.508   # fan cooling ~ ((f-F0)/(1-F0))**FAN_EXP (forced convection h ~ air speed^0.5) [K03]
FAN_F0 = 0.191    # fan threshold: below this PWM the 5015 blower cannot overcome the duct resistance, cooling ~0 [K03]
K_FIL = 0.0054   # W/(K*mm/s) - heat taken by the filament = K_FIL * net feed rate * (T1 - T_amb) (PETG cp) [K04]

MAX_SUBSTEP = 0.3      # s - upper limit of the RK4 substep
RK4_STABILITY = 2.5    # RK4 needs |lambda*h| < 2.78 on the real axis; margin left

# Learning (KLossLearner): k (loss-to-air factor) and k_fil (heat-into-the-print factor in fan-on printing), from the
# energy balance between the measured power and the model's loss, window by window, with a small Kalman filter:
#   measurement  y = (supplied - stored - heatsink path) / duration   [W]
#   model        y = k * (block + fan loss) + k_fil * K_FIL * flow * (T - T_amb)
# Two kinds of window: HOLD (no extrusion, k only) and PRINT (k + k_fil). While the fan is not cooling, the heat
# into the print is filament heating only (factor fixed at 1.0, "A0"). History and scorecards: [K05]-[K09], [K17].
ADAPT_TMIN = 230.0     # C - learn only in the printing temperature range [K11]
ADAPT_TAU = 60.0       # s - learning time constant
ADAPT_WINDOW = 20.0    # s - the energy balance is collected over windows of this length
ADAPT_T_RANGE = 3.0    # C - a window counts as a "steady hold" if its T range is below this
# "Printing temperature" follows the heater target, so other materials (PLA ~210 C) are covered too [K54]:
HOT_FLOOR = 180.0          # C - never counts below this: the 150 C preheat hold stays out of learning [K11]
HOT_BELOW_TARGET = 15.0    # C - within this of the target (PETG 245 -> 230, the old fixed value)
K_LOSS_MIN, K_LOSS_MAX = 0.85, 1.15   # physical bounds of k
PRINT_WINDOW = 60.0       # s - print window; a short window fits between transitions [K17]
PRINT_TAU = 1800.0        # s - time constant of k while printing: faults (seconds-minutes) do not leak into k [K06]
PRINT_T_RANGE = 5.0       # C - allowed T range over the window
PRINT_VALID_FRAC = 0.8    # at least this fraction of the window has a valid missing-power value (no heat-up/target change)
# Kalman: Q_FIL how fast k_fil may change (process noise), R_* window measurement noise. k has no process noise;
# its speed is set by a covariance floor (span/tau) [K07]. A larger Q_FIL swallows slow faults [K17].
Q_FIL = 0.05 ** 2 / 3600.0    # 1/s - k_fil may change ~0.05 per hour
R_HOLD = 0.3 ** 2             # W^2 - measurement noise of a 20 s hold window
R_PRINT = 0.4 ** 2 * 300.0 / PRINT_WINDOW   # W^2 - ~0.4 W residual in a 300 s window; noise ~1/duration
P0_FIL = 0.15 ** 2            # k_fil uncertainty at session start (or material change)
K_FIL_MIN, K_FIL_MAX = 0.8, 1.6   # k_fil = filament heating (1.0) + contact conduction from the nozzle into the print [K08]
# k_fil belongs to the print (geometry, speed), not to the machine or the material: every print (and a material
# change) starts from this anchor with P0_FIL uncertainty and learns from there; k is kept between prints [K26, K27].
K_FIL_ANCHOR = 1.33
K_STATE_MAX_AGE = 7 * 86400.0   # s - k is kept between sessions; older than this is not inherited
EXT_EPS = 1e-6   # mm/s - Klipper reports ~1e-15 crumbs when idle; speeds below this are zero [K16]
# Rejected/removed rules: delaying hold learning after arrival [K10], learning from the 150 C preheat [K11],
# fading k out below 150 C [K12].
# Temperature-dependent heat capacity: C(T) = C * (1 + CAP_T_COEF * (T - CAP_T0)), each node at its own temperature;
# no effect on steady state, fixes the heat-up dynamics [K13]. 0: off (constant C).
CAP_T_COEF = 0.0012  # 1/K
CAP_T0 = 140.0      # C - C(T) = C at this temperature

# This printer's numbers can come from files instead of the defaults above [K52]: code default <- profile.toml (what
# the user knows about the hardware) <- calibration.json (fitted by calibrate.py from a calibration print). Missing
# files keep the defaults; a broken file is reported and ignored (the twin keeps running). See PROFILE_DESIGN.md.
PROFILE_FILE = os.path.join(HERE, "profile.toml")
CALIBRATION_FILE = os.path.join(HERE, "calibration.json")
PROFILE_KEYS = {("", "room_c"): "T_AMB", ("hotend", "heater_power_w"): "P_MAX", ("power", "bed_sag"): "BED_SAG",
                ("part_fan", "threshold"): "FAN_F0", ("part_fan", "exponent"): "FAN_EXP"}
CALIBRATED = ("C1", "C2", "C3", "R31", "R_HB", "HA1", "CRAD", "HA2", "HA2_FAN", "FAN_F0", "FAN_EXP",
              "CAP_T_COEF", "K_FIL")
# the bed twin's numbers come from the same files and land in bed_model (one room temperature for both twins)
BED_PROFILE_KEYS = {("", "room_c"): "T_AMB", ("bed", "heater_power_w"): "P_BED"}
BED_CALIBRATED = ("C_AL", "C_GLASS", "R_AC", "H_AL", "H_GLASS", "EPS_A")


def load_printer_numbers():
    """Applies profile.toml, then calibration.json, to the module's parameters (and the bed twin's, in bed_model).
    Returns (sources, notes): the files used and anything ignored, for the start-up console."""
    g, sources, notes = globals(), [], []
    try:
        if os.path.exists(PROFILE_FILE):
            with open(PROFILE_FILE, "rb") as f:
                prof = tomllib.load(f)
            for (section, key), name in PROFILE_KEYS.items():
                table = prof if not section else prof.get(section, {})
                if key in table:
                    g[name] = float(table[key])
            for (section, key), name in BED_PROFILE_KEYS.items():
                table = prof if not section else prof.get(section, {})
                if key in table:
                    setattr(bed_model, name, float(table[key]))
            sources.append(os.path.basename(PROFILE_FILE))
    except (OSError, ValueError) as e:
        notes.append(f"{os.path.basename(PROFILE_FILE)} ignored: {type(e).__name__}: {e}")
    try:
        if os.path.exists(CALIBRATION_FILE):
            with open(CALIBRATION_FILE, encoding="utf-8") as f:
                cal = json.load(f)
            for name, value in cal.get("params", {}).items():
                if name in CALIBRATED:
                    g[name] = float(value)
                elif name in BED_CALIBRATED:
                    setattr(bed_model, name, float(value))
                else:
                    notes.append(f"unknown calibration parameter ignored: {name}")
            sources.append(f"{os.path.basename(CALIBRATION_FILE)} ({cal.get('date', '?')})")
    except (OSError, ValueError, AttributeError) as e:
        notes.append(f"{os.path.basename(CALIBRATION_FILE)} ignored: {type(e).__name__}: {e}")
    g["T_AMB_K"] = g["T_AMB"] + 273.15
    return sources, notes


PRINTER_SOURCES, PRINTER_NOTES = load_printer_numbers()

def hot_threshold(target):
    """Lowest temperature counted as printing temperature by the learner, the acceptance counters and the
    missing-power alarm: the target minus HOT_BELOW_TARGET, not below HOT_FLOOR. Without a target (old logs, heater
    off) the fixed ADAPT_TMIN as before [K54]."""
    if target is None or target <= 0:
        return ADAPT_TMIN
    return max(HOT_FLOOR, target - HOT_BELOW_TARGET)


def cap_scale(T):
    """Heat capacity factor at T (see CAP_T_COEF)."""
    return max(0.5, 1.0 + CAP_T_COEF * (T - CAP_T0))


def cap_energy(T):
    """Integral of cap_scale over T (per J/K): C * cap_energy(T) is the node's internal energy (up to a constant)."""
    return T + 0.5 * CAP_T_COEF * (T - CAP_T0) ** 2


def stored_energy(T_from, T_to):
    """Energy stored when the three nodes go together from T_from to T_to (J); C(T) included."""
    return (C1 + C2 + C3) * (cap_energy(T_to) - cap_energy(T_from))

# Drift alarm (KAYIP_*): learning could take a slow fault (sock fell off, duct shifted) as "normal" and remove it
# from the residual; so k itself is watched. Reference = last ACCEPTED k level (kept between sessions):
# alarm when k deviates from the reference by K_ALARM_BAND; if k stays within K_ACCEPT_RANGE at the new level for
# K_ACCEPT_TIME (hot operation), the new level is accepted, the alarm closes and the event is reported [K14].
# This moving reference accepts slow degradation step by step; the fixed-reference maintenance indicator catches it [K20].
K_ALARM_BAND = 0.08
K_ALARM_CLEAR = 0.06        # the alarm closes only when the deviation drops below this (experiment 8: k 0.92 at the band edge toggled 13 times)
K_ACCEPT_TIME = 30 * 60.0   # s - hot (T1 > ADAPT_TMIN) time spent outside the band AND steady
K_ACCEPT_RANGE = 0.02       # k counts as "steady" while it stays within this range

# Fault detection (TWO alarms since 2026-10-12): fast faults GUC_* (missing power, below), slow drift KAYIP_*
# (learned k vs. its reference). The temperature residual alarm (SICAKLIK_*) was removed: the open-loop model wanders
# +-2 C in the residual (see ENERGY_*), the anomaly decision is on the power balance. A condition counts as a fault
# only if it lasts the confirmation time WITHOUT interruption (single-sample spikes must not alarm); it ends after
# FAULT_CLEAR seconds unseen.
FAULT_CONFIRM = 3.0
FAULT_CLEAR = 10.0
FAULT_TEXT = {
    "KAYIP_ARTTI": "isi kaybi son saatlere gore artti: silikon corap dusmus, cereyan/hava akimi ya da "
                   "fan kanali bloga yonelmis olabilir",
    "KAYIP_AZALDI_FAN": "fan acik ama beklenen sogutma yok (son saatlere gore): parca sogutma fani "
                        "donmuyor ya da kanali tikali olabilir",
    "KAYIP_AZALDI": "isi kaybi son saatlere gore azaldi: nozzle'da plastik/topak, ortam isinmis (kapali kutu?) "
                    "ya da hotend yalitimi degismis olabilir",
}

# Missing power (OFFICIAL fast-fault alarm, GUC_*): difference (W) between the loss the model expects over the last
# ENERGY_WINDOW seconds and the heat that really left (measured power - stored energy). + = LESS heat leaving than expected.
# Why power, not temperature: the PID keeps the real temperature constant, so the heater power measures the real load
# (a calorimeter); the open-loop temperature residual amplifies and accumulates small power errors at ~15 C/W [K15].
ENERGY_WINDOW = 20.0      # s
ENERGY_THRESHOLD = 2.5    # W
ENERGY_CONFIRM = 5.0      # s - uninterrupted
ENERGY_TMIN = 230.0       # C - only in the printing temperature range (where the model is calibrated best)
ENERGY_T_RANGE = 3.0      # C - a "hold" if the T range in the window is below this
ENERGY_SETTLE = 30.0      # s - wait after a heat-up (heater 3 s avg. >= 95 %) ends
ENERGY_TEXT = {
    "GUC_AZALDI": "beklenenden AZ isi gidiyor: filaman akmiyor/tikanma, fan havasi nozzle'a ulasmiyor "
                  "ya da nozzle etrafinda topak olabilir",
    "GUC_ARTTI": "beklenenden FAZLA isi gidiyor: silikon corap dusmus, cereyan ya da fan kanali bloga "
                 "yonelmis olabilir",
}
FAULT_TEXT.update(ENERGY_TEXT)

# Pause (the twin's only feedback to the printer): if GUC_AZALDI lasts PAUSE_AFTER s without interruption and the
# print is printing. SHADOW mode only logs "would pause" (CSV duraklat, ariza_raporlari.txt); active mode sends a
# pause to Moonraker. Benign events lasted <= 110 s, persistent real faults >= 246 s [K19]. KAYIP_* never pauses.
PAUSE_AFTER = 150.0       # s
PAUSE_MODE = "aktif"      # "golge" (shadow: log only) | "aktif" (active: pause via Moonraker) | "kapali" (off) [K50]

# Material and backup: the material is read from the G-code header ("; filament_type = ") for the CSV; k_fil is not
# stored per material any more, every print starts from K_FIL_ANCHOR [K27]. At every start a dated copy of
# twin_state.json is taken, the newest STATE_BACKUPS are kept [K21].
STATE_BACKUP_DIR = os.path.join(HERE, "durum_yedek")
STATE_BACKUPS = 30

# Maintenance indicator (advice OUTSIDE the model; does not touch prediction or alarms): the own k of every window
# in which k is the only unknown is written to MAINT_FILE. At start-up the daily median of fan-off print windows is
# compared with a FIXED clean reference (MAINT_REF, calibration); advice if the last MAINT_DAYS data days are all
# outside MAINT_BAND in the same direction. The reference is changed by hand only when the hardware changes [K20].
MAINT_FILE = os.path.join(HERE, "bakim_izleme.csv")
MAINT_STATUS_FILE = os.path.join(HERE, "bakim_durumu.json")   # start-up result: read by Unity (it does not recompute the rule)
MAINT_REF = 1.00
MAINT_BAND = 0.06
MAINT_DAYS = 3
MAINT_LOOKBACK = 21          # days - the rule only looks at data this recent
MAINT_MIN_WINDOWS = 2        # at least this many fan-off print windows per day


def maint_record(rows):
    """[(wall_time, kind, k)] -> append to MAINT_FILE."""
    if not rows:
        return
    new = not os.path.exists(MAINT_FILE)
    try:
        with open(MAINT_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "tarih", "tur", "k"])
            for tw, kind, kv in rows:
                w.writerow([round(tw, 1), time.strftime("%Y-%m-%d %H:%M", time.localtime(tw)), kind, round(kv, 4)])
    except OSError as e:
        print(f"[bakim izleme kaydi yazilamadi: {e}]")


def maint_check():
    """Maintenance advice text or None (see MAINT_*). The result (also when there is no advice) is written to
    MAINT_STATUS_FILE: Unity shows it in the start menu and does not compute the rule itself (single source of truth)."""
    status = {"time": round(time.time(), 1), "enough": False, "advice": False, "direction": "",
              "reference": MAINT_REF, "band": MAINT_BAND, "days": [], "text": ""}
    try:
        with open(MAINT_FILE, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("tur") == "fansiz_baski"]
    except OSError:
        rows = []
    cutoff = time.time() - MAINT_LOOKBACK * 86400
    days = {}
    for r in rows:
        try:
            tw, kv = float(r["time"]), float(r["k"])
        except (KeyError, ValueError):
            continue
        if tw >= cutoff and 0.6 < kv < 1.5:
            days.setdefault(time.strftime("%Y-%m-%d", time.localtime(tw)), []).append(kv)
    med = [(dd, sorted(v)[len(v) // 2]) for dd, v in sorted(days.items()) if len(v) >= MAINT_MIN_WINDOWS]
    last = med[-MAINT_DAYS:]
    status["days"] = [{"date": dd, "k": round(kv, 4)} for dd, kv in last]
    text = None
    if len(med) >= MAINT_DAYS:
        status["enough"] = True
        devs = [kv - MAINT_REF for _, kv in last]
        why = None
        if all(dv < -MAINT_BAND for dv in devs):
            status["direction"] = "low"
            why = ("hotend beklenenden AZ isi kaybediyor -> heatsink tarafini kontrol et: heat break termal macunu, "
                   "heatsink/fan cevresinde kurumus plastik ve toz (isi surunmesi = tikanma riski)")
        elif all(dv > MAINT_BAND for dv in devs):
            status["direction"] = "high"
            why = "hotend beklenenden FAZLA isi kaybediyor -> silikon corap, fan kanali yonu, cereyan"
        if why:
            text = (f"BAKIM ONERISI: son {MAINT_DAYS} gun fansiz baski k = "
                    + ", ".join(f"{dd[5:]} {kv:.3f}" for dd, kv in last)
                    + f" (temiz referans {MAINT_REF:.2f}, esik +-{MAINT_BAND:.2f}); {why}")
    status["advice"], status["text"] = text is not None, text or ""
    try:
        tmp = MAINT_STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=1)
        os.replace(tmp, MAINT_STATUS_FILE)
    except OSError as e:
        print(f"[bakim durumu yazilamadi: {e}]")
    return text

# Fault classification (fault_classifier.py): when a fault is confirmed, the event's physical category
# (A fan path / B no filament flow / C insulation-environment / D thermistor / E heater) is computed in the
# background. The category needs data after the event; it runs this many seconds after the confirmation
# (the second one is an update while the event lasts). Does not touch the detection logic or the Twin.
CLASSIFY_DELAYS = (60.0, 180.0)
CLASSIFY_BUFFER = 600.0          # s - latest rows kept in memory (reference + event window)
REPORT_FILE = os.path.join(HERE, "ariza_raporlari.txt")

# G-code plan (gcode_plan.py, roadmap 4.3): when a print starts, the file and the current printer.cfg are
# downloaded from Moonraker and the twin's plan is produced in the background -> PLAN_DIR (read by the Unity Print module).
# Only INPUTS are read from Klipper (which file, when it started); layer time, flow, power are the twin's prediction.
HTTP_URL = f"http://{PRINTER_HOST}"
PLAN_DIR = os.path.join(HERE, "tahminler")
GCODE_DIR = os.path.join(HERE, "yazici", "canli")        # downloaded G-code and printer.cfg
CFG_FALLBACK = os.path.join(HERE, "yazici", "printer.cfg")  # local copy if the Pi cannot be reached
DOWNLOAD_TIMEOUT = 30.0

# Phone monitoring: the script serves a small page on the home network (http://<computer-IP>:WEB_PORT).
# Read only (does not touch the twin's computation); sends nothing to the internet. If the port cannot be opened,
# monitoring continues without the page. `web_port` in config.toml (0 = off); on a Klipper Pi 8080 is often the
# webcam (crowsnest), so the Pi install uses another port.
WEB_PORT = int(CONFIG.get("web_port", 8080)) or None
WEB_HISTORY = 900.0      # s - chart length on the page (15 min, at most 1 point per second)

# Connection: the websockets library pings every PING_INTERVAL and treats the connection as lost if no answer comes
# within PING_TIMEOUT. The default is 20 s; under network congestion (2026-09-26 prints, no messages for 8-16 s
# during a video stream) the script was closing -> more patient + automatic reconnect.
PING_INTERVAL = 20.0
PING_TIMEOUT = 60.0
RECONNECT_DELAY = 5.0
# Klipper sends a status update at least every ~1 s even when idle (temperature noise); a longer silence while
# Klippy is reported ready means a lost subscription (2026-10-20: Klipper RESTART, socket open, no data) [K33]
STATUS_TIMEOUT = 60.0

STATE_SAVE_INTERVAL = 5.0   # s - twin_state.json is written at most this often (not on every message)
FLUSH_INTERVAL = 1.0        # s - the CSV is forced to disk at most this often

GAP_THRESHOLD = 10.0        # seconds - re-sync to reality after longer gaps
# Flow input: the difference of the extruder POSITION (pos_e) between two messages (the instantaneous speed sample
# was sampling retractions wrongly); SIGNED average over the last EXT_AVG_WINDOW seconds (retract + unretract cancel),
# then clipped below 0. Without pos_e (old logs) the instantaneous speed is used [K16].
EXT_AVG_WINDOW = 10.0       # s
POS_E_MAX_RATE = 60.0       # mm/s - pos_e jumps faster than this (Klipper restart etc.) are ignored


def initial_state(T1, pwm, fan):
    """T2/T3 estimate when starting from scratch.
    If the heater is on (pwm > 0) the temperature is assumed to be held: T3 hot enough to carry the whole loss
    through R31 (~55 C above at 245 C), T2 at its steady value. Starting all equal to T1 produced a ~8 C fake
    dip in the first seconds of a hot start.
    If the heater is off (idle / cool-down) all equal T1."""
    if not pwm or pwm <= 0:
        return T1, T1, T1
    fan = fan or 0.0
    dT2 = steady_T2_rise(T1, fan)
    T2 = T_AMB + dT2
    # in steady state the heat through R31 = total loss (block surface + via T2)
    T3 = T1 + R31 * (natural_loss(T1) + (HA2 + fan_conductance(fan)) * dT2)
    return T1, T2, T3


def load_state(current_T1, pwm=0.0, fan=0.0):
    """T1 ALWAYS starts from the real measurement. Only T2/T3 are inherited from the previous run."""
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
    except OSError:
        saved = None
    except ValueError:
        saved = None
        print(f"[UYARI: {STATE_FILE} bozuk - sifirdan baslaniyor; ogrenilmis k icin son saglam yedek {STATE_BACKUP_DIR}]")
    if saved is not None:
        age = time.time() - saved["time"]
        if saved.get("model") != MODEL_VERSION:
            print("[Kayitli durum baska model surumunden - sifirdan baslaniyor]")
        elif age >= STATE_MAX_AGE:
            print(f"[Kayitli durum {age:.0f}s eski - sifirdan baslaniyor]")
        elif abs(saved["T1"] - current_T1) > STATE_MAX_T1_DIFF:
            # if the nozzle cooled/heated in between, the saved T3 (heater core) is no longer valid
            print(f"[Kayitli T1={saved['T1']:.1f}C, simdiki {current_T1:.1f}C - sifirdan baslaniyor]")
        else:
            print(f"[Onceki T2/T3 durumu bulundu, {age:.0f}s once - devraliniyor]")
            return current_T1, saved["T2"], saved["T3"]
    return initial_state(current_T1, pwm, fan)


def load_k():
    """(k, k_fil) learned in earlier sessions (same model version, newer than K_STATE_MAX_AGE).
    k_fil = 1 in old records without it."""
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return 1.0, 1.0
    age = time.time() - saved.get("k_time", saved.get("time", 0))
    k = saved.get("k")
    if k is None or saved.get("model") != MODEL_VERSION or age >= K_STATE_MAX_AGE:
        return 1.0, 1.0
    k = min(K_LOSS_MAX, max(K_LOSS_MIN, float(k)))
    kfil = min(K_FIL_MAX, max(K_FIL_MIN, float(saved.get("k_fil", 1.0))))
    print(f"[Ogrenilmis carpanlar devraliniyor: k={k:.3f}, k_fil={kfil:.3f} ({age/3600:.1f} saat once)]")
    if abs(k - 1.0) > K_ALARM_BAND:
        print(f"[BILGI: hotend kalibrasyona gore %{100 * abs(k - 1.0):.0f} "
              f"{'AZ' if k < 1.0 else 'FAZLA'} isi kaybediyor (kalici durum, alarm degil).]")
    return k, kfil


def load_k_ref(k):
    """Reference of the loss alarm (last accepted k level, see K_ACCEPT_TIME); the inherited k if missing/stale."""
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return k
    age = time.time() - saved.get("k_time", saved.get("time", 0))
    ref = saved.get("k_ref")
    if ref is None or saved.get("model") != MODEL_VERSION or age >= K_STATE_MAX_AGE:
        return k
    ref = min(K_LOSS_MAX, max(K_LOSS_MIN, float(ref)))
    print(f"[Kayip alarmi referansi: k_ref={ref:.3f} (alarm: k, {ref - K_ALARM_BAND:.3f}-{ref + K_ALARM_BAND:.3f} "
          f"disina cikarsa)]")
    return ref


def load_last_material():
    """Material of the last print (for the CSV until the next G-code is read); None without a record."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("malzeme")
    except (OSError, ValueError):
        return None


def backup_state():
    """Dated copy of twin_state.json at start-up; the newest STATE_BACKUPS are kept."""
    try:
        with open(STATE_FILE) as f:
            json.load(f)
    except (OSError, ValueError):
        return   # missing or corrupt: never let a bad copy rotate out the good backups [K34]
    try:
        os.makedirs(STATE_BACKUP_DIR, exist_ok=True)
        dst = os.path.join(STATE_BACKUP_DIR, time.strftime("twin_state_%Y%m%d_%H%M%S.json"))
        shutil.copyfile(STATE_FILE, dst)
        old = sorted(f for f in os.listdir(STATE_BACKUP_DIR) if f.startswith("twin_state_") and f.endswith(".json"))
        for f in old[:-STATE_BACKUPS]:
            os.remove(os.path.join(STATE_BACKUP_DIR, f))
    except OSError as e:
        print(f"[durum yedegi alinamadi: {e}]")


def save_state(T1, T2, T3, k=None, kfil=None, k_ref=None, material=None):
    data = {"time": time.time(), "model": MODEL_VERSION, "T1": T1, "T2": T2, "T3": T3}
    if k is not None:
        data["k"], data["k_time"] = k, time.time()
    if kfil is not None:
        data["k_fil"] = kfil
    if k_ref is not None:
        data["k_ref"] = k_ref
    if material is not None:
        data["malzeme"] = material
    # atomic: a power cut during an in-place write left an empty file and a crash loop (2026-10-20) [K34]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


def natural_loss(T1):
    """Loss from the block near the thermistor to air (natural convection + radiation), without k_loss."""
    T1_K = T1 + 273.15
    return HA1 * (T1 - T_AMB) + CRAD * (T1_K**4 - T_AMB_K**4)


def fan_conductance(fan):
    """Extra conductance from T2 to air by the part cooling fan (W/K), fan speed 0-1.
    Below FAN_F0 the blower cannot overcome the duct resistance, cooling ~0 (experiments 5/6)."""
    return HA2_FAN * max((fan - FAN_F0) / (1.0 - FAN_F0), 0.0) ** FAN_EXP


def steady_T2_rise(T1, fan):
    """T2 - T_amb in steady state. Solved by equating the flow from T1 to T2, (T1 - T2)/R_HB, with the flow out
    of T2, (HA2 + fan conductance)*(T2 - T_amb)."""
    return (T1 - T_AMB) / (1 + R_HB * (HA2 + fan_conductance(fan)))


class KLossLearner:
    """k_loss (loss-to-air factor) and k_fil (heat-into-the-print factor, see K_FIL_MAX) learned from the energy balance.

    STEADY HOLD windows (ADAPT_WINDOW, no extrusion) and PRINT windows (PRINT_WINDOW) are used:
        y = (energy supplied - energy stored - heatsink path loss) / duration
    k scales the losses to air (block surface + fan), not the heatsink path (HA2); so that path is subtracted
    separately. T2 is not measured, so its steady value is used.
    Stored energy is computed over the three masses together (C1 + C2 + C3, C(T) included: stored_energy).
    A window is discarded unless its T range is small and T > ADAPT_TMIN. Transitions such as the heater switching
    off, a target change or a ramp widen the T range, so they are excluded automatically.
    (The old per-sample learner took a few seconds of pwm=0 after the heater switched off as "loss=0" and pulled
    k from 0.96 to 0.86.)
    [k, k_fil] are learned together with a Kalman filter (see _kalman)."""

    def __init__(self, k=1.0, kfil=1.0):
        self.k, self.kfil = k, kfil
        self.t_hot = ADAPT_TMIN                    # printing temperature limit, set by the Twin from the target [K54]
        self.P = [[0.0, 0.0], [0.0, P0_FIL]]      # covariance of [k, k_fil] (k's comes from the floor)
        self.t_kal = None                          # last Kalman update (for process noise)
        self.windows = []                          # (time, kind, window's own k) - maintenance log, see MAINT_*
        self.reset()

    def _kalman(self, now, y, x1, x2, R, a):
        """Single-measurement Kalman update: y = k*x1 + k_fil*x2 (+ noise, variance R).
        a: at least this much gain for k (span/tau of the old exponential learner). Kalman alone trusted the first
        windows right after a heat-up (transient, biased) too much and then narrowed its gain, so it corrected k
        slowly (core 3.77 -> 4.13); k's variance is floored to the value that gives this gain in every window
        ("covariance floor")."""
        P = self.P
        if self.t_kal is not None:
            P[1][1] += Q_FIL * max(0.0, now - self.t_kal)
        self.t_kal = now
        if a < 1.0 and x1 > 0:
            P[0][0] = max(P[0][0], a * R / ((1.0 - a) * x1 * x1))
        Px = [P[0][0] * x1 + P[0][1] * x2, P[1][0] * x1 + P[1][1] * x2]
        S = x1 * Px[0] + x2 * Px[1] + R
        G = [Px[0] / S, Px[1] / S]
        e = y - (self.k * x1 + self.kfil * x2)
        self.k = min(K_LOSS_MAX, max(K_LOSS_MIN, self.k + G[0] * e))
        self.kfil = min(K_FIL_MAX, max(K_FIL_MIN, self.kfil + G[1] * e))
        self.P = [[P[i][j] - G[i] * Px[j] for j in range(2)] for i in range(2)]

    def _learn(self, now, num, E_loss, E_fil, span, tau, R):
        """num: supplied - stored - heatsink path (J); E_loss: model loss scaled by k (J);
        E_fil: filament heat computed with K_FIL (J)."""
        self._kalman(now, num / span, E_loss / span, E_fil / span, R, min(1.0, span / tau))
        if E_fil == 0 and E_loss > 0 and len(self.windows) < 10000:
            # k is the only unknown (hold or fan-off print): the window's own k, independent of the learner (maintenance)
            self.windows.append((now, "tutma" if R == R_HOLD else "fansiz_baski", num / E_loss))

    def reset(self):
        """Discard the window (data gap etc.). The learned k is kept."""
        self.t0 = None
        self.p_t0 = None

    def _start_print(self, now, T1_real):
        self.p_t0, self.p_T_first = now, T1_real
        self.p_T_min = self.p_T_max = T1_real
        self.p_E_in = self.p_E_loss = self.p_E_T2 = self.p_E_fil = self.p_E_fil_off = 0.0
        self.p_valid = self.p_ext = 0.0
        self.p_ok = True

    def update_print(self, now, dt, T1_real, pwm, fan, ext_v, energy_ok, blocked):
        """Print window (PRINT_WINDOW): filament heat is added to the energy balance of the hold.
        energy_ok: the early warning computed a valid missing power for this interval (no heat-up/target change).
        blocked: sudden power deviation (|missing power| > ENERGY_THRESHOLD or a GUC_* alarm) - the window is
        discarded, so faults such as a flow interruption are not taught to k."""
        if self.p_t0 is None:
            self._start_print(now, T1_real)
            return
        dT2 = steady_T2_rise(T1_real, fan)
        self.p_E_in += pwm * P_MAX * dt
        self.p_E_loss += (natural_loss(T1_real) + fan_conductance(fan) * dT2) * dt
        self.p_E_T2 += HA2 * dT2 * dt
        if fan_conductance(fan) > 0:       # fan on: scaled by k_fil (learned)
            self.p_E_fil += K_FIL * ext_v * (T1_real - T_AMB) * dt
        else:                              # fan off: filament heating only, factor 1.0 (fixed)
            self.p_E_fil_off += K_FIL * ext_v * (T1_real - T_AMB) * dt
        self.p_T_min, self.p_T_max = min(self.p_T_min, T1_real), max(self.p_T_max, T1_real)
        self.p_valid += dt if energy_ok else 0.0
        self.p_ext += dt if ext_v > 0.0 else 0.0
        self.p_ok = self.p_ok and T1_real > self.t_hot and not blocked
        span = now - self.p_t0
        if span >= PRINT_WINDOW:
            if (self.p_ok and self.p_T_max - self.p_T_min < PRINT_T_RANGE and self.p_E_loss > 0
                    and self.p_valid >= PRINT_VALID_FRAC * span and self.p_ext >= 0.5 * span):
                stored = stored_energy(self.p_T_first, T1_real)
                # heat into the print in the fan-off part is a fixed known term (K_FIL x 1.0) and is subtracted;
                # k_fil is learned only from the fan-on part ("A0", [K09])
                self._learn(now, self.p_E_in - stored - self.p_E_T2 - self.p_E_fil_off, self.p_E_loss,
                            self.p_E_fil, span, PRINT_TAU, R_PRINT)
            self._start_print(now, T1_real)

    def _start(self, now, T1_real, ok):
        self.t0, self.T_first = now, T1_real
        self.T_min = self.T_max = T1_real
        self.E_in = self.E_loss = self.E_T2 = 0.0
        self.ok = ok

    def update(self, now, dt, T1_real, pwm, fan, extruding):
        ok = T1_real > self.t_hot and not extruding
        if self.t0 is None:
            self._start(now, T1_real, ok)
            return self.k
        dT2 = steady_T2_rise(T1_real, fan)
        self.E_in += pwm * P_MAX * dt
        self.E_loss += (natural_loss(T1_real) + fan_conductance(fan) * dT2) * dt   # scaled by k
        self.E_T2 += HA2 * dT2 * dt                                        # not scaled
        self.T_min, self.T_max = min(self.T_min, T1_real), max(self.T_max, T1_real)
        self.ok = self.ok and ok
        if now - self.t0 >= ADAPT_WINDOW:
            if self.ok and self.T_max - self.T_min < ADAPT_T_RANGE and self.E_loss > 0:
                stored = stored_energy(self.T_first, T1_real)
                self._learn(now, self.E_in - stored - self.E_T2, self.E_loss, 0.0,
                            now - self.t0, ADAPT_TAU, R_HOLD)
            self._start(now, T1_real, ok)
        return self.k


def max_stable_substep(fan, extrude_v):
    """Largest substep for which RK4 stays stable (upper limit MAX_SUBSTEP).

    The speed of the system's fastest mode is bounded from above by the Gershgorin bound of the linearised
    Jacobian (sum of |diagonal| + |off-diagonal| per row). With the current parameters the fastest time constant is
    ~1.5 s and the stability limit a few seconds; with 4 Hz messages (dt ~0.25 s) almost every step is a single
    RK4 step. Splitting into substeps only protects against data gaps and parameter changes."""
    dL = (HA1 + 4 * CRAD * 600.0**3             # loss derivative, safe side up to 600 K
          + K_FIL_MAX * K_FIL * max(extrude_v, 0.0))
    s = cap_scale(0.0) if CAP_T_COEF > 0 else 1.0   # smallest capacity (cold node with C(T) on)
    lam = max((2 / R31 + 2 / R_HB + K_LOSS_MAX * dL) / C1,              # T1 row
              (2 / R_HB + HA2 + K_LOSS_MAX * fan_conductance(fan)) / C2,  # T2 row
              2 / (R31 * C3)) / s                                        # T3 row
    return min(MAX_SUBSTEP, RK4_STABILITY / lam)


def model_step(T1, T2, T3, pwm, fan, extrude_v, dt, k_loss=1.0, max_substep=None, k_fil=1.0):
    """Advances with RK4, split into small substeps for stability.
    k_fil: heat-into-the-print factor (filament + conduction from the nozzle into the print; learned, see K_FIL_MAX);
    multiplies K_FIL."""
    if max_substep is None:
        max_substep = max_stable_substep(fan, extrude_v)

    def deriv(T1, T2, T3):
        k_eff = k_loss   # at every temperature (the fade-out rule was removed on 2026-10-13)
        Q_filament = k_fil * K_FIL * max(extrude_v, 0.0) * (T1 - T_AMB)
        Q_conv1 = k_eff * natural_loss(T1) + Q_filament

        Q_heater_to_1 = (T3 - T1) / R31
        Q_hb = (T1 - T2) / R_HB
        dT1 = (Q_heater_to_1 - Q_hb - Q_conv1) / (C1 * cap_scale(T1))

        Q_c2 = HA2 * (T2 - T_AMB)                          # heat break -> heatsink
        Q_fan = k_eff * fan_conductance(fan) * (T2 - T_AMB)   # the part cooling fan cools the nozzle side
        dT2 = (Q_hb - Q_c2 - Q_fan) / (C2 * cap_scale(T2))

        Q_heater = pwm * P_MAX
        dT3 = (Q_heater - Q_heater_to_1) / (C3 * cap_scale(T3))

        return dT1, dT2, dT3

    n_sub = max(1, math.ceil(dt / max_substep))
    sub_dt = dt / n_sub

    for _ in range(n_sub):
        k1 = deriv(T1, T2, T3)
        k2 = deriv(T1 + sub_dt/2*k1[0], T2 + sub_dt/2*k1[1], T3 + sub_dt/2*k1[2])
        k3 = deriv(T1 + sub_dt/2*k2[0], T2 + sub_dt/2*k2[1], T3 + sub_dt/2*k2[2])
        k4 = deriv(T1 + sub_dt*k3[0], T2 + sub_dt*k3[1], T3 + sub_dt*k3[2])
        T1 = T1 + sub_dt/6*(k1[0] + 2*k2[0] + 2*k3[0] + k4[0])
        T2 = T2 + sub_dt/6*(k1[1] + 2*k2[1] + 2*k3[1] + k4[1])
        T3 = T3 + sub_dt/6*(k1[2] + 2*k2[2] + 2*k3[2] + k4[2])

    return T1, T2, T3


class FaultMonitor:
    """Turns instantaneous alarm conditions into confirmed fault events (confirmation + end delay)."""

    def __init__(self, confirm=None, clear=None, confirm_by_code=None):
        """confirm_by_code: a different confirmation time for some codes (e.g. GUC_*: ENERGY_CONFIRM)."""
        self.confirm = FAULT_CONFIRM if confirm is None else confirm
        self.clear = FAULT_CLEAR if clear is None else clear
        self.confirm_by_code = confirm_by_code or {}
        self.pending = {}     # code -> first moment the condition has been true without interruption
        self.active = {}      # code -> moment the fault started
        self.last_seen = {}   # code -> last moment the condition was true
        self.events = []      # finished faults: (code, start, end)

    def update(self, now, conditions):
        """conditions: fault codes that are true now.
        Returns: (newly started codes, finished events [(code, start, end)])."""
        started, ended = [], []
        for code in conditions:
            self.last_seen[code] = now
            if code in self.active:
                continue
            t0 = self.pending.setdefault(code, now)
            if now - t0 >= self.confirm_by_code.get(code, self.confirm):
                self.active[code] = t0
                del self.pending[code]
                started.append(code)
        for code in [c for c in self.pending if c not in conditions]:
            del self.pending[code]          # interrupted: the confirmation counter restarts
        for code in [c for c in self.active if c not in conditions]:
            if now - self.last_seen[code] >= self.clear:
                event = (code, self.active.pop(code), now)
                self.events.append(event)
                ended.append(event)
        return started, ended


def fault_conditions(k, fan, k_ref=1.0, active=None):
    """Drift alarm (KAYIP_*): has the learned k left the band around the reference (k_ref = last accepted level).
    active: hysteresis state (Twin.k_alarm); if not given, K_ALARM_BAND only. Fast faults (GUC_*) are in energy_conditions."""
    conds = set()
    on = abs(k - k_ref) > K_ALARM_BAND if active is None else active
    if on and k > k_ref:
        conds.add("KAYIP_ARTTI")
    elif on:
        conds.add("KAYIP_AZALDI_FAN" if fan > 0 else "KAYIP_AZALDI")
    return conds


class EnergyMonitor:
    """Missing power (W): loss expected by the model over the last ENERGY_WINDOW seconds - (measured power - stored).
    + = LESS heat leaving than expected, - = MORE. The unmeasured T2 (nozzle side) and T3 (cartridge) are driven by
    the REAL T1 and the measured power (T1 taken constant over each interval, exact exponential solution); stored
    energy is the sum of the three nodes. Independent of the twin's own temperature state; losses are scaled by the
    learned k and k_fil (the same "tare" as the model)."""

    def __init__(self):
        self.T2 = self.T3 = None
        self.t_hot = ENERGY_TMIN       # printing temperature limit, set by the Twin from the target [K54]
        self.reset()

    def reset(self):
        """Data gap: the window is emptied, T2/T3 are set up again at the next measurement."""
        self.T2 = self.T3 = None
        self.hist = deque()        # (interval end, dt, loss*dt, power*dt, E at interval start, T1)
        self.pwm_hist = deque()    # (interval end, dt, pwm) - heat-up (saturation) detection
        self.last_sat = -1e9
        self.last_target = -1e9    # last moment Klipper changed the target temperature
        self.gap = None

    def _energy(self, T1):
        return C1 * cap_energy(T1) + C2 * cap_energy(self.T2) + C3 * cap_energy(self.T3)

    def update(self, now, dt, T1_prev, T1_now, pwm, fan, ext, power_scale=1.0, k=1.0, kfil=1.0):
        """Elapsed interval: T1_prev the known temperature at the start of the interval, pwm/fan/ext its inputs.
        power_scale: bed voltage sag (real power = pwm * P_MAX * power_scale); saturation is detected from the
        commanded pwm. k, kfil: learned factors (as in model_step: k scales the block + fan loss, kfil the heat into
        the print; the heatsink path HA2 is not scaled)."""
        if self.T2 is None:
            _, self.T2, self.T3 = initial_state(T1_prev, pwm, fan)
        k_eff = k
        E0 = self._energy(T1_prev)
        fc = k_eff * fan_conductance(fan)
        P = pwm * P_MAX * power_scale
        loss = (k_eff * natural_loss(T1_prev) + (HA2 + fc) * (self.T2 - T_AMB)
                + kfil * K_FIL * max(ext, 0.0) * (T1_prev - T_AMB))
        G2 = 1 / R_HB + HA2 + fc
        T2_inf = (T1_prev / R_HB + (HA2 + fc) * T_AMB) / G2
        self.T2 = T2_inf + (self.T2 - T2_inf) * math.exp(-dt * G2 / (C2 * cap_scale(self.T2)))
        T3_inf = T1_prev + P * R31
        self.T3 = T3_inf + (self.T3 - T3_inf) * math.exp(-dt / (R31 * C3 * cap_scale(self.T3)))

        self.hist.append((now, dt, loss * dt, P * dt, E0, T1_now))
        while len(self.hist) > 1 and self.hist[1][0] - self.hist[1][1] <= now - ENERGY_WINDOW:
            self.hist.popleft()          # trim so that the window stays at least ENERGY_WINDOW long
        self.pwm_hist.append((now, dt, pwm))
        while self.pwm_hist and self.pwm_hist[0][0] <= now - 3.0:
            self.pwm_hist.popleft()
        span_p = sum(h[1] for h in self.pwm_hist)
        if span_p > 0 and sum(h[1] * h[2] for h in self.pwm_hist) / span_p >= 0.95:
            self.last_sat = now

        span = sum(h[1] for h in self.hist)
        Ts = [h[5] for h in self.hist]
        ok = (span >= 0.8 * ENERGY_WINDOW and T1_now > self.t_hot
              and max(Ts) - min(Ts) < ENERGY_T_RANGE and now - self.last_sat >= ENERGY_SETTLE
              and now - self.last_target >= ENERGY_SETTLE)
        # target change: the heater switches off/on at once but the temperature follows with a lag; meanwhile the
        # power balance looked like "heat is not leaving" (27.09 print start, false GUC_AZALDI)
        if not ok:
            self.gap = None
            return None
        stored = (self._energy(T1_now) - self.hist[0][4]) / span
        self.gap = (sum(h[2] for h in self.hist) - sum(h[3] for h in self.hist)) / span + stored
        return self.gap


def energy_conditions(gap):
    if gap is None:
        return set()
    if gap > ENERGY_THRESHOLD:
        return {"GUC_AZALDI"}
    if gap < -ENERGY_THRESHOLD:
        return {"GUC_ARTTI"}
    return set()


class Twin:
    """Message handler - the live script and evaluate.py use the SAME code (identical results).

    on_message() is called for every Moonraker message (with or without a temperature):
      1) the elapsed interval is advanced with the inputs valid at its START (pwm/fan/extrusion)
         (zero-order hold: a newly received value applies from this moment ON),
      2) the new inputs in the message are stored,
      3) if the message has a new temperature, the residual is computed,
      4) fault tracking: GUC_* (missing power) and KAYIP_* (k drift).
    Formerly the model advanced only on messages with a temperature: a 'heater switched on' arriving in a message
    without a temperature was applied backwards over the whole interval, and at the start of a heat-up the model
    ran ~8 C ahead (benchmark log 2026-09-25 18:48)."""

    def __init__(self, T1, T2, T3, pwm, fan, ext, k=1.0, kfil=1.0, k_ref=None):
        """k, kfil: initial loss and filament factors (in the live script from earlier sessions, see load_k).
        k_ref: reference of the loss alarm (see load_k_ref, K_ACCEPT_TIME); k if not given."""
        self.T1_model, self.T2_model, self.T3_model = T1, T2, T3
        self.pwm, self.fan = pwm or 0.0, fan or 0.0
        self.ext = ext if ext is not None and abs(ext) > EXT_EPS else 0.0
        self.flow = self.ext               # flow fed to the model (from the pos_e difference; else instantaneous speed)
        self.last_e = None                 # (time, pos_e) - last position message
        self.T1_real = T1                  # last known real temperature
        self.prev_time = None
        self.ext_history = deque()         # (interval end, interval length, speed) - net flow average
        self.learner = KLossLearner(k, kfil)
        self.k_ref = k if k_ref is None else k_ref   # reference of the loss alarm, see K_ACCEPT_TIME
        self.kst_lo = self.kst_hi = k               # acceptance: range of k in the latest steady window
        self.kst_t = 0.0                            # hot time spent in this window (s)
        self.kout_t = 0.0                           # hot time the loss alarm has been on (s)
        self.k_alarm = False                        # loss alarm state (with hysteresis, see K_ALARM_CLEAR)
        self.faults = FaultMonitor(confirm_by_code={c: ENERGY_CONFIRM for c in ENERGY_TEXT})
        self.energy = EnergyMonitor()      # does not touch the twin's computation
        self.energy_error = None
        self.target = None                 # Klipper target temperature (logging + missing-power settle time only)
        self.bed = 0.0                     # bed heater pwm (0-1); taken as off if unknown
        self.material = None               # material of the print (G-code header), logging only
        self.print_state = None            # Klipper print_stats.state of the previous message (None = unknown)
        self.pause_flagged = False         # pause already requested for this GUC_AZALDI event (PAUSE_AFTER)

    def anchor_kfil(self):
        """k_fil back to K_FIL_ANCHOR with the start uncertainty (P0_FIL); k is not touched (a hotend property)."""
        self.learner.kfil = min(K_FIL_MAX, max(K_FIL_MIN, K_FIL_ANCHOR))
        P = self.learner.P
        P[1][1], P[0][1], P[1][0] = P0_FIL, 0.0, 0.0
        self.learner.t_kal = None          # idle time before the print adds no process noise on top of P0_FIL [K55]

    def set_material(self, name):
        """The print's material changed (read from the G-code shortly after the print starts): k_fil restarts from
        the anchor, like at a print start."""
        self.anchor_kfil()
        self.material = name

    def on_message(self, now, T1_real=None, pwm=None, fan=None, ext=None, target=None, bed=None, pos_e=None,
                   print_state=None):
        """Process one message. Fields not in the message are given as None.
        pos_e: extruder position (mm, last known value); if present, the flow is computed from it (see POS_E_MAX_RATE).
        print_state: Klipper print_stats.state; entering 'printing' (not from 'paused') is a new print -> k_fil
        restarts from the anchor (K_FIL_ANCHOR).
        Returns: dt, yeni_sicaklik (new temperature), residual (None without a temperature), anomali (an official
        fault is active), k_anomali (learned loss factor outside its band), senkron, eksik_guc (W, None if invalid),
        ariza_basladi (newly confirmed fault codes), ariza_bitti ([(code, start, end)])."""
        # 0) new print: k_fil is a property of the print, not carried over from the previous one [K27]
        if print_state is not None:
            if (print_state == "printing" and self.print_state is not None
                    and self.print_state not in ("printing", "paused")):
                self.anchor_kfil()
            self.print_state = print_state
        out = {"dt": 0.0, "new_temperature": T1_real is not None,
               "residual": None, "anomali": False, "k_anomali": False, "resynced": False,
               "faults_started": [], "faults_ended": [], "eksik_guc": None,
               "k_accepted": None,   # (old reference, new k): a permanent change was accepted (see K_ACCEPT_TIME)
               "duraklat": False}  # GUC_AZALDI has lasted PAUSE_AFTER s without interruption (once per event)

        # 1) advance the elapsed interval with the inputs valid at its start
        if self.prev_time is not None:
            dt = now - self.prev_time
            out["dt"] = dt
            # net filament flow: time-weighted average of the signed speed over the last EXT_AVG_WINDOW seconds
            # (retract / unretract cancel), then clipped below 0
            self.ext_history.append((now, dt, self.flow))
            while self.ext_history and self.ext_history[0][0] < now - EXT_AVG_WINDOW:
                self.ext_history.popleft()
            span = sum(h[1] for h in self.ext_history)
            ext_net = sum(h[1] * h[2] for h in self.ext_history) / span if span > 0 else self.flow
            ext_smoothed = max(ext_net, 0.0)
            T_now = T1_real if T1_real is not None else self.T1_real
            if dt > GAP_THRESHOLD or dt < 0:
                # dt < 0: the clock went backwards (Klipper/Pi may have restarted during a reconnect;
                # a negative step blew up the model). In both cases it is unknown what happened in between.
                # long data gap: unknown what happened in between -> set up ALL nodes consistently as on a
                # restart (equalising only T1 left T2/T3 at old values and produced a fake transient right after)
                self.T1_model, self.T2_model, self.T3_model = initial_state(
                    T_now, pwm if pwm is not None else self.pwm,
                    fan if fan is not None else self.fan)
                self.learner.reset()
                if self.energy is not None:
                    self.energy.reset()
                out["resynced"] = True
            else:
                # with the bed on the supply voltage sags: the real power to the cartridge drops slightly
                scale = 1.0 - BED_SAG * self.bed
                # heat into the print: filament only (1.0) while the fan is not cooling, the learned k_fil with the fan on
                kfil_now = self.learner.kfil if fan_conductance(self.fan) > 0 else 1.0
                self.T1_model, self.T2_model, self.T3_model = model_step(
                    self.T1_model, self.T2_model, self.T3_model,
                    self.pwm * scale, self.fan, ext_smoothed, dt, self.learner.k, k_fil=kfil_now)
                hot = hot_threshold(self.target)
                self.learner.t_hot = hot
                if self.energy is not None:
                    self.energy.t_hot = hot
                self.learner.update(now, dt, T_now, self.pwm * scale, self.fan, ext_smoothed > 0.0)
                if T_now > hot:      # acceptance counters advance only in hot operation, where k is meaningful
                    k = self.learner.k
                    lo, hi = min(self.kst_lo, k), max(self.kst_hi, k)
                    if hi - lo > K_ACCEPT_RANGE:            # k is still changing: restart the steadiness window
                        self.kst_lo = self.kst_hi = k
                        self.kst_t = 0.0
                    else:
                        self.kst_lo, self.kst_hi = lo, hi
                        self.kst_t += dt
                    self.kout_t = self.kout_t + dt if self.k_alarm else 0.0
                    if self.kout_t >= K_ACCEPT_TIME and self.kst_t >= K_ACCEPT_TIME:
                        out["k_accepted"] = (self.k_ref, k)
                        self.k_ref = k
                        self.kout_t = 0.0
                        self.k_alarm = False
                if self.energy is not None:
                    # on an error only the missing power switches off; the twin and monitoring continue
                    try:
                        out["eksik_guc"] = self.energy.update(now, dt, self.T1_real, T_now,
                                                              self.pwm, self.fan, ext_smoothed, scale,
                                                              self.learner.k, kfil_now)
                    except Exception as e:  # noqa: BLE001
                        self.energy, self.energy_error = None, f"{type(e).__name__}: {e}"
                if self.energy is not None:
                    g = out["eksik_guc"]
                    power_alarms = [c for c in ENERGY_TEXT if c in self.faults.active or c in self.faults.pending]
                    blocked = (g is not None and abs(g) > ENERGY_THRESHOLD) or bool(power_alarms)
                    self.learner.update_print(now, dt, T_now, self.pwm * scale, self.fan, ext_smoothed,
                                              g is not None, blocked)
        self.prev_time = now

        # 2) the new inputs in the message apply from now on
        if target is not None:
            # the target temperature does not enter the model (the model runs on the measured power); it is only
            # tracked so that the early warning waits ENERGY_SETTLE seconds after a target change
            if self.target is not None and abs(target - self.target) > 0.5 and self.energy is not None:
                self.energy.last_target = now
            self.target = target
        if bed is not None:
            self.bed = bed
        if pwm is not None:
            self.pwm = pwm
        if fan is not None:
            self.fan = fan
        if ext is not None:
            self.ext = ext if abs(ext) > EXT_EPS else 0.0
        # flow: the pos_e difference between the last two messages applies to the next interval (~0.25 s lag, causal)
        if pos_e is not None:
            if self.last_e is not None:
                dt_e = now - self.last_e[0]
                v = (pos_e - self.last_e[1]) / dt_e if 1e-3 < dt_e <= GAP_THRESHOLD else 0.0
                self.flow = v if abs(v) <= POS_E_MAX_RATE else 0.0
            self.last_e = (now, pos_e)
        elif self.last_e is None:
            self.flow = self.ext

        # 3) compare if a new temperature arrived
        if T1_real is not None:
            self.T1_real = T1_real
            out["residual"] = 0.0 if out["resynced"] else T1_real - self.T1_model
        dev = abs(self.learner.k - self.k_ref)
        self.k_alarm = dev > (K_ALARM_CLEAR if self.k_alarm else K_ALARM_BAND)   # hysteresis: no toggling at the band edge
        out["k_anomali"] = self.k_alarm

        # 4) fault tracking - only on messages with a new temperature (conditions are based on measurements)
        if T1_real is not None:
            conds = fault_conditions(self.learner.k, self.fan, self.k_ref, self.k_alarm)
            if not out["resynced"]:
                conds |= energy_conditions(out["eksik_guc"])
            out["faults_started"], out["faults_ended"] = self.faults.update(now, conds)
            t_az = self.faults.active.get("GUC_AZALDI")
            if t_az is None:
                self.pause_flagged = False
            elif not self.pause_flagged and now - t_az >= PAUSE_AFTER:
                self.pause_flagged = out["duraklat"] = True
        out["anomali"] = bool(self.faults.active)   # CSV 'anomali' = an official alarm is active (Unity status pill)
        return out


LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "twin.lock")


def single_instance_lock():
    """Only one twin at a time: operating-system file lock. A second copy cannot get the lock -> None.
    However the process ends (Ctrl+C, closing the window, crash) the OS releases the lock.
    (2026-09-28: the old and the new script ran at the same time and wrote to the same CSV.)"""
    fh = open(LOCK_FILE, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def source_version():
    """MODEL_VERSION + a short hash of the twin's own source files: identifies the exact code behind a log
    (MODEL_VERSION alone is not bumped on every change). Content hash, so it needs no git (Pi deployment)."""
    import hashlib
    h = hashlib.sha1()
    for name in ("realtime_websocket.py", "bed_model.py", "profile.toml", "calibration.json"):   # numbers too [K52]
        try:
            with open(os.path.join(HERE, name), "rb") as fh:
                h.update(fh.read().replace(b"\r\n", b"\n"))   # same hash for a CRLF and an LF checkout
        except OSError:
            pass
    return f"{MODEL_VERSION}+{h.hexdigest()[:8]}"


CODE_VERSION = source_version()

# One row per message. T_yeni=1: the message has a new temperature (residual on these rows).
# T_yeni=0: only pwm/fan changed; T1_real is the last known value, residual empty.
# anomali = an official alarm is active (since 2026-10-12; before that a raw residual threshold).
# Every message is logged, so evaluate.py replays the live run exactly.
# code_version: only on the first row of each file (see source_version).
LOG_FIELDS = [
    "time", "T1_real", "T1_model", "residual", "pwm",
    "fan_state", "extrude_velocity", "anomali", "k_loss", "eventtime", "T_yeni", "k_anomali",
    "ariza", "eksik_guc", "target", "bed_T", "bed_pwm", "bed_target",
    "head_v", "pos_x", "pos_y", "pos_z", "pos_e", "bed_model", "bed_res", "bed_k", "k_fil",
    "print_state", "z_offset", "z_endstop", "malzeme", "duraklat", "code_version"
]
ROTATE_MIN_ROWS = 240       # a log part shorter than this (~1 min at 4 Hz) is not split off at a print start
IDLE_LOG_INTERVAL = 1.0     # s - while idle, at most one CSV row per interval; the twin still processes every message.
                            # 0: a row for every message. Idle nights were ~43 MB, 24/7 filled the card in ~50 days [K38]
IDLE_T_MAX = 40.0           # C - idle only while nozzle and bed are both below this


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def idle_row(row):
    """Nothing happening: both heaters off (no target, no power) and cold, no print, head still [K38]."""
    return (_num(row["target"]) <= 0 and _num(row["bed_target"]) <= 0
            and _num(row["pwm"]) <= 0 and _num(row["bed_pwm"]) <= 0
            and _num(row["T1_real"]) < IDLE_T_MAX and _num(row["bed_T"]) < IDLE_T_MAX
            and row["print_state"] not in ("printing", "paused") and _num(row["head_v"]) == 0)


def archive_log():
    """Moves a non-empty LOG_FILE to realtime_log_ws_onceki_<last write>.csv (the name Unity lists); a second
    part in the same minute gets seconds in its name so nothing is overwritten. Returns the new path or None."""
    if not (os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 0):
        return None
    with open(LOG_FILE, newline="") as f:   # header only (a start that crashed before any data): nothing to keep,
        f.readline()                        # the next LogFile overwrites it [K35]
        if not f.readline():
            return None
    mt = time.localtime(os.path.getmtime(LOG_FILE))
    old = os.path.join(os.path.dirname(LOG_FILE), f"realtime_log_ws_onceki_{time.strftime('%Y%m%d_%H%M', mt)}.csv")
    if os.path.exists(old):
        old = old[:-4] + time.strftime("%S", mt) + ".csv"
    try:
        os.replace(LOG_FILE, old)
        print(f"[onceki log korundu: {os.path.basename(old)}]")
        return old
    except OSError as e:
        print(f"[onceki log tasinamadi ({e}); ustune yazilacak]")
        return None


class LogFile:
    """The live CSV. rotate() closes it, archives it and starts a new file (one log per print)."""

    def __init__(self):
        self._open()

    def _open(self):
        self.fh = open(LOG_FILE, "w", newline="")
        self.writer = csv.DictWriter(self.fh, fieldnames=LOG_FIELDS)
        self.writer.writeheader()
        self.rows = 0
        self.last_t, self.last_key = None, None

    def skip_idle(self, row, t):
        """True if this row may be left out (idle thinning, K38): idle, less than IDLE_LOG_INTERVAL after the last
        written row (t: the twin's time) and the alarms / print state unchanged. Any activity is written at once."""
        return (IDLE_LOG_INTERVAL > 0 and self.rows > 0 and idle_row(row)
                and (row["ariza"], row["print_state"]) == self.last_key
                and self.last_t is not None and 0 <= t - self.last_t < IDLE_LOG_INTERVAL)

    def write(self, row, t=None):
        if self.rows == 0:
            row = dict(row, code_version=CODE_VERSION)
        self.writer.writerow(row)
        self.rows += 1
        self.last_t, self.last_key = t, (row["ariza"], row["print_state"])

    def flush(self):
        self.fh.flush()

    def rotate(self):
        """Archive the current part and start a new file. Returns the archived path (None if too short to split)."""
        if self.rows < ROTATE_MIN_ROWS:
            return None
        self.fh.close()
        old = archive_log()
        self._open()
        return old

    def close_archive(self):
        """At exit: close and archive the last part, so the session summary names its final file [K35].
        Returns the archived path (None if empty or the move failed)."""
        self.fh.close()
        return archive_log()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fh.close()


def summarize_session(path):
    """Session-to-session trend: summary of one log part to twin_gecmis.csv (see session_history.py).
    Returns a console line or None; never raises (side feature)."""
    try:
        import sys
        import session_history
        row = session_history.summarize(path, sys.modules[__name__])
        if not row:
            return None
        session_history.append(row)
        vals = [f"{k}={row[k]}" for k in ("P_fan0_W", "blok_kaybi_pct", "fan_sogutma_pct", "baski_kaybi_pct")
                if k in row]
        return (f"Oturum ozeti ({os.path.basename(path)}) -> {os.path.basename(session_history.HISTORY_FILE)}: "
                f"{', '.join(vals) if vals else '245C tutma verisi yetersiz, olcut yok'}"
                f"  (trend: python session_history.py)")
    except Exception as e:  # noqa: BLE001 - the summary is a side feature
        return f"[oturum ozeti yazilamadi: {type(e).__name__}: {e}]"


def daily_jobs(live):
    """At start-up and once a day (24/7 operation): dated backup of twin_state.json and the maintenance indicator."""
    backup_state()
    advice = maint_check()
    live["bakim"] = advice or ""
    if advice:
        print(f"\n!! {advice}\n")
        try:
            with open(REPORT_FILE, "a", encoding="utf-8") as rf:
                rf.write(f"\n##### {time.strftime('%Y-%m-%d %H:%M:%S')}  BAKIM  {advice}\n")
        except OSError:
            pass
    else:
        print(f"[bakim gostergesi: oneri yok (fansiz baski k, temiz referans {MAINT_REF:.2f} +-{MAINT_BAND:.2f})]")


def classify_job(rows, t_confirm, codes, delay):
    """Background thread: classifies the fault from the latest rows. Returns: a console line.
    The full report is appended to REPORT_FILE. On an error the live monitoring is not affected, it is only logged."""
    try:
        import sys
        import fault_classifier   # late import: pandas/scipy are loaded only when a fault happens
        summary, report = fault_classifier.classify_live(rows, sys.modules[__name__], t_confirm)
    except Exception as e:  # noqa: BLE001 - classification is a side feature, it must not stop monitoring
        return f"[ariza siniflandirma +{delay:.0f} s ({codes}) basarisiz: {type(e).__name__}: {e}]"
    with open(REPORT_FILE, "a", encoding="utf-8") as rf:
        rf.write(f"\n##### {time.strftime('%Y-%m-%d %H:%M:%S')}  {codes}  (onaydan +{delay:.0f} s)")
        rf.write(report + "\n")
    return f"\n>>>>> ARIZA KATEGORISI ({codes}, onaydan +{delay:.0f} s): {summary}\n      (ayrinti: {REPORT_FILE})\n"


def _download(url, path):
    import shutil
    import urllib.request
    tmp = path + ".part"
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as r, open(tmp, "wb") as out:
        shutil.copyfileobj(r, out)
    os.replace(tmp, path)   # a half-downloaded file never keeps the real name


def update_zoff(zoff, status):
    """Z offset record (from the fields in the message). z: babystep / SET_GCODE_OFFSET Z (mm, + = nozzle further from
    the bed); pos_z (toolhead position) already contains it. endstop: saved stepper_z position_endstop (changes with
    Z_OFFSET_APPLY_ENDSTOP + SAVE_CONFIG; NOT visible in pos_z, + = nozzle closer to the bed)."""
    ho = status.get("gcode_move", {}).get("homing_origin")
    if isinstance(ho, (list, tuple)) and len(ho) >= 3:
        zoff["z"] = ho[2]
    pe = status.get("configfile", {}).get("settings", {}).get("stepper_z", {}).get("position_endstop")
    if pe is not None:
        zoff["endstop"] = pe


def request_pause():
    """Background: pause request to Moonraker (PAUSE_MODE == 'aktif'). Returns: a console line."""
    import urllib.request
    try:
        req = urllib.request.Request(f"{HTTP_URL}/printer/print/pause", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as r:
            return f"[duraklatma istegi gonderildi: HTTP {r.status}]"
    except Exception as e:  # noqa: BLE001
        return f"[duraklatma istegi BASARISIZ: {type(e).__name__}: {e}]"


def gcode_material(path):
    """'; filament_type = X' from the settings block of an Orca/Prusa G-code (block at the end of the file; else None)."""
    found = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("; filament_type ="):
                found = line.split("=", 1)[1].strip().split(";")[0].strip() or None
    return found


def plan_job(filename, t0, init):
    """Background thread: at print start downloads the G-code + printer.cfg and produces the twin's plan.
    init: the state measured at print start (initial condition of the simulation).
    Returns: (console line, material or None). On an error the live monitoring is not affected, it is only logged."""
    material = None
    try:
        import urllib.parse
        os.makedirs(GCODE_DIR, exist_ok=True)
        base = os.path.basename(filename)
        gpath = os.path.join(GCODE_DIR, base)
        _download(f"{HTTP_URL}/server/files/gcodes/{urllib.parse.quote(filename)}", gpath)
        material = gcode_material(gpath)
        import gcode_plan   # late import: pandas is loaded only when a print starts
        cfg, cfg_src = os.path.join(GCODE_DIR, "printer.cfg"), "Pi"
        try:
            _download(f"{HTTP_URL}/server/files/config/printer.cfg", cfg)
        except OSError:
            cfg, cfg_src = CFG_FALLBACK, "yerel kopya"
        stamp = time.strftime("%Y%m%d_%H%M", time.localtime(t0))
        out = os.path.join(PLAN_DIR, f"tahmin_canli_{stamp}_{os.path.splitext(base)[0]}.csv")
        summary = gcode_plan.plan_for_live(gpath, cfg, t0, out, **init)
    except Exception as e:  # noqa: BLE001 - the plan is a side feature, it must not stop monitoring
        return f"[G-code plani uretilemedi: {type(e).__name__}: {e} - izleme devam ediyor]", material
    return f"[G-code plani hazir: {summary}; printer.cfg: {cfg_src}]", material


def show_category(text, live):
    """Classification result: print to the console, put the short form on the phone page."""
    print(text)
    first = next((ln for ln in text.splitlines() if "KATEGORISI" in ln or "basarisiz" in ln), "").strip()
    live["category"] = first.split("): ", 1)[-1] if "): " in first else first


WEB_PAGE = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Hotend ikizi</title>
<style>
:root{--bg:#fafaf8;--card:#fff;--tx:#1b1b1a;--mu:#6d6c67;--gr:#e3e2dc;--ok:#1f8a4c;--wa:#b86e00;--er:#c23b3b;--ln1:#2a78d6;--ln2:#eb6834}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--card:#201f1e;--tx:#efeee9;--mu:#9b9a93;--gr:#34332f;--ln1:#5aa0f0;--ln2:#f08050}}
body{margin:0;padding:12px 14px;background:var(--bg);color:var(--tx);font:15px/1.4 system-ui,sans-serif}
.g{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.c{background:var(--card);border-radius:10px;padding:10px 12px}
.l{font-size:12px;color:var(--mu)}.v{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
#st{margin:10px 0;padding:10px 12px;border-radius:10px;font-weight:600}canvas{width:100%;height:190px;display:block}
.s{font-size:12px;color:var(--mu);margin-top:8px}</style></head><body>
<div id="st">baglaniyor...</div>
<div class="g"><div class="c"><div class="l">gercek T1</div><div class="v" id="T">-</div></div>
<div class="c"><div class="l">model T1</div><div class="v" id="M">-</div></div>
<div class="c"><div class="l">residual</div><div class="v" id="R">-</div></div>
<div class="c"><div class="l">eksik guc</div><div class="v" id="G">-</div></div>
<div class="c"><div class="l">isitici / fan</div><div class="v" id="P" style="font-size:18px">-</div></div>
<div class="c"><div class="l">k / akis</div><div class="v" id="K" style="font-size:18px">-</div></div>
<div class="c" style="grid-column:1/-1"><div class="l">tabla (sicaklik / hedef, guc)</div><div class="v" id="B" style="font-size:18px">-</div></div></div>
<div class="c" style="margin-top:8px"><div class="l">residual (C, mavi) ve eksik guc x4 (W, turuncu) - son 15 dk</div><canvas id="ch"></canvas></div>
<div class="s" id="kat"></div><div class="s" id="up"></div>
<script>
const $=i=>document.getElementById(i),css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
function f(x,d,u){return x==null?'-':(x>0&&u!=='C '?'':'')+x.toFixed(d)+u}
function draw(h){const c=$('ch'),w=c.clientWidth,H=190,r=devicePixelRatio||1;c.width=w*r;c.height=H*r;const x=c.getContext('2d');x.scale(r,r);
if(h.length<2)return;const t0=h[0][0],t1=h[h.length-1][0],Y=v=>H/2-v*(H/2-8)/15,X=t=>(t-t0)/(t1-t0||1)*(w-4)+2;
x.strokeStyle=css('--gr');x.lineWidth=1;for(const v of[-12,-5,0,5,12]){x.beginPath();x.moveTo(0,Y(v));x.lineTo(w,Y(v));x.stroke();x.fillStyle=css('--mu');x.font='10px system-ui';x.fillText(v,2,Y(v)-2)}
for(const[k,col,s]of[[1,'--ln1',1],[2,'--ln2',4]]){x.strokeStyle=css(col);x.lineWidth=2;x.beginPath();let m=0;
for(const p of h){if(p[k]==null){m=0;continue}const yy=Y(Math.max(-15,Math.min(15,p[k]*s)));m?x.lineTo(X(p[0]),yy):x.moveTo(X(p[0]),yy);m=1}x.stroke()}}
async function tick(){try{const d=await(await fetch('/durum',{cache:'no-store'})).json();
$('T').textContent=f(d.T,1,' C');$('M').textContent=f(d.M,1,' C');$('R').textContent=(d.R>0?'+':'')+f(d.R,1,' C');
$('G').textContent=d.G==null?'-':(d.G>0?'+':'')+d.G.toFixed(2)+' W';$('P').textContent=Math.round(d.pwm*100)+'% / '+Math.round(d.fan*100)+'%';
$('K').textContent=d.k.toFixed(3)+' / '+d.ext.toFixed(1);
$('B').textContent=d.bT==null?'-':d.bT.toFixed(1)+' / '+Math.round(d.bTg||0)+' C,  %'+Math.round(100*(d.bP||0))
 +(d.bM==null?'':'  | model '+d.bM.toFixed(1)+' ('+(d.bT-d.bM>0?'+':'')+(d.bT-d.bM).toFixed(1)+'), cam ~'+d.bC.toFixed(0)+' C');
const st=$('st');let msg='normal',col='--ok';
if(d.ariza.length){msg='ARIZA: '+d.ariza.join(', ');col='--er'}
if(d.yas>15){msg='veri gelmiyor ('+Math.round(d.yas)+' s)';col='--wa'}
st.textContent=msg;st.style.background=css(col);st.style.color='#fff';
$('kat').textContent=(d.bakim?d.bakim+'  ':'')+(d.kategori?('son kategori: '+d.kategori):'');$('up').textContent='son veri '+Math.round(d.yas)+' s once';draw(d.h)}
catch(e){$('st').textContent='script calismiyor / baglanti yok';$('st').style.background=css('--er')}}
tick();setInterval(tick,2000);
</script></body></html>"""


async def web_server(live):
    """Phone monitoring page. live: the state dict updated by main (read only)."""
    async def handle(reader, writer):
        try:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode(errors="ignore")
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass
            path = line.split(" ")[1] if len(line.split(" ")) > 1 else "/"
            if path.startswith("/durum"):
                snap = dict(live)
                snap["h"] = list(live["h"])
                snap["yas"] = time.time() - live["wall"] if live["wall"] else 1e9
                body, ctype = json.dumps(snap, default=float).encode(), "application/json"
            else:
                body, ctype = WEB_PAGE.encode(), "text/html; charset=utf-8"
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                         f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except Exception:  # noqa: BLE001 - a page error does not affect monitoring
            pass
        finally:
            writer.close()

    try:
        server = await asyncio.start_server(handle, "0.0.0.0", WEB_PORT)
    except OSError as e:
        print(f"[telefon sayfasi acilamadi ({e}) - izleme sayfasiz devam ediyor]")
        return None
    try:
        import socket
        ip = "127.0.0.1"
        # UDP connect sends no packet; it only finds which network interface is used. PRINTER_HOST may carry a port,
        # and on the Pi it is 127.0.0.1 (loopback): then ask for the interface towards the LAN instead.
        for target in (PRINTER_HOST.split(":")[0], "10.255.255.255"):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((target, 80))
            ip = s.getsockname()[0]; s.close()
            if not ip.startswith("127."):
                break
    except OSError:
        ip = "<bilgisayar-IP>"
    print(f"[telefondan izleme: http://{ip}:{WEB_PORT}  (ayni Wi-Fi/ag)]")
    return server


async def main():
    if not PRINTER_HOST:
        print("\n!! Printer address not set.\n"
              "   Copy config.example.toml to config.toml and set printer_host to your\n"
              "   Moonraker address (IP or hostname), then start again.\n")
        return
    lock = single_instance_lock()
    if lock is None:
        print("\n!! Baska bir ikiz zaten calisiyor (ayni CSV'ye iki kopya yazardi).\n"
              "   Once onu kapat (o pencerede Ctrl+C), sonra tekrar baslat.\n")
        return
    twin = None   # set up once the first real temperature is known
    live = {"T": None, "M": None, "R": None, "G": None, "pwm": 0.0, "fan": 0.0, "k": 1.0, "ext": 0.0,
            "ariza": [], "category": "", "wall": 0.0, "h": deque(),
            "bT": None, "bTg": None, "bP": None, "bM": None, "bC": None}   # phone page
    web = await web_server(live) if WEB_PORT else None
    buffer = deque()      # (model time, csv row) - the last CLASSIFY_BUFFER seconds for classification
    scheduled = []        # [run time, confirmation time, codes, delay]
    loop = asyncio.get_running_loop()
    last_known = {"pwm": 0.0, "fan": 0.0, "ext": 0.0}   # inputs that arrive before the twin is set up
    bed = {}                                             # bed: last known temperature/power/target (logging)
    motion = {}                                          # head speed, Y and Z position (logging)
    pstat = {}                                           # print_stats: print state and file (input)
    gmove = {}                                           # gcode_move: extrusion mode (plan input)
    zoff = {}                                            # Z offset: babystep + saved endstop (logging)
    bed_twin = None                                     # bed twin (bed_model.BedTwin, observation)
    bed_error = None
    mat_last = load_last_material()                      # material of the last print (CSV until the next G-code)
    daily_jobs(live)                                     # state backup + maintenance indicator (again every day)
    print(f"[kod surumu: {CODE_VERSION}]")
    print(f"[yazici sayilari: {', '.join(PRINTER_SOURCES) if PRINTER_SOURCES else 'kod varsayilanlari'}]")
    for note in PRINTER_NOTES:
        print(f"[!! {note}]")

    def on_plan(fut):
        """Plan job finished (runs in the event loop): console line + the print's material (k_fil restarts from the
        anchor if it changed)."""
        nonlocal mat_last
        msg, mat = fut.result()
        print(msg)
        if not mat:
            return
        if twin is None:
            mat_last = mat
            return
        if mat != twin.material:
            twin.set_material(mat)
            print(f"[malzeme: {mat} - k_fil capadan ({K_FIL_ANCHOR:.2f}) ogreniliyor]")

    # if the previous session's log was left without renaming, do not overwrite it: move it to a dated name
    archive_log()
    log = LogFile()
    today = time.strftime("%Y-%m-%d")       # daily jobs run again when the date changes (24/7 operation)

    def on_summary(fut):
        msg = fut.result()
        if msg:
            print(msg)

    with log:
        # Model time: Klipper's eventtime (the moment the values were MEASURED). The moment the message reaches
        # the PC (time.time()) varies by 0.03-0.56 s with network/Moonraker latency; the pwm*dt integral turned
        # this jitter into +-8 C fake oscillations at 245 C.
        # The subscription response may lack eventtime; the decision is made on the first notification and the
        # model clock starts from that notification (the two clocks are never mixed).
        use_klipper_clock = None
        last_save = last_flush = 0.0   # disk writes are thinned out by the PC clock
        energy_error_shown = False
        try:
            while True:
                # If the network congests (e.g. a video stream on the same Wi-Fi) the websocket may drop: the
                # script does not exit, it reconnects after RECONNECT_DELAY and continues the same CSV.
                # If the gap is longer than GAP_THRESHOLD, the twin re-seats on the real temperature.
                try:
                    print("Websocket baglaniliyor...")
                    async with websockets.connect(WS_URL, ping_interval=PING_INTERVAL,
                                                  ping_timeout=PING_TIMEOUT) as ws:
                        print("Baglandi, abone olunuyor...")

                        subscribe_msg = {
                            "jsonrpc": "2.0",
                            "method": "printer.objects.subscribe",
                            "params": {
                                "objects": {
                                    "extruder": ["temperature", "power", "target"],
                                    "heater_bed": ["temperature", "power", "target"],
                                    "fan": ["speed"],
                                    "motion_report": ["live_extruder_velocity", "live_velocity", "live_position"],
                                    # print state and file: INPUT of the twin's G-code plan (the plan is not read from Klipper)
                                    "print_stats": ["state", "filename"],
                                    # absolute_extrude: extrusion mode (M82/M83); Klipper does not reset it between prints,
                                    # initial condition of the plan. homing_origin: Z offset (logging only, 2026-10-14): the
                                    # heat the first layer needs varies 0.4-0.9 W from print to print; squish/nozzle-bed gap
                                    # is a candidate. homing_origin[2] = babystep (SET_GCODE_OFFSET Z). One key: a second
                                    # "gcode_move" entry silently replaced the first, absolute_extrude was never subscribed.
                                    "gcode_move": ["absolute_extrude", "homing_origin"],
                                    # position_endstop = saved Z zero (only sent when connecting)
                                    "configfile": ["settings"],
                                }
                            },
                            "id": 1,
                        }
                        await ws.send(json.dumps(subscribe_msg))

                        # the subscription response (id=1) holds a full snapshot of the current state. While printing,
                        # Moonraker's notifications may arrive before the response: skip them until it comes
                        # (2026-10-11: on a restart mid-print the first message was a notification; target temperatures
                        # and print state come only when they change, so they stayed empty for the whole session)
                        while True:
                            first_response = json.loads(await ws.recv())
                            if first_response.get("id") == 1:
                                break
                        # bed: notifications only carry the changed fields; initial values come from here
                        for key, val in first_response.get("result", {}).get("status", {}).get("heater_bed", {}).items():
                            if val is not None:
                                bed[key] = val
                        # the position too: while the printer is idle live_position does not change and never comes.
                        # Without the initial value from here pos_* stay empty until the first move (2026-10-11: pos_e
                        # was unknown at plan start, so Unity could not compute the progress)
                        mr0 = first_response.get("result", {}).get("status", {}).get("motion_report", {})
                        if mr0.get("live_velocity") is not None:
                            motion["v"] = mr0["live_velocity"]
                        pos0 = mr0.get("live_position")
                        if isinstance(pos0, (list, tuple)) and len(pos0) >= 4:
                            motion["x"], motion["y"], motion["z"], motion["e"] = pos0[0], pos0[1], pos0[2], pos0[3]
                        # print state: if the script (or a reconnect) started mid-print we did not see the start
                        # -> no plan is produced for that print (the plan is aligned to the print start)
                        was = pstat.get("state")
                        for key, val in first_response.get("result", {}).get("status", {}).get("print_stats", {}).items():
                            if val is not None:
                                pstat[key] = val
                        for key, val in first_response.get("result", {}).get("status", {}).get("gcode_move", {}).items():
                            if val is not None:
                                gmove[key] = val
                        st1 = first_response.get("result", {}).get("status", {})
                        update_zoff(zoff, st1)
                        if pstat.get("state") in ("printing", "paused") and was not in ("printing", "paused"):
                            print(f"[baski zaten suruyor: {pstat.get('filename', '?')} - baslangicini "
                                  f"gormedigimiz icin bu baski icin G-code plani uretilmeyecek]")
                        print("Ilk cevap alindi, izleme basliyor. Durdurmak icin Ctrl+C.\n")

                        if twin is None:     # the twin is kept across reconnects
                            try:
                                initial_status = first_response["result"]["status"]
                                ex0 = initial_status["extruder"]
                                pwm0 = ex0.get("power", 0.0) or 0.0
                                fan0 = initial_status.get("fan", {}).get("speed", 0.0) or 0.0
                                ext0 = initial_status.get("motion_report", {}).get("live_extruder_velocity", 0.0) or 0.0
                                k0, kf0 = load_k()
                                twin = Twin(*load_state(ex0["temperature"], pwm0, fan0), pwm0, fan0, ext0,
                                            k0, kf0, load_k_ref(k0))
                                twin.material = mat_last
                                # not in notifications unless it changes: initial value from here
                                twin.target = ex0.get("target")
                            except (KeyError, TypeError):
                                print("Uyari: ilk durum ayristirilamadi, ilk gercek veri mesajinda baslatilacak.")
                        else:
                            # reconnect: refresh inputs that may have changed during the outage from the snapshot
                            # (Moonraker sends only CHANGED fields in notifications; if the fan changed during the
                            # outage the old value would stay until its next change)
                            st0 = first_response.get("result", {}).get("status", {})
                            for key, val in (("pwm", st0.get("extruder", {}).get("power")),
                                             ("fan", st0.get("fan", {}).get("speed")),
                                             ("ext", st0.get("motion_report", {}).get("live_extruder_velocity")),
                                             ("target", st0.get("extruder", {}).get("target"))):
                                if val is not None:
                                    setattr(twin, key, val)

                        # Klippy not ready at connect (printer off, restarting): the subscription answer is an error,
                        # no status will come until notify_klippy_ready -> the silence watchdog stays off meanwhile
                        klippy_down = "error" in first_response
                        if klippy_down:
                            print(f"[Klipper hazir degil: {first_response['error'].get('message', '?')} - "
                                  f"hazir olunca yeniden abone olunacak]")
                        while True:
                            try:
                                message = await asyncio.wait_for(ws.recv(),
                                                                 None if klippy_down else STATUS_TIMEOUT)
                            except websockets.exceptions.ConnectionClosedOK:
                                break
                            except asyncio.TimeoutError:
                                print(f"\n[{STATUS_TIMEOUT:.0f} s durum mesaji yok - yeniden abone olunuyor]")
                                break
                            data = json.loads(message)

                            method = data.get("method")
                            if method in ("notify_klippy_disconnected", "notify_klippy_shutdown"):
                                klippy_down = True
                                print(f"\n[Klipper baglantisi kesildi ({method}) - hazir olunca yeniden abone olunacak]")
                                continue
                            if method == "notify_klippy_ready":
                                # Moonraker drops object subscriptions when Klippy restarts: resubscribe through
                                # the reconnect path (fresh snapshot of print state, position, targets) [K33]
                                print("\n[Klipper hazir - yeniden abone olunuyor]")
                                break
                            if method != "notify_status_update":
                                continue

                            params = data["params"]
                            status = params[0]
                            wall_time = time.time()
                            event_time = params[1] if len(params) > 1 and isinstance(params[1], (int, float)) else None
                            if use_klipper_clock is None:
                                use_klipper_clock = event_time is not None
                                print("[Zaman kaynagi: " + ("Klipper eventtime]" if use_klipper_clock else
                                      f"PC saati - bildirimde eventtime yok, params={str(params)[:120]}]"))
                            if use_klipper_clock and event_time is None:
                                continue  # no time stamp - skip so the two clocks are never mixed
                            now = event_time if use_klipper_clock else wall_time

                            # a field not in the message stays None (Moonraker sends only what changed)
                            ex = status.get("extruder", {})
                            T1_real = ex.get("temperature")
                            pwm = ex.get("power")
                            target = ex.get("target")
                            # bed: enters the nozzle twin only as power (supply voltage sag);
                            # its own twin (bed_model.BedTwin) below, in observation mode
                            bed_msg = status.get("heater_bed", {})
                            for key, val in bed_msg.items():
                                if val is not None:
                                    bed[key] = val
                            fan = status.get("fan", {}).get("speed")
                            ext = status.get("motion_report", {}).get("live_extruder_velocity")
                            # head speed and position: logging only (does the bed loss grow as the bed moves along Y;
                            # nozzle-bed proximity from Z)
                            mr = status.get("motion_report", {})
                            if mr.get("live_velocity") is not None:
                                motion["v"] = mr["live_velocity"]
                            pos = mr.get("live_position")
                            if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                                motion["x"], motion["y"], motion["z"] = pos[0], pos[1], pos[2]
                                if len(pos) >= 4:
                                    # total extruder position (mm of filament): the difference between two moments =
                                    # the real net feed; no sampling/retraction loss (candidate for the flow)
                                    motion["e"] = pos[3]

                            update_zoff(zoff, status)

                            for key, val in status.get("gcode_move", {}).items():
                                if val is not None:
                                    gmove[key] = val
                            # print state (tracked before the twin is set up too, so the start is not missed)
                            ps = status.get("print_stats", {})
                            if ps:
                                was = pstat.get("state")
                                for key, val in ps.items():
                                    if val is not None:
                                        pstat[key] = val
                                if pstat.get("state") != was:
                                    print(f"[baski durumu: {was} -> {pstat.get('state')}]")
                                    # resuming from pause is not a new print
                                    if pstat.get("state") == "printing" and was != "paused":
                                        print(f"[baski basladi: {pstat.get('filename', '?')} - G-code plani hazirlaniyor]")
                                        # one log per print: the part before (idle, the previous print's cool-down)
                                        # is archived and summarized; this message is the new file's first row
                                        old = log.rotate()
                                        if old:
                                            print(f"[log bolundu: {os.path.basename(old)}]")
                                            loop.run_in_executor(None, summarize_session, old).add_done_callback(on_summary)
                                        # initial condition: the state measured now (last known if the twin is not set up yet)
                                        th = twin.T1_real if twin is not None else T1_real
                                        init = {"T_h": th if th is not None else 25.0,
                                                "T_b": bed.get("temperature", 25.0),
                                                "pwm_h": twin.pwm if twin is not None else last_known["pwm"],
                                                "pwm_b": bed.get("power", 0.0) or 0.0,
                                                "tgt_h": (twin.target if twin is not None else None) or 0.0,
                                                "tgt_b": bed.get("target", 0.0) or 0.0,
                                                "fan": twin.fan if twin is not None else last_known["fan"],
                                                "abs_e": bool(gmove.get("absolute_extrude", True))}
                                        fut = loop.run_in_executor(None, plan_job, pstat.get("filename", ""),
                                                                   wall_time, init)
                                        fut.add_done_callback(on_plan)

                            if twin is None:
                                # the initial state could not be read: collect inputs, set up the model at the first temperature
                                for key, val in (("pwm", pwm), ("fan", fan), ("ext", ext)):
                                    if val is not None:
                                        last_known[key] = val
                                if T1_real is None:
                                    continue
                                k0, kf0 = load_k()
                                twin = Twin(*load_state(T1_real, last_known["pwm"], last_known["fan"]),
                                            last_known["pwm"], last_known["fan"], last_known["ext"],
                                            k0, kf0, load_k_ref(k0))
                                twin.material = mat_last

                            r = twin.on_message(now, T1_real, pwm, fan, ext, target, bed.get("power"), motion.get("e"),
                                                pstat.get("state"))
                            if twin.learner.windows:     # maintenance log: window k values (model clock -> wall clock)
                                maint_record([(tn + wall_time - now, kind, kv) for tn, kind, kv in twin.learner.windows])
                                twin.learner.windows.clear()

                            # bed twin (OBSERVATION: no alarm). Set up once the bed temperature and power are first known.
                            # On an error only the bed twin switches off; the nozzle twin and monitoring continue.
                            if bed_twin is None and bed_error is None and "temperature" in bed and "power" in bed:
                                bed_twin = bed_model.BedTwin(bed["temperature"], bed["power"])
                            bed_r = None
                            if bed_twin is not None:
                                try:
                                    hv = motion.get("v")
                                    bed_r = bed_twin.on_message(now, bed_msg.get("temperature"), bed_msg.get("power"),
                                                                fan, hv, None if hv is not None else ext)
                                except Exception as e:  # noqa: BLE001
                                    bed_twin, bed_error = None, f"{type(e).__name__}: {e}"
                                    print(f"[tabla ikizi hata verdi ve kapandi: {bed_error} - izleme devam ediyor]")
                            if wall_time - last_save >= STATE_SAVE_INTERVAL:
                                save_state(twin.T1_model, twin.T2_model, twin.T3_model, twin.learner.k,
                                           twin.learner.kfil, twin.k_ref, twin.material)
                                last_save = wall_time

                            for code in r["faults_started"]:
                                g = r["eksik_guc"]
                                print(f"\n!!!!! ARIZA: {code} - {FAULT_TEXT[code]}"
                                      f"  (residual={r['residual']:+.1f}C, k={twin.learner.k:.3f}"
                                      + (f", eksik guc={g:+.1f} W" if g is not None else "") + ")\n")
                            if r["faults_started"] and not any(abs(s[1] - now) < 60.0 for s in scheduled):
                                # not scheduled again if an event that started recently is already to be classified
                                codes = ",".join(sorted(twin.faults.active))
                                scheduled += [[now + dl, now, codes, dl] for dl in CLASSIFY_DELAYS]
                                print(f"[ariza kategorisi {CLASSIFY_DELAYS[0]:.0f} s sonra hesaplanacak]")
                            for code, t0, t1 in r["faults_ended"]:
                                print(f"[ariza bitti: {code}, {t1 - t0:.0f} s surdu]")
                            if r["k_accepted"]:
                                k_old, k_new = r["k_accepted"]
                                msg = (f"KAYIP kabul edildi: k {k_old:.3f} -> {k_new:.3f} ({100 * (k_new / k_old - 1):+.0f}%), "
                                       f"{K_ACCEPT_TIME / 60:.0f} dk sabit kaldi; yeni referans bu seviye")
                                print(f"\n[{msg}]\n")
                                try:
                                    with open(REPORT_FILE, "a", encoding="utf-8") as rf:
                                        rf.write(f"\n##### {time.strftime('%Y-%m-%d %H:%M:%S')}  KAYIP_KABUL  {msg}\n")
                                except OSError:
                                    pass
                            if r["duraklat"] and PAUSE_MODE != "kapali" and pstat.get("state") == "printing":
                                msg = (f"DURAKLATMA: GUC_AZALDI {PAUSE_AFTER:.0f} s'dir kesintisiz "
                                       f"(eksik guc {r['eksik_guc'] if r['eksik_guc'] is not None else float('nan'):+.1f} W, "
                                       f"{pstat.get('filename', '?')}) - "
                                       + ("GOLGE MOD: baski duraklatilmadi, yalniz kayit" if PAUSE_MODE == "golge"
                                          else "baski duraklatiliyor"))
                                print(f"\n!!!!! {msg}\n")
                                try:
                                    with open(REPORT_FILE, "a", encoding="utf-8") as rf:
                                        rf.write(f"\n##### {time.strftime('%Y-%m-%d %H:%M:%S')}  DURAKLATMA  {msg}\n")
                                except OSError:
                                    pass
                                if PAUSE_MODE == "aktif":
                                    pf = loop.run_in_executor(None, request_pause)
                                    pf.add_done_callback(lambda f: print(f.result()))
                            if twin.energy_error and not energy_error_shown:
                                print(f"[enerji uyarisi hata verdi ve kapandi: {twin.energy_error} - izleme devam ediyor]")
                                energy_error_shown = True
                            if r["resynced"]:
                                print(f"[senkronize edildi - {r['dt']:.1f}s'lik veri boslugu]")
                                if r["dt"] < 0:
                                    # the clock went back: the buffered rows are no longer on the same time axis
                                    buffer.clear()
                                    scheduled.clear()
                            if r["new_temperature"]:
                                alarm_note = "  !! ARIZA: " + ",".join(sorted(twin.faults.active)) if r["anomali"] else ""
                                if r["k_anomali"]:
                                    alarm_note += f"  !! KAYIP DEGISTI (k={twin.learner.k:.3f}, referans {twin.k_ref:.3f})"
                                print(f"T1_real={twin.T1_real:6.1f}C  "
                                      f"T1_model={twin.T1_model:6.1f}C  "
                                      f"residual={r['residual']:+6.1f}C  "
                                      f"dt={r['dt']:.3f}s  "
                                      f"pwm={twin.pwm:.2f}  fan={twin.fan:.2f}  "
                                      f"ext={twin.ext:5.2f}  k={twin.learner.k:.3f} kfil={twin.learner.kfil:.2f}"
                                      + (f"  tabla={bed['temperature']:5.1f}/{bed.get('target', 0):.0f}C "
                                         f"%{100 * bed.get('power', 0):.0f}"
                                         + (f" (model {bed_twin.T_model:5.1f}, res {bed.get('temperature', 0) - bed_twin.T_model:+4.1f})"
                                            if bed_twin is not None else "")
                                         if "temperature" in bed else "")
                                      + alarm_note)

                            row = {
                                "time": wall_time, "eventtime": now if use_klipper_clock else "",
                                "T_yeni": int(r["new_temperature"]),
                                "T1_real": twin.T1_real, "T1_model": twin.T1_model,
                                "residual": "" if r["residual"] is None else r["residual"],
                                "pwm": twin.pwm, "fan_state": twin.fan, "extrude_velocity": twin.ext,
                                "anomali": r["anomali"], "k_loss": twin.learner.k,
                                "k_anomali": r["k_anomali"], "ariza": ";".join(sorted(twin.faults.active)),
                                "eksik_guc": "" if r["eksik_guc"] is None else round(r["eksik_guc"], 3),
                                "target": "" if twin.target is None else twin.target,
                                "bed_T": bed.get("temperature", ""), "bed_pwm": bed.get("power", ""),
                                "bed_target": bed.get("target", ""),
                                "head_v": motion.get("v", ""), "pos_x": motion.get("x", ""),
                                "pos_y": motion.get("y", ""),
                                "pos_z": motion.get("z", ""), "pos_e": motion.get("e", ""),
                                "bed_model": "" if bed_twin is None else round(bed_twin.T_model, 3),
                                "bed_res": "" if not bed_r or bed_r["residual"] is None else round(bed_r["residual"], 3),
                                "bed_k": "" if bed_twin is None else round(bed_twin.learner.k, 4),
                                "k_fil": round(twin.learner.kfil, 4),
                                "print_state": pstat.get("state", ""),
                                "z_offset": zoff.get("z", ""), "z_endstop": zoff.get("endstop", ""),
                                "malzeme": twin.material or "",
                                "duraklat": PAUSE_MODE if r["duraklat"] and pstat.get("state") == "printing" else "",
                            }
                            if not log.skip_idle(row, now):
                                log.write(row, now)

                            # phone page (read-only copy)
                            live.update(T=twin.T1_real, M=twin.T1_model, pwm=twin.pwm, fan=twin.fan,
                                        k=twin.learner.k, ext=twin.ext, G=r["eksik_guc"], wall=wall_time,
                                        ariza=sorted(twin.faults.active),
                                        bT=bed.get("temperature"), bTg=bed.get("target"), bP=bed.get("power"),
                                        bM=None if bed_twin is None else bed_twin.T_model,
                                        bC=None if bed_twin is None else bed_twin.T_glass)
                            if r["resynced"] and r["dt"] < 0:
                                live["h"].clear()
                            if r["residual"] is not None and not r["resynced"]:
                                live["R"] = r["residual"]
                                h = live["h"]
                                if not h or now - h[-1][0] >= 1.0:
                                    h.append([now, round(r["residual"], 2),
                                              None if r["eksik_guc"] is None else round(r["eksik_guc"], 3)])
                                    while h and h[0][0] < now - WEB_HISTORY:
                                        h.popleft()

                            # fault classification: the latest rows are in memory, due jobs run in the background
                            buffer.append((now, row))
                            while buffer[0][0] < now - CLASSIFY_BUFFER:
                                buffer.popleft()
                            for job in [s for s in scheduled if now >= s[0]]:
                                scheduled.remove(job)
                                fut = loop.run_in_executor(None, classify_job, [b[1] for b in buffer], *job[1:])
                                fut.add_done_callback(lambda f: show_category(f.result(), live))
                            if wall_time - last_flush >= FLUSH_INTERVAL:
                                log.flush()
                                last_flush = wall_time
                            if time.strftime("%Y-%m-%d", time.localtime(wall_time)) != today:   # 24/7: once a day
                                today = time.strftime("%Y-%m-%d", time.localtime(wall_time))
                                daily_jobs(live)
                    print("\n[baglanti kapandi]")
                except (websockets.exceptions.WebSocketException, OSError, asyncio.TimeoutError) as e:
                    print(f"\n[baglanti koptu: {type(e).__name__}: {e}]")
                log.flush()
                print(f"[{RECONNECT_DELAY:.0f} s sonra yeniden baglaniliyor - durdurmak icin Ctrl+C]\n")
                await asyncio.sleep(RECONNECT_DELAY)
        finally:
            if web is not None:
                web.close()
            # on exit (Ctrl+C, connection loss) always save the final state
            if twin is not None:
                save_state(twin.T1_model, twin.T2_model, twin.T3_model, twin.learner.k, twin.learner.kfil,
                           twin.k_ref, twin.material)
                events =twin.faults.events + [(c, t0, None) for c, t0 in twin.faults.active.items()]
                print(f"\nAriza ozeti: {f'{len(events)} ariza' if events else 'ariza yok'}")
                for code, t0, t1 in events:
                    duration_txt = f"{t1 - t0:.0f} s" if t1 is not None else "hala aktif"
                    print(f"  {code}: {duration_txt} - {FAULT_TEXT[code]}")
                if events:
                    print(f"  kategoriler (hesaplandiysa): {REPORT_FILE}")
                # session-to-session trend: the last log part is archived first, so its summary in twin_gecmis.csv
                # names the file that stays (the next start used to rename it afterwards) [K35]
                msg = summarize_session(log.close_archive() or LOG_FILE)
                if msg:
                    print(msg)


if __name__ == "__main__":
    import signal

    def _stop(*_):
        raise KeyboardInterrupt   # systemd stop (SIGTERM) ends like Ctrl+C: state saved, session summarized

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nIzleme durduruldu.")
