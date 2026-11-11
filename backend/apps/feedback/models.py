"""Modèles du feedback utilisateur durable.

DOCTRINE (l'unique raison d'être de cette table)
------------------------------------------------
Le signal utilisateur vivait jusqu'ici dans TROIS COLONNES de ``RagQueryLog``
(``feedback_score``/``feedback_reason``/``feedback_comment``). Ce placement le
condamnait : ``RagQueryLog.rag_space`` est en ``CASCADE``, donc supprimer un
RagSpace détruisait *en même temps* la requête et le signalement qui disait
qu'elle s'était mal passée — c'est-à-dire exactement la trace dont on a besoin
pour comprendre, et souvent bien après la suppression de l'espace.

``AnswerFeedback`` corrige cela par construction :

- **aucune** clé étrangère ``CASCADE`` — ni vers ``RagQueryLog``, ni vers
  ``RagSpace``, ``Workspace``, ``User``, ``ChatThread`` ou ``ChatMessage`` ;
- les liens vivants sont en ``SET_NULL`` (ils *deviennent* nuls, ils n'emportent
  rien) ;
- toute information nécessaire à l'interprétation est **dénormalisée à la
  création** dans des champs ``*_hist`` qui, eux, ne bougent plus jamais.

Le modèle suivi est celui, déjà éprouvé dans ce dépôt, de
``LlmEgressAuditEvent`` : ne porter aucune FK vers ce qui peut disparaître.

CE QUE CETTE TABLE NE CONTIENT PAS
----------------------------------
Ni chunk, ni extrait, ni prompt, ni contenu documentaire. La question et la
réponse n'y figurent que sous forme de **hash**. Le seul texte libre est le
commentaire de l'utilisateur — il est borné, et il est exclu des exports par
défaut. Tout le matériel sensible vit dans
:class:`FeedbackDiagnosticSnapshot`, qui expire ; le signal, lui, dure.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

#: Longueur maximale du commentaire libre. Bornée EN BASE et non seulement à
#: l'API : un commentaire est le seul champ de texte libre du modèle, donc le
#: seul vecteur d'injection de contenu volumineux ou personnel.
COMMENT_MAX_CHARS = 2000


class AnswerFeedback(models.Model):
    """Signal utilisateur durable sur une réponse du chat."""

    # --- Notation -----------------------------------------------------------
    RATING_UP = 1
    RATING_DOWN = -1
    RATING_CHOICES = ((RATING_UP, "Pouce haut"), (RATING_DOWN, "Pouce bas"))

    # --- Catégories ---------------------------------------------------------
    # `info_in_docs` est la catégorie à plus forte valeur diagnostique : elle
    # affirme une vérité TERRAIN vérifiable (le terme est dans le corpus), là où
    # les autres expriment une insatisfaction. C'est aussi la seule qui pilote
    # une vérification objective (recherche FTS) dans le calcul de priorité.
    KIND_INFO_IN_DOCS = "info_in_docs"
    KIND_WRONG_SOURCES = "wrong_sources"
    KIND_WRONG_ANSWER = "wrong_answer"
    KIND_WRONG_LANGUAGE = "wrong_language"
    KIND_OTHER = "other"

    KIND_CHOICES = (
        (KIND_INFO_IN_DOCS, "L'information est présente dans mes documents"),
        (KIND_WRONG_SOURCES, "Les sources proposées ne sont pas les bonnes"),
        (KIND_WRONG_ANSWER, "La réponse est fausse ou incomplète"),
        (KIND_WRONG_LANGUAGE, "La langue de la réponse est incorrecte"),
        (KIND_OTHER, "Autre"),
    )
    KIND_VALUES = frozenset(k for k, _ in KIND_CHOICES)

    # --- Cycle de vie -------------------------------------------------------
    STATUS_NEW = "new"
    STATUS_QUALIFIED = "qualified"
    STATUS_EXPORT_APPROVED = "export_approved"
    STATUS_EXPORTED = "exported"
    STATUS_FIXED = "fixed"
    STATUS_CLOSED = "closed"
    STATUS_REJECTED = "rejected"

    STATUS_CHOICES = (
        (STATUS_NEW, "Nouveau"),
        (STATUS_QUALIFIED, "Qualifié"),
        (STATUS_EXPORT_APPROVED, "Export autorisé"),
        (STATUS_EXPORTED, "Exporté"),
        (STATUS_FIXED, "Corrigé"),
        (STATUS_CLOSED, "Classé"),
        (STATUS_REJECTED, "Rejeté"),
    )
    STATUS_VALUES = frozenset(s for s, _ in STATUS_CHOICES)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ------------------------------------------------------------------
    # Liens VIVANTS — jamais CASCADE, toujours SET_NULL.
    # ------------------------------------------------------------------
    query_log = models.ForeignKey(
        "audit.RagQueryLog",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="answer_feedbacks",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="answer_feedbacks",
    )

    # Liens LOGIQUES (UUID sans FK), même convention que
    # ``ChatMessage.rag_query_log_id`` : le feedback reste lisible quand le
    # message ou le fil ont été supprimés.
    message_id = models.UUIDField(null=True, blank=True, db_index=True)
    thread_id = models.UUIDField(null=True, blank=True, db_index=True)

    # ------------------------------------------------------------------
    # Identifiants HISTORIQUES — figés à la création, jamais modifiés.
    # Ce sont eux qui rendent l'incident interprétable après la disparition
    # de tout ce à quoi il se rattachait.
    # ------------------------------------------------------------------
    workspace_id_hist = models.CharField(max_length=64, blank=True, default="", db_index=True)
    rag_space_id_hist = models.UUIDField(null=True, blank=True, db_index=True)
    user_pseudonym = models.CharField(max_length=64, blank=True, default="", db_index=True)
    query_log_id_hist = models.UUIDField(null=True, blank=True, db_index=True)

    question_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    answer_hash = models.CharField(max_length=64, blank=True, default="")
    prompt_hash = models.CharField(max_length=64, blank=True, default="")
    #: Empreinte des chunks RETENUS (sha256 des chunk_id triés). Sert au
    #: regroupement : « même question, même espace, même modèle, même retrieval ».
    sources_fingerprint = models.CharField(max_length=64, blank=True, default="")

    policy_hist = models.CharField(max_length=64, blank=True, default="")
    llm_provider_hist = models.CharField(max_length=64, blank=True, default="")
    llm_model_hist = models.CharField(max_length=128, blank=True, default="")
    log_created_at = models.DateTimeField(null=True, blank=True)

    # ------------------------------------------------------------------
    # Signal utilisateur
    # ------------------------------------------------------------------
    rating = models.SmallIntegerField(choices=RATING_CHOICES)
    kind = models.CharField(max_length=32, blank=True, default="", choices=KIND_CHOICES)
    comment = models.TextField(blank=True, default="")
    #: Renseignés uniquement pour ``info_in_docs`` — ils valent dix commentaires
    #: libres, car ils se promeuvent directement en ``expected_documents`` /
    #: ``expected_answer_contains`` d'une fixture de non-régression.
    expected_document = models.CharField(max_length=512, blank=True, default="")
    expected_term = models.CharField(max_length=512, blank=True, default="")

    # ------------------------------------------------------------------
    # Gouvernance
    # ------------------------------------------------------------------
    status = models.CharField(
        max_length=24, default=STATUS_NEW, choices=STATUS_CHOICES, db_index=True
    )
    priority_score = models.SmallIntegerField(default=0, db_index=True)
    dedup_fingerprint = models.CharField(max_length=64, blank=True, default="", db_index=True)
    app_git_sha = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ------------------------------------------------------------------
    # Immutabilité PARTIELLE
    # ------------------------------------------------------------------
    #: Champs figés après création. Le reste (statut, priorité, et le contenu
    #: que l'utilisateur peut corriger via PATCH) demeure modifiable — un
    #: feedback a un cycle de vie, contrairement à un événement d'audit.
    IMMUTABLE_AFTER_CREATE = (
        "query_log_id",
        "created_by_id",
        "message_id",
        "thread_id",
        "workspace_id_hist",
        "rag_space_id_hist",
        "user_pseudonym",
        "query_log_id_hist",
        "question_hash",
        "answer_hash",
        "prompt_hash",
        "sources_fingerprint",
        "policy_hist",
        "llm_provider_hist",
        "llm_model_hist",
        "log_created_at",
        "rating",
        "app_git_sha",
        "created_at",
    )

    class Meta:
        db_table = "answer_feedback"
        indexes = [
            models.Index(fields=["status", "-priority_score", "-created_at"]),
            models.Index(fields=["rag_space_id_hist", "-created_at"]),
            models.Index(fields=["dedup_fingerprint", "-created_at"]),
            models.Index(fields=["user_pseudonym", "-created_at"]),
        ]
        constraints = [
            # Un utilisateur = un feedback par requête. Partielle sur
            # ``query_log`` non nul : quand le log est purgé, le lien devient
            # NULL et la contrainte cesse de s'appliquer — sans quoi la purge
            # d'audit ferait entrer la table en violation.
            models.UniqueConstraint(
                fields=["query_log", "created_by"],
                condition=models.Q(query_log__isnull=False),
                name="uniq_answer_feedback_log_user",
            ),
            # Littéraux et non constantes de classe : le corps de `Meta` ne voit
            # pas les attributs de la classe englobante (même contrainte que
            # `ChatMessage.Meta`, qui le documente déjà).
            models.CheckConstraint(
                condition=models.Q(rating__in=[1, -1]),
                name="ck_answer_feedback_rating",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind__in=[
                        "info_in_docs",
                        "other",
                        "wrong_answer",
                        "wrong_language",
                        "wrong_sources",
                    ]
                )
                | models.Q(kind=""),
                name="ck_answer_feedback_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "closed",
                        "export_approved",
                        "exported",
                        "fixed",
                        "new",
                        "qualified",
                        "rejected",
                    ]
                ),
                name="ck_answer_feedback_status",
            ),
        ]

    def __str__(self) -> str:
        return f"AnswerFeedback({self.id}, rating={self.rating}, kind={self.kind or '-'}, status={self.status})"

    # -- Détection de modification d'un champ figé --------------------------
    @classmethod
    def from_db(cls, db, field_names, values):
        obj = super().from_db(db, field_names, values)
        obj._loaded_values = dict(zip(field_names, values))
        return obj

    def save(self, *args, **kwargs):
        """Refuse toute réécriture d'un champ historique.

        Une donnée « historique » qu'on peut réécrire n'est plus une preuve. Le
        contrôle est volontairement en Python et non en base : il doit produire
        une erreur lisible côté application, pas une violation de contrainte.
        """
        loaded = getattr(self, "_loaded_values", None)
        if not self._state.adding and loaded:
            changed = [
                f
                for f in self.IMMUTABLE_AFTER_CREATE
                if f in loaded and loaded[f] != getattr(self, f, None)
            ]
            if changed:
                raise ValueError(
                    "AnswerFeedback: champs historiques non modifiables : "
                    + ", ".join(sorted(changed))
                )
        super().save(*args, **kwargs)
        self._loaded_values = {
            f: getattr(self, f, None) for f in self.IMMUTABLE_AFTER_CREATE
        }

    @property
    def has_snapshot(self) -> bool:
        return hasattr(self, "diagnostic_snapshot")


class FeedbackDiagnosticSnapshot(models.Model):
    """Matériel de diagnostic attaché à un feedback — SENSIBLE et EXPIRABLE.

    Séparé d'``AnswerFeedback`` pour quatre raisons, dont aucune n'est
    esthétique :

    1. **Rétention** — le signal se garde des années, ce contenu se garde des
       semaines. Une table unique imposerait le plus court des deux horizons, ou
       conserverait trop longtemps le plus sensible.
    2. **Public** — un admin de workspace peut voir qu'un incident existe et ce
       qu'il vaut ; il n'a pas à lire les extraits documentaires.
    3. **Cycle de vie** — purger ce contenu ne doit pas effacer le signalement.
    4. **Schéma** — le pipeline évolue, ce JSON évolue avec lui ; le signal, lui,
       est un modèle relationnel stable.

    ``capture_mode`` est le champ le plus important de cette table : il dit
    comment la trace a été obtenue. Un ``replay`` n'est PAS une capture — l'index,
    la configuration et les modèles ont pu changer entre-temps. Le confondre avec
    un ``buffer`` ferait conclure sur des données qui ne décrivent pas l'incident.
    """

    #: Trace exacte, écrite pendant l'exécution qui a produit la réponse.
    MODE_BUFFER = "buffer"
    #: Retrieval REJOUÉ après coup. Fidèle seulement si rien n'a changé.
    MODE_REPLAY = "replay"
    #: Ni tampon, ni replay possible : ce qui restait du journal, et rien d'autre.
    MODE_PARTIAL = "partial"

    CAPTURE_MODE_CHOICES = (
        (MODE_BUFFER, "Capture exacte (tampon)"),
        (MODE_REPLAY, "Replay tardif (non fidèle par nature)"),
        (MODE_PARTIAL, "Capture partielle"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # CASCADE assumée DANS CE SENS uniquement : un snapshot sans son feedback
    # n'a pas de sens. L'inverse — un feedback sans snapshot — est au contraire
    # l'état normal après expiration.
    feedback = models.OneToOneField(
        AnswerFeedback,
        on_delete=models.CASCADE,
        related_name="diagnostic_snapshot",
    )

    schema_version = models.PositiveSmallIntegerField(default=1)
    retrieval_trace_version = models.PositiveSmallIntegerField(default=1)
    sources_schema_version = models.PositiveSmallIntegerField(default=3)

    capture_mode = models.CharField(max_length=16, choices=CAPTURE_MODE_CHOICES)

    request_payload = models.JSONField(default=dict, blank=True)
    effective_config = models.JSONField(default=dict, blank=True)
    retrieval_trace = models.JSONField(default=dict, blank=True)
    selected_chunks = models.JSONField(default=dict, blank=True)
    generation_metadata = models.JSONField(default=dict, blank=True)

    #: CALCULÉ, jamais déclaré : un booléen posé à la main finit par mentir, et
    #: c'est lui qui décide si l'export doit être chiffré.
    contains_production_content = models.BooleanField(default=False)

    truncated = models.BooleanField(default=False)
    truncation_reason = models.CharField(max_length=128, blank=True, default="")

    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "feedback_diagnostic_snapshot"
        indexes = [
            models.Index(fields=["expires_at"]),
            models.Index(fields=["capture_mode", "-created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(capture_mode__in=["buffer", "partial", "replay"]),
                name="ck_feedback_snapshot_capture_mode",
            ),
        ]

    def __str__(self) -> str:
        return f"FeedbackDiagnosticSnapshot({self.id}, mode={self.capture_mode})"

    # --- Immutabilité : même patron qu'ActionAuditEvent --------------------
    def save(self, *args, **kwargs):
        """Écriture UNIQUE. Un état « au moment de la réponse » qu'on peut
        réécrire ne décrit plus le moment de la réponse."""
        if not self._state.adding:
            raise ValueError(
                "FeedbackDiagnosticSnapshot is append-only (updates are not allowed)"
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Suppression unitaire interdite : la purge de rétention passe par
        ``QuerySet.delete()``, qui court-circuite cette méthode — exactement la
        convention retenue pour ``ActionAuditEvent``."""
        raise ValueError(
            "FeedbackDiagnosticSnapshot: suppression unitaire interdite (purge par rétention)"
        )

    @property
    def is_exact_capture(self) -> bool:
        """Vrai UNIQUEMENT pour une capture au moment de la réponse."""
        return self.capture_mode == self.MODE_BUFFER
