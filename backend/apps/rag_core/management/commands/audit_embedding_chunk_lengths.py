"""Audit READ-ONLY des longueurs de chunks face à la limite d'un modèle gouverné.

Sert à décider, AVANT toute réindexation, si un RagSpace peut passer sur un modèle
dont la fenêtre est plus courte que le chunking actuel. Le profil Granite
(`fr-en-retrieval`) est en `on_overflow: reject` à 1 200 caractères : un chunk plus
long ne serait pas tronqué en silence, il ferait ÉCHOUER son document.

La commande n'écrit rien. Aucune requête de modification, aucun appel d'embedding.
"""
from __future__ import annotations

import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from apps.rag_core.models import Chunk, Document, RagSpace

#: Limite du profil `fr-en-retrieval` telle qu'exposée par `/health/embeddings`
#: (`max_input_chars`). Défaut de commodité : la vraie limite est celle de la Gateway,
#: et `--max-input-chars` permet de la refléter si le registre change.
DEFAULT_MAX_INPUT_CHARS = 1200

#: Seuil d'arrêt du §14 : au-delà, le modèle n'est pas retenu pour cet espace.
NO_GO_RATIO = 0.20


def _percentile(sorted_values: list[int], p: float) -> int:
    """Percentile par rang au plus proche. Liste vide -> 0."""
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = int(round((p / 100.0) * (len(sorted_values) - 1)))
    return sorted_values[max(0, min(idx, len(sorted_values) - 1))]


class Command(BaseCommand):
    help = (
        "Audit READ-ONLY des longueurs de chunks d'un RagSpace face à la limite "
        "d'entrée d'un modèle d'embeddings gouverné (défaut: 1200 caractères)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--rag-space", dest="rag_space", required=True,
            help="RagSpace à auditer (UUID). OBLIGATOIRE : cet audit n'est jamais global.",
        )
        parser.add_argument(
            "--model", dest="model", default="fr-en-retrieval",
            help="Modèle LOGIQUE visé, pour l'en-tête du rapport (défaut: fr-en-retrieval).",
        )
        parser.add_argument(
            "--max-input-chars", dest="max_input_chars", type=int,
            default=DEFAULT_MAX_INPUT_CHARS,
            help=f"Limite de caractères du modèle visé (défaut: {DEFAULT_MAX_INPUT_CHARS}).",
        )
        parser.add_argument(
            "--json", dest="as_json", action="store_true",
            help="Sortie JSON (pour archivage dans le rapport de lot).",
        )

    def handle(self, *args, **opts):
        space_id = opts["rag_space"]
        model = opts["model"]
        limit = int(opts["max_input_chars"])

        space = RagSpace.objects.filter(id=space_id).first()
        if space is None:
            raise CommandError(f"RagSpace introuvable: {space_id}")

        # `values_list` + itération : on ne charge JAMAIS le texte, seulement sa
        # longueur — un espace volumineux ne doit pas faire exploser la mémoire.
        lengths = sorted(
            Chunk.objects.filter(rag_space_id=space.id)
            .extra(select={"n": "char_length(text)"})
            .values_list("n", flat=True)
        )
        n_chunks = len(lengths)
        n_docs = Document.objects.filter(rag_space_id=space.id).count()

        over = [x for x in lengths if x > limit]
        ratio = (len(over) / n_chunks) if n_chunks else 0.0

        # Répartition par type de document ET par VOIE d'ingestion.
        #
        # La voie est la distinction qui décide réellement du GO/NO-GO, et elle n'est
        # pas visible dans l'extension du fichier. Deux familles de chunks coexistent :
        #
        #   - voie TEXTE (`texte`, `pptx_slide`, `excel_narrative`) : découpée par
        #     `chunk_size`. Un re-chunking gouverné à 1200 la ramène sous la limite.
        #   - voie ATOMIQUE (`excel_table`, tableaux PDF) : PRÉ-fenêtrée par
        #     l'extracteur, jamais redécoupée par `chunk_size`. Elle ignore
        #     `RAG_GOVERNED_CHUNK_SIZE_CHARS` — un re-chunking ne la corrige PAS.
        #
        # Confondre les deux ferait conclure « il suffit de re-chunker » sur un espace
        # dont les classeurs feraient échouer chaque document à l'indexation.
        ATOMIC_KINDS = {"excel_table", "table", "pdf_table"}
        by_ext: Counter[str] = Counter()
        over_by_ext: Counter[str] = Counter()
        by_kind: Counter[str] = Counter()
        over_by_kind: Counter[str] = Counter()
        rows = (
            Chunk.objects.filter(rag_space_id=space.id)
            .extra(select={"n": "char_length(text)"})
            .values_list("document__filename", "meta", "n")
        )
        for filename, meta, n in rows:
            ext = ("." + str(filename or "").rsplit(".", 1)[-1].lower()) if "." in str(filename or "") else "(sans extension)"
            kind = (meta or {}).get("kind") or "texte"
            by_ext[ext] += 1
            by_kind[kind] += 1
            if n > limit:
                over_by_ext[ext] += 1
                over_by_kind[kind] += 1

        atomic_over = sum(v for k, v in over_by_kind.items() if k in ATOMIC_KINDS)
        atomic_total = sum(v for k, v in by_kind.items() if k in ATOMIC_KINDS)
        atomic_ratio = (atomic_over / n_chunks) if n_chunks else 0.0

        payload = {
            "rag_space_id": str(space.id),
            "rag_space_name": getattr(space, "name", ""),
            "target_model_logical": model,
            "max_input_chars": limit,
            "documents": n_docs,
            "chunks": n_chunks,
            "chunks_with_embedding": Chunk.objects.filter(
                rag_space_id=space.id, embedding__isnull=False
            ).count(),
            "min_chars": lengths[0] if lengths else 0,
            "p50_chars": _percentile(lengths, 50),
            "p90_chars": _percentile(lengths, 90),
            "p95_chars": _percentile(lengths, 95),
            "p99_chars": _percentile(lengths, 99),
            "max_chars": lengths[-1] if lengths else 0,
            "chunks_over_limit": len(over),
            "ratio_over_limit": round(ratio, 4),
            "no_go_threshold": NO_GO_RATIO,
            # Dépassements IRRÉDUCTIBLES : voie atomique, que le re-chunking gouverné
            # n'atteint pas. C'est ce chiffre qui décide, pas le total.
            "atomic_chunks": atomic_total,
            "atomic_chunks_over_limit": atomic_over,
            "ratio_atomic_over_limit": round(atomic_ratio, 4),
            "verdict": self._verdict(n_chunks, ratio, atomic_ratio),
            "by_extension": {
                ext: {"chunks": c, "over_limit": over_by_ext.get(ext, 0)}
                for ext, c in by_ext.most_common()
            },
            "by_ingestion_kind": {
                k: {
                    "chunks": c,
                    "over_limit": over_by_kind.get(k, 0),
                    "atomic": k in ATOMIC_KINDS,
                }
                for k, c in by_kind.most_common()
            },
            # Tokens : NON mesurés, et c'est dit explicitement. Granite a une fenêtre
            # de 512 tokens ; sans son tokenizer, toute conversion caractères->tokens
            # serait une estimation présentée comme une mesure. La limite qui fait
            # AUTORITÉ est celle appliquée par la Gateway, en caractères.
            "tokens_measured": False,
            "tokens_note": (
                "Tokens non mesurés (tokenizer Granite absent en local). La garde "
                "effective est celle de la Gateway, exprimée en caractères."
            ),
        }

        if opts["as_json"]:
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False))
            return

        w = self.stdout.write
        w(f"RagSpace         : {payload['rag_space_name']} ({payload['rag_space_id']})")
        w(f"Modèle visé      : {model} (limite {limit} caractères, on_overflow=reject)")
        w(f"Documents        : {n_docs}")
        w(f"Chunks           : {n_chunks} (dont {payload['chunks_with_embedding']} vectorisés)")
        if not n_chunks:
            w(self.style.WARNING("Aucun chunk : audit sans objet (espace vide)."))
            return
        w("")
        w(f"  min            : {payload['min_chars']}")
        w(f"  p50            : {payload['p50_chars']}")
        w(f"  p90            : {payload['p90_chars']}")
        w(f"  p95            : {payload['p95_chars']}")
        w(f"  p99            : {payload['p99_chars']}")
        w(f"  max            : {payload['max_chars']}")
        w("")
        w(f"Chunks > {limit}   : {len(over)} ({ratio:.1%})")
        w("")
        w("Par type de document :")
        for ext, c in by_ext.most_common():
            o = over_by_ext.get(ext, 0)
            w(f"  {ext:20} {c:7d} chunks, {o:6d} > limite ({(o / c if c else 0):.1%})")
        w("")
        w("Par VOIE d'ingestion (c'est elle qui décide) :")
        for k, c in by_kind.most_common():
            o = over_by_kind.get(k, 0)
            tag = "ATOMIQUE — non re-chunkable" if k in ATOMIC_KINDS else "texte — re-chunkable"
            w(f"  {k:20} {c:7d} chunks, {o:6d} > limite ({(o / c if c else 0):6.1%})  [{tag}]")
        w("")
        w(f"Dépassements IRRÉDUCTIBLES (voie atomique) : {atomic_over} ({atomic_ratio:.1%} du total)")
        w("")
        verdict = payload["verdict"]
        style = self.style.SUCCESS if verdict.startswith("GO") else self.style.ERROR
        w(style(f"Verdict : {verdict}"))
        w(payload["tokens_note"])

    @staticmethod
    def _verdict(n_chunks: int, ratio: float, atomic_ratio: float) -> str:
        """Le verdict repose sur les dépassements IRRÉDUCTIBLES, pas sur le total.

        Un dépassement de la voie TEXTE disparaît à la réindexation gouvernée (le
        budget de chunk tombe à `RAG_GOVERNED_CHUNK_SIZE_CHARS`). Un dépassement de la
        voie ATOMIQUE, lui, survit : ces fragments sont pré-fenêtrés par l'extracteur
        et ne repassent jamais par le découpage en taille. La Gateway les REFUSE
        (`on_overflow: reject`), et c'est le document ENTIER qui échoue.
        """
        if not n_chunks:
            return "INDÉTERMINÉ — espace vide, aucune mesure possible"
        if atomic_ratio > NO_GO_RATIO:
            return (
                f"NO-GO — {atomic_ratio:.1%} des chunks dépassent la limite par la voie "
                f"ATOMIQUE (> {NO_GO_RATIO:.0%}), que le re-chunking gouverné n'atteint "
                "pas. Chaque document concerné échouerait à l'indexation. Choisir un "
                "espace pilote sans classeurs ni tableaux atomiques, ou borner d'abord "
                "le fenêtrage de l'extracteur (hors périmètre du lot 3)."
            )
        if atomic_ratio > 0:
            return (
                f"GO SOUS CONDITION — {atomic_ratio:.1%} de dépassements irréductibles "
                f"(voie atomique) et {ratio:.1%} au total. Les documents portant ces "
                "fragments échoueront ; les recenser avant de lancer la réindexation."
            )
        if ratio > 0:
            return (
                f"GO — {ratio:.1%} de dépassements, tous sur la voie TEXTE : le "
                "re-chunking gouverné (1200) les ramène sous la borne."
            )
        return "GO — aucun chunk au-dessus de la limite."
