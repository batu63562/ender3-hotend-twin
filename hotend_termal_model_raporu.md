# Ender 3 Hotend Termal Dijital İkiz — Model Türetimi ve Kalibrasyon Raporu

## 1. Sistem Tanımı

**Donanım:** Ender 3, direct drive, all-metal hotend (generic V6-tarzı kit), 24V/40W ısıtıcı kartuş,
EPCOS 100K B57560G104F termistör, Klipper firmware (Raspberry Pi + Mainsail), veri erişimi Moonraker
HTTP API üzerinden.

**Modellenen alt sistem:** Nozzle bloğu + heat break + heatsink arasındaki ısı transferi (2 düğümlü
lumped-parameter termal model).

**Girdiler:** PWM(t) — ısıtıcı duty cycle (0-1), fan_state(t) — part-cooling fan durumu (0-1)
**Çıktı:** T1(t) — nozzle bloğu / termistör sıcaklığı

## 2. Model Denklemleri

İki düğüm: **Düğüm 1** (nozzle bloğu, ısıtıcı + termistör burada) ve **Düğüm 2** (heatsink).

```
Düğüm 1 (nozzle bloğu):
    C1 * dT1/dt = PWM(t)*P_max - (T1-T2)/R_hb - hA1(t)*(T1 - T_amb)

Düğüm 2 (heatsink):
    C2 * dT2/dt = (T1-T2)/R_hb - hA2*(T2 - T_amb)

Fan'a bağlı taşınım katsayısı (VOF faz oranı / termodinamik kuruluk derecesi x ile
aynı matematiksel yapı — doğrusal enterpolasyon):
    hA1(t) = hA1_still + (hA1_fan - hA1_still) * fan_state(t)
```

**Parametreler:**
- `C1 = m1*cp1` — nozzle bloğunun ısı sığası (J/K)
- `C2 = m2*cp2` — heatsink'in ısı sığası (J/K), bu projede kalibre edilmedi, sabit kabul edildi
- `R_hb` — heat break termal direnci (K/W)
- `hA1_still`, `hA1_fan` — Düğüm 1'in fan kapalı/açık haldeki toplam taşınım katsayısı (W/K)
- `hA2` — heatsink'in taşınım katsayısı (W/K), sabit kabul edildi
- `P_max = 40 W`, `T_amb = 25°C` (sabit varsayıldı)

## 3. Kalibrasyon Metodolojisi

### 3.1 Neden aşamalı (staged) kalibrasyon gerekti

İlk denemede tüm parametreler (`C1, R_hb, hA1_still, hA1_fan`) tek seferde, rastgele bir başlangıç
noktasından `scipy.optimize.least_squares` ile tüm zaman serisine fit edildi. Bu **başarısız oldu**
(RMSE ≈ 48°C) çünkü problem **non-convex** — optimizasyon kötü bir yerel minimuma sıkıştı.

**Çözüm — 3 aşamalı yaklaşım:**

1. **Aşama 1 (Isınma rampası, pwm≈1 sabit):** Sadece `C1` fit edilir (diğerleri kabaca sabit tutulur).
   Bu segment `C1`'e en duyarlı olduğu için tek parametreli, kolay bir problem.
2. **Aşama 2 (Plato/denge bölgesi, dT1/dt≈0):** Enerji dengesinden (`Q_ortalama = hA1*(T_denge-T_amb)`)
   doğrudan `hA1_still` ve `hA1_fan` kaba tahmini çıkarılır.
3. **Aşama 3 (Warm-start global fit):** Aşama 1-2'nin sonuçları, `least_squares`'e **rastgele değil,
   fiziksel olarak mantıklı bir başlangıç noktası** olarak verilir; tüm 4 parametre (+R_hb) birlikte
   tüm zaman serisine (rampa+plato+fan+soğuma) fit edilir.

Bu yöntemle RMSE ≈ 48°C'den ≈ 3-5°C'ye düşürüldü.

### 3.2 Parametre belirsizliği (non-identifiability) ve çoklu-deney kısıtlaması

`h1` (havaya kaçış) ile `R_hb` (heatsink'e kaçış), tek bir `T1(t)` eğrisi üzerinde birbirine çok
benzer etkiler yaratabildiği için (ikisi de "nozzle'da ısı tutma" sonucu veriyor), tek deneyden ayrı
ayrı güvenilir şekilde kestirilemiyor. Bunu azaltmak için **çoklu deney kimliklendirmesi** kullanıldı:
çoraplı ve çorapsız veri setleri **ortak** `C1` ve `R_hb` (fiziksel olarak değişmeyen parametreler)
paylaşacak, ama **ayrı** `hA1_still`/`hA1_fan` (çorabın etkilediği tek parametre) alacak şekilde
**birlikte** (joint) fit edildi.

## 4. Sonuçlar

### 4.1 Kalibre edilmiş parametreler (çoraplı veri, tek başına warm-start fit)

| Parametre | Değer |
|---|---|
| C1 (m1·cp1) | 5.57 J/K |
| hA1_still | 0.075 W/K |
| hA1_fan | 0.143 W/K |
| R_hb | 68.2 K/W |
| RMSE (tam eğri) | 3.16°C |

### 4.2 Çorap etkisi — joint fit sonucu

| Parametre | Çorapsız | Çoraplı | Oran (çorapsız/çoraplı) |
|---|---|---|---|
| hA1_still (fan kapalı) | ~0.068-0.071 | ~0.065-0.075 | ≈1.0 (fark yok) |
| hA1_fan (fan açık) | ~0.143-0.151 | ~0.099-0.105 | **≈1.4-1.5x** |

**Yorum:** Fan kapalıyken (doğal konveksiyon) çorabın ölçülebilir bir etkisi yok — bu rejimde ısı
transferi zaten zayıf. Fan açıkken (zorlanmış konveksiyon) çıplak metal, çoraplı hale göre
**%40-50 daha fazla** ısı kaybediyor — 200°C'de bu yaklaşık 9W'lık bir fark (ısıtıcı maksimum
gücünün ~%23'ü), çorabın PID'nin işini neden kolaylaştırdığını sayısal olarak açıklıyor.

**⚠️ Sınırlama:** Çoraplı test tek seferde yapıldı (dosya adında "sock" geçen). Çorapsız testler
**iki farklı zamanda**, aralarında ortam soğuduktan sonra tekrarlandı — yani "sadece çorap değişti,
her şey sabit kaldı" şartı tam sağlanmadı (oda sıcaklığı, fan pozisyonu gibi kontrolsüz değişkenler
olabilir). Bu yüzden yukarıdaki oran kesin bir sayı değil, **yaklaşık bir mertebe** olarak
okunmalıdır. Daha kesin bir sonuç için: aynı gün, aynı sırayla, oda sıcaklığı sabitken çoklu tekrar
testi ve/veya heatsink'e ikinci bir termistör eklenmesi önerilir.

## 5. Sonraki Adımlar

- Bu kalibre model, projenin 6-7. adımları olan **residual hesaplama** ve **anomali tespiti**
  için referans olarak kullanılacak.
- `T_real(t) - T_model(t)` sürekli izlenerek, belirli bir eşiği aşan sapmalar "termal kaçak",
  "fan arızası" gibi durumlar olarak yorumlanabilir.
