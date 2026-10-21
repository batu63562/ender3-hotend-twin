"""
Session-to-session trend record (for slowly accumulating faults).

Changes that do not show within one session but build up over weeks: leakage residue around the nozzle
threads (loss decreases), cartridge ageing (resistance rises, power drops -> looks like more loss),
sock wear (loss increases), part fan getting dusty (fan cooling decreases).

At the end of each session the live script extracts one summary row from the session CSV and appends it
to GECMIS_FILE. The measures are computed by energy balance ONLY from periods around 245 C without
extrusion and with the temperature held constant (the same signal as fault_classifier.signals). All are
relative to the model calibrated on 2026-09-25 (0 = same as the calibration day):
    P_fan0_W            average heater power to hold 245 C with the fan off
    blok_kaybi_pct      block loss in fan-off holds, % difference from the model (+ = more loss)
    fan_sogutma_pct     fan cooling in fan-on holds, % difference from the model (normally ~ -8%)
    baski_kaybi_pct     total loss to air while printing (extrusion + fan), % difference; filament load via K_FIL
    P150_W, kayip150_pct  real heater power during the 150 C preheat wait at print start (START_GCODE: nozzle at
                        150 C while the bed heats, X0 Y0, no extrusion) and % difference from the model (k=1).
                        Happens by itself in every print and does not touch the print: a maintenance indicator
                        where slow degradation shows (heatsink dust, heat-break contact, fan problems;
                        2026-10-13: 7.4-8.4 W before maintenance, 9.7 W after). Not suitable for learning k
                        (the bed heats below the nozzle), suitable for a trend.
Without enough data (< MIN_HOLD s) that measure stays empty. Faulty sessions are NOT excluded: a slow
fault should still show in the trend once it crosses the threshold and alarms. Fault codes in a separate column.

Room temperature is not measured (T_AMB = 25 C fixed): at 23 C the loss at 245 C looks ~1 % higher.
Natural day-to-day spread of the block loss is ~+-5 %.

Usage:
    python session_history.py                      # trend table and warnings
    python session_history.py --backfill           # rebuild the history from old logs
    python session_history.py --add LOG.csv        # add one log
"""
import argparse
import glob
import os
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(HERE, "twin_gecmis.csv")
MIN_HOLD = 60.0        # s - minimum valid hold time for a measure
T_HOLD = (240.0, 250.0)
DRIFT_WARN = 5.0       # % - warn if the last session differs this much from the median of the earlier ones
SLOPE_WARN = 2.0       # %/week - warn if the block loss slope exceeds this (at least 5 sessions, 3 days)
COLUMNS = ["oturum", "dosya", "sure_dk", "model", "tutma245_s", "P_fan0_W", "blok_kaybi_pct",
           "fan_sogutma_pct", "baski_kaybi_pct", "P150_W", "kayip150_pct", "k_son", "arizalar", "not"]
T150 = 150.0
HOLD150_MIN = 90.0     # s - the 150 C wait must last at least this; the last 60 s are used (arrival excluded)


def hold150(raw, m):
    """Real heater power (W) in the last 60 s of the 150 C preheat wait and % difference from the model (k=1).
    (None, None) if there is no wait."""
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
    """Summary row (dict) from one session log."""
    import evaluate as d
    import fault_classifier as a
    raw = pd.read_csv(path)
    if len(raw) < 20 or raw.T1_real.isna().all():
        return None                                  # empty/very short session: no summary
    tt = pd.to_numeric(raw.time, errors="coerce")
    row = dict(oturum=datetime.fromtimestamp(float(tt.iloc[0])).strftime("%Y-%m-%d %H:%M"),
               dosya=os.path.basename(path), sure_dk=round((tt.max() - tt.iloc[0]) / 60, 1),
               model=m.MODEL_VERSION, not_=note)
    row["P150_W"], row["kayip150_pct"] = hold150(raw, m)
    try:
        df = d.prepare_log(raw.copy())
        sm, t, _ = a.signals(df, m)
    except Exception as e:  # noqa: BLE001 - if the 245 C measures fail, the rest of the summary is still written
        # (2026-10-13: a 15 h session left on overnight crashed signals with an empty array; no summary was written)
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
            x, y, _ = a.fit_two_term(sm.gap[noext].values, sm[noext])   # block and fan together
        else:
            x = x0 if x0 is not None else 0.0      # without fan-off data the block counts as in the model
            y = -(sm.gap[fanon].mean() + x * sm.L_blok[fanon].mean()) / sm.L_fan[fanon].mean()
        row["fan_sogutma_pct"] = round(100 * y, 1)
    if printing.sum() * G >= 2 * MIN_HOLD:
        c = -sm.gap[printing].mean() / (sm.L_blok[printing] + sm.L_fan[printing]).mean()
        row["baski_kaybi_pct"] = round(100 * c, 1)
    return _finish(row, raw)


def _finish(row, raw):
    """Common columns: last k, fault codes, note."""
    if "k_loss" in raw:
        row["k_son"] = round(float(raw.k_loss.iloc[-1]), 3)
    if "ariza" in raw:
        codes = sorted({c for v in raw.ariza.dropna().astype(str) for c in v.split(";") if c})
        row["arizalar"] = ";".join(codes)
    row["not"] = row.pop("not_")
    return row


def append(row):
    new = not os.path.exists(HISTORY_FILE)
    if not new:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            header = f.readline().strip().split(",")
        if header != COLUMNS:          # old column layout (a column was added): convert the file first
            pd.read_csv(HISTORY_FILE).reindex(columns=COLUMNS).to_csv(HISTORY_FILE, index=False)
    pd.DataFrame([row]).reindex(columns=COLUMNS).to_csv(HISTORY_FILE, mode="a", header=new, index=False)


def backfill(m):
    """Rebuilds the history from the benchmark + fault example logs (in time order)."""
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
    out.to_csv(HISTORY_FILE, index=False)
    print(f"{len(out)} oturum yazildi: {HISTORY_FILE}")


def report():
    if not os.path.exists(HISTORY_FILE):
        print("gecmis yok - once 'python session_history.py --backfill' ya da canli scripti calistir")
        return
    g = pd.read_csv(HISTORY_FILE)
    show = ["oturum", "sure_dk", "tutma245_s", "P_fan0_W", "blok_kaybi_pct", "fan_sogutma_pct",
            "baski_kaybi_pct", "P150_W", "kayip150_pct", "k_son", "arizalar", "not"]
    g = g.reindex(columns=list(dict.fromkeys(COLUMNS + list(g.columns))))
    with pd.option_context("display.width", 220, "display.max_colwidth", 42):
        print(g[show].fillna("").to_string(index=False))
    print(f"\n(0 = kalibrasyon gunu; blok kaybinda gunluk dogal oynama ~+-5%, fan sogutmasi normalde ~-8%)")

    # deliberate fault experiments (foil, no sock ...) stay out of the statistics; real faults are included
    experiment = g["not"].fillna("").str.contains("deney")
    if experiment.any():
        print(f"(trend istatistigi {experiment.sum()} kasitli deney oturumunu disarida birakir)")
    for col, name in (("blok_kaybi_pct", "blok kaybi"), ("fan_sogutma_pct", "fan sogutmasi"),
                      ("baski_kaybi_pct", "baskida hava kaybi"), ("kayip150_pct", "150C on isitma kaybi")):
        s = g.loc[~experiment, ["oturum", col]].dropna()
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
