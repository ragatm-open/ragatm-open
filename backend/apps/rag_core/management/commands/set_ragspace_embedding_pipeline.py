"""Configure le provider/modèle d'embeddings d'UN RagSpace (lot 3, §18 étape 3).

Pourquoi une commande plutôt que l'API
--------------------------------------
Le provider `ai_gateway` n'est **volontairement pas** ajouté à
``ALLOWED_EMBEDDING_PROVIDERS`` : l'ouvrir à l'API le rendrait activable par tout
gestionnaire de modèles, sur n'importe quel espace, dans n'importe quel
environnement — y compris en production, où Granite est interdit. Le lot 3 est un
pilote sur UN espace de staging ; la surface d'activation doit être à sa mesure.

Cette commande refuse tout ce qui n'a pas été vérifié : Gateway injoignable, modèle
non autorisé dans l'environnement, dimension divergente. Elle **ne réindexe rien** —
changer le provider rend seulement les chunks existants non conformes, ce que
``audit_embedding_pipelines`` rend immédiatement visible.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.llm_router.embeddings_gateway import (
    PROVIDER_AI_GATEWAY,
    gateway_embeddings_base_url,
    gateway_embeddings_enabled,
)
from apps.rag_core.models import Document, RagSpace

#: Providers acceptés par cette commande. `ai_gateway` n'est pilotable QUE par ici.
_PROVIDERS = ("ai_gateway",)  # édition Open : seul le pipeline gouverné existe


class Command(BaseCommand):
    help = (
        "Configure embedding_provider/embedding_model d'UN RagSpace. "
        "Ne réindexe rien. Vérifie la Gateway avant d'accepter `ai_gateway`."
    )

    def add_arguments(self, parser):
        parser.add_argument("--rag-space", dest="rag_space", required=True, help="RagSpace (UUID).")
        parser.add_argument("--provider", dest="provider", required=True, choices=_PROVIDERS)
        parser.add_argument("--model", dest="model", required=True, help="Modèle LOGIQUE (Gateway) ou nom de modèle.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Affiche ce qui serait fait, sans écrire. À exécuter d'abord.",
        )
        parser.add_argument(
            "--mark-reindex-required", action="store_true",
            help=(
                "Marque les documents de l'espace `reindex_required=True`. Recommandé : "
                "sans cela, le leg vectoriel les exclut déjà (garde P1-D) mais rien ne "
                "signale POURQUOI dans l'interface."
            ),
        )

    def handle(self, *args, **opts):
        space = RagSpace.objects.filter(id=opts["rag_space"]).first()
        if space is None:
            raise CommandError(f"RagSpace introuvable: {opts['rag_space']}")

        provider, model = opts["provider"], (opts["model"] or "").strip()
        if not model:
            raise CommandError("--model ne peut pas être vide.")

        old_provider = space.embedding_provider or ""
        old_model = space.embedding_model or ""

        if provider == PROVIDER_AI_GATEWAY:
            self._preflight_gateway(model)

        w = self.stdout.write
        w(f"RagSpace : {getattr(space, 'name', '')} ({space.id})")
        w(f"  avant  : provider={old_provider or '—'} model={old_model or '—'}")
        w(f"  après  : provider={provider} model={model}")
        n_docs = Document.objects.filter(rag_space_id=space.id).count()
        w(f"  documents concernés : {n_docs} (AUCUN n'est réindexé par cette commande)")

        if opts["dry_run"]:
            w(self.style.WARNING("[dry-run] aucune écriture."))
            return

        space.embedding_provider = provider
        space.embedding_model = model
        space.save(update_fields=["embedding_provider", "embedding_model"])

        marked = 0
        if opts["mark_reindex_required"]:
            marked = Document.objects.filter(rag_space_id=space.id).update(reindex_required=True)

        w(self.style.SUCCESS(f"OK. Espace reconfiguré. documents marqués reindex_required={marked}"))
        w(
            "Prochaine étape : `audit_embedding_pipelines --rag-space <id>` doit montrer "
            "0 chunk conforme (l'espace n'est pas encore réindexé), puis "
            "`reembed_documents --rag-space <id> --provider ai_gateway --model "
            f"{model} --dry-run`."
        )

    def _preflight_gateway(self, model: str) -> None:
        """Refuse `ai_gateway` tant que la Gateway n'a pas confirmé le profil (§15).

        Les trois refus sont ceux du lot : Gateway non ready, modèle non autorisé
        dans l'environnement, dimension différente de `RAG_EMBEDDING_DIM`.
        """
        import requests

        if not gateway_embeddings_enabled():
            raise CommandError(
                "AI_GATEWAY_EMBEDDINGS_ENABLED=0 : activer le chemin gouverné avant de "
                "configurer un espace dessus (sinon toute indexation échouerait)."
            )
        base = gateway_embeddings_base_url()
        if not base:
            raise CommandError("AI_GATEWAY_EMBEDDINGS_BASE_URL est vide.")

        try:
            resp = requests.get(f"{base}/health/embeddings", timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise CommandError(f"Gateway d'embeddings injoignable: {type(exc).__name__}") from exc

        profiles = {m.get("model_logical"): m for m in (data.get("models") or [])}
        profile = profiles.get(model)
        if profile is None:
            raise CommandError(
                f"Modèle logique {model!r} inconnu du registre Gateway "
                f"(connus: {', '.join(sorted(k for k in profiles if k))})."
            )
        if not profile.get("allowed_here"):
            raise CommandError(
                f"Modèle {model!r} NON AUTORISÉ dans l'environnement Gateway "
                f"{data.get('environment')!r}."
            )
        if profile.get("state") != "ready" or profile.get("registry_status") != "ready":
            raise CommandError(
                f"Modèle {model!r} non prêt (state={profile.get('state')!r}, "
                f"registry_status={profile.get('registry_status')!r})."
            )
        expected_dim = int(getattr(settings, "RAG_EMBEDDING_DIM", 768))
        if int(profile.get("dimension") or 0) != expected_dim:
            raise CommandError(
                f"Dimension {profile.get('dimension')!r} != RAG_EMBEDDING_DIM={expected_dim}. "
                "Une migration dimensionnelle est hors périmètre de ce lot."
            )
        if profile.get("expected_digest") != profile.get("observed_digest"):
            raise CommandError(
                "Digest attendu != digest observé côté Gateway : les poids ont changé "
                "sans que le registre le sache. Ne pas migrer."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Gateway OK : env={data.get('environment')} model={model} "
                f"dim={profile.get('dimension')} digest={str(profile.get('observed_digest'))[:8]} "
                f"max_input_chars={profile.get('max_input_chars')} "
                f"on_overflow={profile.get('on_overflow')}"
            )
        )
