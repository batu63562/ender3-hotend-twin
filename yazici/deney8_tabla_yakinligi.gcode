; Deney 8 - tabla yakinligi: fan kapaliyken sicak tablanin ustundeki sicak hava katmani hotend'in
; isi kaybini azaltiyor mu? Nozzle 245 C, tabla 85 C, ekstruzyon YOK. Nozzle tablanin ortasinda
; farkli yuksekliklerde 3'er dakika bekletilir; ikiz tutma gucunu olcer. Sure ~35 dk.
; Ikizi (ikiz_baslat.bat) bu dosyayi baslatmadan ONCE ac.
SET_IDLE_TIMEOUT TIMEOUT=7200
M140 S85
M104 S245
G28
M190 S85
M109 S245
M106 S0
G90
G1 Z50 F600
G1 X117 Y117 F6000
; --- fan kapali, yukaridan asagiya ---
G4 P240000          ; Z50 (referans, tabladan uzak) - ilk 4 dk ayrica oturma
G1 Z10 F600
G4 P180000          ; Z10
G1 Z5 F600
G4 P180000          ; Z5
G1 Z2 F600
G4 P180000          ; Z2
G1 Z1 F600
G4 P180000          ; Z1
G1 Z0.5 F600
G4 P180000          ; Z0.5 (ilk katman yuksekligine yakin)
; --- ayni yukseklikte fan %25 (baskidaki fan) ---
M106 S64
G4 P180000          ; Z0.5 fan %25
G1 Z50 F600
G4 P180000          ; Z50 fan %25
M106 S0
G4 P180000          ; Z50 fan kapali (tekrar: kayma kontrolu)
TURN_OFF_HEATERS
G1 X0 Y220 F3000
M84
