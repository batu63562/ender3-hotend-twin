"""
Tabla (heated bed) dijital ikizi - nozzle ikizinden BAGIMSIZ model dosyasi.

Yapi (2 dugum):
    [isitici ped + aluminyum plaka, T_al]  --R_AC-->  [cam yuzey (karbonlu cam), T_cam]
       ^ termistor burayi olcer                         ^ baskinin gordugu yuzey, olculmez (tahmin)
    Kayiplar: aluminyum alti/kenarlar H_AL*(T_al - T_amb); cam ustu isinim (eps*alan*sigma*(T^4-Ta^4))
              + H_CAM*(T_cam - T_amb) (deneyde ~0 cikti; konveksiyon aluminyum tarafinda toplandi).

Parametreler: tabla deneyi (2026-09-29, 60/85/100C basamaklar + 38 dk soguma, nozzle kapali, tabla
hareketsiz) acik dongu uyumu. Fiziksel kontrol: C_AL ~ 450 g aluminyum + ped (~400 J/K beklenir),
C_CAM ~ 550 g cam (~460-550 J/K beklenir).

Baski sirasinda tabla deneyden FAZLA isi kaybediyor (tabla Y ekseninde surekli hareket ediyor,
parca fani tablaya ufluyor; iki baskida tutma bias +3.3 / +7.4C). Bunu nozzle'daki k_loss gibi
bir tabla kayip carpani (BedLossLearner) sicaklik tutulurken yavasca ogrenir.

Kullanim (cevrimdisi degerlendirme):
    python tabla_model.py            # tabla kayitli loglarda ogrenmeli / ogrenmesiz puan tablosu
"""
import math
from collections import deque

T_AMB = 25.0
P_BED = 220.0          # W - Ender 3 tabla isiticisi nominal (24 V); kendi voltaj dusumu parametrelere gomulu
C_AL = 422.8           # J/K - isitici ped + aluminyum
C_CAM = 582.3          # J/K - cam
R_AC = 0.1369          # K/W - aluminyum -> cam (tau = R_AC*C_CAM ~ 1.3 dk)
H_AL = 1.249           # W/K - aluminyum alti/kenar kaybi (dogrusal; tabladaki konveksiyonun cogu)
H_CAM = 0.0            # W/K - cam ustu dogrusal kayip (deneyde ~0)
EPS_A = 0.9 * 0.235 * 0.235   # cam ust yuzey: emisivite * alan (m^2)
SIG = 5.670e-8
# Baski sirasindaki ek kayip (tabla v1, 2026-09-29; 6 logdaki tutma pencerelerinden):
#   kayip carpani = 1 + BED_FAN*fan + BED_ACT*aktivite
# fan: parca fani (0-1) havasi baskiya/cama carpiyor (tam fanda ~%18 ek kayip).
# aktivite: son ACT_WINDOW saniyede kafanin hareketli oldugu zaman orani (tabla Y'de hareket ediyor).
# Baski bitince ikisi de sifirlanir: soguma hareketsiz deneydeki kayipla hesaplanir.
BED_FAN = 0.18
BED_ACT = 0.035
ACT_WINDOW = 30.0      # s
ACT_SPEED = 5.0        # mm/s - kafa bundan hizliysa "hareketli"
MAX_SUBSTEP = 1.0      # s
GAP_THRESHOLD = 10.0   # s - daha uzun veri boslugunda olcume yeniden otur

# tabla kayip carpani ogrenmesi (sadece sicaklik TUTULURKEN)
LEARN_WINDOW = 60.0    # s - enerji dengesi penceresi
LEARN_TAU = 300.0      # s - ogrenme zaman sabiti
LEARN_T_RANGE = 1.0    # C - pencerede tabla sicaklik araligi bundan kucukse "tutma"
LEARN_TMIN = 45.0      # C - dusuk sicaklikta kayip kucuk, oran gurultulu
K_MIN, K_MAX = 0.7, 1.6


def rad(T):
    return SIG * EPS_A * ((T + 273.15) ** 4 - (T_AMB + 273.15) ** 4)


def losses(T_al, T_cam):
    """Havaya kayiplar (W): aluminyum tarafi, cam tarafi."""
    return H_AL * (T_al - T_AMB), H_CAM * (T_cam - T_AMB) + rad(T_cam)


def step(T_al, T_cam, pwm, dt, k=1.0):
    """Acik Euler, kucuk alt adimlarla (en hizli mod ~R_AC*C_AL ~ 1 dk; 1 s adim rahat kararli)."""
    n = max(1, math.ceil(dt / MAX_SUBSTEP)); h = dt / n
    for _ in range(n):
        q = (T_al - T_cam) / R_AC
        l_al, l_cam = losses(T_al, T_cam)
        T_al += h * (pwm * P_BED - q - k * l_al) / C_AL
        T_cam += h * (q - k * l_cam) / C_CAM
    return T_al, T_cam


def initial_glass(T_al, pwm):
    """Baslangicta cam: tabla isitiliyorsa kararli durum (camdan cikan = gelen), degilse T_al."""
    if not pwm or pwm <= 0 or T_al < 35:
        return T_al
    Tg = T_al
    for _ in range(50):                      # q = (T_al - Tg)/R = kayip_cam(Tg) -> sabit nokta
        Tg = T_al - R_AC * (H_CAM * (Tg - T_AMB) + rad(Tg))
    return Tg


class BedLossLearner:
    """Tabla tutulurken enerji dengesinden kayip carpani k: k = (verilen - depolanan) / model kaybi."""

    def __init__(self):
        self.k = 1.0
        self.t0 = None

    def update(self, now, dt, T_al, T_cam, pwm, mult=1.0):
        """mult: bilinen ek kayip carpani (fan, aktivite); k sadece bunun OTESINDEKI farki ogrenir."""
        # isitici acik mi: pencerenin ORTALAMA gucuyle (asagida). 2026-10-14: eskiden her ornekte pwm > 0.02
        # isteniyordu; tabla PID cikisi her dakika 0'a dusup 1'e ciktigi icin (Kd 948) pencerelerin neredeyse
        # hepsi atiliyordu, k baskida 23 dk 1.00'da kaldi (gereken ~1.20, residual -9 C). Ortalama kosulla
        # 12 log tutma rms 2.76 -> 1.40 (bias -1.17 -> -0.30), isinma ayni, soguma 1.80 -> 1.63.
        ok = T_al > LEARN_TMIN
        if self.t0 is None:
            self.t0, self.T_first, self.T_min, self.T_max = now, T_al, T_al, T_al
            self.Tc_first = T_cam
            self.E_in = self.E_loss = 0.0
            self.ok = ok
            return self.k
        l_al, l_cam = losses(T_al, T_cam)
        self.E_in += pwm * P_BED * dt
        self.E_loss += (l_al + l_cam) * mult * dt
        self.T_min, self.T_max = min(self.T_min, T_al), max(self.T_max, T_al)
        self.ok = self.ok and ok
        span = now - self.t0
        if span >= LEARN_WINDOW:
            if (self.ok and self.E_in > 0.02 * P_BED * span and self.T_max - self.T_min < LEARN_T_RANGE
                    and self.E_loss > 0):
                stored = C_AL * (T_al - self.T_first) + C_CAM * (T_cam - self.Tc_first)
                k_win = (self.E_in - stored) / self.E_loss
                self.k += min(1.0, span / LEARN_TAU) * (k_win - self.k)
                self.k = min(K_MAX, max(K_MIN, self.k))
            self.t0 = None
            self.update(now, 0.0, T_al, T_cam, pwm, mult)
        return self.k


class BedTwin:
    """Mesaj isleyici (nozzle Twin ile ayni duzen): gecen araligi araligin BASINDAKI tabla gucuyle
    ilerletir, sonra yeni girdileri alir, yeni sicaklik geldiyse residual hesaplar."""

    def __init__(self, T_al, pwm=0.0, learn=True, extras=True):
        self.T_model = T_al
        self.T_cam = initial_glass(T_al, pwm)
        self.pwm = pwm or 0.0
        self.fan = 0.0
        self.moving = False
        self.T_real = T_al
        self.prev = None
        self.learn = learn
        self.extras = extras               # False: fan/aktivite etkisi yok (hareketsiz deney modeli)
        self.learner = BedLossLearner()
        self.act_hist = deque()            # (aralik_sonu, dt, hareketli mi)
        self.act = 0.0

    def mult(self):
        return 1.0 + BED_FAN * self.fan + BED_ACT * self.act if self.extras else 1.0

    def on_message(self, now, T_real=None, pwm=None, fan=None, head_v=None, ext=None):
        """head_v (kafa hizi) yoksa aktivite ekstruzyondan (ext) anlasilir."""
        out = {"residual": None, "senkron": False}
        if self.prev is not None:
            dt = now - self.prev
            if dt > GAP_THRESHOLD or dt < 0:
                T0 = T_real if T_real is not None else self.T_real
                self.T_model, self.T_cam = T0, initial_glass(T0, self.pwm)
                self.learner.t0 = None
                self.act_hist.clear()
                out["senkron"] = True
            else:
                self.act_hist.append((now, dt, 1.0 if self.moving else 0.0))
                while self.act_hist and self.act_hist[0][0] < now - ACT_WINDOW:
                    self.act_hist.popleft()
                span = sum(h[1] for h in self.act_hist)
                self.act = sum(h[1] * h[2] for h in self.act_hist) / span if span > 0 else 0.0
                k = self.learner.k if self.learn else 1.0
                self.T_model, self.T_cam = step(self.T_model, self.T_cam, self.pwm, dt, k * self.mult())
                if self.learn:
                    # ogrenme OLCULEN aluminyum sicakligi ve modelin cam tahmini ile
                    self.learner.update(now, dt, self.T_real, self.T_cam, self.pwm, self.mult())
        self.prev = now
        if pwm is not None:
            self.pwm = pwm
        if fan is not None:
            self.fan = fan
        if head_v is not None:
            self.moving = head_v > ACT_SPEED
        elif ext is not None:
            self.moving = abs(ext) > 0.1
        if T_real is not None:
            self.T_real = T_real
            if not out["senkron"]:
                out["residual"] = T_real - self.T_model
        return out


# ---------------------------------------------------------------------------
# Cevrimdisi degerlendirme
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import numpy as np
    import pandas as pd

    HERE = os.path.dirname(os.path.abspath(__file__))
    L = os.path.join(HERE, "loglar")
    LOGS = [("tabla deneyi (egitim)", os.path.join(L, "deneyler", "tabla_deney1.csv")),
            ("28.09 string", os.path.join(L, "baskilar", "2809_string_temiz.csv")),
            ("28.09 aksam", os.path.join(L, "baskilar", "2809_aksam_240C.csv")),
            ("29.09 benchy1", os.path.join(L, "baskilar", "2909_benchy1.csv")),
            ("29.09 geri cekme", os.path.join(L, "baskilar", "2909_geri_cekme_testi.csv")),
            ("29.09 benchy2", os.path.join(L, "baskilar", "2909_benchy2.csv")),
            ("29.09 huni", os.path.join(L, "baskilar", "2909_huni.csv")),
            ("29.09 topak/havada", os.path.join(L, "baskilar", "2909_nozzle_filament_havada_baski.csv")),
            ("29.09 aksam uzun", os.path.join(L, "baskilar", "2909_aksam_uzun.csv"))]

    def replay(path, learn, extras):
        d = pd.read_csv(path, low_memory=False)
        for c in ("eventtime", "bed_T", "bed_pwm", "fan_state", "head_v", "extrude_velocity"):
            if c in d:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        d = d.dropna(subset=["eventtime"]).reset_index(drop=True)
        # tabla gucu ilk kez bilindigi andan basla (eski script kopyalari degismeyen gucu gormuyordu:
        # 29.09 benchy1'in basinda tabla %100 isinirken guc sutunu bos)
        d = d.iloc[int(d.bed_pwm.first_valid_index()):].reset_index(drop=True)
        T = d.bed_T.ffill().bfill().values; u = d.bed_pwm.ffill().fillna(0).values; t = d.eventtime.values
        fan = d.fan_state.values; ext = d.extrude_velocity.values
        hv = d.head_v.values if "head_v" in d else np.full(len(d), np.nan)
        newT = np.r_[True, np.diff(T) != 0]                       # tabla sicakligi degisen satirlar
        tw = BedTwin(T[0], u[0], learn, extras)
        res = np.full(len(d), np.nan); ks = np.ones(len(d))
        for i in range(len(d)):
            r = tw.on_message(t[i], T[i] if newT[i] else None, u[i],
                              None if np.isnan(fan[i]) else fan[i], None if np.isnan(hv[i]) else hv[i],
                              None if np.isnan(ext[i]) else ext[i])
            if r["residual"] is not None:
                res[i] = r["residual"]
            ks[i] = tw.learner.k
        return T, u, res, ks

    VARIANTS = [("hareketsiz model", False, False), ("+ fan/aktivite", False, True), ("+ fan/akt. + ogrenme", True, True)]
    print(f"{'':22s} {'':22s} {'RMSE':>5s} {'isinma':>7s} {'tutma (bias)':>14s} {'soguma':>7s} {'son k':>6s}")
    for name, p in LOGS:
        for vname, learn, extras in VARIANTS:
            T, u, res, ks = replay(p, learn, extras)
            m = {"isinma": u > 0.95, "tutma": (u > 0.05) & (u < 0.95), "soguma": (u == 0) & (T > 30)}
            f = lambda w: np.sqrt(np.nanmean(res[w] ** 2)) if w.any() else float("nan")
            print(f"{name:22s} {vname:22s} {np.sqrt(np.nanmean(res**2)):5.2f} {f(m['isinma']):7.2f} "
                  f"{f(m['tutma']):6.2f} ({np.nanmean(res[m['tutma']]):+5.2f}) {f(m['soguma']):7.2f} {ks[-1]:6.3f}")
        print()
