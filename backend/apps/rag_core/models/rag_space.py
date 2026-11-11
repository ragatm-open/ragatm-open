import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.llm_router.model_defaults import (
    default_ragspace_chat_model,
    default_ragspace_chat_provider,
    default_ragspace_embedding_model,
    default_ragspace_embedding_provider,
)
from apps.workspaces.models import Workspace


class RagSpace(models.Model):
    """
    Espace RAG étanche (isolation stricte).
    Toutes les entités RAG doivent être filtrées par rag_space.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="rag_spaces",
        null=False,
        blank=False,
    )

    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Périmètre owner (owner_scope) : source de vérité d'autorisation ----
    # Le membership `owner` (du même workspace) qui possède ce RagSpace. Un owner
    # ne voit/gère QUE les RagSpaces dont owner_scope == son propre membership.
    # PROTECT : on ne supprime pas un membership owner tant que son périmètre
    # porte des RagSpaces (cf. cycle de vie owner). Nullable pour backfill
    # progressif derrière OWNER_SCOPE_ENABLED. Le client ne choisit JAMAIS cette
    # valeur : le backend la dérive du membership de l'acteur à la création.
    owner_scope = models.ForeignKey(
        "workspaces.Membership",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="owned_ragspaces",
    )

    # --- Créateur (AUDIT UNIQUEMENT — jamais utilisé seul comme ACL) --------
    # Traçabilité de la création. L'autorisation passe par owner_scope +
    # RagSpaceAccess, pas par ce champ. SET_NULL : conserver l'espace si le
    # compte créateur est supprimé.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_rag_spaces",
    )

    prompt_template = models.ForeignKey(
        "chat.PromptTemplate",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rag_spaces",
    )

    # Soft delete
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deleted_rag_spaces",
    )

    # --- Chat / génération ---
    # Les défauts sont des CALLABLES, pas des littéraux : ils lisent
    # `DEFAULT_RAGSPACE_*` (cf. apps/llm_router/model_defaults.py), seule source de
    # vérité. Un littéral ici survivrait à tout changement de configuration
    # d'environnement sans que rien ne le signale — c'est exactement ce qui a fait
    # servir `llama3.1:8b` en production alors que la configuration visait
    # `qwen3:30b-instruct`.
    #
    # Ces défauts ne s'appliquent qu'aux créations qui n'énoncent PAS le champ
    # (seeds, bootstrap, code futur). Les vues de l'API les résolvent en plus
    # explicitement, pour que la réponse HTTP porte la valeur réellement persistée.
    llm_provider = models.CharField(
        max_length=32,
        default=default_ragspace_chat_provider,
        choices=[("ollama", "Modèle local (AI Gateway)")],
    )
    llm_model = models.CharField(
        max_length=128, blank=True, default=default_ragspace_chat_model
    )

    # --- Embeddings / indexation ---
    # Configurable PAR espace (distinct du chat). IA locale par défaut
    # (ollama / nomic-embed-text) ; changer ces deux champs nécessite de réindexer
    # les documents de l'espace (les vecteurs existants ne sont comparables qu'avec
    # le même provider/modèle).
    embedding_provider = models.CharField(
        max_length=32,
        default=default_ragspace_embedding_provider,
        # `ai_gateway` (lot 3) est un provider À PART ENTIÈRE : il ne détourne pas
        # `ollama`. Un espace qui le porte est GOUVERNÉ — ses vecteurs portent une
        # identité de pipeline vérifiée et sa recherche vectorielle est filtrée
        # strictement dessus. Édition Open : c'est le SEUL provider d'embeddings.
        choices=[("ai_gateway", "AI Gateway (pipeline gouverné)")],
    )
    embedding_model = models.CharField(
        max_length=128,
        blank=True,
        default=default_ragspace_embedding_model,
    )

    # --- Seuil de pertinence vectorielle, par espace ---
    # `RAG_MIN_SCORE` est un seuil ABSOLU sur une similarité cosinus. Or l'échelle de
    # cette similarité dépend du modèle d'embedding ET de l'appariement linguistique :
    # une question française contre un corpus anglais plafonne bas, même quand le
    # document est le bon. Mesuré sur un corpus bilingue : les documents PERTINENTS
    # culminent à 0,73 quand le défaut global est 0,76 — le seuil est alors au-dessus
    # des deux populations et coupe indistinctement le pertinent et le hors-sujet.
    #
    # Le seuil devient donc réglable par espace, parce que c'est l'espace qui porte le
    # couple (modèle d'embedding, langue du fonds). `NULL` = utiliser le défaut global :
    # aucun espace existant n'est affecté.
    min_score = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "Seuil de pertinence vectorielle propre à cet espace. Vide = défaut global "
            "(RAG_MIN_SCORE). N'est pris en compte que si RAG_SPACE_MIN_SCORE_ENABLED."
        ),
    )

    # --- Repli LLM cross-provider (historique) ---
    # Opt-in historique d'un repli vers un fournisseur externe en cas de panne Gateway.
    # Édition Open : aucun fournisseur externe n'existe, le champ est conservé pour la
    # compatibilité du schéma et n'ouvre rien (le routeur lève une erreur contrôlée).
    allow_external_fallback = models.BooleanField(default=False)

    # --- RagATM_Core scoping (additif, inerte pour l'instant) ---
    # Métadonnées de scoping introduites pour la trajectoire RagATM_Core
    # (cf. rapport d'évaluation, PR 1). Ces champs ne changent AUCUN comportement
    # actuel : ni l'ingestion, ni le retrieval, ni les permissions, ni l'API ne
    # les lisent dans cette PR. Ils permettront plus tard de résoudre
    # (project, environment) -> rag_space pour /core/retrieve.
    # Défauts de compatibilité pour l'existant : project="ragatm", environment="prod".
    project = models.CharField(max_length=64, default="ragatm")
    environment = models.CharField(max_length=32, default="prod")

    class Meta:
        db_table = "rag_space"

    def __str__(self) -> str:
        return f"{self.name} ({self.id})"

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self, *, by_user=None) -> None:
        if self.deleted_at is None:
            self.deleted_at = timezone.now()
            if by_user is not None:
                self.deleted_by = by_user
            self.save(update_fields=["deleted_at", "deleted_by"])
