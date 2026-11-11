#!/usr/bin/env python3
"""ci/check_image_lock_parity.py — les distributions installées dans l'image correspondent EXACTEMENT au lock.

    ci/check_image_lock_parity.py installed.txt backend/requirements.lock [distribution-à-ignorer ...]

`installed.txt` = sortie de `pip freeze --all` dans l'image. Les noms sont canonisés selon PEP 503
(minuscules, toute suite de `-`, `_`, `.` → `-`) : `pdfminer.six`, `pdfminer_six` et `pdfminer-six` désignent la
même distribution. Les versions sont comparées exactement. Échec (code 1) si une distribution du lock manque dans
l'image ou si sa version diffère ; les distributions ignorées (ex. le paquet applicatif lui-même) sont listées en argument."""
import re, sys
def canon(n): return re.sub(r"[-_.]+", "-", n).lower()
def parse(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\;]+)", line.strip())
        if m: out[canon(m.group(1))] = m.group(2)
    return out
installed, locked = parse(sys.argv[1]), parse(sys.argv[2]); ignore = {canon(x) for x in sys.argv[3:]}
problems = []
for name, ver in sorted(locked.items()):
    if name in ignore: continue
    if name not in installed: problems.append(f"MANQUANT  {name}=={ver}")
    elif installed[name] != ver: problems.append(f"VERSION   {name}: lock {ver} / installé {installed[name]}")
print(f"lock: {len(locked)} paquets, installés: {len(installed)}, divergences: {len(problems)}")
for p in problems: print("  " + p)
sys.exit(1 if problems else 0)
