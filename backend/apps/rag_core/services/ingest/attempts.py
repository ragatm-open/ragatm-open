# backend/apps/rag_core/services/ingest/attempts.py
"""Début de tentative d'ingestion : `Document.ingest_started_at`.

Le reaper périodique (`reindex_jobs.recover_stale_items`) ne peut distinguer une
ingestion vivante d'une ingestion morte que par l'ÂGE de la tentative en cours. Cet
horodatage est donc écrit dans le MÊME `UPDATE` que `status=INGESTING`, à l'heure de
la base : jamais une écriture séparée, jamais l'horloge du processus.

Une tentative logique = un horodatage stable :

- `new_attempt=True` : le site OUVRE une tentative (exécution d'une tâche Celery, y
  compris une relance — elle repasse par PENDING — ou réindexation synchrone admise
  par HTTP). L'horodatage est toujours renouvelé ;
- `new_attempt=False` : le site est INTERNE à une tentative éventuellement déjà
  ouverte (réindexation, TX1 de l'indexation). Si la ligne est déjà INGESTING avec un
  horodatage, il est conservé ; sinon (upload synchrone, item de job depuis
  READY/FAILED), la tentative commence ici.

Le `CASE` est évalué sur la ligne AVANT mise à jour (sémantique SQL de `UPDATE … SET`).

Erreur ACTIVE d'une tentative antérieure (lot STALE-ERROR-METADATA)
-------------------------------------------------------------------
Les marqueurs d'échec écrits dans `Document.meta` par le règlement terminal d'une
tentative (`extraction.error_code`, `extraction.extraction_status` ∈ {failed, rejected},
`duplicate_of`) appartiennent à CETTE tentative. Une nouvelle tentative les retire dans
le MÊME `UPDATE` que `status=INGESTING` : il n'existe aucune fenêtre où la ligne est
INGESTING avec l'ancien code encore actif, et aucune lecture-modification-écriture
(l'expression est évaluée côté serveur sur la ligne courante). Les métadonnées valides
(`extraction.method/pages/warnings`, `extraction_status` ∈ {success, partial} d'un
index publié, `import`, `ingest_enqueue`, `visual_enrichment`…) sont conservées.
L'historique reste dans `ActionAuditEvent` ; `Document.error_message`, réécrit à chaque
règlement, reste l'autorité de la tentative courante.
"""
from __future__ import annotations

from django.db.models import Case, DateTimeField, F, Func, When
from django.db.models.fields.json import JSONField
from django.db.models.functions import Now

from apps.rag_core.models.document import DocumentStatus


#: Valeurs de `meta.extraction.extraction_status` qui ne décrivent JAMAIS un index publié :
#: elles sont écrites par le règlement d'une tentative échouée et périment avec elle.
#: `success` / `partial` décrivent l'index actif et sont conservés.
STALE_FAILURE_EXTRACTION_STATUSES: tuple[str, ...] = ("failed", "rejected")


class ResetStaleErrorMetadata(Func):
    """`meta` débarrassé des marqueurs d'erreur ACTIVE d'une tentative antérieure.

    Expression PostgreSQL (jsonb) à passer dans le même `UPDATE` que `status=INGESTING`
    (argument : `F("meta")`, évalué UNE fois dans une sous-requête scalaire). Formes
    tolérées sans exception, rendues INCHANGÉES : `meta` NULL, scalaire ou tableau ;
    `extraction` absent ou non-objet (seul `duplicate_of` est alors retiré).
    Miroir Python exact : `strip_stale_error_metadata`.
    """

    arity = 1
    output_field = JSONField()
    template = (
        "(SELECT CASE"
        " WHEN jsonb_typeof(_m) IS DISTINCT FROM 'object' THEN _m"
        " WHEN jsonb_typeof(_m -> 'extraction') IS DISTINCT FROM 'object' THEN _m #- '{duplicate_of}'"
        " WHEN (_m -> 'extraction' ->> 'extraction_status') IN ('failed', 'rejected')"
        "  THEN _m #- '{extraction,error_code}' #- '{extraction,extraction_status}' #- '{duplicate_of}'"
        " ELSE _m #- '{extraction,error_code}' #- '{duplicate_of}'"
        " END FROM (SELECT (%(expressions)s)::jsonb AS _m) AS _reset_stale_error)"
    )


def strip_stale_error_metadata(meta):
    """Miroir Python EXACT de `ResetStaleErrorMetadata` (parité testée).

    Sert aux tests et aux simulations À BLANC (classer la tentative courante d'une ligne
    historique sans l'écrire). Rend un nouvel objet ; l'argument n'est pas modifié.
    """
    if not isinstance(meta, dict):
        return meta
    out = dict(meta)
    out.pop("duplicate_of", None)
    ext = out.get("extraction")
    if isinstance(ext, dict):
        ext = dict(ext)
        ext.pop("error_code", None)
        if ext.get("extraction_status") in STALE_FAILURE_EXTRACTION_STATUSES:
            ext.pop("extraction_status", None)
        out["extraction"] = ext
    return out


def ingesting_update_fields(*, new_attempt: bool) -> dict:
    """Champs à passer à `QuerySet.update(...)` pour entrer en INGESTING.

    Comprend toujours la neutralisation de l'erreur active périmée (`meta`) : le début
    de tentative et le reset sont UNE transition logique, pour les quatre sites.
    """
    if new_attempt:
        started = Now()
    else:
        started = Case(
            When(
                status=DocumentStatus.INGESTING,
                ingest_started_at__isnull=False,
                then=F("ingest_started_at"),
            ),
            default=Now(),
            output_field=DateTimeField(),
        )
    return {
        "status": DocumentStatus.INGESTING,
        "ingest_started_at": started,
        "meta": ResetStaleErrorMetadata(F("meta")),
    }
