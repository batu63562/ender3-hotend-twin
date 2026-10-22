# Roadmap

The core stays the same in every version: **a thermal model computes the heater power the hotend should
need; when the real heater deviates, the twin raises an alarm; slow drifts are learned by `k`.** Versions do
not add features for their own sake. Each one proves **one claim** about that core, and a feature that does
not serve the claim of its version waits.

| Version | Claim | Status |
|---|---|---|
| [v0.1](#v01--it-works-on-one-printer) | It works on one printer | releasing |
| [v0.2](#v02--it-is-reliable) | It is reliable | done (2026-10-22) |
| [v0.3](#v03--the-physics-is-not-ender-3-specific) | The physics is not Ender 3 specific | in progress |
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
`k_fil` (every print restarts `k_fil` from a fixed anchor, [K27]) with the drift alarm (`KAYIP_*`),
maintenance advice, shadow-mode pause, the G-code plan, the bed
twin (observation only) and the Unity replay/live view. Validated on one Ender 3 with PETG and TPU at
235–245 °C. See [Limitations](README.md#limitations).

Release tasks:
- [x] Printer address in `config.toml` (`printer_host`) instead of in the code.
      New public names (config keys, arguments, file names) are English from the start.
- [x] `requirements.txt`, `.gitignore` (live logs, learned state, caches).
- [x] `LICENSE` (MIT while the repository is private; GPL-3.0 from the open release, [K58]).
- [x] A small curated sample dataset published as a release asset, so the twin can be replayed without a
      printer: three demo recordings with plans and toolpaths (`ender3-twin-sample-data-v0.1.zip`), and
      every log behind the scores (`ender3-twin-eval-data-v0.1.zip`, reproduces `evaluate.py` exactly).
- [x] README score table regenerated with documented commands (`evaluate.py --live-start`).
- [x] A ready-to-run Windows build of the Unity viewer in the release, opening the sample dataset.
      `Twin/Toolpath` and `Twin/Ghost` in *Always Included Shaders*. (Built 2026-10-19; the scene's data folder
      must be emptied again before any rebuild, it now points at the Pi.)
- [x] Remaining Turkish runtime messages in the Unity start menu translated to English.
- [x] The build looks for `sample_data` next to the exe when no data folder is set (else Desktop/dt).
- [ ] README: short demo clip of a real fault being caught, results table, "try it without a printer".
      Deferred to the end of v0.3: v0.2 caught better demo material (a layer shift, a gappy benchy roof).
- [ ] GitHub topics and a social preview image.

**Done when:** someone without a printer can replay a sample log and see the alarms.

---

## v0.2 — It is reliable

**More experiments**, aimed at the largest known errors and at the fault library:
- Cool-down: the model cools too slowly (largest remaining phase error). **Diagnosed, kept as a limitation**
  (experiment 14, 2026-10-20): heat-up and free cooling see different effective heat capacities, and the model
  is fitted to heat-up. No alarm impact. The structural fix moves to v0.3 (see the calibration print below).
- Bed: the `k` learned while printing carries into cool-down (+1…+4 °C).
- Heat-up: the ~0.5–1 s heater-to-thermistor lag (`deney9` showed the mirror signature; not yet modelled).
- Induced faults on purpose, to grow the labelled library. **Done** (experiments 10–13, 2026-10-20): extruder
  latch open (caught, shadow pause at 173 s), part-fan duct blocked at 60 % fan (caught, 172 s) and at 25 % fan
  (below threshold), draft on the hotend (caught), overhang tulip (no contact-loss signature). Filament cut,
  partial clog and sock removal were dropped: same thermal signature as the latch, unsafe on an all-metal
  hotend, or already covered by the September experiments.

**Shadow → active pause.** Follow 10–20 prints in shadow mode, report the false-pause rate and detection
delay, then switch `PAUSE_MODE` to `"aktif"`. (First day on the Pi, 2026-10-19: 7 prints, 0 shadow pauses.)
- [x] Report script (`benchmark/report_v02.py`). 2026-10-20: 136 logs, 59.9 print hours; 13 of 15 labelled
      faults caught (median delay 26 s), 3 shadow pauses, all on real faults; no false pause. The current system
      alone (`--since 2026-10-18`, after the `k_fil` anchor): 15.6 print hours, 0 false alarms, 3 of 3 induced
      faults caught; older logs replay with the starting `k` their twin had then, which was often far off.
- [x] Hand-label the remaining unlabelled alarms (2026-10-21; most were a short bump at the first-layer start
      in old logs whose starting `k` was far off).
- [x] One real Pause/Resume with the new park position (X0 Y220): verified mid-print on 2026-10-21.
- [x] The active pause request tested end-to-end against stub servers: exactly one pause request on a real
      fault replay (the latch-open experiment), none on a clean print.
- [ ] 10–20 normal prints on the Pi in shadow mode (11 so far, 2026-10-22). Archive report 2026-10-22: 160 logs,
      66.5 print hours, 17 of 20 labelled faults caught (median delay 28 s), 4 shadow pauses, all on real faults,
      **no false pause**; the longest labelled benign event lasts 83 s (pause after 150 s). The current system
      alone: 22.2 print hours, 5 of 6 labelled faults caught (the one missed is below the threshold by design),
      no false pause. Newly caught in real prints: a layer shift and a gappy benchy roof.
- [x] Switch `PAUSE_MODE` to `"aktif"` (2026-10-22).

**Continuous (24/7) operation:**
- [x] Split logs per print instead of only at start-up.
- [x] Run the maintenance check and the `twin_state` backup daily, not only at start-up.
- [x] Write the code version into the log (`MODEL_VERSION` is not updated automatically).
- [x] Run the twin on the Raspberry Pi as a service (a sleeping PC stops it mid-print). Running since
      2026-10-19; the service waits for clock sync at boot (the Pi has no battery clock). First day: 7 prints,
      no restart, no alarm.
- [x] Unity on the PC reads the CSV from the Pi over the network (read-only share).
- [x] Survives Klipper restarts, emergency stop and a power cut of the Pi (re-subscribes, atomic state file,
      clock-sync wait verified after a real power loss; 2026-10-20).
- [x] Finished logs copied to the PC with `sync_pi_logs.py`.
- [ ] A few days of unattended running, including a power cycle of the printer. (Running since 2026-10-19;
      two accidental USB unplugs reset the Pi, both recovered on their own with the learned state intact.)
- [x] A retention rule for idle logs: at most one row per second while nothing happens (an idle night went
      from ~43 MB to ~12 MB; deployed 2026-10-21).

**Done when:** false-alarm rate per print hour and detection delay are reported on a held-out set, and the
pause is active. **Done 2026-10-22** (git tag `v0.2`; scores frozen in `benchmark/baseline_v0.2.json`). The
unattended-running item continues during v0.3: the twin keeps running on the Pi with the v0.2 code.

---

## v0.3 — The physics is not Ender 3 specific

Already done while v0.2 was closing (no model change yet): `calibrate.py` (fits the thermal parameters to a
calibration log, with a per-log `k`, a prior and an identifiability report), `yazici/calibration_print.py` (the
calibration G-code, checked on a virtual printer with `yazici/virtual_print.py`) and a first real calibration
print, `benchmark/baseline_snapshot.py` (the "reproduces the v0.2 scores exactly" check), the profile and
calibration file design (`PROFILE_DESIGN.md`), the part-fan curve after maintenance (experiment 15). The first
real calibration showed the measurement chain works; the remaining obstacle is structural: heat-up and free
cooling see different heat capacities, so a fit on the calibration print improves cool-down and arrival but
costs heat-up.

Separate the generic physics from this printer's numbers. The idea is that users should not have to type
in thermal resistances they do not know. The printer measures itself, like Klipper's `PID_CALIBRATE`:

1. **Profile = starting guess.** The user selects what they know: heater power (40/50/60 W), block type,
   nozzle, thermistor, part fan, materials. This sets initial parameter values and their uncertainty.
2. **Calibration print = system identification.** A ~55 min G-code without extrusion
   (`yazici/calibration_245.gcode`, generated by `yazici/calibration_print.py` from experiments 9, 14 and 15:
   full-power and slow ramps — the slow one raises the target 1 °C at a time, so no printer.cfg change —
   free cooling, part-fan steps, small steps, holds going down, full-power steps up) fits the heat capacities,
   resistances, fan curve and `k` for that hotend.
   - *(This structural question was closed as a limitation in v0.3; see Progress: [K59], [K60].)*
   - Structural candidate to test here, not before: **temperature-dependent cartridge contact resistance**,
     R31(T) = R31₀ + β·(T − T₀), replacing today's temperature-dependent heat capacity (same parameter count).
     The aluminium block expands about twice as much as the steel cartridge, so the gap and the contact
     resistance grow with temperature. At full power the cartridge then has to run further above the block the
     hotter it gets, storing energy that exists only while the heater is on and vanishes in cooling. Caveats
     from experiment 14: it needs a larger cartridge capacity than today's 0.83 J/K (else the cartridge would
     run ~300 K above the block), part of the extra heat scales with heating rate rather than power, and an
     energy balance of the holds right after arrival finds no return of the 80–180 J missing during the ramp
     (a store in the cartridge would give back ~100–200 J when the PID drops from 40 W to the hold power). So
     the extra heat looks like a loss that happens only while ramping, not a store: the candidate in this
     simple form is weak, and the heat path into the heat break / heatsink during a ramp is the other lead.
     Judged on a second printer as well, so that it is not an Ender 3 artefact.
   - Stronger candidate (experiment fits, 2026-10-20): **heat lost from the hot cartridge** rather than from the
     block surface (radiation moved from the block to the cartridge). It fits the calibration logs at 1.4 °C
     instead of 2.3 °C and improves arrival and cool-down on recent prints without hurting heat-up, but it costs
     the fan-on print phase in a first, patched scoring. To be built into the twin properly and judged through
     the acceptance gate together with the new part-fan curve (threshold ~14 %, amplitude +26 %).
3. **Online learning = fine tuning.** `k` and `k_fil` keep adapting, as today.

Materials: a material table (density, heat capacity, typical temperature) from which the filament heating
term is computed. `k_fil` itself turned out to depend on the print, not the material: every print starts
from one anchor (v0.1, [K27]).

Progress (2026-10-22, all on the Pi):
- [x] Printer numbers from `profile.toml` + `calibration.json`, reproducing v0.2 exactly ([K52]).
- [x] English identifiers, done together with this refactor since it touches the same code (public names and
      comments were already translated before v0.1). Behaviour-neutral: replaying the logs gives exactly the
      same scores before and after. CSV columns and alarm codes stay as they are (data contract) ([K53]).
- [x] Learning and alarms were tied to 230 °C (PETG); they now follow the print target (target − 15 °C, not
      below 180 °C), so that PLA (200–215 °C) is covered ([K54]). Passed the gate on replays; **open:** a real
      TPU print below 230 °C on this printer.
- [x] Synthetic slow-fault test kept as a tool (`benchmark/slow_fault_test.py`); the early-print detection
      limit is documented, not tuned away ([K55]).
- [x] `calibrate.py --write` produces the calibration file (`--params ""` gives today's values exactly) ([K56]).
- [x] Structural candidates + the new part-fan curve through the gate ([K59]): none passed. Heat lost from the
      cartridge needed an implausibly hot cartridge and hurt fan-cooled printing; a full re-calibration traded
      heat-up for holds. **Closed as a limitation** ([K60]; README *Limitations*): transients only, no effect on
      alarms or learning. Open lead for later: a phenomenological transient-loss term (β·|dT/dt|, zero in holds)
      through the gate, labelled as such; the root cause needs a second sensor (heat break / heatsink).

Carried over from v0.2: a few days of unattended running (ongoing on the Pi), and the demo clip of v0.1.

**Open release at the end of v0.3** ([K58]): the code repository becomes public under **GPL-3.0** (the G-code
plan reuses Klipper's GPLv3 PID and lookahead code). One public repository holds the README, the code and a single
v0.3 release (Windows viewer, sample data, and the evaluation data that reproduces the scores); the separate
showcase repository is dropped.

**Done when:** the Ender 3 runs from a profile + calibration file and reproduces the v0.2 scores exactly
(the profile step did; later changes are deliberate, gated, and get a new frozen scorecard at the end of v0.3).

---

## v0.4 — It works on another printer

Run on at least one different Klipper printer (different hotend and fan) with **no code change**: profile +
calibration print only. Testers are recruited after the open release at the end of v0.3.

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
