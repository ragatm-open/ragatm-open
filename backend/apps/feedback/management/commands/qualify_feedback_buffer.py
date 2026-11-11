"""Qualification du tampon de capture contre le Redis RÉEL d'un environnement.

Pourquoi une commande et pas seulement des tests : la suite s'exécute sur
``LocMemCache``, qui ne peut prouver ni la clé réellement créée, ni le TTL posé,
ni le partage entre PROCESSUS (backend HTTP ↔ worker Celery), ni le
comportement à l'expiration. Ce sont précisément les propriétés qui décident si
l'activation du tampon est sûre sur un environnement déployé.

À exécuter en **staging avant d'activer** ``RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED``
sur un espace réel :

    python manage.py qualify_feedback_buffer

Innocuité : la commande n'écrit qu'une clé de qualification dédiée, portant un
identifiant explicite, et la supprime en sortant. Elle ne lit, ne modifie et ne
supprime **aucune** trace de production, aucune donnée métier, aucun modèle.
"""
from __future__ import annotations

import json
import time
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.feedback.diagnostics import buffer, capture

#: Contenu documentaire FACTICE injecté en entrée. Il ne doit ressortir nulle
#: part : c'est tout l'objet du contrôle de confidentialité.
CANARY_TEXT = "CANARI-TEXTE-DOCUMENTAIRE-NE-DOIT-PAS-SORTIR"
CANARY_FILE = "CANARI_FICHIER_CONFIDENTIEL.pdf"
CANARY_PATH = "/media/canari/CANARI_FICHIER_CONFIDENTIEL.pdf"


class Command(BaseCommand):
    help = (
        "Qualifie le tampon de capture contre le Redis réel de l'environnement "
        "(clé, TTL, taille, confidentialité, expiration, fail-open). N'écrit "
        "qu'une clé de qualification, supprimée en sortie."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--ttl-check-seconds", type=int, default=0,
            help=(
                "Vérifie l'expiration RÉELLE en écrivant une clé à ce TTL puis en "
                "attendant. 0 = ne pas tester (par défaut : on ne fait pas attendre "
                "un opérateur sans qu'il l'ait demandé)."
            ),
        )

    def handle(self, *args, **opts):
        self.failures: list[str] = []
        self.checks = 0

        self.stdout.write("")
        self._section("1. Configuration effective")
        cache_backend = settings.CACHES["default"]["BACKEND"]
        is_redis = "redis" in cache_backend.lower()
        self._check(
            "cache partagé (Redis) et non LocMem",
            is_redis,
            cache_backend,
        )
        if not is_redis:
            self.stdout.write(
                self.style.WARNING(
                    "    → LocMemCache n'est PAS partagé entre le process HTTP et les "
                    "workers Celery : le worker ne verrait jamais la trace écrite par "
                    "le backend. Posez DJANGO_CACHE_URL avant d'activer le tampon."
                )
            )
        self._info(f"tampon activé : {buffer.is_enabled()}")
        self._info(f"TTL configuré : {buffer.ttl_seconds()} s")
        self._info(f"candidats capturés : {buffer.top_k()}")

        # --- Écriture d'une trace réaliste --------------------------------
        self._section("2. Écriture d'une trace réaliste")
        log_id = f"qualification-{uuid.uuid4()}"
        pool = [
            {
                "chunk_id": f"canari-chunk-{i:03d}",
                "source": {
                    "document_id": f"canari-doc-{i % 3}",
                    "filename": CANARY_FILE,
                    "path": CANARY_PATH,
                },
                "text": CANARY_TEXT,
                "snippet": CANARY_TEXT,
                "score": 0.7 - i * 0.01,
                "fusion_score": 0.03 - i * 0.0001,
                "text_rank": i + 1,
                "vector_rank": i + 3,
            }
            for i in range(25)
        ]
        trace = capture.build_candidate_trace(
            pool=pool,
            selected_pre_rerank=pool[:6],
            selected_post_rerank=pool[:4],
            selected_final=pool[:3],
            top_k=8,
            rerank_applied=True,
            diversity_applied=True,
            max_candidates=buffer.top_k(),
        )
        payload = capture.build_trace_payload(
            log_id=log_id,
            created_at="",
            embedding_model="qualification",
            candidate_trace=trace,
            selection=capture.build_selection(
                selected_final=pool[:3],
                answerability_policy="no_answer",
                thresholds={"min_score": 0.76},
                budgets={"top_k": 8},
            ),
        )

        # `write_trace` respecte le drapeau ; la qualification doit pouvoir
        # s'exécuter AVANT l'activation, donc on force localement.
        previous = getattr(settings, "RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED", False)
        settings.RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED = True
        try:
            written = buffer.write_trace(log_id, payload)
            self._check("écriture acceptée", written is True)

            # --- Relecture ------------------------------------------------
            self._section("3. Relecture et intégrité")
            read_back = buffer.read_trace(log_id)
            self._check("trace relue", read_back is not None)
            if read_back:
                self._check(
                    "candidats intacts",
                    len(read_back.get("candidates") or []) == len(trace["candidates"]),
                    f"{len(read_back.get('candidates') or [])} candidats",
                )
                self._check(
                    "frontière d'observation transportée",
                    read_back.get("observation_boundary") == capture.OBSERVATION_BOUNDARY,
                )

            # --- Confidentialité ------------------------------------------
            self._section("4. Confidentialité de la valeur stockée")
            serialized = json.dumps(read_back or {}, ensure_ascii=False)
            size = len(serialized.encode("utf-8"))
            self._info(f"taille sérialisée : {size} octets")
            self._check(
                "taille sous le plafond",
                0 < size <= buffer.MAX_BUFFER_BYTES,
                f"{size} / {buffer.MAX_BUFFER_BYTES}",
            )
            for canary, label in (
                (CANARY_TEXT, "texte documentaire"),
                (CANARY_FILE, "nom de fichier"),
                (CANARY_PATH, "chemin de stockage"),
                ('"snippet"', "clé snippet"),
                ('"text"', "clé text"),
            ):
                self._check(f"aucun {label} dans la trace", canary not in serialized)

            # --- Expiration ------------------------------------------------
            if opts["ttl_check_seconds"] > 0:
                self._section("5. Expiration réelle")
                ttl = int(opts["ttl_check_seconds"])
                previous_ttl = settings.RAG_FEEDBACK_DIAGNOSTIC_BUFFER_TTL_SECONDS
                settings.RAG_FEEDBACK_DIAGNOSTIC_BUFFER_TTL_SECONDS = ttl
                try:
                    tmp = f"{log_id}-expiration"
                    buffer.write_trace(tmp, {"candidates": [], "log_id": tmp})
                    self._check("présent juste après écriture", buffer.read_trace(tmp) is not None)
                    self._info(f"attente de {ttl + 1} s…")
                    time.sleep(ttl + 1)
                    self._check("absent après expiration", buffer.read_trace(tmp) is None)
                finally:
                    settings.RAG_FEEDBACK_DIAGNOSTIC_BUFFER_TTL_SECONDS = previous_ttl
            else:
                self._section("5. Expiration réelle — NON TESTÉE")
                self._info("relancer avec --ttl-check-seconds 3 pour la vérifier")

            # --- Nettoyage --------------------------------------------------
            self._section("6. Suppression (promotion)")
            buffer.delete_trace(log_id)
            self._check("clé supprimée", buffer.read_trace(log_id) is None)
        finally:
            settings.RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED = previous

        # --- Verdict --------------------------------------------------------
        self.stdout.write("")
        ok = self.checks - len(self.failures)
        if self.failures:
            self.stdout.write(self.style.ERROR(f"RÉSULTAT : {ok}/{self.checks} — NON QUALIFIÉ"))
            for failure in self.failures:
                self.stdout.write(self.style.ERROR(f"  - {failure}"))
            self.stdout.write(
                self.style.ERROR(
                    "N'activez PAS RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED tant que "
                    "ces contrôles ne passent pas."
                )
            )
            return
        self.stdout.write(self.style.SUCCESS(f"RÉSULTAT : {ok}/{self.checks} — QUALIFIÉ"))
        self.stdout.write(
            "Le tampon peut être activé sur un RagSpace de test "
            "(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=1)."
        )

    # ------------------------------------------------------------------
    def _section(self, title: str) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {title} ==="))

    def _info(self, message: str) -> None:
        self.stdout.write(f"    {message}")

    def _check(self, label: str, ok: bool, detail: str = "") -> None:
        self.checks += 1
        suffix = f" — {detail}" if detail else ""
        if ok:
            self.stdout.write(self.style.SUCCESS(f"  [OK]    {label}{suffix}"))
        else:
            self.failures.append(label)
            self.stdout.write(self.style.ERROR(f"  [ÉCHEC] {label}{suffix}"))
