# Baskı kuyruğu

Yazıcıyla yapılacak her şey, sırasıyla. Güncellik: 2026-10-21 (yarın başlanıyor).
Bir madde bitince Claude'a söyle; işaretler, logu alır ve inceler.

## Kurallar (her seferinde)

- **İkiz hep açık** (Pi'de servis): ayrıca bir şey başlatma. Her baskı kendi log dosyası olur.
- **Görünür bir kusur olursa** (kalkma, sünme, topak, kayma): saatini ya da Z yüksekliğini not et → etiket olur.
- **Deneylerden (bölüm B) önce Claude'a haber ver**: Pi'deki öğrenilmiş k / k_fil'i kaydeder. Fanlı tutmalar k'yı
  oynatır (deney14a'da 1.007 → 0.959); deneyden sonra geri yükleme komutunu verir (sudo, sende):
  `ssh -t batu63562@192.168.0.25 "sudo systemctl stop ender3-twin && cp ~/ender3-twin/durum_yedek/<YEDEK>.json ~/ender3-twin/twin_state.json && sudo systemctl start ender3-twin"`
- Deneyler filament harcamaz (ekstrüzyon yok, nozzle 50 mm havada); yazıcının başında durman gerekmez.

## A. Normal baskılar (v0.2 gölge mod: hedef 10–20, şu an 7)

Ne basacağını bulamıyorsan: Orca Slicer'ın kendi kalibrasyon baskıları (Kalibrasyon menüsü) hem yazıcın için yararlı
hem ikize en çok şeyi öğretenler.

| # | Baskı | Filament | Süre | İkize ne öğretir |
|---|---|---|---|---|
| 1 | **İlk katmanı geniş herhangi bir parça + Pause/Resume denemesi** | PETG | ne olursa | İlk katman ~2. dk sarı adayı (K37); park X0 Y220 ilk kez gerçek duraklatmada (K32) |
| 2 | ~~Sıcaklık kulesi 250 → 230~~ (21.10 BİTTİ) (Orca: Kalibrasyon → Sıcaklık) | PETG | ~1 sa | Baskı içinde hedef değişimi: ısınma/varış, k_fil'in sıcaklıkla değişimi |
| 3 | **TPU sıcaklık kulesi 235 → 215** | TPU | ~1 sa | **230 °C altında baskı** = PLA'daki durumun elindeki filamentle karşılığı (K42); log sonra yeni kodla yeniden oynatılır |
| 4 | **Aynı parça iki kez: fan %40 ve %60** | PETG | 2 × kısa | Fan eğrisinin baskıdaki karşılığı (K39) |
| 5 | **Uzun baskı (> 2 sa)**, gece de olur | PETG | > 2 sa | k ve k_fil'in uzun süre kararlılığı; gölge mod saati |
| 6 | **Çıkıntı / köprü testi** (Orca: Kalibrasyon) | PETG | kısa | Sarı aday (eşik altı 1–2.5 W); kusur olursa Z'yi not et |
| 7 | **Bir TPU baskı daha** (normal sıcaklıkta) | TPU | ne olursa | k_fil çapası 1.33 TPU'da tutuyor mu |
| 8+ | Ne istersen | PETG/TPU | — | Gölge mod sayacı (10–20'ye tamamlamak) |

**Pause/Resume denemesi (1. baskıda, ilk katmandan sonra):** Mainsail → Pause → kafa X0 Y220'ye gitmeli (eskiden
X230 Y230'da gövdeye çarpıp baskıyı kaydırıyordu) → ~30 s bekle → Resume → baskı kaymadan devam etmeli. Saati not et.

## B. Deneyler (filamentsiz; yazıcı boşken)

| # | Deney | Dosya | Süre | Neden |
|---|---|---|---|---|
| 1 | ~~Fan eğrisi~~ (21.10 BİTTİ, K44) | `yazici/deney15_fan_egrisi.gcode` | ~45 dk | Fan 0/15/20/25/40/60/100 ve geri, 245 + 215 °C; bakım ve kanal montajından sonra fan ölçümü (K39/K40) |
| 2 | ~~Kalibrasyon baskısı, PETG sıcaklığı~~ (21.10 BİTTİ, K45) | `yazici/calibration_245.gcode` | ~55 dk | v0.3'ün ilk gerçek kalibrasyon verisi; deney15'ten SONRA |
| 3 | Kalibrasyon baskısı, PLA sıcaklığı | `py yazici/calibration_print.py --temp 210 --mid 130 --holds 190,170,150` | ~55 dk | Model 235–245 dışında ilk kez (filament gerekmez) |
| 4 | Kalibrasyon baskısı, ABS/ASA sıcaklığı | `py yazici/calibration_print.py --temp 255 --holds 225,195,160` | ~55 dk | Üst uç (`max_temp` 260) |

Önerilen sıra: B1 → B2 aynı gün arka arkaya olabilir (arada geri yükleme gerekmez, ikisinden sonra bir kez). B3, B4
acelesiz.

## Önerilen ilk günler

- **Gün 1:** A1 (Pause/Resume ile) → boşta B1 (deney15) → A2 (PETG sıcaklık kulesi).
- **Gün 2:** A3 (TPU sıcaklık kulesi) → B2 (kalibrasyon 245) → durum geri yükleme → A4 (fan %40 / %60).
- **Sonra:** A5 gece, A6, A7; B3/B4 boş bir günde.
A1–A7 bitince gölge mod 14 baskıya çıkar → aktif duraklatma kararı için yeterli.

## Elde olmayan

PLA / ABS ile gerçek baskı yok: baskı içi doğrulama (k_fil çapası, %100 fanla baskı, PLA'da tespit hassasiyeti)
v0.4 test kullanıcılarına kalır (K42).

## Bitenler

- [x] deney10–13 kasıtlı arızalar (20.10, K32)
- [x] deney14a/b ısınma kapasitesi (20.10, K36)
- [x] K38 deploy: boşta log seyreltme (21.10)
- [x] Etiketler: 08.10, 15.10, 29.09, 13.10 bakım testi (21.10, K43)
