"""Regenerate ../i18n.js (zh -> en phrase table used by common.js) from i18n_tr.txt (one `中文=English` per line)."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
d = {}
for line in open(os.path.join(HERE, "i18n_tr.txt"), encoding="utf8"):
    line = line.rstrip("\n")
    if "=" in line:
        k, v = line.split("=", 1)
        d[k] = v
with open(os.path.join(HERE, "..", "i18n.js"), "w", encoding="utf8") as f:
    f.write("// zh -> en phrase table (generated from tools/i18n_tr.txt by tools/gen_i18n.py)\nvar I18N_EN = "
            + json.dumps(d, ensure_ascii=False, indent=0) + ";\n")
print(len(d), "phrases")
