# Kasıtlı arıza deneyleri (v0.2 "induced faults on purpose") — plan, 2026-10-19

Amaç: etiketli arıza kütüphanesini büyütmek ve gölge duraklatmayı aktife almadan önce gerçek arızaların **baskı
sırasında** yakalandığını göstermek. Yalnız veri toplama; model, parametre ve alarm koduna dokunulmaz (kabul kapısı).
G-code'lar `py yazici/ariza_deneyleri.py` ile üretilir (taşıyıcı geometri, akış ve zamanlar oradaki sabitlerde).

## Neden yeniden deney? (eylül deneyleri bugünkü kodla, 19.10)

| Eylül (baskı yok, 245 °C sabit) | bugünkü ikiz |
|---|---|
| deney3 çorapsız | eksik güç −3.5 W (fansız) / −6.5 W (fanlı), GUC_ARTTI 332 s: güçlü |
| deney2 kanal folyolu | GUC yok, yalnız KAYIP_AZALDI_FAN 218 s (eksik güç +0.2 W) |
| deney1b mandal açık (1.65 mm/s) | GUC_AZALDI 29–35 s; 150 s duraklatmaya hiç varmazdı |
| deney4 havada baskı | alarm yok (doğru) |

Hiçbiri baskı değildi: `print_state`, gölge duraklatma (150 s), k_fil çapası/öğrenmesi ile arıza etkileşimi hiç
denenmedi. Aynen tekrarlamanın değeri düşük (sınıflandırıcı dördünü de doğru buluyor); değerli olan baskı içindeki hâli.

## Ortak tasarım

- **Algılanabilirlik akışla ölçeklenir.** Filaman terimi `K_FIL × akış × (T − T_oda)` ≈ 245 °C'de filaman mm/s başına
  1.19 W (fanlı baskıda × k_fil 1.33). Tam akış kesilmesi 2.5 W eşiğini ancak ~1.6 mm/s filaman (~3.8 mm³/s) üstünde
  geçer; deney1b'nin sınırda kalması bu. Taşıyıcı baskı bu yüzden ~7 mm³/s: 70 × 70 mm kare tüp, 2 duvar,
  0.65 × 0.3 mm çizgi, 40 mm/s, PETG 245 °C, tabla 85 °C, ilk 2 katman fansız sonra %25 (profilinizdeki gibi);
  katman ~14 s. Plan kontrolü (`gcode_plan.py`): akış 6.6–7.0 mm³/s, ısıtıcı ~26 W.
- **Etiketi saat değil yazıcı koyar.** Arıza noktasında G-code `PAUSE` verir: kafa park eder, LCD'de (M117) ne
  yapılacağı yazar; siz yapıp Mainsail'de **Resume**'a basarsınız. Logdaki `paused → printing` geçişi etiketin
  başlangıcıdır. İkiz devam etmeyi aynı baskı sayar (k_fil çapaya dönmez, log dönmez).
- **Kontrol penceresi kendiliğinden var.** Arıza kaldırıldıktan sonraki duraklat → devam penceresi aynı prosedürün
  arızasız hâli (plasebo kolu): duraklatma/devamın kendisi yanlış alarm üretiyor mu, onu da ölçer.
- **Önceden tahmin** her G-code'un başında yazılı (ölçmeden önce tahmin: sonra kıyaslanır).
- İkiz Pi'de çalışıyor, başlatılacak bir şey yok. Telefonda izleyin: `http://192.168.0.25:8090`.
- Pi'nin öğrendiği durumu kirletmeme: her arıza penceresi 4 dk (KAYIP yeni seviyeyi ancak 30 dk sabit kalınca kabul
  eder); taşıyıcı fanlı olduğu için baskıda yalnız k_fil öğrenir ve o her baskıda çapaya döner.

## Sıra (19.10'da kullanıcıyla kararlaştırıldı)

### 1. deney10 — ekstrüder mandalı açık (B) · `deney10_mandal_acik.gcode` · ~25 dk
- Fizik: filaman çıkmazsa onu ısıtmaya ve baskıya iletime giden ısı gitmez; motor dönmeye devam ettiği için
  `pos_e` artar, ikiz akış bekler.
- Tahmin: eksik güç ~**+4.6 W** → Resume'dan ~25 s sonra GUC_AZALDI, ~3 dk sonra gölge duraklatma (CSV `duraklat`).
  Kontrolde ±0.5 W, alarm yok.
- Adımlar: (1) Baskıyı başlat, 6 dk normal. (2) PAUSE 1 "B OPEN latch>Resume": mandalı tam aç, Resume. 4 dk boyunca
  kafa aynı katmanı 0.3 mm yukarıdan tekrar dolaşır, böylece tüpte boşluk oluşmaz. Resume'dan sonra plastik çıkıyorsa
  mandal açılmamıştır: hemen açın (ya da Cancel). (3) PAUSE 2 "B CLOSE latch>Resume": kapat, Resume, 4 dk kontrol.
- Not edilecek: yalnız olağandışı bir şey olursa (ör. "ilk ~10 s mandal yarı açıktı").
- Risk: yok denecek kadar az (mandal açıkken dişli boşta döner, filamanı kemirmez).

### 2. deney11 — parça fanının emişi kapalı (A), %25 ve %60 · `deney11_fan_kapali.gcode` · ~37 dk
- Fizik: blower bloğun alt kısmını (T2) ve taze baskıyı soğutur; emiş kapanınca ikisi de durur.
- Tahmin: **%25'te ~+2.7 W = sınırda** (fan kaybı ~1.6 W + k_fil'in fanlı payı ~1.1 W); belki yalnız eşik altı ya
  da KAYIP_AZALDI_FAN (eylül folyosu gibi). **%60'ta ~+5 W** → GUC_AZALDI + gölge duraklatma.
  %25 sonucu doğrudan şu soruyu cevaplar: gerçek ayarınızda ölü fan görülür mü?
- Adımlar: (1) 6 dk normal (%25). (2) PAUSE 1 "A1 COVER fan>Resume": 5015 blower'ın **emiş deliğini** (yüzündeki
  yuvarlak delik) kâğıt + bantla hava almayacak şekilde kapat, Resume, 4 dk. (3) PAUSE 2 "A1 UNCOVER": çıkar, Resume;
  3 dk kontrol, sonra G-code fanı %60'a alır ve 4 dk oturtur. (4) PAUSE 3 "A2 COVER": tekrar kapat, Resume, 4 dk.
  (5) PAUSE 4 "A2 UNCOVER": çıkar, Resume, 3 dk kontrol.
- **Asla heatsink fanını kapatmayın**: all-metal + PETG'de ısı sürünmesi hotend'i tıkar.
- Risk: düşük (yalnız çıkıntı kalitesi; blower kapalı emişle ısınmaz).

### 3. deney13 — çıkıntı "lalesi" (sarı seviye adayı) · `deney13_cikinti_lale.gcode` · ~45 dk
- Fizik: k_fil > 1 kısmı nozzle'dan alttaki katmana iletilen ısı. Çıkıntı açısı büyüyünce çizgi alttakine dayanamaz,
  havada asılı kalır, bu yol kaybolur → beklenenden az ısı gider (Pencil Topper'da Z 15.3'ten +1.5 W).
- Geometri: yan yana iki lale (x = 72 ve 163), 2 duvar, 2 katman dolu taban; Z 6 mm'ye kadar dik (referans), sonra
  çıkıntı açısı Z ile doğrusal 0° → 70° (Z 30). Z ↔ açı tablosu dosyanın başında; LCD her 5°'de açıyı yazar. Hız
  (40 mm/s) ve fan (%25) **bilerek sabit**: çıkıntı yavaşlaması/çıkıntı fanı yok, değişen tek şey temas.
- Tahmin: dik kısımda ~0 W; açı büyüdükçe eksik güç artı yönde **+1…1.5 W**'a doğru yükselir, PETG %25 fanda
  büyük olasılıkla ~50–60°'den itibaren. Resmi alarm beklenmiyor. k_fil öğrenicisi (1–2.5 W bölgesinde açık) bir
  kısmını yutabilir; analiz k_fil sabit tutularak da tekrarlanır.
- Adımlar: başlat, başka bir şey yok. Kenarın ilk kıvrıldığı/sarktığı açıyı LCD'den not edin (sonra cetvelle
  yüksekliği ölçün). Kenar ~1 mm'den fazla yukarı kıvrılınca **Cancel** (nozzle takılabilir); saati not edin.
- Risk: düşük; geç değil erken iptal edin.

### 4. deney12 — cereyan (C, GUC_ARTTI) · `deney12_cereyan.gcode` · ~25 dk
- Fizik: zorlanmış taşınım bloğun havaya kaybını (k terimi) artırır; PID daha fazla güç verir.
- Tahmin: bilinmiyor, ~1–5 W **fazla** kayıp (çorap bloğun çoğunu örtüyor). Baskı içindeki ilk GUC_ARTTI adayı.
  GUC_ARTTI duraklatmaz: yalnız büyüklük ve gecikme ölçülür.
- Adımlar: (1) 6 dk normal. (2) PAUSE 1 "C FAN ON>Resume": masa vantilatörünü ~50 cm önden, nozzle hizasında, en
  düşük kademede hotend'e çevirip aç, Resume, 4 dk. **Not edin: mesafe, kademe, yön.** (3) PAUSE 2 "C FAN OFF":
  kapat, Resume, 4 dk kontrol.
- Risk: düşük (parça biraz çarpılabilir; tabla PID'i telafi eder, tabla ikizi de görür).

### Elenenler
- **Filaman kesmek:** ısıl olarak deney10'un aynısı (B), fazladan yükleme zahmeti; gerçek "filaman bitti" zaten
  etiketli (1510 goofy bust).
- **Kısmi tıkanma:** `M221` kullanılamaz: ikiz akışı step motorun konumundan (`motion_report.live_position`) okur,
  M221 motoru gerçekten yavaşlattığı için ikiz bunu doğru biçimde arıza saymaz. Gerçek tıkanma oluşturmak all-metal
  + PETG'de kalıcı tıkanma riski. En yakın vekil zaten iki kez kayıtlı (SpeedTest eritme sınırı). İleride istenirse:
  mandal yarı gevşek → aralıklı kaydırma.
- **Çorap çıkarmak:** eylülde zaten güçlü yakalanıyor; sıcak nozzle'la elle iş. Daha önemlisi, uzun sürerse Pi'deki
  k çorapsız seviyeyi öğrenir ve sonraki normal baskılarda yanlış GUC_AZALDI verir (7/24 testini kirletir). Isı kaybı
  artışını (ARTTI) deney12 güvenli tarafta ölçüyor.
- **Fan kanalını çevirmek:** deney11 aynı fiziği (fan havası nozzle'a ulaşmıyor) ele nozzle'a yaklaşmadan ve geri
  alınabilir biçimde ölçüyor.

## Deney sonrası iş akışı

1. **Logu al.** Pi'de log baskı başına döner: deneyin logu bir sonraki baskı başlayana kadar
   `~/ender3-twin/realtime_log_ws.csv`, sonra `realtime_log_ws_onceki_<son yazma>.csv`. Explorer'da
   `\\192.168.0.25\ender3-twin` ya da PowerShell'de:
   ```
   scp batu63562@192.168.0.25:ender3-twin/realtime_log_ws_onceki_20261020_1432.csv loglar/deneyler/deney10_mandal_acik.csv
   ```
   (Dosya adı örnek; ilk satırdaki `print_filename`/zamandan doğru parça seçilir.) Servise dokunmayın.
2. **Adlandır.** `loglar/deneyler/deneyNN_<ad>.csv` (deney9 düzeni) ve etiket kopyası
   `benchmark/ariza_ornekleri/YYYY-MM-DD_HHMM_klipper_deneyNN_<ad>.csv` (HHMM = logun başladığı yerel saat).
3. **Pencere sınırlarını oku** (etiketlerin zaman tabanı `fault_classifier` ile aynı: Klipper `eventtime` − ilk satır):
   ```
   py -c "import sys,evaluate as ev; df=ev.read_log(sys.argv[1]); t=df.t-df.t.iloc[0]; ps=df.print_state.fillna('').astype(str); [print(f'{t[i]:8.1f} s  {ps[i]}') for i in df.index[ps!=ps.shift()]]; d=df[df.duraklat.notna()] if 'duraklat' in df else df[:0]; print('golge duraklatma:', [round(x,1) for x in t[d.index]])" loglar/deneyler/deney10_mandal_acik.csv
   ```
   R1 = ilk `printing` (paused'dan sonra) = arıza başı, P2 = ikinci `paused` = arıza sonu, R2 → son = kontrol.
4. **Etiket satırları** (`benchmark/ariza_ornekleri/etiketler.csv`, sütunlar değişmez; kullanıcı onayıyla eklenir):
   ```
   2026-10-DD_HHMM_klipper_deney10_mandal_acik.csv,<R1>,<P2>,"baski icinde ekstruder mandali acik (deney10, fan %25, 7 mm3/s)",B,<set>
   2026-10-DD_HHMM_klipper_deney10_mandal_acik.csv,<R2>,<son>,"deney10 kontrol: mandal kapali, ayni duraklat/devam",-,<set>
   ```
   deney11: iki A satırı (fan %25 / %60, gözlenen metninde fan yazılı) + iki "-" kontrol. deney12: bir C + bir "-".
   deney13: sonuç görülmeden beklenen yazılmaz (sarı adayı; açı ↔ eksik güç eğrisi önce incelenir, büyük olasılıkla
   B biçiminde = filaman yüküyle ölçekli). Bir log tek bir sete girer; etiketleri logun setini taşır.
5. **Set ataması** (`benchmark/setler.csv`): içeriğe bakmadan **mekanik dönüşüm**, eklendiği andaki son satırın
   tersi (bugün son satır 1810_2057 = ayar; Pi'nin 19.10 logları önce eklenirse ona göre). "Sınava iyi örnek olur"
   diye seçmek sınavı kirletir. Satır: `dosya,set,not,sha256_16,eklenme`; sınavsa parmak izi:
   ```
   py -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest()[:16])" loglar/deneyler/deney10_mandal_acik.csv
   ```
6. **Kontroller** (hepsi yalnız okur):
   ```
   py fault_classifier.py benchmark/ariza_ornekleri/<dosya>.csv --window <R1> <P2>
   py fault_classifier.py --labels
   py benchmark/scan_subthreshold.py --extra loglar/deneyler
   py evaluate.py --set calibration --live-start
   py evaluate.py --set test --live-start
   ```
   Bakılacaklar: GUC'nin Resume'dan gecikmesi, süresi, gölge duraklatma anı (`duraklat`), kontrolde yanlış olay,
   k_fil'in pencerede ne kadar kaydığı (yutma), sınıflandırıcının kategorisi; tahminle ölçülenin farkı.
7. **Kayıt.** Sonuç KARARLAR.md'ye (yeni [Kxx], MODEL DIŞI: veri) ve CLAUDE.md'deki etiket/duraklatma notlarına.

## Duraklatma kararına katkısı
Aktife geçiş (ROADMAP v0.2) için gereken iki sayı: gerçek B/A arızasında **tespit + duraklatma gecikmesi**
(deney10, deney11 %60) ve **yanlış duraklatma** (kontrol pencereleri + normal baskılar). Deney11 %25 "sarı"
çıkarsa bu bir sınır olarak README Limitations'a yazılır, eşik değiştirilmez (kabul kapısı).
