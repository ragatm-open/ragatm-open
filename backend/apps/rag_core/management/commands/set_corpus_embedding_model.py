"""Renseigne le modèle d'embeddings d'un corpus métier, APRÈS l'avoir prouvé.

    python manage.py set_corpus_embedding_model                      # inventaire
    python manage.py set_corpus_embedding_model --corpus-slug refvc \\
        --model text-embedding-3-small                               # vérification
    python manage.py set_corpus_embedding_model --corpus-slug refvc \\
        --model text-embedding-3-small --confirm                     # écriture

Pourquoi une commande plutôt qu'une migration
---------------------------------------------
Une migration qui attribuerait un modèle par défaut à tous les corpus écrirait une
provenance qu'elle ne connaît pas. Or c'est précisément une provenance non fondée qui a
causé l'incident : un corpus sans modèle explicite voyait le sien décidé par des
variables globales, et le leg vectoriel mourait en silence dans l'environnement où
elles différaient.

Cette commande ne devine donc rien. Elle **vérifie** que les vecteurs déjà stockés sont
bien ceux que produit le couple (provider, modèle) annoncé — par comparaison cosinus sur
un échantillon de chunks réels — et n'écrit qu'ensuite. Sans preuve, elle refuse.

Aucune réindexation : on documente le modèle qui a produit les vecteurs, on ne les
recalcule pas.
"""
from __future__ import annotations

import math

from django.core.management.base import BaseCommand, CommandError

#: Similarité cosinus minimale entre un vecteur stocké et un vecteur recalculé pour
#: considérer que le couple annoncé est bien celui d'origine. Deux encodages du même
#: texte par le même modèle sont identiques à la précision flottante près ; un modèle
#: différent descend très en dessous. Le seuil est donc volontairement exigeant.
IDENTITY_COSINE_THRESHOLD = 0.999

#: Nombre de chunks échantillonnés. Plus d'un : une coïncidence sur un seul chunk ne
#: prouverait rien.
SAMPLE_SIZE = 3


def _cosine(a, b) -> float:
    num = sum(float(x) * float(y) for x, y in zip(a, b))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    return num / (na * nb) if na and nb else 0.0


class Command(BaseCommand):
    help = (
        "Renseigne DomainCorpus.embedding_model après avoir prouvé, par comparaison "
        "cosinus sur des chunks réels, que les vecteurs stockés viennent de ce modèle."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--corpus-slug", default=None)
        parser.add_argument("--model", default=None, help="Modèle annoncé, à prouver.")
        parser.add_argument(
            "--provider", default=None,
            help="Provider si le corpus n'en porte pas encore (sinon celui du corpus).",
        )
        parser.add_argument("--confirm", action="store_true", help="Écrit après vérification.")

    def handle(self, *args, **opts) -> None:
        from apps.corpus.models import DomainCorpus

        if not opts["corpus_slug"]:
            self._inventory()
            return

        corpus = DomainCorpus.objects.filter(slug=opts["corpus_slug"]).first()
        if corpus is None:
            raise CommandError(f"corpus introuvable : {opts['corpus_slug']!r}")

        provider = (opts["provider"] or corpus.embedding_provider or "").strip().lower()
        model = (opts["model"] or "").strip()
        if not provider:
            raise CommandError("provider absent : préciser --provider.")
        if not model:
            raise CommandError("--model est obligatoire : il n'y a pas de défaut légitime.")

        ok, detail = self._prove(corpus, provider=provider, model=model)
        for line in detail:
            self.stdout.write(f"  {line}")
        if not ok:
            raise CommandError(
                "REFUS : les vecteurs stockés ne correspondent pas au couple annoncé. "
                "Écrire ce modèle inscrirait une provenance fausse."
            )

        if not opts["confirm"]:
            self.stdout.write(
                f"Vérification OK. Relancer avec --confirm pour écrire "
                f"provider={provider!r} model={model!r}."
            )
            return

        before = (corpus.embedding_provider, corpus.embedding_model)
        corpus.embedding_provider = provider
        corpus.embedding_model = model
        corpus.save(update_fields=["embedding_provider", "embedding_model", "updated_at"])
        self.stdout.write(
            f"Écrit. Avant : provider={before[0]!r} model={before[1]!r} — "
            f"Après : provider={provider!r} model={model!r}"
        )
        self.stdout.write(
            "Rollback : relancer avec les valeurs précédentes "
            "(aucune réindexation n'est nécessaire dans un sens comme dans l'autre)."
        )

    # -- inventaire ---------------------------------------------------------
    def _inventory(self) -> None:
        from django.db.models import Count

        from apps.corpus.models import DomainCorpus

        self.stdout.write("Corpus métier et provenance déclarée de leurs vecteurs :")
        for c in DomainCorpus.objects.annotate(n=Count("chunks")).order_by("slug"):
            flag = ""
            if c.embedding_provider and not (c.embedding_model or "").strip():
                flag = "   <<< modèle MANQUANT : résolution globale, leg vectoriel fragile"
            self.stdout.write(
                f"  {c.slug:<24} chunks={c.n:<6} provider={c.embedding_provider or '—':<10} "
                f"model={c.embedding_model or '—'}{flag}"
            )

    # -- preuve -------------------------------------------------------------
    def _prove(self, corpus, *, provider: str, model: str) -> tuple[bool, list[str]]:
        from apps.corpus.models import DomainCorpusChunk
        from apps.llm_router.egress import domain_corpus_context
        from apps.llm_router.embeddings import embed_texts

        chunks = list(
            DomainCorpusChunk.objects.filter(corpus=corpus, embedding__isnull=False)
            .order_by("id")[:SAMPLE_SIZE]
        )
        if not chunks:
            return False, ["aucun chunk vectorisé : rien à prouver, rien à déclarer"]

        dim = len(chunks[0].embedding)
        try:
            # Le couple PROPOSÉ (et non celui du corpus) est ce qui va être encodé :
            # le contexte doit donc porter ce provider-là, sinon la preuve tournerait
            # sous une politique qui ne décrit pas l'appel réellement émis.
            vectors = embed_texts(
                [c.text for c in chunks], dim=dim, provider=provider, model=model,
                context=domain_corpus_context(
                    corpus_id=getattr(corpus, "id", ""), provider=provider,
                    operation="embedding.document",
                ),
            )
        except Exception as exc:  # provider injoignable, modèle inconnu, clé absente
            return False, [f"encodage impossible : {type(exc).__name__}: {str(exc)[:120]}"]

        lines = [f"dimension stockée : {dim}", f"couple annoncé    : {provider} / {model}"]
        sims = [_cosine(list(c.embedding), v) for c, v in zip(chunks, vectors)]
        for i, sim in enumerate(sims, start=1):
            lines.append(f"chunk {i} : cosinus {sim:.6f}")
        ok = all(sim >= IDENTITY_COSINE_THRESHOLD for sim in sims)
        lines.append(
            f"verdict : {'IDENTIQUE' if ok else 'DIFFÉRENT'} "
            f"(seuil {IDENTITY_COSINE_THRESHOLD})"
        )
        return ok, lines
