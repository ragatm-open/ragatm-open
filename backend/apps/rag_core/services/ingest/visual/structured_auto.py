"""Extraction structurée AUTOMATIQUE.

Branchée APRÈS l'enrichissement visuel différé d'une génération (``visual_enrichment_task``),
sur la même file ``ingest_visual`` — aucun second pipeline :

    visuels de la génération (asset_hashes de l'état d'enrichissement)
      -> CLASSIFICATION déterministe depuis l'évidence ``describe`` PUBLIÉE (fraîche ou
         reportée par une réindexation) : CHART / TABLE / OTHER / UNKNOWN
      -> CHART -> task=chart, TABLE -> task=table, OTHER/UNKNOWN -> rien
      -> ``structured.extract_asset`` (normalisation + publication existantes, garde de
         génération sous verrou)
      -> confirmation par la mise en page du moteur (``raw_output.layout_boxes``) : sans
         élément ET avec l'autre type détecté, UNE bascule de tâche, jamais plus.

Pourquoi l'évidence ``describe`` : c'est la classification déjà produite pour chaque
visuel (son ``visual_kind``, et à défaut la première phrase de sa description). Le modèle
Vision de staging rend ``photo`` pour tout ; la phrase, elle, nomme l'objet (« un
graphique… », « un tableau… »). Un visuel sans description (échec, décoratif) reste
``UNKNOWN`` : repli documenté = aucune extraction automatique, commande disponible.

GPU occupé (``503 structx_gpu_busy``, Vision encore chargée après le ``describe``) : le
worker ne dort jamais ; l'état est persisté et la tâche se reprogramme
(``apply_async(countdown)``, ``Retry-After`` respecté), au plus ``AUTO_MAX_ATTEMPTS`` fois.
Au-delà : ``failed`` (relance par la commande). Le document n'est jamais touché.

Cycle de vie : une réindexation crée une génération neuve, supprime les fragments
structurés (jamais reportés) et rejoue l'enrichissement, donc ce module ; une tâche dont
la génération n'est plus celle du document ne publie rien (garde sous verrou). La
suppression du document emporte ses fragments (cascade) ; la publication refuse un
document disparu ou un espace supprimé.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

from django.db import transaction
from django.utils import timezone

from . import structured as S

logger = logging.getLogger(__name__)

AUTO_KEY = "auto"
CLASS_CHART = "CHART"
CLASS_TABLE = "TABLE"
CLASS_OTHER = "OTHER"
CLASS_UNKNOWN = "UNKNOWN"
TASK_FOR_CLASS = {CLASS_CHART: "chart", CLASS_TABLE: "table"}
#: Bascule de tâche quand le moteur détecte l'AUTRE type et ne rend rien.
LAYOUT_LABEL_FOR_TASK = {"chart": "chart", "table": "table"}

#: Passes de la tâche automatique pour une génération (1 + reprises sur GPU occupé).
AUTO_MAX_ATTEMPTS = 7
#: Attente minimale avant reprise (s) — la Vision libère la VRAM ~5 min après usage.
AUTO_BACKOFF_S = (30, 60, 120, 180, 240, 300)
#: Borne de travail par document (anti-emballement) : CELLE DU RAIL VISUEL
#: (``rail.MAX_ASSETS_PER_DOCUMENT``, 32). L'ancienne valeur 16 aurait tronqué en silence
#: la découverte d'un espace à borne relevée (STRUCTURED-VISUAL-CAP-32) ; à borne 12 elle
#: n'agissait jamais.
AUTO_MAX_ASSETS = 32

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_RETRY = "retry_scheduled"
STATUS_DONE = "succeeded"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

# Têtes nominales de la PREMIÈRE phrase de description (texte replié sans accents).
# Formes PLURIELLES comprises (« quatre graphiques ») ; « diagramme circulaire / en anneau /
# en barres » est un graphique, « diagramme » seul ne l'est pas ; « courbe » adjectif
# (« lignes courbes ») n'en est pas un (constats du pilote STRUCTURED-VISUAL-PILOT-STAGING-3).
_CHART_RE = re.compile(
    r"\b(graphiques?|graphes?|histogrammes?|(?<!ligne )(?<!lignes )courbes?|camemberts?"
    r"|diagrammes? (?:circulaires?|en anneaux?|(?:en|a) (?:barres|batons|secteurs))"
    r"|bar charts?|line charts?|pie charts?|charts?|histograms?|graphs?)\b")
_TABLE_RE = re.compile(
    r"\b(tableaux?(?! (?:electrique|general|divisionnaire|de distribution|blanc|noir|tgbt))"
    r"|registre|feuille de calcul|spreadsheet|data table)\b")
_DESCRIPTION_RE = re.compile(r"^(?:Description|Summary)\s*:\s*(.+)$", re.M)
#: Section ``Objects:`` du contrat ``describe`` : une ligne « - <nom> (count: …; attributes: …) »
#: par objet, jusqu'à l'en-tête de section suivant (« Visible text: », « Observable relations: »).
_OBJECTS_HEAD_RE = re.compile(r"^Objects\s*:\s*$", re.M)
_OBJECT_LINE_RE = re.compile(r"^-\s+([^(\n]+?)\s*(?:\(|$)")
_OBJECT_ATTRIBUTES_RE = re.compile(r"attributes\s*:\s*([^)\n]*)", re.IGNORECASE)
#: Borne de lecture de la liste (anti-emballement sur une description anormale).
MAX_OBJECT_LINES = 40


def _object_names(text: str) -> list:
    """NOMS des objets de la première section ``Objects:`` (jamais leurs attributs)."""
    match = _OBJECTS_HEAD_RE.search(text or "")
    if not match:
        return []
    names = []
    for line in (text[match.end():].lstrip("\n").splitlines())[:MAX_OBJECT_LINES]:
        line = line.strip()
        if not line.startswith("-"):
            break  # en-tête de section suivant
        m = _OBJECT_LINE_RE.match(line)
        if m:
            names.append(m.group(1).strip())
    return names


def _object_attributes(text: str) -> list:
    """ATTRIBUTS des objets de la première section ``Objects:`` (« (count: 1; attributes: …) »)."""
    match = _OBJECTS_HEAD_RE.search(text or "")
    if not match:
        return []
    out = []
    for line in (text[match.end():].lstrip("\n").splitlines())[:MAX_OBJECT_LINES]:
        line = line.strip()
        if not line.startswith("-"):
            break
        m = _OBJECT_ATTRIBUTES_RE.search(line)
        if m:
            out.append(m.group(1))
    return out


def _fold(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).casefold()


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]


def classify_from_describe(kind: Optional[str], text: Optional[str]) -> tuple:
    """(classe, preuve) d'UN visuel d'après son évidence ``describe``. Déterministe.

    1. ``visual_kind`` Vision ``chart`` / ``table`` -> la classe correspondante ;
    2. sinon la première phrase de ``Description`` (ou ``Summary``) : la tête nominale
       citée la PREMIÈRE l'emporte (« un tableau … d'un graphique » -> TABLE) ;
    3. sinon les NOMS d'objets de la section ``Objects:`` (PAGE-TABLE-ROUTING-1 : une page
       entière décrite comme « un rapport … » qui liste « - Tableau (count: 1) ») : un
       tableau -> TABLE, à défaut un graphique -> CHART ; attributs et texte visible ignorés ;
    4. sinon le CORPS de ``Description`` et les ATTRIBUTS des objets (TABLE-RECOVERY-1 : page
       réelle dont l'objet « Section 5. DETAILS DES COMPOSANTS » porte « attributes: tableau
       avec colonnes … ») : un tableau nommé, et AUCUN graphique nommé -> TABLE (candidat
       seulement) ; jamais le texte visible (OCR de la page) ;
    5. rien -> OTHER (photo, plan, carte, diagramme, logo…).
    Le routage n'ouvre aucune publication : preuve de région, grille et politique de tâches
    restent appliquées en aval.
    """
    if kind == "chart":
        return CLASS_CHART, "describe.visual_kind=chart"
    if kind == "table":
        return CLASS_TABLE, "describe.visual_kind=table"
    if text is None:
        return CLASS_UNKNOWN, "no_describe_evidence"
    sentences = [_first_sentence(m.group(1)) for m in _DESCRIPTION_RE.finditer(text)]
    if not sentences:
        return CLASS_UNKNOWN, "describe_without_description"
    for sentence in sentences:
        folded = _fold(sentence)
        chart, table = _CHART_RE.search(folded), _TABLE_RE.search(folded)
        if chart or table:
            if chart and (not table or chart.start() < table.start()):
                return CLASS_CHART, f"describe.description:{chart.group(1)!r}"
            return CLASS_TABLE, f"describe.description:{table.group(1)!r}"
    names = [_fold(n) for n in _object_names(text)]
    for cls, rx, label in ((CLASS_TABLE, _TABLE_RE, "tableau"), (CLASS_CHART, _CHART_RE, "graphique")):
        if any(rx.match(n) for n in names):  # tête du NOM (« tableau statistique »), exclusions comprises
            return cls, f"describe.objects:{label!r}"
    body = _fold(" ".join([m.group(1) for m in _DESCRIPTION_RE.finditer(text)] + _object_attributes(text)))
    table = _TABLE_RE.search(body)
    if table and not _CHART_RE.search(body) and not any(_CHART_RE.search(n) for n in names):
        return CLASS_TABLE, f"describe.body_or_attributes:{table.group(1)!r}"
    return CLASS_OTHER, "describe.description:no_chart_or_table_head"


def describe_evidence(document_id) -> dict:
    """asset_hash -> (visual_kind, texte) de l'évidence ``describe`` PUBLIÉE (hors structuré)."""
    from apps.rag_core.models import Chunk

    out: dict = {}
    rows = (Chunk.objects.filter(document_id=document_id, meta__has_key="visual")
            .exclude(meta__visual__has_key=S.STRUCTURED_KEY)
            .order_by("chunk_index").values_list("meta", "text"))
    for meta, text in rows:
        visual = (meta or {}).get("visual") or {}
        h = visual.get("asset_hash")
        if not h:
            continue
        kind, prev = out.get(h, (visual.get("kind"), ""))
        out[h] = (kind, (prev + "\n" + (text or "")).strip())
    return out


# --------------------------------------------------------------------------- #
# Portes
# --------------------------------------------------------------------------- #
def auto_enabled_for(doc) -> bool:
    from django.conf import settings

    if not getattr(settings, "STRUCTURED_VISUAL_AUTO_ENABLED", False):
        return False
    try:
        S.check_scope(doc)
    except S.StructuredVisualRefused:
        return False
    return True


def _auto_state(doc) -> dict:
    return dict(((doc.meta or {}).get(S.DOCUMENT_META_KEY) or {}).get(AUTO_KEY) or {})


def _save_auto(document_id, generation: str, patch: dict, *, reset: bool = False) -> bool:
    """Fusionne ``patch`` dans l'état automatique SI la génération est courante (verrou)."""
    from apps.rag_core.models import Document

    from . import deferred as D

    with transaction.atomic():
        locked = Document.objects.select_for_update().filter(id=document_id).first()
        if locked is None or D.state_of(locked).get("generation") != generation:
            return False
        meta = dict(locked.meta or {})
        registry = dict(meta.get(S.DOCUMENT_META_KEY) or {})
        state = {} if reset else dict(registry.get(AUTO_KEY) or {})
        if state.get("generation") not in (None, generation):
            state = {}
        state.update(patch)
        state["generation"] = generation
        state["updated_at"] = timezone.now().isoformat()
        registry[AUTO_KEY] = state
        meta[S.DOCUMENT_META_KEY] = registry
        Document.objects.filter(id=locked.id).update(meta=meta)
    return True


# --------------------------------------------------------------------------- #
# Enfilement (depuis visual_enrichment_task)
# --------------------------------------------------------------------------- #
def enqueue_after_enrichment(*, document_id, generation: str, request_id=None, access_context=None) -> bool:
    """Planifie la passe automatique d'une génération dont l'enrichissement est terminé.
    Jamais d'exception : l'enrichissement est déjà réglé, ceci n'est qu'un ajout."""
    from apps.rag_core.models import Document

    from . import deferred as D

    try:
        doc = Document.objects.select_related("rag_space").filter(id=document_id).first()
        if doc is None or not auto_enabled_for(doc):
            return False
        state = D.state_of(doc)
        if state.get("generation") != generation or state.get("status") not in D.TERMINAL_STATUSES:
            return False
        if not state.get("asset_hashes"):
            return False
        if not _save_auto(doc.id, generation, {"status": STATUS_PENDING, "attempt": 0, "assets": {},
                                               "queued_at": timezone.now().isoformat()}, reset=True):
            return False
        _schedule(document_id=str(doc.id), generation=generation, attempt=1, countdown=0,
                  request_id=request_id, access_context=access_context)
        return True
    except Exception:  # noqa: BLE001 - un ajout ne casse jamais l'enrichissement réglé
        logger.exception("structured auto enqueue failed document_id=%s", document_id)
        return False


def _schedule(*, document_id: str, generation: str, attempt: int, countdown: float,
              request_id=None, access_context=None) -> None:
    from apps.rag_core.services.ingest.queues import INGEST_VISUAL_QUEUE
    from apps.rag_core.tasks import structured_visual_task

    structured_visual_task.apply_async(
        kwargs={"document_id": document_id, "generation": generation, "attempt": attempt,
                "request_id": request_id, "access_context": access_context},
        queue=INGEST_VISUAL_QUEUE, countdown=max(0, int(countdown)))


#: Refus de publication qui disent « cette génération n'a plus rien à publier ».
_STALE_CODES = frozenset({"superseded", "not_found", "source_changed", "rag_space_deleted"})


def _extract(doc, asset, task, client, generation):
    """``extract_asset`` sans relance en ligne. Un refus « périmé » remonte (la passe
    s'arrête) ; toute autre erreur ne marque que CE visuel, jamais le document."""
    try:
        return S.extract_asset(doc=doc, asset=asset, task=task, client=client,
                               inline_retry=False, generation=generation)
    except S.StructuredVisualRefused as exc:
        if exc.code in _STALE_CODES:
            raise
        out = S.StructuredOutcome(document_id=str(doc.id), asset_hash=asset.content_hash, task=task)
        out.status, out.error_code = "failed", exc.code
        return out
    except Exception as exc:  # noqa: BLE001 - un visuel ne fait jamais échouer les autres
        logger.warning("structured auto asset failed document_id=%s cls=%s asset=%s",
                       doc.id, exc.__class__.__name__, asset.content_hash[:12])
        out = S.StructuredOutcome(document_id=str(doc.id), asset_hash=asset.content_hash, task=task)
        out.status, out.error_code = "failed", f"structured_auto_{exc.__class__.__name__}"
        return out


# --------------------------------------------------------------------------- #
# Passe automatique
# --------------------------------------------------------------------------- #
def run_auto(*, document_id, generation: str, attempt: int = 1, client=None,
             reschedule: bool = True, request_id=None, access_context=None) -> dict:
    """Une passe : classe les visuels non réglés, extrait, publie. Rend un résumé.

    Sur occupation GPU (503 relançable), s'arrête, persiste et se reprogramme (bornée)."""
    from apps.llm_router.gateway.structured_extract import StructuredExtractClient
    from apps.rag_core.models import Document
    from apps.rag_core.models.document import DocumentStatus

    from . import deferred as D

    doc = Document.objects.select_related("rag_space").filter(id=document_id).first()
    if doc is None:
        return {"skipped": "not_found"}
    if D.state_of(doc).get("generation") != generation:
        return {"skipped": "superseded"}
    if doc.status != DocumentStatus.READY or not auto_enabled_for(doc):
        _save_auto(doc.id, generation, {"status": STATUS_FAILED, "error_code": "not_ready_or_disabled"})
        return {"skipped": "not_ready_or_disabled"}

    state = _auto_state(doc)
    assets_state = dict(state.get("assets") or {}) if state.get("generation") == generation else {}
    if not _save_auto(doc.id, generation, {"status": STATUS_RUNNING, "attempt": attempt}):
        return {"skipped": "superseded"}

    discovered = {a.content_hash: a for a in S.discover(doc)}
    evidence = describe_evidence(doc.id)
    hashes = [h for h in (D.state_of(doc).get("asset_hashes") or []) if h in discovered][:AUTO_MAX_ASSETS]
    client = client or StructuredExtractClient.from_settings()
    allowed = S.allowed_tasks()
    busy = None
    for h in hashes:
        entry = dict(assets_state.get(h) or {})
        if entry.get("status") in ("published", "empty", "skipped", "refused"):
            continue
        if "class" not in entry:
            kind, text = evidence.get(h, (None, None))
            cls, proof = classify_from_describe(kind, text)
            entry.update({"class": cls, "evidence": proof, "source_page": discovered[h].source_page})
        task = TASK_FOR_CLASS.get(entry["class"])
        if task is None or task not in allowed:
            # Classe sans tâche (OTHER/UNKNOWN) ou tâche exclue par la politique : aucun appel.
            entry["status"] = "skipped"
            if task is not None:
                entry["skip_reason"] = "structured_visual_task_not_allowed"
            assets_state[h] = entry
            continue
        entry["task"] = task
        tried = [task]
        try:
            out = _extract(doc, discovered[h], task, client, generation)
        except S.StructuredVisualRefused as exc:
            return {"skipped": exc.code}
        calls = out.gateway_calls
        other = "table" if task == "chart" else "chart"
        if (out.status == "empty" and LAYOUT_LABEL_FOR_TASK[other] in out.layout_labels
                and other in allowed):
            # Le moteur a vu l'AUTRE type : une seule bascule, jamais une boucle, jamais vers
            # une tâche exclue par la politique.
            tried.append(other)
            try:
                out = _extract(doc, discovered[h], other, client, generation)
            except S.StructuredVisualRefused as exc:
                return {"skipped": exc.code}
            calls += out.gateway_calls
            entry["fallback_task"] = other
        entry.update({"status": out.status, "error_code": out.error_code, "layout_labels": out.layout_labels,
                      "chunks": out.chunks_published, "tasks_called": tried,
                      "calls": int(entry.get("calls") or 0) + calls})
        assets_state[h] = entry
        if out.status == "failed" and out.retryable:
            entry["status"] = "retryable"
            busy = out
            break

    if busy is not None and attempt < AUTO_MAX_ATTEMPTS and reschedule:
        wait = max(float(busy.retry_after_s or 0), AUTO_BACKOFF_S[min(attempt - 1, len(AUTO_BACKOFF_S) - 1)])
        _save_auto(doc.id, generation, {"status": STATUS_RETRY, "assets": assets_state, "attempt": attempt,
                                        "retry_in_s": wait, "last_error": busy.error_code})
        _schedule(document_id=str(doc.id), generation=generation, attempt=attempt + 1, countdown=wait,
                  request_id=request_id, access_context=access_context)
        return {"status": STATUS_RETRY, "retry_in_s": wait, "assets": assets_state}

    failed = [h for h, e in assets_state.items() if e.get("status") in ("failed", "retryable")]
    done = [h for h, e in assets_state.items() if e.get("status") in ("published", "empty")]
    status = STATUS_FAILED if failed and not done else STATUS_PARTIAL if failed else STATUS_DONE
    patch = {"status": status, "assets": assets_state, "attempt": attempt,
             "completed_at": timezone.now().isoformat()}
    if busy is not None:
        patch["error_code"] = "structured_auto_retry_budget_exhausted"
    _save_auto(doc.id, generation, patch)
    return {"status": status, "assets": assets_state}
