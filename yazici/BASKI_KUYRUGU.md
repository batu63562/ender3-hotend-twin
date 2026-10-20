# Baskı kuyruğu

Yazıcıyla yapılacak her şey tek yerde: bekleyen normal baskılar, deneyler ve neye yaradıkları.
Güncellik: 2026-10-20. Bir madde bitince işaretle (ya da Claude'a söyle, işaretlesin).

## Her deneyden önce ve sonra

- **Önce:** Claude'a haber ver; Pi'deki öğrenilmiş durumu (k, k_fil) kaydeder.
- **Fanlı tutma içeren deneylerden sonra** (deney15, kalibrasyon baskısı): öğrenilmiş k oynar (deney14a'da 1.007 → 0.959).
  Durumu geri yükle (sudo ister; Claude yedeğin adını verir):
  ```
  ssh -t batu63562@192.168.0.25 "sudo systemctl stop ender3-twin && cp ~/ender3-twin/durum_yedek/<YEDEK>.json ~/ender3-twin/twin_state.json && sudo systemctl start ender3-twin"
  ```
- Deney logları Pi'de kendiliğinden ayrı dosya olur; `py sync_pi_logs.py` ile PC'ye gelir.

## 1. v0.2 — normal baskılarla kendiliğinden birikenler

- [ ] **Pause/Resume denemesi** (bir sonraki normal baskıda, ilk katmandan sonra):
  Mainsail → Pause → kafa X0 Y220'ye gitmeli (eskiden X230 Y230'da gövdeye çarpıyordu) → ~30 s bekle → Resume →
  baskı kaymadan devam etmeli. Saati not et. (park makrosu 20.10'da `printer.cfg`'de; K32)
- [ ] **Gölge modda normal baskılar:** hedef 10–20, şu an Pi'de 7 normal baskı (19.10). Sonra aktif duraklatma kararı.
  Ne basacağını bulamıyorsan, ikize en çok şey öğretenler (yararlı bir şey basmak da serbest; çeşitlilik önemli):
  - **Sıcaklık kulesi** (PETG, 235→250 ya da 250→230, katman başına 5 °C): baskı içinde hedef değişimi → ikizin
    ısınma/varış davranışı ve k_fil'in sıcaklıkla değişimi baskı koşulunda (K36/K40 ısınma sorusuna doğrudan veri).
  - **Farklı fan seviyesiyle bir baskı** (%40 ve %60, aynı parça): fan eğrisinin baskıdaki karşılığı (K39: %100'de
    ölçülen çarpan %25–40'a taşınmıyor).
  - **Uzun bir baskı (> 2 saat):** k_fil ve k'nın uzun süre kararlılığı; gölge mod saati.
  - **Çıkıntı / köprü testi:** sarı aday (eşik altı 1–2.5 W) için; hata görürsen Z yüksekliğini not et.
  - **İlk katmanı geniş bir parça:** ilk katmanın ~2. dakikasındaki +1.2–1.4 W sarı adayı (K37).
  - **Bir TPU baskı:** malzeme çeşitliliği (çapa 1.33'ün TPU'da da tuttuğunu doğrulamak).
  Her baskıda görünür bir kusur olursa (kalkma, sünme, topak) saatiyle / Z'siyle not et → etiket.

## 2. Deneyler (yazıcı boşken; baskı malzemesi harcamaz, ekstrüzyon yok)

- [ ] **deney15 — fan eğrisi** (`yazici/deney15_fan_egrisi.gcode`, ~45 dk): fan 0/15/20/25/40/60/100 ve geri, 245 +
  215 °C. Eylül'deki fan ölçümünün bakım ve kanal montajı sonrası tekrarı (K39, K40). Sonra durum geri yükleme.
- [ ] **v0.3 kalibrasyon baskısı — ilk deneme** (`yazici/calibration_245.gcode`, ~55 dk; `yazici/calibration_print.py`
  ile üretildi): tam güç + yavaş rampa (hedef merdiveni, `max_power` değişmez), serbest soğuma, fan merdiveni,
  ±10 °C adımlar, inen tutmalar, farklı sıcaklıklardan tam güç adımları. deney9 + 14 + 15'in tek baskıda genelleştirilmiş hâli. Sanal yazıcıda denendi
  (`yazici/virtual_print.py`). deney15'ten SONRA (fan adımlarını ayrıca karşılaştırmak için). Sonra durum geri yükleme.

## 3. v0.3c — malzemeler (v0.3a refactor'undan ve PETG kalibrasyonundan SONRA)

PETG'ye bağlı yerler ve çözümleri yol haritası notunda (KARARLAR K42). PLA'nın asıl engeli kodda (öğrenme ve alarm
yalnız 230 °C üstünde) — o baskısız çözülür; aşağıdakiler ondan sonra anlamlı.
Elde yalnız PETG ve TPU var (20.10). Kalibrasyon baskısında ekstrüzyon olmadığı için modelin başka sıcaklıklarda
doğrulanması filament istemez; yalnız baskı içi kısım (k_fil çapası, %100 fanla baskı) gerçek PLA/ABS ister.
- [ ] **Düşük sıcaklık kalibrasyonu** (PLA aralığı, filamentsiz): `py yazici/calibration_print.py --temp 210 --mid 130
  --holds 190,170,150` (~55 dk). Model 235–245 dışında ilk kez.
- [ ] **Yüksek sıcaklık kalibrasyonu** (ABS/ASA aralığı, filamentsiz): `--temp 255 --holds 225,195,160` (`max_temp` 260).
- [ ] **TPU'yu 220–235 °C arasında basmak** (ör. TPU sıcaklık kulesi 235 → 220, ya da bir parça 225 °C'de): 230'un
  ALTINDA baskı = PLA'daki durumun elimizdeki filamentle karşılığı. Göreli eşik (v0.3a) bununla gerçek donanımda sınanır.
- [ ] **PETG sıcaklık kulesi** (230 → 250): baskı içi hedef değişimi, k_fil'in sıcaklıkla değişimi (bölüm 1'deki öneriyle aynı baskı).
- [ ] **1–2 TPU baskı daha** (çapanın TPU'da tuttuğunu tekrar görmek).
- PLA / ABS ile gerçek baskı: elde yok. Baskı içi doğrulama (k_fil çapası, %100 fan, tespit hassasiyeti) v0.4'teki
  test kullanıcılarına kalır; README'de "PLA/ABS baskı içi davranışı bu yazıcıda doğrulanmadı" diye yazılır.

## 4. Yazıcı başında, baskı değil

- [ ] **K38 deploy** (log seyreltme): `powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -PiUser batu63562`
  (baskı yokken). Deploy'dan sonraki ilk gece dosyası ~12 MB olmalı (önce ~43 MB).
- [ ] **İki belirsiz etiket** (K37, `benchmark/ariza_ornekleri/etiket_onerileri.csv`): 08.10 19:56 "X takıldı" baskısı
  (Mainsail History'de 19:55 civarı başlayan iş; X takılması alarmdan önce mi sonra mı?), 13.10 11:03 bakım testi
  (C mi "-" mi?).

## Bitenler

- [x] deney10–13 kasıtlı arızalar (20.10, K32)
- [x] deney14a/b ısınma kapasitesi (20.10, K36)
