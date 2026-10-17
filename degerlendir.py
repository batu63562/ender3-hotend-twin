"""
Benchmark degerlendirmesi.

realtime_websocket.py'deki modeli benchmark/ klasorundeki loglar uzerinde CANLI SCRIPT
GIBI tekrar oynatir (ayni model_step / alarm mantigi / k_loss ogrenmesi, ayni dongu
mantigi) ve her log icin faz bazli skor tablosu basar.

Fazlar sadece GERCEK veriden (pwm, fan, T1_real) belirlenir, modelden bagimsizdir:
    isinma      isitici %100 (son 3 s ortalama pwm >= 0.95)
    varis       isinma bittikten sonraki ilk 30 s (asma / oturma)
    tutma245    245C civari tutma (T>235, son 30 s'de T araligi < 3C), fan kapali
    tutma245fan ayni, fan acik
    tutma_diger diger sicakliklarda tutma (150, 180, 220 ...)
    soguma      isitici 15 s'dir kapali, T >= 35C
    bosta       isitici 15 s'dir kapali, T < 35C
    gecis       geri kalan her sey (hedef degisimi vb.)

Residual isareti canli script ile ayni: residual = T1_real - T1_model
(+ = gercek modelden sicak, - = model gercekten sicak).

Kullanim:
    python degerlendir.py                          # skor tablosu
    python degerlendir.py --kaydet baseline        # benchmark/skorlar/baseline.csv
    python degerlendir.py --karsilastir baseline   # kayitli skora gore fark
    python degerlendir.py --set sinav              # benchmark/setler.csv'deki sinav loglari

Setler (benchmark/setler.csv): 'ayar' = parametre/esik ayarlarken bakilan loglar, 'sinav' =
hicbir ayarda kullanilmayan, sadece puan tablosu icin dondurulmus loglar (2026-10-09'dan
itibaren). Kural: bir model degisikligi sinav loglarina bakilarak ayarlanmaz; yeni baskilar
sinav setine eklenir. Sinav dosyasi degismisse (parmak izi tutmuyorsa) uyari verilir.
Baska bir scriptten (parametre denemesi):
    from degerlendir import evaluate, print_scorecard
    print_scorecard(evaluate(overrides={"HA1": 0.04}))
"""
import argparse
import glob
import importlib
import os

import numpy as np
import pandas as pd

import realtime_websocket as rw

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH_DIR = os.path.join(HERE, "benchmark")
SCORE_DIR = os.path.join(BENCH_DIR, "skorlar")
SET_FILE = os.path.join(BENCH_DIR, "setler.csv")
PHASES = ["bosta", "isinma", "varis", "tutma245", "tutma245fan", "tutma_diger", "soguma", "gecis"]


# ---------------------------------------------------------------------------
# Log okuma ve faz etiketleme
# ---------------------------------------------------------------------------
def read_log(path):
    return prepare_log(pd.read_csv(path))


def prepare_log(df):
    """CSV'den ya da canli scriptin bellekteki satirlarindan gelen tabloyu hazirlar."""
    if "eventtime" in df:
        df["eventtime"] = pd.to_numeric(df.eventtime, errors="coerce")
    klipper = "eventtime" in df and df.eventtime.notna().all()
    df["t"] = df.eventtime if klipper else df.time
    df["clock"] = "klipper" if klipper else "pc"
    df["extrude_velocity"] = df.extrude_velocity.fillna(0.0)
    df["fan_state"] = df.fan_state.fillna(0.0)
    df["pwm"] = df.pwm.fillna(0.0)
    # yeni loglar her mesaji kaydeder; T_yeni=1 olan satirlarda yeni sicaklik var.
    # eski loglarda her satir bir sicaklik mesajiydi.
    df["has_T"] = (df.T_yeni == 1) if "T_yeni" in df else True
    return df.reset_index(drop=True)


def mono_time(t):
    """Klipper yeniden baslarsa eventtime geri gider (10.10 iptaller logu); kayan pencereler icin tekduze
    zaman: geri adimlar 0 sayilir. Tekduze loglarda t'nin kendisi (sonuc birebir ayni)."""
    t = np.asarray(t, dtype=float)
    d = np.diff(t, prepend=t[0])
    if (d >= 0).all():
        return t
    return t[0] + np.cumsum(np.maximum(d, 0.0))


def label_phases(df):
    """Her ornege faz etiketi (sadece gercek veriden)."""
    tm = mono_time(df.t.values)
    s = df.set_index(pd.to_datetime(tm, unit="s"))
    pwm_mean3 = s.pwm.rolling("3s").mean().values
    pwm_max15 = s.pwm.rolling("15s").max().values
    T_range30 = (s.T1_real.rolling("30s").max() - s.T1_real.rolling("30s").min()).values
    # en son GERCEK isinma ani (3 s boyunca %100). Tek ornekli pwm=1 sivrilikleri tutma
    # sirasinda PID'de normal; onlari isinma saymamak icin tekil pwm>=0.99 kullanilmaz.
    heat_t = pd.Series(tm).where(pd.Series(pwm_mean3 >= 0.95)).ffill().values
    since_sat = tm - np.nan_to_num(heat_t, nan=-1e12)

    T, fan = df.T1_real.values, df.fan_state.values
    ph = np.full(len(df), "gecis", dtype=object)
    hold = (T_range30 < 3.0) & (T > 100)
    ph[hold & (T <= 235)] = "tutma_diger"
    ph[hold & (T > 235) & (fan == 0)] = "tutma245"
    ph[hold & (T > 235) & (fan > 0)] = "tutma245fan"
    ph[since_sat < 30.0] = "varis"
    off = pwm_max15 <= 0.01
    ph[off & (T >= 35)] = "soguma"
    ph[off & (T < 35)] = "bosta"
    ph[pwm_mean3 >= 0.95] = "isinma"
    return ph


# ---------------------------------------------------------------------------
# Canli calismanin tekrari: canli script ile AYNI Twin sinifi kullanilir
# ---------------------------------------------------------------------------
def replay(df, m, events=None, k0=1.0, kfil0=1.0, state=None, k_ref0=None):
    """m: realtime_websocket modulu (parametreleri degistirilmis olabilir).
    events: liste verilirse onaylanmis ariza olaylari (kod, baslangic, bitis|None) eklenir.
    state: sozluk verilirse sondaki filaman carpani ve kayip alarmi referansi yazilir (state["kfil"],
    state["k_ref"], satir satir state["k_refs"]); zincirleme icin. k_ref0: devralinan referans (None: k0).
    Satir 0 canli scriptteki ilk mesaj: model burada taze baslar (kayitli durum yok).
    Loglanan pwm/fan/ekstruzyon, o mesajdan SONRA gecerli degerlerdir; Twin bunlari bir sonraki
    araliga uygular - canli calismayla ayni sira."""
    t, T, has_T = df.t.values, df.T1_real.values, df.has_T.values
    pwm, fan, ext = df.pwm.values, df.fan_state.values, df.extrude_velocity.values
    n = len(df)
    T1m = np.empty(n); res = np.zeros(n); anom = np.zeros(n, bool); ks = np.ones(n); synced = np.zeros(n, bool)
    refs = np.ones(n)

    # k0: canli scriptin onceki oturumdan devraldigi kayip carpani (varsayilan: taze baslangic)
    twin = m.Twin(*m.initial_state(T[0], pwm[0], fan[0]), pwm[0], fan[0], ext[0], k=k0, kfil=kfil0, k_ref=k_ref0)
    # hedef sicaklik (2026-09-28'den itibaren loglaniyor) sadece erken uyariyi etkiler
    tg = pd.to_numeric(df.target, errors="coerce").values if "target" in df else np.full(n, np.nan)
    # tabla isitici gucu (2026-09-28'den itibaren loglaniyor): nozzle kartusunun besleme voltaji
    bp = pd.to_numeric(df.bed_pwm, errors="coerce").values if "bed_pwm" in df else np.full(n, np.nan)
    # ekstruder konumu (2026-09-29'dan itibaren loglaniyor): canli scriptteki gibi son bilinen deger;
    # akis bundan hesaplanir, yoksa anlik hiz
    pe = pd.to_numeric(df.pos_e, errors="coerce").ffill().values if "pos_e" in df else np.full(n, np.nan)
    # malzeme (2026-10-16'dan itibaren loglaniyor): canli ikiz baski basinda G-code'dan malzemeyi okuyup k_fil'i o
    # malzemenin kayitli degerine gecirir (Twin.set_material); kayitli deger logdaki o satirin k_fil'i
    mz = df["malzeme"].fillna("").astype(str).values if "malzeme" in df else None
    kfl = pd.to_numeric(df["k_fil"], errors="coerce").values if "k_fil" in df else np.full(n, np.nan)
    if mz is not None:
        twin.material = mz[0] or None
    for i in range(n):
        if mz is not None and mz[i] and mz[i] != twin.material:
            twin.set_material(mz[i], kfl[i] if np.isfinite(kfl[i]) else None)
        r = twin.on_message(t[i], T[i] if has_T[i] else None, pwm[i], fan[i], ext[i],
                            None if np.isnan(tg[i]) else tg[i], None if np.isnan(bp[i]) else bp[i],
                            None if np.isnan(pe[i]) else pe[i])
        T1m[i] = twin.T1_model; ks[i] = twin.learner.k; refs[i] = twin.k_ref
        synced[i] = r["senkron"]; anom[i] = r["anomali"]
        res[i] = r["residual"] if r["residual"] is not None else 0.0
    if events is not None:
        events.extend(twin.faults.events + [(c, t0, None) for c, t0 in twin.faults.active.items()])
    if state is not None:
        state["kfil"], state["k_ref"], state["k_refs"] = twin.learner.kfil, twin.k_ref, refs
    return T1m, res, anom, ks, synced


def load_model(overrides=None):
    m = importlib.reload(rw)
    for k, v in (overrides or {}).items():
        if not hasattr(m, k):
            raise KeyError(f"realtime_websocket.py'de '{k}' yok")
        setattr(m, k, v)
    m.T_AMB_K = m.T_AMB + 273.15
    return m


def short_name(path):
    p = os.path.basename(path).replace(".csv", "").split("_")
    if len(p[0]) == 10 and len(p) >= 3:          # 2026-09-25_1443_klipper_...
        return f"{p[0][5:]} {p[1]} {p[2]}"
    return " ".join(p)[:22]                       # 0810_sonunda_basarili, deney7_...


def set_paths(name):
    """benchmark/setler.csv'den bir setin log yollari ('hepsi' = tumu). Sinav dosyalarinin
    parmak izi kontrol edilir: dondurulmus bir log sonradan degistiyse uyari basilir."""
    import hashlib
    s = pd.read_csv(SET_FILE)
    if name != "hepsi":
        s = s[s.set == name]
    paths = []
    for r in s.itertuples():
        p = os.path.join(HERE, r.dosya)
        if not os.path.exists(p):
            print(f"!! setteki log yok: {r.dosya}")
            continue
        if r.set == "sinav":
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for b in iter(lambda: f.read(1 << 20), b""):
                    h.update(b)
            if h.hexdigest()[:16] != r.sha256_16:
                print(f"!! sinav logu degismis (parmak izi tutmuyor): {r.dosya}")
        paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# Skorlama
# ---------------------------------------------------------------------------
def evaluate(overrides=None, paths=None, chain=False):
    """Uzun formatta skor DataFrame'i: log, clock, faz, n, sure_s, rmse, bias, max, anomali.
    chain=True: loglar verilen sirayla ardisik oturumlar sayilir, her biri oncekinin son k'sini
    devralir (canli scriptteki hatirlama); False: her log k=1 ile taze baslar."""
    m = load_model(overrides)
    paths = paths or sorted(p for p in glob.glob(os.path.join(BENCH_DIR, "*.csv")) if p != SET_FILE)
    rows = []
    k_carry, kfil_carry, ref_carry = 1.0, 1.0, 1.0
    for p in paths:
        df = read_log(p)
        events, st = [], {}
        k_start, kfil_start, ref_start = (k_carry, kfil_carry, ref_carry) if chain else (1.0, 1.0, 1.0)
        _, res, anom, ks, synced = replay(df, m, events, k0=k_start, kfil0=kfil_start, state=st, k_ref0=ref_start)
        k_carry, kfil_carry, ref_carry = float(ks[-1]), st["kfil"], st["k_ref"]
        kanom = abs(ks - st["k_refs"]) > m.K_ALARM_BAND   # ogrenilen kayip yavas referanstan bandin disinda
        fault_start = np.zeros(len(df), bool)          # onaylanmis arizanin basladigi satirlar
        for _, t0, _ in events:
            hit = np.flatnonzero(df.t.values == t0)    # olay ani bir mesaj zamani (saat geri gitse de dogru satir)
            fault_start[hit[0] if len(hit) else min(np.searchsorted(df.t.values, t0), len(df) - 1)] = True
        ph = label_phases(df)
        dt = np.r_[0.0, np.diff(mono_time(df.t.values))]
        valid = ~synced & df.has_T.values        # hata sadece yeni sicaklik gelen satirlarda
        valid[0] = False
        for phase in PHASES + ["TUMU"]:
            in_phase = (ph == phase) if phase != "TUMU" else np.ones(len(df), bool)
            mask = valid & in_phase
            if not mask.any():
                continue
            r = res[mask]
            rows.append(dict(log=short_name(p), clock=df.clock[0], faz=phase, n=int(mask.sum()),
                             sure_s=float(dt[in_phase & ~synced].sum()), rmse=float(np.sqrt(np.mean(r ** 2))),
                             bias=float(r.mean()), max=float(np.abs(r).max()),
                             anomali=int(anom[mask].sum()), k_anomali=int(kanom[mask].sum()),
                             ariza=int(fault_start[in_phase].sum()), k_son=float(ks[-1]), kfil_son=kfil_carry))
    return pd.DataFrame(rows)


def pooled(sc, clock=None):
    """Loglar birlesik, faz bazli (RMSE'ler ornek sayisiyla agirlikli birlesir)."""
    s = sc if clock is None else sc[sc.clock == clock]
    out = []
    for phase in PHASES + ["TUMU"]:
        g = s[s.faz == phase]
        if len(g):
            out.append(dict(faz=phase, n=int(g.n.sum()), sure_s=g.sure_s.sum(),
                            rmse=float(np.sqrt((g.rmse ** 2 * g.n).sum() / g.n.sum())),
                            bias=float((g.bias * g.n).sum() / g.n.sum()), max=g["max"].max(),
                            anomali=int(g.anomali.sum()),
                            k_anomali=int(g.k_anomali.sum()) if "k_anomali" in g else 0,
                            ariza=int(g.ariza.sum()) if "ariza" in g else 0))
    return pd.DataFrame(out)


def print_scorecard(sc):
    cols = [p for p in PHASES + ["TUMU"] if p in set(sc.faz)]
    print("RMSE (bias) [C] - residual = gercek - model;  '-' = bu fazda veri yok\n")
    head = f"{'log':22s}{'saat':8s}" + "".join(f"{c:>15s}" for c in cols)
    print(head); print("-" * len(head))
    for log, g in sc.groupby("log", sort=False):
        cells = {r.faz: f"{r.rmse:5.2f} ({r.bias:+5.1f})" for r in g.itertuples()}
        print(f"{log:22s}{g.clock.iloc[0]:8s}" + "".join(f"{cells.get(c, '-'):>15s}" for c in cols))

    for title, clock in (("KLIPPER ZAMANLI LOGLAR (karar icin esas)", "klipper"), ("TUM LOGLAR", None)):
        p = pooled(sc, clock)
        if not len(p):
            continue
        print(f"\n{title}")
        print(f"  {'faz':12s}{'sure':>8s}{'RMSE':>8s}{'bias':>8s}{'max':>8s}{'anomali':>9s}{'k_anom':>8s}{'ariza':>7s}")
        for r in p.itertuples():
            print(f"  {r.faz:12s}{r.sure_s:7.0f}s{r.rmse:8.2f}{r.bias:+8.2f}{r.max:8.2f}{r.anomali:9d}"
                  f"{r.k_anomali:8d}{r.ariza:7d}")


def print_comparison(sc, ref, ref_name):
    print(f"\nFARK: simdiki - '{ref_name}'  (RMSE, negatif = iyilesme)\n")
    cols = [p for p in PHASES + ["TUMU"] if p in set(sc.faz)]
    merged = sc.merge(ref, on=["log", "faz"], suffixes=("", "_ref"), how="left")
    head = f"{'log':22s}" + "".join(f"{c:>13s}" for c in cols)
    print(head); print("-" * len(head))
    for log, g in merged.groupby("log", sort=False):
        cells = {r.faz: (f"{r.rmse - r.rmse_ref:+6.2f}" if pd.notna(r.rmse_ref) else "yeni") for r in g.itertuples()}
        print(f"{log:22s}" + "".join(f"{cells.get(c, '-'):>13s}" for c in cols))
    for clock, title in (("klipper", "klipper"), (None, "tum")):
        a, b = pooled(sc, clock), pooled(ref, clock)
        if len(a) and len(b):
            j = a.merge(b, on="faz", suffixes=("", "_ref"))
            print(f"  birlesik ({title}): " + "  ".join(f"{r.faz} {r.rmse - r.rmse_ref:+.2f}" for r in j.itertuples()))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--kaydet", metavar="AD", help="skorlari benchmark/skorlar/AD.csv olarak sakla")
    ap.add_argument("--karsilastir", metavar="AD", help="benchmark/skorlar/AD.csv ile karsilastir")
    ap.add_argument("--set", choices=["ayar", "sinav", "hepsi"],
                    help="benchmark/setler.csv'deki log seti (verilmezse sadece benchmark/*.csv)")
    ap.add_argument("--zincir", action="store_true",
                    help="loglari ardisik oturum say: her biri oncekinin ogrendigi k ile baslar")
    a = ap.parse_args()

    sc = evaluate(paths=set_paths(a.set) if a.set else None, chain=a.zincir)
    print_scorecard(sc)
    if a.karsilastir:
        print_comparison(sc, pd.read_csv(os.path.join(SCORE_DIR, a.karsilastir + ".csv")), a.karsilastir)
    if a.kaydet:
        os.makedirs(SCORE_DIR, exist_ok=True)
        sc.to_csv(os.path.join(SCORE_DIR, a.kaydet + ".csv"), index=False)
        print(f"\nKaydedildi: benchmark/skorlar/{a.kaydet}.csv")
