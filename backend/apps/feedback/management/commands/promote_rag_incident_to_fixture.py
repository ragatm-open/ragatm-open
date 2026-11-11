"""Promotion d'un incident corrigé en cas de non-régression.

Le format de sortie n'est pas nouveau : c'est le JSONL déjà consommé par
``rag_benchmark`` (``expected_documents``, ``expected_chunk_ids``,
``should_answer``…). Réutiliser le harnais existant plutôt qu'en créer un
second signifie qu'un incident promu entre immédiatement dans la mesure de
qualité déjà en place, avec ses seuils et son ``--fail-under``.

CE QUI EST REFUSÉ ICI
---------------------
Un cas dont le texte attendu vient d'un document de production **n'est pas versé
au dépôt sans anonymisation ni autorisation explicite** (``--allow-content``).
Un test de non-régression est un fichier qui vit indéfiniment dans l'historique
git : c'est le pire endroit où déposer un extrait métier par inadvertance.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:60] or "incident"


class Command(BaseCommand):
    help = "Promeut un incident importé en cas de non-régression (JSONL rag_benchmark)."

    def add_arguments(self, parser):
        parser.add_argument("--incident", required=True, help="Répertoire de l'incident importé.")
        parser.add_argument("--output", required=True, help="Fichier .jsonl ou répertoire de sortie.")
        parser.add_argument(
            "--rag-space", default="",
            help="Nom du RagSpace attendu par le benchmark (champ `rag_space` du cas).",
        )
        parser.add_argument(
            "--allow-content", action="store_true",
            help=(
                "Autorise l'écriture d'un `expected_answer_contains` issu du "
                "signalement. À n'utiliser qu'après vérification qu'aucun contenu "
                "confidentiel n'est versé au dépôt."
            ),
        )
        parser.add_argument("--case-id", default="", help="Identifiant du cas (défaut : incident-<id>).")

    def handle(self, *args, **opts):
        root = Path(opts["incident"])
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise CommandError(f"Incident introuvable ou incomplet : {manifest_path}")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        request = self._read(root / "request.json")
        selected = self._read(root / "selected_chunks.json")
        generation = self._read(root / "generation.json")
        behavior = (root / "expected_behavior.md")

        question = (request or {}).get("question")
        if not question:
            raise CommandError(
                "La question est absente du paquet (export de niveau 1, ou "
                "minimisation active) : un cas de non-régression sans question "
                "ne mesure rien."
            )

        sources = (selected or {}).get("sources") or []
        expected_chunk_ids = [s.get("chunk_id") for s in sources if s.get("chunk_id")]
        expected_documents = sorted(
            {s.get("filename") for s in sources if s.get("filename")}
        )

        policy = (generation or {}).get("policy") or ""
        # Un incident « I don't know alors que l'information existe » se promeut
        # en cas devant RÉPONDRE : c'est l'inverse de la politique observée.
        should_answer = policy == "no_answer" or bool(expected_chunk_ids)

        case: dict = {
            "id": opts["case_id"] or f"incident-{manifest.get('incident_id')}",
            "question": question,
            "should_answer": should_answer,
            "rag_space": opts["rag_space"] or "",
            "expected_documents": expected_documents,
            "expected_chunk_ids": expected_chunk_ids,
            "metadata": {
                "incident_id": manifest.get("incident_id"),
                "source_git_sha": manifest.get("git_sha"),
                "source_environment": manifest.get("source_environment"),
                "capture_mode": manifest.get("capture_mode"),
                "capture_is_exact": manifest.get("capture_is_exact"),
                "observed_policy": policy,
                "embedding_model": manifest.get("embedding_model"),
            },
        }

        # Champs additionnels non lus par le schéma v1 de `rag_benchmark` mais
        # utiles aux assertions dédiées (cf. docs) : conservés dans le cas.
        forbidden = [s.get("document_id") for s in sources if s.get("excluded_as_wrong")]
        if forbidden:
            case["forbidden_document_ids"] = forbidden

        detected_language = ((request or {}).get("language") or {}).get(
            "detected_question_language"
        )
        if detected_language:
            case["expected_answer_language"] = detected_language
        if policy:
            case["expected_policy"] = "ok" if should_answer else policy

        if opts["allow_content"] and behavior.is_file():
            term = self._extract_expected_term(behavior.read_text(encoding="utf-8"))
            if term:
                case["expected_answer_contains"] = [term]
        elif behavior.is_file():
            self.stdout.write(
                self.style.WARNING(
                    "  ⚠ `expected_answer_contains` NON généré : il proviendrait du "
                    "signalement, donc potentiellement d'un contenu de production. "
                    "Utilisez --allow-content après vérification."
                )
            )

        # Garde INVERSÉE à l'origine : elle ne se déclenchait que si
        # `should_answer` était FAUX, donc jamais dans le cas problématique.
        # Constaté en qualification staging : la commande émettait une ligne
        # JSONL que `rag_benchmark` REFUSE à la lecture — un cas de
        # non-régression invalide, découvert seulement au moment de le rejouer.
        #
        # Le cas concerné est justement le plus intéressant : une ABSTENTION.
        # `should_answer` y vaut True (la réponse aurait dû exister), mais le
        # journal ne porte aucune source retenue — il n'y a donc rien de
        # mesurable à en tirer automatiquement. On refuse ICI, avec la marche
        # à suivre, plutôt que de produire un fichier qui cassera en CI.
        if should_answer and not expected_documents and not expected_chunk_ids:
            raise CommandError(
                "Cas non mesurable : `should_answer=true` exige "
                "`expected_documents` ou `expected_chunk_ids`, et cet incident "
                "n'en porte aucun.\n"
                "  C'est la signature d'une ABSTENTION : le journal ne contient "
                "aucune source retenue, donc rien à mesurer automatiquement.\n"
                "  Marche à suivre : compléter le cas à la main à partir de "
                "`expected_behavior.md` — le document et le terme attendus y "
                "figurent, tels que déclarés par l'utilisateur — puis rejouer "
                "`rag_benchmark` sur le fichier obtenu."
            )

        out = Path(opts["output"])
        if out.is_dir() or not out.suffix:
            out.mkdir(parents=True, exist_ok=True)
            out = out / f"{slugify(case['id'])}.jsonl"
        else:
            out.parent.mkdir(parents=True, exist_ok=True)

        line = json.dumps(case, ensure_ascii=False)
        with out.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

        self.stdout.write(self.style.SUCCESS(f"Cas de non-régression écrit : {out}"))
        self.stdout.write(f"  id                  : {case['id']}")
        self.stdout.write(f"  should_answer       : {case['should_answer']}")
        self.stdout.write(f"  expected_documents  : {len(expected_documents)}")
        self.stdout.write(f"  expected_chunk_ids  : {len(expected_chunk_ids)}")
        self.stdout.write("")
        self.stdout.write(
            "  Rejouer : python manage.py rag_benchmark --dataset "
            f"{out} --rag-space <uuid> --fail-under"
        )
        if not manifest.get("capture_is_exact", True):
            self.stdout.write(
                self.style.WARNING(
                    "  ⚠ Cas issu d'une capture NON exacte : relire les attendus "
                    "avant de le rendre bloquant en CI."
                )
            )

    @staticmethod
    def _read(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    @staticmethod
    def _extract_expected_term(markdown: str) -> str:
        match = re.search(r"terme \*\*(.+?)\*\*", markdown)
        return match.group(1).strip() if match else ""
