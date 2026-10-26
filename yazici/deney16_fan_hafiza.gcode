; Experiment 16 (K76): does the fan-off loss at 245 C drop for good once the part fan has run?
; 26.10 calibration print: 245 C fan-off holds took 20.0 W before the fan first ran, 18.1-18.3 W after.
; Holds A (fan never ran since cold), B (after 2 min fan 100 %), C (after a second fan run).
; A > B = C: the first fan run changes something for good (repeatable). A = B = C: 26.10 was a one-off.
; No extrusion, bed off, nozzle 50 mm above the bed centre. START COLD (nozzle < 40 C).
; Back up the twin's twin_state.json before, restore it after (fan runs move the learned k). ~24 min.
SET_IDLE_TIMEOUT TIMEOUT=7200
M140 S0
M104 S0
M107
G28
G90
G1 Z50 F600
G1 X117 Y117 F6000
G4 P60000          ; calm
; --- A: heat-up and hold, the fan has not run ---
M117 d16 A: 245 fan never ran
M109 S245
G4 P300000         ; hold 245, fan 0
; --- first fan run ---
M117 d16 fan 100 percent
M106 S255
G4 P120000
M107
G4 P60000          ; calm after the fan
; --- B: hold after the first fan run ---
M117 d16 B: 245 after fan
G4 P300000
; --- second fan run ---
M117 d16 fan 100 percent again
M106 S255
G4 P120000
M107
G4 P60000
; --- C: hold after the second fan run ---
M117 d16 C: 245 after fan 2
G4 P300000
M104 S0
M107
M117 d16 done
