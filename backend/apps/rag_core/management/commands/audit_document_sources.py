"""Audit READ-ONLY de la disponibilité des FICHIERS SOURCES d'un RagSpace.

Répond à la question qui manquait aux préconditions du pilote : **les documents que
je m'apprête à réindexer ont-ils encore leur fichier d'origine ?**

Son absence a coûté un pilote entier. La base de staging est une copie de celle de
production ; le volume média, lui, ne l'est pas. Les références documentaires
existaient donc toutes, et aucun audit ne regardait le disque : la réindexation a
découvert le problème document par document, en les faisant tous échouer.

Une réindexation NOMINALE ré-extrait le fichier d'origine. Sans lui :

- le re-chunking est impossible (c'est le texte source qui est redécoupé) ;
- `--from-chunks` ré-embarque le texte des chunks SANS les redécouper, donc sans
  ramener les fragments sous la fenêtre d'un modèle plus court.

La commande n'écrit rien : ni fichier, ni document, ni chunk.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.rag_core.models import Chunk, Document, RagSpace

#: Statuts de PÉRIMÈTRE, distincts du statut documentaire. Un document « hors
#: périmètre » n'est pas un problème : il n'était pas censé être réindexé.
SCOPE_INCLUDED = "inclus"
SCOPE_EXCLUDED = "exclu"
SCOPE_OUT = "hors_perimetre"

#: États de la source, du plus sain au plus grave.
SRC_PRESENT = "present"
SRC_MISSING = "absent"
SRC_EMPTY = "vide"
SRC_UNREADABLE = "illisible"
SRC_SYMLINK = "lien_symbolique"
SRC_DIVERGENT = "divergent"
SRC_NO_REFERENCE = "sans_reference"


class Command(BaseCommand):
    help = (
        "Audit READ-ONLY de disponibilité des fichiers sources d'un RagSpace, "
        "document par document. Produit un manifeste (JSON ou CSV) et un verdict "
        "`media_ready_for_reindex`."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--rag-space", dest="rag_space", required=True,
            help="RagSpace audité (UUID). OBLIGATOIRE : cet audit n'est jamais global.",
        )
        parser.add_argument(
            "--status", dest="status", default="READY",
            help="Statut documentaire du PÉRIMÈTRE (défaut: READY). `*` pour tous.",
        )
        parser.add_argument(
            "--exclude-document", dest="exclude_documents", action="append", default=[],
            metavar="UUID",
            help="Document hors périmètre, nommé (répétable). Audité mais non exigé.",
        )
        parser.add_argument(
            "--checksum", action="store_true",
            help=(
                "Calcule le SHA-256 des fichiers présents et le compare à "
                "`Document.sha256`. Coûteux (lecture intégrale) mais c'est la seule "
                "vérification qui distingue « un fichier est là » de « LE fichier est là »."
            ),
        )
        parser.add_argument("--json", dest="as_json", action="store_true", help="Manifeste JSON.")
        parser.add_argument("--csv", dest="as_csv", action="store_true", help="Manifeste CSV.")
        parser.add_argument(
            "--fail-under", action="store_true",
            help="Code retour ≠ 0 si `media_ready_for_reindex` est faux (usage en script).",
        )

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        space = RagSpace.objects.filter(id=opts["rag_space"]).first()
        if space is None:
            raise CommandError(f"RagSpace introuvable: {opts['rag_space']}")

        excluded = {str(x).strip() for x in (opts["exclude_documents"] or []) if str(x).strip()}
        media_root = str(settings.MEDIA_ROOT or "")

        qs = Document.objects.filter(rag_space_id=space.id)
        rows = []
        for doc in qs.order_by("created_at").only(
            "id", "filename", "storage_file", "status", "sha256", "rag_space_id"
        ):
            doc_id = str(doc.id)
            if doc_id in excluded:
                scope = SCOPE_EXCLUDED
            elif opts["status"] != "*" and doc.status != opts["status"]:
                scope = SCOPE_OUT
            else:
                scope = SCOPE_INCLUDED
            rows.append(self._inspect(doc, scope, media_root, checksum=opts["checksum"]))

        included = [r for r in rows if r["scope"] == SCOPE_INCLUDED]
        present = [r for r in included if r["source_state"] == SRC_PRESENT]
        missing = [r for r in included if r["source_state"] in (SRC_MISSING, SRC_NO_REFERENCE)]
        unreadable = [r for r in included if r["source_state"] in (SRC_UNREADABLE, SRC_EMPTY, SRC_SYMLINK)]
        divergent = [r for r in included if r["source_state"] == SRC_DIVERGENT]

        summary = {
            "rag_space_id": str(space.id),
            "rag_space_name": getattr(space, "name", ""),
            "media_root": media_root,
            "checksum_verified": bool(opts["checksum"]),
            "documents_inclus": len(included),
            "sources_disponibles": len(present),
            "sources_absentes": len(missing),
            "sources_illisibles": len(unreadable),
            "sources_divergentes": len(divergent),
            "documents_exclus": sum(1 for r in rows if r["scope"] == SCOPE_EXCLUDED),
            "documents_hors_perimetre": sum(1 for r in rows if r["scope"] == SCOPE_OUT),
            # Le verdict exige la PERFECTION sur le périmètre : une réindexation
            # partielle laisse des documents en échec, donc un espace à moitié migré.
            "media_ready_for_reindex": (
                len(included) > 0
                and len(present) == len(included)
                and not missing and not unreadable and not divergent
            ),
        }

        if opts["as_json"]:
            self.stdout.write(json.dumps({"summary": summary, "manifest": rows},
                                         indent=1, ensure_ascii=False))
        elif opts["as_csv"]:
            w = csv.DictWriter(self.stdout, fieldnames=list(rows[0].keys()) if rows else [])
            w.writeheader()
            w.writerows(rows)
        else:
            self._render(summary, rows)

        if opts["fail_under"] and not summary["media_ready_for_reindex"]:
            sys.exit(1)

    # ------------------------------------------------------------------
    def _inspect(self, doc, scope: str, media_root: str, *, checksum: bool) -> dict:
        """Fiche d'un document. Ne suit JAMAIS un lien symbolique."""
        ff = getattr(doc, "storage_file", None)
        rel = str(getattr(ff, "name", "") or "")
        row = {
            "document_id": str(doc.id),
            "rag_space_id": str(doc.rag_space_id),
            "chemin_relatif_attendu": rel,
            "nom_original": doc.filename or "",
            "statut_document": doc.status,
            "scope": scope,
            "source_state": SRC_NO_REFERENCE,
            "taille_octets": None,
            "checksum_attendu": (doc.sha256 or "") or None,
            "checksum_observe": None,
            "chunks": Chunk.objects.filter(document_id=doc.id).count(),
        }
        if not rel:
            return row

        path = rel if os.path.isabs(rel) else os.path.join(media_root, rel)
        # `lstat` et non `stat` : un lien symbolique doit être VU comme tel, pas suivi.
        # Un média installé par copie ne doit contenir aucun lien — c'en serait un qui
        # pointerait hors du volume, donc un fichier qui disparaîtrait au redéploiement.
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            row["source_state"] = SRC_MISSING
            return row
        except OSError:
            row["source_state"] = SRC_UNREADABLE
            return row

        if os.path.islink(path):
            row["source_state"] = SRC_SYMLINK
            return row
        row["taille_octets"] = int(st.st_size)
        if st.st_size == 0:
            row["source_state"] = SRC_EMPTY
            return row
        if not os.access(path, os.R_OK):
            row["source_state"] = SRC_UNREADABLE
            return row

        if checksum and row["checksum_attendu"]:
            h = hashlib.sha256()
            try:
                with open(path, "rb") as fh:
                    for block in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(block)
            except OSError:
                row["source_state"] = SRC_UNREADABLE
                return row
            row["checksum_observe"] = h.hexdigest()
            if row["checksum_observe"] != row["checksum_attendu"]:
                # Un fichier PRÉSENT mais DIFFÉRENT est plus dangereux qu'un fichier
                # absent : la réindexation réussirait, sur un autre contenu.
                row["source_state"] = SRC_DIVERGENT
                return row

        row["source_state"] = SRC_PRESENT
        return row

    def _render(self, summary: dict, rows: list) -> None:
        w = self.stdout.write
        w(f"RagSpace   : {summary['rag_space_name']} ({summary['rag_space_id']})")
        w(f"MEDIA_ROOT : {summary['media_root']}")
        w(f"checksums  : {'vérifiés' if summary['checksum_verified'] else 'NON vérifiés (--checksum)'}")
        w("")
        for k in ("documents_inclus", "sources_disponibles", "sources_absentes",
                  "sources_illisibles", "sources_divergentes", "documents_exclus",
                  "documents_hors_perimetre"):
            w(f"  {k:26} {summary[k]}")
        w("")
        states = Counter(r["source_state"] for r in rows if r["scope"] == "inclus")
        w("états des sources (périmètre) : " + (", ".join(f"{k}={v}" for k, v in states.most_common()) or "—"))
        bad = [r for r in rows if r["scope"] == "inclus" and r["source_state"] != SRC_PRESENT]
        if bad:
            w("")
            w("documents en défaut :")
            for r in bad[:20]:
                w(f"  {r['document_id']}  {r['source_state']:16} {r['nom_original'][:60]}")
            if len(bad) > 20:
                w(f"  … et {len(bad) - 20} autres")
        w("")
        ok = summary["media_ready_for_reindex"]
        w((self.style.SUCCESS if ok else self.style.ERROR)(
            f"media_ready_for_reindex : {str(ok).lower()}"))
