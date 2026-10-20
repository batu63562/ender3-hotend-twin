# Profile and calibration files — design (v0.3, draft)

Goal of v0.3: the generic physics is separated from this printer's numbers. Today every number lives in the code
(`realtime_websocket.py`, `bed_model.py`). This document sorts them by where they should come from and proposes
the files. Nothing here is implemented yet. Design notes and the evidence behind each choice are in
`KARARLAR.md` [K36]–[K41] (Turkish).

## Layers

Values are resolved from the bottom up; each layer overrides the one below it.

| Layer | File | Written by | Changes |
|---|---|---|---|
| 1. Generic defaults | the code | developer | never per printer |
| 2. Profile | `profile.toml` | the user (a wizard later) | when the hardware changes |
| 3. Calibration | `calibration.json` | `calibrate.py --write` from a calibration print | after a calibration print |
| 4. Live state | `twin_state.json` | the live twin | continuously (k, k_fil, references) |

The acceptance test of v0.3: the Ender 3 runs from a profile and a calibration file that hold today's values, and
`py benchmark/baseline_snapshot.py --check v0.2` reports IDENTICAL.

## Where each number goes

### Profile — what the user knows (or can look up) about the hardware

| Constant | Today | Profile key (proposal) | Note |
|---|---|---|---|
| `P_MAX` | 40 W | `hotend.heater_power_w` | 30/40/50/60 W cartridges are common. Nominal at the supply voltage. |
| `BED_SAG` | 0.042 | `power.bed_sag` | Supply sag with the bed on. Default 0; can be calibrated (one bed-on hold). |
| `FAN_F0`, `FAN_EXP` | 0.191, 0.508 | `part_fan.type` → defaults | Threshold and exponent depend on the blower and duct (4010 / 5015 / CPAP). Refined by the fan steps of the calibration print (K39: the 100 % factor alone does not carry to 25–40 %). |
| `K_FIL` | 0.0054 | from `materials` | Filament heat = density × cp × filament area. Today fixed at a PETG value; `k_fil` corrects it per print (anchor 1.33, K27). |
| nozzle / filament diameter | from `printer.cfg` | — | Already read from the printer configuration; nothing to add. |
| `T_AMB` | 25 C | `room_c` (optional) | Fixed assumption; the room was 20.6–24.8 C (K36). `k` absorbs the difference. Could later be measured from idle (bed and nozzle both cold). |
| bed: `P_BED`, `C_AL`, `C_CAM`, `EPS_A` | 220 W, … | `bed.*` | Bed size, heater power, plate (glass / PEI / steel). The bed twin is observation only; its calibration is a later step. |

### Calibration — fitted from the calibration print

The set the data can pin down (K39, K41): fitted as log-factors around the profile's starting values, with the
profile's uncertainty as a prior.

| Constant(s) | Fitted as | Identified by the calibration print (virtual run) |
|---|---|---|
| `C1`, `C2`, `C3` | one factor `C_ALL` (fixed split) | ±0.8 % |
| `R31` | itself (with `C3` fixed it is the cartridge time constant) | ±20 % |
| `R_HB` | itself | ±14 % |
| `CRAD` (with `HA1`) | `CRAD`: the shape of the loss over temperature; its level is `k` | ±5 % |
| `HA2` | fixed ratio to `HA1` (the data cannot separate block and heat-break loss, r = −1.00) | — |
| `HA2_FAN`, `FAN_F0`, `FAN_EXP` | amplitude from the fan steps; threshold and exponent from the steps around the threshold | amplitude ±2.7 % |
| `CAP_T_COEF` | itself | ±14 % |
| starting `k` | per log, fitted together | — |

Open structural question (K36, K40): one capacity cannot fit heat-up and cooling at once. A structure where the
hot cartridge loses part of the heat (S3d) fits the calibration logs much better (1.4 vs 2.3 C) but is not
accepted yet; if it is, `EPS3` joins this table, and the calibration print alone separates it only weakly
(×2.2): it has to be fitted together with step experiments.

### Live state — learned while running (unchanged)

`k`, `k_fil`, `k_ref` (KAYIP reference), last material, model state; bed `k`. Stays in `twin_state.json`.

### Generic — algorithm settings, not printer numbers

Learning windows and time constants (`ADAPT_*`, `PRINT_*`, `Q_FIL`, `R_HOLD`, `R_PRINT`, `P0_FIL`), alarm logic
(`ENERGY_WINDOW`, `ENERGY_CONFIRM`, `ENERGY_SETTLE`, `FAULT_*`, `K_ALARM_*`, `K_ACCEPT_*`, `PAUSE_AFTER`), numerics
(`MAX_SUBSTEP`, `RK4_STABILITY`, `GAP_THRESHOLD`, `EXT_*`, `POS_E_MAX_RATE`), logging (`IDLE_LOG_INTERVAL`, …).

Two of them are thresholds in watts and may have to scale with the printer:
- `ENERGY_THRESHOLD` (2.5 W): a full flow stop is visible only above ~1.6 mm/s of filament on this hotend. On a
  hotter / faster hotend the filament heat is larger, the threshold could stay; on a small one it may have to be
  lower. Decide with the second printer (v0.4).
- `ADAPT_TMIN` / `ENERGY_TMIN` (230 C): tied to PETG at 235–245 C. PLA at 200–215 C would never be "hot" for the
  learner and the alarm. Proposal: relative to the print temperature (e.g. target − 15 C).

## Files (proposal)

`profile.toml` (user, English keys, versioned with `profile_version`):

```toml
profile_version = 1
name = "Ender 3, direct drive, all-metal"
room_c = 23            # optional

[hotend]
heater_power_w = 40
block = "volcano-like aluminium"   # informational; selects starting values for the capacities

[part_fan]
type = "5015 blower"   # selects FAN_F0 / FAN_EXP starting values

[power]
bed_sag = 0.042

[bed]
heater_power_w = 220
plate = "glass"
```

`calibration.json` (written by `calibrate.py --write`, read by the twin; English keys, values in SI units):

```json
{
  "calibration_version": 1,
  "date": "2026-10-21",
  "source_logs": ["realtime_log_ws_onceki_20261021_1410.csv"],
  "code_version": "3dugum-gecikenkutle-v4+…",
  "params": {"C1": 2.0, "C2": 4.034, "C3": 0.8303, "R31": 3.0, "R_HB": 1.805, "HA1": 0.03624, "CRAD": 1.5259e-10,
             "HA2": 0.004435, "HA2_FAN": 0.0293, "FAN_F0": 0.191, "FAN_EXP": 0.508, "CAP_T_COEF": 0.0012},
  "uncertainty_log_factor": {"C_ALL": 0.008, "R31": 0.19, "…": 0.0},
  "fit_rms_c": 1.4,
  "k_start": 1.0
}
```

## Order of work

1. Close v0.2, tag it, `baseline_snapshot.py --save v0.2` (a candidate snapshot `v0.2-aday` exists).
2. Behaviour-neutral refactor: constants read through one loader (code defaults ← profile ← calibration), English
   identifiers (step 3 of the language change). `--check v0.2` must stay IDENTICAL after every step.
3. `calibrate.py --write` produces `calibration.json`; the Ender's file holds today's values exactly.
4. First real calibration print (`yazici/calibration_245.gcode`), fitted; its values go through the acceptance gate
   like any model change (they are scored on logs of the same hardware state, `--score-since`).
5. Structural question (S3d) separately, through the gate.
