# Case study: a nozzle swap fooled the twin. Re-learn or recalibrate?

*October 2026. Decision entries in brackets ([K74] …) are in [KARARLAR.md](KARARLAR.md), the project's decision log
(in Turkish); the numbers below are copied from there.*

## The twin in one paragraph

A three-node thermal model of the hotend computes the heater power the hotend *should* need. Klipper's PID keeps the
nozzle at its set temperature, so the real heater power is a direct measurement of the real heat load. When the real
power deviates from the model by more than 2.5 W, the twin raises an alarm (`GUC_*`, "missing power"). If too little
power is needed for 150 s continuously during a print (`GUC_AZALDI`), something is wrong (filament not flowing, a
blob insulating the block) and the twin pauses the print. Slow, harmless drifts (a draft, an aging sock) are absorbed
by one learned loss factor `k`, which moves slowly enough that a real fault still shows.

## What happened

On 27 October the clogged 0.6 mm brass nozzle was replaced by a 0.4 mm hardened-steel one (a bi-metal "TZ3.0"
type). The first Benchys printed fine, but to the twin they looked wrong: +2.5 to +5 W of missing power, model
temperature 13–21 °C off, more than ten `GUC_AZALDI` events over five attempts. One lasted 190 s, and the twin
**paused a good print**. It was the first false pause since pausing went live five days earlier (all earlier
pauses, live and in 70 replayed print hours, were real faults) [K78].

Why `k` did not simply learn the new nozzle: the learner only accepts windows with no open `GUC` alarm, so during
those prints it learned nothing, and every attempt started again with the brass `k`. The pause itself was the proof
that learning was all it needed: in the 10 minutes the paused printer held 245 °C with the fan at 33 % and no flow,
`k` settled at 0.89 and the missing power went to zero.

## What the swap actually changed

The one-hour calibration print (holds at four temperatures, part-fan steps, free cooling, no filament) on the steel
nozzle, compared with the same print on brass [K78]:

| | brass (21 Oct) | steel (27 Oct) |
|---|---|---|
| fan-off heater power at 245 °C | 17.4–17.6 W | 17.0–17.4 W (~2 % less) |
| part-fan cooling coefficient `HA2_FAN` | 0.0368 | 0.0246 (**−33 %**) |
| extra loss with the fan at 100 % / 40 %, 245 °C | 7.6 / 4.1 W | 5.2 / 2.6 W |

Steel conducts about a fifth as well as brass, so the tip the fan blows on is cooler and loses less heat by
convection. With the fan off, the block and the sock dominate the losses, and those barely changed. The swap changed
the *shape* of the loss, not just its size: fan-off losses wanted `k` ≈ 0.92–1.00, fan at 33 % wanted 0.89. A single
multiplier cannot serve both, so averaging `k` over a print with the fan on leaves an error on each side.

## The question, frozen before the data

Two options, and the decision was **pre-registered on 26 October, before the new nozzle arrived** [K74]:

- **A:** keep the numbers in use and let `k` adapt online.
- **B:** recalibrate: fit the model to the new nozzle's calibration print (`calibrate.py --staged`).

The test data were every steel print after the calibration print (the print B is fitted to was excluded), at least
4 print hours. Both models started from `k` = 1 and learned on their own through the whole chain. **B would replace A
only if it passed all four criteria:**

1. overall temperature RMSE improves by at least 0.05 °C;
2. the fan-on hold, the core case, gets no more than 0.10 °C worse;
3. unlabelled (false) `GUC` events do not increase;
4. no fewer slow faults are caught: `slow_fault_test` injects a fault that ramps in over 10 or 30 minutes (the heater
   power reduced by 3 or 4 W in the replayed log), in four scenarios.

Why freeze it first: in this project, changes that looked certain to help had failed on held-out data several
times. Writing the bar down before seeing the numbers keeps it from being moved afterwards. I also wrote down physical
predictions for the steel nozzle, which were not criteria. About half of them were wrong; see the end.

## Result: A stays

6.03 print hours, 13 logs, decided on 28 October [K84]:

| RMSE, °C | A (adapt) | B (recalibrate) |
|---|---|---|
| heat-up | 18.35 | 20.83 |
| arrival at target | 19.66 | 23.61 |
| hold 245 °C, fan off | 5.98 | 4.14 |
| hold, fan on | 7.39 | **7.81** |
| other holds | 3.81 | 2.92 |
| cool-down | 7.82 | 4.33 |
| **all** | **7.94** | **8.31** |
| false `GUC` events | 7 | 3 |
| slow 3 W / 10 min fault, fan on: caught | 2 of 4 | **0 of 4** |

The absolute errors are large because this chain contained two Raspberry Pi power cuts (the twin restarted against
a hot hotend) and several short cancelled prints. Both models saw the same chain.

B was better in holds and cool-down and gave fewer false alarms, which is what a fresh fit should do. But it was worse
overall and in the fan-on hold, and above all it **missed every slow fault injected with the fan on**. It failed
criteria 1, 2 and 4, so A stays.

## Why recalibration lost the criterion that matters most

The calibration print fits the fan term to the nozzle exactly as it is that day. A fault that creeps in while the fan
is running looks, to that model and to the learner on top of it, like a slightly different fan coefficient, and it
gets absorbed. A is less exact, so its learner has more to correct, but the learner moves slowly enough that a ramp
of a few watts over ten minutes still stands out.

It is the same trade-off as numerical diffusion in CFD: an upwind scheme is stable because its error smooths
everything, including the features you were trying to resolve. A model that explains the past perfectly has, in the
same way, more room to explain a fault away. This was not a one-off. A dry run of the same comparison with the brass
nozzle's own fresh calibration, in October, failed the same way: better cool-down, worse heat-up, and fewer slow
faults caught with the fan on (4 W over 30 minutes: 15 → 11 of 15) [K66], [K82].

## What changed in practice

- **Pausing back on** (28 October). Live, the twin had meanwhile learned `k` 1.007 → 0.903 over a 30-minute stable
  shift, and in about 6 print hours it raised no false `GUC`. The one `GUC` that day was real (the hotend fan cover
  fell off and the fan blew straight onto the block) [K84].
- **Rule for part changes:** run the first few prints in shadow mode, where the twin only logs "would pause". The
  message that announces a new accepted `k` level now suggests a calibration print and a note in the hardware log
  when a part has changed [K83].
- **Another printer is different:** there is nothing to adapt from, so a new printer starts with its own calibration
  print ([TESTING.md](TESTING.md)). This test answers a narrower question: on the *same* printer, after a part change,
  online learning was the better default.
- **Open risk:** replaying A from `k` = 1, the 28 October reference Benchy shows one false `GUC_AZALDI` of 139 s,
  close to the 150 s pause threshold. Live it did not happen, because the live `k` had already settled.

## What I got wrong

The physics predictions written down before the swap [K74, K78]:

| prediction | outcome |
|---|---|
| heat capacity changes by ≤ 3 % | wrong: −8 % |
| fan-off `k` within ±0.03 of brass | right in the calibration print (−2 %) |
| internal resistance `R31` unchanged within the fit's error | wrong (~2.8 standard errors) |
| heat into the print `k_fil` 10–20 % *lower* (steel tip conducts less) | wrong direction: ~1.45 vs 1.19 |
| the "fan memory" effect seen on brass was the old nozzle | right: gone on steel |

The prediction that mattered, that the fan term would change, was not on the list, and the false pause found it
first. One nozzle swap, one printer, six print hours: this is a single case, not a law. But the method transfers:
freeze the question and the bar before the data, compare against what the system already does, and count the faults
a "better" model stops seeing, not only the error it removes.
