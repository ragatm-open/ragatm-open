"""Processus de rendu ``ragatm-pdfium/1`` — exécuté dans un INTERPRÉTEUR NEUF par page.

Pourquoi un interpréteur neuf (mesuré, CV-3C) : le rendu d'une police substituée par
PDFium dépend de l'HISTORIQUE du processus (caches globaux de polices). Sur la fixture
F13, la même page donne trois pixels différents selon ce qui a été rendu avant elle. Un
préchauffage « une fois par processus » ne suffit donc pas dans un worker de longue
durée ; l'identité n'est garantie que si l'histoire du processus est elle-même fixée :

    interpréteur neuf → préchauffage synthétique fixe (process-v1) → UNE page.

Aucun import Django : lancé par ``python -I <ce fichier>``.

Protocole : argv = ``dpi page_number expected_w expected_h [--no-warmup]`` ;
stdin = octets PDF ; stdout = ``uint32_be(W) ‖ uint32_be(H) ‖ RGB8`` (row-major) ;
échec = code de sortie ≠ 0 et un code stable sur stderr. ``--no-warmup`` n'existe que
pour le test qui prouve la nécessité du préchauffage.
"""
import struct
import sys

EXIT_CONTRACT_MISMATCH = 3
EXIT_RENDER_FAILED = 4
_ROWS_PER_WRITE = 256


def _warmup_pdf() -> bytes:
    """Page synthétique à police TrueType NON embarquée (``Tahoma``), hors des polices
    intégrées de PDFium : elle emprunte le chemin de substitution avant la page réelle."""
    content = b"BT /F1 18 Tf 72 760 Td (warmup) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /TrueType /BaseFont /Tahoma /Encoding /WinAnsiEncoding >>",
    ]
    out, offsets = b"%PDF-1.7\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def main(argv) -> int:
    import pypdfium2 as pdfium

    dpi, page_number, exp_w, exp_h = (int(v) for v in argv[1:5])
    if "--no-warmup" not in argv[5:]:
        warm = pdfium.PdfDocument(_warmup_pdf())
        page = warm[0]
        page.render(scale=1, draw_annots=False, may_draw_forms=False).close()
        page.close()
        warm.close()

    raw = sys.stdin.buffer.read()
    doc = pdfium.PdfDocument(raw)
    if not 1 <= page_number <= len(doc):
        sys.stderr.write("raster_page_absent")
        return EXIT_CONTRACT_MISMATCH
    page = doc[page_number - 1]
    bitmap = page.render(
        scale=dpi / 72, rotation=0, crop=(0, 0, 0, 0), may_draw_forms=False, draw_annots=False,
        fill_color=(255, 255, 255, 255), grayscale=False, rev_byteorder=False, prefer_bgrx=False,
    )
    if bitmap.mode != "BGR":
        sys.stderr.write(f"raster_bitmap_mode:{bitmap.mode}")
        return EXIT_CONTRACT_MISMATCH
    if (bitmap.width, bitmap.height) != (exp_w, exp_h):
        sys.stderr.write(f"raster_size:{bitmap.width}x{bitmap.height}")
        return EXIT_CONTRACT_MISMATCH

    out = sys.stdout.buffer
    out.write(struct.pack(">II", bitmap.width, bitmap.height))
    bgr = bitmap.to_numpy()  # vue (H, W, 3) qui neutralise le stride
    for y in range(0, bitmap.height, _ROWS_PER_WRITE):
        # BGR natif → RGB8 : permutation de canaux seulement, par blocs de lignes.
        out.write(bgr[y:y + _ROWS_PER_WRITE, :, ::-1].tobytes())
    out.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as e:  # noqa: BLE001 - code stable, aucun contenu
        sys.stderr.write(f"raster_render_failed:{type(e).__name__}")
        sys.exit(EXIT_RENDER_FAILED)
