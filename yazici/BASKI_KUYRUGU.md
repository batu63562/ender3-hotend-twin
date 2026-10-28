# Baskı kuyruğu

Yazıcıyla yapılacak her şey, sırasıyla. Güncellik: 2026-10-28 (çelik nozzle dönemi; sıra: aşağıda "Şimdiki sıra").
Bir madde bitince Claude'a söyle; işaretler, logu alır ve inceler.

## Kurallar (her seferinde)

- **İkiz hep açık** (Pi'de servis): ayrıca bir şey başlatma. Her baskı kendi log dosyası olur.
- **Görünür bir kusur olursa** (kalkma, sünme, topak, kayma): saatini ya da Z yüksekliğini not et → etiket olur.
- **Deneylerde yedek gerekmez (K79, 28.10):** `yazici/deney*.gcode` dosyalarında `; twin: experiment` satırı var;
  ikiz iş bitince öğrendiği k / k_ref'i iş başındaki değerlere kendisi döndürür (konsolda "Deney bitti … geri alindi").
  Kalibrasyon baskısı ve ısı kulesi İŞARETSİZ (öğrenmeleri istenir); aynı donanımda kalibrasyon tekrarı ise elle yedek.
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
| 5 | **Uç kalıntısı (K78 açık madde)** — normal bir PETG baskısından sonra, nozzle'ı TEMİZLEMEDEN, < 40 °C | `yazici/deney17_uc_kalintisi.gcode` | ~38 dk | Oturum başı 1.5 W: uçta plastik mi? İki PAUSE'da ucu sıcakken temizle, ne çıktığını not et, RESUME |

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

## Şimdiki sıra (28.10, çelik 0.4 nozzle)

Duraklatma 28.10'dan yeniden AKTİF (K84; servis bir sonraki boşta yeniden başlatılınca): GUC_AZALDI 150 s sürerse ikiz
duraklatır. Duraklatırsa iptal etme; nedenine bak, saati not et.

1. ~~Normal PETG baskılar, K74 için ≥ 4 saat~~ (28.10 BİTTİ: 6.03 saat, B geçmedi, A kalır, K84). Normal baskılar
   K63 sayacına gitmeye devam eder.
2. ~~deney17~~ (28.10 BİTTİ, K85: uç kalıntısı etkisi yok, tutmalar 17.1–17.4 W) (`yazici/deney17_uc_kalintisi.gcode`, ~38 dk) — normal bir baskının HEMEN ardından: filament takılı, nozzle
   TEMİZLENMEDEN, < 40 °C. İki PAUSE'da ucu sıcakken temizle, ne çıktığını not et, RESUME. Yedek gerekmez (K79).
3. ~~4 saat dolunca K74~~ (28.10 BİTTİ: A kalır; `pause_mode` satırı silindi, K84).
4. **Sonra, acelesiz:** ikinci ısı kulesi (yeni sürüm: 2 mm geri çekme + prime/silme; tekrarlanabilirlik ve "temas mı fan
   mı"), bir TPU baskı (A7), K63 değerlendirmesi (23.10'dan ≥ 20 PETG saati, sonuçlar dönem dönem de).

Bitti (27.10): nozzle değişimi, deney16 (fan hafızası çelikte yok), kalibrasyon baskısı, ısı kulesi (K78).

## Etiket bekleyenler (log gelince)

(yok; 23–26.10 etiketlendi, 26.10)

## Bitenler

- [x] deney10–13 kasıtlı arızalar (20.10, K32)
- [x] deney14a/b ısınma kapasitesi (20.10, K36)
- [x] K38 deploy: boşta log seyreltme (21.10)
- [x] Etiketler: 08.10, 15.10, 29.09, 13.10 bakım testi (21.10, K43)
- [x] Aktif duraklatma + k_ref 1.007 (22.10, K50); v0.3 adım 1–3 deploy (22.10, K52–K54)
