; Deney 15 - parca fani egrisi, bakim ve kanal montaji sonrasi (KARARLAR K39/K40).
; Fan modeli (FAN_F0 esik, FAN_EXP us, HA2_FAN genlik) Eylul'de deney5/6 ile olculdu; 13.10 bakimi ve kanalin yeniden
; takilmasindan beri fan etkisi farkli (deney8, K39: %100'de olculen carpan %25-40 baskilara tasinmiyor).
; Bu dosya fani BASKIDA KULLANILAN seviyelerde ve esik civarinda adimlar; her adimda kararli tutma gucu = o fan
; seviyesindeki kayip. Iki sicaklik (245, 215): fan etkisinin sicaklikla olceklenmesi. Merdiven yukari VE asagi
; (tekrarlanabilirlik, histerezis yok mu).
; Ekstruzyon YOK, tabla KAPALI (24 V dusmesi karismasin), nozzle tablanin ortasinda 50 mm yukarida. Sure ~45 dk.
; Ikiz Pi'de calisiyor; dosya baski gibi baslatilir (kendi logu olur).
; DIKKAT: fanli tutmalar Pi'deki ogrenilmis k'yi oynatir (deney14a'da 1.007 -> 0.959). Deneyden sonra durum geri
; yuklenecek (sudo ister): oncesinde k / k_fil kaydedilir.
SET_IDLE_TIMEOUT TIMEOUT=7200
M140 S0
M106 S0
G28
G90
G1 Z50 F600
G1 X117 Y117 F6000
; --- 245: fan merdiveni yukari (esik civari 15/20/25, baski seviyeleri 40/60, tam 100) ---
M117 d15 245 fan 0
M109 S245
G4 P180000          ; fan 0 (ilk tutma biraz uzun: isinmadan sonra oturma)
M117 d15 245 fan 15
M106 S38
G4 P150000
M117 d15 245 fan 20
M106 S51
G4 P150000
M117 d15 245 fan 25
M106 S64
G4 P150000
M117 d15 245 fan 40
M106 S102
G4 P150000
M117 d15 245 fan 60
M106 S153
G4 P150000
M117 d15 245 fan 100
M106 S255
G4 P150000
; --- 245: asagi (tekrar) ---
M117 d15 245 fan 60b
M106 S153
G4 P150000
M117 d15 245 fan 25b
M106 S64
G4 P150000
M117 d15 245 fan 0b
M106 S0
G4 P150000
; --- 215: daha seyrek merdiven (sicaklik olceklemesi) ---
M117 d15 215 fan 0
M109 S215
G4 P150000
M117 d15 215 fan 25
M106 S64
G4 P150000
M117 d15 215 fan 40
M106 S102
G4 P150000
M117 d15 215 fan 100
M106 S255
G4 P150000
M117 d15 215 fan 0b
M106 S0
G4 P150000
; --- bitis ---
TURN_OFF_HEATERS
M106 S0
G1 X0 Y220 F3000
M84
M117 d15 bitti
