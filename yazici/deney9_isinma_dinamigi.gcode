; Deney 9 - isinma/soguma dinamigi. 150->245 isinmada ogrenilmis k ile model hizli (sinav isinma bias -4.8 C);
; residual isitici %100'e ciktiktan sonraki ilk ~10 s'de olusuyor, sogumada yok. Baski loglari hep ayni uyarimi
; tasidigi icin (150 tutma -> %100 -> 245 -> soguma) sebepler ayrilamiyor. Bu deney farkli sicakliklardan hem
; YUKARI (isitici %100, acik cevrim) hem ASAGI (isitici kapali, saf soguma) adimlar ve PID'in doymadigi kucuk
; adimlar uretir.
; Ayirt edilecekler:
;  - isitici gucu sicaklikla dusuyor mu (kartus direnci): yukari adim hatasi taban sicakligiyla buyur
;  - R31 / gecikme: hata yalniz isitici acikken (yukari) var, asagi adimlarda yok
;  - k(T): tutmalarda kararli guc her sicaklikta modelle karsilastirilir
; Ekstruzyon YOK, parca fani KAPALI, tabla KAPALI (tabla acikken 24 V duser: BED_SAG karismasin).
; Nozzle tablanin ortasinda, 50 mm yukarida bekler (sizan PETG tablaya yapismasin). Sure ~45 dk.
; Ikizi (ikiz_baslat.bat) bu dosyayi baslatmadan ONCE ac.
SET_IDLE_TIMEOUT TIMEOUT=7200
M140 S0
M106 S0
G28
G90
G1 Z50 F600
G1 X117 Y117 F6000
; --- yukari merdiven: isitici %100 adimlari, farkli taban sicakliklarindan ---
M109 S120
G4 P180000          ; 120 tutma
M109 S150
G4 P180000          ; 150 tutma
M109 S200
G4 P180000          ; 200 tutma
M109 S245
G4 P240000          ; 245 tutma (4 dk: en uzun zaman sabitine yetsin)
; --- asagi merdiven: isitici kapali, saf soguma (hedefe yaklasinca PID yeniden devreye girer) ---
M109 S215
G4 P180000          ; 215 tutma
M109 S170
G4 P180000          ; 170 tutma
M109 S150
G4 P180000          ; 150 tutma
; --- baskilardaki klasik adim: tekrarlanabilirlik ---
M109 S245
G4 P180000          ; 245 tutma
; --- kucuk adimlar: PID doymaz ---
M109 S235
G4 P120000          ; 235 tutma
M109 S245
G4 P120000          ; 245 tutma
; --- serbest soguma: isitici kapali, uzun egri ---
TURN_OFF_HEATERS
TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=90
G1 X0 Y220 F3000
M84
