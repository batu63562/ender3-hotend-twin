import asyncio
import json
import time
import csv
import math
import os
import shutil
import tomllib
from collections import deque

import websockets

import tabla_model   # tabla ikizi (ayri model dosyasi; bu dosyadaki nozzle modeline dokunmaz)

# Tum dosyalar script'in klasorune yazilir (VS Code hangi klasorden calistirirsa calistirsin)
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "realtime_log_ws.csv")
STATE_FILE = os.path.join(HERE, "twin_state.json")
STATE_MAX_AGE = 1800   # 30 dakika - bunun ustunde dosya "bayat" sayilir
STATE_MAX_T1_DIFF = 5.0  # C - kayitli T1 ile simdiki T1 bundan fazla farkliysa devralma

# Yazicinin adresi config.toml'dan (config.example.toml'dan kopyalanir; git'e girmez). Dosya yoksa da import
# edilebilir: degerlendir.py ve plan araclari yaziciya baglanmaz, adres yalniz canli ikizde gerekir (main).
CONFIG_FILE = os.path.join(HERE, "config.toml")


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


CONFIG = load_config()
PRINTER_HOST = str(CONFIG.get("printer_host", "")).strip()
WS_URL = f"ws://{PRINTER_HOST}/websocket"

# Hotend modeli, 3 dugumlu RC agi:
#   [isitici kartus T3] --R31--> [termistor civari blok T1] --R_HB--> [nozzle/heat break ustu T2]
# T1 havaya kaybeder (tasinim + isinim, k ile olceklenir) ve baskiya isi verir (filaman + temas, k_fil);
# T2 heat break uzerinden heatsink'e (HA2) ve parca fanina (HA2_FAN, k ile olceklenir) kaybeder.
# Parametreler Eylul kalibrasyon loglarina fiziksel sinirlarla fit edildi (benchmark/). Heatsink fani
# Klipper'dan okunmuyor; hep calistigi icin etkisi HA2 icinde.
MODEL_VERSION = "3dugum-gecikenkutle-v4"   # twin_state.json farkli surumden kalmissa devralinmaz [K01]
T_AMB = 25      # oda sicakligi (C); sabit varsayim - gercek oda 21-25 C, fark k'ya yansir
T_AMB_K = T_AMB + 273.15
P_MAX = 40      # W - isitici kartusun tam gucu
BED_SAG = 0.042  # tabla acikken 24 V hat duser: gercek guc = pwm * P_MAX * (1 - BED_SAG * tabla_pwm) [K02]

C3 = 0.8303     # J/K - isitici kartus
R31 = 3.0       # K/W - kartus -> blok (245C tutmada kartus ~55C daha sicak)
C1 = 2.0        # J/K - termistor civari blok
HA1 = 0.03624   # W/K - bloktan havaya tasinim
CRAD = 1.5259e-10
R_HB = 1.805    # K/W - blok -> geri kalan sicak metal
C2 = 4.034      # J/K - geri kalan sicak metal (nozzle, blogun uzak tarafi, heat break ustu)
HA2 = 0.004435  # W/K - geri kalan metalden disari (heat break -> heatsink) kayip
HA2_FAN = 0.0293  # W/K - parca sogutma fani tam hizda T2'den ek iletkenlik [K03]
FAN_EXP = 0.508   # fan sogutmasi ~ ((f-F0)/(1-F0))**FAN_EXP (zorlanmis tasinim h ~ hava hizi^0.5) [K03]
FAN_F0 = 0.191    # fan esigi: 5015 blower bu PWM'in altinda kanal direncini yenemiyor, sogutma ~0 [K03]
K_FIL = 0.0054   # W/(K*mm/s) - filamanin aldigi isi = K_FIL * net besleme hizi * (T1 - T_amb) (PETG cp'si) [K04]

MAX_SUBSTEP = 0.3      # s - RK4 alt adimi ust siniri
RK4_STABILITY = 2.5    # RK4 gercek eksende |lambda*h| < 2.78 ister; pay birakildi

# Ogrenme (KLossLearner): k (havaya kayip carpani) ve k_fil (fanli baskida baskiya giden isi carpani), olculen
# guc ile modelin kaybi arasindaki enerji dengesinden, pencere pencere ve kucuk bir Kalman filtresiyle:
#   olcum  y = (verilen - depolanan - heatsink yolu) / sure   [W]
#   model  y = k * (blok + fan kaybi) + k_fil * K_FIL * akis * (T - T_amb)
# Iki tur pencere: TUTMA (ekstruzyon yok, yalniz k) ve BASKI (k + k_fil). Fan sogutmuyorken baskiya giden isi
# yalniz filamanin isinmasi (carpan 1.0 sabit, "A0"). Gecmis ve karneler: [K05]-[K09], [K17].
ADAPT_TMIN = 230.0     # C - yalniz baski sicakligi bolgesinde ogren [K11]
ADAPT_TAU = 60.0       # s - ogrenme zaman sabiti
ADAPT_WINDOW = 20.0    # s - enerji dengesi bu uzunlukta pencerelerde toplanir
ADAPT_T_RANGE = 3.0    # C - pencere boyunca T araligi bundan kucukse "sabit tutma" sayilir
K_LOSS_MIN, K_LOSS_MAX = 0.85, 1.15   # k'nin fiziksel siniri
PRINT_WINDOW = 60.0       # s - baski penceresi; kisa pencere gecislerin arasina sigar [K17]
PRINT_TAU = 1800.0        # s - baskida k'nin zaman sabiti: arizalar (saniye-dakika) k'ya sizmaz [K06]
PRINT_T_RANGE = 5.0       # C - pencere boyunca izin verilen T araligi
PRINT_VALID_FRAC = 0.8    # pencerenin en az bu kadari gecerli eksik-guc olcumu (isinma/hedef degisimi yok)
# Kalman: Q_FIL k_fil'in ne hizla degisebilecegi (surec gurultusu), R_* pencere olcum gurultusu. k'nin surec
# gurultusu yok; hizini kovaryans tabani (span/tau) belirler [K07]. Q_FIL'i buyutmek yavas arizalari yutar [K17].
Q_FIL = 0.05 ** 2 / 3600.0    # 1/s - k_fil saatte ~0.05 degisebilir
R_HOLD = 0.3 ** 2             # W^2 - 20 s tutma penceresinin olcum gurultusu
R_PRINT = 0.4 ** 2 * 300.0 / PRINT_WINDOW   # W^2 - 300 s pencerede artik ~0.4 W; gurultu ~1/sure
P0_FIL = 0.15 ** 2            # k_fil'in oturum (ya da malzeme degisimi) basi belirsizligi
K_FIL_MIN, K_FIL_MAX = 0.8, 1.6   # k_fil = filaman isinmasi (1.0) + nozzle'dan baskiya temasla iletim [K08]
K_STATE_MAX_AGE = 7 * 86400.0   # s - k oturumlar arasi saklanir; bundan eskisi devralinmaz
EXT_EPS = 1e-6   # mm/s - Klipper bosta 1e-15 mertebesi kirinti verir; bundan kucuk hiz sifir [K16]
# Reddedilen/kaldirilan kurallar: varistan sonra tutma ogrenmesini bekletmek [K10], 150 C on isitmadan ogrenmek [K11],
# k'yi 150 C altinda sondurmek [K12].
# Sicakliga bagli isi kapasitesi: C(T) = C * (1 + CAP_T_COEF * (T - CAP_T0)), her dugum kendi sicakliginda;
# kararli duruma etkisi yok, isinma dinamigini duzeltir [K13]. 0: kapali (sabit C).
CAP_T_COEF = 0.0012  # 1/K
CAP_T0 = 140.0      # C - C(T) = C bu sicaklikta

def cap_scale(T):
    """Isi kapasitesinin T'deki carpani (bkz. CAP_T_COEF)."""
    return max(0.5, 1.0 + CAP_T_COEF * (T - CAP_T0))


def cap_energy(T):
    """cap_scale'in T'ye gore integrali (J/K basina): C * cap_energy(T) dugumun ic enerjisi (sabit haric)."""
    return T + 0.5 * CAP_T_COEF * (T - CAP_T0) ** 2


def stored_energy(T_from, T_to):
    """Uc dugum birlikte T_from'dan T_to'ya gecerken depolanan enerji (J); C(T) dahil."""
    return (C1 + C2 + C3) * (cap_energy(T_to) - cap_energy(T_from))

# Kayma alarmi (KAYIP_*): ogrenme yavas bir arizayi (corap dustu, kanal kaydi) "normal" sayip residual'dan
# silebilir; o yuzden k'nin kendisi izlenir. Referans = son KABUL edilen k seviyesi (oturumlar arasi saklanir):
# k referanstan K_ALARM_BAND kadar sapinca alarm; k yeni seviyede K_ACCEPT_TIME boyunca (sicak calisma)
# K_ACCEPT_RANGE icinde kalirsa yeni seviye kabul edilir, alarm kapanir ve olay rapora yazilir [K14].
# Bu kayan referans yavas bozulmayi adim adim kabul eder; onu sabit referansli bakim gostergesi yakalar [K20].
K_ALARM_BAND = 0.08
K_ALARM_CLEAR = 0.06        # alarm ancak sapma bunun altina inince kapanir (deney 8: k 0.92 bant kenarinda 13 kez ac-kapa)
K_ACCEPT_TIME = 30 * 60.0   # s - bant disinda VE sabit kalinan sicak (T1 > ADAPT_TMIN) sure
K_ACCEPT_RANGE = 0.02       # k bu aralikta kaliyorsa "sabit" sayilir

# Ariza tespiti (2026-10-12'den itibaren IKI alarm): hizli arizalar GUC_* (eksik guc, asagida), yavas kayma KAYIP_*
# (ogrenilen k'nin oturum basina gore sapmasi). Sicaklik residual alarmi (SICAKLIK_*) kaldirildi: acik cevrim model
# residual'da +-2C gezinir (bkz. ENERGY_*), anomali karari guc dengesinde. Bir durum ancak onay suresi KESINTISIZ
# surerse ariza sayilir (tek ornekli sivrilikler alarm uretmesin), FAULT_CLEAR saniye gorulmezse biter.
FAULT_CONFIRM = 3.0
FAULT_CLEAR = 10.0
FAULT_TEXT = {
    "KAYIP_ARTTI": "isi kaybi son saatlere gore artti: silikon corap dusmus, cereyan/hava akimi ya da "
                   "fan kanali bloga yonelmis olabilir",
    "KAYIP_AZALDI_FAN": "fan acik ama beklenen sogutma yok (son saatlere gore): parca sogutma fani "
                        "donmuyor ya da kanali tikali olabilir",
    "KAYIP_AZALDI": "isi kaybi son saatlere gore azaldi: nozzle'da plastik/topak, ortam isinmis (kapali kutu?) "
                    "ya da hotend yalitimi degismis olabilir",
}

# Eksik guc (RESMI hizli-ariza alarmi, GUC_*): son ENERGY_WINDOW saniyede modelin bekledigi kayip ile gercekte giden
# isi (olculen guc - depolanan enerji) arasindaki fark (W). + = beklenenden AZ isi gidiyor.
# Neden sicaklik degil guc: PID gercek sicakligi sabit tuttugu icin isitici gucu gercek yukun olcumu (kalorimetre);
# acik cevrim sicaklik residual'i kucuk guc hatalarini ~15 C/W ile buyutup biriktirir [K15].
ENERGY_WINDOW = 20.0      # s
ENERGY_THRESHOLD = 2.5    # W
ENERGY_CONFIRM = 5.0      # s - kesintisiz
ENERGY_TMIN = 230.0       # C - sadece baski sicakligi bolgesinde (model orada en iyi kalibre)
ENERGY_T_RANGE = 3.0      # C - pencerede T araligi bundan kucukse "tutma"
ENERGY_SETTLE = 30.0      # s - isinma (isitici 3 s ort. >= %95) bittikten sonra bekleme
ENERGY_TEXT = {
    "GUC_AZALDI": "beklenenden AZ isi gidiyor: filaman akmiyor/tikanma, fan havasi nozzle'a ulasmiyor "
                  "ya da nozzle etrafinda topak olabilir",
    "GUC_ARTTI": "beklenenden FAZLA isi gidiyor: silikon corap dusmus, cereyan ya da fan kanali bloga "
                 "yonelmis olabilir",
}
FAULT_TEXT.update(ENERGY_TEXT)

# Duraklatma (ikizin yaziciya tek geri beslemesi): GUC_AZALDI PAUSE_AFTER s kesintisiz surerse ve baski
# yazdiriliyorsa. GOLGE modda yalniz "duraklatirdim" kaydi (CSV duraklat, ariza_raporlari.txt); aktif modda Moonraker'a
# pause. Zararsiz olaylar <= 110 s, kalici gercek arizalar >= 246 s surdu [K19]. KAYIP_* asla duraklatmaz.
PAUSE_AFTER = 150.0       # s
PAUSE_MODE = "golge"      # "golge" (yalniz kayit) | "aktif" (Moonraker'a pause) | "kapali"

# Malzeme ve yedek: k ortak (hotend ozelligi); k_fil malzeme basina saklanir. Malzeme G-code basligindan
# ("; filament_type = ") okunur, degisince k_fil o malzemenin kaydindan yeniden ogrenilir (Twin.set_material).
# Her acilista twin_state.json'in tarihli kopyasi alinir, en yeni STATE_BACKUPS tanesi tutulur [K21].
STATE_BACKUP_DIR = os.path.join(HERE, "durum_yedek")
STATE_BACKUPS = 30

# Bakim gostergesi (model DISI tavsiye; tahmine ve alarma dokunmaz): k'nin tek bilinmeyen oldugu her pencerenin
# kendi k'si MAINT_FILE'a yazilir. Acilista fansiz baski pencerelerinin gunluk medyani SABIT temiz referansla
# (MAINT_REF, kalibrasyon) kiyaslanir; son MAINT_DAYS veri gununde ayni yonde MAINT_BAND disindaysa bakim onerisi.
# Referans yalniz donanim degisince elle degistirilir [K20].
MAINT_FILE = os.path.join(HERE, "bakim_izleme.csv")
MAINT_STATUS_FILE = os.path.join(HERE, "bakim_durumu.json")   # acilistaki sonuc: Unity okur (kurali yeniden hesaplamaz)
MAINT_REF = 1.00
MAINT_BAND = 0.06
MAINT_DAYS = 3
MAINT_LOOKBACK = 21          # gun - kural yalniz bu kadar yeni veriye bakar
MAINT_MIN_WINDOWS = 2        # gun basina en az bu kadar fansiz baski penceresi


def maint_record(rows):
    """[(duvar_zamani, tur, k)] -> MAINT_FILE'a ekle."""
    if not rows:
        return
    new = not os.path.exists(MAINT_FILE)
    try:
        with open(MAINT_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "tarih", "tur", "k"])
            for tw, kind, kv in rows:
                w.writerow([round(tw, 1), time.strftime("%Y-%m-%d %H:%M", time.localtime(tw)), kind, round(kv, 4)])
    except OSError as e:
        print(f"[bakim izleme kaydi yazilamadi: {e}]")


def maint_check():
    """Bakim onerisi metni ya da None (bkz. MAINT_*). Sonuc (oneri yoksa da) MAINT_STATUS_FILE'a yazilir:
    Unity acilis menusunde gosterir, kurali kendisi hesaplamaz (tek dogruluk kaynagi burasi)."""
    status = {"time": round(time.time(), 1), "enough": False, "advice": False, "direction": "",
              "reference": MAINT_REF, "band": MAINT_BAND, "days": [], "text": ""}
    try:
        with open(MAINT_FILE, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("tur") == "fansiz_baski"]
    except OSError:
        rows = []
    cutoff = time.time() - MAINT_LOOKBACK * 86400
    days = {}
    for r in rows:
        try:
            tw, kv = float(r["time"]), float(r["k"])
        except (KeyError, ValueError):
            continue
        if tw >= cutoff and 0.6 < kv < 1.5:
            days.setdefault(time.strftime("%Y-%m-%d", time.localtime(tw)), []).append(kv)
    med = [(dd, sorted(v)[len(v) // 2]) for dd, v in sorted(days.items()) if len(v) >= MAINT_MIN_WINDOWS]
    last = med[-MAINT_DAYS:]
    status["days"] = [{"date": dd, "k": round(kv, 4)} for dd, kv in last]
    text = None
    if len(med) >= MAINT_DAYS:
        status["enough"] = True
        devs = [kv - MAINT_REF for _, kv in last]
        why = None
        if all(dv < -MAINT_BAND for dv in devs):
            status["direction"] = "low"
            why = ("hotend beklenenden AZ isi kaybediyor -> heatsink tarafini kontrol et: heat break termal macunu, "
                   "heatsink/fan cevresinde kurumus plastik ve toz (isi surunmesi = tikanma riski)")
        elif all(dv > MAINT_BAND for dv in devs):
            status["direction"] = "high"
            why = "hotend beklenenden FAZLA isi kaybediyor -> silikon corap, fan kanali yonu, cereyan"
        if why:
            text = (f"BAKIM ONERISI: son {MAINT_DAYS} gun fansiz baski k = "
                    + ", ".join(f"{dd[5:]} {kv:.3f}" for dd, kv in last)
                    + f" (temiz referans {MAINT_REF:.2f}, esik +-{MAINT_BAND:.2f}); {why}")
    status["advice"], status["text"] = text is not None, text or ""
    try:
        tmp = MAINT_STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=1)
        os.replace(tmp, MAINT_STATUS_FILE)
    except OSError as e:
        print(f"[bakim durumu yazilamadi: {e}]")
    return text

# Ariza siniflandirma (ariza_siniflandir.py): ariza onaylaninca olayin fiziksel kategorisi
# (A fan yolu / B filaman akmiyor / C yalitim-cevre / D termistor / E isitici) arka planda
# hesaplanir. Kategori icin olaydan sonra veri gerekir; onaydan bu kadar saniye sonra calisir
# (ikincisi olay surerken guncelleme). Tespit mantigina ve Twin'e dokunmaz.
CLASSIFY_DELAYS = (60.0, 180.0)
CLASSIFY_BUFFER = 600.0          # s - bellekte tutulan son satirlar (referans + olay penceresi)
REPORT_FILE = os.path.join(HERE, "ariza_raporlari.txt")

# G-code plani (gcode_tahmin.py, yol haritasi 4.3): baski baslayinca dosya ve guncel printer.cfg
# Moonraker'dan indirilir, ikizin plani arka planda uretilir -> PLAN_DIR (Unity Print modulu okur).
# Klipper'dan sadece GIRDI okunur (hangi dosya, ne zaman basladi); katman zamani, akis, guc ikizin tahmini.
HTTP_URL = f"http://{PRINTER_HOST}"
PLAN_DIR = os.path.join(HERE, "tahminler")
GCODE_DIR = os.path.join(HERE, "yazici", "canli")        # indirilen G-code ve printer.cfg
CFG_FALLBACK = os.path.join(HERE, "yazici", "printer.cfg")  # Pi'ye ulasilamazsa yerel kopya
DOWNLOAD_TIMEOUT = 30.0

# Telefondan izleme: script ev aginda kucuk bir sayfa yayinlar (http://<bilgisayar-IP>:WEB_PORT).
# Sadece okur (ikizin hesabina dokunmaz); internete bir sey gondermez. Port acilamazsa izleme
# sayfasiz devam eder. Kapatmak icin WEB_PORT = None.
WEB_PORT = 8080
WEB_HISTORY = 900.0      # s - sayfadaki grafik suresi (15 dk, saniyede en fazla 1 nokta)

# Baglanti: websockets kutuphanesi PING_INTERVAL'da bir yoklar, PING_TIMEOUT icinde cevap gelmezse
# baglantiyi kopmus sayar. Varsayilan 20 s; ag tikanmasinda (2026-09-26 baskilari, video akisi
# sirasinda 8-16 s mesaj gelmedi) script kapaniyordu -> daha sabirli + otomatik yeniden baglanma.
PING_INTERVAL = 20.0
PING_TIMEOUT = 60.0
RECONNECT_DELAY = 5.0

STATE_SAVE_INTERVAL = 5.0   # s - twin_state.json en fazla bu siklikta yazilir (her mesajda degil)
FLUSH_INTERVAL = 1.0        # s - CSV diske en fazla bu siklikta zorlanir

GAP_THRESHOLD = 10.0        # saniye - bunun ustundeki bosluklarda gercege senkronize ol
# Akis girdisi: ekstruder KONUMUNUN (pos_e) iki mesaj arasindaki farki (anlik hiz ornegi geri cekmeleri yanlis
# ornekliyordu); son EXT_AVG_WINDOW saniyede ISARETLI ortalanir (geri cekme + geri itme birbirini goturur), sonra
# 0'in alti kirpilir. pos_e yoksa (eski loglar) anlik hiz kullanilir [K16].
EXT_AVG_WINDOW = 10.0       # s
POS_E_MAX_RATE = 60.0       # mm/s - bundan hizli pos_e sicramasi (Klipper yeniden baslatma vb.) yok sayilir


def initial_state(T1, pwm, fan):
    """Sifirdan baslarken T2/T3 tahmini.
    Isitici calisiyorsa (pwm > 0) sicaklik tutuluyor varsayilir: T3, R31 uzerinden tum kaybi
    tasiyacak kadar sicak (245C'de ~55C ustte), T2 kararli degerinde. Hepsini T1'e esit
    baslatmak sicak baslangicta ilk saniyelerde ~8C sahte cukur uretiyordu.
    Isitici kapaliysa (bosta / soguma) hepsi T1'e esit."""
    if not pwm or pwm <= 0:
        return T1, T1, T1
    fan = fan or 0.0
    dT2 = steady_T2_rise(T1, fan)
    T2 = T_AMB + dT2
    # kararli durumda R31'den gecen isi = toplam kayip (blok yuzeyi + T2 uzerinden)
    T3 = T1 + R31 * (natural_loss(T1) + (HA2 + fan_conductance(fan)) * dT2)
    return T1, T2, T3


def load_state(current_T1, pwm=0.0, fan=0.0):
    """T1 HER ZAMAN gercek olcumden baslar. Sadece T2/T3 onceki calismadan devralinir."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            saved = json.load(f)
        age = time.time() - saved["time"]
        if saved.get("model") != MODEL_VERSION:
            print("[Kayitli durum baska model surumunden - sifirdan baslaniyor]")
        elif age >= STATE_MAX_AGE:
            print(f"[Kayitli durum {age:.0f}s eski - sifirdan baslaniyor]")
        elif abs(saved["T1"] - current_T1) > STATE_MAX_T1_DIFF:
            # nozzle o arada soguduysa/isindiysa kayitli T3 (isitici cekirdek) artik gecersiz
            print(f"[Kayitli T1={saved['T1']:.1f}C, simdiki {current_T1:.1f}C - sifirdan baslaniyor]")
        else:
            print(f"[Onceki T2/T3 durumu bulundu, {age:.0f}s once - devraliniyor]")
            return current_T1, saved["T2"], saved["T3"]
    return initial_state(current_T1, pwm, fan)


def load_k():
    """Onceki oturumlarda ogrenilen (k, k_fil) (ayni model surumu, K_STATE_MAX_AGE'den yeni).
    k_fil'siz eski kayitta k_fil = 1."""
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return 1.0, 1.0
    age = time.time() - saved.get("k_time", saved.get("time", 0))
    k = saved.get("k")
    if k is None or saved.get("model") != MODEL_VERSION or age >= K_STATE_MAX_AGE:
        return 1.0, 1.0
    k = min(K_LOSS_MAX, max(K_LOSS_MIN, float(k)))
    kfil = min(K_FIL_MAX, max(K_FIL_MIN, float(saved.get("k_fil", 1.0))))
    print(f"[Ogrenilmis carpanlar devraliniyor: k={k:.3f}, k_fil={kfil:.3f} ({age/3600:.1f} saat once)]")
    if abs(k - 1.0) > K_ALARM_BAND:
        print(f"[BILGI: hotend kalibrasyona gore %{100 * abs(k - 1.0):.0f} "
              f"{'AZ' if k < 1.0 else 'FAZLA'} isi kaybediyor (kalici durum, alarm degil).]")
    return k, kfil


def load_k_ref(k):
    """Kayip alarminin referansi (son kabul edilen k seviyesi, bkz. K_ACCEPT_TIME); kayit yoksa/eskiyse devralinan k."""
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return k
    age = time.time() - saved.get("k_time", saved.get("time", 0))
    ref = saved.get("k_ref")
    if ref is None or saved.get("model") != MODEL_VERSION or age >= K_STATE_MAX_AGE:
        return k
    ref = min(K_LOSS_MAX, max(K_LOSS_MIN, float(ref)))
    print(f"[Kayip alarmi referansi: k_ref={ref:.3f} (alarm: k, {ref - K_ALARM_BAND:.3f}-{ref + K_ALARM_BAND:.3f} "
          f"disina cikarsa)]")
    return ref


def load_materials():
    """(son malzeme, {malzeme: k_fil}) - kayit yoksa (None, {})."""
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return None, {}
    mats = {m: min(K_FIL_MAX, max(K_FIL_MIN, float(v))) for m, v in (saved.get("malzeme_kfil") or {}).items()}
    last = saved.get("malzeme")
    if mats:
        print(f"[Malzeme basina k_fil: " + ", ".join(f"{m} {v:.2f}" for m, v in mats.items())
              + f"; son malzeme {last or '?'}]")
    return last, mats


def backup_state():
    """Acilista twin_state.json'in tarihli kopyasi; en yeni STATE_BACKUPS tanesi kalir."""
    if not os.path.exists(STATE_FILE):
        return
    try:
        os.makedirs(STATE_BACKUP_DIR, exist_ok=True)
        dst = os.path.join(STATE_BACKUP_DIR, time.strftime("twin_state_%Y%m%d_%H%M%S.json"))
        shutil.copyfile(STATE_FILE, dst)
        old = sorted(f for f in os.listdir(STATE_BACKUP_DIR) if f.startswith("twin_state_") and f.endswith(".json"))
        for f in old[:-STATE_BACKUPS]:
            os.remove(os.path.join(STATE_BACKUP_DIR, f))
    except OSError as e:
        print(f"[durum yedegi alinamadi: {e}]")


def save_state(T1, T2, T3, k=None, kfil=None, k_ref=None, material=None, materials=None):
    data = {"time": time.time(), "model": MODEL_VERSION, "T1": T1, "T2": T2, "T3": T3}
    if k is not None:
        data["k"], data["k_time"] = k, time.time()
    if kfil is not None:
        data["k_fil"] = kfil
    if k_ref is not None:
        data["k_ref"] = k_ref
    if material is not None or materials:
        mats = dict(materials or {})
        if material is not None and kfil is not None:
            mats[material] = kfil              # simdiki malzemenin guncel k_fil'i
        data["malzeme"], data["malzeme_kfil"] = material, mats
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)


def natural_loss(T1):
    """Termistor civari bloktan havaya kayip (dogal tasinim + isinim), k_loss haric."""
    T1_K = T1 + 273.15
    return HA1 * (T1 - T_AMB) + CRAD * (T1_K**4 - T_AMB_K**4)


def fan_conductance(fan):
    """Parca sogutma faninin T2'den havaya ek iletkenligi (W/K), fan hizi 0-1.
    FAN_F0 altinda blower kanalin direncini yenemiyor, sogutma ~0 (deney 5/6)."""
    return HA2_FAN * max((fan - FAN_F0) / (1.0 - FAN_F0), 0.0) ** FAN_EXP


def steady_T2_rise(T1, fan):
    """Kararli durumda T2 - T_amb. T1'den T2'ye gelen akis (T1 - T2)/R_HB, T2'den cikan
    akisa (HA2 + fan iletkenligi)*(T2 - T_amb) esitlenip cozulur."""
    return (T1 - T_AMB) / (1 + R_HB * (HA2 + fan_conductance(fan)))


class KLossLearner:
    """k_loss (havaya kayip carpani) ve k_fil (baskiya giden isi carpani, bkz. K_FIL_MAX) enerji dengesinden ogrenilir.

    SABIT TUTMA pencereleri (ADAPT_WINDOW, ekstruzyon yok) ve BASKI pencereleri (PRINT_WINDOW) kullanilir:
        y = (verilen enerji - depolanan enerji - heatsink yolu kaybi) / sure
    k havaya olan kayiplari (blok yuzeyi + fan) olcekler, heatsink yolunu (HA2) degil; o
    yuzden o yol ayri cikarilir. T2 olculmedigi icin kararli durum degeri kullanilir.
    Depolanan enerji uc kutlenin toplamiyla hesaplanir (C1 + C2 + C3, C(T) dahil: stored_energy).
    Pencere boyunca T araligi kucuk ve T > ADAPT_TMIN degilse pencere atilir. Isiticinin kapanmasi,
    hedef degisimi, rampa gibi gecislerde T araligi buyudugu icin bunlar otomatik disarida kalir.
    (Eski ornek-bazli ogrenme, isitici kapaninca birkac saniye pwm=0'i "kayip=0" sanip
    k'yi 0.96'dan 0.86'ya cekiyordu.)
    [k, k_fil] Kalman filtresiyle birlikte ogrenilir (bkz. _kalman)."""

    def __init__(self, k=1.0, kfil=1.0):
        self.k, self.kfil = k, kfil
        self.P = [[0.0, 0.0], [0.0, P0_FIL]]      # [k, k_fil] kovaryansi (k'ninki tabandan gelir)
        self.t_kal = None                          # son Kalman guncellemesi (surec gurultusu icin)
        self.windows = []                          # (zaman, tur, pencere k'si) - bakim izleme kaydi, bkz. MAINT_*
        self.reset()

    def _kalman(self, now, y, x1, x2, R, a):
        """Tek olcumlu Kalman guncellemesi: y = k*x1 + k_fil*x2 (+ gurultu, varyans R).
        a: k icin en az bu kadar kazanc (eski ustel ogrenicinin span/tau'su). Kalman yalniz kalsa
        isinmadan hemen sonraki (gecis etkili, sapmali) ilk pencerelere fazla guvenip sonra
        kazanci daralttigi icin k'yi yavas duzeltiyordu (cekirdek 3.77 -> 4.13); k varyansi
        her pencerede bu kazanci verecek degere tabanlanir ("kovaryans tabani")."""
        P = self.P
        if self.t_kal is not None:
            P[1][1] += Q_FIL * max(0.0, now - self.t_kal)
        self.t_kal = now
        if a < 1.0 and x1 > 0:
            P[0][0] = max(P[0][0], a * R / ((1.0 - a) * x1 * x1))
        Px = [P[0][0] * x1 + P[0][1] * x2, P[1][0] * x1 + P[1][1] * x2]
        S = x1 * Px[0] + x2 * Px[1] + R
        G = [Px[0] / S, Px[1] / S]
        e = y - (self.k * x1 + self.kfil * x2)
        self.k = min(K_LOSS_MAX, max(K_LOSS_MIN, self.k + G[0] * e))
        self.kfil = min(K_FIL_MAX, max(K_FIL_MIN, self.kfil + G[1] * e))
        self.P = [[P[i][j] - G[i] * Px[j] for j in range(2)] for i in range(2)]

    def _learn(self, now, num, E_loss, E_fil, span, tau, R):
        """num: verilen - depolanan - heatsink yolu (J); E_loss: k ile olceklenen model kaybi (J);
        E_fil: K_FIL ile hesaplanan filaman isisi (J)."""
        self._kalman(now, num / span, E_loss / span, E_fil / span, R, min(1.0, span / tau))
        if E_fil == 0 and E_loss > 0 and len(self.windows) < 10000:
            # k tek bilinmeyen (tutma ya da fansiz baski): pencerenin kendi k'si, ogreniciden bagimsiz (bakim izleme)
            self.windows.append((now, "tutma" if R == R_HOLD else "fansiz_baski", num / E_loss))

    def reset(self):
        """Pencereyi at (veri boslugu vb.). Ogrenilmis k korunur."""
        self.t0 = None
        self.p_t0 = None

    def _start_print(self, now, T1_real):
        self.p_t0, self.p_T_first = now, T1_real
        self.p_T_min = self.p_T_max = T1_real
        self.p_E_in = self.p_E_loss = self.p_E_T2 = self.p_E_fil = self.p_E_fil_off = 0.0
        self.p_valid = self.p_ext = 0.0
        self.p_ok = True

    def update_print(self, now, dt, T1_real, pwm, fan, ext_v, energy_ok, blocked):
        """Baski penceresi (PRINT_WINDOW): tutmadaki enerji dengesine filaman isisi eklenir.
        energy_ok: erken uyari bu aralikta gecerli bir eksik guc hesapladi (isinma/hedef degisimi yok).
        blocked: ani guc sapmasi (|eksik guc| > ENERGY_THRESHOLD ya da GUC_* alarmi) - pencere atilir,
        boylece akis kesintisi gibi arizalar k'ya ogretilmez."""
        if self.p_t0 is None:
            self._start_print(now, T1_real)
            return
        dT2 = steady_T2_rise(T1_real, fan)
        self.p_E_in += pwm * P_MAX * dt
        self.p_E_loss += (natural_loss(T1_real) + fan_conductance(fan) * dT2) * dt
        self.p_E_T2 += HA2 * dT2 * dt
        if fan_conductance(fan) > 0:       # fanli: k_fil ile olceklenen (ogrenilen)
            self.p_E_fil += K_FIL * ext_v * (T1_real - T_AMB) * dt
        else:                              # fansiz: yalniz filamanin isinmasi, carpan 1.0 (sabit)
            self.p_E_fil_off += K_FIL * ext_v * (T1_real - T_AMB) * dt
        self.p_T_min, self.p_T_max = min(self.p_T_min, T1_real), max(self.p_T_max, T1_real)
        self.p_valid += dt if energy_ok else 0.0
        self.p_ext += dt if ext_v > 0.0 else 0.0
        self.p_ok = self.p_ok and T1_real > ADAPT_TMIN and not blocked
        span = now - self.p_t0
        if span >= PRINT_WINDOW:
            if (self.p_ok and self.p_T_max - self.p_T_min < PRINT_T_RANGE and self.p_E_loss > 0
                    and self.p_valid >= PRINT_VALID_FRAC * span and self.p_ext >= 0.5 * span):
                stored = stored_energy(self.p_T_first, T1_real)
                # fansiz kisimdaki baskiya giden isi sabit (K_FIL x 1.0) bilinen terim olarak cikarilir;
                # k_fil yalniz fanli kisimdan ogrenilir ("A0", [K09])
                self._learn(now, self.p_E_in - stored - self.p_E_T2 - self.p_E_fil_off, self.p_E_loss,
                            self.p_E_fil, span, PRINT_TAU, R_PRINT)
            self._start_print(now, T1_real)

    def _start(self, now, T1_real, ok):
        self.t0, self.T_first = now, T1_real
        self.T_min = self.T_max = T1_real
        self.E_in = self.E_loss = self.E_T2 = 0.0
        self.ok = ok

    def update(self, now, dt, T1_real, pwm, fan, extruding):
        ok = T1_real > ADAPT_TMIN and not extruding
        if self.t0 is None:
            self._start(now, T1_real, ok)
            return self.k
        dT2 = steady_T2_rise(T1_real, fan)
        self.E_in += pwm * P_MAX * dt
        self.E_loss += (natural_loss(T1_real) + fan_conductance(fan) * dT2) * dt   # k ile olceklenen
        self.E_T2 += HA2 * dT2 * dt                                        # olceklenmeyen
        self.T_min, self.T_max = min(self.T_min, T1_real), max(self.T_max, T1_real)
        self.ok = self.ok and ok
        if now - self.t0 >= ADAPT_WINDOW:
            if self.ok and self.T_max - self.T_min < ADAPT_T_RANGE and self.E_loss > 0:
                stored = stored_energy(self.T_first, T1_real)
                self._learn(now, self.E_in - stored - self.E_T2, self.E_loss, 0.0,
                            now - self.t0, ADAPT_TAU, R_HOLD)
            self._start(now, T1_real, ok)
        return self.k


def max_stable_substep(fan, extrude_v):
    """RK4'un kararli kalacagi en buyuk alt adim (ust sinir MAX_SUBSTEP).

    Sistemin en hizli modunun hizi, lineerlestirilmis Jacobian'in Gershgorin sinirindan
    (satir basina |kosegen| + |kosegen disi| toplami) yukaridan tahmin edilir. Simdiki parametrelerde
    en hizli zaman sabiti ~1.5 s, kararlilik siniri birkac saniye; 4 Hz mesajlarda (dt ~0.25 s) neredeyse
    her adim tek RK4 adimi. Alt adimlara bolme yalniz veri bosluklarinda ve parametre degisince koruma."""
    dL = (HA1 + 4 * CRAD * 600.0**3             # kayip turevi, 600 K'e kadar guvenli taraf
          + K_FIL_MAX * K_FIL * max(extrude_v, 0.0))
    s = cap_scale(0.0) if CAP_T_COEF > 0 else 1.0   # en kucuk kapasite (C(T) acikken soguk dugum)
    lam = max((2 / R31 + 2 / R_HB + K_LOSS_MAX * dL) / C1,              # T1 satiri
              (2 / R_HB + HA2 + K_LOSS_MAX * fan_conductance(fan)) / C2,  # T2 satiri
              2 / (R31 * C3)) / s                                        # T3 satiri
    return min(MAX_SUBSTEP, RK4_STABILITY / lam)


def model_step(T1, T2, T3, pwm, fan, extrude_v, dt, k_loss=1.0, max_substep=None, k_fil=1.0):
    """RK4 ile, kararlilik icin kucuk alt-adimlara bolerek ilerler.
    k_fil: baskiya giden isi carpani (filaman + nozzle'dan baskiya iletim; ogrenilen, bkz. K_FIL_MAX); K_FIL ile carpilir."""
    if max_substep is None:
        max_substep = max_stable_substep(fan, extrude_v)

    def deriv(T1, T2, T3):
        k_eff = k_loss   # her sicaklikta (sondurme kurali 2026-10-13'te kaldirildi)
        Q_filament = k_fil * K_FIL * max(extrude_v, 0.0) * (T1 - T_AMB)
        Q_conv1 = k_eff * natural_loss(T1) + Q_filament

        Q_heater_to_1 = (T3 - T1) / R31
        Q_hb = (T1 - T2) / R_HB
        dT1 = (Q_heater_to_1 - Q_hb - Q_conv1) / (C1 * cap_scale(T1))

        Q_c2 = HA2 * (T2 - T_AMB)                          # heat break -> heatsink
        Q_fan = k_eff * fan_conductance(fan) * (T2 - T_AMB)   # parca sogutma fani nozzle tarafini sogutur
        dT2 = (Q_hb - Q_c2 - Q_fan) / (C2 * cap_scale(T2))

        Q_heater = pwm * P_MAX
        dT3 = (Q_heater - Q_heater_to_1) / (C3 * cap_scale(T3))

        return dT1, dT2, dT3

    n_sub = max(1, math.ceil(dt / max_substep))
    sub_dt = dt / n_sub

    for _ in range(n_sub):
        k1 = deriv(T1, T2, T3)
        k2 = deriv(T1 + sub_dt/2*k1[0], T2 + sub_dt/2*k1[1], T3 + sub_dt/2*k1[2])
        k3 = deriv(T1 + sub_dt/2*k2[0], T2 + sub_dt/2*k2[1], T3 + sub_dt/2*k2[2])
        k4 = deriv(T1 + sub_dt*k3[0], T2 + sub_dt*k3[1], T3 + sub_dt*k3[2])
        T1 = T1 + sub_dt/6*(k1[0] + 2*k2[0] + 2*k3[0] + k4[0])
        T2 = T2 + sub_dt/6*(k1[1] + 2*k2[1] + 2*k3[1] + k4[1])
        T3 = T3 + sub_dt/6*(k1[2] + 2*k2[2] + 2*k3[2] + k4[2])

    return T1, T2, T3


class FaultMonitor:
    """Anlik alarm kosullarini onaylanmis ariza olaylarina cevirir (onay + bitis gecikmesi)."""

    def __init__(self, confirm=None, clear=None, confirm_by_code=None):
        """confirm_by_code: bazi kodlar icin farkli onay suresi (ornek GUC_*: ENERGY_CONFIRM)."""
        self.confirm = FAULT_CONFIRM if confirm is None else confirm
        self.clear = FAULT_CLEAR if clear is None else clear
        self.confirm_by_code = confirm_by_code or {}
        self.pending = {}     # kod -> kosulun kesintisiz dogru oldugu ilk an
        self.active = {}      # kod -> arizanin basladigi an
        self.last_seen = {}   # kod -> kosulun en son dogru oldugu an
        self.events = []      # biten arizalar: (kod, baslangic, bitis)

    def update(self, now, conditions):
        """conditions: su an dogru olan ariza kodlari.
        Donus: (yeni baslayan kodlar, biten olaylar [(kod, baslangic, bitis)])."""
        started, ended = [], []
        for code in conditions:
            self.last_seen[code] = now
            if code in self.active:
                continue
            t0 = self.pending.setdefault(code, now)
            if now - t0 >= self.confirm_by_code.get(code, self.confirm):
                self.active[code] = t0
                del self.pending[code]
                started.append(code)
        for code in [c for c in self.pending if c not in conditions]:
            del self.pending[code]          # kesildi: onay sayaci sifirdan
        for code in [c for c in self.active if c not in conditions]:
            if now - self.last_seen[code] >= self.clear:
                event = (code, self.active.pop(code), now)
                self.events.append(event)
                ended.append(event)
        return started, ended


def fault_conditions(k, fan, k_ref=1.0, active=None):
    """Kayma alarmi (KAYIP_*): ogrenilen k referanstan (k_ref = son kabul edilen seviye) bandin disina cikti mi.
    active: histerezisli durum (Twin.k_alarm); verilmezse yalniz K_ALARM_BAND. Hizli arizalar (GUC_*) energy_conditions'ta."""
    conds = set()
    on = abs(k - k_ref) > K_ALARM_BAND if active is None else active
    if on and k > k_ref:
        conds.add("KAYIP_ARTTI")
    elif on:
        conds.add("KAYIP_AZALDI_FAN" if fan > 0 else "KAYIP_AZALDI")
    return conds


class EnergyMonitor:
    """Eksik guc (W): son ENERGY_WINDOW saniyede modelin bekledigi kayip - (olculen guc - depolanan).
    + = beklenenden AZ isi gidiyor, - = FAZLA. Olculmeyen T2 (nozzle tarafi) ve T3 (kartus),
    GERCEK T1 ve olculen guc ile surulur (her aralikta T1 sabit sayilip tam ustel cozum);
    depolanan enerji uc dugumun toplami. Ikizin kendi sicaklik durumundan bagimsizdir; kayiplar
    ogrenilmis k ve k_fil ile olceklenir (modelle ayni "dara")."""

    def __init__(self):
        self.T2 = self.T3 = None
        self.reset()

    def reset(self):
        """Veri boslugu: pencere bosaltilir, T2/T3 bir sonraki olcumde yeniden kurulur."""
        self.T2 = self.T3 = None
        self.hist = deque()        # (aralik_sonu, dt, kayip*dt, guc*dt, E_aralik_basi, T1)
        self.pwm_hist = deque()    # (aralik_sonu, dt, pwm) - isinma (doygunluk) tespiti
        self.last_sat = -1e9
        self.last_target = -1e9    # Klipper hedef sicakligini en son degistirdigi an
        self.gap = None

    def _energy(self, T1):
        return C1 * cap_energy(T1) + C2 * cap_energy(self.T2) + C3 * cap_energy(self.T3)

    def update(self, now, dt, T1_prev, T1_now, pwm, fan, ext, power_scale=1.0, k=1.0, kfil=1.0):
        """Gecen aralik: T1_prev araligin basindaki bilinen sicaklik, pwm/fan/ext araligin girdileri.
        power_scale: tabla voltaj dusumu (gercek guc = pwm * P_MAX * power_scale); doygunluk
        tespiti komut edilen pwm ile yapilir. k, kfil: ogrenilmis carpanlar (model_step ile ayni sekilde:
        k blok + fan kaybini, kfil baskiya giden isiyi olcekler; heatsink yolu HA2 olceklenmez)."""
        if self.T2 is None:
            _, self.T2, self.T3 = initial_state(T1_prev, pwm, fan)
        k_eff = k
        E0 = self._energy(T1_prev)
        fc = k_eff * fan_conductance(fan)
        P = pwm * P_MAX * power_scale
        loss = (k_eff * natural_loss(T1_prev) + (HA2 + fc) * (self.T2 - T_AMB)
                + kfil * K_FIL * max(ext, 0.0) * (T1_prev - T_AMB))
        G2 = 1 / R_HB + HA2 + fc
        T2_inf = (T1_prev / R_HB + (HA2 + fc) * T_AMB) / G2
        self.T2 = T2_inf + (self.T2 - T2_inf) * math.exp(-dt * G2 / (C2 * cap_scale(self.T2)))
        T3_inf = T1_prev + P * R31
        self.T3 = T3_inf + (self.T3 - T3_inf) * math.exp(-dt / (R31 * C3 * cap_scale(self.T3)))

        self.hist.append((now, dt, loss * dt, P * dt, E0, T1_now))
        while len(self.hist) > 1 and self.hist[1][0] - self.hist[1][1] <= now - ENERGY_WINDOW:
            self.hist.popleft()          # pencere en az ENERGY_WINDOW kalacak sekilde kirp
        self.pwm_hist.append((now, dt, pwm))
        while self.pwm_hist and self.pwm_hist[0][0] <= now - 3.0:
            self.pwm_hist.popleft()
        span_p = sum(h[1] for h in self.pwm_hist)
        if span_p > 0 and sum(h[1] * h[2] for h in self.pwm_hist) / span_p >= 0.95:
            self.last_sat = now

        span = sum(h[1] for h in self.hist)
        Ts = [h[5] for h in self.hist]
        ok = (span >= 0.8 * ENERGY_WINDOW and T1_now > ENERGY_TMIN
              and max(Ts) - min(Ts) < ENERGY_T_RANGE and now - self.last_sat >= ENERGY_SETTLE
              and now - self.last_target >= ENERGY_SETTLE)
        # hedef degisimi: isitici hemen kapanir/acilir ama sicaklik gecikmeyle izler; o arada guc
        # dengesi "isi gitmiyor" gibi gorunuyordu (27.09 baski basi, yanlis GUC_AZALDI)
        if not ok:
            self.gap = None
            return None
        stored = (self._energy(T1_now) - self.hist[0][4]) / span
        self.gap = (sum(h[2] for h in self.hist) - sum(h[3] for h in self.hist)) / span + stored
        return self.gap


def energy_conditions(gap):
    if gap is None:
        return set()
    if gap > ENERGY_THRESHOLD:
        return {"GUC_AZALDI"}
    if gap < -ENERGY_THRESHOLD:
        return {"GUC_ARTTI"}
    return set()


class Twin:
    """Mesaj isleyici - canli script ve degerlendir.py AYNI kodu kullanir (birebir ayni sonuc).

    Her Moonraker mesajinda (icinde sicaklik olsun olmasin) on_message() cagrilir:
      1) gecen aralik, araligin BASINDA gecerli olan girdilerle (pwm/fan/ekstruzyon) ilerletilir
         (sifirinci dereceden tutma: yeni gelen deger bu andan SONRASI icin gecerli),
      2) mesajdaki yeni girdiler kaydedilir,
      3) mesajda yeni sicaklik varsa residual hesaplanir,
      4) ariza takibi: GUC_* (eksik guc) ve KAYIP_* (k kaymasi).
    Eskiden model sadece sicaklik iceren mesajlarda ilerliyordu: sicakliksiz bir mesajla gelen
    'isitici acildi' bilgisi o yuzden tum araliga geriye dogru uygulaniyor, isinma basinda
    model ~8C one geciyordu (benchmark 2026-09-25 18:48 logu)."""

    def __init__(self, T1, T2, T3, pwm, fan, ext, k=1.0, kfil=1.0, k_ref=None):
        """k, kfil: baslangic kayip ve filaman carpani (canli scriptte onceki oturumlardan, bkz. load_k).
        k_ref: kayip alarminin referansi (bkz. load_k_ref, K_ACCEPT_TIME); verilmezse k."""
        self.T1_model, self.T2_model, self.T3_model = T1, T2, T3
        self.pwm, self.fan = pwm or 0.0, fan or 0.0
        self.ext = ext if ext is not None and abs(ext) > EXT_EPS else 0.0
        self.flow = self.ext               # modele giden akis hizi (pos_e farkindan; yoksa anlik hiz)
        self.last_e = None                 # (zaman, pos_e) - son konum mesaji
        self.T1_real = T1                  # son bilinen gercek sicaklik
        self.prev_time = None
        self.ext_history = deque()         # (aralik_sonu, aralik_suresi, hiz) - net akis ortalamasi
        self.learner = KLossLearner(k, kfil)
        self.k_ref = k if k_ref is None else k_ref   # kayip alarminin referansi, bkz. K_ACCEPT_TIME
        self.kst_lo = self.kst_hi = k               # kabul: k'nin son sabit penceredeki araligi
        self.kst_t = 0.0                            # bu pencerede gecen sicak sure (s)
        self.kout_t = 0.0                           # kayip alarminin acik kaldigi sicak sure (s)
        self.k_alarm = False                        # kayip alarmi durumu (histerezisli, bkz. K_ALARM_CLEAR)
        self.faults = FaultMonitor(confirm_by_code={c: ENERGY_CONFIRM for c in ENERGY_TEXT})
        self.energy = EnergyMonitor()      # ikizin hesabina dokunmaz
        self.energy_error = None
        self.target = None                 # Klipper hedef sicakligi (sadece kayit + eksik guc bekleme suresi)
        self.bed = 0.0                     # tabla isitici pwm (0-1); bilinmiyorsa kapali sayilir
        self.material = None               # baskinin malzemesi (G-code basligi), bkz. set_material
        self.pause_flagged = False         # bu GUC_AZALDI olayi icin duraklatma zaten istendi mi (PAUSE_AFTER)

    def set_material(self, name, kfil=None):
        """Baskinin malzemesi degisti: k_fil o malzemenin kayitli degerinden (kfil; None = kayit yok, mevcut deger
        kalir) ve oturum basi belirsizligiyle (P0_FIL) yeniden ogrenilir. k ortak kalir (hotend ozelligi)."""
        if kfil is not None:
            self.learner.kfil = min(K_FIL_MAX, max(K_FIL_MIN, kfil))
        P = self.learner.P
        P[1][1], P[0][1], P[1][0] = P0_FIL, 0.0, 0.0
        self.material = name

    def on_message(self, now, T1_real=None, pwm=None, fan=None, ext=None, target=None, bed=None, pos_e=None):
        """Bir mesaji isle. Mesajda olmayan alanlar None verilir.
        pos_e: ekstruder konumu (mm, son bilinen deger); varsa akis bundan hesaplanir (bkz. POS_E_MAX_RATE).
        Donus: dt, yeni_sicaklik, residual (sicaklik yoksa None), anomali (resmi ariza aktif mi),
        k_anomali (ogrenilen kayip carpani normal bandin disinda), senkron, eksik_guc (W, gecersizse None),
        ariza_basladi (yeni onaylanan ariza kodlari), ariza_bitti ([(kod, baslangic, bitis)])."""
        out = {"dt": 0.0, "yeni_sicaklik": T1_real is not None,
               "residual": None, "anomali": False, "k_anomali": False, "senkron": False,
               "ariza_basladi": [], "ariza_bitti": [], "eksik_guc": None,
               "k_kabul": None,   # (eski referans, yeni k): kalici degisim kabul edildi (bkz. K_ACCEPT_TIME)
               "duraklat": False}  # GUC_AZALDI PAUSE_AFTER s'dir kesintisiz (olay basina bir kez)

        # 1) gecen araligi, araligin basindaki girdilerle ilerlet
        if self.prev_time is not None:
            dt = now - self.prev_time
            out["dt"] = dt
            # net filaman akisi: son EXT_AVG_WINDOW saniyedeki isaretli hizin zaman agirlikli
            # ortalamasi (geri cekme / geri itme birbirini goturur), sonra 0'in alti kirpilir
            self.ext_history.append((now, dt, self.flow))
            while self.ext_history and self.ext_history[0][0] < now - EXT_AVG_WINDOW:
                self.ext_history.popleft()
            span = sum(h[1] for h in self.ext_history)
            ext_net = sum(h[1] * h[2] for h in self.ext_history) / span if span > 0 else self.flow
            ext_smoothed = max(ext_net, 0.0)
            T_now = T1_real if T1_real is not None else self.T1_real
            if dt > GAP_THRESHOLD or dt < 0:
                # dt < 0: saat geri gitti (yeniden baglanirken Klipper/Pi yeniden baslamis olabilir;
                # negatif adim modeli patlatiyordu). Ikisinde de arada ne oldugu bilinmiyor.
                # uzun veri boslugu: arada ne oldugu bilinmiyor -> yeniden baslar gibi TUM
                # dugumleri tutarli kur (sadece T1'i esitlemek T2/T3'u eski degerde birakip
                # hemen ardindan sahte bir gecis uretiyordu)
                self.T1_model, self.T2_model, self.T3_model = initial_state(
                    T_now, pwm if pwm is not None else self.pwm,
                    fan if fan is not None else self.fan)
                self.learner.reset()
                if self.energy is not None:
                    self.energy.reset()
                out["senkron"] = True
            else:
                # tabla acikken besleme voltaji duser: kartusa giden gercek guc biraz azalir
                scale = 1.0 - BED_SAG * self.bed
                # baskiya giden isi: fan sogutmuyorken yalniz filaman (1.0), fan acikken ogrenilen k_fil
                kfil_now = self.learner.kfil if fan_conductance(self.fan) > 0 else 1.0
                self.T1_model, self.T2_model, self.T3_model = model_step(
                    self.T1_model, self.T2_model, self.T3_model,
                    self.pwm * scale, self.fan, ext_smoothed, dt, self.learner.k, k_fil=kfil_now)
                self.learner.update(now, dt, T_now, self.pwm * scale, self.fan, ext_smoothed > 0.0)
                if T_now > ADAPT_TMIN:      # kabul sayaclari yalniz k'nin anlamli oldugu sicak calismada ilerler
                    k = self.learner.k
                    lo, hi = min(self.kst_lo, k), max(self.kst_hi, k)
                    if hi - lo > K_ACCEPT_RANGE:            # k hala degisiyor: sabitlik penceresi yeniden
                        self.kst_lo = self.kst_hi = k
                        self.kst_t = 0.0
                    else:
                        self.kst_lo, self.kst_hi = lo, hi
                        self.kst_t += dt
                    self.kout_t = self.kout_t + dt if self.k_alarm else 0.0
                    if self.kout_t >= K_ACCEPT_TIME and self.kst_t >= K_ACCEPT_TIME:
                        out["k_kabul"] = (self.k_ref, k)
                        self.k_ref = k
                        self.kout_t = 0.0
                        self.k_alarm = False
                if self.energy is not None:
                    # hata verirse sadece eksik guc kapanir, ikiz ve izleme devam eder
                    try:
                        out["eksik_guc"] = self.energy.update(now, dt, self.T1_real, T_now,
                                                              self.pwm, self.fan, ext_smoothed, scale,
                                                              self.learner.k, kfil_now)
                    except Exception as e:  # noqa: BLE001
                        self.energy, self.energy_error = None, f"{type(e).__name__}: {e}"
                if self.energy is not None:
                    g = out["eksik_guc"]
                    guc = [c for c in ENERGY_TEXT if c in self.faults.active or c in self.faults.pending]
                    blocked = (g is not None and abs(g) > ENERGY_THRESHOLD) or bool(guc)
                    self.learner.update_print(now, dt, T_now, self.pwm * scale, self.fan, ext_smoothed,
                                              g is not None, blocked)
        self.prev_time = now

        # 2) mesajdaki yeni girdiler bundan sonrasi icin gecerli
        if target is not None:
            # hedef sicaklik modele girmez (model olculen gucle calisir); sadece erken uyari
            # hedef degistikten sonra ENERGY_SETTLE saniye beklesin diye izlenir
            if self.target is not None and abs(target - self.target) > 0.5 and self.energy is not None:
                self.energy.last_target = now
            self.target = target
        if bed is not None:
            self.bed = bed
        if pwm is not None:
            self.pwm = pwm
        if fan is not None:
            self.fan = fan
        if ext is not None:
            self.ext = ext if abs(ext) > EXT_EPS else 0.0
        # akis: son iki mesaj arasindaki pos_e farki bir sonraki araliga uygulanir (~0.25 s gecikme, nedensel)
        if pos_e is not None:
            if self.last_e is not None:
                dt_e = now - self.last_e[0]
                v = (pos_e - self.last_e[1]) / dt_e if 1e-3 < dt_e <= GAP_THRESHOLD else 0.0
                self.flow = v if abs(v) <= POS_E_MAX_RATE else 0.0
            self.last_e = (now, pos_e)
        elif self.last_e is None:
            self.flow = self.ext

        # 3) yeni sicaklik geldiyse karsilastir
        if T1_real is not None:
            self.T1_real = T1_real
            out["residual"] = 0.0 if out["senkron"] else T1_real - self.T1_model
        dev = abs(self.learner.k - self.k_ref)
        self.k_alarm = dev > (K_ALARM_CLEAR if self.k_alarm else K_ALARM_BAND)   # histerezis: bant kenarinda ac-kapa yok
        out["k_anomali"] = self.k_alarm

        # 4) ariza takibi - sadece yeni sicaklik gelen mesajlarda (kosullar olcume dayali)
        if T1_real is not None:
            conds = fault_conditions(self.learner.k, self.fan, self.k_ref, self.k_alarm)
            if not out["senkron"]:
                conds |= energy_conditions(out["eksik_guc"])
            out["ariza_basladi"], out["ariza_bitti"] = self.faults.update(now, conds)
            t_az = self.faults.active.get("GUC_AZALDI")
            if t_az is None:
                self.pause_flagged = False
            elif not self.pause_flagged and now - t_az >= PAUSE_AFTER:
                self.pause_flagged = out["duraklat"] = True
        out["anomali"] = bool(self.faults.active)   # CSV 'anomali' = resmi alarm aktif (Unity durum hapi)
        return out


LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "twin.lock")


def single_instance_lock():
    """Ayni anda tek ikiz: isletim sistemi dosya kilidi. Ikinci kopya kilidi alamaz -> None.
    Surec nasil biterse bitsin (Ctrl+C, pencereyi kapatmak, cokme) kilidi isletim sistemi birakir.
    (2026-09-28: eski ve yeni script ayni anda calisip ayni CSV'ye yazmisti.)"""
    fh = open(LOCK_FILE, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def classify_job(rows, t_confirm, codes, delay):
    """Arka plan is parcacigi: son satirlardan arizayi siniflandirir. Donus: konsol satiri.
    Tam rapor REPORT_FILE'a eklenir. Hata olursa canli izleme etkilenmez, sadece yazilir."""
    try:
        import sys
        import ariza_siniflandir   # gec import: pandas/scipy sadece ariza olunca yuklenir
        summary, report = ariza_siniflandir.classify_live(rows, sys.modules[__name__], t_confirm)
    except Exception as e:  # noqa: BLE001 - siniflandirma yan ozellik, izlemeyi durdurmamali
        return f"[ariza siniflandirma +{delay:.0f} s ({codes}) basarisiz: {type(e).__name__}: {e}]"
    with open(REPORT_FILE, "a", encoding="utf-8") as rf:
        rf.write(f"\n##### {time.strftime('%Y-%m-%d %H:%M:%S')}  {codes}  (onaydan +{delay:.0f} s)")
        rf.write(report + "\n")
    return f"\n>>>>> ARIZA KATEGORISI ({codes}, onaydan +{delay:.0f} s): {summary}\n      (ayrinti: {REPORT_FILE})\n"


def _download(url, path):
    import shutil
    import urllib.request
    tmp = path + ".part"
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as r, open(tmp, "wb") as out:
        shutil.copyfileobj(r, out)
    os.replace(tmp, path)   # yarim inmis dosya asla gercek adla kalmaz


def update_zoff(zoff, status):
    """Z ofseti kaydi (mesajda gelen alanlarla). z: babystep / SET_GCODE_OFFSET Z (mm, + = nozzle tabladan uzak);
    pos_z (toolhead konumu) bunu zaten icerir. endstop: kayitli stepper_z position_endstop (Z_OFFSET_APPLY_ENDSTOP +
    SAVE_CONFIG ile degisir; pos_z'de GORUNMEZ, + = nozzle tablaya yakin)."""
    ho = status.get("gcode_move", {}).get("homing_origin")
    if isinstance(ho, (list, tuple)) and len(ho) >= 3:
        zoff["z"] = ho[2]
    pe = status.get("configfile", {}).get("settings", {}).get("stepper_z", {}).get("position_endstop")
    if pe is not None:
        zoff["endstop"] = pe


def request_pause():
    """Arka plan: Moonraker'a baskiyi duraklat istegi (PAUSE_MODE == 'aktif'). Donus: konsol satiri."""
    import urllib.request
    try:
        req = urllib.request.Request(f"{HTTP_URL}/printer/print/pause", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as r:
            return f"[duraklatma istegi gonderildi: HTTP {r.status}]"
    except Exception as e:  # noqa: BLE001
        return f"[duraklatma istegi BASARISIZ: {type(e).__name__}: {e}]"


def gcode_material(path):
    """Orca/Prusa G-code'unun ayar blogundaki '; filament_type = X' (blok dosyanin sonunda; yoksa None)."""
    found = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("; filament_type ="):
                found = line.split("=", 1)[1].strip().split(";")[0].strip() or None
    return found


def plan_job(filename, t0, init):
    """Arka plan is parcacigi: baski basinda G-code + printer.cfg indirir, ikizin planini uretir.
    init: baskinin basladigi andaki olculen durum (simulasyonun baslangic kosulu).
    Donus: (konsol satiri, malzeme ya da None). Hata olursa canli izleme etkilenmez, sadece yazilir."""
    material = None
    try:
        import urllib.parse
        os.makedirs(GCODE_DIR, exist_ok=True)
        base = os.path.basename(filename)
        gpath = os.path.join(GCODE_DIR, base)
        _download(f"{HTTP_URL}/server/files/gcodes/{urllib.parse.quote(filename)}", gpath)
        material = gcode_material(gpath)
        import gcode_tahmin   # gec import: pandas sadece baski baslayinca yuklenir
        cfg, cfg_src = os.path.join(GCODE_DIR, "printer.cfg"), "Pi"
        try:
            _download(f"{HTTP_URL}/server/files/config/printer.cfg", cfg)
        except OSError:
            cfg, cfg_src = CFG_FALLBACK, "yerel kopya"
        stamp = time.strftime("%Y%m%d_%H%M", time.localtime(t0))
        out = os.path.join(PLAN_DIR, f"tahmin_canli_{stamp}_{os.path.splitext(base)[0]}.csv")
        summary = gcode_tahmin.plan_for_live(gpath, cfg, t0, out, **init)
    except Exception as e:  # noqa: BLE001 - plan yan ozellik, izlemeyi durdurmamali
        return f"[G-code plani uretilemedi: {type(e).__name__}: {e} - izleme devam ediyor]", material
    return f"[G-code plani hazir: {summary}; printer.cfg: {cfg_src}]", material


def show_category(text, live):
    """Siniflandirma sonucu: konsola yaz, telefon sayfasina kisa halini koy."""
    print(text)
    first = next((ln for ln in text.splitlines() if "KATEGORISI" in ln or "basarisiz" in ln), "").strip()
    live["kategori"] = first.split("): ", 1)[-1] if "): " in first else first


WEB_PAGE = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Hotend ikizi</title>
<style>
:root{--bg:#fafaf8;--card:#fff;--tx:#1b1b1a;--mu:#6d6c67;--gr:#e3e2dc;--ok:#1f8a4c;--wa:#b86e00;--er:#c23b3b;--ln1:#2a78d6;--ln2:#eb6834}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--card:#201f1e;--tx:#efeee9;--mu:#9b9a93;--gr:#34332f;--ln1:#5aa0f0;--ln2:#f08050}}
body{margin:0;padding:12px 14px;background:var(--bg);color:var(--tx);font:15px/1.4 system-ui,sans-serif}
.g{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.c{background:var(--card);border-radius:10px;padding:10px 12px}
.l{font-size:12px;color:var(--mu)}.v{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
#st{margin:10px 0;padding:10px 12px;border-radius:10px;font-weight:600}canvas{width:100%;height:190px;display:block}
.s{font-size:12px;color:var(--mu);margin-top:8px}</style></head><body>
<div id="st">baglaniyor...</div>
<div class="g"><div class="c"><div class="l">gercek T1</div><div class="v" id="T">-</div></div>
<div class="c"><div class="l">model T1</div><div class="v" id="M">-</div></div>
<div class="c"><div class="l">residual</div><div class="v" id="R">-</div></div>
<div class="c"><div class="l">eksik guc</div><div class="v" id="G">-</div></div>
<div class="c"><div class="l">isitici / fan</div><div class="v" id="P" style="font-size:18px">-</div></div>
<div class="c"><div class="l">k / akis</div><div class="v" id="K" style="font-size:18px">-</div></div>
<div class="c" style="grid-column:1/-1"><div class="l">tabla (sicaklik / hedef, guc)</div><div class="v" id="B" style="font-size:18px">-</div></div></div>
<div class="c" style="margin-top:8px"><div class="l">residual (C, mavi) ve eksik guc x4 (W, turuncu) - son 15 dk</div><canvas id="ch"></canvas></div>
<div class="s" id="kat"></div><div class="s" id="up"></div>
<script>
const $=i=>document.getElementById(i),css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
function f(x,d,u){return x==null?'-':(x>0&&u!=='C '?'':'')+x.toFixed(d)+u}
function draw(h){const c=$('ch'),w=c.clientWidth,H=190,r=devicePixelRatio||1;c.width=w*r;c.height=H*r;const x=c.getContext('2d');x.scale(r,r);
if(h.length<2)return;const t0=h[0][0],t1=h[h.length-1][0],Y=v=>H/2-v*(H/2-8)/15,X=t=>(t-t0)/(t1-t0||1)*(w-4)+2;
x.strokeStyle=css('--gr');x.lineWidth=1;for(const v of[-12,-5,0,5,12]){x.beginPath();x.moveTo(0,Y(v));x.lineTo(w,Y(v));x.stroke();x.fillStyle=css('--mu');x.font='10px system-ui';x.fillText(v,2,Y(v)-2)}
for(const[k,col,s]of[[1,'--ln1',1],[2,'--ln2',4]]){x.strokeStyle=css(col);x.lineWidth=2;x.beginPath();let m=0;
for(const p of h){if(p[k]==null){m=0;continue}const yy=Y(Math.max(-15,Math.min(15,p[k]*s)));m?x.lineTo(X(p[0]),yy):x.moveTo(X(p[0]),yy);m=1}x.stroke()}}
async function tick(){try{const d=await(await fetch('/durum',{cache:'no-store'})).json();
$('T').textContent=f(d.T,1,' C');$('M').textContent=f(d.M,1,' C');$('R').textContent=(d.R>0?'+':'')+f(d.R,1,' C');
$('G').textContent=d.G==null?'-':(d.G>0?'+':'')+d.G.toFixed(2)+' W';$('P').textContent=Math.round(d.pwm*100)+'% / '+Math.round(d.fan*100)+'%';
$('K').textContent=d.k.toFixed(3)+' / '+d.ext.toFixed(1);
$('B').textContent=d.bT==null?'-':d.bT.toFixed(1)+' / '+Math.round(d.bTg||0)+' C,  %'+Math.round(100*(d.bP||0))
 +(d.bM==null?'':'  | model '+d.bM.toFixed(1)+' ('+(d.bT-d.bM>0?'+':'')+(d.bT-d.bM).toFixed(1)+'), cam ~'+d.bC.toFixed(0)+' C');
const st=$('st');let msg='normal',col='--ok';
if(d.ariza.length){msg='ARIZA: '+d.ariza.join(', ');col='--er'}
if(d.yas>15){msg='veri gelmiyor ('+Math.round(d.yas)+' s)';col='--wa'}
st.textContent=msg;st.style.background=css(col);st.style.color='#fff';
$('kat').textContent=(d.bakim?d.bakim+'  ':'')+(d.kategori?('son kategori: '+d.kategori):'');$('up').textContent='son veri '+Math.round(d.yas)+' s once';draw(d.h)}
catch(e){$('st').textContent='script calismiyor / baglanti yok';$('st').style.background=css('--er')}}
tick();setInterval(tick,2000);
</script></body></html>"""


async def web_server(live):
    """Telefondan izleme sayfasi. live: main'in guncelledigi durum sozlugu (sadece okunur)."""
    async def handle(reader, writer):
        try:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode(errors="ignore")
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass
            path = line.split(" ")[1] if len(line.split(" ")) > 1 else "/"
            if path.startswith("/durum"):
                snap = dict(live)
                snap["h"] = list(live["h"])
                snap["yas"] = time.time() - live["wall"] if live["wall"] else 1e9
                body, ctype = json.dumps(snap, default=float).encode(), "application/json"
            else:
                body, ctype = WEB_PAGE.encode(), "text/html; charset=utf-8"
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                         f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except Exception:  # noqa: BLE001 - sayfa hatasi izlemeyi etkilemez
            pass
        finally:
            writer.close()

    try:
        server = await asyncio.start_server(handle, "0.0.0.0", WEB_PORT)
    except OSError as e:
        print(f"[telefon sayfasi acilamadi ({e}) - izleme sayfasiz devam ediyor]")
        return None
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((PRINTER_HOST, 80))        # paket gondermez; sadece hangi ag arayuzunun kullanildigini bulur
        ip = s.getsockname()[0]; s.close()
    except OSError:
        ip = "<bilgisayar-IP>"
    print(f"[telefondan izleme: http://{ip}:{WEB_PORT}  (ayni Wi-Fi/ag)]")
    return server


async def main():
    if not PRINTER_HOST:
        print("\n!! Printer address not set.\n"
              "   Copy config.example.toml to config.toml and set printer_host to your\n"
              "   Moonraker address (IP or hostname), then start again.\n")
        return
    lock = single_instance_lock()
    if lock is None:
        print("\n!! Baska bir ikiz zaten calisiyor (ayni CSV'ye iki kopya yazardi).\n"
              "   Once onu kapat (o pencerede Ctrl+C), sonra tekrar baslat.\n")
        return
    twin = None   # ilk gercek sicaklik bilinince kurulur
    live = {"T": None, "M": None, "R": None, "G": None, "pwm": 0.0, "fan": 0.0, "k": 1.0, "ext": 0.0,
            "ariza": [], "kategori": "", "wall": 0.0, "h": deque(),
            "bT": None, "bTg": None, "bP": None, "bM": None, "bC": None}   # telefon sayfasi
    web = await web_server(live) if WEB_PORT else None
    buffer = deque()      # (model zamani, csv satiri) - siniflandirma icin son CLASSIFY_BUFFER saniye
    scheduled = []        # [calisma zamani, onay zamani, kodlar, gecikme]
    loop = asyncio.get_running_loop()
    last_known = {"pwm": 0.0, "fan": 0.0, "ext": 0.0}   # twin kurulmadan once gelen girdiler
    bed = {}                                             # tabla: son bilinen sicaklik/guc/hedef (kayit)
    motion = {}                                          # kafa hizi, Y ve Z konumu (kayit)
    pstat = {}                                           # print_stats: baski durumu ve dosya (girdi)
    gmove = {}                                           # gcode_move: ekstruzyon modu (planin girdisi)
    zoff = {}                                            # Z ofseti: babystep + kayitli endstop (kayit)
    bed_twin = None                                     # tabla ikizi (tabla_model.BedTwin, gozlem)
    bed_error = None
    backup_state()                                       # acilista twin_state.json yedegi (son STATE_BACKUPS)
    mat_last, mats = load_materials()                    # malzeme basina k_fil (bkz. set_material)
    advice = maint_check()                               # bakim gostergesi (tavsiye, bkz. MAINT_*)
    live["bakim"] = advice or ""
    if advice:
        print(f"\n!! {advice}\n")
        try:
            with open(REPORT_FILE, "a", encoding="utf-8") as rf:
                rf.write(f"\n##### {time.strftime('%Y-%m-%d %H:%M:%S')}  BAKIM  {advice}\n")
        except OSError:
            pass
    else:
        print(f"[bakim gostergesi: oneri yok (fansiz baski k, temiz referans {MAINT_REF:.2f} +-{MAINT_BAND:.2f})]")

    def on_plan(fut):
        """Plan isi bitti (olay dongusunde calisir): konsol satiri + baskinin malzemesi degistiyse k_fil degisimi."""
        nonlocal mat_last
        msg, mat = fut.result()
        print(msg)
        if not mat:
            return
        if twin is None:
            mat_last = mat
            return
        if mat == twin.material:
            print(f"[malzeme: {mat} (ayni) - k_fil {twin.learner.kfil:.2f} ile devam]")
            return
        if twin.material:
            mats[twin.material] = twin.learner.kfil
        new = mats.get(mat)
        twin.set_material(mat, new)
        print(f"[malzeme: {twin.material} -> k_fil "
              + (f"kayitli {new:.2f}" if new is not None else f"kaydi yok, {twin.learner.kfil:.2f} ile")
              + " (yeniden ogreniliyor)]")

    # onceki oturumun logu yeniden adlandirilmadan kaldiysa ustune yazma: tarihli ada tasi
    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 0:
        stamp = time.strftime("%Y%m%d_%H%M", time.localtime(os.path.getmtime(LOG_FILE)))
        old = os.path.join(os.path.dirname(LOG_FILE), f"realtime_log_ws_onceki_{stamp}.csv")
        try:
            os.replace(LOG_FILE, old)
            print(f"[onceki log korundu: {os.path.basename(old)}]")
        except OSError as e:
            print(f"[onceki log tasinamadi ({e}); ustune yazilacak]")

    with open(LOG_FILE, "w", newline="") as f:
        # Her mesaj bir satir. T_yeni=1: mesajda yeni sicaklik var (residual bu satirlarda).
        # T_yeni=0: sadece pwm/fan degisti; T1_real son bilinen deger, residual bos.
        # anomali = resmi alarm aktif (2026-10-12'den itibaren; oncesinde ham residual esigiydi).
        # Tum mesajlar kayitli oldugu icin degerlendir.py canli calismayi birebir tekrarlar.
        writer = csv.DictWriter(f, fieldnames=[
            "time", "T1_real", "T1_model", "residual", "pwm",
            "fan_state", "extrude_velocity", "anomali", "k_loss", "eventtime", "T_yeni", "k_anomali",
            "ariza", "eksik_guc", "target", "bed_T", "bed_pwm", "bed_target",
            "head_v", "pos_x", "pos_y", "pos_z", "pos_e", "bed_model", "bed_res", "bed_k", "k_fil",
            "print_state", "z_offset", "z_endstop", "malzeme", "duraklat"
        ])
        writer.writeheader()

        # Model zamani: Klipper'in eventtime'i (degerlerin OLCULDUGU an). Mesajin PC'ye
        # ulastigi an (time.time()) ag/Moonraker gecikmesiyle 0.03-0.56 s arasi oynuyor;
        # pwm*dt integrali bu titremeyle 245C'de +-8C sahte salinim uretiyordu.
        # Abonelik cevabinda eventtime olmayabiliyor; karar ilk bildirimde verilir ve
        # model saati o bildirimden baslar (iki saat asla karistirilmaz).
        use_klipper_clock = None
        last_save = last_flush = 0.0   # disk yazimlari PC saatine gore seyreltilir
        energy_error_shown = False
        try:
            while True:
                # Ag tikanirsa (ornegin ayni Wi-Fi'da video akisi) websocket kopabilir: script
                # kapanmaz, RECONNECT_DELAY sonra yeniden baglanir ve ayni CSV'ye devam eder.
                # Aradaki bosluk GAP_THRESHOLD'dan uzunsa ikiz gercek sicakliga yeniden oturur.
                try:
                    print("Websocket baglaniliyor...")
                    async with websockets.connect(WS_URL, ping_interval=PING_INTERVAL,
                                                  ping_timeout=PING_TIMEOUT) as ws:
                        print("Baglandi, abone olunuyor...")

                        subscribe_msg = {
                            "jsonrpc": "2.0",
                            "method": "printer.objects.subscribe",
                            "params": {
                                "objects": {
                                    "extruder": ["temperature", "power", "target"],
                                    "heater_bed": ["temperature", "power", "target"],
                                    "fan": ["speed"],
                                    "motion_report": ["live_extruder_velocity", "live_velocity", "live_position"],
                                    # baski durumu ve dosya: ikizin G-code planinin GIRDISI (plan Klipper'dan okunmaz)
                                    "print_stats": ["state", "filename"],
                                    # ekstruzyon modu (M82/M83): Klipper baskilar arasinda sifirlamaz, planin baslangic kosulu
                                    "gcode_move": ["absolute_extrude"],
                                    # Z ofseti (sadece kayit, 2026-10-14): ilk katmanin istedigi isi baskidan baskiya
                                    # 0.4-0.9 W oynuyor; ezme/nozzle-tabla araligi aday. homing_origin[2] = babystep
                                    # (SET_GCODE_OFFSET Z); position_endstop = kayitli Z sifiri (yalniz baglanirken gelir)
                                    "gcode_move": ["homing_origin"],
                                    "configfile": ["settings"],
                                }
                            },
                            "id": 1,
                        }
                        await ws.send(json.dumps(subscribe_msg))

                        # abonelik cevabi (id=1) o anki durumun tam anlik goruntusunu icerir. Baski sirasinda
                        # Moonraker'in bildirimleri cevaptan once gelebilir: cevabi gelene kadar onlari atla
                        # (2026-10-11: baski ortasinda yeniden baslatmada ilk mesaj bildirim cikti; hedef sicakliklar
                        # ve baski durumu sadece degisince geldigi icin oturum boyunca bos kaldi)
                        while True:
                            first_response = json.loads(await ws.recv())
                            if first_response.get("id") == 1:
                                break
                        # tabla: bildirimler sadece degisen alanlari getirir; baslangic degerleri buradan
                        for key, val in first_response.get("result", {}).get("status", {}).get("heater_bed", {}).items():
                            if val is not None:
                                bed[key] = val
                        # konum da ayni: yazici bostayken live_position degismedigi icin hic gelmez. Baslangic
                        # degeri buradan alinmazsa pos_* ilk harekete kadar bos kalir (2026-10-11: plan baslangicinda
                        # pos_e bilinmedigi icin Unity ilerlemeyi hesaplayamadi)
                        mr0 = first_response.get("result", {}).get("status", {}).get("motion_report", {})
                        if mr0.get("live_velocity") is not None:
                            motion["v"] = mr0["live_velocity"]
                        pos0 = mr0.get("live_position")
                        if isinstance(pos0, (list, tuple)) and len(pos0) >= 4:
                            motion["x"], motion["y"], motion["z"], motion["e"] = pos0[0], pos0[1], pos0[2], pos0[3]
                        # baski durumu: script (ya da yeniden baglanti) baski ortasinda basladiysa baslangic
                        # anini gormedik -> o baski icin plan uretilmez (plan baskinin basina hizalanir)
                        was = pstat.get("state")
                        for key, val in first_response.get("result", {}).get("status", {}).get("print_stats", {}).items():
                            if val is not None:
                                pstat[key] = val
                        for key, val in first_response.get("result", {}).get("status", {}).get("gcode_move", {}).items():
                            if val is not None:
                                gmove[key] = val
                        st1 = first_response.get("result", {}).get("status", {})
                        update_zoff(zoff, st1)
                        if pstat.get("state") in ("printing", "paused") and was not in ("printing", "paused"):
                            print(f"[baski zaten suruyor: {pstat.get('filename', '?')} - baslangicini "
                                  f"gormedigimiz icin bu baski icin G-code plani uretilmeyecek]")
                        print("Ilk cevap alindi, izleme basliyor. Durdurmak icin Ctrl+C.\n")

                        if twin is None:     # yeniden baglanmada ikiz korunur
                            try:
                                initial_status = first_response["result"]["status"]
                                ex0 = initial_status["extruder"]
                                pwm0 = ex0.get("power", 0.0) or 0.0
                                fan0 = initial_status.get("fan", {}).get("speed", 0.0) or 0.0
                                ext0 = initial_status.get("motion_report", {}).get("live_extruder_velocity", 0.0) or 0.0
                                k0, kf0 = load_k()
                                twin = Twin(*load_state(ex0["temperature"], pwm0, fan0), pwm0, fan0, ext0,
                                            k0, kf0, load_k_ref(k0))
                                twin.material = mat_last
                                # hedef degismedikce bildirimde gelmez: baslangic degeri buradan
                                twin.target = ex0.get("target")
                            except (KeyError, TypeError):
                                print("Uyari: ilk durum ayristirilamadi, ilk gercek veri mesajinda baslatilacak.")
                        else:
                            # yeniden baglanma: kopuklukta degismis olabilecek girdileri anlik durumdan
                            # tazele (Moonraker bildirimlerde sadece DEGISEN alanlari gonderir; fan
                            # kopuklukta degistiyse bir sonraki degisimine kadar eski deger kalirdi)
                            st0 = first_response.get("result", {}).get("status", {})
                            for key, val in (("pwm", st0.get("extruder", {}).get("power")),
                                             ("fan", st0.get("fan", {}).get("speed")),
                                             ("ext", st0.get("motion_report", {}).get("live_extruder_velocity")),
                                             ("target", st0.get("extruder", {}).get("target"))):
                                if val is not None:
                                    setattr(twin, key, val)

                        async for message in ws:
                            data = json.loads(message)

                            if data.get("method") != "notify_status_update":
                                continue

                            params = data["params"]
                            status = params[0]
                            wall_time = time.time()
                            event_time = params[1] if len(params) > 1 and isinstance(params[1], (int, float)) else None
                            if use_klipper_clock is None:
                                use_klipper_clock = event_time is not None
                                print("[Zaman kaynagi: " + ("Klipper eventtime]" if use_klipper_clock else
                                      f"PC saati - bildirimde eventtime yok, params={str(params)[:120]}]"))
                            if use_klipper_clock and event_time is None:
                                continue  # zaman damgasi yok - iki saati karistirmamak icin atla
                            now = event_time if use_klipper_clock else wall_time

                            # mesajda olmayan alan None kalir (Moonraker sadece degisenleri gonderir)
                            ex = status.get("extruder", {})
                            T1_real = ex.get("temperature")
                            pwm = ex.get("power")
                            target = ex.get("target")
                            # tabla: nozzle ikizine sadece guc olarak girer (besleme voltaji dususu);
                            # kendi ikizi (tabla_model.BedTwin) asagida, gozlem modunda
                            bed_msg = status.get("heater_bed", {})
                            for key, val in bed_msg.items():
                                if val is not None:
                                    bed[key] = val
                            fan = status.get("fan", {}).get("speed")
                            ext = status.get("motion_report", {}).get("live_extruder_velocity")
                            # kafa hizi ve konumu: sadece kayit (tabla Y ekseninde hareket ettikce tabla
                            # kaybi artiyor mu; Z ile nozzle-tabla yakinligi)
                            mr = status.get("motion_report", {})
                            if mr.get("live_velocity") is not None:
                                motion["v"] = mr["live_velocity"]
                            pos = mr.get("live_position")
                            if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                                motion["x"], motion["y"], motion["z"] = pos[0], pos[1], pos[2]
                                if len(pos) >= 4:
                                    # ekstruder toplam konumu (mm filaman): iki an arasindaki fark = gercek
                                    # net besleme; ornekleme/geri cekme kaybi yok (akis hesabi icin aday)
                                    motion["e"] = pos[3]

                            update_zoff(zoff, status)

                            for key, val in status.get("gcode_move", {}).items():
                                if val is not None:
                                    gmove[key] = val
                            # baski durumu (twin kurulmadan once de izlenir: baslangic ani kacmasin)
                            ps = status.get("print_stats", {})
                            if ps:
                                was = pstat.get("state")
                                for key, val in ps.items():
                                    if val is not None:
                                        pstat[key] = val
                                if pstat.get("state") != was:
                                    print(f"[baski durumu: {was} -> {pstat.get('state')}]")
                                    # pause'dan devam yeni baski degildir
                                    if pstat.get("state") == "printing" and was != "paused":
                                        print(f"[baski basladi: {pstat.get('filename', '?')} - G-code plani hazirlaniyor]")
                                        # baslangic kosulu: su an olculen durum (ikiz henuz kurulmadiysa son bilinen)
                                        th = twin.T1_real if twin is not None else T1_real
                                        init = {"T_h": th if th is not None else 25.0,
                                                "T_b": bed.get("temperature", 25.0),
                                                "pwm_h": twin.pwm if twin is not None else last_known["pwm"],
                                                "pwm_b": bed.get("power", 0.0) or 0.0,
                                                "tgt_h": (twin.target if twin is not None else None) or 0.0,
                                                "tgt_b": bed.get("target", 0.0) or 0.0,
                                                "fan": twin.fan if twin is not None else last_known["fan"],
                                                "abs_e": bool(gmove.get("absolute_extrude", True))}
                                        fut = loop.run_in_executor(None, plan_job, pstat.get("filename", ""),
                                                                   wall_time, init)
                                        fut.add_done_callback(on_plan)

                            if twin is None:
                                # ilk durum alinamamisti: girdileri biriktir, ilk sicaklikta modeli kur
                                for key, val in (("pwm", pwm), ("fan", fan), ("ext", ext)):
                                    if val is not None:
                                        last_known[key] = val
                                if T1_real is None:
                                    continue
                                k0, kf0 = load_k()
                                twin = Twin(*load_state(T1_real, last_known["pwm"], last_known["fan"]),
                                            last_known["pwm"], last_known["fan"], last_known["ext"],
                                            k0, kf0, load_k_ref(k0))
                                twin.material = mat_last

                            r = twin.on_message(now, T1_real, pwm, fan, ext, target, bed.get("power"), motion.get("e"))
                            if twin.learner.windows:     # bakim izleme: pencere k'leri (model saati -> duvar saati)
                                maint_record([(tn + wall_time - now, kind, kv) for tn, kind, kv in twin.learner.windows])
                                twin.learner.windows.clear()

                            # tabla ikizi (GOZLEM: alarm yok). Tabla sicakligi ve gucu ilk kez bilinince kurulur.
                            # Hata verirse sadece kendisi kapanir, nozzle ikizi ve izleme devam eder.
                            if bed_twin is None and bed_error is None and "temperature" in bed and "power" in bed:
                                bed_twin = tabla_model.BedTwin(bed["temperature"], bed["power"])
                            bed_r = None
                            if bed_twin is not None:
                                try:
                                    hv = motion.get("v")
                                    bed_r = bed_twin.on_message(now, bed_msg.get("temperature"), bed_msg.get("power"),
                                                                fan, hv, None if hv is not None else ext)
                                except Exception as e:  # noqa: BLE001
                                    bed_twin, bed_error = None, f"{type(e).__name__}: {e}"
                                    print(f"[tabla ikizi hata verdi ve kapandi: {bed_error} - izleme devam ediyor]")
                            if wall_time - last_save >= STATE_SAVE_INTERVAL:
                                save_state(twin.T1_model, twin.T2_model, twin.T3_model, twin.learner.k,
                                           twin.learner.kfil, twin.k_ref, twin.material, mats)
                                last_save = wall_time

                            for code in r["ariza_basladi"]:
                                g = r["eksik_guc"]
                                print(f"\n!!!!! ARIZA: {code} - {FAULT_TEXT[code]}"
                                      f"  (residual={r['residual']:+.1f}C, k={twin.learner.k:.3f}"
                                      + (f", eksik guc={g:+.1f} W" if g is not None else "") + ")\n")
                            if r["ariza_basladi"] and not any(abs(s[1] - now) < 60.0 for s in scheduled):
                                # yakin zamanda baslamis bir olay zaten siniflandirilacaksa tekrar planlanmaz
                                codes = ",".join(sorted(twin.faults.active))
                                scheduled += [[now + dl, now, codes, dl] for dl in CLASSIFY_DELAYS]
                                print(f"[ariza kategorisi {CLASSIFY_DELAYS[0]:.0f} s sonra hesaplanacak]")
                            for code, t0, t1 in r["ariza_bitti"]:
                                print(f"[ariza bitti: {code}, {t1 - t0:.0f} s surdu]")
                            if r["k_kabul"]:
                                k_old, k_new = r["k_kabul"]
                                msg = (f"KAYIP kabul edildi: k {k_old:.3f} -> {k_new:.3f} ({100 * (k_new / k_old - 1):+.0f}%), "
                                       f"{K_ACCEPT_TIME / 60:.0f} dk sabit kaldi; yeni referans bu seviye")
                                print(f"\n[{msg}]\n")
                                try:
                                    with open(REPORT_FILE, "a", encoding="utf-8") as rf:
                                        rf.write(f"\n##### {time.strftime('%Y-%m-%d %H:%M:%S')}  KAYIP_KABUL  {msg}\n")
                                except OSError:
                                    pass
                            if r["duraklat"] and PAUSE_MODE != "kapali" and pstat.get("state") == "printing":
                                msg = (f"DURAKLATMA: GUC_AZALDI {PAUSE_AFTER:.0f} s'dir kesintisiz "
                                       f"(eksik guc {r['eksik_guc'] if r['eksik_guc'] is not None else float('nan'):+.1f} W, "
                                       f"{pstat.get('filename', '?')}) - "
                                       + ("GOLGE MOD: baski duraklatilmadi, yalniz kayit" if PAUSE_MODE == "golge"
                                          else "baski duraklatiliyor"))
                                print(f"\n!!!!! {msg}\n")
                                try:
                                    with open(REPORT_FILE, "a", encoding="utf-8") as rf:
                                        rf.write(f"\n##### {time.strftime('%Y-%m-%d %H:%M:%S')}  DURAKLATMA  {msg}\n")
                                except OSError:
                                    pass
                                if PAUSE_MODE == "aktif":
                                    pf = loop.run_in_executor(None, request_pause)
                                    pf.add_done_callback(lambda f: print(f.result()))
                            if twin.energy_error and not energy_error_shown:
                                print(f"[enerji uyarisi hata verdi ve kapandi: {twin.energy_error} - izleme devam ediyor]")
                                energy_error_shown = True
                            if r["senkron"]:
                                print(f"[senkronize edildi - {r['dt']:.1f}s'lik veri boslugu]")
                                if r["dt"] < 0:
                                    # saat geri gitti: arabellekteki satirlar artik ayni zaman ekseninde degil
                                    buffer.clear()
                                    scheduled.clear()
                            if r["yeni_sicaklik"]:
                                etiket = "  !! ARIZA: " + ",".join(sorted(twin.faults.active)) if r["anomali"] else ""
                                if r["k_anomali"]:
                                    etiket += f"  !! KAYIP DEGISTI (k={twin.learner.k:.3f}, referans {twin.k_ref:.3f})"
                                print(f"T1_real={twin.T1_real:6.1f}C  "
                                      f"T1_model={twin.T1_model:6.1f}C  "
                                      f"residual={r['residual']:+6.1f}C  "
                                      f"dt={r['dt']:.3f}s  "
                                      f"pwm={twin.pwm:.2f}  fan={twin.fan:.2f}  "
                                      f"ext={twin.ext:5.2f}  k={twin.learner.k:.3f} kfil={twin.learner.kfil:.2f}"
                                      + (f"  tabla={bed['temperature']:5.1f}/{bed.get('target', 0):.0f}C "
                                         f"%{100 * bed.get('power', 0):.0f}"
                                         + (f" (model {bed_twin.T_model:5.1f}, res {bed.get('temperature', 0) - bed_twin.T_model:+4.1f})"
                                            if bed_twin is not None else "")
                                         if "temperature" in bed else "")
                                      + etiket)

                            row = {
                                "time": wall_time, "eventtime": now if use_klipper_clock else "",
                                "T_yeni": int(r["yeni_sicaklik"]),
                                "T1_real": twin.T1_real, "T1_model": twin.T1_model,
                                "residual": "" if r["residual"] is None else r["residual"],
                                "pwm": twin.pwm, "fan_state": twin.fan, "extrude_velocity": twin.ext,
                                "anomali": r["anomali"], "k_loss": twin.learner.k,
                                "k_anomali": r["k_anomali"], "ariza": ";".join(sorted(twin.faults.active)),
                                "eksik_guc": "" if r["eksik_guc"] is None else round(r["eksik_guc"], 3),
                                "target": "" if twin.target is None else twin.target,
                                "bed_T": bed.get("temperature", ""), "bed_pwm": bed.get("power", ""),
                                "bed_target": bed.get("target", ""),
                                "head_v": motion.get("v", ""), "pos_x": motion.get("x", ""),
                                "pos_y": motion.get("y", ""),
                                "pos_z": motion.get("z", ""), "pos_e": motion.get("e", ""),
                                "bed_model": "" if bed_twin is None else round(bed_twin.T_model, 3),
                                "bed_res": "" if not bed_r or bed_r["residual"] is None else round(bed_r["residual"], 3),
                                "bed_k": "" if bed_twin is None else round(bed_twin.learner.k, 4),
                                "k_fil": round(twin.learner.kfil, 4),
                                "print_state": pstat.get("state", ""),
                                "z_offset": zoff.get("z", ""), "z_endstop": zoff.get("endstop", ""),
                                "malzeme": twin.material or "",
                                "duraklat": PAUSE_MODE if r["duraklat"] and pstat.get("state") == "printing" else "",
                            }
                            writer.writerow(row)

                            # telefon sayfasi (sadece okunan kopya)
                            live.update(T=twin.T1_real, M=twin.T1_model, pwm=twin.pwm, fan=twin.fan,
                                        k=twin.learner.k, ext=twin.ext, G=r["eksik_guc"], wall=wall_time,
                                        ariza=sorted(twin.faults.active),
                                        bT=bed.get("temperature"), bTg=bed.get("target"), bP=bed.get("power"),
                                        bM=None if bed_twin is None else bed_twin.T_model,
                                        bC=None if bed_twin is None else bed_twin.T_cam)
                            if r["senkron"] and r["dt"] < 0:
                                live["h"].clear()
                            if r["residual"] is not None and not r["senkron"]:
                                live["R"] = r["residual"]
                                h = live["h"]
                                if not h or now - h[-1][0] >= 1.0:
                                    h.append([now, round(r["residual"], 2),
                                              None if r["eksik_guc"] is None else round(r["eksik_guc"], 3)])
                                    while h and h[0][0] < now - WEB_HISTORY:
                                        h.popleft()

                            # ariza siniflandirma: son satirlar bellekte, zamani gelen is arka planda calisir
                            buffer.append((now, row))
                            while buffer[0][0] < now - CLASSIFY_BUFFER:
                                buffer.popleft()
                            for job in [s for s in scheduled if now >= s[0]]:
                                scheduled.remove(job)
                                fut = loop.run_in_executor(None, classify_job, [b[1] for b in buffer], *job[1:])
                                fut.add_done_callback(lambda f: show_category(f.result(), live))
                            if wall_time - last_flush >= FLUSH_INTERVAL:
                                f.flush()
                                last_flush = wall_time
                    print("\n[baglanti sunucu tarafindan kapatildi]")
                except (websockets.exceptions.WebSocketException, OSError, asyncio.TimeoutError) as e:
                    print(f"\n[baglanti koptu: {type(e).__name__}: {e}]")
                f.flush()
                print(f"[{RECONNECT_DELAY:.0f} s sonra yeniden baglaniliyor - durdurmak icin Ctrl+C]\n")
                await asyncio.sleep(RECONNECT_DELAY)
        finally:
            if web is not None:
                web.close()
            # cikista (Ctrl+C, baglanti kopmasi) son durumu mutlaka kaydet
            if twin is not None:
                save_state(twin.T1_model, twin.T2_model, twin.T3_model, twin.learner.k, twin.learner.kfil,
                           twin.k_ref, twin.material, mats)
                events =twin.faults.events + [(c, t0, None) for c, t0 in twin.faults.active.items()]
                print(f"\nAriza ozeti: {f'{len(events)} ariza' if events else 'ariza yok'}")
                for code, t0, t1 in events:
                    sure = f"{t1 - t0:.0f} s" if t1 is not None else "hala aktif"
                    print(f"  {code}: {sure} - {FAULT_TEXT[code]}")
                if events:
                    print(f"  kategoriler (hesaplandiysa): {REPORT_FILE}")
                # oturumlar arasi trend: bu oturumun ozeti twin_gecmis.csv'ye (bkz. gecmis.py)
                f.flush()
                try:
                    import sys
                    import gecmis
                    row = gecmis.summarize(LOG_FILE,sys.modules[__name__])
                    if row:
                        gecmis.append(row)
                        vals = [f"{k}={row[k]}" for k in ("P_fan0_W", "blok_kaybi_pct", "fan_sogutma_pct",
                                                          "baski_kaybi_pct") if k in row]
                        print(f"Oturum ozeti -> {os.path.basename(gecmis.GECMIS_FILE)}: "
                              f"{', '.join(vals) if vals else '245C tutma verisi yetersiz, olcut yok'}"
                              f"  (trend: python gecmis.py)")
                except Exception as e:  # noqa: BLE001 - ozet yan ozellik
                    print(f"[oturum ozeti yazilamadi: {type(e).__name__}: {e}]")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nIzleme durduruldu.")
