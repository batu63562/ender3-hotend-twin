"""
Oturumlar arasi trend kaydi (yavas biriken arizalar icin).

Tek bir oturumda gorunmeyen, haftalar icinde biriken degisimler: nozzle dislerinden sizinti
birikmesi (kayip azalir), kartus yaslanmasi (direnc artar, guc duser -> kayip artmis gibi
gorunur), corap yipranmasi (kayip artar), parca fani tozlanmasi (fan sogutmasi azalir).

Her oturumun sonunda canli script oturum CSV'sinden bir ozet satiri cikarir ve GECMIS_FILE'a
ekler. Olcutler SADECE 245C civarinda, ekstruzyonsuz, sicakligin sabit tutuldugu donemlerden
enerji dengesiyle hesaplanir (ariza_siniflandir.signals ile ayni sinyal). Hepsi 2026-09-25'te
kalibre edilen modele goredir (0 = kalibrasyon gunuyle ayni):
    P_fan0_W            fan kapaliyken 245C'yi tutmak icin ortalama isitici gucu
    blok_kaybi_pct      fan kapali tutmada blok kaybi modele gore % fark (+ = daha fazla kayip)
    fan_sogutma_pct     fanli tutmada fan sogutmasi modele gore % fark (normalde ~ -8%)
    baski_kaybi_pct     baskida (ekstruzyon + fan) toplam hava kaybi % fark; filaman yuku K_FIL ile
    P150_W, kayip150_pct  baski basindaki 150C on isitma beklemesinde (START_GCODE: tabla isinirken nozzle 150C,
                        X0 Y0, ekstruzyonsuz) isiticiya giden gercek guc ve modele (k=1) gore % fark. Her baskida
                        kendiliginden olusan, baskiya dokunmayan bakim gostergesi: heatsink tozu, heat break temasi,
                        fan sorunlari gibi yavas bozulmalar burada gorunur (2026-10-13: bakim oncesi 7.4-8.4 W,
                        sonrasi 9.7 W). k ogrenmek icin uygun degil (tabla nozzle'in altinda isiniyor), trend icin uygun.
Yeterli veri yoksa (< MIN_HOLD s) o olcut bos kalir. Arizali oturumlar DISLANMAZ: yavas bir ariza
esigi gecip alarm verdiginde de trendde gorunmeli. Ariza kodlari ayri sutunda.

Oda sicakligi olculmuyor (T_AMB = 25C sabit): oda 23C ise 245C'deki kayip ~%1 fazla gorunur.
Gunluk dogal oynama blok kaybinda ~+-%5.

Kullanim:
    python gecmis.py                      # trend tablosu ve uyarilar
    python gecmis.py --doldur             # eski loglardan gecmisi (yeniden) olustur
    python gecmis.py --ekle LOG.csv       # tek bir logu ekle
"""
import argparse
import glob
import os
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
GECMIS_FILE = os.path.join(HERE, "twin_gecmis.csv")
MIN_HOLD = 60.0        # s - bir olcut icin gereken en az gecerli tutma suresi
T_HOLD = (240.0, 250.0)
DRIFT_WARN = 5.0       # % - son oturum onceki oturumlarin medyanindan bu kadar farkliysa uyar
SLOPE_WARN = 2.0       # %/hafta - blok kaybi egimi bunu asarsa uyar (en az 5 oturum, 3 gun)
COLUMNS = ["oturum", "dosya", "sure_dk", "model", "tutma245_s", "P_fan0_W", "blok_kaybi_pct",
           "fan_sogutma_pct", "baski_kaybi_pct", "P150_W", "kayip150_pct", "k_son", "arizalar", "not"]
T150 = 150.0
HOLD150_MIN = 90.0     # s - 150C beklemesi en az bu kadar surmeli; son 60 s kullanilir (varis gecisi disarida)


def hold150(raw, m):
    """150C on isitma beklemesinin son 60 s'sindeki gercek isitici gucu (W) ve modele (k=1) gore % fark.
    Bekleme yoksa (None, None)."""
    if "target" not in raw or "time" not in raw:
        return None, None
    tg = pd.to_numeric(raw.target, errors="coerce").ffill()
    T = pd.to_numeric(raw.T1_real, errors="coerce").ffill()
    t = pd.to_numeric(raw.time, errors="coerce")
    pe = pd.to_numeric(raw.pos_e, errors="coerce").ffill() if "pos_e" in raw else pd.Series(0.0, index=raw.index)
    on = ((tg - T150).abs() < 0.5) & ((T - T150).abs() < 1.5)
    seg = (on != on.shift()).cumsum()[on]
    if seg.empty:
        return None, None
    best = max((g.index for _, g in seg.groupby(seg)), key=lambda ix: t[ix[-1]] - t[ix[0]])
    if t[best[-1]] - t[best[0]] < HOLD150_MIN or pe[best].max() - pe[best].min() > 0.05:
        return None, None
    last = best[t[best] >= t[best[-1]] - 60.0]
    pwm = pd.to_numeric(raw.pwm, errors="coerce")[last]
    bed = pd.to_numeric(raw.bed_pwm, errors="coerce").ffill().fillna(0.0)[last] if "bed_pwm" in raw else 0.0
    dt = t[last].diff().fillna(0.0)
    P = float((pwm * m.P_MAX * (1 - m.BED_SAG * bed) * dt).sum() / dt.sum())
    model = m.natural_loss(T150) + m.HA2 * m.steady_T2_rise(T150, 0.0)
    return round(P, 2), round(100 * (P / model - 1), 1)


def summarize(path, m, note=""):
    """Bir oturum logundan ozet satiri (dict)."""
    import evaluate as d
    import fault_classifier as a
    raw = pd.read_csv(path)
    if len(raw) < 20 or raw.T1_real.isna().all():
        return None                                  # bos/cok kisa oturum: ozet yok
    tt = pd.to_numeric(raw.time, errors="coerce")
    row = dict(oturum=datetime.fromtimestamp(float(tt.iloc[0])).strftime("%Y-%m-%d %H:%M"),
               dosya=os.path.basename(path), sure_dk=round((tt.max() - tt.iloc[0]) / 60, 1),
               model=m.MODEL_VERSION, not_=note)
    row["P150_W"], row["kayip150_pct"] = hold150(raw, m)
    try:
        df = d.prepare_log(raw.copy())
        sm, t, _ = a.signals(df, m)
    except Exception as e:  # noqa: BLE001 - 245C olcutleri hesaplanamazsa ozetin geri kalani yine yazilsin
        # (2026-10-13: 15 saatlik, gece kapali kalan oturumda signals bos dizi ile coktu, ozet hic yazilmamisti)
        row["not_"] = (note + "; " if note else "") + f"245C olcutleri yok ({type(e).__name__})"
        return _finish(row, raw)
    G = a.GRID_DT
    hold = sm.valid & sm["T"].between(*T_HOLD)
    noext = hold & (sm.L_fil < 0.05)
    fan0 = noext & (sm.fan < 0.01)
    fanon = noext & (sm.fan >= 0.01)
    printing = hold & (sm.L_fil >= 0.05) & (sm.fan >= 0.01)
    row["tutma245_s"] = round(hold.sum() * G)
    x0 = None
    if fan0.sum() * G >= MIN_HOLD:
        x0 = -sm.gap[fan0].mean() / sm.L_blok[fan0].mean()
        row["P_fan0_W"] = round(sm.P[fan0].mean(), 2)
        row["blok_kaybi_pct"] = round(100 * x0, 1)
    if fanon.sum() * G >= MIN_HOLD:
        if x0 is not None and a.fan_varies(sm[noext]):
            x, y, _ = a.fit_two_term(sm.gap[noext].values, sm[noext])   # blok ve fan birlikte
        else:
            x = x0 if x0 is not None else 0.0      # fan kapali veri yoksa blok modeldeki gibi sayilir
            y = -(sm.gap[fanon].mean() + x * sm.L_blok[fanon].mean()) / sm.L_fan[fanon].mean()
        row["fan_sogutma_pct"] = round(100 * y, 1)
    if printing.sum() * G >= 2 * MIN_HOLD:
        c = -sm.gap[printing].mean() / (sm.L_blok[printing] + sm.L_fan[printing]).mean()
        row["baski_kaybi_pct"] = round(100 * c, 1)
    return _finish(row, raw)


def _finish(row, raw):
    """Ortak sutunlar: son k, ariza kodlari, not."""
    if "k_loss" in raw:
        row["k_son"] = round(float(raw.k_loss.iloc[-1]), 3)
    if "ariza" in raw:
        codes = sorted({c for v in raw.ariza.dropna().astype(str) for c in v.split(";") if c})
        row["arizalar"] = ";".join(codes)
    row["not"] = row.pop("not_")
    return row


def append(row):
    new = not os.path.exists(GECMIS_FILE)
    if not new:
        with open(GECMIS_FILE, encoding="utf-8") as f:
            header = f.readline().strip().split(",")
        if header != COLUMNS:          # eski sutun duzeni (yeni sutun eklendi): once dosyayi yeni duzene cevir
            pd.read_csv(GECMIS_FILE).reindex(columns=COLUMNS).to_csv(GECMIS_FILE, index=False)
    pd.DataFrame([row]).reindex(columns=COLUMNS).to_csv(GECMIS_FILE, mode="a", header=new, index=False)


def backfill(m):
    """Benchmark + ariza ornekleri loglarindan gecmisi bastan olusturur (zaman sirasiyla)."""
    paths = glob.glob(os.path.join(HERE, "benchmark", "*.csv")) + \
            glob.glob(os.path.join(HERE, "benchmark", "ariza_ornekleri", "*.csv"))
    paths = [p for p in paths if not p.endswith(("etiketler.csv", "setler.csv"))]
    rows = []
    for p in paths:
        base = os.path.basename(p)
        note = ("ariza ornegi: " if "ariza_ornekleri" in p else "benchmark: ") + "_".join(base[:-4].split("_")[3:])
        row = summarize(p, m, note)
        if row:
            rows.append(row)
    out = pd.DataFrame(rows).reindex(columns=COLUMNS).sort_values("oturum")
    out.to_csv(GECMIS_FILE, index=False)
    print(f"{len(out)} oturum yazildi: {GECMIS_FILE}")


def report():
    if not os.path.exists(GECMIS_FILE):
        print("gecmis yok - once 'python session_history.py --backfill' ya da canli scripti calistir")
        return
    g = pd.read_csv(GECMIS_FILE)
    show = ["oturum", "sure_dk", "tutma245_s", "P_fan0_W", "blok_kaybi_pct", "fan_sogutma_pct",
            "baski_kaybi_pct", "P150_W", "kayip150_pct", "k_son", "arizalar", "not"]
    g = g.reindex(columns=list(dict.fromkeys(COLUMNS + list(g.columns))))
    with pd.option_context("display.width", 220, "display.max_colwidth", 42):
        print(g[show].fillna("").to_string(index=False))
    print(f"\n(0 = kalibrasyon gunu; blok kaybinda gunluk dogal oynama ~+-5%, fan sogutmasi normalde ~-8%)")

    # kasitli ariza deneyleri (folyo, corapsiz ...) istatistige girmez; gercek arizalar girer
    deney = g["not"].fillna("").str.contains("deney")
    if deney.any():
        print(f"(trend istatistigi {deney.sum()} kasitli deney oturumunu disarida birakir)")
    for col, name in (("blok_kaybi_pct", "blok kaybi"), ("fan_sogutma_pct", "fan sogutmasi"),
                      ("baski_kaybi_pct", "baskida hava kaybi"), ("kayip150_pct", "150C on isitma kaybi")):
        s = g.loc[~deney, ["oturum", col]].dropna()
        if len(s) < 2:
            continue
        last, prev = s[col].iloc[-1], s[col].iloc[:-1].median()
        msg = f"{name}: son oturum {last:+.1f}%, onceki {len(s) - 1} oturumun medyani {prev:+.1f}%"
        if abs(last - prev) > DRIFT_WARN:
            msg += f"  !! {DRIFT_WARN:.0f}%'ten fazla fark"
        days = (pd.to_datetime(s.oturum) - pd.to_datetime(s.oturum.iloc[0])).dt.total_seconds() / 86400
        if len(s) >= 5 and days.iloc[-1] >= 3:
            slope = np.polyfit(days, s[col], 1)[0] * 7
            msg += f"; egim {slope:+.1f}%/hafta" + ("  !! yavas kayma" if abs(slope) > SLOPE_WARN else "")
        print(msg)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Session-to-session trend of hotend losses (twin_gecmis.csv).")
    ap.add_argument("--backfill", action="store_true", help="rebuild the history from the benchmark and fault logs")
    ap.add_argument("--add", metavar="LOG", help="add one session log to the history")
    args = ap.parse_args()
    if args.backfill or args.add:
        import evaluate as d
        mod = d.load_model()
        if args.backfill:
            backfill(mod)
        else:
            r = summarize(args.add, mod)
            if r:
                append(r); print("eklendi")
            else:
                print("log bos/cok kisa - eklenmedi")
    report()
