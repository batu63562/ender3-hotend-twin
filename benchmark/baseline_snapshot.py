"""Freeze the scores of a version and check later code against them (v0.3: "reproduces the v0.2 scores exactly").

`--save NAME` replays the September core set (fresh start), the calibration set and the test set (both from the
live start) with the current code and writes every log's phase scores, alarm counts and final k / k_fil to
benchmark/baseline_NAME.json, together with the code version. `--check NAME` replays again and lists every value
that differs. A behaviour-neutral refactor (English identifiers, parameters moved into a profile file) must show
no difference. The test set is replayed here only as an identity check; it is never used to tune anything.

Usage (in dt/):   py benchmark/baseline_snapshot.py --save v0.2
                  py benchmark/baseline_snapshot.py --check v0.2 [--tol 1e-9]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import evaluate as ev  # noqa: E402

FIELDS = ["n", "sure_s", "rmse", "bias", "max", "anomali", "k_anomali", "ariza", "k_son", "kfil_son"]


def snapshot():
    out = {}
    for name, kw in (("september", {}), ("calibration", dict(paths=ev.set_paths("ayar"), live=True)),
                     ("test", dict(paths=ev.set_paths("sinav"), live=True))):
        sc = ev.evaluate(**kw)
        out[name] = {f"{r.log}|{r.faz}": {f: (round(float(getattr(r, f)), 9)) for f in FIELDS}
                     for r in sc.itertuples()}
        print(f"{name}: {sc.log.nunique()} logs, {len(sc)} rows", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--save", metavar="NAME")
    g.add_argument("--check", metavar="NAME")
    ap.add_argument("--tol", type=float, default=1e-9, help="allowed absolute difference (default 1e-9)")
    a = ap.parse_args()

    import realtime_websocket as rw
    path = os.path.join(HERE, "benchmark", f"baseline_{a.save or a.check}.json")
    if a.save:
        data = {"code_version": rw.CODE_VERSION, "scores": snapshot()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        print(f"saved {os.path.relpath(path)} (code {rw.CODE_VERSION})")
        return
    ref = json.load(open(path, encoding="utf-8"))
    now = snapshot()
    diffs = []
    for setname, rows in ref["scores"].items():
        cur = now.get(setname, {})
        for key in sorted(set(rows) | set(cur)):
            if key not in cur or key not in rows:
                diffs.append(f"{setname} {key}: {'missing now' if key not in cur else 'new'}")
                continue
            for f in FIELDS:
                if abs(rows[key][f] - cur[key][f]) > a.tol:
                    diffs.append(f"{setname} {key} {f}: {rows[key][f]} -> {cur[key][f]}")
    print(f"reference code {ref['code_version']}, now {rw.CODE_VERSION}: "
          + ("IDENTICAL" if not diffs else f"{len(diffs)} differences"))
    for d in diffs[:200]:
        print("  " + d)
    sys.exit(1 if diffs else 0)


if __name__ == "__main__":
    main()
