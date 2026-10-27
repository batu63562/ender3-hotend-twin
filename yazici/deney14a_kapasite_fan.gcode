; Deney 14a - isinmada fazladan "kapasite" nereden geliyor? (KARARLAR K36)
; deney9: sogumada gorunen isi kapasitesi duz ~6.4 J/K, isinmada (%100 guc) sicaklikla artiyor (230 C'de ~8 J/K).
; Isinmada eksik kalan guc dP(T) = (C_isinma - C_soguma) * dT/dt. Adaylar farkli seyle olceklenir:
;  (a) isitici gucu sicakken dusuyor      -> dP gucle olceklenir (ayni gucte, yavas rampada da AYNI)
;  (b) rampada heat break'e gecici akis   -> dP isinma hiziyla olceklenir
;  (c) kartus-blok temas direnci R31(T)   -> dP guc x hizla olceklenir
; Bu dosya (max_power 1.0, yaziciyi DEGISTIRMEDEN): fan kapali ve fan %100 rampalar (ayni guc, farkli hiz) +
; her iki fan durumunda tutmalar (kayip L(T)) + iki fan durumunda serbest soguma (C_soguma tekrar).
; 14b (max_power 0.6) ayri dosya: guc kaldiraci.
; Ekstruzyon YOK, tabla KAPALI (24 V dusmesi karismasin), nozzle tablanin ortasinda 50 mm yukarida. Sure ~35 dk.
; Baslarken nozzle SOGUK olmali (asagida 40 C'ye kadar beklenir): ilk rampa oda sicakligindan baslar.
; Ikiz Pi'de calisiyor; dosya baski gibi baslatilir (kendi logu olur). Deneyden sonra Pi'deki k/k_fil kontrol edilir.
; twin: experiment
SET_IDLE_TIMEOUT TIMEOUT=7200
M140 S0
M104 S0
G28
G90
G1 Z50 F600
G1 X117 Y117 F6000
; --- soguk baslangic ---
M117 d14a: soguma bekleniyor
M106 S255
TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=40
M106 S0
G4 P60000           ; fan durduktan sonra 1 dk sakin
; --- A1: odadan 245'e %100, fan kapali (C_isinma tam aralik) ---
M117 d14a A1 rampa fan kapali
M109 S245
G4 P240000          ; 245 tutma fan kapali (L)
; --- A2: serbest soguma fan kapali 245 -> 100 (C_soguma) ---
M117 d14a A2 soguma fan kapali
M104 S0
TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=100
; --- A3: 100 -> 150 -> 200 %100, fan kapali; tutmalar (L) ---
M117 d14a A3 tutmalar fan kapali
M109 S150
G4 P180000          ; 150 tutma fan kapali
M109 S200
G4 P180000          ; 200 tutma fan kapali
; --- A4: fan %100 tutmalar (L_fan) ---
M117 d14a A4 tutmalar fan 100
M106 S255
G4 P180000          ; 200 tutma fan %100
M104 S0
TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=150
M109 S150
G4 P180000          ; 150 tutma fan %100
; --- A5: 150 -> 245 %100, fan %100 (ayni guc, daha yavas rampa) ---
M117 d14a A5 rampa fan 100
M109 S245
G4 P240000          ; 245 tutma fan %100 (L_fan)
; --- A6: serbest soguma fan %100 245 -> 100 ---
M117 d14a A6 soguma fan 100
M104 S0
TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=100
; --- bitis ---
M106 S0
TURN_OFF_HEATERS
G1 X0 Y220 F3000
M84
M117 d14a bitti
