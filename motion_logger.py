"""Motion twin recorder [K92]: records the ADXL345 and Klipper's executed toolhead moves (trapq) for every print.

Runs on the Klipper Pi as its own service (pi/ender3-motion.service), next to the thermal twin but independent of it:
it only reads, never sends G-code, and a crash here cannot touch the thermal model or its alarms.
Data comes from Klipper's API socket (the same streams as Klipper's scripts/motan/data_logger.py), so recording does
not stall the print the way ACCELEROMETER_MEASURE does. Both streams carry Klipper's print_time: no alignment needed.

Per print, in motion/ next to this file (gzip CSV, readable with read_adxl / read_trapq below):
  motion_YYYYMMDD_HHMM_adxl.csv.gz   raw sensor counts at full rate (~3200 Hz); lossless, ~40-70 MB per print hour
  motion_YYYYMMDD_HHMM_trapq.csv.gz  executed moves: time,duration,start_v,accel,x0,y0,z0,dx,dy,dz
Recording runs while print_stats is printing or paused (heat-up included). Nothing is ever deleted here; recording
stops (and does not start) while the disk has less than MIN_FREE_MB free, so the twin and Klipper never run out.

Usage on the Pi: python motion_logger.py            (service)
                 python motion_logger.py --now 30   (test: record 30 s right away, print or not)
"""
import argparse
import gzip
import json
import os
import selectors
import shutil
import signal
import socket
import sys
import time
import zlib

SOCK = os.path.expanduser("~/printer_data/comms/klippy.sock")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "motion")
MIN_FREE_MB = 800        # the SD card is shared with Klipper, Moonraker and the thermal twin's logs
FLUSH_S = 30             # gzip sync flush: a power cut loses at most this much
SENSOR = "adxl345"
FREEFALL = 9806.65       # mm/s^2; Klipper's adxl345.py scales (full resolution, 3.3 V)
SCALE_XY = 0.003774 * FREEFALL
SCALE_Z = 0.003906 * FREEFALL
RECORDING_STATES = ("printing", "paused")
TRAPQ_HEADER = "time,duration,start_v,accel,x0,y0,z0,dx,dy,dz"


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Conn:
    """One Klipper API connection: JSON messages terminated by 0x03."""

    def __init__(self):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(SOCK)
        self.buf = b""

    def send(self, msg):
        self.s.sendall(json.dumps(msg).encode() + b"\x03")

    def read(self):
        chunk = self.s.recv(1 << 16)
        if not chunk:
            raise ConnectionError("klippy closed the API socket")
        self.buf += chunk
        *msgs, self.buf = self.buf.split(b"\x03")
        return [json.loads(m) for m in msgs]

    def close(self):
        self.s.close()


class Recording:
    """Data connection + the two output files of one print."""

    def __init__(self, filename, stamp=None):
        os.makedirs(OUT_DIR, exist_ok=True)
        stamp = stamp or time.strftime("%Y%m%d_%H%M")
        base = os.path.join(OUT_DIR, "motion_" + stamp)
        n = 2
        while os.path.exists(base + "_adxl.csv.gz"):        # a second print within the same minute
            base = os.path.join(OUT_DIR, "motion_%s_%d" % (stamp, n))
            n += 1
        self.path = base + "_adxl.csv.gz"
        self.fa = gzip.open(self.path, "wt", compresslevel=6, newline="\n")
        self.ft = gzip.open(base + "_trapq.csv.gz", "wt", compresslevel=6, newline="\n")
        self.filename = filename
        self.ft.write("# ender3-twin motion log v1: executed toolhead moves (Klipper trapq), time in print_time\n"
                      "# file=%s\n%s\n" % (filename, TRAPQ_HEADER))
        self.t0 = None
        self.last_us = 0
        self.n_adxl = self.n_moves = self.overflows = 0
        self.last_flush = time.monotonic()
        self.conn = Conn()
        self.conn.send({"id": 101, "method": "adxl345/dump_adxl345",
                        "params": {"sensor": SENSOR, "response_template": {"q": "adxl"}}})
        self.conn.send({"id": 102, "method": "motion_report/dump_trapq",
                        "params": {"name": "toolhead", "response_template": {"q": "trapq"}}})
        log("recording %s -> %s" % (filename, os.path.basename(base)))

    def handle(self, msg):
        q = msg.get("q")
        if q is None:
            if "error" in msg:
                raise RuntimeError("klippy refused the data subscription: %s" % msg["error"])
            return
        p = msg["params"]
        if q == "adxl":
            rows = p["data"]
            if self.t0 is None and rows:
                self.t0 = rows[0][0]
                # file= last: G-code names may contain spaces
                self.fa.write("# ender3-twin motion log v1: ADXL345 raw counts at full rate. "
                              "time = t0 + cumsum(dt_us)/1e6 (Klipper print_time, s); accel mm/s^2 = count * scale\n"
                              "# scale_xy=%.7f scale_z=%.7f t0=%.6f file=%s\ndt_us,x,y,z\n" %
                              (SCALE_XY, SCALE_Z, self.t0, self.filename))
            out = []
            for t, ax, ay, az in rows:
                t_us = round((t - self.t0) * 1e6)
                out.append("%d,%d,%d,%d\n" % (t_us - self.last_us, round(ax / SCALE_XY), round(ay / SCALE_XY),
                                               round(az / SCALE_Z)))
                self.last_us = t_us
            self.fa.writelines(out)
            self.n_adxl += len(rows)
            self.overflows += p.get("overflows", 0) or 0
        elif q == "trapq":
            self.ft.writelines("%.6f,%.6f,%.4f,%.2f,%.4f,%.4f,%.4f,%.6f,%.6f,%.6f\n" % (t, dur, sv, acc, *pos, *d)
                               for t, dur, sv, acc, pos, d in p["data"])
            self.n_moves += len(p["data"])
        now = time.monotonic()
        if now - self.last_flush > FLUSH_S:
            self.fa.flush(zlib.Z_SYNC_FLUSH)
            self.ft.flush(zlib.Z_SYNC_FLUSH)
            self.last_flush = now

    def close(self, why):
        try:
            self.conn.close()
        finally:
            self.fa.close()
            self.ft.close()
        mb = os.path.getsize(self.path) / 1e6
        log("stopped (%s): %d samples (%.1f min), %d moves, %d overflows, %.1f MB" %
            (why, self.n_adxl, self.n_adxl / 3200 / 60, self.n_moves, self.overflows, mb))


def free_mb():
    return shutil.disk_usage(os.path.dirname(OUT_DIR)).free / 1e6


def run_service():
    sel = selectors.DefaultSelector()
    while True:
        status = rec = None
        try:
            status = Conn()
            status.send({"id": 1, "method": "objects/subscribe",
                         "params": {"objects": {"print_stats": ["state", "filename"]},
                                    "response_template": {"q": "status"}}})
            sel.register(status.s, selectors.EVENT_READ, status)
            log("connected to klippy")
            ps = {}
            low_disk_warned = False
            while True:
                for key, _ in sel.select(timeout=10):
                    for msg in key.data.read():
                        if key.data is status:
                            if msg.get("id") == 1 and "result" in msg:
                                ps.update(msg["result"]["status"].get("print_stats", {}))
                            elif msg.get("id") == 1 and "error" in msg:
                                raise RuntimeError(msg["error"])
                            elif msg.get("q") == "status":
                                ps.update(msg["params"]["status"].get("print_stats", {}))
                        else:
                            rec.handle(msg)
                want = ps.get("state") in RECORDING_STATES
                disk_ok = free_mb() > MIN_FREE_MB
                if want and not disk_ok and not low_disk_warned:
                    log("NOT recording: only %.0f MB free on the SD card (limit %d MB)" % (free_mb(), MIN_FREE_MB))
                    low_disk_warned = True
                if rec is None and want and disk_ok:
                    rec = Recording(ps.get("filename", ""))
                    sel.register(rec.conn.s, selectors.EVENT_READ, rec.conn)
                elif rec is not None and (not want or not disk_ok):
                    sel.unregister(rec.conn.s)
                    rec.close(ps.get("state") if disk_ok else "low disk")
                    rec = None
                if not want:
                    low_disk_warned = False
        except (OSError, ConnectionError, RuntimeError, ValueError, KeyError) as e:
            log("klippy connection lost (%s), retrying in 5 s" % e)
        finally:
            for c in (rec.conn if rec else None, status):
                if c is not None:
                    try:
                        sel.unregister(c.s)
                    except (KeyError, ValueError):
                        pass
            if rec is not None:
                rec.close("interrupted")
            if status is not None:
                status.close()
        time.sleep(5)


def run_test(secs):
    rec = Recording("test", stamp=time.strftime("%Y%m%d_%H%M%S") + "_test")
    t_end = time.time() + secs
    try:
        while time.time() < t_end:
            for msg in rec.conn.read():
                rec.handle(msg)
    finally:
        rec.close("test done")


def _lines(path):
    """Text lines of a gzip file, tolerating a missing end (power cut, file still being written)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    with open(path, "rb") as f:
        data = d.decompress(f.read())
    return data.decode().splitlines()


def read_adxl(path):
    """-> (time [print_time s], accel [n, 3] mm/s^2 in the SENSOR's axes), numpy arrays. Needs numpy."""
    import numpy as np
    lines = _lines(path)
    meta = dict(kv.split("=", 1) for kv in lines[1][2:].split(" file=")[0].split(" "))
    rows = [ln for ln in lines[3:] if ln.count(",") == 3]
    a = np.array([r.split(",") for r in rows], dtype=np.int64)
    t = float(meta["t0"]) + np.cumsum(a[:, 0]) / 1e6
    acc = a[:, 1:].astype(float) * [float(meta["scale_xy"]), float(meta["scale_xy"]), float(meta["scale_z"])]
    return t, acc


def read_trapq(path):
    """-> numpy array [n, 10] with the columns of TRAPQ_HEADER, sorted by time. Needs numpy."""
    import numpy as np
    rows = [ln for ln in _lines(path) if ln and not ln[0] in "#t"]
    q = np.array([r.split(",") for r in rows if r.count(",") == 9], dtype=float).reshape(-1, 10)
    return q[np.argsort(q[:, 0])]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--now", type=float, metavar="SECONDS", help="test: record this long right away, then exit")
    args = ap.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # systemd stop -> finally blocks close the files
    if args.now:
        run_test(args.now)
    else:
        run_service()
