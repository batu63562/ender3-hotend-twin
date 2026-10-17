# Roadmap

The core stays the same in every version: **a thermal model computes the heater power the hotend should
need; when the real heater deviates, the twin raises an alarm; slow drifts are learned by `k`.** Versions do
not add features for their own sake. Each one proves **one claim** about that core, and a feature that does
not serve the claim of its version waits.

| Version | Claim | Status |
|---|---|---|
| [v0.1](#v01--it-works-on-one-printer) | It works on one printer | releasing |
| [v0.2](#v02--it-is-reliable) | It is reliable | next |
| [v0.3](#v03--the-physics-is-not-ender-3-specific) | The physics is not Ender 3 specific | planned |
| [v0.4](#v04--it-works-on-another-printer) | It works on another printer | planned |
| [v0.5](#v05--other-people-can-install-it) | Other people can install it | planned |
| [v0.6+](#v06--motion-twin-accelerometer) | Motion is twinned too | direction |

Only the next version has a firm scope. Later versions are directions and may change with what the
earlier ones show.

Every model change in every version goes through the acceptance gate described in the
[README](README.md#evaluation-methodology): physical reason, calibration-set improvement, no September
phase made worse without explanation, no test-set regression, labelled and synthetic slow faults still
caught, one change at a time. Decisions are recorded in [`KARARLAR.md`](KARARLAR.md).

---

## v0.1 — It works on one printer

The current state: a 3-node hotend model, the missing-power alarm (`GUC_*`), online learning of `k` and
`k_fil` with the drift alarm (`KAYIP_*`), maintenance advice, shadow-mode pause, the G-code plan, the bed
twin (observation only) and the Unity replay/live view. Validated on one Ender 3 with PETG and TPU at
235–245 °C. See [Limitations](README.md#limitations).

Release tasks:
- [ ] Printer address in a config file / command-line argument instead of `PRINTER_IP` in the code.
      New public names (config keys, arguments, file names) are English from the start.
- [ ] `LICENSE`, `requirements.txt`, `.gitignore` (live logs, learned state, caches).
- [ ] A default `twin_state.json` for a fresh start.
- [ ] A small curated sample dataset (calibration runs, a few prints, labelled faults) published as a
      release asset, so the twin can be replayed without a printer.
- [ ] A ready-to-run Windows build of the Unity viewer in the release, opening the sample dataset.
      `Twin/Toolpath` and `Twin/Ghost` in *Always Included Shaders*.
- [ ] Remaining Turkish runtime messages in the Unity start menu translated to English.
- [ ] README: short demo clip of a real fault being caught, results table, "try it without a printer".
- [ ] GitHub topics and a social preview image.

**Done when:** someone without a printer can replay a sample log and see the alarms.

---

## v0.2 — It is reliable

**More experiments**, aimed at the largest known errors and at the fault library:
- Cool-down: the model cools too slowly (largest remaining phase error).
- Bed: the `k` learned while printing carries into cool-down (+1…+4 °C).
- Heat-up: the ~0.5–1 s heater-to-thermistor lag (`deney9` showed the mirror signature; not yet modelled).
- Induced faults on purpose, to grow the labelled library: filament cut, partial clog, extruder slip,
  sock removed, fan duct misaligned.

**Shadow → active pause.** Follow 10–20 prints in shadow mode, report the false-pause rate and detection
delay, then switch `PAUSE_MODE` to `"aktif"`.

**Continuous (24/7) operation:**
- Split logs per print / per day instead of only at start-up.
- Run the maintenance check and the `twin_state` backup daily, not only at start-up.
- Write the code version into the log (`MODEL_VERSION` is not updated automatically).
- Run the twin on the Raspberry Pi as a service (a sleeping PC stops it mid-print); Unity stays on the PC
  and reads the CSV over the network.

**Done when:** false-alarm rate per print hour and detection delay are reported on a held-out set, and the
pause is active.

---

## v0.3 — The physics is not Ender 3 specific

Separate the generic physics from this printer's numbers. The idea is that users should not have to type
in thermal resistances they do not know. The printer measures itself, like Klipper's `PID_CALIBRATE`:

1. **Profile = starting guess.** The user selects what they know: heater power (40/50/60 W), block type,
   nozzle, thermistor, part fan, materials. This sets initial parameter values and their uncertainty.
2. **Calibration print = system identification.** A ~40 min G-code (generalized from
   `yazici/deney9_isinma_dinamigi.gcode`: temperature steps up and down, holds, fan steps, free cooling)
   fits the heat capacities, resistances and `k` for that hotend.
3. **Online learning = fine tuning.** `k` and `k_fil` keep adapting, as today.

Materials: a material table (density, heat capacity, typical temperature) and a default `k_fil` for a
material with no history. Per-material `k_fil` storage already exists.

English code pass, done together with this refactor since it touches the same code: identifiers and
comments are translated to English. It must be behaviour-neutral, so replaying the logs gives exactly the
same scores before and after. CSV columns and alarm codes stay as they are (data contract).

**Done when:** the Ender 3 runs from a profile + calibration file and reproduces the v0.2 scores exactly.

---

## v0.4 — It works on another printer

Run on at least one different Klipper printer (different hotend and fan) with **no code change**: profile +
calibration print only. Testers are recruited after the v0.1 release.

**Done when:** alarm performance on the second printer is comparable to the Ender 3, or the differences are
explained physically.

---

## v0.5 — Other people can install it

- Installer / setup guide for the Pi service.
- A lightweight web interface (grown from the phone page): status, alarms, profile and calibration wizard.
- Unity remains the showcase and replay tool; it is not required to use the twin.
- User-facing Python messages (console, phone page, fault reports) in English. Code identifiers, CSV
  columns and alarm codes stay as they are: they are the data contract that the archived logs, the evaluation
  tool and Unity depend on (see the README glossary).

**Done when:** someone sets it up on their own printer without help from the author.

---

## v0.6+ — Motion twin (accelerometer)

A second twin built with the same discipline as the thermal one, using an ADXL345 (already common on
Klipper printers for input shaping):

- commanded trajectory → mass–spring (belt) model → expected acceleration;
- learned belt stiffness / resonance frequency as the counterpart of `k` (belt loosening = slow drift alarm);
- unexplained impulses as a collision / skipped-step (layer shift) alarm.

An accelerometer does not give position (double integration drifts with t²). Geometry still comes from
the G-code. The accelerometer marks *where* on the toolpath a shift or collision happened.

Starts only after v0.4, so that one generalized twin exists before a second one is built.

---

## Out of scope

- Firmware without Moonraker (stock Marlin, closed ecosystems): the twin needs heater PWM, fan and extruder
  position as a live stream.
- Print-quality defects below ~1 W of heat-flow change (stringing, small layer defects). These need a camera.
- Enclosed printers until ambient temperature is measured or modelled (it is assumed constant today).
