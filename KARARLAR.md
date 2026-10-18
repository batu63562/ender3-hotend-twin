# Karar defteri (decision log)

`realtime_websocket.py` içindeki parametrelerin ve kuralların **geçmişi**: neden bu değer, neler denenip
reddedildi, hangi karneyle kabul edildi. Kod yorumları yalnızca "ne + neden"i kısaca söyler ve buraya
`[Kxx]` ile atıf yapar. (Metinler koddan taşındı; tarih ve puanlar o günün değerlendirme setlerine göredir.)

Puan dili: RMSE °C; "ayar" = kalibrasyon seti, "sınav" = yalnız son kontrol seti, "Eylül çekirdek" =
21–25.09 kalibrasyon logları (`benchmark/*.csv`). Ayrıntılı protokol: README "Evaluation".

---

## K01 — Model sürümü (`MODEL_VERSION`)
- v3 (2026-09-27): fan eşiği + K_FIL 0.0058 (adım 8).
- v4 (2026-09-28): tabla voltaj düşümü + K_FIL 0.0054 (adım 10).
`twin_state.json` farklı sürümden kalmışsa devralınmaz.

## K02 — Tabla açıkken besleme düşümü (`BED_SAG = 0.042`)
Tabla ısıtıcısı açıkken 24 V besleme düşer, nozzle kartuşunun gücü azalır:
gerçek güç = pwm · P_MAX · (1 − BED_SAG · tabla_pwm). Adım 10 (2026-09-28): multimetre ile ölçüldü — kartuş
voltajı tabla kapalı ~23.9 V, tabla %100'de 23.4 V → (23.4/23.9)² = 0.958. Tabla kayıtlı 2 baskıda 150 °C bekleme
hatası −4.1 → +0.1 °C, ısınma −12.8 → −7.6 °C, ilk katmanlar −3.7 → −1.6 °C. Tabla kaydı yoksa etki 0.

## K03 — Fan eğrisi (`FAN_EXP`, `FAN_F0`, `HA2_FAN`)
Eşik üstünde fan soğutması ~ ((f−F0)/(1−F0))^FAN_EXP (karekök benzeri: zorlanmış taşınım h ~ hava_hızı^0.5).
Adım 8 (2026-09-27): deney 6 fan eğrisi (%30–100, 245 °C tutma) + deney 5 (%20, %50) uyumu; eski saf karekök
(F0 = 0) %20'de soğutmayı 4–5 kat fazla tahmin ediyordu. FAN_F0: 5015 blower bu PWM'in altında kanal direncini
yenemiyor (%20'de ölçülen soğutma tam hızın 0.08–0.13'ü). HA2_FAN: fan dönemine fit (adım 2c), fanlı/fansız tutma
güç farkından beklenen ~0.033 ile uyumlu.

## K04 — Filaman ısı katsayısı (`K_FIL = 0.0054`)
Filamanın aldığı ısı = K_FIL · net besleme hızı · (T1 − T_amb).
- Adım 10: tabla voltaj düzeltmesiyle tabla kayıtlı baskılara uyum (0.0058, tabla kaynaklı güç kaybının bir kısmını
  "filaman fazla ısı çekiyor" diye yutuyordu).
- Adım 8: yeni fan eğrisiyle 8 baskı loguna (25–26.09) ORTAK uyum. Eski 0.0043 tek baskıya eski fan kuralıyla
  uydurulmuştu; %20–50 fanda fazla hesaplanan fan soğutması eksik filaman yükünü telafi ediyordu.
- Serbest ekstrüzyonda (deney 1b/5) fandan bağımsız ~0.0057–0.0071 ölçüldü. PETG teorik tam ısınma
  1.27e-3 · 2.405 · cp, cp 1.6–1.8 J/gK → ~0.0049–0.0055.
- Bilinen ödün: fan %30–60 baskılarda (25.09) hata 1.7 → 3.5 °C; PETG profilindeki (fan %0/%20) baskılarda 4–7 → 1–2 °C.

## K05 — Kayıp çarpanı `k` öğrenmesi: tutma pencereleri
245 °C'deki gerçek kayıp günden güne ~±0.5 W değişiyor (hava akımı, oda vb.) ve 245 °C'de 1 W hata ~7.6 °C kalıcı
sapma demek. Sıcaklık TUTULURKEN enerji dengesinden k yavaşça öğrenilir. Yalnız ≥230 °C (`ADAPT_TMIN`): 150 °C'de
öğrenilen oran 245 °C'ye uymuyordu (bkz. K11).

## K06 — Baskı sırasında öğrenme (2026-10-09)
Baskılar ısınır ısınmaz başladığı için tutma penceresi hiç oluşmuyor; k Ekim boyunca 1'de kaldı (blok kaybı ~%7
azalmıştı: residual +5..9 °C). Baskı sırasında da aynı enerji dengesinden ÇOK YAVAŞ öğrenilir (`PRINT_TAU` = 30 dk:
arıza — saniye/dakika — k'ya sızmaz, saatler/günler süren kayma emilir).

## K07 — Ortak öğrenme [k, k_fil] Kalman (2026-10-09)
Tek k, baskıda filamana giden fazla ısıyı da yutuyordu. Ekim'de tutmada doğru k 0.92, baskıda öğrenilen 0.95–0.965;
baskı k'sı yavaş ilk katmanlara taşınınca +7 °C residual. Eylül baskı pencereleri (ayar seti, 112 × 300 s)
regresyonu: fazla kayıp akışla ölçekleniyor, fanla değil (fan ×0.91, filaman ×1.21 = 0.0065; serbest ekstrüzyon
deney 1b/5 de 0.0057–0.0071 ölçmüştü). O yüzden k (blok + fan kaybı) ve k_fil (K_FIL çarpanı) BİRLİKTE, küçük bir
Kalman filtresiyle öğrenilir:

    ölçüm  y = (verilen − depolanan − heatsink yolu) / süre   [W]
    model  y = k · (blok + fan kaybı) + k_fil · K_FIL · akış · (T − T_amb)

Tutma pencerelerinde akış 0: sadece k. k'nın hızı eski üstel öğreniciyle aynı kalır (tutma τ 60 s, baskı 1800 s;
`_kalman` kovaryans tabanı). Saf Kalman ısınmadan hemen sonraki sapmalı ilk pencerelere fazla güvenip kazancı
daraltıyordu (çekirdek 3.77 → 4.13); k varyansı her pencerede span/τ kazancını verecek değere tabanlanır.

## K08 — k_fil'in anlamı ve sınırları (2026-10-13)
"BASKIYA GİDEN ISI" çarpanı, yalnız filaman değil: akışla orantılı kayıp = filamanın ısınması (PETG cp'sinden
K_FIL ~0.0052, k_fil ~1.0) + nozzle'ın düz yüzünden bastığı sıcak plastiğe iletim (baskı hızıyla, yani akışla
büyür). Bakım sonrası baskı regresyonu (300 s pencereler): k 1.00, k_fil 1.56, artık 0.21 W.
Sınırlar: 12.10'da 0.8–1.3'e çekilmişti ("filaman fiziksel olsun"); 13.10'da tavana dayanan k_fil'in taşan kısmını
k'ya ittiği görüldü (baskıda k 0.96 → 1.03; söndürme kuralı kalkınca 150 °C'de +8 °C). 1.6: ayar zincir 3.90 → 3.91
(nötr), 13.10 zinciri baskı RMSE 6.14 → 3.25, k 1.029 → 1.010. Alt sınır 0.8 (filamanın kendisi ~1.0'ın altına düşmez).
16.10 notu: k_fil yavaş bir "filaman özelliği" değil; fan hızı, parça geometrisi ve malzemeyle değişiyor (K17).

## K09 — A0: fan durumuna göre baskıya giden ısı (2026-10-14)
Fan soğutmuyorken (`fan_conductance == 0`: kapalı ya da eşik altı) baskıya giden ısı yalnız filamanın ısınması,
çarpan 1.0 SABİT; k_fil yalnız fanlı kısımda uygulanır ve oradan öğrenilir. Neden: fanlı baskıda öğrenilen ek kayıp
fansız ilk katmanlara taşınıyor, ilk katmanlarda +8..+10 °C residual ve +1.5 W eksik güç üretiyordu; bakım sonrası
kalibrasyon değerleriyle fansız ilk katman zaten doğru (−1.7 °C). Ek kayıp parçadan bağımsız (14.10 havaya baskı:
parça koptuktan sonra da aynı). Karne: Eylül çekirdek 3.99 → 3.73 (yalnız fanlı tutma 3.21 → 2.16, diğer fazlar
aynı); yeni ayar logları canlı başlangıçtan 7.92 → 4.18 (fansız +8.1 → −1.0) ve 5.21 → 4.16; sınav zincir 3.13 → 2.91
(245 fansız bias +1.7 → +0.1), canlı başlangıç 3.55 → 3.50; arıza kütüphanesi aynı.

## K10 — HOLD_SETTLE iki kez reddedildi
- (2026-10-14, ablasyon) Isınmadan sonra 120 s tutma öğrenmesini atlamak: varıştan sonraki ilk ~60 s'de pencere
  k'sı +0.06..+0.10 yüksek çıkıyor (fiziksel olarak doğru), ama ayar zincir 4.05 → 4.06 (nötr), sınav 3.06 → 3.31
  (kötü): yüksek k, modelde eksik olan "baskıda sabit ek kayıp" terimini kazara telafi ediyordu.
- (2026-10-14 akşam, A0 üstüne aynı 120 s) Eylül TÜMÜ 3.73 → 3.69, ayar zincir 4.70 → 4.71 (nötr), sınav 2.94 → 3.16
  (tutma245 4.04 → 4.64); bakımın KAYIP_ARTTI alarmı kayboldu, bakım sonrası ilk baskıda k 0.964 kaldı (ilk katman
  −10.8 °C, yanlış GUC_ARTTI). Bakımdan hemen sonraki yüksek k (~1.05) büyük ölçüde GERÇEKTİ; bir günde ~1.01–1.02'ye
  indi. Erken pencereler fazla değil, baskılar yavaş öğreniyor.

## K11 — 150 °C ön ısıtmadan öğrenme reddedildi (2026-10-12, ayar zincir)
TÜMÜ 4.07 → 4.55, alarm 6 → 15; oturum bazında 150 °C tutması 245 °C'yi temsil etmiyor (tabla nozzle'ın altında
ısınıyor). O tutma bakım göstergesi olarak `gecmis.py`'de (P150_W).

## K12 — k söndürme kuralı kaldırıldı (2026-10-13)
`K_LOSS_FADE_LOW` (150 °C altında k uygulanmazdı). Öğrenilen k artık her sıcaklıkta aynen uygulanır.
Geçmiş: ayar zincirde iki kez reddedilmişti (C(T)'siz TÜMÜ 4.07 → 4.07; C(T) ile 3.88 → 3.88). Neden kaldırıldı:
kuralın "soğumada k yanlış" gerekçesi ölçümle çürüdü. Isıtıcı kapalı soğumada fit edilen kayıp çarpanı (T > 150)
tutmadaki k'yı izliyor: Eylül baskıları soğuma ~0.99 / tutma ~0.97, 12.10 ~0.93 / 0.85–0.96. Ekim kaybı gerçek, her
sıcaklıkta geçerli. Sınav zincir (C(T) açık): TÜMÜ 3.46 → 3.21, 150 °C tutma 8.71 → 4.30 (bias +7.45 → +0.87), soğuma
4.07 → 3.49. Bedeli: ısınma 4.41 → 5.82 (bias −1.75 → −4.81) — açık gözlem (deney 9, K18).

## K13 — Sıcaklığa bağlı ısı kapasitesi C(T) (2026-10-13, KABUL)
Isıtıcı doygunken (pwm = 1) enerji dengesi: gerçek/model oranı 60–100 °C'de ~1.10, 145 °C'de 1.00, 230–240 °C'de ~0.92
(soğuk ısınmalar, tabla kapalı). Efektif güç/kapasite oranı sıcaklıkla ~%0.12/K düşüyor: Al'nin cp'si bunun ~1/3'ü,
kalanı kartuş direnci (sıcaklık katsayısı) ya da termistör ölçeği olabilir. Tek terimle
C(T) = C · (1 + CAP_T_COEF · (T − CAP_T0)), her düğüm kendi sıcaklığında; kararlı duruma etkisi yok. 4 soğuk ısınmada
fit, 9 baskı ısınmasında (fit'e girmedi) RMSE 7.23 → 3.76 °C. Karne (ayar zincir, TÜMÜ/ısınma/varış/diğer
tutmalar/soğuma): 4.07/5.57/5.70/3.65/4.45 → 3.88/3.34/3.18/3.27/4.07, alarmlar aynı. Çekirdek TÜMÜ 3.91 → 3.76.
Ödün: Eylül diğer tutmalar 0.81 → 2.58.

## K14 — KAYIP alarmının referansı (dara → yavaş filtre → kabul)
Öğrenme yavaş bir arızayı (çorap düştü, hava akımı, kanal kayması) zamanla "normal" sayıp residual'dan silebilir;
o yüzden k'nın kendisi de izlenir.
- "Dara" (2026-10-09): bant 1.0'a değil oturum başında devralınan k'ya göre. Ekim'de hotend kalıcı olarak k = 0.92'ye
  oturdu: 1.0'a göre bant her oturumda boş alarm çaldırıyordu (deney 8: 12 kez aç-kapa).
- Yavaş referans (2026-10-12): oturum başı referansı kısa oturumlarda merdiven gibi kayıyordu (12.10 sabahı k
  0.939 → 0.890 → 0.858 iki kısa oturumda, her adım bandın altında, alarm yok). Saatlik alçak geçiren filtreye geçildi.
- Kabul (2026-10-13, filtrenin yerine): 13.10 bakımından sonra k 0.88 → 1.0'a çıkıp orada kaldı; 8 saatlik filtre
  yüzünden alarm bütün baskı boyunca çaldı (ISA-18.2 "nuisance"). Artık: k referanstan bant kadar sapınca alarm çalar;
  k yeni seviyede `K_ACCEPT_TIME` boyunca (sıcak çalışma) `K_ACCEPT_RANGE` içinde kalırsa yeni seviye KABUL edilir.
- Histerezis (`K_ALARM_CLEAR = 0.06`): deney 8'de k 0.92 bant kenarında 13 kez aç-kapa.
- Sınır: kayan referans yavaş bozulmayı adım adım kabul edip yutar → sabit referanslı bakım göstergesi (K20).

## K15 — Eksik güç alarmı (`GUC_*`) ve eşikleri
Neden sıcaklık değil güç (2026-10-11): PID gerçek sıcaklığı sabit tuttuğu için ısıtıcı gücü gerçek yükün ölçümü
(kalorimetre); açık çevrim model bilmediği ~0.3–0.6 W'ı 52 s'lik ısıl zaman sabitiyle biriktirip residual'da ±2 °C
gezinir. Eksik güç bu birikimi yapmaz ve ~15–20 s'de oturur. Kayıplar öğrenilmiş k ve k_fil ile ölçeklenir (k'sız
halde Ekim'de taban ~+1.2 W kaymıştı). Eşikler (2026-09-26, ~50 dk normal veri 230 °C üstü): 20 s / 2.5 W / 5 s onay.
Arıza kütüphanesi: filaman akmıyor 6/6 (18–28 s), çorapsız 73 s. Hedef değişiminden ve ısıtıcı doygunluğundan sonra
30 s bekleme: 27.09 baskı başında yanlış GUC_AZALDI.

## K16 — Akış girdisi: 10 s işaretli ortalama ve `pos_e` farkı
- Adım 9 (2026-09-27): 2 s → 10 s. Boşluk dolgusu gibi uzun ileri-geri desenlerde 2 s pencere geri kısımları kırpıp
  akışı ~3 kat fazla sayıyordu (27.09: 10 yanlış alarm, residual +12 °C). 10 s: yanlış alarm 0, RMSE 3.23 → 2.68 °C;
  bedeli filaman akmama tespitinde +3–5 s. Örnekleri tek tek kırpmak net akışı %12 fazla gösteriyordu.
- 2026-10-11: `live_extruder_velocity` ~4 Hz'de alınan ANLIK örnek; geri çekmeler ~0.03 s sürdüğü için örnek ya hiç
  denk gelmiyor ya da −35 mm/s'yi 0.25 s tutuyor (aliasing) → 30 s dilimlerde 0.3–0.45 W hayalet yük, ~200 s'lik
  salınım. `live_position[3]` (pos_e) KONUM: iki mesaj arası fark gerçek besleme. Skor: ayar 4.27 → 4.13, sınav
  5.26 → 5.11, sınav arızaları 28 → 16.
- `EXT_EPS`: Klipper boşta 8.9e-16 gibi kırıntılar veriyor; deney 8'de tüm tutma pencerelerini reddettirmişti.

## K17 — Baskı öğrenme penceresi 300 → 60 s (2026-10-16, KABUL)
300 s pencerelerin çoğu fan/hedef değişimi ya da kısa olay içerdiği için atılıyordu; k_fil 5 dk'da bir 0.1–0.15'lik
basamakla, geç oturuyordu. 60 s pencere geçişlerin arasına sığıyor (Ekim ayar: 82 → ~435 geçerli güncelleme).
Filtrenin zaman sabiti ~√(R/Q): R ~1/pencere, pencere başına Q ~pencere → DEĞİŞMEZ, yavaş arızalar eskisi kadar
yutulmaz. Q_FIL'i büyütmek (k_fil'i hızlandırmak) REDDEDİLDİ: yapay yavaş arıza testinde (logdaki pwm, fanlı baskıda
rampa ile X W azaltıldı, 5 enjeksiyon) yutma. Puanlar (Eylül TÜMÜ / ayar canlı baş. TÜMÜ, arıza / sınav TÜMÜ /
3 W-10 dk, 4 W-30 dk rampa):

| Varyant | Eylül | Ayar, arıza | Sınav | 3 W 10 dk | 4 W 30 dk |
|---|---|---|---|---|---|
| 300 s, Q ×1 | 3.73 | 3.95, 15 | 3.60 | 5/5 7.0 dk | 5/5 21.7 dk |
| **60 s, Q ×1 (seçilen)** | 3.62 | 3.76, 12 | 3.30 | 5/5 8.8 dk | 5/5 24.9 dk |
| 60 s, Q ×2 | 3.62 | 3.74, 12 | 3.29 | 5/5 9.5 dk | 4/5 |
| 60 s, Q ×4 | 3.61 | 3.72, 12 | 3.27 | 4/5 | 3/5 |
| 60 s, Q ×16 | 3.61 | 3.68, 12 | 3.25 | 2/5 | 2/5 |
| 60 s, Q ×256 | 3.60 | 3.57, 10 | 3.20 | 1/5 | 2/5 |

Eylül fanlı tutma 2.16 → 1.63, kötüleşen faz yok; etiketli arızalar tüm varyantlarda aynı sürede yakalanıyor.

## K18 — Isınma hatası ve deney 9 (2026-10-15)
Tutma k'sı 120–245 °C arası düz (1.07–1.13) → k(T) yok. Isıtıcı gücünün sıcakken düşmesi çürüdü. Adımlarda ayna
imzası (yukarı ilk 5–10 s model −3.3 °C önde, aşağı +1.6 geride) = ısıtıcı ile sensör arasında ~0.5–1 s gecikme.
Termistör gecikmesi (yalnız çıkış) puanlandı: τ 0.5 s Eylül ısınma 2.16 → 1.97 ama soğuma 6.45 → 6.68, TÜMÜ
3.73 → 3.79 → UYGULANMADI.

## K19 — Duraklatma (gölge mod)
- 2026-10-16: GUC_AZALDI kesintisiz `PAUSE_AFTER` sürerse duraklat (ilk geri besleme: dijital gölge → ikiz).
  Örnek az → önce GÖLGE (yalnız kayıt). KAYIP_* asla duraklatmaz.
- 2026-10-17: 60 → 150 s. Gölge modun ilk günü iki ZARARSIZ ilk katman olayı (TPU feet tek nesne ~56 s, TPU küp ilk
  katman başı ~110 s; baskılar sağlam) "duraklatırdım" dedi. 150 s en uzun zararsız olaya 40 s pay bırakır; kalıcı
  gerçek arızalar (filaman durdu 246 s+, topak/havada baskı 286 s+, filaman bitmesi) yine yakalanır.

## K20 — Bakım göstergesi (2026-10-16)
k'nın tek bilinmeyen olduğu her pencerenin kendi k'si (tutma; fansız baskı) kaydedilir. Açılışta FANSIZ BASKI
pencerelerinin günlük medyanı SABİT referansla (kalibrasyondaki temiz hotend, 1.00) kıyaslanır; son 3 veri gününde
aynı yönde ±0.06 dışındaysa bakım önerisi. Geriye dönük (28.09–15.10): Ekim bozulması (heatsink'te kurumuş PETG,
heat break macunsuz) 0.92/0.92/0.92/0.91 → 11.10'da öneri (bakım 13.10'da yapıldı); Eylül ve bakım sonrası yanlış
öneri yok. Bakım gününde referansı yenilemek daha kötüydü (bakım sonrası geçici rejim 1.04 → 0.98). Tutma k'sı
kaydedilir ama kurala girmez (seyrek; deney 9 havada 1.09). Tek bakım olayına dayanır: tavsiye.

## K21 — Malzeme profili ve durum yedeği (2026-10-16)
k ortak (hotend özelliği). k_fil malzemeye bağlı → malzeme başına saklanır; malzeme G-code başlığından okunur,
değişince k_fil o malzemenin kaydından ve oturum başı belirsizliğiyle (P0_FIL) başlar. Aynı malzemede nozzle sıcaklığı
(240/245) ayrılmaz: filaman terimi zaten (T − T_amb) ile ölçekli. Her açılışta `twin_state.json` yedeklenir (son 30).


## K22 — Baskı dışı tutma pencereleri (2026-10-18) — REDDEDİLDİ
Olay (17.10): filaman değişimi sırasında (240 °C, Z 98, elle çekme/itme Klipper'da görünmez) 3 tutma penceresi k'yı
1.012 → 0.955 çekti; KAYIP 30 dk sonra kabul etti (k_ref 0.953); ardından PETG baskısında k_fil 1.60 sınırda kaldı,
eksik güç −0.3…−0.56 W. Durum `durum_yedek/twin_state_20261017_202503.json`'dan geri alındı (kullanıcı onayı).
Tabla gerilim düşüşü (BED_SAG) hipotezi test edilemedi: tabla açık ve kapalı tutma pencereleri farklı dönemlerden
(bakım öncesi/sonrası), aynı oturumda ikisi birden yok.
Aday "tutma öğrenmesi yalnız print_state = printing/paused": ayar canlı başlangıç TÜMÜ 3.50 = 3.50, arıza 13 = 13,
tutma245 4.14 → 4.43 (kötü), fansız baskı eksik güç p95 1.45 → 1.65, etiketler aynı, 250 pencere atıldı; filaman
değişimi logunda log sonu k 0.980 → 1.000. Deney G-code'ları Klipper'da baskı sayıldığından onları ayırmaz.
Ayar iyileşmesi yok, bir faz kötü → UYGULANMADI. Sınırlılık: sıcakken yapılan elle işlemler (filaman değişimi,
temizleme) k'ya öğrenilebilir; nadir, durum yedeğinden geri alınır.


## K23 — "Canlı başlangıç" `degerlendir.py --canli` oldu (2026-10-18)
README'deki 3.76 / 3.30 (K17) belgelenmiş bir komutla üretilemiyordu: canlı başlangıç (her log kaydedildiği andaki
k_loss / k_fil ile, KAYIP referansı = k) geçici betikle hesaplanmıştı ve setler o günden beri büyüdü. `--canli`
eklendi (yalnız değerlendirme aracı, model aynı). Doğrulama: 15.10 setleriyle ayar 3.76 / 12 arıza, sınav 3.30 / 12
(K17 ile birebir); taze mod çıktısı değişmedi. Bugünkü setler (README): Eylül 3.62, ayar 34 log 3.50 / 13,
sınav 45 log 3.36 / 14; taze başlangıçla sınav 4.83 (Ekim k'sı 1'den uzak). Değerlendirme verisi temiz bir
klonda (yalnız git + veri arşivi) satırı satırına aynı skoru veriyor.


## K24 — Dışa açık adlar İngilizce (2026-10-18, v0.1 öncesi)
Dil kuralı: kullanıcının yazdığı adlar yayından sonra değiştirilemez. Bu tarihten önceki kayıtlarda eski adlar geçer:
`degerlendir.py` → `evaluate.py`, `ariza_siniflandir.py` → `fault_classifier.py`, `gcode_tahmin.py` → `gcode_plan.py`,
`gecmis.py` → `session_history.py`, `kapali_dongu.py` → `closed_loop.py`, `tabla_model.py` → `bed_model.py`,
`ikiz_baslat.bat` → `start_twin.bat`. Bayraklar: `--set ayar|sinav|hepsi` → `calibration|test|all` (setler.csv'deki
değerler Türkçe kalır), `--canli` → `--live-start`, `--zincir` → `--chain`, `--kaydet` → `--save`, `--karsilastir`
→ `--compare`, `--etiketler` → `--labels`, `--pencere` → `--window`, `--yol` → `--toolpath`, `--goreli-e` →
`--relative-e`, `--doldur` → `--backfill`, `--ekle` → `--add`. Davranış aynı: eski kodla (`pre-english` etiketi) skorlar,
plan, takım yolu, arıza sınıflandırıcı ve kapalı döngü çıktıları birebir karşılaştırıldı.
Adım 2 (aynı gün): Python yorumları ve docstring'leri İngilizce. Docstring'ler çıkarılınca AST adım 1 ile birebir aynı
(kod tek karakter değişmedi); Eylül skoru ve etiketli arıza çıktısı aynı. Konsol/telefon mesajları Türkçe kalır (v0.5),
tanımlayıcılar v0.3 refactor'uyla.


## K25 — Abonelikte çift `gcode_move` anahtarı (2026-10-18, HATA DÜZELTMESİ)
14.10'da Z ofseti kaydı için `"gcode_move": ["homing_origin"]` ikinci bir anahtar olarak eklenmiş; Python sözlüğünde
son anahtar kazandığı için `absolute_extrude` (planın başlangıç ekstrüzyon modu, 14.10) hiç abone edilmedi ve plan
her baskıda mutlak modu varsaydı (`gmove.get("absolute_extrude", True)`). Etkilenen aralık: z_offset sütununun ilk
göründüğü 14.10 ~15:06'dan START_GCODE'daki M82'nin etkin olduğu ana kadar (15.10, Klipper yeniden başlatma); o
aralıkta oturumun ilk baskısı olmayan ve önceki baskı M83'te bıraktıysa plan prime çizgisinde 14 mm eksik filaman
saydı (Unity takım yolu ilerlemesi 14 mm kayar). Bugün etkisiz: Pi'deki printer.cfg'de M82 var, planlayıcı makrodaki
M82'yi işliyor. Önemi v0.4: makrosunda M82 olmayan yazıcılarda mod yine Klipper'dan okunmalı. Model ve alarmlar
etkilenmez (plan girdisi). Düzeltme: iki alan tek listede; canlı ikizle henüz doğrulanmadı.


## K26 — k_fil tavanı (1.6) 17.10'da: incelendi, v0.1'de DEĞİŞİKLİK YOK (2026-10-18)
17.10 TPU küp serisinde k_fil baskıdan baskıya 1.42 → 1.60 tırmandı, son baskılarda %83–100 tavanda; ardından PETG
baskısı TPU değeriyle başladı (PETG kaydı yok: malzeme takibi 16.10'da TPU ile başladı) ve %98 tavanda kaldı; fanlı
baskıda eksik güç −0.2…−0.44 W. Deneme (overrides, model değişikliği değil) `K_FIL_MAX` 1.6 → 2.0: k_fil serbest
kalınca yalnız 1.65–1.73'e çıkıyor (kaçmıyor); Eylül fazları aynı, ayar canlı başlangıç TÜMÜ 3.50 → 3.47 (fanlı tutma
3.39 → 3.35), arıza 13 = 13, 17.10 loglarında olay 0 = 0. PETG baskısındaki k düşüşü (1.012 → 0.980) tavandan DEĞİL:
2.0 ile de 0.975; nedeni K22'deki filaman değişimi tutma pencereleri. Sonuç: tavanın hatası ~0.1 k_fil ≈ 0.2–0.4 W,
alarm eşiğinin (2.5 W) çok altında; alarmlar değişmiyor → yayın haftasında model değişikliği yapılmadı. Tavanı 2.0'a
çıkarmak v0.2 adayı (tam kabul kapısı: sınav, etiketler, yapay yavaş arıza testi). Malzeme tablosu (ρ·A·cp) bunu
çözmez: malzemeler arası fark ≤ %10, oysa aynı malzemede k_fil iki günde 0.98 → 1.60 (TPU) oynuyor → baskıya giden
ısıyı malzeme sabiti değil geometri/hız belirliyor (K08, K17). Açık soru: PETG profili fanı %20'de (eşiğin hemen
üstü, soğutma ~0) ama k_fil ~1.7 istiyor; A0'a göre fansızda 1.0 yetiyordu → k_fil'i fan değil baskı evresi (ilk
katman tabla üstü / sonraki katmanlar plastik üstü) belirliyor olabilir.


## K27 — k_fil çapası (v0.2 adayı, 2026-10-18) — AYAR AŞAMASI
Fikir: k makinenin özelliği (oturumlar arası taşınır), k_fil baskının özelliği (geometri/hız; K26) → baskı başında
k_fil = a + (k_son − a)·exp(−Δt/τ); a = malzeme çapası, τ = 0 her baskı çapadan, τ = ∞ bugünkü yapı, arası "yay".
Değerlendirme düzeneği (model değişmedi): ayar seti, canlı başlangıç, baskı başı/sonu `print_state`'ten (34 logun
15'inde var; diğerlerinde aday = temel). Çapa = temel koşuda baskı sonu k_fil medyanı: PETG 1.35, TPU 1.32 (neredeyse
aynı → malzeme ayrımı belki gereksiz).
| aday | TÜMÜ | fanlı tutma | arıza | eksik güç fanlı rms | tavanda |
|---|---|---|---|---|---|
| temel (τ ∞, tavan 1.6) | 3.497 | 3.39 | 13 | 0.616 W | %9 |
| τ 0 / 15 dk / 1 sa / 4 sa, tavan 1.6 | 3.463 | 3.36 | 12 | 0.609 W | %7 |
| τ ∞, tavan 2.0 | 3.474 | 3.35 | 13 | 0.612 W | %0 |
| τ 0…4 sa, tavan 2.0 | 3.449 | 3.33 | 12 | 0.607 W | %0 |
τ 0–4 sa arası sonuç aynı → yay sabiti önemsiz, en basiti τ = 0 (her baskı çapadan). Kaybolan arıza: 15.10 TPU duvarsız
gyroid küpte etiketli YANLIŞ GUC_AZALDI (3 → 2). Log bazında karışık: gyroid 6.35 → 5.86, yarıda durdurulan 3.98 → 3.72,
17.10 PETG 2.82 → 2.73; havaya basılan 4.12 → 4.22, voronoi 2.60 → 2.75. Eylül logları print_state içermediği için
etkilenmez (yapı gereği). Kalan kapı adımları: etiketli arızalar, yapay yavaş arıza (baskı BAŞINDA enjekte edilen dahil:
her baskıda yeniden öğrenme baştan var olan arızayı yutabilir), sonra kullanıcı onayı ve sınav. Tek değişiklik kuralı:
önce çapa, sonra ayrı adım olarak tavan 2.0. Uygulama notu: Twin baskı başını bilmeli (on_message'a print_state),
canlı ve evaluate aynı kodu kullansın.
**Kapı adım 5 (2026-10-18, ayar):** yapay yavaş arıza (K17 yöntemi: ölçülen ısıtıcı gücü rampayla azaltıldı, sonra
sabit), ayardaki print_state'li 7 baskı, fan açılınca + baskı ortası, 3 W/10 dk ve 4 W/30 dk = 20 senaryo:
| yer | arıza | temel | çapa |
|---|---|---|---|
| baskı ortası | 3 W / 10 dk | 3/3, 8.9 dk | 3/3, 8.9 dk |
| baskı ortası | 4 W / 30 dk | 3/3, 23.8 dk | 3/3, 23.7 dk |
| fan açılınca | 3 W / 10 dk | 3/7, 11.0 dk | 3/7, 10.1 dk |
| fan açılınca | 4 W / 30 dk | 7/7, 27.5 dk | 7/7, 27.5 dk |
Çapa hiçbir senaryoda kötü değil (2'sinde daha erken). Gyroid'in 0.1 dk'sı yapay arıza değil, oradaki mevcut zararsız
olay. Ayar etiketleri: gyroid yanlış GUC 50 → 38 s, feet 61 = 61 s (ikisi de duraklatma eşiğinin altında); Eylül
etiketleri yapı gereği aynı. → Adım 5 ayarda GEÇTİ; sıradaki: kullanıcı onayı, sonra sınav (adım 4).
**Ayrı ve ESKİ bir zayıflık bulundu:** fan açılırken başlayan 3 W/10 dk arıza temelde de 4/7 yutuluyor (baskı
ortasında 3/3 yakalanıyor). Neden: k_fil'in Kalman belirsizliği boşta Q_FIL·süre ile büyüyor (bir gecede ~0.03,
P0_FIL'den büyük) → her baskı başında filtre hızlı → baskı başındaki yavaş arıza k_fil'e (0.8'e kadar) öğretiliyor.
Çapanın bunu ne bozduğu ne düzelttiği: iki yapı da başlangıçta aynı belirsizlikte. Sonraki aday (ayrı adım): çapa iyi
bir başlangıç verdiği için baskı başı belirsizliği küçültmek (P0_FIL ↓ ya da boşta büyümeye tavan) → erken yutma azalır;
bedeli yeni geometriye yavaş uyum, karne gerekli.
**Çapa duyarlılığı (ayar, τ 0, tavan 1.6):** tek çapa 1.15 / 1.25 / 1.33 / 1.45 / 1.55 → TÜMÜ 3.456 / 3.458 / 3.465 /
3.508 / 3.554, arıza 12 / 12 / 12 / 13 / 15, fanlı eksik güç bias +0.023 / +0.036 / +0.046 / +0.064 / +0.076 W. Malzeme
başına 1.35/1.32 = tek 1.33 (3.464) → malzeme ayrımı gereksiz (k_fil'i geometri belirliyor, K26). 1.15–1.33 düz, 1.45 üstü
uçurum: yüksek çapa eksik gücü GUC_AZALDI yönüne kaydırıp yanlış alarm üretiyor. Medyan baskı SONU değerinden; k_fil
baskı içinde tırmandığı için doğru başlangıç biraz aşağıda.
**Adım 5, tek çapa 1.25:** yakalama sayıları aynı (3/3, 3/3, 3/7, 7/7); 1 senaryo YAVAŞ (feet, fan açılınca, 4 W/30 dk:
29.4 → 34.7 dk), 3'ü hızlı (voronoi 11.2 → 10.1, PETG ortası 12.1 → 11.2 ve 24.5 → 23.7). Gyroid yanlış GUC 1 → 0.
Düşük çapa baskı başında eksik gücü negatife kaydırır → arızanın önce bu sapmayı aşması gerekir (yüksek çapanın tersi):
çapa erken baskıda alarm istatistiğine bir bias, yanlış alarm ↔ tespit gecikmesi ödünleşimi. 1.35/1.32'de yavaş
senaryo yoktu (gyroid 50 → 38 s). Karar kullanıcıda: 1.33 (kapıyı temiz geçmesi beklenir, adım 5 tek 1.33 ile tekrar)
ya da 1.25 (yanlış alarm 0, bir senaryoda +5 dk gecikme: kapı kuralına açık istisna).
**Seçim: tek çapa 1.33 (kullanıcı). Kapı sonucu — GEÇTİ (2026-10-18):**
- Adım 5 (ayar): 20 yapay senaryonun hiçbirinde temelden yavaş/yutan yok (voronoi 11.2 → 10.1, PETG ortası 24.5 → 23.7
  daha hızlı; yakalama 3/3, 3/3, 3/7, 7/7 aynı). Ayar etiketleri: gyroid yanlış GUC 50 s → YOK; feet 61 = 61 s.
- Adım 4 (sınav, onaydan sonra): TÜMÜ 3.36 → 3.34, fanlı tutma 2.18 → 2.12, soğuma 4.38 → 4.43 (+0.05; soğumada akış
  yok, fark k ile k_fil'in Kalman'daki bağından k'ya küçük yansıma), diğer fazlar aynı; arıza 14 = 14; fanlı eksik güç
  bias −0.018 → −0.004 W, rms 0.435 → 0.427 W. Sınav etiketleri birebir aynı (11.10 ve 15.10 B arızaları aynı anda
  yakalanıyor, 12.10 ikisinde de yok, 16.10 zararsız olay aynı).
- Adım 3: Eylül logları print_state içermediği için yapı gereği aynı.
Sıradaki: uygulama (Twin baskı başını bilmeli; malzeme kaydı baskı başında çapayı ezmemeli → `malzeme_kfil` deposu
çapayla gereksizleşiyor), uygulanan kodun bu düzenekle aynı skoru verdiğinin doğrulanması. v0.1'i etkilememesi için
ayrı dalda.
**Uygulandı (dal `v0.2-kfil-capa`, kullanıcı onayı):** `K_FIL_ANCHOR = 1.33`; `Twin.on_message(print_state=)` printing'e
girişte (pause'dan dönüş hariç, ilk mesaj hariç) ve `set_material`'de `anchor_kfil()` (k_fil = çapa, P = P0_FIL; k'ya
dokunmaz). Malzeme başına k_fil deposu (`malzeme_kfil`, `load_materials`) kaldırıldı; son malzeme yalnız CSV için
saklanır. Canlı döngü ve `evaluate.replay` aynı yoldan print_state geçirir; `gcode_plan` yeni baskıyı çapayla planlar.
`MODEL_VERSION` değişmedi (durum anlamı aynı; öğrenilmiş k korunur). Doğrulama: Eylül çıktısı ve arıza sınıflandırıcı
etiket çıktısı v0.1 ile BİREBİR aynı; ayar 3.46 / 12 arıza, sınav 3.34 / 14 arıza, fanlı tutma 2.12, soğuma 4.43 —
düzenekteki sayılarla aynı. Birim testi: standby → printing çapaya döner; paused → printing, ilk mesaj ve bilinmeyen
durum dönmez.
**Canlı test ve v0.1'e alınması (18.10, kullanıcı kararı):** dal kodu iki PETG baskısında (Articulated Coffee Buddy
1 sa 42 dk ve 37 dk, ikinci baskı oturum yeniden başlatıldıktan sonra): ikisinde de baskı başında k_fil 1.59 / 1.38 →
1.33, sonra 1.33 → ~1.45 → 1.38 ve 1.33 → 1.46; tavana değmedi; baskıda eksik güç +0.09 / −0.13 W, rms 0.42 / 0.41 W;
alarm ve gölge duraklatma yok; k 1.012 → 1.007 → 1.010 (17.10'daki tavan-PETG baskısında 1.012 → 0.980'di). Oturum sonu
yolu (durum kaydı, twin_gecmis özeti, durum yedeği) çalıştı. Demo kayıtlarının alarmları çapayla aynı. `main` dalın
ucuna ileri alındı (birleştirme); README skorları güncellendi: ayar 3.46 / 12, sınav 3.34 / 14.


## K28 — Duraklatma kuralının geriye dönük raporu (2026-10-19, v0.2 gün 1)
Bütün arşiv (loglar/baskilar + deneyler, 115 log, 88.6 saat; print_state'li 53 log, 48.8 baskı saati) güncel kodla,
her log kaydedildiği andaki k/k_fil ile yeniden oynatıldı (bozuk 2809_string ve 3 "_geometri" dosyası atlandı).
GUC_AZALDI olayı 46; süre medyanı 35 s, %90 76 s, %95 110 s. 150 s kuralıyla duraklatma 2, ikisi de gerçek ve kesintisiz
arıza (2909 topak/havada baskı 286 s, 3009 stand2 filaman durdu 246 s). **Yanlış duraklatma 0.** Kullanıcı 150 s'den önce
durdurduğu için duraklatmaya varmayan gerçek arızalar: 1510 goofy bust filaman bitti (63 s) ve 0810_1451 (119 s,
14:35; eksik güç +3.8 W, baskı 1.9 dk sonra iptal; Mainsail'de 08.10 benchy PETG 0.3 mm iptal, kullanıcı: temizlikten
sonraki ilk benchy'ler hatalıydı, elle durdurdu → gerçek). En uzun bilinen zararsız olay 83 s (1610_2005 TPU küp) →
eşik payı 67 s. Aktif moda geçiş kararı gün 4'te (yeni baskılar + arıza deneyleri ile).


## K29 — 7/24 çalışma: baskı başına log, günlük işler, kod sürümü (2026-10-19, v0.2 gün 1, MODEL DIŞI)
- **Log baskı başına** (kullanıcı kararı): her yeni baskıda (`print_state` → printing, pause dönüşü hariç) canlı CSV
  kapanır, `realtime_log_ws_onceki_<son yazma>.csv` olarak arşivlenir, aynı adla yeni dosya açılır; ilk satırı baskının
  ilk mesajı. 240 satırdan (~1 dk) kısa parça ayrılmaz. Aynı dakikada ikinci arşive saniye eklenir (üzerine yazma yok).
  Arşivlenen parçanın oturum özeti arka planda `twin_gecmis.csv`'ye (çıkışta da son parçanınki). Unity canlı görünüm
  baskı başında sıfırlanır (LiveCsvTail dosya küçülünce baştan okur).
- **Günlük işler:** durum yedeği ve bakım göstergesi açılışta ve tarih değişince (`daily_jobs`).
- **`code_version` sütunu** (yalnız her dosyanın ilk satırı): `MODEL_VERSION` + `realtime_websocket.py` ve `bed_model.py`
  içeriğinin 8 haneli sha1'i (satır sonu CRLF/LF farkı yok sayılır; git gerektirmez, Pi için).
Test: sahte Moonraker (14.10 cap/chain/glass logunu websocket'ten 60× hızla yeniden yayınlar, `main()` gerçek döngüsüyle):
3 baskı → 3 dosya, her biri tek `printing → complete`; satır kaybı yok (8665 / kaynak 8666, fark abonelik cevabı);
`code_version` her dosyanın ilk satırında; ikinci arşiv saniye ekli; 3 oturum özeti; parçalar `evaluate.py` ile
oynatılıyor. Model sınıfları (Twin, KLossLearner, EnergyMonitor, FaultMonitor, model_step) AST'de aynı, Eylül 3.62.


## K30 — İkiz Raspberry Pi'de servis (2026-10-19, v0.2 gün 2, MODEL DIŞI) — KURULUM BETİKLERİ, PI'DE DENENMEDİ
Neden: PC uyuyunca baskı ortasında ikiz kesiliyordu (17.10 13:05). Kurulum: PC'de `deploy.ps1` (scp ile kod + istenirse
öğrenilmiş durum, sonra Pi'de `pi/install.sh`): Python 3.11+ kontrolü, venv, websockets (zorunlu) + numpy/pandas (plan,
sınıflandırıcı, özet için; kurulamazsa ikiz yine çalışır), `config.toml` = `127.0.0.1:7125` (Moonraker doğrudan) ve
telefon sayfası 8090 (8080 genelde crowsnest webcam), systemd servisi `Nice=10`, `IOSchedulingClass=idle` (Klipper'ın
zamanlaması öncelikli; baskı başındaki plan hesabı da bu öncelikte), `Restart=on-failure`. İsteğe bağlı salt-okunur
Samba paylaşımı (Unity veri klasörü `\\<pi>\ender3-twin`). Kod: `web_port` config anahtarı (0 = kapalı); SIGTERM
(systemctl stop) Ctrl+C gibi biter (durum kaydı + oturum özeti). Unity: `LiveCsvTail` boyutu açılan dosyadan okur
(Windows ağ paylaşımında FileInfo.Length ~10 s önbellekte kalabiliyor), "script çalışıyor" eşiği paylaşımda 15 s.
`.gitattributes`: .sh ve .service LF. Pi'de doğrulanacak: kurulum, Klipper "Timer too close" yok, baskı başı plan
süresi, Unity'nin paylaşımdan canlı akıcılığı.
