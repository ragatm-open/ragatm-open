"""Manifeste de rafraîchissement production -> staging, et sa vérification.

Deux usages, un seul format :

1. **Émettre** l'instantané d'un environnement (base + médias), avant ou après
   copie ::

       python manage.py media_refresh_manifest --emit /tmp/source.json \\
           --environment production --role source

2. **Vérifier** que la copie est complète — code de sortie non nul sinon ::

       python manage.py media_refresh_manifest --verify \\
           --source /tmp/source.json --destination /tmp/destination.json

La vérification est en **échec fermé** : elle ne conclut ``COMPLETE`` que si la
destination détient au moins autant de documents et de fichiers que la source, et
qu'aucune référence n'y est privée de son fichier. Un cas « 268 lignes / 222
fichiers » sort en ``INCOMPLETE``, jamais en succès.

Cette commande **ne copie rien et ne supprime rien** : elle compte et compare. La
copie appartient à ``scripts/staging/refresh_from_production.sh``.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.rag_core.services import media_refresh_manifest as mrm


class Command(BaseCommand):
    help = (
        "Émet ou vérifie un manifeste de rafraîchissement (base + médias). "
        "Ne copie rien, ne supprime rien."
    )

    def add_arguments(self, parser):
        parser.add_argument("--emit", metavar="FICHIER",
                            help="Écrit le manifeste de CET environnement dans FICHIER (- = stdout).")
        parser.add_argument("--environment", default="",
                            help="Nom de l'environnement décrit (ex. production, staging).")
        parser.add_argument("--role", default="source", choices=["source", "destination"],
                            help="Rôle de cet environnement dans le rafraîchissement.")
        parser.add_argument("--checksums", action="store_true",
                            help=("Empreinte par sha256 du CONTENU (détecte la corruption). "
                                  "Plus lent : lit chaque fichier. Défaut : chemin + taille."))
        parser.add_argument("--verify", action="store_true",
                            help="Compare deux manifestes au lieu d'en produire un.")
        parser.add_argument("--source", metavar="FICHIER", help="Manifeste source (avec --verify).")
        parser.add_argument("--destination", metavar="FICHIER",
                            help="Manifeste destination (avec --verify).")
        parser.add_argument("--json", action="store_true", help="Sortie JSON (supervision).")

    def handle(self, *args, **opts):
        if opts["verify"]:
            return self._verify(opts)
        if not opts["emit"]:
            raise CommandError("Préciser --emit FICHIER, ou --verify --source X --destination Y.")
        return self._emit(opts)

    # ----------------------------------------------------------------- émission
    def _emit(self, opts) -> None:
        manifest = mrm.collect(
            environment=opts["environment"] or "(non nommé)",
            role=opts["role"],
            digest_mode=mrm.DIGEST_CONTENT if opts["checksums"] else mrm.DIGEST_SIZE,
        )
        payload = mrm.dumps(manifest)
        target = opts["emit"]
        if target == "-":
            self.stdout.write(payload)
        else:
            Path(target).write_text(payload + "\n", encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"manifeste écrit : {target}"))

        if not opts["json"] and target != "-":
            t = manifest["totals"]
            self.stdout.write(
                f"  environnement : {manifest['environment']} ({manifest['role']})\n"
                f"  documents     : {t['document_count']}\n"
                f"  référencés    : {t['referenced_media_count']}\n"
                f"  présents      : {t['existing_media_count']}\n"
                f"  introuvables  : {t['missing_media_count']}\n"
                f"  orphelins     : {t['orphan_file_count']}"
            )

    # -------------------------------------------------------------- vérification
    def _verify(self, opts) -> None:
        if not opts["source"] or not opts["destination"]:
            raise CommandError("--verify exige --source et --destination.")

        source = self._read(opts["source"], "source")
        destination = self._read(opts["destination"], "destination")
        verdict = mrm.verify(source, destination)

        if opts["json"]:
            self.stdout.write(json.dumps(verdict, indent=2, ensure_ascii=False))
        else:
            self._render(verdict, source, destination)

        if verdict["result"] != mrm.RESULT_COMPLETE:
            # Échec fermé : un rafraîchissement incomplet ne doit JAMAIS pouvoir
            # être pris pour un succès par le script appelant.
            raise SystemExit(1)

    def _read(self, path: str, label: str) -> dict:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            # Un manifeste illisible vaut échec, jamais « rien à signaler ».
            raise CommandError(f"manifeste {label} illisible ({path}) : {e}") from e

    def _render(self, verdict, source, destination) -> None:
        w = self.stdout.write
        w("Vérification de rafraîchissement (base + médias)")
        w(f"  source      : {source.get('environment')} ({source.get('media_root')})")
        w(f"  destination : {destination.get('environment')} ({destination.get('media_root')})")
        for domain, d in verdict["domains"].items():
            w(f"  {domain} :")
            w(f"      documents  source={d['source_documents']} destination={d['destination_documents']}")
            w(f"      fichiers   source={d['source_media_existing']} destination={d['destination_media_existing']}")
            w(f"      références sans fichier en destination : {d['destination_media_missing']}")
            if d["source_files_not_transported"] is not None:
                w("      fichiers de la source non transportés : "
                  f"{d['source_files_not_transported']}")
        w("")
        if verdict["result"] == mrm.RESULT_COMPLETE:
            w(self.style.SUCCESS("  COMPLETE — base et médias concordent."))
        else:
            w(self.style.ERROR("  INCOMPLETE — le rafraîchissement n'est PAS conforme :"))
            for r in verdict["reasons"]:
                w(self.style.ERROR(f"      - {r}"))
