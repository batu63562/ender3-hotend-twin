"""Copy the twin's finished logs from the Raspberry Pi to this PC (one way, never deletes).

The twin on the Pi archives every log part as realtime_log_ws_onceki_<date>_<time>.csv (one per print, K29/K35).
This script copies the archived parts that are new or changed into a local folder; the live file
(realtime_log_ws.csv) is still being written and is never copied. With --plans the G-code plans and toolpaths
(tahminler/) are copied too. A copy is written to a temporary name first and then renamed, so an interrupted run
never leaves a half file under a real name.

Source: --source, else `pi_share` in config.toml (e.g. "\\\\192.168.0.25\\ender3-twin", the Samba share of
pi/install.sh). Destination: --dest, default loglar/pi next to this script.

Usage (in dt/):   py sync_pi_logs.py [--source PATH] [--dest FOLDER] [--plans] [--dry-run]
The copies can be added to the v0.2 report with:   py benchmark/report_v02.py --extra loglar/pi
"""
import argparse
import glob
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def config_share():
    """`pi_share` from config.toml, or None. Read without importing the twin (that would need its settings)."""
    try:
        import tomllib
        with open(os.path.join(HERE, "config.toml"), "rb") as f:
            return str(tomllib.load(f).get("pi_share", "")).strip() or None
    except (OSError, ValueError):
        return None


def copy_new(files, dest, dry_run):
    """Copies files whose name is missing in dest or whose size differs. Returns (copied, unchanged, bytes)."""
    os.makedirs(dest, exist_ok=True)
    copied = unchanged = size = 0
    for src in sorted(files):
        dst = os.path.join(dest, os.path.basename(src))
        n = os.path.getsize(src)
        if os.path.exists(dst) and os.path.getsize(dst) == n:
            unchanged += 1
            continue
        print(f"  {'would copy' if dry_run else 'copy'} {os.path.basename(src)}  ({n / 1e6:.1f} MB)")
        if not dry_run:
            tmp = dst + ".part"
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        copied += 1
        size += n
    return copied, unchanged, size


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", help="the Pi's twin folder (default: pi_share in config.toml)")
    ap.add_argument("--dest", default=os.path.join(HERE, "loglar", "pi"), help="local folder (default: loglar/pi)")
    ap.add_argument("--plans", action="store_true", help="also copy G-code plans and toolpaths (tahminler/)")
    ap.add_argument("--dry-run", action="store_true", help="only list what would be copied")
    a = ap.parse_args()

    source = a.source or config_share()
    if not source:
        sys.exit("No source: pass --source or set pi_share in config.toml (e.g. \"\\\\\\\\192.168.0.25\\\\ender3-twin\").")
    if not os.path.isdir(source):
        sys.exit(f"Source not reachable: {source}")

    logs = glob.glob(os.path.join(source, "realtime_log_ws_onceki_*.csv"))
    c, u, b = copy_new(logs, a.dest, a.dry_run)
    print(f"logs: {c} {'to copy' if a.dry_run else 'copied'} ({b / 1e6:.1f} MB), {u} already here  -> {a.dest}")
    if a.plans:
        plans = glob.glob(os.path.join(source, "tahminler", "*"))
        c, u, b = copy_new([p for p in plans if os.path.isfile(p)], os.path.join(a.dest, "tahminler"), a.dry_run)
        print(f"plans: {c} {'to copy' if a.dry_run else 'copied'} ({b / 1e6:.1f} MB), {u} already here")


if __name__ == "__main__":
    main()
