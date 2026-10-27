; Deney 14b - guc kaldiraci (KARARLAR K36; once deney14a'nin basindaki aciklamayi oku).
; ONCE printer.cfg [extruder] bolumune `max_power: 0.6` yaz, RESTART; deneyden SONRA satiri sil, RESTART.
; Isitici en fazla %60 (~24 W): ayni sicaklikta rampa hem daha dusuk guc hem cok daha yavas.
;  (a) dP gucle (x0.6), (b) dP hizla, (c) dP guc x hizla olceklenir -> 14a'nin A1 rampasiyla karsilastirilir.
; Hedefler 245 degil 235 ve 200: 24 W ile 245'te kayip (~20 W) neredeyse tum gucu yer, varis cok uzar.
; Fan KAPALI, ekstruzyon YOK, tabla KAPALI, nozzle 50 mm yukarida. Sure ~20 dk. Nozzle SOGUK baslar.
; twin: experiment
SET_IDLE_TIMEOUT TIMEOUT=7200
M140 S0
M104 S0
G28
G90
G1 Z50 F600
G1 X117 Y117 F6000
; --- soguk baslangic ---
M117 d14b: soguma bekleniyor
M106 S255
TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=40
M106 S0
G4 P60000
; --- B1: odadan 235'e %60, fan kapali ---
M117 d14b B1 rampa guc 60
M109 S235
G4 P180000          ; 235 tutma (L; guc tavani altinda mi kontrol)
; --- B2: serbest soguma 235 -> 120 ---
M117 d14b B2 soguma
M104 S0
TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=120
; --- B3: 120 -> 200 %60 ---
M117 d14b B3 rampa guc 60
M109 S200
G4 P180000          ; 200 tutma
; --- bitis ---
TURN_OFF_HEATERS
G1 X0 Y220 F3000
M84
M117 d14b bitti: max_power satirini sil
