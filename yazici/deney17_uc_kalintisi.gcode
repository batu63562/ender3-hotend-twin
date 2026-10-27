; Experiment 17 (K78 open item): is the session-to-session 1.5 W at 245 C fan-off plastic left on the nozzle tip?
; High holds (19-20 W) came after nozzle work or prints (26.10 brass after cold pull, 27.10 steel after Benchys), low
; holds (17.0-17.6 W) after the nozzle had been idle or fan-blown. The bed temperature does not explain it (archive:
; 27.10 steel deney16 stayed at 19 W while the bed cooled 46 -> 29 C; 26.10 brass was high with the bed at 26 C).
; Holds: A as found -> PAUSE: clean the tip -> B clean -> purge 15 mm in the air -> C with what hangs on it ->
; fan 100 % -> D -> PAUSE: clean again -> E clean. Tip residue: A > B, C > B, E = B. No residue effect: A = B = C = E.
; RUN AFTER A NORMAL PETG PRINT, filament loaded, nozzle NOT cleaned, nozzle < 40 C. Bed off, nozzle 50 mm above
; X117 Y117 (same spot as deney16 and the calibration print). At each PAUSE the head parks: clean the tip at
; temperature (brass brush / tweezers, careful, 245 C), write down what you removed, then RESUME. ~38 min.
; The twin restores its learned k at the end (marker below, K79): no state backup needed.
; twin: experiment
SET_IDLE_TIMEOUT TIMEOUT=7200
M140 S0
M104 S0
M107
G28
G90
M82
G1 Z50 F600
G1 X117 Y117 F6000
G4 P60000          ; calm
; --- A: hold as found ---
M117 d17 A: as found
M109 S245
G4 P300000
; --- clean the tip ---
M117 d17 CLEAN tip, RESUME
PAUSE
G1 Z50 F600
G1 X117 Y117 F6000
; --- B: hold clean ---
M117 d17 B: clean
G4 P300000
; --- purge in the air, leave what hangs ---
M117 d17 purge 15 mm
G92 E0
G1 E15 F60
G92 E0
; --- C: hold with the purge residue ---
M117 d17 C: after purge
G4 P300000
; --- fan run ---
M117 d17 fan 100 percent
M106 S255
G4 P120000
M107
G4 P60000
; --- D: hold after the fan ---
M117 d17 D: after fan
G4 P300000
; --- clean again ---
M117 d17 CLEAN tip, RESUME
PAUSE
G1 Z50 F600
G1 X117 Y117 F6000
; --- E: hold clean ---
M117 d17 E: clean again
G4 P300000
M104 S0
M107
M117 d17 done
