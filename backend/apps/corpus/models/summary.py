"""Résumé persisté d'un document de **corpus métier** (cache + traçabilité coûts).

Table SŒUR de ``rag_document_summary``, pas une extension de celle-ci.

Pourquoi une table séparée plutôt qu'un ``GenericForeignKey``
-------------------------------------------------------------
``DocumentSummary`` porte deux FK dures (``document`` → ``Document``,
``rag_space`` → ``RagSpace``). Les rendre nullables et polymorphes ferait perdre
l'intégrité référentielle sur le chemin privé — celui qui est en production —
pour accommoder un second cas. Un ``GenericForeignKey`` coûterait la même
intégrité, plus une jointure ``ContentType`` sur chaque lecture de cache, pour un
domaine qui ne connaît que **deux** familles de documents et n'en attend pas de
troisième. Une table sœur garde les deux schémas stricts et lisibles.

Pourquoi le cache est indexé par RagSpace CONSOMMATEUR
------------------------------------------------------
Un corpus métier est partagé entre workspaces, mais la politique d'exécution LLM
(provider, modèle, egress) vient de l'espace **consommateur** — cf.
``apps.corpus.services.summary``. Deux workspaces autorisés sur le même corpus
peuvent donc résumer le même document avec deux providers différents. Servir à
l'un le résumé produit pour l'autre reviendrait à contourner sa configuration, et
potentiellement sa politique d'egress. La clé d'unicité inclut donc
``rag_space`` : c'est une décision explicite, pas un effet de bord.

Champs volontairement ABSENTS : ``thread_id`` et ``request_id``
---------------------------------------------------------------
Cette ligne est un **cache partagé** (plusieurs fils, plusieurs utilisateurs
peuvent attendre le même résumé). Y stocker un fil ou une requête d'origine
reproduirait exactement le défaut que le chemin privé documente et évite : le
lien fil ↔ résumé est porté par ``ChatMessage`` (``summary_id`` + ``request_id``
+ ``thread``), et la finalisation procède par fan-out sur tous les messages en
attente. Un identifiant de fil ici n'en servirait qu'un seul.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from .corpus import DomainCorpus
from .document import DomainCorpusDocument


class CorpusSummaryStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"  # généré en async (document long), en cours
    READY = "READY", "Ready"
    FAILED = "FAILED", "Failed"


class DomainCorpusDocumentSummary(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Contenu résumé (corpus métier) ---
    document = models.ForeignKey(
        DomainCorpusDocument, on_delete=models.CASCADE, related_name="summaries"
    )
    #: Dupliqué depuis le document pour l'étanchéité par requête (même convention
    #: que ``DomainCorpusChunk.corpus``) : une purge par corpus n'a pas à joindre.
    corpus = models.ForeignKey(
        DomainCorpus, on_delete=models.CASCADE, related_name="document_summaries"
    )

    # --- Contexte CONSOMMATEUR (politique d'exécution + périmètre de cache) ---
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="corpus_document_summaries"
    )
    rag_space = models.ForeignKey(
        "rag_core.RagSpace", on_delete=models.CASCADE, related_name="corpus_document_summaries"
    )
    #: Premier demandeur. Informationnel (support / facturation) : la ligne étant
    #: partagée, elle n'appartient à personne. ``SET_NULL`` — un résumé survit à la
    #: suppression de son demandeur.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="corpus_document_summaries",
    )

    # Clé de variante déterministe : "{mode}:{scope}:{page|''}:{language}".
    # Construite par ``summary_service.build_variant_key`` — la MÊME fonction que
    # le privé, pour que les deux caches se lisent de la même façon.
    variant_key = models.CharField(max_length=80)

    mode = models.CharField(max_length=16, default="bullets")
    scope = models.CharField(max_length=16, default="document")
    page = models.IntegerField(null=True, blank=True)
    language = models.CharField(max_length=8, default="fr")

    # Noms alignés sur ``DocumentSummary`` (``summary``/``error_message``/``status``)
    # et NON sur des synonymes : ``summary_messages.settle_from_summary_row`` lit ces
    # attributs en duck-typing pour finaliser les messages du fil. Les renommer
    # imposerait un second chemin de finalisation, donc un second endroit où le
    # statut d'un message peut diverger de celui de son résumé.
    summary = models.TextField(blank=True, default="")
    truncated = models.BooleanField(default=False)

    status = models.CharField(
        max_length=16, choices=CorpusSummaryStatus.choices, default=CorpusSummaryStatus.READY
    )
    error_message = models.TextField(blank=True, default="")
    #: Code STABLE et court (``document_not_ready``, ``enqueue_failed``…), destiné
    #: aux logs et aux tableaux de bord. ``error_message`` reste le détail humain.
    error_code = models.CharField(max_length=64, blank=True, default="")

    # Empreinte des chunks + version de prompt ayant servi à générer ce résumé.
    # Change au moindre réindex du document → le cache est ignoré et régénéré.
    source_fingerprint = models.CharField(max_length=64, blank=True, default="")
    #: Incrémenté à chaque régénération forcée (``refresh=true``). Observabilité :
    #: un compteur qui grimpe signale un résumé que les utilisateurs rejettent.
    refresh_revision = models.PositiveIntegerField(default=0)

    # Coûts / traçabilité LLM
    provider = models.CharField(max_length=32, blank=True, default="")
    model = models.CharField(max_length=128, blank=True, default="")
    prompt_tokens = models.IntegerField(null=True, blank=True)
    completion_tokens = models.IntegerField(null=True, blank=True)
    total_tokens = models.IntegerField(null=True, blank=True)
    cost_usd = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "domain_corpus_document_summary"
        constraints = [
            models.UniqueConstraint(
                fields=["rag_space", "document", "variant_key"],
                name="uniq_corpus_summary_space_document_variant",
            ),
        ]
        indexes = [
            models.Index(fields=["document", "status"]),
            models.Index(fields=["corpus", "created_at"]),
            models.Index(fields=["workspace", "created_at"]),
            # Reaper des résumés PENDING abandonnés (broker perdu).
            models.Index(fields=["status", "updated_at"]),
            models.Index(fields=["source_fingerprint"]),
        ]

    def __str__(self) -> str:
        return f"DomainCorpusDocumentSummary({self.document_id}, {self.variant_key})"
