# Python ↔ Unity arayüz sözleşmesi

Unity görselleştirmesi (`Ender3_Twin/`) Python ikizine **ağ üzerinden değil, CSV dosyası üzerinden** bağlıdır.
`realtime_websocket.py`'de değişiklik yaparken aşağıdakileri bozma.

## Canlı mod
- Unity, `dt/realtime_log_ws.csv` dosyasını saniyede 4 kez yoklar ve **yeni eklenen satırları** okur.
- Dosyayı açık tutmaz; her okumada açıp kapatır (Python yazabilir ve yeniden adlandırabilir).
- Yarım yazılmış son satırı bekletir, tamamlanınca işler.
- Dosya küçülürse ya da yeniden oluşturulursa (yeni oturum) baştan okur.
- **2026-10-19'dan itibaren log baskı başına bölünür:** her yeni baskı başında (`print_state` → `printing`, pause'dan
  dönüş hariç) Python canlı dosyayı kapatır, `realtime_log_ws_onceki_...` adıyla arşivler ve aynı adla yeni bir dosya
  açar (ilk satırı baskının ilk mesajı). Unity'nin canlı görüntüsü baskı başında sıfırlanır (kullanıcı onayı). 1 dk'dan
  kısa bir parça (ör. ikiz baskıdan hemen önce açıldıysa) ayrılmaz, yeni baskı aynı dosyada devam eder.
- **2026-10-20'den itibaren (K35) ikiz kapanırken de** son parça arşivlenir: ikiz çalışmıyorken `realtime_log_ws.csv`
  yoktur (Unity bunu "dosya bulunamadı" olarak görür), son oturum hemen kayıt listesinde çıkar. Yalnız başlıktan
  oluşan dosya (veri gelmeden çöken açılış) arşivlenmez, bir sonraki açılış üstüne yazar.
- **K38 (2026-10-20, deploy sonrası geçerli): boşta seyreltme.** İki ısıtıcı da kapalı ve soğuk, baskı yok, kafa
  hareketsizken saniyede en çok 1 satır yazılır ("her mesaj bir satır" kuralı yalnız boşta gevşer). Herhangi bir
  etkinlik (hedef, güç, sıcaklık ≥ 40 °C, baskı, hareket, alarm değişimi) anında tam hıza döndürür. Unity'nin 3 s
  "no data" ve 5 s "script çalışıyor" eşikleri 1 Hz ile karşılanır.
- Dosyanın son yazılma zamanı 5 s'den yeniyse menü "script çalışıyor" der. Bu yüzden `FLUSH_INTERVAL` 5 s'nin altında kalmalı (şu an 1 s).
- Canlı veri 3 s'den uzun gelmezse Unity üst çubukta "no data" uyarısı gösterir.
- Unity canlı görüntüyü verinin `liveDelay` = 1.5 s gerisinden oynatır (Playhead), böylece paketler arasında ara değer alabilir.
  `FLUSH_INTERVAL` bu tampondan küçük kalmalı; büyütülürse Unity'de `liveDelay` de büyütülmeli, yoksa hareket her pakette takılır.

## Replay modu
- Unity, `dt/` içindeki `realtime_log_ws_*.csv` dosyalarını (ör. `realtime_log_ws_onceki_YYYYMMDD_HHMM.csv`) kayıt olarak listeler.
- Eski oturum dosyalarının adlandırma kalıbını değiştirme; menüdeki tarih etiketi bu kalıptan okunur.
  Aynı dakikada ikinci bir parça arşivlenirse adına saniye eklenir (`..._YYYYMMDD_HHMMSS.csv`); Unity bunu tarih
  olarak çözemez ve genel etiketle (ad · tarih · boyut) listeler.

## Sütunlar
Unity sütunları **sırayla değil, başlık adıyla** okur. Sıra değişebilir, yeni sütun eklenebilir.
**Aşağıdaki adları değiştirmek Unity'yi bozar:**

| Sütun | Unity'de kullanımı |
|---|---|
| `time` | Zaman ekseni (unix saniye). **Zorunlu**, artan olmalı. |
| `T1_real`, `T1_model`, `residual`, `target`, `pwm` | Hotend kartı ve grafikleri |
| `fan_state` | Fan (ileride pervane animasyonu) |
| `anomali` | `True`/`False` → üst çubuktaki durum hapı. 2026-10-12'den itibaren **resmi alarm aktif** demek (`ariza` dolu); öncesindeki loglarda ham residual eşiği (\|residual\| > 12 °C). |
| `eksik_guc` | Güç dengesi (W, + = beklenenden az ısı gidiyor), resmi alarmın sinyali (eşik ±2.5 W). Unity hotend detayında, kartında ve sparkline'ında **ek kayıp = −eksik_guc** olarak gösterir (değerlendirilmeyen aralıkta çizgi kesilir). Boş hücre "geçersiz" demek (ısınma, hedef değişimi, 230 °C altı): son değer tutulmaz. 2026-09-26'dan beri loglanıyor. |
| `k_loss` | Öğrenilen kayıp çarpanı k → hotend kartında. |
| `ariza` | Aktif resmi alarm kodları (`;` ile) → üst çubuktaki durum hapında ve hotend kartında okunur etiket (`GUC_AZALDI` → "Low heat demand", `GUC_ARTTI` → "High heat demand", `KAYIP_ARTTI/AZALDI` → "Heat loss drifted up/down"). Her satırda o anki küme; boş = alarm yok. |
| `bed_T`, `bed_model`, `bed_res`, `bed_target`, `bed_pwm` | Tabla kartı ve grafikleri |
| `pos_x`, `pos_y`, `pos_z` | Eksen hareketi (`AxisMotion`): makine koordinatı, mm. `pos_x` 2026-10-09'dan itibaren loglanıyor (Klipper'ın komut ettiği konum, ölçüm değil). Unity örnekler arasında doğrusal ara değer alır. |
| `pos_e` | Kümülatif filament (mm). Print modülünün **ilerleme değişkeni**: plan ile gerçek aynı filament miktarında karşılaştırılır (takvim sapması, kalan süre). |
| `extrude_velocity` | Filament hızı (mm/s) → gerçek hacimsel akış (× 2.405 mm²). |
| `print_state` | Klipper `print_stats.state`: `standby` / `printing` / `paused` / `complete` / `cancelled` / `error` (2026-10-10'dan itibaren). Print kartında durum (Unity'de 4.3-C ile kullanılacak). |

Kayıt sütunları (Unity okumuyor, 2026-10-14'ten itibaren): `z_offset` = Klipper `gcode_move.homing_origin` Z
(babystep / `SET_GCODE_OFFSET Z`, mm, + = nozzle tabladan uzak; `pos_z` bunu zaten içerir), `z_endstop` = kayıtlı
`stepper_z.position_endstop` (bağlanırken okunur; `pos_z`'de görünmez, + = nozzle tablaya yakın). İlk katman ısı
ihtiyacının baskıdan baskıya oynamasını ezmeyle ilişkilendirmek için.

2026-10-16'dan itibaren: `malzeme` = baskının malzemesi (G-code başlığındaki `filament_type`, ikiz baskı başında
okur; boş = bilinmiyor) → Unity Print kartında. `duraklat` = `GUC_AZALDI` `PAUSE_AFTER` (17.10'dan beri 150 s) kesintisiz sürdü ve baskı
yazdırılıyordu (`golge`: yalnız kayıt, baskı duraklatılmadı; `aktif`: Moonraker'a pause gönderildi); olay başına tek
satır dolu. Unity `duraklat`'ı henüz okumuyor: gölge moddan aktife geçilince eklenecek (kullanıcı kararı, 16.10).
Resmi alarm aralıkları (`anomali`) Unity'de zaman çubuğunda kırmızı şerit; N / P ile alarmdan alarma atlanır.

2026-10-19'dan itibaren: `code_version` = logu yazan kodun sürümü (`MODEL_VERSION` + ikizin kaynak dosyalarının kısa
içerik özeti, ör. `3dugum-gecikenkutle-v4+68444b15`); yalnız her dosyanın İLK satırında dolu. Unity okumuyor.

Alarm sütunu `ariza` = aktif resmi arızalar (Unity okuyor, yukarıda). 2026-10-12'den itibaren yalnızca iki tür:
`GUC_AZALDI` / `GUC_ARTTI` (eksik güç, hızlı arızalar) ve `KAYIP_*` (öğrenilen k'nin son kabul edilen seviyeye göre kayması; yeni seviyede 30 dk sabit kalırsa kabul edilir ve alarm kapanır, 2026-10-13).
Sıcaklık residual alarmı (`SICAKLIK_*`), `deneme_uyari` sütunu ve 11.10'da kısa süre yazılan `T1_gozlemci` / `ek_kayip`
sütunları kaldırıldı (eski loglarda görülebilir; Unity okumuyor).

Kurallar:
- Ondalık ayırıcı **nokta** (`22.87`), virgül değil.
- Boş hücre = "bu satırda yeni değer yok"; Unity son geçerli değeri tutar (ör. `T_yeni=0` satırlarındaki boş `residual`).
- Boolean değerler `True` / `False`.
- Kodlama UTF-8, ayırıcı virgül.

Unity tarafında sütun eşlemesi tek bir yerde: `Ender3_Twin/Assets/Scripts/TwinCsv.cs`.
Yeni bir sütunu Unity'de göstermek için oraya bir satır eklemek yeterli.

## Tahmin dosyası (Print modülü: plan ↔ gerçek)
İkizin G-code'dan ürettiği plan: `gcode_plan.py` → `dt/tahminler/tahmin_<ad>.csv`.
- Doğrulama modu (`--log`): ad = log adı; `time` sütunu var ve logun `time`'ı ile **aynı PC saati**
  (senkron: 2. katmandaki `M104 S240`). Unity bu dosyayı, zaman aralığı oynatılan kayıtla çakıştığı için eşleştirir.
- Canlı mod: `realtime_websocket.py` baskı başlayınca (`print_state` → `printing`) G-code'u ve printer.cfg'yi
  Moonraker'dan indirir, planı arka planda üretir → `tahmin_canli_<YYYYMMDD_HHMM>_<gcode adı>.csv`.
  `time` = baskının başladığı an (PC saati) + `t`; simülasyon o anda ölçülen sıcaklıklardan başlar.
- Elle baskı öncesi mod: ad = G-code adı; `time` yok, sadece `t` (Unity bunu bir kayda hizalayamaz).
- Zaman **bükülmez**: plan ile gerçek arasındaki katman zamanı farkı (takvim sapması) bir residual'dır, gizlenmemeli.

| Sütun | Anlamı |
|---|---|
| `time` | unix s (sadece doğrulama modunda) |
| `t` | simülasyon başından s (adım 0.3 s = Klipper PID aralığı) |
| `katman_z` | planlanan katman Z (mm, G-code). Logdaki `pos_z` Z ofseti kadar farklı olabilir |
| `tip` | Orca `;TYPE` bölgesi (Inner wall, Outer wall, Sparse infill, Internal Bridge, Top surface, Custom…) |
| `akis_anlik` | anlık planlanan hacimsel akış (mm³/s) |
| `akis` | ikizin hotend modelinde kullandığı 10 s ortalama net akış (mm³/s) |
| `T1`, `pwm` | tahmini nozul sıcaklığı (°C) ve ısıtıcı PWM'i (0–1; güç = pwm × 40 W) |
| `bed_T`, `bed_pwm` | tahmini tabla sıcaklığı ve PWM'i |
| `target`, `bed_target`, `fan` | G-code'dan gelen girdiler (hedefler, fan 0–1) |

## Bakım durumu dosyası
`dt/bakim_durumu.json`: ikiz her açılışta bakım göstergesinin sonucunu yazar (`realtime_websocket.maint_check`);
Unity açılış menüsünde gösterir, kuralı kendisi hesaplamaz. Alanlar: `time` (unix, yazıldığı an), `enough` (son 3 veri
gününde yeterli fansız baskı penceresi var mı), `advice` (öneri var mı), `direction` (`low` = k temiz referansın altında
→ heatsink tarafı; `high` = üstünde → çorap, fan kanalı, cereyan; boş = öneri yok), `reference` (1.00), `band` (0.06),
`days` (`[{date, k}]`, son 3 günün fansız baskı k medyanı), `text` (Türkçe konsol metni).

## Takım yolu dosyası (Unity'de G-code geometrisi)
Her canlı planın yanında `tahminler/yol_<plan adının tahmin_ sonrası>.bin` (ör. `tahmin_canli_X.csv` → `yol_canli_X.bin`).
İkili, little-endian: `"YOL1"` (4 bayt) + segment sayısı (int32) + her segment için 6 × float32:
`x0 y0 x1 y1 z e`. Koordinatlar G-code (makine) mm; `e` = planın başından beri kümülatif filaman (koşu maksimumu),
segment sonunda. Unity gerçek ilerlemeyi (`pos_e` koşu maksimumu − baskı başı) bu `e`'ye eşler: `e` ≤ ilerleme olan
segmentler görünür. Ardışık eş doğrultulu segmentler 0.05 mm toleransla birleştirilmiştir.
Planın E ekseni baskı başındaki Klipper ekstrüzyon moduna bağlıdır (`gcode_move.absolute_extrude`; START_GCODE'daki
prime çizgisi önceki baskıdan kalan modda yürür). 14.10'da üç baskıda gerçek nozul konumuyla hizalama: 0.0 mm.
