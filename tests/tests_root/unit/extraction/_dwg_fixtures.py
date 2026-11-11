"""fixtures DWG auto-suffisantes (aucun LibreDWG, aucun corpus externe).

- `dwg_bytes(version)` : octets plausibles d'un DWG (en-tête `ACxxxx` + 5 octets nuls,
  ≥ 256 octets) — suffisants pour la détection et l'admission ; jamais un vrai dessin.
- `make_fake_converter(...)` : faux `dwg2dxf` (script `sh`, exécuté par RagATM SANS shell)
  qui enregistre argv/cwd/env/entrée et produit un DXF de test selon un mode. Il prouve la
  sécurité de l'enveloppe (argv, tempdir, nettoyage, délai, tailles, sorties) — JAMAIS
  que LibreDWG convertit réellement un AC1032 (qualification hors dépôt).
- constructeurs de DXF : dessin sémantique de référence, variantes CRLF / entité exotique,
  MTEXT longs découpés « à la 0.14 » (tronçons de 250, code final 3 pour un multiple exact).
"""
from __future__ import annotations

import io
import os
import stat
from pathlib import Path

XREF_PATH = "\\\\server\\share\\plan-ext.dwg"


def dwg_bytes(version: str = "AC1032", size: int = 512) -> bytes:
    """Octets plausibles d'un DWG : `ACxxxx` + 5 nuls + octet de maintenance + remplissage
    déterministe (jamais une séquence textuelle)."""
    head = version.encode("ascii") + b"\x00" * 5 + b"\x00\x03\xc0\x01\x00\x00\x21\x00\x1e"
    body = bytes((i * 7 + 3) % 251 for i in range(max(0, size - len(head))))
    return head + body


# --------------------------------------------------------------------------- DXF payloads
def semantic_dxf_lf() -> bytes:
    """Dessin R2018 de référence : calque CVC, TEXT, MTEXT accentué, bloc EQUIP à ATTRIB,
    cote, géométrie, layout RDC, XREF déclarée (jamais résolue)."""
    import ezdxf

    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    doc.layers.add("CVC")
    msp = doc.modelspace()
    msp.add_text("CTA-DWG-01", dxfattribs={"layer": "CVC"})
    msp.add_mtext("{\\fArial|b1;Débit} 15000 m3/h\\Ptempérature 21 %%d", dxfattribs={"layer": "CVC"})
    blk = doc.blocks.new("EQUIP")
    blk.add_attdef("REPERE", (0, 0))
    blk.add_attdef("DEBIT", (0, 1))
    ins = msp.add_blockref("EQUIP", (10, 10), dxfattribs={"layer": "CVC"})
    ins.add_auto_attribs({"REPERE": "CTA-DWG-01", "DEBIT": "15000 m3/h"})
    msp.add_linear_dim(base=(0, 5), p1=(0, 0), p2=(2500, 0), dxfattribs={"layer": "CVC"}).render()
    for i in range(10):
        msp.add_line((i, 0), (i, 1), dxfattribs={"layer": "CVC"})
    lay = doc.layouts.new("RDC")
    lay.add_text("Tableau TGBT", dxfattribs={"layer": "0"})
    doc.blocks.new("PLAN_EXT", dxfattribs={"flags": 4, "xref_path": XREF_PATH})
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode("utf-8")


def to_crlf(lf: bytes) -> bytes:
    return lf.replace(b"\n", b"\r\n")


def semantic_dxf_with_exotic(dxftype: str = "SECTIONOBJECT") -> bytes:
    """Même dessin + une entité exotique en tags bruts (ezdxf la charge en `DXFTagStorage`,
    sans attribut `layer` supporté) : tolérée lors de la qualification."""
    import ezdxf

    text = semantic_dxf_lf().decode("utf-8")
    owner = ezdxf.read(io.StringIO(text)).modelspace().block_record.dxf.handle
    marker = "  0\nSECTION\n  2\nENTITIES\n"
    i = text.index(marker) + len(marker)
    ent = (f"  0\n{dxftype}\n  5\nFFF1\n330\n{owner}\n100\nAcDbEntity\n  8\n0\n"
           f"100\nAcDbSection\n 90\n1\n  1\nSection Plane DWG\n")
    return (text[:i] + ent + text[i:]).encode("utf-8")


def text_of_length(n: int) -> str:
    s = "PREFIX-"
    i = 1
    while len(s) < n:
        s += f"{i:06d}-"
        i += 1
    return s[:n]


LONG_LENGTHS = (249, 250, 251, 499, 500, 501, 750, 1000)
UNICODE_TEXT = "Débit réglé à 12 000 m³/h — température 21 °C ; façade Nord Ø 300 mm ; " * 4


def long_mtext_dxf(style: str = "0.14") -> tuple[bytes, dict]:
    """DXF R2000 (cp1252) dont les MTEXT longs sont réécrits avec le découpage du writer
    LibreDWG : `style="0.14"` = tronçons de 250 en code 3, dernier tronçon en code 1 sauf
    multiple exact de 250 (tout en code 3, comportement 0.14 observé en DWG-1) ;
    `style="master"` = dernier tronçon toujours en code 1. Renvoie (octets, {clé: texte})."""
    import ezdxf

    expected = {f"len{n}": text_of_length(n) for n in LONG_LENGTHS}
    expected["unicode"] = UNICODE_TEXT[:300].strip()
    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    for k, (key, txt) in enumerate(expected.items()):
        msp.add_mtext(f"@@{key}@@", dxfattribs={"layer": "0", "insert": (0, 10 * k)})
    s = io.StringIO()
    doc.write(s)
    text = s.getvalue()
    for key, txt in expected.items():
        chunks = [txt[i:i + 250] for i in range(0, len(txt), 250)] or [""]
        lines = []
        for j, c in enumerate(chunks):
            last = j == len(chunks) - 1
            code = "1" if (last and (style == "master" or len(txt) % 250 != 0)) else "3"
            lines.append(f"  {code}\n{c}\n")
        text = text.replace(f"  1\n@@{key}@@\n", "".join(lines))
    return text.encode(doc.encoding), expected


# --------------------------------------------------------------------------- fake converter
_SCRIPT = r'''#!/bin/sh
# Faux dwg2dxf — TESTS SEULEMENT. Lancé par RagATM sans shell (argv direct).
D="__DIR__"
if [ "$1" = "--version" ]; then echo "__VERSION__"; exit 0; fi
printf '%s\n' "$@" > "$D/argv.txt"
pwd > "$D/cwd.txt"
env | sort > "$D/env.txt"
out=""; in=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift; out="$1" ;;
    -y) ;;
    *) in="$1" ;;
  esac
  shift
done
printf '%s' "$in" > "$D/input_name.txt"
stat -c %s "$in" > "$D/input_size.txt" 2>/dev/null
stat -c %a "$in" > "$D/input_mode.txt" 2>/dev/null
stat -c %a . > "$D/cwd_mode.txt" 2>/dev/null
sha256sum "$in" | cut -c1-64 > "$D/input_sha.txt"
case "__MODE__" in
  ok) cp "$D/payload.dxf" "$out" ;;
  ok_warn) echo "Warning: Unstable Class entity 517 HELIX (0xfff) 138/0" >&2; cp "$D/payload.dxf" "$out" ;;
  fail) echo "ERROR: cannot decode entities section" >&2; exit 3 ;;
  malformed) echo "READ ERROR 0x800" >&2; exit 1 ;;
  noout) exit 0 ;;
  empty) : > "$out" ;;
  invalid) printf '  0\nSECTION\n  2\nHEADER\ngarbage\n' > "$out" ;;
  big) head -c __SIZE__ /dev/zero > "$out" ;;
  sleep) sleep 120 & echo $! > "$D/grandchild.txt"; sleep 120 ;;
  noisy) i=0; while [ $i -lt 60000 ]; do echo "Warning: Unstable Class object 505 MATERIAL (0x481) $i/0 in __SECRET__" >&2; i=$((i+1)); done; cp "$D/payload.dxf" "$out" ;;
  crash) kill -SEGV $$ ;;
esac
'''


def make_fake_converter(
    directory: Path, *, mode: str = "ok", payload: bytes | None = None,
    version: str = "dwg2dxf 0.14", size: int = 0,
) -> Path:
    """Écrit le faux convertisseur dans `directory` et renvoie son chemin (exécutable)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "payload.dxf").write_bytes(payload if payload is not None else semantic_dxf_lf())
    script = directory / "fake-dwg2dxf"
    script.write_text(
        _SCRIPT.replace("__DIR__", str(directory)).replace("__VERSION__", version)
        .replace("__MODE__", mode).replace("__SIZE__", str(size)).replace("__SECRET__", "TMPDIR=$TMPDIR"),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return script


def record(directory: Path, name: str) -> str:
    p = Path(directory) / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        # zombie = mort pour nous
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False
