"""Renseigne les empreintes d'indexation des documents corpus — SUR PREUVE UNIQUEMENT.

    python manage.py backfill_corpus_embedding_fingerprints --inventory
    python manage.py backfill_corpus_embedding_fingerprints --corpus refvc --dry-run
    python manage.py backfill_corpus_embedding_fingerprints --corpus refvc --apply

Pourquoi une commande, et pourquoi une preuve
---------------------------------------------
La migration de schéma n'écrit aucune empreinte. Attribuer aux documents historiques
l'identité *déclarée aujourd'hui* par le corpus serait exactement la supposition que ce
lot cherche à rendre impossible : elle transformerait une ignorance en certitude, et
c'est précisément ainsi qu'une comparaison croisée devient invisible.

La preuve est donc la même que celle de ``set_corpus_embedding_model`` : on réencode
quelques chunks RÉELS du document avec le couple annoncé et on exige une similarité
cosinus ≥ 0,999 avec les vecteurs stockés. Deux encodages du même texte par le même
modèle sont identiques à la précision flottante près ; un modèle différent descend très
en dessous.

Trois verdicts, trois traitements
---------------------------------
``proven_consistent``   : l'empreinte est écrite, ``reindex_required=False``.
``proven_inconsistent`` : les vecteurs ne viennent PAS du couple déclaré —
                          ``reindex_required=True``, aucune empreinte écrite.
``unknown``             : la preuve n'a pas pu être faite (provider injoignable, aucun
                          chunk vectorisé…) — ``reindex_required=True`` par prudence.

Aucun embedding n'est modifié, aucun chunk supprimé, aucune calibration touchée.
"""
from __future__ import annotations

import math

from django.core.management.base import BaseCommand, CommandError

#: Similarité minimale entre un vecteur stocké et un vecteur recalculé pour conclure
#: que le couple annoncé est bien celui d'origine. Volontairement exigeante.
IDENTITY_COSINE_THRESHOLD = 0.999

#: Chunks échantillonnés par document. Plus d'un : une coïncidence sur un seul chunk ne
#: prouverait rien.
SAMPLE_SIZE = 3

PROVEN_CONSISTENT = "proven_consistent"
PROVEN_INCONSISTENT = "proven_inconsistent"
UNKNOWN = "unknown"


def _cosine(a, b) -> float:
    num = sum(float(x) * float(y) for x, y in zip(a, b))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    return num / (na * nb) if na and nb else 0.0


class Command(BaseCommand):
    help = (
        "Renseigne DomainCorpusDocument.embedding_* après avoir PROUVÉ, par comparaison "
        "cosinus sur des chunks réels, que les vecteurs viennent du couple déclaré."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--corpus", default=None, help="slug du corpus (défaut : tous)")
        parser.add_argument("--inventory", action="store_true", help="état actuel, aucune mesure")
        parser.add_argument("--dry-run", action="store_true", help="mesure et classe, n'écrit rien")
        parser.add_argument("--apply", action="store_true", help="écrit les documents prouvés")
        parser.add_argument("--sample-size", type=int, default=SAMPLE_SIZE)

    def handle(self, *args, **opts) -> None:
        from apps.corpus.models import DomainCorpus

        corpora = DomainCorpus.objects.all().order_by("slug")
        if opts["corpus"]:
            corpora = corpora.filter(slug=opts["corpus"])
            if not corpora.exists():
                raise CommandError(f"corpus introuvable : {opts['corpus']!r}")

        if opts["inventory"]:
            self._inventory(corpora)
            return
        if opts["apply"] and opts["dry_run"]:
            raise CommandError("--apply et --dry-run sont exclusifs.")

        totals = {PROVEN_CONSISTENT: 0, PROVEN_INCONSISTENT: 0, UNKNOWN: 0}
        written = marked = 0
        for corpus in corpora:
            counts, w, m = self._process(corpus, apply=bool(opts["apply"]),
                                         sample_size=int(opts["sample_size"]))
            for key, value in counts.items():
                totals[key] += value
            written += w
            marked += m

        self.stdout.write("")
        self.stdout.write(
            f"TOTAL  prouvés cohérents={totals[PROVEN_CONSISTENT]}  "
            f"prouvés incohérents={totals[PROVEN_INCONSISTENT]}  inconnus={totals[UNKNOWN]}"
        )
        if opts["apply"]:
            self.stdout.write(f"Écrit : {written} empreintes, {marked} documents marqués à réindexer.")
            self.stdout.write(
                "Rollback : aucune donnée n'a été détruite. Vider les colonnes d'empreinte "
                "et remettre reindex_required=False restaure l'état antérieur."
            )
        else:
            self.stdout.write("Aucune écriture (mode plan). Relancer avec --apply pour persister.")

    # -- inventaire ---------------------------------------------------------
    def _inventory(self, corpora) -> None:
        from apps.corpus.services.embedding_consistency import (
            active_calibration_state,
            consistency_report,
            embedding_dimension,
        )

        self.stdout.write(f"Dimension attendue : {embedding_dimension()}")
        self.stdout.write(
            f"{'corpus':<24}{'identité déclarée':<44}{'prêts':>7}{'cohér.':>8}"
            f"{'incohér.':>10}{'inconnus':>10}{'réindex':>9}  statut"
        )
        for corpus in corpora:
            report = consistency_report(corpus)
            valid, code, version = active_calibration_state(corpus)
            identity = f"{corpus.embedding_provider or '—'}/{corpus.embedding_model or '—'}"
            self.stdout.write(
                f"{corpus.slug:<24}{identity:<44}{report.ready:>7}{report.compatible:>8}"
                f"{report.incompatible:>10}{report.unknown:>10}"
                f"{report.reindex_required:>9}  {report.status}"
            )
            self.stdout.write(
                f"{'':<24}calibration : "
                + (f"v{version} valide" if valid else f"{code or 'aucune'}")
            )

    # -- traitement ---------------------------------------------------------
    def _process(self, corpus, *, apply: bool, sample_size: int) -> tuple[dict, int, int]:
        from apps.corpus.domain.embedding_identity import (
            CorpusEmbeddingIdentityError,
            EmbeddingIdentity,
            normalize_model,
            normalize_provider,
        )
        from apps.corpus.models import DomainCorpusChunk, DomainCorpusDocument
        from apps.corpus.services.embedding_consistency import embedding_dimension
        from apps.rag_core.domain.eligibility import ELIGIBLE_DOCUMENT_STATUS

        counts = {PROVEN_CONSISTENT: 0, PROVEN_INCONSISTENT: 0, UNKNOWN: 0}
        written = marked = 0
        self.stdout.write("")
        self.stdout.write(f"=== corpus {corpus.slug} ===")

        try:
            identity = EmbeddingIdentity(
                provider=normalize_provider(corpus.embedding_provider),
                model=normalize_model(corpus.embedding_model),
                dimension=embedding_dimension(),
            )
        except CorpusEmbeddingIdentityError as exc:
            # Sans identité déclarée, il n'y a rien à prouver : on ne devine pas ce que
            # le corpus aurait dû être.
            self.stdout.write(f"  identité déclarée inutilisable ({exc.code}) — aucun document traité")
            return counts, 0, 0

        documents = DomainCorpusDocument.objects.filter(
            corpus=corpus, status=ELIGIBLE_DOCUMENT_STATUS
        ).order_by("original_filename", "id")

        for document in documents:
            verdict, detail = self._classify(
                document, identity=identity, sample_size=sample_size,
                chunk_model=DomainCorpusChunk,
            )
            counts[verdict] += 1
            self.stdout.write(f"  {str(document.id)[:8]}… {verdict:<20} {detail}")
            if not apply:
                continue
            if verdict == PROVEN_CONSISTENT:
                DomainCorpusDocument.objects.filter(id=document.id).update(
                    embedding_provider=identity.provider,
                    embedding_model=identity.model,
                    embedding_dimension=identity.dimension,
                    embedding_fingerprint=identity.fingerprint,
                    reindex_required=False,
                )
                written += 1
            else:
                # Ni empreinte, ni certitude : le document reste exploitable en lexical
                # et sort du vectoriel une fois le filtrage activé.
                DomainCorpusDocument.objects.filter(id=document.id).update(
                    reindex_required=True
                )
                marked += 1

        self.stdout.write(
            f"  -> cohérents={counts[PROVEN_CONSISTENT]} "
            f"incohérents={counts[PROVEN_INCONSISTENT]} inconnus={counts[UNKNOWN]}"
        )
        return counts, written, marked

    # -- preuve -------------------------------------------------------------
    def _classify(self, document, *, identity, sample_size: int, chunk_model) -> tuple[str, str]:
        """Réencode des chunks réels et compare. Ne modifie rien."""
        from apps.llm_router.egress import domain_corpus_context
        from apps.llm_router.embeddings import embed_texts

        chunks = list(
            chunk_model.objects.filter(document=document, embedding__isnull=False)
            .order_by("chunk_index", "id")[:max(1, sample_size)]
        )
        if not chunks:
            return UNKNOWN, "aucun chunk vectorisé : rien à prouver"

        stored_dim = len(chunks[0].embedding)
        if stored_dim != identity.dimension:
            # Une dimension différente est une preuve d'incohérence, pas une ignorance.
            return PROVEN_INCONSISTENT, f"dimension stockée {stored_dim} ≠ {identity.dimension}"

        try:
            vectors = embed_texts(
                [c.text for c in chunks], dim=identity.dimension,
                provider=identity.provider, model=identity.model,
                # Réencodage de PREUVE : mêmes octets sortants qu'une indexation,
                # donc même politique. L'identité porte le provider réellement utilisé.
                context=domain_corpus_context(
                    corpus_id=getattr(getattr(document, "corpus", None), "id", ""),
                    provider=identity.provider,
                    operation="embedding.document",
                ),
            )
        except Exception as exc:  # provider injoignable, modèle inconnu, clé absente
            return UNKNOWN, f"encodage impossible : {type(exc).__name__}"

        sims = [_cosine(list(c.embedding), v) for c, v in zip(chunks, vectors)]
        worst = min(sims)
        if all(s >= IDENTITY_COSINE_THRESHOLD for s in sims):
            return PROVEN_CONSISTENT, f"cosinus min {worst:.6f} sur {len(sims)} chunks"
        return PROVEN_INCONSISTENT, f"cosinus min {worst:.6f} < {IDENTITY_COSINE_THRESHOLD}"
