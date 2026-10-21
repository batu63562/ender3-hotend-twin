"""Set the KAYIP reference k_ref in twin_state.json (run only while the twin service is stopped)."""
import json, os, sys
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "twin_state.json")
d = json.load(open(p))
old = d.get("k_ref")
d["k_ref"] = float(sys.argv[1])
tmp = p + ".tmp"
with open(tmp, "w") as f:
    json.dump(d, f)
    f.flush(); os.fsync(f.fileno())
os.replace(tmp, p)
print("k_ref", old, "->", d["k_ref"], "(k", d.get("k"), ")")
