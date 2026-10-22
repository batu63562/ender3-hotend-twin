# Ender 3 Hotend Digital Twin

A physics-based digital twin of a 3D printer hotend (Creality Ender 3, direct drive, all-metal hotend,
Klipper + Moonraker on a Raspberry Pi). It runs next to the printer, predicts how much heater power the hotend
*should* need, and raises an alarm when the real heater deviates from that prediction.

<!-- demo GIF: media/demo.gif (faults caught in the viewer; added with the v0.3 release) -->

**One sentence:** a thermal model computes the heater power the hotend should need; when the real heater
power deviates, the twin raises an alarm; slow drifts are absorbed by a learned loss factor `k`.

No extra sensors are added. Everything comes from signals Klipper already has: nozzle temperature,
heater PWM, part-fan speed, extruder position, bed temperature/power. In the literature this is
**model-based fault detection with analytical redundancy** (a model replaces a second, redundant sensor),
built on a **grey-box model** (fixed physics + a few parameters learned online). Since v0.2 it closes one
feedback path automatically: it pauses the print on a persistent missing-power alarm. For that path it is a
**digital twin** in Kritzinger et al.'s (2018) terminology; everything else (advice, the plan) is still a
**digital shadow**.

> Comments are in English; code identifiers and console messages are still in Turkish (the author's language).
> See the [glossary](#glossary). Identifiers will be renamed with the v0.3 refactor; CSV columns and alarm codes
> keep their names.
> The decision history (why each parameter has its value, what was tried and rejected, with scores)
> is in [`KARARLAR.md`](KARARLAR.md) (Turkish); code comments refer to it as `[Kxx]`.
> Planned versions: [`ROADMAP.md`](ROADMAP.md).

### Try it without a printer

1. Download `ender3-twin-viewer-windows-v0.1.zip` from the [latest release](../../releases/latest) and
   unzip it anywhere.
2. Run `Ender 3 Digital Twin.exe`. The start menu lists three real recordings from the `sample_data`
   folder next to it.
3. Open **demo1 filament runout PETG**, press **N** to jump to the alarm: the spool ran empty at ~61.5 min,
   the twin raises `GUC_AZALDI` ("low heat demand") about 20 s later, and the G-code toolpath shows where
   the print was. Mouse: drag to orbit, right-drag to pan, wheel to zoom. Keys: **H** / **B** hotend / bed
   view, **Space** pause, **1–3** speed, **Esc** back (view → overview → start menu).

To replay the same logs through the Python twin instead (Windows/macOS/Linux), see
[Running it](#running-it) and the evaluation-data release asset.

---

## Contents
1. [What it detects (and what it cannot)](#what-it-detects-and-what-it-cannot)
2. [Architecture and data flow](#architecture-and-data-flow)
3. [The thermal model](#the-thermal-model)
4. [The two signals: temperature residual vs. missing power](#the-two-signals)
5. [Online learning: `k` and `k_fil`](#online-learning-k-and-k_fil)
6. [Alarms and actions](#alarms-and-actions)
7. [Other modules](#other-modules)
8. [Evaluation methodology](#evaluation-methodology)
9. [Running it](#running-it)
10. [Files](#files)
11. [Limitations](#limitations)
12. [Glossary](#glossary)
13. [License](#license)

---

## What it detects (and what it cannot)

The PID controller keeps the nozzle at its set temperature, so most faults do **not** show up in the
temperature itself. They show up in the **effort** the controller needs: the heater power. A fault that
removes or adds heat load changes heater power by watts: the heater becomes a calorimeter.

| Detected (heat-flow faults) | Typical signal | Time to alarm |
|---|---|---|
| Filament stops flowing (run-out, jam, extruder slipping) | +2…+8 W "missing power" | 15–30 s |
| Lines not sticking / not pressed on the bed (first layer) | +2…+3 W | ~20 s |
| Silicone sock lost, draft, fan duct blowing on the block | −2.5 W or more | ~1 min |
| Slow hotend degradation (dirty heatsink, lost thermal paste) | learned `k` drifts | days (maintenance advice) |

| Not detected | Why |
|---|---|
| Part detaching from the bed, collisions, layer shifts that keep the nozzle on the part | Mechanical; heat balance changes < 1 W. Needs an accelerometer or camera. (A shift that leaves the nozzle printing beside the part *is* seen: less heat goes into the print.) |
| Slow faults that build up over ~10 min near the alarm threshold, at the start of a print | Indistinguishable from a print that simply needs less heat; `k_fil` learns them (see [Online learning](#online-learning-k-and-k_fil)). |
| Print-quality issues (e.g. 0.1 mm layer defects, stringing) | Change heat flow < 1 W, same size as normal part-to-part variation. |
| Thermistor offset vs. real loss change | Needs a second temperature sensor. |

Real examples are in [Evaluation](#evaluation-methodology).

---

## Architecture and data flow

```
Klipper ──websocket──► Moonraker ──► realtime_websocket.py ──► realtime_log_ws.csv ──► Unity (3D view)
 (PID, heaters,                       │  Twin.on_message()              │
  fan, motion)                        │   ├─ hotend model (3 nodes, RK4) ├─► twin_state.json (learned k, k_fil, refs)
                                      │   ├─ EnergyMonitor ("missing power")
                                      │   ├─ KLossLearner (k, k_fil Kalman)
                                      │   ├─ FaultMonitor (GUC_*, KAYIP_*)
                                      │   └─ shadow pause / maintenance advice
                                      ├─ bed twin (bed_model.py, observation only)
                                      ├─ G-code plan at print start (gcode_plan.py → tahminler/)
                                      └─ phone page (http://<pc>:8080)
```

- **Same code live and offline.** `Twin.on_message()` processes one Moonraker message. The live script and
  the evaluation tool (`evaluate.py`) call exactly the same code, so a recorded log replays to the
  identical result. Every model change is scored this way before it goes live.
- **Clock.** The model integrates on Klipper's `eventtime` (when values were measured), not the PC arrival
  time; network jitter otherwise created fake ±8 °C oscillations.
- **Zero-order hold.** Inputs (PWM, fan, flow) received in a message apply to the *next* interval, exactly
  as the firmware applied them.
- **One-way to Unity.** Unity only reads the CSV (and a few JSON/plan files); see `UNITY_ARAYUZ.md` for the
  column contract.

---

## The thermal model

Three lumped thermal masses (an RC network), integrated with RK4:

```
 heater cartridge T3 ──R31──► block near thermistor T1 ──R_HB──► nozzle / heat-break top T2
      ▲                          │                                 │
   P = pwm·P_MAX·(1−BED_SAG·bed) │ k·natural_loss(T1)              │ HA2·(T2−T_amb)        (to heatsink)
                                 │ k_fil·K_FIL·flow·(T1−T_amb)     │ k·G_fan(fan)·(T2−T_amb) (part fan)
                                 ▼  (to air and into the print)    ▼
```

$$C_3(T_3)\,\dot T_3 = P - \frac{T_3-T_1}{R_{31}}$$
$$C_1(T_1)\,\dot T_1 = \frac{T_3-T_1}{R_{31}} - \frac{T_1-T_2}{R_{HB}} - k\,L(T_1) - k_{fil}\,K_{FIL}\,\dot e\,(T_1-T_{amb})$$
$$C_2(T_2)\,\dot T_2 = \frac{T_1-T_2}{R_{HB}} - H_{A2}(T_2-T_{amb}) - k\,G_{fan}(f)\,(T_2-T_{amb})$$

- `L(T) = HA1·(T−T_amb) + CRAD·(T⁴−T_amb⁴)`: natural convection + radiation from the block.
- `G_fan(f) = HA2_FAN · max((f−F0)/(1−F0), 0)^0.508`: part-fan cooling with a threshold (the 5015 blower
  does not push air through the duct below ~19 % PWM) and forced-convection exponent ≈ 0.5.
- `C(T) = C·(1 + 0.0012·(T−140))`: temperature-dependent heat capacity (fixes heat-up dynamics).
- `ė` = net filament feed rate from the extruder **position** difference, averaged over 10 s with sign
  (retract + unretract cancel), then clipped at 0.
- `BED_SAG`: when the bed heater is on, the 24 V rail sags and the cartridge delivers ~4 % less power.
- Parameters were fitted to dedicated calibration runs (September, `benchmark/`) within physical bounds.
  See `hotend_termal_model_raporu.md` for the original modelling report.

Why three nodes: with a single mass the model lagged ~10 °C during heat-up; heat reaches the thermistor
first and the nozzle/heat-break side later.

---

<a name="the-two-signals"></a>
## The two signals: temperature residual vs. missing power

The twin produces two comparisons with reality:

1. **Temperature residual** = measured T1 − model T1 (open loop). Intuitive and good for visualization,
   but an open-loop model integrates any small power error: around 245 °C, 1 W of unexplained load becomes
   ~15 °C of residual, with a ~52 s memory. A ±2 °C wander is normal.
2. **Missing power** (`eksik_guc`, W) = power the model expects to be lost − (measured heater power −
   energy stored in the three masses), over the last 20 s, driven by the **measured** T1.
   `+` means less heat is going out than expected (e.g. filament not flowing), `−` means more.
   It settles in ~15–20 s and does not accumulate model drift.

Because the controller holds temperature constant, the heater power acts as a calorimeter: **missing power
is the official alarm signal.** It is only evaluated near the print target (above target − 15 °C), with a stable
temperature and 30 s after a heat-up or set-point change. Industry practice for closed-loop systems is the
same (monitor the controller effort, not the controlled variable).

| Situation | Residual | Missing power |
|---|---|---|
| Normal fan-on printing | ±2 °C | ±0.3 W |
| First layer with `k` off by 0.03–0.05 | +5 °C | +0.6…0.9 W |
| Filament ran out | +16 °C | +4.5 W (alarm in 20 s) |
| 150 °C preheat, heat-up, cool-down | ±4…9 °C | not evaluated |

---

## Online learning: `k` and `k_fil`

Two dimensionless correction factors are learned online from an **energy balance** over windows:

```
measurement  y = (energy in − energy stored − heat-break path) / duration          [W]
model        y = k · (block + fan loss) + k_fil · K_FIL · flow · (T − T_amb)
```

- **`k` — loss-to-air factor.** Absorbs slow changes of the hotend itself (dirt/plastic residue on the
  block, thermal-paste contact, sock, room temperature). Kept between sessions in `twin_state.json`.
- **`k_fil` — heat-into-the-print factor**, only when the part fan is cooling. It is not just filament
  heating: the hot nozzle face also conducts heat into freshly laid plastic, which depends on fan speed,
  geometry and material. With the fan off, heat into the print = filament heating only (`k_fil` fixed at 1.0).
- **Windows.** *Hold* windows (20 s, no extrusion, near the print target, stable T) teach `k`. "Near the print
  target" means above target − 15 °C and never below 180 °C (so a 150 °C preheat is never learned from); with no
  target known it falls back to 230 °C. The same limit gates the missing-power alarm. *Print* windows (60 s,
  stable T, valid missing power, no alarm) teach `k` and `k_fil` together. Windows containing a fault
  (|missing power| > 2.5 W or an active alarm) are discarded so faults are not learned as "normal".
- **Estimator.** A 2-state Kalman filter on `[k, k_fil]`. `k_fil` has process noise (it may change ~0.05 per
  hour). `k` has none; its speed is set by a covariance floor equivalent to a time constant (60 s in holds,
  30 min while printing) so short faults never leak into it.
- **Anchor.** `k` belongs to the machine and is carried from print to print. `k_fil` belongs to the print
  (geometry, speed): every new print starts it from a fixed anchor (1.33) with fresh uncertainty and learns
  from there. Carrying it over let a series of similar prints push it to its upper bound; a per-material
  value made no difference (PETG and TPU anchors came out 1.35 and 1.32). See `KARARLAR.md` [K27].

**The core trade-off ("error reduction vs. error swallowing").** A faster learner makes residuals smaller
but also absorbs slow faults as the "new normal". This was tested by injecting synthetic slow faults into
real logs (heater power reduced along a ramp): making `k_fil` 16× faster improved scores slightly but
missed 3 of 5 ten-minute, 3 W faults. The chosen setting keeps the filter time constant and only shortens
the window (`[K17]`). It is the same dilemma as numerical diffusion in CFD.

The test is a permanent tool (`python benchmark/slow_fault_test.py`, part of the acceptance gate). Current
detection limits on the calibration set: a 3 W fault ramping in over 10 min is caught mid-print (17 of 18,
~9 min), but only 8 of 20 times when it starts as the part fan turns on, early in the print; a 4 W fault over
30 min is always caught (~21–26 min). Early in a print the learner cannot tell a slow fault from a print that
needs less heat: learned `k_fil` legitimately ranges 1.1–1.6 between prints (~±1 W), and a 3 W fault is only
0.5 W above the threshold. Every setting that swallowed less (a tighter start uncertainty) raised false alarms on
healthy fan-cooled prints, so it was not changed (`[K55]`). Fast faults (most real ones: jams, run-outs) are
unaffected.

---

## Alarms and actions

| Code | Meaning | Rule |
|---|---|---|
| `GUC_AZALDI` / `GUC_ARTTI` | Less / more heat leaving than expected (fast faults) | \|missing power\| > 2.5 W for 5 s |
| `KAYIP_ARTTI` / `KAYIP_AZALDI(_FAN)` | Learned `k` drifted from its last accepted level | \|k − k_ref\| > 0.08 (hysteresis 0.06) |

- **Acceptance.** If `k` stays at a new level for 30 min of hot time, the new level is accepted as the
  reference and the `KAYIP` alarm closes (e.g. after maintenance). This avoids alarms that never stop for a
  known permanent change.
- **Maintenance advice** (not an alarm). Accepting changes means a *slow* degradation is accepted step by
  step. So each window's own `k` is also logged (`bakim_izleme.csv`), and at start-up the daily median is
  compared with a **fixed** clean reference (1.00). Three data days outside ±0.06 in the same direction →
  advice (low `k`: check heat-break thermal paste, dried plastic around the heatsink, i.e. heat-creep risk).
  Replayed on past data it would have advised maintenance two days before the real one, with no false advice.
  This mirrors aircraft-engine trend monitoring: a moving reference for events, a fixed one for wear.
- **Pause.** If `GUC_AZALDI` lasts 150 s continuously during printing, the twin sends
  `POST /printer/print/pause` to Moonraker (`PAUSE_MODE = "aktif"`, the default since v0.2; `"golge"` only logs
  "would pause", `"kapali"` turns it off). In 66.5 print hours of replayed logs all 4 pauses were real faults;
  the longest labelled benign event lasted 83 s. `KAYIP` never pauses.
- **Fault category** (`fault_classifier.py`): after an alarm, a rule-based classifier suggests a physical
  category (A fan path / B no filament flow / C insulation-environment / D thermistor / E heater).
  Diagnostic only; known to confuse B and C in some cases.

---

## Other modules

| File | Role |
|---|---|
| `realtime_websocket.py` | Live twin: Moonraker connection, model, learner, alarms, CSV, phone page, plan trigger, shadow pause, maintenance advice. |
| `evaluate.py` | Offline evaluation: replays logs through the live code, phase-by-phase RMSE and alarm counts (`--set calibration|test`, `--live-start`, `--chain`). |
| `bed_model.py` | Bed twin (aluminium plate + glass, 2 nodes) with its own loss learner. Observation only, no alarm. |
| `gcode_plan.py` | The "plan": parses the G-code, replicates Klipper's motion planner, simulates hotend/bed with the PID (`closed_loop.py`), writes the expected timeline and the toolpath (`tahminler/`). Unity compares plan vs. reality (schedule deviation, flow, power). |
| `closed_loop.py` | Closed-loop (PID) simulation used by the plan and for validation. |
| `fault_classifier.py` | Post-alarm physical category (see above). |
| `session_history.py` | Per-session summary history (`twin_gecmis.csv`). |
| Viewer (Unity 6) | 3D printer from CAD, replay/live timeline, alarm map on the G-code toolpath. Released as a Windows build; its source is not public yet. It only reads the CSV/plan files (contract: `UNITY_ARAYUZ.md`). |

---

## Evaluation methodology

Logs are split into two sets in `benchmark/setler.csv`:
- **ayar** (calibration): used to choose and tune. New logs alternate between the sets.
- **sınav** (test): never used to choose a parameter; only a final check after a change is approved.

A model change is accepted only through an **acceptance gate** (all of):
1. a physical reason or a bug fix;
2. improvement on the calibration set (stateful changes scored from each log's recorded live start state);
3. no unexplained degradation of any September calibration **phase** (heat-up, arrival, 245 °C hold, fan
   hold, other holds, cool-down) — a total score must not hide a phase getting worse;
4. no degradation on the test set;
5. labelled faults detected as before **and** the synthetic slow-fault test not worse;
6. one change at a time.

Many intuitively right changes failed this gate (see `KARARLAR.md`), which is the point of having it.

Fault library: `benchmark/ariza_ornekleri/etiketler.csv` (labelled windows: expected category, or `-` for
benign events that must not trigger a pause).

Scores (RMSE of the temperature residual over all phases, °C, lower is better; *alarms* = confirmed alarm
onsets). Each number is the `TUMU` row under *KLIPPER ZAMANLI LOGLAR* of the command shown:

| Set | Logs | Command | RMSE | Alarms |
|---|---|---|---|---|
| September calibration runs | 9 | `python evaluate.py` | 3.62 | 0 |
| Calibration set (`ayar`) | 49 | `python evaluate.py --set calibration --live-start` | 3.43 | 17 |
| Test set (`sınav`) | 57 | `python evaluate.py --set test --live-start` | 3.54 | 19 |

`--live-start` starts each log from the `k` and `k_fil` the live twin had when it was recorded,
as it really ran. Without it every log starts from `k = 1`, which is wrong for the October logs (the real
`k` drifted to 0.87–1.06): the test set then scores 4.83. The logs are in the evaluation-data release
asset; the September runs are in the repository. These are the v0.2 scores (2026-10-22, frozen per log in
`benchmark/baseline_v0.2.json`; `python benchmark/baseline_snapshot.py --check v0.2` compares any later code
against them). The sets grew since v0.1 (37 / 48 logs: 3.48 / 3.28) with induced-fault experiments, calibration
experiments and prints with real faults, so the numbers are not comparable across versions; the model is the
same.

Alarm performance (`python benchmark/report_v02.py`, 2026-10-22; every log replayed with the live twin's code,
alarms matched against the labelled fault windows): 160 logs, 66.5 print hours; 17 of 20 labelled faults
caught, median delay 28 s; 4 shadow pauses, all on real faults, no false pause; false alarms 0.11 per print hour
on the test set. The current system alone (`--since 2026-10-18`): 22.2 print hours, 5 of 6 labelled faults
caught, no false pause.

Representative real events: filament run-out detected in ~20 s (+4.5 W); a layer shift caught in the minute
it was heard (the nozzle printing beside the part takes less heat); a gappy benchy roof at the height where it
was seen; a single bad first-layer object
out of four localized from the log + toolpath; maintenance need (dirty heatsink, missing thermal paste)
visible as a slow `k` decline from 1.00 to 0.89 over two weeks, then restored by maintenance.

---

## Running it

Requirements: Python 3.11+, `pip install -r requirements.txt` (`websockets`; `numpy`, `pandas` for the
plan, classifier and evaluation tools). Klipper + Moonraker reachable on the network.

1. Copy `config.example.toml` to `config.toml` and set `printer_host` (not needed for offline evaluation).
   Optional: `profile.example.toml` → `profile.toml` (heater power, part-fan curve, room) and a
   `calibration.json` (thermal parameters); without them the twin uses the built-in values of the author's
   Ender 3. See `PROFILE_DESIGN.md`.
2. Start the twin **before** starting a print (the G-code plan is only produced if the twin sees the print
   start): double-click `start_twin.bat` or run `python realtime_websocket.py`.
3. Optional: open `http://<pc-ip>:8080` on a phone. The released Windows viewer replays what is in its
   `sample_data` folder: copy your own `realtime_log_ws_*.csv` and `tahminler/` files there to view them.
   Following a live session from the released viewer is planned (v0.5).
4. Offline evaluation: `python evaluate.py --set calibration --live-start` (or `test`; `--chain` instead of
   `--live-start` to carry state from log to log).

**On the Klipper Raspberry Pi (recommended for continuous use).** A PC that goes to sleep stops the twin
mid-print; on the Pi it runs as a systemd service next to Klipper, at lower CPU/I/O priority. From this folder
on the PC (needs SSH access to the Pi):

```
.\deploy.ps1 -PiUser <pi user> -WithState -Samba
```

`-WithState` carries the learned state over once (stop the PC twin first), `-Samba` shares the twin's folder
read-only so the Unity viewer can read it (`\\<pi>\ender3-twin` as its data folder). The phone page moves to
port 8090 (8080 is usually the webcam). Logs: `journalctl -u ender3-twin -f`.

To copy the finished logs from the Pi to the PC (one way, never deletes; the live file is skipped), set
`pi_share` in `config.toml` and run `python sync_pi_logs.py` (`--plans` also copies `tahminler/`). They land in
`loglar/pi/` and can be added to the v0.2 report with `python benchmark/report_v02.py --extra loglar/pi`.

The twin never sends G-code to the printer. The only command it sends is the pause request (on by default
since v0.2; set `PAUSE_MODE = "golge"` in `realtime_websocket.py` to only log it).

---

## Files

| Path | Content |
|---|---|
| `realtime_log_ws.csv` | Live log (one row per Moonraker message), one file per print: at each print start and when the twin stops, the part is archived as `realtime_log_ws_onceki_YYYYMMDD_HHMM.csv` and summarized into `twin_gecmis.csv`. The first row carries `code_version`. Column contract: `UNITY_ARAYUZ.md`. |
| `twin_state.json` | Learned `k` and `k_fil`, `KAYIP` reference, last material, last model state. Backed up to `durum_yedek/` at each start (last 30). |
| `bakim_izleme.csv`, `bakim_durumu.json` | Maintenance indicator data and last result. |
| `ariza_raporlari.txt` | Alarm reports, classifications, shadow-pause and maintenance messages. |
| `tahminler/` | G-code plans (`tahmin_*.csv`) and toolpaths (`yol_*.bin`). |
| `benchmark/` | Calibration runs, set split, fault library. |
| `loglar/` | Archived logs (`baskilar/` prints, `deneyler/` experiments, `pi/` raw copies from the Pi by `sync_pi_logs.py`). |
| `KARARLAR.md` | Decision log (Turkish). |
| `UNITY_ARAYUZ.md` | Python ↔ Unity data contract. |

---

## Limitations

- **Ambient temperature is assumed constant (25 °C).** Room changes (21–25 °C) are absorbed by `k`; an
  enclosure (ABS/ASA at 40–50 °C inside) would shift `k` strongly and confuse the drift alarm and the
  maintenance advice.
- **First-layer level uncertainty.** Fan-off first layers depend on the exact `k` of the day; consecutive
  prints differ by 0.4–0.9 W. The twin learns it slowly by design.
- **Heat-up and cool-down see different heat capacities; the model is fitted to heat-up.** Measured without the
  model (experiments 9 and 14): in free cooling the hotend behaves as a constant ~6.3 J/K, the same with the part
  fan off and at 100 %. While the heater ramps at full power, extra heat is needed that grows with temperature
  (6–8 W at 230 °C, as if the capacity were 1.5–2 J/K larger) and scales with both heater power and heating
  rate; it does not come back after arrival, so it is lost rather than stored. The model's
  temperature-dependent capacity copies the heat-up behaviour, so it cools 5–13 % too slowly: cool-down is the
  weakest phase (September core set RMSE 6.4 °C, model too warm). A heater power that falls when hot was ruled
  out. This affects neither the alarms (missing power is evaluated only near the print target) nor the learning of
  `k`. The ~0.5–1 s heater-to-thermistor lag is not modelled either (adding it hurt cool-down).
  **Kept as a limitation in v0.3** (`KARARLAR.md` [K59], [K60]): seen through the model, the hotend loses about 5 %
  more heat while its temperature changes than while it holds, in both directions. Re-calibrating the numbers
  cannot fix it (any fit trades heat-up against holds), and the structures tried (a heat-break node, radiation
  from the cartridge, sensor lag) failed the acceptance gate. Where it matters: the temperature residual drifts
  2–4 °C during heat-up and cool-down (display and scores only), and the plan's heat-up time is off by ~6 s on
  average. Where it does not: missing power, alarms, pause and the learning of `k` / `k_fil` (all evaluated only
  when the temperature is steady), and the plan's power while printing (bias +0.08 W). Finding the mechanism
  needs a second measurement, e.g. a thermistor on the heat break or heatsink. When calibrating another hotend,
  fit the losses from holds (calorimetry) and keep each log's `k` fixed, so the fit does not trade one regime
  against the other.
- **Validated range:** PETG and TPU, 235–245 °C. Above 245 °C, PLA at 100 % fan and enclosed materials are
  untested. Printing below 230 °C (the learning and alarm limit now follows the target) is checked only by
  replaying calibration experiments with 200–215 °C holds, not yet by a real print.
- **Detectability scales with the heat path.** A full filament stop is seen only above ~1.6 mm/s of filament
  (~3.8 mm³/s); slow, thin features stay below the 2.5 W threshold. A blocked part-fan duct is caught at 60 %
  fan (+4 W, induced test 2026-10-20) but stays within ±1 W at 25 % fan, where the blower barely cools.
- **Single maintenance event** behind the maintenance advice thresholds.
- **Portability:** the structure is generic for FFF hotends; the parameters are specific to this hotend and
  must be re-fitted (a ~1 h calibration print: holds at several temperatures, fan steps, free cooling).

---

## Glossary

| Turkish | English |
|---|---|
| ikiz | twin |
| eksik güç (`eksik_guc`) | missing power (expected − actual heat leaving) |
| residual | measured − model temperature |
| `GUC_AZALDI` / `GUC_ARTTI` | heat demand dropped / rose (fast fault alarm) |
| `KAYIP_ARTTI` / `KAYIP_AZALDI` | heat loss increased / decreased (drift alarm on `k`) |
| kayıp, kayıp çarpanı `k` | loss, loss-to-air factor |
| `k_fil` | heat-into-print factor |
| tutma | hold (constant temperature) |
| ısınma / varış / soğuma | heat-up / arrival / cool-down |
| baskı | print |
| ilk katman | first layer |
| tabla | bed |
| fan / fansız / fanlı | fan / fan-off / fan-on |
| akış (`akis`) | flow |
| malzeme | material (filament type) |
| ayar / sınav | calibration set / test set |
| arıza | fault |
| bakım (önerisi) | maintenance (advice) |
| duraklatma, gölge mod | pause, shadow mode |
| plan, tahmin | plan (G-code based prediction) |
| karne | scorecard |
| KARARLAR | decisions |

---

## License

Copyright (C) 2026 Baturay TATLIATUN. The code is licensed under the
[GNU General Public License v3.0](LICENSE). Parts of the G-code plan are derived from
[Klipper](https://github.com/Klipper3d/klipper) (GPLv3): the PID in `closed_loop.KlipperPID`
(`klippy/extras/heaters.py`) and the look-ahead in `gcode_plan.py` (`klippy/toolhead.py`).

The Windows viewer's 3D model uses third-party parts under their own licenses: the Satsana 5015 Fan Duct
for Ender 3 / CR-10 by eric-anastas ([CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)) and a
modified Ender 3 direct drive mount for BMG extruder (Drivinator remix) by asd913
([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)), both from Printables; the Ender 3 frame is from
[Creality's open-source release](https://github.com/Creality3DPrinting/Ender-3) (GPLv3).
