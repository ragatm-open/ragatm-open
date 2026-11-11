from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.llm_router.egress import LlmExecutionContext
from apps.rag_core.models import Document, DocumentStatus, RagSpace


class Command(BaseCommand):
    help = (
        "Re-embed existing documents with the currently configured EMBEDDINGS_PROVIDER. "
        "Use after switching off the 'stub' provider so chunks get real semantic vectors. "
        "By default it enqueues reindex_document_task (Celery); use --sync to run inline."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--rag-space",
            dest="rag_space",
            default=None,
            help="RagSpace ciblé (UUID). OBLIGATOIRE, sauf `--all-spaces` explicite.",
        )
        parser.add_argument(
            "--all-spaces",
            action="store_true",
            help=(
                "Autorise une exécution GLOBALE (tous les espaces). Auparavant c'était "
                "le comportement par défaut quand `--rag-space` était omis : une faute "
                "de frappe suffisait à relancer l'ingestion de tout le parc. La "
                "capacité est conservée, elle n'est plus implicite (lot 3, §15)."
            ),
        )
        # --- Ciblage du pipeline GOUVERNÉ (lot 3) ---------------------------
        parser.add_argument(
            "--provider",
            dest="provider",
            default=None,
            choices=["ai_gateway"],
            help=(
                "Provider ATTENDU pour cet espace. N'écrit rien : c'est une ASSERTION. "
                "Si la configuration de l'espace diffère, la commande refuse — plutôt "
                "que de réindexer avec un pipeline que l'opérateur ne croit pas utiliser."
            ),
        )
        parser.add_argument(
            "--model",
            dest="model",
            default=None,
            help="Modèle logique ATTENDU. Même sémantique d'assertion que `--provider`.",
        )
        parser.add_argument(
            "--document",
            dest="document",
            default=None,
            help="Limiter à UN document (UUID). Utile pour la reprise après échec.",
        )
        parser.add_argument(
            "--exclude-document",
            dest="exclude_documents",
            action="append",
            default=[],
            metavar="UUID",
            help=(
                "Retire un document du PÉRIMÈTRE (répétable). Le document n'est ni "
                "réindexé, ni réétiqueté : ses chunks gardent leur `pipeline_id` "
                "actuel (NULL pour un chunk historique). Sert aux documents dont on "
                "SAIT qu'ils seraient refusés — un classeur dont les fragments "
                "tabulaires dépassent la fenêtre du modèle. Les exclure est un acte "
                "nommé et tracé ; les laisser échouer serait un incident silencieux "
                "à trier après coup."
            ),
        )
        parser.add_argument(
            "--status",
            dest="status",
            default=DocumentStatus.READY,
            choices=[c[0] for c in DocumentStatus.choices],
            help="Document status to re-embed (default: READY).",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Run reindex inline in this process instead of enqueuing Celery tasks.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List the documents that would be re-embedded without doing anything.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Process at most N documents (0 = no limit).",
        )
        parser.add_argument(
            "--from-chunks",
            action="store_true",
            help=(
                "Ré-embarque depuis le TEXTE DES CHUNKS déjà stockés, sans re-extraire le "
                "fichier source. Indispensable quand storage_file est absent "
                "(no_storage_file_for_reindex) : le chemin normal ré-extrait le fichier "
                "original, impossible pour les documents ingérés sans conservation."
            ),
        )
        parser.add_argument(
            "--continue-with-missing",
            action="store_true",
            help=(
                "Poursuit malgré des documents dont la source est introuvable. Ceux-ci "
                "sont IGNORÉS — jamais réindexés, jamais déclarés conformes : leurs "
                "chunks gardent leur `pipeline_id` actuel. Sans ce drapeau, la commande "
                "refuse de lancer des opérations vouées à l'échec."
            ),
        )
        parser.add_argument(
            "--actor-user-id",
            type=int,
            default=None,
            help=(
                "ID de l'acteur de SERVICE pour la revalidation owner_scope à l'exécution "
                "(Lot 3). Requis une fois OWNER_SCOPE_ENABLED actif (sinon la tâche est "
                "refusée : jamais de None comme bypass). Sans effet flags OFF."
            ),
        )

    def _sources_missing(self, doc_ids) -> list[str]:
        """UUID des documents dont le fichier source est introuvable.

        Ne duplique PAS l'audit global (`audit_media_storage`) : celui-ci balaie
        tout le stockage, ici on ne regarde que le périmètre sélectionné, juste
        avant de travailler. Le test passe par l'API de stockage, jamais par
        ``os.path`` — cf. ``documents/source_availability``.
        """
        from apps.rag_core.services.documents.source_availability import source_file_exists

        missing: list[str] = []
        for doc in Document.objects.filter(id__in=list(doc_ids)).only("id", "storage_file"):
            if not source_file_exists(doc):
                missing.append(str(doc.id))
        return missing

    def handle(self, *args, **opts):
        rag_space_id = opts["rag_space"]
        status = opts["status"]
        sync = opts["sync"]
        dry_run = opts["dry_run"]
        limit = int(opts["limit"] or 0)
        from_chunks = opts.get("from_chunks", False)

        # --- Ciblage EXPLICITE (lot 3, §15) ---------------------------------
        if not rag_space_id and not opts.get("all_spaces"):
            raise CommandError(
                "--rag-space est obligatoire. Pour une réindexation GLOBALE délibérée, "
                "passer --all-spaces (une exécution globale ne doit jamais être le "
                "résultat d'un argument oublié)."
            )
        if rag_space_id and opts.get("all_spaces"):
            raise CommandError("--rag-space et --all-spaces sont mutuellement exclusifs.")

        space = None
        if rag_space_id:
            space = RagSpace.objects.filter(id=rag_space_id).first()
            if space is None:
                raise CommandError(f"RagSpace not found: {rag_space_id}")

        # Assertions de pipeline : on refuse de réindexer un espace dont la
        # configuration n'est pas celle que l'opérateur croit.
        expected_pipeline = self._preflight(space, opts)

        qs = Document.objects.filter(status=status)
        if rag_space_id:
            qs = qs.filter(rag_space_id=rag_space_id)
        if opts.get("document"):
            qs = qs.filter(id=opts["document"])
            if not qs.exists():
                raise CommandError(f"Document introuvable (ou statut != {status}): {opts['document']}")

        # --- Exclusions EXPLICITES ------------------------------------------
        # Validées AVANT toute exécution : un UUID mal recopié doit faire échouer la
        # commande, jamais élargir silencieusement le périmètre à un document qu'on
        # croyait exclu.
        excluded_ids = [str(x).strip() for x in (opts.get("exclude_documents") or []) if str(x).strip()]
        excluded_rows = []
        if excluded_ids:
            found = {
                str(d["id"]): d["filename"]
                for d in Document.objects.filter(id__in=excluded_ids).values("id", "filename")
            }
            unknown = [x for x in excluded_ids if x not in found]
            if unknown:
                raise CommandError(
                    "--exclude-document : document(s) introuvable(s) : " + ", ".join(unknown)
                )
            if rag_space_id:
                hors = list(
                    Document.objects.filter(id__in=excluded_ids)
                    .exclude(rag_space_id=rag_space_id)
                    .values_list("id", flat=True)
                )
                if hors:
                    raise CommandError(
                        "--exclude-document : document(s) hors de l'espace ciblé : "
                        + ", ".join(str(x) for x in hors)
                    )
            excluded_rows = [(k, found[k]) for k in excluded_ids]
            qs = qs.exclude(id__in=excluded_ids)

        doc_ids = list(qs.order_by("created_at").values_list("id", flat=True))
        if limit > 0:
            doc_ids = doc_ids[:limit]

        total = len(doc_ids)
        if total == 0:
            self.stdout.write(self.style.WARNING("No matching documents to re-embed."))
            return

        # --- Résumé AVANT exécution ------------------------------------------
        from apps.rag_core.models import Chunk

        n_chunks = Chunk.objects.filter(document_id__in=doc_ids).count()
        self.stdout.write(
            f"{'[dry-run] ' if dry_run else ''}"
            f"Re-embedding {total} document(s) / {n_chunks} chunk(s) actuels "
            f"(status={status}, mode={'sync' if sync else 'celery'})."
        )
        if space is not None:
            self.stdout.write(
                f"  espace   : {getattr(space, 'name', '')} ({space.id})\n"
                f"  provider : {space.embedding_provider or '—'}\n"
                f"  modèle   : {space.embedding_model or '—'}\n"
                f"  pipeline attendu : {expected_pipeline or '(déterminé à la première réponse Gateway)'}"
            )
        if excluded_rows:
            self.stdout.write(
                f"  EXCLUS du périmètre : {len(excluded_rows)} document(s) — "
                "ni réindexés, ni réétiquetés (pipeline_id inchangé) :"
            )
            for doc_id, fn in excluded_rows:
                self.stdout.write(f"    - {doc_id}  {fn}")

        # --- PRÉ-VOL : disponibilité des sources ------------------------------
        # `--from-chunks` ré-embarque depuis le texte déjà stocké : il ne lit aucun
        # fichier, le pré-vol n'a donc pas lieu d'être sur ce chemin.
        missing_ids: list[str] = []
        if not from_chunks:
            missing_ids = self._sources_missing(doc_ids)
            self.stdout.write(
                "  pré-vol sources : "
                f"sélectionnés={total} présents={total - len(missing_ids)} "
                f"introuvables={len(missing_ids)}"
            )
            if missing_ids and dry_run:
                # Un `--dry-run` ne lance rien : il n'y a rien à refuser. Il
                # DOIT en revanche nommer les documents concernés — c'est le
                # moment où l'opérateur découvre le problème sans rien casser.
                self.stdout.write(
                    self.style.WARNING(
                        f"  [dry-run] {len(missing_ids)} document(s) sans fichier source — "
                        "une exécution réelle serait refusée sans --continue-with-missing :"
                    )
                )
                for doc_id in missing_ids:
                    self.stdout.write(f"    - {doc_id}  (source_file_missing)")
            elif missing_ids and not opts.get("continue_with_missing"):
                # Refus AVANT tout travail : lancer N réindexations dont on sait
                # déjà qu'elles échoueront produit N documents FAILED et aucun
                # progrès. L'opérateur tranche explicitement.
                raise CommandError(
                    f"{len(missing_ids)} document(s) sur {total} n'ont plus de fichier "
                    "source dans le stockage : la réindexation échouerait pour chacun "
                    "(source_file_missing). Fournir les sources à nouveau, restreindre "
                    "le périmètre, ou passer --continue-with-missing pour les IGNORER "
                    "explicitement (ils ne seront ni réindexés ni réétiquetés)."
                )
            if missing_ids and not dry_run:
                self.stdout.write(
                    self.style.WARNING(
                        f"  --continue-with-missing : {len(missing_ids)} document(s) IGNORÉS "
                        "(source introuvable). Ni réindexés, ni réétiquetés."
                    )
                )
                for doc_id in missing_ids:
                    self.stdout.write(f"    - {doc_id}  (source_file_missing)")
                skipped = set(missing_ids)
                doc_ids = [d for d in doc_ids if str(d) not in skipped]
                total = len(doc_ids)
                if total == 0:
                    self.stdout.write(
                        self.style.WARNING("Aucun document exploitable après pré-vol.")
                    )
                    return

        if dry_run:
            for doc_id in doc_ids:
                self.stdout.write(f"  - {doc_id}")
            self.stdout.write(self.style.WARNING("[dry-run] aucune écriture, aucun appel d'embedding."))
            return

        if from_chunks:
            ok, failed = self._reembed_from_chunks(doc_ids)
            self.stdout.write(
                self.style.SUCCESS(f"Done (--from-chunks). reembedded={ok} failed={failed} total={total}")
            )
            if failed:
                raise CommandError(f"{failed} document(s) en échec de ré-embedding.")
            return

        ok = 0
        failed = 0
        for doc_id in doc_ids:
            try:
                if sync:
                    # Inline path: no Celery worker required.
                    from apps.rag_core.services.ingest_document import reindex_existing_document

                    res = reindex_existing_document(document_id=str(doc_id))
                    if getattr(res, "status", None) == DocumentStatus.READY:
                        ok += 1
                        self.stdout.write(self.style.SUCCESS(f"  OK   {doc_id} chunks={res.chunks_created}"))
                    else:
                        failed += 1
                        self.stdout.write(self.style.ERROR(f"  FAIL {doc_id} status={getattr(res, 'status', '?')}"))
                else:
                    from apps.rag_core.services.ingest.queues import ingest_queue_for
                    from apps.rag_core.tasks import reindex_document_task

                    task_kwargs = dict(
                        document_id=str(doc_id),
                        access_context={"user_id": opts.get("actor_user_id"), "request_id": "reembed_command"},
                    )
                    # File DÉDIÉE par document (IFC) : même isolation
                    # que l'upload et les jobs de réindexation.
                    filename = Document.objects.filter(id=doc_id).values_list("filename", flat=True).first()
                    from apps.rag_core.services.ingest.enqueue_token import stamp_enqueue_token

                    task_kwargs["enqueue_token"] = stamp_enqueue_token(doc_id)
                    queue = ingest_queue_for(filename=filename)
                    if queue:
                        reindex_document_task.apply_async(kwargs=task_kwargs, queue=queue)
                    else:
                        reindex_document_task.delay(**task_kwargs)
                    ok += 1
                    self.stdout.write(f"  queued {doc_id}")
            except Exception as e:  # noqa: BLE001 — report and continue across the batch
                failed += 1
                self.stdout.write(self.style.ERROR(f"  ERROR {doc_id}: {e}"))

        summary = f"Done. {'reindexed' if sync else 'queued'}={ok} failed={failed} total={total}"
        self.stdout.write(self.style.SUCCESS(summary) if failed == 0 else self.style.WARNING(summary))
        self._report_coverage(space, excluded_ids)

    # ------------------------------------------------------------------ helpers
    def _preflight(self, space, opts) -> str:
        """Vérifie que l'espace est bien celui qu'on croit, et que la cible est prête.

        Renvoie la famille de pipeline attendue (vide si indéterminable sans appel).
        Refuse — jamais d'avertissement — dans les cas du §15 : Gateway non prête,
        modèle non autorisé, dimension divergente.
        """
        want_provider = (opts.get("provider") or "").strip().lower()
        want_model = (opts.get("model") or "").strip()
        if not (want_provider or want_model):
            return ""
        if space is None:
            raise CommandError("--provider/--model exigent --rag-space (assertion par espace).")

        got_provider = (space.embedding_provider or "").strip().lower()
        got_model = (space.embedding_model or "").strip()
        if want_provider and want_provider != got_provider:
            raise CommandError(
                f"provider attendu {want_provider!r} != configuré {got_provider!r} sur cet espace. "
                "Configurer d'abord avec `set_ragspace_embedding_pipeline`."
            )
        if want_model and want_model != got_model:
            raise CommandError(
                f"modèle attendu {want_model!r} != configuré {got_model!r} sur cet espace."
            )

        from apps.llm_router.embeddings import space_is_governed

        if not space_is_governed(space):
            return ""

        # Espace gouverné : on demande son identité à la Gateway AVANT de réindexer.
        # Échouer ici coûte une commande ; échouer à mi-parcours coûte un espace à
        # moitié migré.
        from apps.rag_core.services.embedding_pipeline_coverage import expected_pipeline_family

        try:
            family = expected_pipeline_family(space, probe=True)
        except Exception as exc:
            raise CommandError(
                f"Gateway d'embeddings indisponible ou refusante ({type(exc).__name__}: {exc}). "
                "Réindexation refusée."
            ) from exc
        if not family:
            raise CommandError("La Gateway n'a pas rendu de famille de pipeline exploitable.")
        return family

    def _report_coverage(self, space, excluded_ids=None) -> None:
        """Couverture APRÈS exécution — la seule preuve qu'une migration est complète.

        Calculée sur le PÉRIMÈTRE DÉCLARÉ : les documents exclus sont comptés à part,
        jamais fondus dans le verdict ni effacés du décompte.
        """
        if space is None:
            return
        from apps.rag_core.services.embedding_pipeline_coverage import compute_coverage

        cov = compute_coverage(space, excluded_document_ids=excluded_ids)
        self.stdout.write("")
        self.stdout.write("Couverture de pipeline après exécution :")
        for k, v in cov.to_dict().items():
            self.stdout.write(f"  {k}: {v}")
        if cov.governed and not cov.is_pilot_acceptable:
            self.stdout.write(
                self.style.WARNING(
                    "Couverture INCOMPLÈTE : ne pas déclarer le pilote réussi. "
                    "Relancer sur les documents restants (`--document`), ou investiguer."
                )
            )

    def _reembed_from_chunks(self, doc_ids) -> tuple[int, int]:
        """Ré-embarque les chunks EXISTANTS depuis leur texte stocké (sans le fichier).

        Écrit aussi l'empreinte provider/model sur le document : sans elle, la clause
        de cohérence de ``search_chunks_vector`` exclut le document du leg vectoriel —
        l'index paraît peuplé mais le sémantique est inerte.
        """
        from django.conf import settings

        from django.db import transaction

        from apps.llm_router.embedding_pipeline import TASK_DOCUMENT
        from apps.llm_router.embeddings import embed_texts_with_identity, resolve_space_embedding
        from apps.rag_core.models import Chunk

        dim = int(getattr(settings, "RAG_EMBEDDING_DIM", 768))
        batch_size = max(1, int(getattr(settings, "RAG_EMBED_BATCH_SIZE", 64)))
        ok = failed = 0
        for doc_id in doc_ids:
            try:
                doc = Document.objects.select_related("rag_space").get(id=doc_id)
                chunks = list(Chunk.objects.filter(document=doc).order_by("chunk_index"))
                texts = [(c.text or "").strip() for c in chunks]
                if not chunks or not any(texts):
                    failed += 1
                    self.stdout.write(self.style.ERROR(f"  FAIL {doc_id}: aucun chunk texte"))
                    continue

                provider, model = resolve_space_embedding(doc.rag_space)
                # Réindexation = ré-ENVOI de tout le contenu du document. C'est le
                # chemin qui expédierait le fonds documentaire entier si un provider
                # externe existait. Il obéit à la politique de l'espace comme
                # l'ingestion initiale.
                emb_ctx = LlmExecutionContext.for_rag_space(
                    doc.rag_space, operation="embedding.document"
                )
                # Lot 3 : lots BORNÉS (la Gateway refuse > 64 textes) et identité de
                # pipeline relevée. Ce chemin ré-embarquait auparavant en un seul appel
                # et n'écrivait aucun `pipeline_id` : sur un espace gouverné, il aurait
                # produit des vecteurs neufs sous une identité périmée — invisibles au
                # retrieval, ou pire, comparés à tort.
                vectors: list[list[float]] = []
                doc_pipeline_id: str | None = None
                doc_family: str | None = None
                for i in range(0, len(texts), batch_size):
                    emb_batch = embed_texts_with_identity(
                        texts[i:i + batch_size],
                        dim=dim, provider=provider, model=model,
                        task=TASK_DOCUMENT,
                        rag_space_id=getattr(doc.rag_space, "id", None),
                        document_id=doc.id,
                        context=emb_ctx,
                    )
                    if doc_pipeline_id is None:
                        doc_pipeline_id = emb_batch.pipeline_id
                        # Famille portée par le lot (attestée), jamais découpée de l'identifiant.
                        doc_family = emb_batch.family or None
                    elif (emb_batch.pipeline_id != doc_pipeline_id
                          or (emb_batch.family or None) != doc_family):
                        raise RuntimeError("embedding_pipeline_changed_mid_document")
                    vectors.extend(emb_batch.vectors)

                if len(vectors) != len(chunks):
                    raise RuntimeError(f"{len(vectors)} vecteurs pour {len(chunks)} chunks")
                for v in vectors:
                    if len(v) != dim:
                        raise RuntimeError(f"dimension {len(v)} != RAG_EMBEDDING_DIM {dim}")

                # Écriture ATOMIQUE : un document à moitié ré-embarqué mélangerait deux
                # pipelines dans un même document, ce que l'indexation nominale refuse.
                with transaction.atomic():
                    for c, v in zip(chunks, vectors):
                        c.embedding = v
                        c.pipeline_id = doc_pipeline_id or None
                        c.pipeline_family = doc_family
                        c.save(update_fields=["embedding", "pipeline_id", "pipeline_family"])

                    Document.objects.filter(id=doc.id).update(
                        embedding_provider=(provider or "").strip().lower(),
                        embedding_model=(model or "").strip(),
                        reindex_required=False,
                    )
                ok += 1
                self.stdout.write(self.style.SUCCESS(f"  OK   {doc_id} chunks={len(chunks)}"))
            except Exception as e:  # noqa: BLE001 — on rapporte et on continue le lot
                failed += 1
                self.stdout.write(self.style.ERROR(f"  ERROR {doc_id}: {type(e).__name__}: {e}"))
        return ok, failed
