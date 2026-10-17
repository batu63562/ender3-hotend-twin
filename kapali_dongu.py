"""
Kapali dongu simulasyonu: ikizin icine Klipper'in PID'si konur, isitici gucu GIRDI olmaktan cikar,
ikiz tarafindan TAHMIN edilir. Baski oncesi tahminin (G-code -> sicaklik/guc) cekirdegi.

Golge (canli ikiz):   gercek pwm -> [hotend modeli] -> T_model   (residual = T_gercek - T_model)
Kapali dongu (bu):    hedef -> [Klipper PID] -> pwm_tahmin -> [hotend modeli] -> T_tahmin
                                    ^-------------- T_tahmin (geri besleme) ----'
                      filaman akisi ve fan modele bozucu etki olarak girer.

Sinav (G-code gerekmez): bir logdaki hedef sicaklik, fan, filaman akisi ve tabla gucu bu donguye
verilir; ikizin tahmin ettigi isitici gucu ve sicaklik, logdaki GERCEK pwm ve sicaklikla karsilastirilir.
Ikiz gucu de dogru tahmin ediyorsa yaziciyi gercekten "taklit ediyor" demektir.

PID: klippy/extras/heaters.py ControlPID (klasik konum formu) birebir:
    Kp, Ki, Kd = pid_kp/255, pid_ki/255, pid_kd/255
    turev: min_deriv_time (= smooth_time, varsayilan 1 s) ile yumusatilir
    integral [0, max_power/Ki] araliginda tutulur; cikis doyduysa integral guncellenmez (anti-windup)
    hedef <= 0 iken isitici kapali.
Sicaklik okuma ve PID guncellemesi her REPORT_TIME (0.3 s) - Klipper'daki ADC raporlama araligi.

Kullanim:
    python kapali_dongu.py --set ayar        # benchmark/setler.csv (PID 26.09'da degisti: 28.09 oncesi atlanir)
    python kapali_dongu.py --set sinav
    python kapali_dongu.py LOG.csv
"""
import argparse
import os

import numpy as np
import pandas as pd

import degerlendir as d

# printer.cfg SAVE_CONFIG blogu (2026-09-26 PID ayari, kullanicidan 2026-10-09)
PID_KP, PID_KI, PID_KD = 40.262, 12.201, 33.216
PID_PARAM_BASE = 255.0
SMOOTH_TIME = 1.0          # [extruder] smooth_time (printer.cfg'de yok -> Klipper varsayilani)
MAX_POWER = 1.0
REPORT_TIME = 0.3          # s - Klipper ADC sicaklik raporu / PID guncelleme araligi
PID_SINCE = pd.Timestamp("2026-09-28").timestamp()   # bu PID'nin kesin gecerli oldugu ilk gun
AVG = 10.0                 # s - guc karsilastirmasi bu ortalamayla (PID'nin anlik dalgasi degil, enerji)


class KlipperPID:
    """klippy/extras/heaters.py ControlPID'nin birebir kopyasi."""

    PID_SETTLE_DELTA, PID_SETTLE_SLOPE = 1.0, 0.1    # M109/M190 bekleme kosulu (heaters.py)

    def __init__(self, T0, pwm0=0.0, target0=0.0, gains=(PID_KP, PID_KI, PID_KD)):
        self.Kp, self.Ki, self.Kd = (g / PID_PARAM_BASE for g in gains)
        self.integ_max = MAX_POWER / self.Ki
        self.prev_T, self.prev_t, self.prev_deriv = T0, None, 0.0
        # log ortasindan basliyorsa integral, o anki gercek guce denk gelecek sekilde kurulur
        err = target0 - T0
        self.prev_integ = min(self.integ_max, max(0.0, (pwm0 - self.Kp * err) / self.Ki)) if target0 > 0 else 0.0

    def update(self, t, T, target):
        dt = REPORT_TIME if self.prev_t is None else t - self.prev_t
        dT = T - self.prev_T
        if dt >= SMOOTH_TIME:
            deriv = dT / dt
        else:
            deriv = (self.prev_deriv * (SMOOTH_TIME - dt) + dT) / SMOOTH_TIME
        err = target - T
        integ = min(self.integ_max, max(0.0, self.prev_integ + err * dt))
        co = self.Kp * err + self.Ki * integ - self.Kd * deriv
        out = min(MAX_POWER, max(0.0, co))
        self.prev_T, self.prev_t, self.prev_deriv = T, t, deriv
        if co == out:
            self.prev_integ = integ
        return out if target > 0 else 0.0

    def busy(self, smoothed_T, target):
        """Klipper ControlPID.check_busy: M109/M190 bu False olana kadar bekler."""
        return abs(target - smoothed_T) > self.PID_SETTLE_DELTA or abs(self.prev_deriv) > self.PID_SETTLE_SLOPE


def inputs(df):
    """Logdan REPORT_TIME izgarasinda girdiler (sifirinci dereceden tutma): hedef, fan, net akis, tabla."""
    t = df.t.values
    grid = np.arange(t[0], t[-1], REPORT_TIME)
    idx = np.clip(np.searchsorted(t, grid, side="right") - 1, 0, len(t) - 1)
    tg = pd.to_numeric(df.target, errors="coerce").ffill().fillna(0.0).values
    bp = pd.to_numeric(df.bed_pwm, errors="coerce").ffill().fillna(0.0).values if "bed_pwm" in df else np.zeros(len(df))
    # ikizle ayni net akis: son 10 s'deki isaretli hizin zaman ortalamasi, 0'in alti kirpilir
    ev = pd.Series(df.extrude_velocity.values, index=pd.to_datetime(t, unit="s"))
    ext = np.maximum(ev.rolling(f"{d.rw.EXT_AVG_WINDOW:.0f}s").mean().values, 0.0)
    return grid, tg[idx], df.fan_state.values[idx], ext[idx], bp[idx], idx


def simulate(df, m, k=1.0, kfil=1.0):
    """Kapali dongu: donus (izgara, T_tahmin, pwm_tahmin, logdaki satir indeksi)."""
    grid, tg, fan, ext, bp, idx = inputs(df)
    T0 = float(df.T1_real.iloc[0])
    T1, T2, T3 = m.initial_state(T0, float(df.pwm.iloc[0]), float(df.fan_state.iloc[0]))
    pid = KlipperPID(T0, float(df.pwm.iloc[0]), float(tg[0]))
    Ts, Ps = np.empty(len(grid)), np.empty(len(grid))
    for i, t in enumerate(grid):
        pwm = pid.update(t, T1, tg[i])          # termistor = modelin T1 dugumu
        Ts[i], Ps[i] = T1, pwm
        scale = 1.0 - m.BED_SAG * bp[i]
        T1, T2, T3 = m.model_step(T1, T2, T3, pwm * scale, fan[i], ext[i], REPORT_TIME, k, k_fil=kfil)
    return grid, Ts, Ps, idx


def score(df, grid, Ts, Ps, idx):
    """Gercekle karsilastirma: baski bolumunde guc (W, 10 s ortalama) ve sicaklik; isinma suresi."""
    P_real = pd.to_numeric(df.pwm, errors="coerce").ffill().values[idx] * d.rw.P_MAX
    T_real = df.T1_real.ffill().values[idx]
    tg = pd.to_numeric(df.target, errors="coerce").ffill().fillna(0.0).values[idx]
    n = max(1, int(AVG / REPORT_TIME))
    avg = lambda x: pd.Series(x).rolling(n, min_periods=n).mean().values
    Pr, Pp = avg(P_real), avg(Ps * d.rw.P_MAX)
    hold = (tg >= 230) & (np.abs(T_real - tg) < 3) & (grid - grid[0] > 300)
    hold &= ~np.isnan(Pr)
    out = dict(tutma_dk=hold.sum() * REPORT_TIME / 60)
    if hold.any():
        e = Pp[hold] - Pr[hold]
        out.update(guc_bias=e.mean(), guc_rmse=np.sqrt(np.mean(e ** 2)), P_gercek=Pr[hold].mean(),
                   T_rmse=np.sqrt(np.mean((Ts[hold] - T_real[hold]) ** 2)),
                   doyum_gercek=np.mean(P_real[hold] >= 0.99 * d.rw.P_MAX) * 100,
                   doyum_tahmin=np.mean(Ps[hold] >= 0.99) * 100)
    # isinma: hedefin 230 ustune ciktigi andan hedef-2C'ye ulasma suresi (gercek ve tahmin)
    up = np.flatnonzero((tg[1:] >= 230) & (tg[:-1] < 230))
    if len(up):
        i0 = up[0] + 1
        def reach(T):
            j = np.flatnonzero(T[i0:] >= tg[i0] - 2.0)
            return (grid[i0 + j[0]] - grid[i0]) if len(j) else np.nan
        if T_real[i0] < tg[i0] - 20:
            out.update(isinma_gercek=reach(T_real), isinma_tahmin=reach(Ts))
    return out


def run(paths, chain_k=True):
    m = d.load_model()
    rows = []
    k_prev, kfil_prev = 1.0, 1.0
    for p in paths:
        df = d.read_log(p)
        if df.time.iloc[0] < PID_SINCE:
            continue
        # k, k_fil: canli ikizin bu baskidan ONCE bilecegi deger = onceki logun sonunda ogrenilen (golge
        # tekrarindan). Bu logun kendi olcumlerinden ogrenilen k kullanilmaz (sizinti olurdu).
        k_use, kfil_use = (k_prev, kfil_prev) if chain_k else (1.0, 1.0)
        grid, Ts, Ps, idx = simulate(df, m, k_use, kfil_use)
        r = dict(log=d.short_name(p), k=k_use, **score(df, grid, Ts, Ps, idx))
        rows.append(r)
        if chain_k:
            st = {}
            _, _, _, ks, _ = d.replay(df, m, k0=k_prev, kfil0=kfil_prev, state=st)
            k_prev, kfil_prev = float(ks[-1]), st["kfil"]
        print(f"  {r['log']:22s} k {k_use:.3f}  baski {r.get('tutma_dk', 0):5.1f} dk  "
              + (f"guc gercek {r['P_gercek']:5.2f} W  tahmin hatasi {r['guc_bias']:+5.2f} W (rms {r['guc_rmse']:4.2f})  "
                 f"T rms {r['T_rmse']:4.2f} C  doyum %{r['doyum_gercek']:.1f}/%{r['doyum_tahmin']:.1f}" if 'guc_bias' in r else "")
              + (f"  isinma {r['isinma_gercek']:.0f}/{r['isinma_tahmin']:.0f} s" if 'isinma_gercek' in r else ""))
    return pd.DataFrame(rows)


def summarize(sc, title):
    s = sc.dropna(subset=["guc_bias"])
    w = s.tutma_dk
    print(f"\n{title}: {len(s)} log, {w.sum():.0f} dk baski")
    print(f"  guc tahmini: bias {np.average(s.guc_bias, weights=w):+.2f} W, rms {np.sqrt(np.average(s.guc_rmse**2, weights=w)):.2f} W "
          f"(gercek ort {np.average(s.P_gercek, weights=w):.1f} W)")
    print(f"  sicaklik tahmini (baskida): rms {np.sqrt(np.average(s.T_rmse**2, weights=w)):.2f} C")
    h = sc.dropna(subset=["isinma_gercek", "isinma_tahmin"])
    if len(h):
        print(f"  isinma suresi: gercek ort {h.isinma_gercek.mean():.0f} s, tahmin ort {h.isinma_tahmin.mean():.0f} s, "
              f"ort mutlak fark {np.mean(np.abs(h.isinma_tahmin - h.isinma_gercek)):.0f} s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?")
    ap.add_argument("--set", choices=["ayar", "sinav", "hepsi"])
    ap.add_argument("--k1", action="store_true", help="k=1 sabit (onceki oturumdan k devralma yok)")
    a = ap.parse_args()
    paths = [a.log] if a.log else d.set_paths(a.set or "sinav")
    sc = run(paths, chain_k=not a.k1)
    summarize(sc, a.set or os.path.basename(a.log or ""))
