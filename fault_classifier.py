"""
Ariza siniflandirma (CEVRIMDISI - canli scripte dokunmaz).

Canli ikiz arizayi "yon" olarak bulur (SICAKLIK_YUKSEK_FAN vb.). Bu arac bir ariza olayini
logdan alip HANGI FIZIKSEL TERIMIN bozuldugunu arar:

    A  fan yolu        kanal tikali / dondu, fan yavasladi      -> sapma fan kaybiyla olceklenir
    B  filaman akmiyor tikanma, filaman bitti, disli kaziyor    -> sapma filaman yukuyle olceklenir
    C  yalitim/cevre   corap dustu, cereyan, topak, kutu isindi -> sapma tum hava kaybiyla olceklenir
    D  termistor       fiziksel olarak imkansiz okuma hizi
    E  isitici         isitici %100'de ama sicaklik dusuyor

Yontem - enerji dengesi (sicaklik tutulurken PID gucu kayiplari karsilar):
    eksik_guc = modelin bekledigi kayip - (olculen guc - depolanan enerji hizi)
    (+ = beklenenden AZ isi gidiyor, - = beklenenden FAZLA isi gidiyor)
Bu enerji sinyali "anlamli bir sapma var mi" kapisi ve fan degisen pencerelerdeki ayristirma
icin kullanilir. Tek terimli hipotezler ise IKIZ GERI OYNATMASIYLA test edilir: ikiz olay
anindaki durumdan her hipotezle ayri ayri yeniden oynatilir (isil gecikme modelin icinde),
gunluk kayip carpani ve mumkunse filaman katsayisi olaydan onceki referanstan yerinde olculur
(bkz. replay_hypotheses). Hipotezler:
    A: sapma = a * fan_kaybi            (a = kaybolan fan sogutmasi orani; <0 fazladan sogutma)
    B: sapma = b * filaman_yuku         (b = akmayan filaman orani)
    C: sapma = c * (blok_kaybi + fan)   (c = tum hava kaybindaki oransal degisim)
Oran fiziksel sinirlar icindeyse ve uyum iyiyse hipotez "uyumlu". Iki hipotez veride ayni
sekli veriyorsa (ornegin fan hic degismediyse A ile C) ikisi birden raporlanir: veri o
ayrimi yapamiyor demektir.

Fan pencerede degistiyse (ve ekstruzyon yoksa) A/C ayrimi dogrudan yapilir: sapma blok ve fan
terimlerine AYRI ayri bolunur,
    sapma = -(x * blok_kaybi + y * fan_kaybi)
x (blok kaybi degisimi) normal bandin disindaysa C, x normal ve y (fan sogutmasi degisimi)
disindaysa A. Bantlar benchmark'taki normal fanli loglardan (X_BAND, Y_BAND).

Kullanim:
    python ariza_siniflandir.py LOG.csv                  # ikizin buldugu olaylari siniflandir
    python ariza_siniflandir.py LOG.csv --pencere 330 480  # elle verilen pencereyi (s) siniflandir
    python ariza_siniflandir.py --etiketler              # benchmark/ariza_ornekleri/etiketler.csv
"""
import argparse
import os

import numpy as np
import pandas as pd

import evaluate as d

HERE = os.path.dirname(os.path.abspath(__file__))
LABEL_FILE = os.path.join(HERE, "benchmark", "ariza_ornekleri", "etiketler.csv")

GRID_DT = 0.5          # s - loglar bu aralikta esit ornekleniyor
SMOOTH = 20.0          # s - guc/kayip sinyalleri bu pencerede ortalanir (PID gurultusu + gecikme)
QUASI_RATE = 0.3       # C/s - bundan hizli degisimde enerji dengesi guvenilmez (rampa)
T_VALID = 150.0        # C - bunun altinda analiz yapilmaz
REF_LEN = 180.0        # s - olaydan onceki referans penceresi
REF_MIN = 30.0         # s - referansta en az bu kadar gecerli veri olmali
ANALYSIS_LEN = 300.0   # s - olay basindan itibaren en fazla bu kadari incelenir
MIN_SHIFT = 1.2        # W - sapma (ortalama ya da bir hipotezin acikladigi kisim) bundan kucukse
                       #     "anlamli guc farki yok". Normal benchmark pencerelerinde |ortalama|
                       #     en fazla 0.91 W, %95'i 0.67 W.
MIN_TERM = 0.3         # W - terimin ortalamasi bundan kucukse o hipotez test edilemez
                       #     (ornegin fan kapaliyken A, ekstruzyon yokken B)
RMS_TIE = 1.25         # en iyi uyumun rms'inin bu kati icindeki hipotezler de "uyumlu"
BOUNDS = {"A": (-1.5, 1.15), "B": (-0.3, 1.15), "C": (-0.6, 0.6)}
RES_RATE_MAX = 8.0     # C/s - D: okuma modelden bu hizda ayrisiyorsa fiziksel degil (sicrama).
                       #     Normal loglarda residual en fazla 2.9 C/s (klipper), 5.8 C/s (eski http)
                       #     degisiyor. Okumanin kendi hizi kullanilamaz: soguk isinmada 10 C/s normal.
SAT_PWM = 0.95         # E: isitici doygun sayilir
FAN_SPREAD = 2.0       # W - fan kaybi pencerede en az bu kadar degisiyorsa (ornegin fan 0 -> %50,
                       #     245C'de 4.4 W) blok ve fan terimi ayri ayri cozulur
# iki terimli ayristirmada normal bantlar (benchmark'taki fanli 3 log, 19 pencere, 2026-09-25):
# blok |x| <= %2.4, fan y -%18..0 (ortalama -%8: model fan sogutmasini ~%8 fazla tahmin ediyor).
# Tek gunluk veri -> genis tutuldu; blok bandi canli k alarmiyla (K_ALARM_BAND) ayni.
X_BAND = 0.08
Y_BAND = (-0.22, 0.12)


# ---------------------------------------------------------------------------
# Sinyaller
# ---------------------------------------------------------------------------
def bed_scale(df, m):
    """Her satir icin kartus guc carpani 1 - BED_SAG * tabla_pwm (tabla kaydi yoksa 1)."""
    if "bed_pwm" not in df:
        return np.ones(len(df))
    bp = pd.to_numeric(df.bed_pwm, errors="coerce").ffill().fillna(0.0).values
    return 1.0 - m.BED_SAG * bp


def signals(df, m):
    """Logu esit zaman izgarasina cevirip enerji dengesi terimlerini hesaplar."""
    t = (df.t - df.t.iloc[0]).values
    hasT = df.has_T.values
    g = np.arange(0.0, t[-1], GRID_DT)
    idx = np.searchsorted(t, g, side="right") - 1          # sifirinci dereceden tutma
    pwm, fan, ext = df.pwm.values[idx], df.fan_state.values[idx], df.extrude_velocity.values[idx]
    T = np.interp(g, t[hasT], df.T1_real.values[hasT])

    s = pd.DataFrame({"t": g, "T": T, "pwm": pwm, "fan": fan})
    n2 = max(1, int(round(m.EXT_AVG_WINDOW / GRID_DT)))     # ikizle ayni net akis ortalamasi
    s["flow"] = np.clip(pd.Series(ext).rolling(n2, min_periods=1).mean().values, 0, None)
    s["P"] = pwm * m.P_MAX * bed_scale(df, m)[idx]     # tabla acikken kartus gucu azalir (ikizle ayni)
    # T2 (nozzle tarafi) ve T3 (kartus) olculmuyor: modelin kendi denklemleriyle, GERCEK T1 ve
    # olculen guc surulerek hesaplanir. Bunlarin depoladigi isi dengeye girmezse fan acilirken
    # (T2 isi birakir) ve isinma sonrasi (T3 isi birakir) ~2 W sahte sapma cikiyordu.
    fanc = np.array([m.fan_conductance(f) for f in fan])     # modelin kendi fan kurali (esik dahil)
    T1s, T2s, T3s = m.initial_state(T[0], pwm[0], fan[0])
    T2 = np.empty(len(g)); T3 = np.empty(len(g))
    for i in range(len(g)):
        T2[i], T3[i] = T2s, T3s
        # T1 bu adimda sabit sayilir -> her iki dugum icin tam ustel cozum (kosulsuz kararli)
        G2 = 1 / m.R_HB + m.HA2 + fanc[i]
        T2_inf = (T[i] / m.R_HB + (m.HA2 + fanc[i]) * m.T_AMB) / G2
        T2s = T2_inf + (T2s - T2_inf) * np.exp(-GRID_DT * G2 / m.C2)
        T3_inf = T[i] + s.P.values[i] * m.R31
        T3s = T3_inf + (T3s - T3_inf) * np.exp(-GRID_DT / (m.R31 * m.C3))
    s["L_blok"] = m.HA1 * (T - m.T_AMB) + m.CRAD * ((T + 273.15) ** 4 - m.T_AMB_K ** 4)
    s["L_hs"] = m.HA2 * (T2 - m.T_AMB)
    s["L_fan"] = fanc * (T2 - m.T_AMB)
    s["L_fil"] = m.K_FIL * s.flow * (T - m.T_AMB)
    s["store"] = (m.C1 * np.gradient(T, g) + m.C2 * np.gradient(T2, g) + m.C3 * np.gradient(T3, g))

    n = max(1, int(round(SMOOTH / GRID_DT)))
    sm = s.rolling(n, center=True, min_periods=n // 2).mean()
    sm["t"] = g
    sm["dTdt"] = np.gradient(sm["T"].values, g)
    # E kurali icin hizli turev (2 s): kesik kesik guc kaybi 20 s ortalamada kayboluyordu
    n_fast = max(1, int(round(2.0 / GRID_DT)))
    sm["dTdt_fast"] = np.gradient(pd.Series(T).rolling(n_fast, center=True, min_periods=1).mean().values, g)
    # son 3 s'deki en dusuk pwm: PID tutma sirasinda tek ornekli %100 sivrilikleri normal (fan
    # acilinca vb.); ariza ise KESINTISIZ doygunluk demek
    sm["pwm_min3"] = pd.Series(pwm).rolling(max(1, int(round(3.0 / GRID_DT))), min_periods=1).min().values
    # son 15 s'deki en yuksek pwm: isinma sonunda PID gucu kesince kartusta biriken isi birkac
    # saniye daha sicakligi yukseltir (normal). 15 s tamamen kapaliysa o isi bitmistir (R31*C3 ~2.5 s).
    sm["pwm_max15"] = pd.Series(pwm).rolling(max(1, int(round(15.0 / GRID_DT))), min_periods=1).max().values
    sm["gap"] = (sm.L_blok + sm.L_hs + sm.L_fan + sm.L_fil) - (sm.P - sm.store)
    # veri boslugu yakinindaki noktalar gecersiz
    near_gap = np.zeros(len(g), bool)
    for i in np.where(np.diff(t) > m.GAP_THRESHOLD)[0]:
        near_gap |= (g > t[i] - SMOOTH) & (g < t[i + 1] + SMOOTH)
    sm["valid"] = (sm["T"] > T_VALID) & (sm.dTdt.abs() < QUASI_RATE) & ~near_gap & sm.gap.notna()
    # ham sinyaller (D/E kurallari icin, yumusatilmamis)
    sm["T_raw"], sm["pwm_raw"] = T, pwm
    return sm, t, hasT


# ---------------------------------------------------------------------------
# Siniflandirma
# ---------------------------------------------------------------------------
def fan_varies(seg):
    """Fan kaybi pencerede yeterince degisiyor mu (blok ve fan terimi ayri ayri cozulebilir mi)."""
    lf = seg.L_fan.values
    return np.percentile(lf, 90) - np.percentile(lf, 10) >= FAN_SPREAD


def fit_two_term(dg, seg):
    """sapma = -(x * blok_kaybi + y * fan_kaybi):  x = blok kaybindaki, y = fan sogutmasindaki
    oransal degisim (+ = gercekte DAHA FAZLA kayip). Donus: x, y, uyum rms (W)."""
    X = -np.column_stack([seg.L_blok.values, seg.L_fan.values])
    (x, y), *_ = np.linalg.lstsq(X, dg, rcond=None)
    rms = float(np.sqrt(np.mean((dg - X @ [x, y]) ** 2)))
    return float(x), float(y), rms


def fit_hypotheses(dg, terms):
    """dg: referansa gore sapma (W). terms: {hipotez: regresor dizisi}. Tek parametreli LS."""
    out = {}
    for h, x in terms.items():
        if np.mean(np.abs(x)) < MIN_TERM:
            out[h] = dict(oran=np.nan, rms=np.nan, uygun=False, neden=f"terim ~0 ({np.mean(x):.2f} W): test edilemez")
            continue
        a = float(np.dot(x, dg) / np.dot(x, x))
        rms = float(np.sqrt(np.mean((dg - a * x) ** 2)))
        lo, hi = BOUNDS[h]
        ok = lo <= a <= hi
        out[h] = dict(oran=a, rms=rms, uygun=ok,
                      neden="" if ok else f"oran {a:+.2f} fiziksel sinir [{lo}, {hi}] disinda")
    return out


# ---------------------------------------------------------------------------
# Ikiz geri oynatmasiyla hipotez testi
# ---------------------------------------------------------------------------
# Enerji penceresi (20 s ortalama) PID ve isil gecikmeyi kaba yakalar: kisa bir ekstruzyonda
# guc farki ekstruzyon bittikten sonra da suruyor, filaman testi yaniliyordu (deney 1).
# Burada ikiz, olay anindaki durumdan baslayip olculen guc/fan/akisla her hipotez icin ayri
# ayri yeniden oynatilir; gecikme modelin icinde oldugu icin kendiliginden hesaba katilir.
# Filaman yuku baglama gore degisiyor (havaya serbest ekstruzyon, baskidaki K_FIL'in 1.33 kati
# isi cekti - deney 1). O yuzden referans doneminde hem ekstruzyonlu hem ekstruzyonsuz kisim
# varsa filaman katsayisi da yerinde olculur ve B orani ona gore verilir.
REPLAY_EPS = 0.05      # Gauss-Newton sonlu fark adimi (carpan)
REPLAY_TIE_C = 0.3     # C - en iyi uyuma bu kadar yakin hipotezler de "uyumlu"
REF_EXT_MIN = 20.0     # s - referansta en az bu kadar ekstruzyonlu VE ekstruzyonsuz sure varsa
                       #     filaman katsayisi yerinde olculur
K_FIL_PRIOR = 0.15     # K_FIL yerinde olculemezse oransal belirsizligi: baskida kalibre edildi (tek
                       # baski, tek malzeme); serbest ekstruzyonda 1.33 kati olculdu (baska baglam)
B_MAX = 1.05           # filaman testi: belirsizlik (2 sigma) hesaba katildiktan sonra bile akmayan
                       # filaman orani bunu asiyorsa B elenir (filaman yukunden fazlasi kaybolamaz)


def replay_inputs(df, m):
    """Twin ile ayni girdi sirasi: aralik (t[i-1], t[i]] onceki mesajin pwm/fan'i ve son 2 s'nin
    isaretli ortalama akisiyla (0'in alti kirpilir) ilerletilir."""
    t = (df.t - df.t.iloc[0]).values
    ext = df.extrude_velocity.values
    ext_sm = np.zeros(len(t))
    hist = []
    for i in range(1, len(t)):
        hist.append((t[i], t[i] - t[i - 1], ext[i - 1]))
        while hist and hist[0][0] < t[i] - m.EXT_AVG_WINDOW:
            hist.pop(0)
        span = sum(h[1] for h in hist)
        ext_sm[i] = max(sum(h[1] * h[2] for h in hist) / span, 0.0) if span > 0 else max(ext[i - 1], 0.0)
    return dict(t=t, T=df.T1_real.values, hasT=df.has_T.values, pwm=df.pwm.values * bed_scale(df, m),
                fan=df.fan_state.values, ext_sm=ext_sm)


WARMUP = 30.0          # s - geri oynatma baslangictan bu kadar once baslar (T2/T3 otursun), sonra
                       #     ucu de gercek T1'e kaydirilir. PID gucu 5 s'de %37-65 oynadigi icin
                       #     T3'u o anki guce gore kurmak ~15C hatali baslangic ve sahte gecis veriyordu.


def _real_T1(R, i):
    last = (R["t"] >= R["t"][i] - 1.0) & (R["t"] <= R["t"][i]) & R["hasT"]
    return float(R["T"][last].mean()) if last.any() else float(R["T"][i])


def _sim(ctx, i0, i1, kn, kf, kfil, base=None):
    """Satir i0'dan i1'e ikizi carpanlarla oynat (kn: blok kaybi, kf: fan sogutmasi, kfil: filaman).
    Baslangic: WARMUP saniye once 'base' carpanlariyla (verilmezse ayni carpanlar) oynatilir, i0'da
    uc dugum birlikte gercek T1'e kaydirilir (ic sicaklik farklari = isi akislari korunur).
    Donus: (i0, i1] araligindaki sicaklik olcumlerinde gercek - ikiz."""
    m, R = ctx["m"], ctx["rp"]
    t, T, hasT, pwm, fan, ext = R["t"], R["T"], R["hasT"], R["pwm"], R["fan"], R["ext_sm"]
    iw = int(np.searchsorted(t, t[i0] - WARMUP))
    T1 = _real_T1(R, iw)
    T1, T2, T3 = m.initial_state(T1, float(pwm[max(0, iw - 20):iw + 1].mean()), fan[iw])

    def f(T1, T2, T3, u, fc, q, kn, kfil):
        q31 = (T3 - T1) / m.R31; qhb = (T1 - T2) / m.R_HB
        d1 = (q31 - qhb - kn * m.natural_loss(T1) - kfil * m.K_FIL * q * (T1 - m.T_AMB)) / m.C1
        d2 = (qhb - m.HA2 * (T2 - m.T_AMB) - fc * (T2 - m.T_AMB)) / m.C2
        return d1, d2, (u * m.P_MAX - q31) / m.C3

    bn, bf, bfil = base if base is not None else (kn, kf, kfil)
    out = []
    for i in range(iw + 1, i1 + 1):
        if i == i0 + 1:                      # olay basi: gercek sicakliga kaydir
            off = _real_T1(R, i0) - T1
            T1, T2, T3 = T1 + off, T2 + off, T3 + off
        on = i > i0
        cn, cf, cfil = (kn, kf, kfil) if on else (bn, bf, bfil)
        dt = t[i] - t[i - 1]
        u, fc, q = pwm[i - 1], cf * m.fan_conductance(fan[i - 1]), ext[i]
        n = max(1, int(np.ceil(dt / 0.25))); h = dt / n     # model kararlilik siniri ~2.7 s
        for _ in range(n):
            a = f(T1, T2, T3, u, fc, q, cn, cfil)
            b = f(T1 + h/2*a[0], T2 + h/2*a[1], T3 + h/2*a[2], u, fc, q, cn, cfil)
            c = f(T1 + h/2*b[0], T2 + h/2*b[1], T3 + h/2*b[2], u, fc, q, cn, cfil)
            e = f(T1 + h*c[0], T2 + h*c[1], T3 + h*c[2], u, fc, q, cn, cfil)
            T1 += h/6*(a[0] + 2*b[0] + 2*c[0] + e[0]); T2 += h/6*(a[1] + 2*b[1] + 2*c[1] + e[1])
            T3 += h/6*(a[2] + 2*b[2] + 2*c[2] + e[2])
        if on and hasT[i]:
            out.append(T[i] - T1)
    return np.array(out)


def _fit(ctx, i0, i1, to_mult, p0, base=None, cov=False):
    """Gauss-Newton (2 tur): to_mult(p) -> (kn, kf, kfil). Donus: p, residual rms (C)[, kovaryans].
    Kovaryans: residual ornekleri 0.25 s aralikli ve cok ilintili; bagimsiz ornek ~ her 10 s'de bir."""
    p = np.array(p0, float)
    for _ in range(2):
        r = _sim(ctx, i0, i1, *to_mult(p), base=base)
        J = np.column_stack([(_sim(ctx, i0, i1, *to_mult(p + REPLAY_EPS * e), base=base) - r) / REPLAY_EPS
                             for e in np.eye(len(p))])
        p = p + np.linalg.lstsq(J, -r, rcond=None)[0]
    r = _sim(ctx, i0, i1, *to_mult(p), base=base)
    rms = float(np.sqrt(np.mean(r ** 2)))
    if not cov:
        return p, rms
    n_eff = max((ctx["rp"]["t"][i1] - ctx["rp"]["t"][i0]) / 10.0, 1.0)
    return p, rms, rms ** 2 * np.linalg.pinv(J.T @ J) * len(r) / n_eff


def replay_baseline(ctx, t_start):
    """Referans doneminden gunluk kayip carpani (kb) ve mumkunse filaman katsayisi (kfil).
    Donus: kb, kfil, kfil'in oransal belirsizligi (1 sigma), not."""
    R = ctx["rp"]; t = R["t"]
    i0 = int(np.searchsorted(t, max(t_start - REF_LEN, 0.0)))
    i1 = int(np.searchsorted(t, t_start))
    sm = ctx["sm"]
    ref = (sm.t >= t[i0]) & (sm.t < t_start)
    if i1 - i0 < 20 or sm.valid[ref].sum() * GRID_DT < REF_MIN or sm.valid[ref].mean() < 0.7:
        return 1.0, 1.0, K_FIL_PRIOR, "referans yok/yetersiz: gunluk carpan 1, filaman katsayisi model (K_FIL)"
    dts = np.diff(t[i0:i1 + 1]); ex = R["ext_sm"][i0 + 1:i1 + 1] > 0.1
    t_ext, t_no = dts[ex].sum(), dts[~ex].sum()
    if t_ext >= REF_EXT_MIN and t_no >= REF_EXT_MIN:
        (kb, kfil), rms, cv = _fit(ctx, i0, i1, lambda p: (p[0], p[0], p[1]), [1.0, 1.0], cov=True)
        sig = float(np.sqrt(cv[1, 1]) / max(abs(kfil), 0.05))
        return kb, kfil, sig, (f"referans {t_start - t[i0]:.0f} s (ekstruzyonlu {t_ext:.0f} s + ekstruzyonsuz {t_no:.0f} s): "
                               f"gunluk kayip carpani {kb:.3f}, filaman yuku yerinde olculdu = K_FIL x {kfil:.2f} "
                               f"(+-{sig*100:.0f}%), uyum {rms:.2f} C")
    (kb,), rms = _fit(ctx, i0, i1, lambda p: (p[0], p[0], 1.0), [1.0])
    why = "hep ekstruzyon var" if t_ext >= REF_EXT_MIN else "ekstruzyon yok"
    return kb, 1.0, K_FIL_PRIOR, (f"referans {t_start - t[i0]:.0f} s: gunluk kayip carpani {kb:.3f}, uyum {rms:.2f} C; "
                                  f"filaman katsayisi ayrica olculemedi ({why}) -> model K_FIL (+-{K_FIL_PRIOR*100:.0f}%)")


def replay_hypotheses(ctx, t_start, t_end, seg):
    """A/B/C hipotezlerini ikiz geri oynatmasiyla test eder. Oranlar enerji yontemiyle ayni anlamda:
    a = kaybolan fan sogutmasi, b = akmayan filaman, c = hava kayiplarindaki azalma (hepsi oransal)."""
    R = ctx["rp"]; t = R["t"]
    kb, kf0, sig, note = replay_baseline(ctx, t_start)
    i0 = int(np.searchsorted(t, t_start)); i1 = int(np.searchsorted(t, t_end, side="right")) - 1
    maps = {"A": lambda p: (kb, kb * (1 - p[0]), kf0),
            "B": lambda p: (kb, kb, kf0 * (1 - p[0])),
            "C": lambda p: (kb * (1 - p[0]), kb * (1 - p[0]), kf0)}
    terms = {"A": seg.L_fan.values, "B": seg.L_fil.values, "C": (seg.L_blok + seg.L_fan).values}
    base_rms = float(np.sqrt(np.mean(_sim(ctx, i0, i1, kb, kb, kf0) ** 2)))
    out = {}
    for h in ("A", "B", "C"):
        if np.mean(np.abs(terms[h])) < MIN_TERM:
            out[h] = dict(oran=np.nan, rms=np.nan, uygun=False, neden=f"terim ~0 ({np.mean(terms[h]):.2f} W): test edilemez")
            continue
        (p,), rms = _fit(ctx, i0, i1, maps[h], [0.0], base=(kb, kb, kf0))
        if h == "B":
            # filaman katsayisi belirsiz: gercek yuk 2 sigma buyukse gereken oran o kadar kuculur
            p_min = p / (1 + 2 * sig) if p > 0 else p * (1 + 2 * sig)
            ok = BOUNDS["B"][0] <= p_min <= B_MAX
            why = (f"filaman yukunun {p:.2f} kati kaybolmali; K_FIL belirsizligi (+-{sig*100:.0f}%, 2 sigma) "
                   f"ile bile {p_min:.2f} > {B_MAX}") if not ok else (
                   f"(K_FIL +-{sig*100:.0f}% ile en az {p_min:.2f})" if p > B_MAX else "")
            out[h] = dict(oran=float(p), rms=rms, uygun=ok, neden=why)
            continue
        lo, hi = BOUNDS[h]
        ok = lo <= p <= hi
        out[h] = dict(oran=float(p), rms=rms, uygun=ok,
                      neden="" if ok else f"oran {p:+.2f} fiziksel sinir [{lo}, {hi}] disinda")
    return out, note + f"; arizasiz varsayimla ikiz-gercek farki {base_rms:.1f} C", kf0


def rule_checks(sm, w, ctx):
    """D (termistor) ve E (isitici) kurallari, ham veri uzerinde."""
    notes = []
    t0, t1 = sm.t[w].min(), sm.t[w].max()
    # D: residual'in ~1 s'deki degisimi (okuma modelden fiziksel olmayan hizda ayrisiyor mu)
    t, sel = ctx["t"], ctx["hasT"] & ~ctx["synced"]
    tt, rr = t[sel], ctx["res"][sel]
    win = (tt >= t0) & (tt <= t1)
    tt, rr = tt[win], rr[win]
    if len(tt) > 2:
        j = np.clip(np.searchsorted(tt, tt + 1.0), 0, len(tt) - 1)   # ~1 s sonraki olcum
        span = tt[j] - tt
        ok = (span >= 0.5) & (span < 3.0)
        rate = np.where(ok, (rr[j] - rr) / np.where(ok, span, 1), 0)
        rmax = float(np.max(np.abs(rate))) if ok.any() else 0.0
        if rmax > RES_RATE_MAX:
            notes.append(("D", f"okuma modelden {rmax:.1f} C/s hizla ayrisiyor (normalde < 3 C/s): "
                               "termistor/konnektor sicramasi"))
    # E: isitici doygun ve sicaklik dusuyor (>= 10 s)
    seg = sm[w]
    # PID 3 s'dir kesintisiz %100 verirken sicaklik dusuyor: normal calismada olmaz (isinmada %100'de
    # sicaklik YUKSELIR). Toplam sure sayilir, kesik kesik (gevsek krimp) olanlar da yakalanir.
    sat_fall = (seg.pwm_min3 >= SAT_PWM) & (seg.dTdt_fast < -0.5) & (seg["T"] > 100)
    if sat_fall.sum() * GRID_DT >= 5:
        notes.append(("E", f"isitici %{SAT_PWM*100:.0f}+ iken sicaklik toplam {sat_fall.sum()*GRID_DT:.0f} s "
                           "dusuyor: kartusa guc gelmiyor (kopuk/gevsek kablo ya da krimp, bozuk kartus)"))
    # isitici kapaliyken sicaklik yukseliyor
    heat_off_rise = (seg.pwm_max15 <= 0.02) & (seg.dTdt_fast > 0.3) & (seg["T"] > 40)
    if heat_off_rise.sum() * GRID_DT >= 10:
        notes.append(("E", "isitici kapaliyken sicaklik yukseliyor: MOSFET kontrolsuz ya da termistor hatali"))
    return notes


def prepare(df, m):
    """Bir log icin gereken her sey bir kez hesaplanir: enerji sinyalleri + ikizin tekrari."""
    sm, t, hasT = signals(df, m)
    events = []
    _, res, _, _, synced = d.replay(df, m, events)
    return dict(df=df, m=m, sm=sm, t=t, hasT=hasT, res=res, synced=synced, events=events,
                rp=replay_inputs(df, m))


def classify_window(ctx, t_start, t_end):
    """[t_start, t_end] (log basindan saniye) penceresini siniflandir. Donus: sonuc sozlugu."""
    sm = ctx["sm"]
    rep = {"pencere": (t_start, t_end), "kategori": [], "notlar": [], "hipotezler": {}}

    # referans: olaydan onceki gecerli veri
    ref = sm.valid & (sm.t >= t_start - REF_LEN) & (sm.t < t_start - SMOOTH / 2)
    if ref.sum() * GRID_DT >= REF_MIN:
        g0 = float(sm.gap[ref].mean()); noise = float(sm.gap[ref].std())
        rep["notlar"].append(f"referans: olaydan onceki {ref.sum()*GRID_DT:.0f} s, eksik guc {g0:+.2f} W (gunluk oynama), "
                             f"gurultu {noise:.2f} W")
    else:
        g0, noise = 0.0, np.nan
        rep["notlar"].append("referans penceresi yok (olay log basinda): model esas alindi, g0 = 0")

    w_all = (sm.t >= t_start) & (sm.t <= t_end)
    w = w_all & sm.valid
    rep["kurallar"] = rule_checks(sm, w_all, ctx)
    for cat, txt in rep["kurallar"]:
        rep["kategori"].append(cat); rep["notlar"].append(f"[{cat}] {txt}")
    if rep["kurallar"]:
        rep["birincil"] = rep["kurallar"][0][0]     # dogrudan kural (D/E) enerji analizinden once gelir

    if w.sum() * GRID_DT < 15:
        rep["notlar"].append(f"pencerede sicaklik tutulan gecerli veri {w.sum()*GRID_DT:.0f} s: enerji analizi yapilamadi")
        if not rep["kategori"]:
            rep["kategori"] = ["?"]
        return rep

    seg = sm[w]
    dg = seg.gap.values - g0
    rep["sapma"] = float(dg.mean())
    rep["terimler"] = {k: float(seg[k].mean()) for k in ("L_blok", "L_fan", "L_fil")}
    rep["notlar"].append(f"pencerede {w.sum()*GRID_DT:.0f} s gecerli veri: ortalama sapma {dg.mean():+.2f} W  "
                         f"(model: blok {seg.L_blok.mean():.1f} W, fan {seg.L_fan.mean():.2f} W, "
                         f"filaman {seg.L_fil.mean():.2f} W)")
    terms = {"A": seg.L_fan.values, "B": seg.L_fil.values, "C": (seg.L_blok + seg.L_fan).values}
    hyp = fit_hypotheses(dg, terms)
    rep["hipotezler"] = hyp
    feas = {h: r for h, r in hyp.items() if r["uygun"]}
    # anlamlilik: ortalama sapma ya da bir hipotezin acikladigi kisim (kisa ekstruzyon olayi
    # pencerede seyrelse de filaman terimiyle birlikte oynadigi icin buradan yakalanir)
    explained = max([abs(r["oran"]) * np.sqrt(np.mean(terms[h] ** 2)) for h, r in feas.items()], default=0.0)
    if abs(dg.mean()) < MIN_SHIFT and explained < MIN_SHIFT:
        rep["notlar"].append(f"anlamli guc farki yok (sapma ve aciklanan kisim < {MIN_SHIFT} W): isi dengesi normal")
        rep["hipotezler"] = {}
        if not rep["kategori"]:
            rep["kategori"] = ["-"]
        return rep
    if fan_varies(seg) and seg.L_fil.mean() < MIN_TERM:
        # fan pencerede degisti: blok ve fan terimi ayri ayri cozulur, A/C ayrimi veriden yapilir
        x, y, rms = fit_two_term(dg, seg)
        rep["ayristirma"] = dict(x=x, y=y, rms=rms)
        rep["notlar"].append(f"fan degisti ({seg.fan.min()*100:.0f}-{seg.fan.max()*100:.0f}%): blok ve fan ayri cozuldu -> "
                             f"blok kaybi {x*100:+.0f}% (normal +-{X_BAND*100:.0f}%), fan sogutmasi {y*100:+.0f}% "
                             f"(normal {Y_BAND[0]*100:+.0f}..{Y_BAND[1]*100:+.0f}%), uyum rms {rms:.2f} W")
        if abs(x) > X_BAND:
            cat = "C"
            rep["notlar"].append(f"blok kaybi normal bandin disinda: {'corap/cereyan (daha fazla kayip)' if x > 0 else 'yalitim/ortam isindi (daha az kayip)'}"
                                 + ("; fan sogutmasi da degisti (corapsiz blok fan havasini dogrudan alir)"
                                    if not Y_BAND[0] <= y <= Y_BAND[1] else ""))
        elif not Y_BAND[0] <= y <= Y_BAND[1]:
            cat = "A"
            rep["notlar"].append("blok kaybi normal, sadece fan sogutmasi degisti: "
                                 + ("hava nozzle'a az ulasiyor (kanal tikali/fan yavas)" if y < 0
                                    else "fazladan sogutma (kanal bloga donmus?)"))
        else:
            cat = "-"
            rep["notlar"].append("iki oran da normal bantta")
        rep["kategori"].append(cat)
        if not rep.get("birincil"):
            rep["birincil"] = cat
    else:
        # tek terimli hipotezler ikiz geri oynatmasiyla (enerji penceresi yukarida sadece anlamlilik icin)
        hyp, note, _ = replay_hypotheses(ctx, t_start, t_end, seg)
        rep["hipotezler"] = hyp; rep["hipotez_birim"] = "C"
        rep["notlar"].append(note)
        feas = {h: r for h, r in hyp.items() if r["uygun"]}
        if feas:
            best = min(r["rms"] for r in feas.values())
            tie = max(best * RMS_TIE, best + REPLAY_TIE_C)
            chosen = sorted(h for h, r in feas.items() if r["rms"] <= tie)
            ranked = sorted(chosen, key=lambda h: feas[h]["rms"])
            # fan sabitken A ile C fiziksel olarak ayni sekli verir; aradaki rms farki modelin ic
            # yapisindan (kayip T1'den mi T2'den mi) gelir, veriden degil -> ikisi birlikte kalir
            ac_tied = "A" in feas and "C" in feas and ranked[0] in ("A", "C")
            if ac_tied:
                chosen = sorted(set(chosen) | {"A", "C"})
            rep["kategori"] += chosen
            # en iyi iki uyum pratikte ayniysa "en olasi" denmez
            if not rep.get("birincil") and not ac_tied and (
                    len(ranked) == 1 or feas[ranked[1]]["rms"] - feas[ranked[0]]["rms"] > 0.1):
                rep["birincil"] = ranked[0]
            if "A" in chosen and "C" in chosen:
                f = seg.fan.values
                rep["notlar"].append(f"A ile C ayrilamiyor: pencerede fan {f.min()*100:.0f}-{f.max()*100:.0f}% "
                                     "(fan degismeden 'fan sogutmasi kayboldu' ile 'blok yalitildi' ayni gorunur)")
        else:
            rep["kategori"].append("?")
            rep["notlar"].append("hicbir tek terim sapmayi fiziksel sinirlar icinde aciklamiyor (birlesik ariza?)")

    # baslangic hizi: sapmanin %10 -> %90'a cikis suresi
    s = pd.Series(sm.gap.values - g0, index=sm.t).loc[t_start - 60: t_end]
    tgt = dg.mean()
    if abs(tgt) >= MIN_SHIFT and len(s):
        frac = (s / tgt).values
        tt = s.index.values
        i90 = np.argmax(frac >= 0.9) if (frac >= 0.9).any() else None
        if i90 is not None:
            before = np.where(frac[:i90] <= 0.1)[0]
            if len(before):
                rise = tt[i90] - tt[before[-1]]
                rep["baslangic_s"] = float(rise)
                rep["notlar"].append(f"sapma {rise:.0f} s icinde olustu ({'ani' if rise < 60 else 'yavas'}; "
                                     f"{SMOOTH:.0f} s yumusatma yuzunden ani bir adim da ~{0.8*SMOOTH:.0f} s gorunur)")
    return rep


CAT_TEXT = {
    "A": "fan yolu (kanal tikali/dondu, fan yavasladi, topak havayi kesiyor)",
    "B": "filaman akmiyor (tikanma, filaman bitti, disli kaziyor)",
    "C": "yalitim/cevre (corap, cereyan, topak blogu sardi, ortam; termistorun yuvasindan kaymasi da "
         "ayni imzayi verir)",
    "D": "termistor",
    "E": "isitici",
    "-": "anlamli sapma yok",
    "?": "belirsiz",
}


def category_line(rep):
    cats = sorted(set(rep["kategori"]))
    first = rep.get("birincil")
    if first and len(cats) > 1:
        return f"en olasi {first}; {', '.join(c for c in cats if c != first)} de disarida birakilamiyor"
    return " veya ".join(cats)


def format_report(rep, title):
    a, b = rep["pencere"]
    lines = [f"\n=== {title}  [{a:.0f} - {b:.0f} s]"]
    lines += [f"  {n}" for n in rep["notlar"]]
    if "ayristirma" in rep and rep.get("hipotezler"):
        lines.append("  (tek terimli uyumlar, sadece bilgi icin - karar yukaridaki ayristirmadan)")
    for h, r in rep.get("hipotezler", {}).items():
        if np.isnan(r["oran"]):
            lines.append(f"    {h}: {r['neden']}")
        else:
            flag = "uyumlu" if r["uygun"] else "ELENDI"
            unit = rep.get("hipotez_birim", "W")
            lines.append(f"    {h}: oran {r['oran']:+.2f}  uyum rms {r['rms']:.2f} {unit}  -> {flag} {r['neden']}")
    lines.append(f"  KATEGORI: {category_line(rep)}")
    lines += [f"    {c} = {CAT_TEXT.get(c, c)}" for c in sorted(set(rep["kategori"]))]
    return "\n".join(lines)


def print_report(rep, title):
    print(format_report(rep, title))


# ---------------------------------------------------------------------------
# Olaylar
# ---------------------------------------------------------------------------
def fault_events(ctx, merge=60.0):
    """Ikizin (canli scriptle ayni kod) onayladigi arizalar, yakin olanlar birlestirilir.
    Donus: [(baslangic_s, bitis_s, kodlar)] log basina gore."""
    df = ctx["df"]
    t0 = df.t.iloc[0]; t_last = df.t.iloc[-1] - t0
    ev = sorted((a - t0, (b - t0) if b is not None else t_last, c) for c, a, b in ctx["events"])
    out = []
    for a, b, c in ev:
        if out and a <= out[-1][1] + merge:
            out[-1] = (out[-1][0], max(out[-1][1], b), out[-1][2] | {c})
        else:
            out.append((a, b, {c}))
    return out


def find_onset(sm, t_conf, g0_guess_end):
    """Onaylanma anindan geriye: sapmanin yukselmeye basladigi an (onaydan once en fazla 120 s)."""
    ref = sm.valid & (sm.t >= g0_guess_end - REF_LEN) & (sm.t < g0_guess_end)
    g0 = sm.gap[ref].mean() if ref.sum() * GRID_DT >= REF_MIN else 0.0
    after = sm.valid & (sm.t >= t_conf) & (sm.t < t_conf + 60)
    if not after.any():
        return t_conf
    level = sm.gap[after].mean() - g0
    s = sm[(sm.t >= t_conf - 120) & (sm.t <= t_conf)]
    below = s.t[(s.gap - g0) / level < 0.3] if abs(level) > MIN_SHIFT else pd.Series(dtype=float)
    return float(below.max()) if len(below) else t_conf


def classify_log(path, m=None, window=None):
    m = m or d.load_model()
    ctx = prepare(d.read_log(path), m)
    reps = []
    if window:
        rep = classify_window(ctx, window[0], window[1])
        print_report(rep, f"{os.path.basename(path)}  (elle verilen pencere)")
        return [rep]
    evs = fault_events(ctx)
    if not evs:
        print(f"{os.path.basename(path)}: ikiz onaylanmis ariza bulmadi")
    for a, b, codes in evs:
        onset = find_onset(ctx["sm"], a, a - 60)
        end = min(b, onset + ANALYSIS_LEN)
        rep = classify_window(ctx, onset, end)
        rep["kodlar"] = codes
        print_report(rep, f"{os.path.basename(path)}  ikiz: {', '.join(sorted(codes))} (onay {a:.0f} s, "
                          f"sapma baslangici ~{onset:.0f} s)")
        reps.append(rep)
    return reps


def classify_live(rows, m, t_confirm):
    """Canli scriptten: rows = bellekteki son satirlar (CSV ile ayni alanlar), m = calisan model
    modulu, t_confirm = arizanin onaylandigi an (model saati). Donus: (kategori satiri, tam rapor)."""
    df = d.prepare_log(pd.DataFrame(rows))
    ctx = prepare(df, m)
    t0 = df.t.iloc[0]
    a = t_confirm - t0
    onset = find_onset(ctx["sm"], a, a - 60)
    end = min(ctx["t"][-1], onset + ANALYSIS_LEN)
    rep = classify_window(ctx, onset, end)
    title = (f"canli: onaydan {ctx['t'][-1] - a:.0f} s sonra, sapma baslangici onaydan "
             f"{a - onset:.0f} s once (pencere saniyeleri arabellek basina gore)")
    expl = "; ".join(f"{c} = {CAT_TEXT.get(c, c)}" for c in sorted(set(rep["kategori"])))
    return f"{category_line(rep)}\n      {expl}", format_report(rep, title)


def evaluate_labels():
    """etiketler.csv: dosya, baslangic_s, bitis_s, gozlenen, beklenen (ornek 'A|C'), set.
    set: 'ayar' = esikler bu orneklere bakilarak ayarlandi (skor iyimser), 'sinav' = 2026-10-09'dan
    sonra eklenen, hicbir esik ayarinda kullanilmayan ornekler. Karar sinav skoruna gore verilir."""
    lab = pd.read_csv(LABEL_FILE)
    m = d.load_model()
    rows = []
    for r in lab.itertuples():
        path = os.path.join(os.path.dirname(LABEL_FILE), r.dosya)
        end = r.bitis_s if pd.notna(r.bitis_s) else 1e9
        rep = classify_log(path, m, (r.baslangic_s, end))[0]
        got = set(rep["kategori"]); exp = set(str(r.beklenen).split("|"))
        rows.append((r.dosya[:40], r.gozlenen, "|".join(sorted(exp)), "|".join(sorted(got)),
                     "DOGRU" if got == exp else ("KISMEN" if got & exp else "YANLIS"),
                     getattr(r, "set", "ayar")))
    print("\nETIKETLI ORNEKLER")
    for row in rows:
        print(f"  [{row[5]:5s}] {row[0]:42s} {row[1]:32s} beklenen {row[2]:6s} bulunan {row[3]:6s} {row[4]}")
    for s in ("ayar", "sinav"):
        res = [row[4] for row in rows if row[5] == s]
        if res:
            print(f"  {s}: {res.count('DOGRU')}/{len(res)} dogru, {res.count('KISMEN')} kismen, {res.count('YANLIS')} yanlis"
                  + ("  (esikler bu orneklere gore ayarlandi: iyimser)" if s == "ayar" else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Physical fault category for the twin's alarms (diagnostic only).")
    ap.add_argument("log", nargs="?", help="session log: classify the alarms the twin found")
    ap.add_argument("--window", nargs=2, type=float, metavar=("START", "END"),
                    help="classify this window instead (seconds from log start)")
    ap.add_argument("--labels", action="store_true",
                    help="score against benchmark/ariza_ornekleri/etiketler.csv")
    a = ap.parse_args()
    if a.labels:
        evaluate_labels()
    elif a.log:
        classify_log(a.log, window=a.window)
    else:
        ap.print_help()
