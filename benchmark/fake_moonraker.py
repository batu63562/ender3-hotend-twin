"""Fake Moonraker: replays a twin log over a websocket so the LIVE loop of realtime_websocket.py can be tested
without the printer (what evaluate.py cannot reach: G-code download and plan start, print-state handling around
the Twin, console and report messages, the pause request).

  py benchmark/fake_moonraker.py LOG [LOG ...] --gcode FILE --sandbox DIR [--speed 30] [--k 1.0 --k-ref 1.0]

--sandbox DIR copies the twin (the files deploy.ps1 sends to the Pi) into DIR, writes its config.toml (this server,
no phone page, shadow pauses) and, with --k, a twin_state.json. Then run the twin there in a second terminal:
  py DIR/realtime_websocket.py
Nothing outside DIR is touched. Every log row becomes one notify_status_update with the logged Klipper eventtime;
rows are sent --speed times faster than they were recorded. A log that starts printing (logs are split at print
starts, K29) is preceded by PRE_ROLL s of 'standby' messages, so the twin sees the print start. HTTP: GET /server/files/gcodes/<name> (--gcode), GET /server/files/config/printer.cfg (--cfg),
POST /printer/print/pause (printed, not acted on).
"""
import argparse
import asyncio
import http
import json
import os
import shutil
import sys
import time

import numpy as np
import pandas as pd
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import evaluate as ev  # noqa: E402

PRE_ROLL = 5.0          # s of idle messages before a log that starts printing
TWIN_FILES = ["realtime_websocket.py", "bed_model.py", "evaluate.py", "fault_classifier.py", "gcode_plan.py",
              "closed_loop.py", "session_history.py", "yazici/printer.cfg", "profile.toml", "calibration.json"]


def sandbox(d, port, k, k_ref, kfil):
    """A runnable copy of the twin in d, pointed at this server."""
    os.makedirs(d, exist_ok=True)
    for f in TWIN_FILES:
        if os.path.exists(os.path.join(ROOT, f)):
            os.makedirs(os.path.dirname(os.path.join(d, f)), exist_ok=True)
            shutil.copy(os.path.join(ROOT, f), os.path.join(d, f))
    with open(os.path.join(d, "config.toml"), "w") as fh:
        fh.write(f'printer_host = "127.0.0.1:{port}"\nweb_port = 0\npause_mode = "shadow"\n')
    if k is not None:
        sys.path.insert(0, d)
        import realtime_websocket as rw
        with open(os.path.join(d, "twin_state.json"), "w") as fh:
            json.dump({"time": time.time(), "model": rw.MODEL_VERSION, "T1": 25.0, "T2": 25.0, "T3": 25.0, "k": k,
                       "k_time": time.time(), "k_fil": kfil, "k_ref": k if k_ref is None else k_ref,
                       "malzeme": "PETG"}, fh)
    print(f"sandbox: {d} (run: py {os.path.join(d, 'realtime_websocket.py')})")


def rows(logs):
    """(eventtime, status) per log row, only fields the twin subscribes to. Several logs of one twin session
    (consecutive files, one Klipper clock) are played back to back."""
    df = ev.prepare_log(pd.concat([pd.read_csv(p, low_memory=False) for p in logs], ignore_index=True))
    num = lambda c: pd.to_numeric(df[c], errors="coerce").values if c in df else np.full(len(df), np.nan)  # noqa: E731
    T, pwm, tg = num("T1_real"), num("pwm"), num("target")
    bT, bP, bG = num("bed_T"), num("bed_pwm"), num("bed_target")
    fan, ext, v = num("fan_state"), num("extrude_velocity"), num("head_v")
    px, py_, pz, pe = (pd.Series(num(c)).ffill().values for c in ("pos_x", "pos_y", "pos_z", "pos_e"))
    ps = df["print_state"].fillna("").astype(str).values if "print_state" in df else np.full(len(df), "")
    f = lambda x: None if np.isnan(x) else float(x)  # noqa: E731
    out = []
    for i in range(len(df)):
        ex = {"power": f(pwm[i]), "target": f(tg[i])}
        if df.has_T[i]:
            ex["temperature"] = f(T[i])
        st = {"extruder": ex, "heater_bed": {"temperature": f(bT[i]), "power": f(bP[i]), "target": f(bG[i])},
              "fan": {"speed": f(fan[i])},
              "motion_report": {"live_extruder_velocity": f(ext[i]), "live_velocity": f(v[i]),
                                "live_position": [f(px[i]), f(py_[i]), f(pz[i]), f(pe[i])]}}
        if ps[i]:
            st["print_stats"] = {"state": ps[i]}
        out.append((float(df.t[i]), st))
    if out and out[0][1].get("print_stats", {}).get("state") == "printing":
        # a few idle messages first: the Twin learns the print state only from messages, and a print that starts
        # with its first message is not a print START for it (live, the service runs long before a print)
        idle = {k: dict(v) for k, v in out[0][1].items()}
        idle["print_stats"] = {"state": "standby"}
        out = [(out[0][0] - PRE_ROLL + j, idle) for j in range(int(PRE_ROLL))] + out
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("log", nargs="+", help="twin log(s) to replay (realtime_log_ws*.csv; several: one session in order)")
    ap.add_argument("--gcode", help="the G-code the log printed (served for the plan; its name is the print's file name)")
    ap.add_argument("--cfg", default=os.path.join(ROOT, "yazici", "printer.cfg"), help="printer.cfg to serve")
    ap.add_argument("--port", type=int, default=7199)
    ap.add_argument("--speed", type=float, default=30.0, help="replay this many times faster than recorded")
    ap.add_argument("--sandbox", help="make a runnable twin copy in this folder first (see above)")
    ap.add_argument("--k", type=float, help="with --sandbox: the twin's saved k (twin_state.json)")
    ap.add_argument("--k-ref", type=float, help="with --sandbox --k: its alarm reference (default k)")
    ap.add_argument("--kfil", type=float, default=1.33)
    ap.add_argument("--wait", type=float, default=60.0, help="s to wait for the twin to connect")
    a = ap.parse_args()
    for f in [*a.log, a.cfg] + ([a.gcode] if a.gcode else []):
        if not os.path.isfile(f):
            sys.exit(f"not found: {f}")
    if a.sandbox:
        sandbox(a.sandbox, a.port, a.k, a.k_ref, a.kfil)
    data = rows(a.log)
    fname = os.path.basename(a.gcode) if a.gcode else "replay.gcode"
    first_state = next((s["print_stats"]["state"] for _, s in data if "print_stats" in s), "standby")
    snap = {k: dict(v) for k, v in data[0][1].items()}
    snap["extruder"].setdefault("temperature", 25.0)
    snap["print_stats"] = {"state": "standby" if first_state == "printing" else first_state, "filename": fname}
    snap["gcode_move"] = {"absolute_extrude": True, "homing_origin": [0.0, 0.0, 0.0, 0.0]}
    snap["configfile"] = {"settings": {"extruder": {"filament_diameter": 1.75}}}
    print(f"{', '.join(os.path.basename(p) for p in a.log)}: {len(data)} rows, {(data[-1][0] - data[0][0]) / 60:.1f} min -> "
          f"~{(data[-1][0] - data[0][0]) / a.speed / 60:.1f} min at x{a.speed:g}; file name {fname}")

    def http_reply(connection, request):
        if request.path == "/websocket":
            return None
        if request.path.startswith("/server/files/gcodes/") and a.gcode:
            body = open(a.gcode, "rb").read()
        elif request.path == "/server/files/config/printer.cfg":
            body = open(a.cfg, "rb").read()
        elif request.path == "/printer/print/pause":
            print(f"  [HTTP pause request at row {sent[0]}]")
            body = b'{"result": "ok"}'
        else:
            return Response(404, "Not Found", Headers({"Content-Length": "0"}), b"")
        print(f"  [HTTP {request.path}]")
        return Response(200, "OK", Headers({"Content-Length": str(len(body))}), body)

    sent, done = [0], asyncio.Event()

    async def handler(ws):
        sub = json.loads(await ws.recv())
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": sub.get("id"), "result": {"eventtime": data[0][0],
                                                                                    "status": snap}}))
        print("  [twin subscribed, replaying]")
        t0, w0 = data[0][0], time.monotonic()
        for i, (t, st) in enumerate(data):
            if "print_stats" in st:
                st["print_stats"].setdefault("filename", fname)
            delay = (t - t0) / a.speed - (time.monotonic() - w0)
            if delay > 0:
                await asyncio.sleep(delay)
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "notify_status_update", "params": [st, t]}))
            sent[0] = i + 1
        print(f"  [all {len(data)} rows sent]")
        await asyncio.sleep(3)
        done.set()

    async def run():
        async with serve(handler, "127.0.0.1", a.port, process_request=http_reply):
            print(f"listening on 127.0.0.1:{a.port} (twin: printer_host = \"127.0.0.1:{a.port}\")")
            await asyncio.wait_for(done.wait(), a.wait + (data[-1][0] - data[0][0]) / a.speed + 30)
    asyncio.run(run())


if __name__ == "__main__":
    main()
