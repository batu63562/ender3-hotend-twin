# Baskı kuyruğu

Yazıcıyla yapılacak her şey, sırasıyla. Güncellik: 2026-10-22 gece (yarın köprü testiyle başlanıyor).
Bir madde bitince Claude'a söyle; işaretler, logu alır ve inceler.

## Kurallar (her seferinde)

- **İkiz hep açık** (Pi'de servis): ayrıca bir şey başlatma. Her baskı kendi log dosyası olur.
- **Görünür bir kusur olursa** (kalkma, sünme, topak, kayma): saatini ya da Z yüksekliğini not et → etiket olur.
- **Deneylerden (bölüm B) önce Claude'a haber ver**: Pi'deki öğrenilmiş k / k_fil'i kaydeder. Fanlı tutmalar k'yı
  oynatır (deney14a'da 1.007 → 0.959); deneyden sonra geri yükleme komutunu verir (sudo, sende):
  `ssh -t batu63562@192.168.0.25 "sudo systemctl stop ender3-twin && cp ~/ender3-twin/durum_yedek/<YEDEK>.json ~/ender3-twin/twin_state.json && sudo systemctl start ender3-twin"`
- Deneyler filament harcamaz (ekstrüzyon yok, nozzle 50 mm havada); yazıcının başında durman gerekmez.

## A. Normal baskılar

Duraklatma 22.10'dan beri AKTİF (K50): GUC_AZALDI 150 s sürerse ikiz baskıyı kendisi duraklatır (park X0 Y220).
Duraklatırsa iptal etme; Mainsail'de neden durduğuna bak (filaman, tıkanma), sonra Resume ya da Cancel, saati not et.
Başlamadan önce son kod Pi'de olsun (K55 deploy).

Ne basacağını bulamıyorsan: Orca Slicer'ın kendi kalibrasyon baskıları (Kalibrasyon menüsü) hem yazıcın için yararlı
hem ikize en çok şeyi öğretenler.

| # | Baskı | Filament | Süre | İkize ne öğretir |
|---|---|---|---|---|
| 1 | ~~İlk katmanı geniş parça + Pause/Resume~~ (21.10 BİTTİ: park doğrulandı, eksen kayması yakalandı, K47) | PETG | ne olursa | İlk katman ~2. dk sarı adayı (K37); park X0 Y220 ilk kez gerçek duraklatmada (K32) |
| 2 | ~~Sıcaklık kulesi 250 → 230~~ (21.10 BİTTİ) (Orca: Kalibrasyon → Sıcaklık) | PETG | ~1 sa | Baskı içinde hedef değişimi: ısınma/varış, k_fil'in sıcaklıkla değişimi |
| 3 | ~~TPU sıcaklık kulesi 235 → 215~~ (22.10 BİTTİ: 240 → 210, K64) | TPU | ~1 sa | **230 °C altında baskı** = PLA'daki durumun elindeki filamentle karşılığı (K42); log sonra yeni kodla yeniden oynatılır |
| 4 | ~~Aynı parça iki kez: fan %40 ve %60~~ (21.10 BİTTİ: benchy, K48) | PETG | 2 × kısa | Fan eğrisinin baskıdaki karşılığı (K39) |
| 5 | **Uzun baskı (> 2 sa)**, gece de olur | PETG | > 2 sa | k ve k_fil'in uzun süre kararlılığı; aktif duraklatmanın gözetimsiz saati. 21.10 sol-üst (2 sa 3 dk) kusurlu bitti → temiz biri hâlâ gerekli |
| 6 | ~~Çıkıntı / köprü testi~~ (22.10 BİTTİ: köprü testi ×3, K62) | PETG | kısa | Sarı aday (eşik altı 1–2.5 W); kusur olursa Z'yi ya da saati not et (sarkan köprü, kopan çıkıntı) |
| 7 | **Bir TPU baskı daha** (normal sıcaklıkta) | TPU | ne olursa | k_fil çapası 1.33 TPU'da tutuyor mu |
| 8+ | Ne istersen | PETG/TPU | — | Gözetimsiz çalışma verisi, yeni etiketler |

**Pause/Resume denemesi (1. baskıda, ilk katmandan sonra):** Mainsail → Pause → kafa X0 Y220'ye gitmeli (eskiden
X230 Y230'da gövdeye çarpıp baskıyı kaydırıyordu) → ~30 s bekle → Resume → baskı kaymadan devam etmeli. Saati not et.

## B. Deneyler (filamentsiz; yazıcı boşken)

| # | Deney | Dosya | Süre | Neden |
|---|---|---|---|---|
| 1 | ~~Fan eğrisi~~ (21.10 BİTTİ, K44) | `yazici/deney15_fan_egrisi.gcode` | ~45 dk | Fan 0/15/20/25/40/60/100 ve geri, 245 + 215 °C; bakım ve kanal montajından sonra fan ölçümü (K39/K40) |
| 2 | ~~Kalibrasyon baskısı, PETG sıcaklığı~~ (21.10 BİTTİ, K45) | `yazici/calibration_245.gcode` | ~55 dk | v0.3'ün ilk gerçek kalibrasyon verisi; deney15'ten SONRA |
| 3 | Kalibrasyon baskısı, PLA sıcaklığı | `yazici/calibration_210.gcode` (hazır; sanal yazıcıda 50 dk, aşma 1 °C) | ~55 dk | Model 235–245 dışında ilk kez (filament gerekmez) |
| 4 | Kalibrasyon baskısı, ABS/ASA sıcaklığı | `yazici/calibration_255.gcode` (hazır; sanal yazıcıda en çok 256.1 °C < max_temp 260) | ~55 dk | Üst uç (`max_temp` 260) |

Önerilen sıra: B1 → B2 aynı gün arka arkaya olabilir (arada geri yükleme gerekmez, ikisinden sonra bir kez). B3, B4
acelesiz.

## Önerilen sıra (23.10'dan)

- A6 köprü testi → A3 TPU sıcaklık kulesi (en önemlisi: göreli eşiğin gerçek sınavı) → A7 → A5 bir gece.
- B3/B4 boş bir günde, önce haber ver.

## Önerilen ilk günler (21–22.10, geçmiş)

- **Gün 1:** A1 (Pause/Resume ile) → boşta B1 (deney15) → A2 (PETG sıcaklık kulesi).
- **Gün 2:** A3 (TPU sıcaklık kulesi) → B2 (kalibrasyon 245) → durum geri yükleme → A4 (fan %40 / %60).
- **Sonra:** A5 gece, A6, A7; B3/B4 boş bir günde.
A1–A7 bitince gölge mod 14 baskıya çıkar → aktif duraklatma kararı için yeterli.

## Elde olmayan

PLA / ABS ile gerçek baskı yok: baskı içi doğrulama (k_fil çapası, %100 fanla baskı, PLA'da tespit hassasiyeti)
v0.4 test kullanıcılarına kalır (K42).

## Yeni nozzle takılınca (TZ3.0 bi-metal sertleştirilmiş çelik; K74 sırası)

0. Takmadan önce: uyumluluk (MK8/Ender hotend'i), heat break'e dayanmalı, sıcakken sık. Yeni nozzle 0.4 mm:
   `printer.cfg` `nozzle_diameter: 0.400` + Klipper yeniden başlat, Orca'da 0.4 profili.
1. **Yedek:** `twin_state.json` → `durum_yedek/`.
2. **Takarken ikiz KAPALI:** `sudo systemctl stop ender3-twin`.
3. **İkizi başlat, nozzle soğuyunca (< 40 °C) deney16** `yazici/deney16_fan_hafiza.gcode` (~24 dk, filamentsiz; K76).
   Tutma A eski "245 °C fansız 5 dk" adımının yerine (k'nın başlangıç ve 5. dk değeri); A > B ≈ C mi, fan hafızası
   çelikte de var mı? Bir iş olduğu için öğrendiği k baskı başında geri alınmaz (K75; boşta tutma alınırdı).
4. **Kalibrasyon baskısı** `yazici/calibration_245.gcode` (~55 dk, filamentsiz). Durumu geri YÜKLEME (yeni nozzle'ın k'sı).
5. **Isı kulesi** `yazici/heat_tower_245.gcode` (~56 dk, PETG ~11 g, tabla 80 °C; 0.4 nozzle, bölüm başı dara; K73, K77).
6. **Normal PETG baskılar**, toplam ≥ 4 saat (K74 sınama verisi). Logları Claude'a ver.

## Etiket bekleyenler (log gelince)

- 23.10: TPU retraction testi, her katta ipliklenme (nemli TPU; kurutulacak) → `-` beklenir (ısıl arıza değil).
- 23.10 12:00–13:00: TPU → PETG geçişinde nozzle tıkandı, elle açıldı (baskı yok) → `-`. Nozzle takılırken heatsink
  vidaları gevşemiş, sıkıldı (donanım aynı). İkiz durumu önceden alınan yedekten geri yüklendi
  (`durum_yedek/twin_state_kalibrasyon_oncesi.json`).

## Bitenler

- [x] deney10–13 kasıtlı arızalar (20.10, K32)
- [x] deney14a/b ısınma kapasitesi (20.10, K36)
- [x] K38 deploy: boşta log seyreltme (21.10)
- [x] Etiketler: 08.10, 15.10, 29.09, 13.10 bakım testi (21.10, K43)
- [x] Aktif duraklatma + k_ref 1.007 (22.10, K50); v0.3 adım 1–3 deploy (22.10, K52–K54)
