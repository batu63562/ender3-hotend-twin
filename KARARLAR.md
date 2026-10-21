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
Betik (19.10 akşam): `py benchmark/report_v02.py` aynı raporu tek komutla üretir (yanlış alarm / baskı saati ayar–sınav
ayrı, etiketli arızaların alarm ve duraklatma gecikmesi, gölge duraklatmalar; kopyalar zaman aralığından eşleşir).
19.10 dahil: 129 log, 57.4 baskı saati, GUC_AZALDI 46 (medyan 37 s), duraklatma 2 (stand2 + 2909 havada baskı),
etiketli 12 arızadan 10'u yakalandı (medyan gecikme 28 s), yanlış alarm 0.14 / baskı saati (etiketsizler dahil).
2909 havada baskı ve 08.10 olayları etiketsiz: betik "false?" listeler, elle etiketlenene kadar yanlış sayılır.
**20.10 yeniden koşum** (19.10'un 7 Pi baskısı setlere eklendi, dönüşüm deney12 = sınavdan devam: 1122 ayar, 1148
sınav, 1238 ayar, 1600 sınav, 1731 ayar, 1809 sınav, 1909 ayar; deney10–13 dahil): 134 log, 59.3 baskı saati.
19.10 baskılarında alarm 0, duraklatma 0 (Pencil ve LCD Case sarı adayları eşik altında kaldı). Etiketli 15 arızadan
13'ü yakalandı (medyan gecikme 26 s; kaçanlar deney2 kanal folyo — yalnız KAYIP — ve 1210 tabla kirli ilk katman),
duraklatma 3/15 (stand2 167 s, deney10 173 s, deney11b 171 s). Yanlış alarm / baskı saati: ayar 0.111, sınav 0.155
(sınavın 10 "yanlışı"nın 7'si etiketsiz 08.10/13.10 olayı, gerçek oranın üst sınırı; etiketli "arızasız" yanlışlar
yalnız 16.10 TPU ilk katman olayları). Tek "yanlış" duraklatma 2909 havada baskı (286 s) — gerçek arıza, etiketsiz
(yukarıda). Gerçek yanlış duraklatma hâlâ 0. v0.2 bitti ölçütü için kalan: etiketsiz sınav olaylarını elle etiketlemek.


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


## K30 — İkiz Raspberry Pi'de servis (2026-10-19, v0.2 gün 2, MODEL DIŞI) — PI'DE ÇALIŞIYOR (19.10, bkz. K31)
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

## K31 — Pi servisi saat senkronunu bekler; Pi'de ilk gün (2026-10-19, v0.2 gün 2, MODEL DIŞI)
Olay: Pi'nin pilli saati yok. Açılışta saat son kaydedilen zamandan (02:03) başlıyor, NTP sonra 11:11'e atlatıyor;
ikiz o arada başladığı için log adı (`_0203`), ilk satırların `time`'ı ve durum yaşı ("0.0 saat önce") yanlıştı,
Unity'de 9 saatlik sahte boşluk. Model etkilenmedi: adım zamanı Klipper `eventtime`. Çözüm `pi/ender3-twin.service`:
`ExecStartPre` `timedatectl NTPSynchronized=yes` olana kadar en çok 120 s bekler (`-` önekli: internetsiz zaman
aşımı hata sayılmaz, ikiz senkronsuz saatle yine başlar), `TimeoutStartSec=180`, `After=time-sync.target`.
`systemd-time-wait-sync` seçilmedi (zaman aşımı yok, internetsiz Pi'de ikiz hiç başlamazdı). Elektrik kesintisi
sonrası açılışta henüz denenmedi.
Pi'de ilk gün (servis 11:46–20:21, yeniden başlama 0): 7 baskı (PETG), her birinde log döndü, k_fil çapalandı, plan
3–14 s'de hazır; resmi alarm 0, gölge duraklatma 0, k 1.017 → 1.009; klippy.log'da "Timer too close" 0. SSH anahtarla
(kullanıcı `batu63562`), Samba paylaşımı, telefon sayfası (8090) ve Unity'nin paylaşımdan canlı izlemesi çalışıyor. Konsoldaki
telefon adresi Pi'de `<bilgisayar-IP>` yazıyordu (yazıcı adresi `127.0.0.1:7125`: port + loopback); düzeltildi, 21:39'da
deploy edildi.

## K32 — Kasıtlı arıza deneyleri: plan (2026-10-19) ve sonuçlar (2026-10-20) (v0.2, MODEL DIŞI: veri)
Neden: eylül deneyleri (deney1–4) baskı değildi; `print_state`, gölge duraklatma (150 s) ve k_fil çapası/öğrenmesi ile
arıza etkileşimi hiç denenmedi. Bugünkü kodla yeniden oynatma: çorapsız GUC_ARTTI 332 s (güçlü), folyolu kanal yalnız
KAYIP_AZALDI_FAN (+0.2 W), mandal açık GUC_AZALDI 29–35 s (1.65 mm/s: eşik sınırında, duraklatmaya varmaz).
Tasarım (`yazici/ARIZA_DENEYLERI.md`, G-code'lar `yazici/ariza_deneyleri.py` ile): taşıyıcı ~7 mm³/s kare tüp
(tam akış kesilmesi eşiği ancak ~3.8 mm³/s üstünde geçer); arıza pencereleri PAUSE/RESUME ile çerçeveli: etiket
sınırları `print_state` geçişinden, arıza kaldırıldıktan sonraki pencere kontrol. Sıra (kullanıcı kararı):
deney10 mandal açık (B, tahmin +4.6 W, gölge duraklatma), deney11 fan emişi kapalı %25 (~+2.7 W, sınırda) ve %60
(~+5 W), deney13 çıkıntı lalesi 0 → 70° (sarı adayı, +1…1.5 W), deney12 cereyan (C, GUC_ARTTI, büyüklük bilinmiyor).
Elenenler: filaman kesme (deney10 ile aynı ısıl imza), kısmi tıkanma (`M221` ikiz için arıza değil, ikiz akışı step
konumundan okur; gerçek tıkanma all-metal + PETG'de riskli), çorap çıkarma (Pi'deki k'yı kirletir; eylülde zaten
yakalanıyor), kanal çevirme (deney11 aynı fizik, daha güvenli). Etiket ve set satırları deneylerden sonra, set ataması
mekanik dönüşümle.
**Sonuçlar (20.10, Pi'deki canlı ikiz, gölge mod):**
- deney10 mandal açık (fan %25): +4.0…4.7 W, GUC_AZALDI Resume'dan 28 s sonra, **gölge duraklatma 173 s**, sınıflandırıcı B.
- deney11 fan %25'te kanal ağızları (emiş kanala bakıyor, ulaşılamadı; tek/iki ağız, sıra karışık): hep ±0.5…+1 W,
  alarm yok → **%25 fanda kanal/fan arızası eşik altı** (model fan soğutması %25'te ~1.5 W; eşik %19). Etiketsiz.
- deney11b fan %60, iki ağız folyolu: +3.9…4.2 W, GUC_AZALDI 26 s, **gölge duraklatma 172 s**; sınıflandırıcı A|B|C
  (fan sabitken A/B/C aynı şekil, kod bunu söylüyor).
- deney12 cereyan (vantilatör bozuk → saç kurutma soğuk, en güçlü ayar, 50 cm, ~50 s): −4…−5.7 W, GUC_ARTTI ~20 s,
  sınıflandırıcı C. Baskı içindeki ilk GUC_ARTTI.
- deney13 çıkıntı lalesi 0 → 70° (çökmeden tamamlandı): eksik güç düz −0.3 W; k_fil 1.33 → 1.49. k_fil 1.33'te sabit
  sayılırsa −0.16 → −0.8 W: baskıya DAHA FAZLA ısı (aday: katman süresi 12.5 → 26 s, alttaki katman soğuyor).
  **Temas kaybı imzası yok**; deney10'daki havaya baskı da ±0.7 W → Pencil Topper +1.5 W'ın kaynağı başka (topak?).
- Arızasız duraklat/devam pencereleri (5): resmi alarm 0, yanlış gölge duraklatma 0.
- Park sorunu: Mainsail `PAUSE` varsayılanı X230 Y230; her Resume'da baskı sola kaydı (araba gövdeye çarpıp adım
  kaçırıyor olmalı; END_PRINT'in X0 Y220'si sorunsuz). Üretici `PAUSE X=0 Y=220` yazıyor (deney12 bununla basıldı).
  Mainsail Pause düğmesi için kalıcı çözüm: Pi'deki `printer.cfg`'ye `_CLIENT_VARIABLE` (özel park X0 Y220, dz 2)
  20.10 13:32'de eklendi (yedek `printer.cfg.bak_20261020_park`, Klipper yüklü; PC kopyası `yazici/printer.cfg`).
  Gerçek bir Pause/Resume ile henüz denenmedi (aktif duraklatmadan önce bir deneme baskısında doğrulanacak).
- Etiketler (8 satır, hepsi sınav; setler dönüşümü 10 sınav, 11 ayar, 11b sınav, 13 ayar, 12 sınav):
  `fault_classifier --labels` sınav 5/12 doğru, 4 kısmen, 3 yanlış. Üç yanlışın üçü de arızasız pencere ve sebep
  sınıflandırıcının referansı: pencereden önceki 180 s arızayı (11b +3.3 W, 12 −3.1 W) ya da fansız ilk katmanları
  (10) içeriyor, arızanın KALKMASINI arıza sanıyor. Canlı ikiz bu pencerelerde alarm vermedi. Etiketler araca göre
  kaydırılmadı (sınıflandırıcının sınırı olarak kayıtlı; bakımda tutulan modül, değişiklik ayrı karar).
- Duraklatma kararına katkı: iki gerçek B/A arızasında duraklatma ~172 s'de, kontrollerde yanlış duraklatma yok.

## K33 — Klipper yeniden başlayınca yeniden abonelik + sessizlik bekçisi (2026-10-20, v0.2, MODEL DIŞI)
Olay: 20.10 13:32'de park makrosu için Klipper RESTART (Moonraker `POST /printer/restart`). Websocket açık kaldı,
servis "active", hata yok ama CSV 13:32:38'de durdu; servis elle yeniden başlatılana kadar ikiz verisizdi. Sebep:
Moonraker Klippy koptuğunda nesne aboneliklerini düşürür ve `notify_klippy_disconnected` / `_shutdown` / `_ready`
gönderir; ikiz bunları yok sayıyordu. Yazıcının elektriği kesilip açılınca (v0.2 hedefi) aynısı olurdu.
Çözüm (`main()`, model sınıfları dokunulmadı):
- `notify_klippy_ready` → bağlantı kapatılır, mevcut yeniden bağlanma yolu abone olur (ilk cevabın anlık görüntüsüyle
  baskı durumu, konum, hedefler, fan tazelenir — kopuştaki değişiklikler kaçmaz). Ayrı bir yeniden abonelik kodu yok.
- `notify_klippy_disconnected` / `_shutdown` → yalnız konsola yazılır, "Klipper yok" bayrağı.
- Bağlanırken abonelik cevabı hata ise (Klipper henüz hazır değil) aynı bayrak; `ready` gelince yeniden abone olunur.
- **Bekçi:** Klipper hazırken 60 s (`STATUS_TIMEOUT`) durum mesajı gelmezse yeniden abone olunur (bildirimi kaçan
  bilinmeyen durumlar için). Boşta bile mesaj aralığı ≤ 1.3 s (19.10 logları, sıcaklık gürültüsü) → yanlış tetik
  beklenmez; Klipper yokken bekçi kapalı (gece kapalı yazıcıda dakikada bir yeniden bağlanma olmasın).
Test (sahte Moonraker, coffee buddy logu 30×, bekçi testte 5 s): %30'da disconnected → 3 s → ready, eski soket
sessiz; ikinci bağlantıda abonelik hatası, 2 s sonra ready; %60'ta bildirimsiz sessizlik. 4 bağlantı, 9117 / 9117 satır
yazıldı, Klipper zamanında boşluk 0.5 s'yi geçmedi, alarm 0. AST: yalnız `main` değişti + yeni `STATUS_TIMEOUT`;
Twin, KLossLearner, EnergyMonitor, FaultMonitor, model_step aynı. Eylül TÜMÜ 3.62.
Pi'de doğrulandı (20.10 13:44 deploy, 13:45:17 `curl -X POST …/printer/restart`, boşta): disconnected → 5 s sonra
ready → 5 s bekleme → yeniden abone, 10.2 s veri boşluğu, ikiz yeniden oturdu, CSV akmaya devam etti.
Yazıcı şalterini kapatıp açma (13:46–13:48, Pi ayrı adaptörde): Klipper kopmadı — kart USB'den beslenip açık kaldı
(klippy.log'da "Lost communication" yok, ikiz bu arada 417 satır yazdı; şalter kapalıyken MCU ve termistör çalışıyor,
yalnız ısıtıcı/motor gücü yok). Ardından FIRMWARE_RESTART: disconnected → ready → yeniden abone, 11.3 s boşluk.
`notify_klippy_shutdown` yolu 14:05 M112 (acil durdurma, boşta) ile denendi: shutdown → 43 s (Klipper shutdown durumunda da durum mesajı göndermeye devam etti, 125 satır; yeniden
bağlanma döngüsü yok) → FIRMWARE_RESTART → disconnected → ready → yeniden abone, 11.1 s veri boşluğu.

## K34 — Durum dosyası atomik yazılır; bozuk durumda çökme yok (2026-10-20, v0.2, MODEL DIŞI)
Olay (K33'ün USB testi, 13:49:59): yazıcı–Pi USB kablosu çekilince **Pi'nin kendisi** anında kapandı (günlük son
satıra kadar normal veri, Klipper kopma bildirimi yok, kapanma kaydı yok; sebep bilinmiyor — çekme/takma anında 5 V
geçici olayı adayı, yeni açılışta `get_throttled` 0x0). Açılışta `twin_state.json` 0 bayttı: `save_state` dosyayı
yerinde yazıyordu (`open("w")` önce sıfırlar, 5 s'de bir) ve kesinti o pencereye denk geldi. Sonuç: `load_state`
JSON hatasıyla çöktü, systemd 10 s'de bir yeniden başlattı ve her açılışta `backup_state` BOŞ dosyanın kopyasını
`durum_yedek/`'e ekledi — son 30 yedek tutulduğu için ~5 dk'da tüm sağlam yedekler silinecekti. Elle kurtarıldı
(13:52, 13:43:58 yedeği geri kondu; k = 1.007, k_fil = 1.317 kayıpsız).
Çözüm (üç küçük değişiklik):
- `save_state`: geçici dosyaya yaz + `fsync` + `os.replace` (atomik; bakım durumu dosyası zaten böyle yazılıyordu).
- `load_state`: bozuk dosyada çökme yerine uyarı + sıfırdan başlangıç (diğer yükleyiciler zaten böyleydi); uyarı
  sağlam yedeğin yerini söyler. Yedekten otomatik geri yükleme EKLENMEDİ (atomik yazmayla olmaması gerekir).
- `backup_state`: okunamayan/bozuk dosyayı yedeklemez (kötü kopya sağlam yedekleri döndürüp silemez).
Test: boş dosyayla `backup_state` yedek eklemedi, `load_state` (24, 24, 24) döndü, `save_state` sonrası `.tmp`
kalmadı; sahte Moonraker döngüsü boş durum dosyasıyla başladı, K33 senaryoları yine 9117 / 9117 satır, çıkışta durum
sağlam yazıldı. AST: model sınıfları aynı (değişen: `load_state`, `backup_state`, `save_state`, `main`). Eylül 3.62.
`notify_klippy_shutdown` (MCU kopması) gerçekte hâlâ görülmedi: USB çekilince Pi de kapandı.
Pi'de 20.10 13:58 deploy (kod sürümü `34ffc9b9`): durum her 5 s atomik yazılıyor, `.tmp` kalmıyor, k/k_fil devralındı.

## K35 — Kapanışta son log arşivlenir; boş log arşivlenmez (2026-10-20, v0.2, MODEL DIŞI)
Olay (20.10 Pi kontrolü): `twin_gecmis.csv`'de kapanışta yazılan 5 özet satırı (19.10 19:09; 20.10 12:52, 13:36,
13:52, 13:57) dosya olarak `realtime_log_ws.csv` gösteriyordu: özet kapanışta canlı dosyanın adıyla yazılıyor, bir
sonraki açılış o dosyayı `_onceki_<son yazma>` adına taşıyordu → özet artık var olmayan bir adı gösteriyordu (veri
kaybı yok). Ayrıca K34'teki çökme döngüsü, her açılışta yalnız başlıklı canlı dosyayı arşivleyip 4 boş (275 bayt) log
bırakmıştı.
Çözüm: (1) kapanışta (`main` finally) son parça önce kapatılıp arşivlenir (`LogFile.close_archive`), özet arşivin
adıyla yazılır; arşivlenemezse eski davranış (canlı adla). Çökmede (finally çalışmaz) açılıştaki `archive_log` eskisi
gibi taşır. Yan etki: ikiz çalışmıyorken `realtime_log_ws.csv` yok, son oturum hemen Unity'nin kayıt listesinde
(UNITY_ARAYUZ.md). (2) `archive_log` yalnız başlık satırı olan dosyayı taşımaz; yeni `LogFile` üstüne yazar.
Test (sahte Moonraker, 14.10 cap/chain/glass 60×, başlangıçta yalnız başlıklı canlı dosya): boş arşiv yok, 3 parça
2148 + 2302 + 4215 = 8665 satır (K29 ile aynı), kapanış özeti `_1416.csv`'yi gösteriyor ve dosya var, kapanıştan sonra
canlı dosya yok. AST: değişen `archive_log`, `LogFile`, `main`; model sınıfları aynı.
Pi'de 20.10 14:41 deploy (kod sürümü `5c7029fb`; eski kod kapanırken son kez yanlış adla yazdı). Pi verisi elle
düzeltildi (yedek `durum_yedek/twin_gecmis_K35_oncesi.csv`): Pi dönemindeki 8 satır arşivin ilk satır zamanıyla
eşlenip doğru adı aldı, 4 boş log silindi. 26.09–18.10 arasındaki 115 eski satır (PC dönemi, `-WithState` ile gelen
geçmiş) aynı hatayı taşıyor; o loglar PC'de `loglar/` altında başka adlarla, Pi'de eşlenemedi, olduğu gibi bırakıldı.

## K36 — Soğuma hatasının teşhisi: ısınma ile soğuma farklı kapasite görüyor (2026-10-20, v0.2, ANALİZ, MODEL DEĞİŞMEDİ)
Eylül soğuma 6.44 (bias −6.1). Şekil: ısıtıcı kapanınca residual 10–20 s −1.3, 1–2 dk −7.5 (tepe), 4–8 dk −5.0 → hata
dakikalar boyunca birikiyor, ilk saniyelerin sıçraması değil (kartuş enerjisi τ = C3·R31 ≈ 2.5 s'de boşalır, elendi).
Aynı sıcaklıkta gerçek/model soğuma hızı (27 log, Eylül + ayar, fansız): 40–160 °C ~1.05, 160–220 °C 1.08–1.13.
Model bağımsız kapasite (deney9, ayar): tutma gücü = kayıp L(T); C_soğuma = L/|dT/dt| 100–220 °C'de DÜZ ~6.2–6.5 J/K;
C_ısınma = (P − L)/(dT/dt) sıcaklıkla artıyor (80–100 °C 6.49 → 220–240 °C 7.98). Model C(T) (K, 13.10 `CAP_T_COEF`)
ısınmaya fit edilmişti, ısınmanın eğimini kopyalıyor: 140 °C'de 6.87, 200–220 °C'de 7.44 → soğumada %7–15 fazla.
Tanı taraması (Eylül, taze k): `CAP_T_COEF` 0.0006 / 0 soğumayı yalnız 6.44 → 6.25 / 6.30 yapıyor (seviye de yüksek);
düz C ×0.93 (ölçülen seviye) soğuma 6.44 → 3.47 AMA ısınma 2.32 → 7.22, varış 1.79 → 10.13. Tek kapasite ikisini
tutturamaz: ısınmada %100 güçte sıcaklıkla büyüyen, soğumada geri gelmeyen bir enerji akışı/eksiği var. Adaylar:
(a) tam güçte ısıtıcı gücü sıcakken düşüyor (230 °C'de ~%10 gerekir; deney9 tutma k'sı 120 → 245 °C 1.07 → 1.13, bu
kısmen buna uyar — 15.10'daki "P(T) çürüdü" sonucu bu gözle yeniden incelenmeli), (b) hızlı rampada heat break'e
geçici ısı akışı (dT/dt ile ölçeklenir), (c) kartuş–blok temas direnci sıcaklıkla artıyor (güçle ölçeklenir, gradyanda
enerji). Ayırıcı deney: aynı sıcaklık aralığında farklı sabit güçlerle (ör. `max_power` 1.0 / 0.6) rampa + aynı
tutmalardan serbest soğuma. Alarm etkisi yok (GUC yalnız ≥230 °C tutmada). Ayrıca oda sıcaklığı ölçüldü (uzun boşta
nozzle + tabla): Eylül sonu 24.4–24.8 °C (T_AMB = 25 doğru), 16–20.10 20.6–22.3 °C → Ekim/kış için ayrı aday.
**deney14 planı (20.10):** `yazici/deney14a_kapasite_fan.gcode` (yazıcı değişmeden, ~35 dk): odadan 245'e %100 fan
kapalı (A1), 245 tutma, serbest soğuma → 100 (A2), 150/200 tutmalar fan kapalı (A3) ve fan %100 (A4), 150 → 245 %100
fan %100 (A5: aynı güç, ~%30 yavaş rampa), 245 tutma fan %100, serbest soğuma fan %100 (A6).
`yazici/deney14b_kapasite_guc60.gcode` (`max_power: 0.6`, ~20 dk): odadan 235'e %60 (B1), tutma, soğuma → 120, 120 → 200
%60 (B3). Okuma: ΔP(T) = (C_ısınma − C_soğuma)·dT/dt; A1 = A5 ise (a), ΔP ∝ hız ise (b), ΔP ∝ güç × hız ise (c).
Deney öncesi Pi durumu: k 1.0073, k_fil 1.3175, k_ref 1.045 (fan %100 tutmaları k'yı oynatabilir; sonra kontrol).
**deney14 sonuçları (20.10, ayar; modelden bağımsız, `scratchpad soguma/d14.py`):**
- Serbest soğuma kapasitesi C = L/|dT/dt| fan kapalı ve %100'de AYNI (130–210 °C 5.8–6.5 / 5.7–6.1 J/K; 14b 5.9–6.5) →
  gerçek bir kapasite, ~6.3 J/K; model 140 °C'de 6.86, 210 °C'de ~7.4.
- Tutma kaybı (oda 24.4 °C): 245 °C fan kapalı 18.13 W (model k=1 18.74 → k ≈ 0.97), fan %100 25.77 W; 150 °C 8.27 / 12.20 W.
- Rampalarda eksik güç ΔP = P − L(T) − C_soğuma·dT/dt (W) ve ek kapasite ΔC = ΔP/(dT/dt):
  | T (°C) | A1 40 W fan kapalı | A5 40 W fan %100 | B1 24 W fan kapalı |
  | 170–190 | 4.90 (3.78 K/s, ΔC 1.30) | 2.68 (3.39, 0.79) | 1.58 (1.77, 0.89) |
  | 190–210 | 6.18 (3.25, 1.90) | 3.74 (2.80, 1.34) | 2.04 (1.35, 1.51) |
  | 210–230 | 5.72 (2.98, 1.92) | 4.05 (2.26, 1.79) | 1.64 (1.08, 1.52) |
  ΔP 90–110 °C'de ~0.4 W, sıcaklıkla 6–8 W'a büyüyor.
- (a) güç düşüşü REDDEDİLDİ: aynı 40 W'ta fanla yavaşlayan rampada ΔP ~0.6×, %60 güçte ~0.3× (a 1.0× / 0.6× der).
- (b) ile (c) arası: B1/A1 ΔC oranı 0.69–0.79; (b) saf hız 1.0, (c) saf güç 0.6 der. A5'in ΔP'si düşük taraflı (fanın
  rampadaki kaybı tutmadan küçük: T2 geride). Ek enerji yalnız ısıtıcı açıkken var, sıcaklıkla büyüyor, kabaca hem hız
  hem güçle ölçekleniyor → ısıtıcı–blok arayüzü (kartuş sıcaklığı ölçülemiyor) baş şüpheli; tek mekanizmaya oturmuyor.
- Yan etki: fan %100 tutmaları Pi'de k'yı 1.007 → 0.959 indirdi, KAYIP_AZALDI; deney öncesi durum geri yüklenecek
  (`durum_yedek/twin_state_20261020_144115.json`).
- Pi durumu 16:06'da geri yüklendi (k 1.007, k_fil 1.317, KAYIP_AZALDI kapandı).
**Karar (20.10, kullanıcı):** soğuma çekirdek için önemli değil (alarm, k öğrenmesi, plan kullanmıyor; yalnız görüntü
ve TÜMÜ puanı) → v0.2'de SINIRLAMA (README Limitations, ROADMAP v0.2). Yapısal aday v0.3 kalibrasyon baskısına not
edildi (ROADMAP v0.3 madde 2): C(T) yerine R31(T) = R31₀ + β·(T − T₀) (alüminyum blok çelik kartuştan ~2× genleşir →
temas boşluğu/direnci sıcaklıkla artar → tam güçte kartuş bloğun üstünde giderek daha sıcak, enerji yalnız ısıtıcı
açıkken birikir). Açık sorunlar: C3 0.83 J/K ile ~300 K kartuş farkı gerekir (gerçek kartuş ~1.5–2 J/K olmalı), ΔP'nin
bir kısmı hızla ölçekleniyor (heat break?). Olası yan kazanç: ilk katmanların ~2. dakikasındaki +1.2–1.4 W sarı aday
(19.10) bu deponun varıştan sonra oturması olabilir — sarı seviyeye geçince bu gözle yeniden açılacak.

## K37 — Etiketsiz alarmların incelenmesi; ısınma sonrası ilk katman geçicisi (2026-10-20, v0.2, ANALİZ, KARAR KULLANICIDA)
K28 raporundaki etiketsiz GUC olayları bağlamıyla incelendi (replay, canlı başlangıç; eksik güç, akış, z, hedef).
Öneriler `benchmark/ariza_ornekleri/etiket_onerileri.csv`'de (etiketler.csv'ye YAZILMADI; güven + gerekçe sütunlu,
göreli yolla orijinal loglara bağlı): 0810_sonunda_basarili, 0810_2135, 0810_1804 (16 s), 1510 goofy bust → "-"
(ısınma sonrası ilk katman geçicisi); 0810_1804 (486 s) → "-" (baskı sonu hareketi); 2909 havada baskı → B (K28: gerçek,
286 s); 0810_duz_baski_x_takildi ve 1310 bakım testi (GUC_ARTTI, bakım kaybı gerçekten artırdı, k 0.876 bayattı; C mi
"-" mi) → düşük güven, kullanıcı kararı.
**Bulgu:** sınavdaki "yanlış" alarmların çoğu AYNI imza: 245'e varıştan 25–90 s sonra, ilk katman başında (z 0.3),
eksik güç +1.4…+4.3 W, 4 dk'da sıfıra iner; 16–34 s sürer, duraklatmaya varmaz. K36'nın ısınma deposu hipoteziyle
uyumlu (ısınmada biriken ek enerji varıştan sonra bloğa döner → ısıtıcı daha az güç ister → GUC_AZALDI).
Önerilerle rapor (bilgi): yanlış duraklatma ayar 0 / sınav 0, sınav yanlış alarm 0.155 → 0.093 / baskı saati.
`ENERGY_SETTLE` taraması (bilgi, kod değişmedi; etiketler + öneriler): 30 s (bugün) yanlış 38, yakalanan 15/17;
90 s yanlış 26 (sınav 8 → 4), yakalanan 14/17 (kaçan yalnız düşük güvenli 1310 bakım etiketi; çorapsız 78 → 134 s);
150 s yanlış 23, 13/17 (deney1 mandalı da kaçar). 90 s alarm kuralı adayı — kabul kapısından geçmeli; ama tolerans
genişletmek kök nedeni gizler: fiziksel çözüm K36'nın ısınma deposu (v0.3 R31(T)). Karar kullanıcıda.
**Ek (20.10 akşam): enerji korunumu sınaması (`soguma/store.py`) — depo DEĞİL, rampada kayıp.** Rampada (>120 °C)
eksik enerji ∫(P − L − C_soğuma·dT/dt)dt: 40 W rampalarda 80–180 J (14a A1 179, A5 125; deney9 60–131), 14b B1 751 J
(193 s'lik yavaş rampa). Varıştan sonraki temiz tutmada geri dönen ∫(P_ss − P)dt (kalan sıcaklık değişimi düzeltilmiş):
−128…+−4 J, yani ≈ 0 ya da tersi (ilk 30 s ısıtıcı kararlıdan 0.6–1.5 W FAZLA). Kartuşta depo (c) olsaydı PID 40 W'tan
tutma gücüne inerken ~100–200 J geri verirdi; görülmüyor → ek ısı rampada kaybolan ısı (heat break/heatsink'e?), basit
R31(T) adayı zayıfladı (ROADMAP v0.3 notu düzeltildi). Modelin eksik gücü de temiz tutmalarda varıştan sonra ±1.2 W
(14a, deney9) → K37'deki ilk katman tümseği (+1.4…+4.3 W) ısınmadan DEĞİL, ilk katmanın kendisinden (tabla yakınlığı,
ekstrüzyon başlangıcı, prime). K37'deki "K36 deposu" bağlantısı geri alındı.
**K37 ek (20.10 akşam): tümseğin büyük kısmı başlangıç k'sı.** 08.10 loglarının ilk satırındaki k 1.000 (o dönem k
canlıda öğrenilmiyordu; gerçek fansız k ~0.92, bkz. ilk katman = k ölçer) → yeniden oynatmada model ilk katmanda
~1.4 W fazla kayıp bekliyor (17.8 W / birim k × 0.08); 1510 goofy bust k 1.080 ile başladı (gerçek ~0.98, +1.8 W).
Üstüne binen küçük geçici sapma eşiği aşıyor. Bugünkü sistemde (k doğru taşınıyor) 19.10'un 7 Pi baskısında bu tip alarm
0. → Sınav yanlış alarm oranı büyük ölçüde k öğrenmesi olgunlaşmadan önceki logların ürünü; `ENERGY_SETTLE` 90 s önerisi
bu yüzden zayıf (eski hatayı gizler, bugünkü sistemde sorunu yok). v0.2 raporu için öneri: "bugünkü sistem" dönemi
(k_fil çapası, K27, 18.10 sonrası) ayrı satır olarak raporlansın.
**Bugünkü sistem karnesi (20.10, `report_v02.py --extra loglar/pi --since 2026-10-18`, yeni `--since` seçeneği):**
29 log, 15.6 baskı saati (ayar 8.8, sınav 6.8): yanlış alarm 0, yanlış duraklatma 0; etiketli 3 kasıtlı arızanın 3'ü
yakalandı (medyan 26 s), 2'si gölge duraklatmaya vardı (171–173 s; deney12 cereyanı ~50 s sürdü, duraklatma gerekmez).
v0.2 iddiasının asıl ölçüsü bu satır; saat sayısı küçük, normal baskılarla büyümeli.

## K38 — Log hacmi ve saklama (2026-10-20, v0.2, ÖNERİ, KARAR KULLANICIDA, MODEL DIŞI)
Ölçüm (Pi'nin 24 arşiv parçası, 28.9 saat): boşta (hedef 0, T < 40 °C, baskı yok) 3.26 MB/saat, 3.7 satır/s; baskıda
4.43 MB/saat, 4.0 satır/s. 7/24 açık Pi günde ~80–100 MB yazar; kartta 4.7 GB boş → ~50 gün (önceki "~100 gece"
tahmini yalnız geceyi sayıyordu). Journal 200 MB ile sınırlı (sorun değil). Seçenekler:
- A) Boşta seyreltme: boştayken en çok 1 satır/s yaz (model her mesajı işlemeye devam eder, yalnız CSV seyrekleşir).
  Boşta ~3.7× az (~0.9 MB/saat); Unity'nin "veri yok" eşiği 3 s, "script çalışıyor" 5 s → 1 Hz ikisini de karşılar.
  Silme yok. Yeniden oynatmada yalnız boşta (oda sıcaklığı) adım aralığı büyür. Veri sözleşmesi notu: "her mesaj bir
  satır" kuralı boşta gevşer (UNITY_ARAYUZ.md'ye yazılmalı).
- B) N günden eski arşivleri gzip'le (~8–10×). Unity kayıt listesi yalnız .csv görür → eski oturumlar listeden düşer;
  `sync_pi_logs.py` N günden önce çalıştırılırsa PC'de .csv kopyası kalır.
- C) N günden eski, yalnız boşta parçaları sil. Önerilmez (geri dönüşsüz; bilgi değeri düşük ama sıfır değil: oda sıcaklığı).
Öneri: A (basit, silme yok, 7/24 boşta ağırlıklı kullanımda ~4 aya çıkarır); gerekirse sonra B. Deploy `sudo` ister.
**Uygulama (20.10 akşam, kullanıcı onayı; DEPLOY EDİLMEDİ):** `IDLE_LOG_INTERVAL` 1 s, `idle_row`: iki ısıtıcının da
hedefi ve gücü 0, ikisi < `IDLE_T_MAX` 40 °C, baskı/duraklatma yok, kafa hareketsiz; alarm ya da baskı durumu değişince
satır her hâlükârda yazılır; aralık ikizin zamanıyla (Klipper eventtime) ölçülür, saat geri giderse yazılır.
Çevrimdışı (24 Pi logu): 108 → 68 MB, boşta gece 43.6 → 11.5 MB; sıcak önce gelen loglarda yeniden oynatma birebir.
Sahte Moonraker (gece boşta 20 dk + deney10): aktif satır 7312 = 7312, ilk aktif satır aynı an, boşta aralık 1.00 s
(en çok 1.51), alarm aynı saniyede. Yeniden oynatmada k farkı ~2e-5, model bir yerde en çok 0.30 °C: öğrenici
pencereleri (20/60 s) bir öncekinin bittiği mesajda başladığı için seyreltme pencere ızgarasını < 1 s kaydırıyor.
CANLI ikizi etkilemez (her mesaj işlenir), yalnız logun yeniden oynatılmasını. Doğrulama (20.10 akşam): model
sıcaklıkları baskıya geçişte ve sonrasında birebir aynı; ilk ayrışma baskının 486. s'sinde bir öğrenme penceresinin
kapanışında (önce k / k_fil, sonra model): pencere 20 s'yi geçen ilk mesajda kapandığı için boşta 1 s aralıklı mesajlar
ızgarayı ≤ 1 s kaydırıyor. Replay'i 1–3 satır geç başlatmak fark YARATMIYOR (0.25 s'lik kayma ilk pencerelerde aynı
mesajlara oturup sönüyor), 1 s'lik kayma sönmüyor. Büyüklük: k_fil geçici en çok 0.02 (sonda 2e-5), k 0.0012, model en
çok 0.30 °C (%99 0.25), alarm aynı saniyede. Tam eşitlik istenirse öğrenici pencereleri mutlak zaman ızgarasına
bağlanabilir (model kodu → kabul kapısı); şimdilik gerek görülmedi.
**? (hocalara sorulacak):** "Olay yokken seyrek, olay olunca tam hız" kaydın gözlemleri bozmadığı argümanı (CFD'de çıktı
yazma sıklığı benzetmesi; boşta tek gözlem oda sıcaklığı, saatlik ölçekte; her etkinlik seyreltmeyi anında bitirir;
yeniden oynatmada öğrenme penceresi fazının kayması kabul edilebilir mi?).
**Düzeltme (20.10 gece, `scan_subthreshold.py --extra loglar/pi`):** "ilk katman tümseği ısınmadan değil" sonucu yalnız
HIZLI rampalara (%100 güç; 14a, deney9) dayanıyordu. 14b'nin yavaş rampasından (%60, 193 s) sonra, z 50'de baskısız
tutmada da +1.46 W / 71 s eşik altı sapma var → ısınma sonrası geçici en azından yavaş rampada var; ilk katmanla ayrımı
henüz kesin değil. Bugünkü sistemde (19.10, k doğru) 7 baskının 5'inde ilk katman ~2. dk +1.2…+1.4 W, 76–138 s (sarı
aday, eşik altı). Sarı seviye incelemesinde ikisi birlikte ele alınmalı.

## K39 — Fit aracı (`calibrate.py`) ve ilk tanılama sonucu (2026-10-20, v0.3 hazırlığı, ANALİZ, MODEL DEĞİŞMEDİ)
v0.3'ün asıl iş yükü kalibrasyon logundan parametre çıkaran araç; v0.2 baskıları beklenirken paralel başlandı
(kullanıcı isteği). `calibrate.py`: canlı ikizin `model_step`'i logdaki ısıtıcı gücü + fanla açık çevrimde koşar,
seçilen sabitleri T1 farkının en küçük kareleriyle fit eder (scipy; canlı ikiz gerektirmez), her loga kendi k'sını verir
(oturum seviyesi), tanılanabilirliği raporlar (Jacobian'dan ±1 se, bilgisiz parametreler, |r| > 0.9 çiftler).
- **Araç doğrulaması (sentetik):** bugünkü modelin kendi çıktısı (0.01 °C yuvarlama) rastgele bozulmuş başlangıçtan fit
  edildi: deney9 + 14a + 14b ile 10 parametrenin hepsi %1 içinde geri bulundu (yalnız deney9: fan yok → HA2_FAN bilgisiz).
- **Ayrılamayanlar (gürültüsüz bile):** HA1~HA2 −1.00 (blok yüzeyi ve heat break kaybı ikisi de T − T_oda ile orantılı,
  T2 ≈ T1), HA2~HA2_FAN, C2~kayıplar. Gerçek veride ayrıca C3~R31 −1.00 (yalnız τ = C3·R31 belirlenebiliyor), C1~C3,
  C1~R31. → kalibrasyon için parametre azaltma: tek toplam kayıp (HA1 + HA2), kartuş için yalnız τ.
- **Gerçek veri (3 log, her log kendi k'sıyla):** bugünkü parametreler 3.59 °C (k: deney9 1.102, 14a 1.015, 14b 0.975);
  10 parametre fit 2.19 °C (deney9 2.39 → 1.62, 14a 4.63 → 2.88, 14b 3.65 → 1.57). AMA 4 parametre sınırda ve fiziksel
  değil: C1 2 → 5.06, C2 4.03 → 1.35, C3 0.83 → 0.21 (sınır), R_HB 1.8 → 7.2 (sınır), HA2 /4 (sınır), CAP_T_COEF
  0.0012 → 0.0017. τ_kartuş = C3·R31: 2.5 → 0.59 s (deney9'un ~0.5–1 s gecikme bulgusuyla uyumlu). Fit, yapıdaki
  eksiği (K36 ısınma–soğuma asimetrisi) telafi ediyor → değerler modele ALINMADI.
- **v0.3'e dersler:** (1) oturum k'sı her zaman ayrı fit edilmeli; (2) serbest fit fiziksel olmayan değerlere kaçıyor →
  ROADMAP'teki "profil = başlangıç tahmini + belirsizlik" doğrudan önsel (prior) olarak fit'e girmeli (cezalı/MAP fit);
  (3) parametre sayısı tanılanabilir olanlara indirilmeli; (4) 3 log az, her aday Eylül + ayar setinde puanlanmadan
  kabul kapısına gelmez. Sıradaki: önselli fit + azaltılmış parametre seti, sonra kalibrasyon G-code'u bu
  tanılanabilirliğe göre tasarlanır.
**K39 ek — azaltılmış + önselli fit ve genelleme (20.10 gece):** varsayılan set C_ALL (üç kapasite tek çarpan), R31 (C3
sabit = kartuş τ), R_HB, CRAD (kayıp şekli; seviye log başına k), HA2_FAN, CAP_T_COEF. Veri ağırlığı dürüst: açık çevrim
hatası ~60 s ilişkili → 5444 örnek ≈ 91 bağımsız değer. Sonuç: ayrılamayan çift yok, se %0.5–9, sınırda değer yok;
C_ALL 0.925 (= deney14 soğuma ölçümü 6.3 J/K), HA2_FAN ×1.20 (= deney8'in 1.2–1.4×), R31 ×1.37, R_HB ×1.14, CRAD ×1.01,
CAP_T_COEF ×1.31; kalibrasyon logları 3.59 → 2.27 °C. ±%30 önsel neredeyse hiç değiştirmiyor (veri baskın).
Genelleme (`--score`):
| fit | Eylül TÜMÜ (ısınma / varış / soğuma) | 14.10+ ayar 22 log, canlı baş. TÜMÜ (ısınma / varış / soğuma) |
| bugünkü | 3.53 (2.32 / 1.79 / 6.44) | 2.88 (3.76 / 5.26 / 4.55) |
| 6 param | 3.82 (7.24 / 8.02 / 4.54) | 3.12 (4.78 / 3.78 / 4.21) |
| yalnız fan ×1.22 | 4.43 (2.32 / 1.79 / 8.40) | 3.20 (3.64 / 5.21 / 4.42) |
| C_ALL 0.93 + C(T) ×1.39 | 3.65 (9.26 / 8.25 / 2.78) | 2.80 (6.57 / 3.94 / 4.31) |
Hiçbiri bugünkü baskılarda açık kazanç vermiyor. (1) Kapasite: soğumaya göre inen C baskıların hızlı ısınmasını bozuyor
— K36 asimetrisi v0.3 kalibrasyonunun ASIL ENGELİ (yapı değişmeden kalibrasyon ısınma ile soğuma arasında bir yer seçer).
(2) Fan: %100'de ölçülen çarpan %25–40 baskı fanına genellenmiyor → eğri şekli (FAN_F0, FAN_EXP) de ölçülmeli,
kalibrasyon G-code'u fanı baskıda kullanılan seviyelerde adımlamalı. (3) Eylül seti eski donanım durumu (13.10 bakım,
kanal yeniden takıldı): yapı sınaması için doğru, bugünkü parametrelerin sınaması için değil → kalibrasyon aynı
donanım dönemindeki loglarla sınanmalı (`--score-since`).

## K40 — Isınma asimetrisi için yapı adayları (2026-10-20 gece, v0.3 yolu A, ANALİZ, MODEL DEĞİŞMEDİ)
Soru (K36, K39): ısınmada kaybolan 80–180 J nereye gidiyor? Adaylar `calibrate.py` ile kalibrasyon loglarında (deney9,
14a, 14b; log başına k) fit edildi; geçici yamalarla (model_step + öğrenici/eksik güç kartuş kaybını ayırır + başlangıç
k'sı K_MAP ile ölçeklenir) Eylül ve 14.10+ ayar setinde (22 log, canlı başlangıç) puanlandı.
| aday | kalibrasyon rms | sonuç |
| bugünkü yapı, log başına k | 3.59 | referans |
| bugünkü yapı, 6 param yeniden fit (K39) | 2.27 | baskılara genellenmedi |
| S1: heat break + heatsink düğümü (C4, G24, G4A) | 2.27 | C4 alt sınıra → veri istemedi, ELENDİ |
| kartuştan doğrusal kayıp (G3A), CRAD sabit | 2.28 | G3A alt sınıra, ELENDİ |
| kartuştan radyasyon (EPS3), CRAD sabit | 2.22 | EPS3 alt sınıra, ELENDİ |
| **S3d: radyasyon bloktan kartuşa (CRAD serbest + EPS3)** | **1.36–1.43** | ayrılamayan çift yok |
S3d değerleri: CRAD /4 (sınırda), EPS3 4.1e-11, R31 ×2.5 (7.5 K/W: tam güçte kartuş bloktan ~300 K sıcak — sınırda
fiziksel), C_ALL 0.82, CAP_T_COEF ×0.84, R_HB ×1.67, HA2_FAN 0.87; log k'ları bugünkünün ~1.16 katı (K_MAP).
Fizik: ısınırken çok ısınan kartuş fazla ısıyı radyasyonla (T⁴) kaybeder, soğumada kartuş = blok → kayıp yok.
Adil puanlama (K_MAP 1.16; 1.05 ile tutmalarda −4 °C bias çıktı — eşleme önemli):
- 14.10+ ayar 22 log: TÜMÜ 2.88 → 3.68; ısınma 3.76 → 3.71, varış 5.26 → 3.85, soğuma 4.55 → 3.79, diğer tutmalar
  3.40 → 3.15, tutma245 4.66 → 4.57, FANLI TUTMA 2.12 → 3.83 (baskıların çoğu), geçiş 4.95 → 5.22.
- Eylül (eski donanım): TÜMÜ 3.53 → 5.34 (ısınma 2.32 → 3.97, varış 1.79 → 3.76, soğuma 6.44 → 7.73).
Hüküm: S3d mevcut yapının yapamadığını yapıyor (aynı dönemde ısınma bozulmadan varış + soğuma iyileşiyor), ama fanlı
baskı ve Eylül kötüleşiyor → bu hâliyle KABUL EDİLMEZ. Fanlı baskıdaki kayıp büyük ölçüde puanlama yamalarının
eksikleri olabilir (fan eğrisi yalnız %100'de ölçüldü, k_fil çapası 1.33 eski yapıya göre, K_MAP kaba eşleme); bunlar
geçici yamayla değil, yapıyı ikizin içine tutarlı yerleştirip (öğrenici, eksik güç, initial_state, çapa) ölçülebilir —
gerçek model değişikliği, kabul kapısı. Araç: `benchmark/structure_cartridge_loss.py` (K_MAP ortam değişkeni).
v0.3'e not: kalibrasyon baskısına baskı fan seviyelerinde (25/40/60/100 %) fan adımları + iki güç sınırlı rampa şart.
**K40 karar ve sıra (20.10, kullanıcı):** S3d yolu sürdürülecek; ölçüt soğuma DEĞİL (çekirdeğe hizmet etmiyor):
fanlı baskı aynı ya da daha iyi, ısınma/varış daha iyi; asıl değer v0.3 (ısınma + soğuma aynı parametrelerle →
kalibrasyon başka yazıcıya taşınabilir). Sıra: (1) `yazici/deney15_fan_egrisi.gcode` (fan 0/15/20/25/40/60/100 ve geri,
245 + 215 °C, ~45 dk; Eylül deney5/6'nın bakım sonrası tekrarı) — v0.2 beklenirken alınabilir, sonra Pi durumu geri
yüklenir; (2) v0.2 kapanınca S3d ayrı dalda ikizin içine tutarlı yerleştirilir (öğrenici, eksik güç, initial_state,
k_fil çapası) ve kabul kapısından geçirilir.

## K41 — v0.3 hazırlığı: kalibrasyon baskısı üreteci, sanal yazıcı, karne aracı, baskı kuyruğu (2026-10-20 gece)
Kullanıcı isteği: v0.3 için onay gerektirmeyenleri yap; baskı gerektirenleri tek listede topla. Canlı koda dokunulmadı.
- **`yazici/calibration_print.py` → `yazici/calibration_245.gcode`** (~52–58 dk, ekstrüzyon yok, tabla kapalı, Z50):
  soğuk başlangıç, odadan tam güç rampa + tutma, serbest soğuma → MID, **yavaş rampa = hedefi 1 °C'lik adımlarla
  artırmak** (PID doymaz: sanal baskıda güç medyan 0.47, en çok 0.63, 0.83 °C/s; deney14b'nin `max_power` değişikliği
  gerekmez), fan merdiveni (15/25/40/60/100 ve 0), ±10 °C küçük adımlar, inen tutmalar (215/185/150; 215'te fan
  40/100), 150 → 185 → 215 → 245 tam güç adımları (kartuş bloktan çok sıcakken farklı sıcaklıklar), 60 °C'ye serbest soğuma.
- **`yazici/virtual_print.py`**: ısıtıcı G-code'unu sanal yazıcıda koşar (closed_loop.KlipperPID, katsayılar
  `printer.cfg` SAVE_CONFIG'den: 38.530 / 12.232 / 30.342) ve ikiz logu yazar → baskıdan önce süre ve
  `calibrate.py --synthetic` ile tanılanabilirlik. (Not: `closed_loop.py`'nin kendi sabitleri 40.262 / 12.201 / 33.216
  eski; G-code planı PID'i printer.cfg'den okuduğu için canlı plan etkilenmez; yalnız closed_loop'un çevrimdışı
  doğrulama aracı. Bakımda tutulan modül, değişiklik ayrı karar.)
- **Tasarımın tanılanabilirliği** (sanal baskı, belirsizlik ~1 °C model hatası ve ~52 bağımsız değer varsayımıyla):
  bugünkü yapı 6 parametre: kapasite ±%0.8, fan ±%2.7, CRAD ±%5, R_HB ±%14, C(T) ±%14, R31 ±%20, ilişkili çift yok
  (yalnız deney9: fan bilgisiz). Yukarı adımlar eklenmeden önce R31 ±%28, R31~R_HB 0.91. S3d: EPS3 ×2.25, CRAD ±%39,
  CRAD~EPS3 −0.96, CRAD~HA2_FAN 0.98 → kartuş ve blok kaybı tek baskıda zayıf ayrılıyor (T3 ölçülmüyor); gerçek veride
  deney9 + 14a + 14b birlikte EPS3 ×1.14 verdi → S3d sınanacaksa kalibrasyon baskısı deney loglarıyla birlikte.
- **`benchmark/baseline_snapshot.py`**: `--save NAME` Eylül (taze), ayar ve sınav (canlı başlangıç) her logun faz
  skorlarını, alarm sayılarını, son k / k_fil'i ve kod sürümünü `benchmark/baseline_NAME.json`'a yazar; `--check NAME`
  farkları listeler. v0.3 refactor'unun "v0.2 skorlarını birebir üret" şartı bununla. Sınav yalnız özdeşlik denetimi.
- **`yazici/BASKI_KUYRUGU.md`**: bekleyen tüm baskı/deneyler (Pause/Resume, gölge mod baskıları + ikize yararlı baskı
  önerileri, deney15, kalibrasyon baskısı, K38 deploy, iki etiket) ve deney öncesi/sonrası durum yedekleme.

## K42 — Yol haritası: baskılar ve diğer filamentler (2026-10-20 gece, PLAN)
Sıra: v0.2 kapanış (Pause/Resume, 5–10 PETG, deney15, K38 deploy, 2 etiket, `v0.2` etiketi) → v0.3a refactor (baskısız:
profil + kalibrasyon dosyası, İngilizce adlar, göreli sıcaklık eşikleri; `baseline_snapshot --check` IDENTICAL) → v0.3b
PETG kalibrasyon baskısı (fit + kapı, S3d kararı) → v0.3c malzemeler → v0.4 ikinci yazıcı. Baskı listesi:
`yazici/BASKI_KUYRUGU.md`.
Kodun PETG'ye bağlı yerleri: (1) `ADAPT_TMIN` / `ENERGY_TMIN` 230 °C → PLA (200–215) için ikiz hiç öğrenmez ve alarm
vermez; öneri: hedefe göreli (ör. hedef − 15 °C), eski loglarla sınanır (baskısız); (2) model 235–245 °C'de doğrulandı
→ malzeme başına kalibrasyon baskısı (`calibration_print.py --temp 210 / 255`); (3) fan modeli %20–40'ta → deney15 +
kalibrasyon fan merdiveni %100'ü ölçer (PLA); (4) `K_FIL` PETG ısı kapasitesine sabit, k_fil çapası 1.33 (PETG/TPU
≈ aynı, K27) → PLA/ABS'de 3–5 baskıyla doğrulanır, malzeme tablosu v0.3; (5) `T_AMB` 25 sabit → kapalı kasada ABS/ASA
kapsam dışı. Fiziksel sınırlar: PLA'da filaman ısısı (T − T_oda) 210 °C'de 245'in ~%84'ü → aynı akışta tam kesinti
eşiğe daha geç ulaşır; `printer.cfg` `max_temp: 260` → üst sınır ~255 °C (PC, naylon kapsam dışı).
**K42 ek:** elde yalnız PETG ve TPU (kullanıcı, 20.10). Kalibrasyon baskısı ekstrüzyonsuz → 210 ve 255 °C kalibrasyonu
filamentsiz yapılır (model doğrulaması). 230 °C altı baskı durumu TPU'yu 220–235 °C'de basarak sınanır (göreli eşik).
PLA/ABS baskı içi doğrulaması (k_fil çapası, %100 fanla baskı, tespit hassasiyeti) v0.4 test kullanıcılarına kalır.

## K43 — Etiketler, ENERGY_SETTLE reddi, closed_loop PID geçmişi (2026-10-21, kullanıcı onayları)
- **Etiketler** (`etiketler.csv` 30 → 37, göreli yolla orijinal loglara): 0810_sonunda_basarili, 0810_2135, 0810_1804
  (16 s ve 486 s), 1510 goofy bust → "-"; 0810_duz_baski_x_takildi → "-" (kullanıcı: 19:53 işi tamamlandı; alarm
  19:56:22, ilk katman başı; X takılmasının zamanı bilinmiyor); 2909 havada baskı → B (ayar). 1310 bakım testi (C mi
  "-" mi) kullanıcı kararını bekliyor (`etiket_onerileri.csv`'de tek satır). Rapor (`--extra loglar/pi`): etiketli
  arıza 14/16 yakalandı (medyan 26 s), duraklatma 4/16, **yanlış duraklatma her sette 0**; sınav yanlış alarm
  0.155 / baskı saati (değişmedi; 10'un 8'i artık doğrulanmış "-", 2'si 1310).
- **`ENERGY_SETTLE` 90 s adayı REDDEDİLDİ** (K37; kullanıcı onayı): yanlış alarmlar eski logların yanlış başlangıç
  k'sından; bugünkü sistemde (18.10+) bu tip alarm yok; tolerans genişletmek kök nedeni gizlerdi.
- **`closed_loop.py` PID geçmişi** (bakımda tutulan modül, hata düzeltmesi, kullanıcı onayı): 28.09'dan beri hep
  40.262 / 12.201 / 33.216 varsayılıyordu. Pi'deki SAVE_CONFIG yedeklerinden (printer-YYYYMMDD_HHMMSS.cfg, kaydetmeden
  ÖNCEKİ içerik): 08.10 17:50:33'e kadar 40.837 / 13.612 / 30.628, 13.10 11:06:51'e kadar 40.262 / 12.201 / 33.216,
  sonra 38.530 / 12.232 / 30.342 (bakım). `PID_HISTORY` + `pid_gains_at(t)` (yerel saat, Europe/Istanbul);
  `simulate` logun başlangıç zamanının katsayılarını kullanır; varsayılan = güncel. Etki: ayar seti kapalı çevrim
  doğrulaması değişmedi (güç rms 1.47 W, sıcaklık rms 1.11 °C; katsayılar ~%5 farklı, kapalı çevrim duyarsız).
  `gcode_plan.py` katsayıları printer.cfg'den kendisi verdiği için etkilenmez. (deney14b ısınma 96 / 47 s: closed_loop
  `max_power` 1.0 varsayar, o deneyde 0.6 — bilinen istisna.)
**K43 ek (21.10, kullanıcı):** 1310 bakım testi → C (ikiz ısıl yoldaki gerçek değişimi 29 s'de yakaladı). Etiketler 38;
rapor: etiketli arıza 15/17 yakalandı (medyan 26 s), duraklatma 4/17, yanlış duraklatma 0; sınav yanlış alarm
0.155 → 0.093 / baskı saati (8 yanlış, hepsi doğrulanmış "-"), tümü 0.100. Sınavda etiketsiz olay kalmadı; ayarda
3 kısa olay etiketsiz (2909 benchy1 1410 s / 17 s, 1510 gyroid küp 305 s / 20 s ve 345 s / 29 s; dosya adları
"ariza_yok", baskılar sağlam — etiketlenirse "-"). `etiket_onerileri.csv` boşaltıldı.

## K44 — deney15: fan eğrisi bakım sonrası (2026-10-21, ANALİZ, MODEL DEĞİŞMEDİ)
Log `loglar/deneyler/deney15_fan_egrisi.csv` (Pi `realtime_log_ws_onceki_20261021_1056.csv`, 09:06 başlangıç,
complete). 20.10 gece önce yarıda kesilen bir deneme (pause/cancel; park X≈0 Y220 Z+2 doğrulandı, havada) k'yı
1.007 → 0.917 indirdi; durum geri yüklenmeden deney15 ve ardından PETG sıcaklık kulesi (10:56) başladı (k 0.926) →
kule bitince geri yükleme.
Her tutmanın son 90 s ortalama ısıtıcı gücü (245 °C fan 0: 17.34 W, iniş 17.34; tekrarlanabilirlik ≤ 0.1 W):
245 °C fan %15 / 20 / 25 / 40 / 60 / 100 → +1.09 / 1.19 / 2.93 / 4.38 / 6.15 / 8.04 W (iniş %60 6.23, %25 2.96);
215 °C (fan 0 14.21 W) %25 / 40 / 100 → +2.32 / 3.57 / 6.52 W. Model (k=1) 0 / 0.65 / 1.67 / 3.13 / 4.36 / 6.08 W →
fan etkisi 1.3–1.8× az tahmin, eşik çok yüksek (%15'te model 0, ölçülen +1.1 W).
Fit (ΔP = HA2_FAN·((f − F0)/(1 − F0))^EXP·(T2 − T_oda)): HA2_FAN 0.0293 → 0.0369 (+%26), FAN_F0 0.191 → 0.138, FAN_EXP
0.508 → 0.520 (aynı: zorlanmış konveksiyon h ∝ hız^0.5 korunuyor); rms 1.23 → 0.35 W. Uymayan tek nokta %20 (1.19 / fit
2.04): blower %15–20'de düzgün dönmüyor olabilir (baskılarda ≥ %25 kullanılıyor). Deney8'in "fan 1.2–1.4×" bulgusu ve
K39'un "%100 çarpanı %25–40'a taşınmıyor" bulgusu bununla açıklanıyor: eşik + genlik birlikte değişmiş (bakım, kanal).
MODELE ALINMADI: baskıda k_fil fan hatasını kısmen telafi ediyor (çapa 1.33 değişir) → kabul kapısı; v0.3'te
kalibrasyon baskısı + S3d ile birlikte (PROFILE_DESIGN: fan eşiği/üssü kalibrasyondan).

## K45 — İlk gerçek kalibrasyon baskısı (2026-10-21, v0.3, ANALİZ, MODEL DEĞİŞMEDİ)
`yazici/calibration_245.gcode` 12:08–13:01 basıldı (başlangıç k 0.926: önceki havada deneylerden; sonra durum
13:05'te geri yüklendi, k 1.007). Log `loglar/deneyler/kalibrasyon_245_ilk.csv` (ayar). Aynı gün PETG sıcaklık kulesi
(10:56–12:05, Pi `..._1208.csv`) ve deney15 (K44).
`calibrate.py --params C_ALL,R31,R_HB,CRAD,HA2_FAN,FAN_F0,CAP_T_COEF`: 3419 örnek (~57 bağımsız), rms 5.88 → 1.83 °C
(log k 0.957). C_ALL 0.947 ±0.8 %, R31 ×1.03 ±18 %, R_HB ×1.00 ±13 %, CRAD ×0.97 ±5 %, HA2_FAN ×1.30 ±3 %, FAN_F0 0.186
±4 %, CAP_T_COEF ×1.25 ±10 %; ilişkili çift yok → tasarım gerçekte de sanal baskının öngördüğü gibi tanılanabilir
(K41), değerler bağımsız ölçümlerle tutarlı (deney14 C ×0.92; deney15 fan ×1.26).
Genelleme: Eylül TÜMÜ 3.53 → 4.31 (ısınma 2.32 → 7.29, varış 1.79 → 7.57; eski donanım); 14.10+ ayar 26 log (canlı
başlangıç) TÜMÜ 3.05 → 3.08: varış 5.33 → 4.33, tutma245 4.55 → 3.89, soğuma 4.96 → 3.72, geçiş 5.20 → 4.09 iyi;
ısınma 4.54 → 6.25, fanlı tutma 2.12 → 2.46 kötü. → K36 ödünleşimi gerçek kalibrasyonda da: ölçüm zinciri (baskı,
araç, tanılanabilirlik) ÇALIŞIYOR; v0.3'ün kalan engeli yalnız yapı (ısınmadaki fazla ısı; K40 S3d yolu).
Not: deney15'in tek başına fit'i FAN_F0 0.138 (FAN_EXP serbest) verdi, burada 0.186 (FAN_EXP sabit 0.508): eşik ile
üs birbirine bağlı; fan eğrisi v0.3'te ikisi birlikte, deney15 + kalibrasyon logu ortak fit edilmeli.
