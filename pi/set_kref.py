"""Set the KAYIP reference k_ref in twin_state.json (run only while the twin service is stopped).
Usage on the Pi:  python3 ~/ender3-twin/pi/set_kref.py 1.007   [K50]"""
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
p = next(c for c in (os.path.join(here, "twin_state.json"), os.path.join(os.path.dirname(here), "twin_state.json"))
         if os.path.exists(c))   # next to the script, or one folder up (deploy puts it in pi/)
d = json.load(open(p))
old = d.get("k_ref")
d["k_ref"] = float(sys.argv[1])
tmp = p + ".tmp"
with open(tmp, "w") as f:
    json.dump(d, f)
    f.flush(); os.fsync(f.fileno())
os.replace(tmp, p)
print("k_ref", old, "->", d["k_ref"], "(k", d.get("k"), ")")
