# backend/apps/rag_core/services/ingest/queues.py
"""
Routage PAR DOCUMENT des tâches d'ingestion vers une file Celery dédiée.

`CELERY_TASK_ROUTES` route par NOM de tâche ; ici la même tâche (`ingest_document_task`,
`reindex_job_item_task`, `reindex_document_task`) doit changer de file selon le document.
Une maquette IFC coûte ~115 Mo RSS d'import IfcOpenShell par processus, des secondes de
parsing, et un crash natif du parseur n'est pas rattrapable en Python : elle ne doit
jamais occuper un slot du worker interactif (`celery`) ni du worker `reindex`.

Contrat :
- `ingest_queue_for(filename=…)` renvoie la file dédiée du format quand sa porte est
  ouverte, sinon `None` (= file par défaut de la tâche) : `ingest_ifc` pour un `.ifc`
  (`DOCUMENT_IFC_ENABLED`), `ingest_dwg` pour un `.dwg` (`DOCUMENT_DWG_ENABLED`,
  conversion LibreDWG en sous-processus, isolée du worker général),
  `ingest_visual` pour une image autonome quand l'analyse visuelle est ACTIVE pour la
  source `image` (appel au Gateway vision, une tâche = un document,
  visuels traités séquentiellement, aucune tâche fille). Le critère est l'extension,
  exactement celui qui gouverne la détection ;
- tous les points d'enfilement passent par cet helper : upload, reindex unitaire,
  items des jobs de réindexation, commande `reembed_documents` ;
- les relances `self.retry()` restent sur la file d'origine (Celery recopie
  `exchange`/`routing_key` du `delivery_info`) ;
- EXPLOITATION : `DOCUMENT_IFC_ENABLED` ne doit pas être activé tant qu'aucun worker
  `--queues=ingest_ifc` ne tourne (concurrence 1 recommandée) — sinon les maquettes
  restent PENDING. Même règle pour `DOCUMENT_VISUAL_ANALYSIS_ENABLED` et un worker
  `--queues=ingest_visual` (concurrence 1 recommandée), qu'aucun déploiement ne déclare
  encore. Garde statique : `tests/tests_root/deploy/test_celery_queue_consumers.py`.
- `ingest_visual` accueille AUSSI le PDF depuis une version antérieure, le DOCX depuis
 et le PPTX depuis une version antérieure, quand l'analyse visuelle
  est active pour la source correspondante :
  la découverte des visuels intégrés est suivie d'appels Gateway, qui n'ont leur place
  ni dans une requête HTTP ni sur le worker interactif. Chaque source a sa PROPRE
  porte : ouvrir `image` ne route ni les PDF, ni les DOCX, ni les PPTX.
- VISUAL-ENRICHMENT-DEFERRED : un PDF, DOCX ou PPTX n'est PLUS routé vers `ingest_visual`
  pour son ingestion PRIMAIRE (texte, OCR, tableaux) : elle part sur la file par défaut
  et le document devient READY sans attendre la vision. `ingest_visual` reçoit :
  l'image autonome (la vision y est l'évidence PRIMAIRE), la reprise PRIMAIRE d'un
  PDF/DOCX/PPTX sans autre évidence que ses visuels (`visual_primary=True`), et la tâche
  d'enrichissement visuel différé (`visual_enrichment_task`).
"""
from __future__ import annotations

INGEST_IFC_QUEUE = "ingest_ifc"
INGEST_DWG_QUEUE = "ingest_dwg"
INGEST_VISUAL_QUEUE = "ingest_visual"
INGEST_AUDIO_QUEUE = "ingest_audio"

#: Extensions d'image autonome (celles que la détection d'image peut accepter).
_IMAGE_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif")

#: Extension -> (file dédiée, nom de la porte dans `extraction.config`).
_DEDICATED: tuple[tuple[str, str, str], ...] = (
    (".ifc", INGEST_IFC_QUEUE, "ifc_enabled"),
    (".dwg", INGEST_DWG_QUEUE, "dwg_enabled"),
    *((suffix, INGEST_VISUAL_QUEUE, "visual_image_analysis_active") for suffix in _IMAGE_SUFFIXES),
    # Audio : l'ingestion d'un audio part sur le worker qui porte ffmpeg ;
    # la chaîne ASR elle-même y est toujours enfilée (`audio.orchestrator.AUDIO_QUEUE`).
    *((suffix, INGEST_AUDIO_QUEUE, "audio_ingest_enabled") for suffix in (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".webm")),
)

#: Sources à évidence visuelle SECONDAIRE : leur passe primaire ne va jamais sur
#: `ingest_visual` ; seule leur reprise `visual_primary` y va (porte de la source).
_DEFERRED_VISUAL: tuple[tuple[str, str], ...] = (
    (".pdf", "visual_pdf_analysis_active"),
    (".docx", "visual_docx_analysis_active"),
    (".pptx", "visual_pptx_analysis_active"),
)


def ingest_queue_for(*, filename: str | None, visual_primary: bool = False) -> str | None:
    """File dédiée pour CE document, ou None (file par défaut de la tâche).

    ``visual_primary`` : reprise PRIMAIRE d'un document dont la seule évidence est
    visuelle — elle, et elle seule, va sur `ingest_visual` pour un PDF/DOCX/PPTX.
    """
    name = str(filename or "").lower()
    if not name:
        return None
    from .extraction import config

    for suffix, queue, gate in _DEDICATED:
        if name.endswith(suffix):
            return queue if bool(getattr(config, gate)()) else None
    if visual_primary:
        for suffix, gate in _DEFERRED_VISUAL:
            if name.endswith(suffix) and bool(getattr(config, gate)()):
                return INGEST_VISUAL_QUEUE
    return None
