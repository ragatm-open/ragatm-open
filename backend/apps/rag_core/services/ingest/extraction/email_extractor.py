# backend/apps/rag_core/services/ingest/extraction/email_extractor.py
from __future__ import annotations

"""
Courriels (.msg, .eml) et CONTENU de leurs pièces jointes.

Architecture retenue : une pièce jointe est un SOUS-DOCUMENT LOGIQUE de son courriel,
pas un `Document` persistant. Ses fragments appartiennent au document courriel et
portent leur provenance (index, chemin, empreinte, format). Conséquences voulues :

- la contrainte `(rag_space, sha256)` des documents n'est pas touchée : le même PDF
  joint à deux courriels donne deux jeux de fragments, chacun rattaché à SON message ;
- les droits, la suppression (cascade des fragments), la réindexation (publication
  atomique existante) et les relances sont ceux du courriel, sans mécanisme parallèle ;
- aucune pièce n'est stockée à part : rien d'orphelin à nettoyer.

Chaque pièce passe par les MÊMES primitives qu'un dépôt : `validate_and_classify`
(type réel, taille, refus explicites, portes de format de l'environnement), puis
l'extracteur du registre. Elles s'exécutent dans le processus d'extraction du courriel,
donc sous SON budget de temps ; le traitement est SÉQUENTIEL et borné en nombre, en
taille unitaire, en volume cumulé (partagé avec les imbriqués) et en profondeur.
Aucune pièce ne fait échouer le courriel : chacune reçoit un statut terminal.
"""

import hashlib
import re
import time
from typing import Callable, List, Optional, Tuple

from . import config, mime as M
from .base import (
    Block,
    BlockType,
    ExtractionContext,
    ExtractionError,
    ExtractionResult,
    RejectedFile,
    UnsupportedFormat,
)
from .email_model import MSG_UNSUPPORTED_CODE, AttachmentPayload, ExtractedEmail, MsgParseError, ParsedEmail

# --- statuts d'une pièce jointe (contrat API / UI, valeurs stables) -----------
ATT_PENDING = "PENDING"            # extraite, en attente d'indexation (jamais publié tel quel)
ATT_INDEXED = "INDEXED"
ATT_PARTIALLY_INDEXED = "PARTIALLY_INDEXED"
ATT_UNSUPPORTED = "UNSUPPORTED"
ATT_REJECTED = "REJECTED"
ATT_FAILED = "FAILED"
ATT_SKIPPED_LIMIT = "SKIPPED_LIMIT"
ATT_DUPLICATE = "DUPLICATE_IN_EMAIL"
ATT_NOT_INGESTED = "NOT_INGESTED"  # ingestion des pièces désactivée pour cet espace
# Pièce LUE correctement mais sans texte exploitable (logo, visuel décoratif, fichier vide) —
# Ce n'est ni une erreur technique, ni un contenu indexé.
ATT_NO_TEXT = "NO_EXTRACTABLE_TEXT"
#: Codes d'extracteur qui signifient « lu, mais rien à indexer » — et SEULEMENT ceux-là. Un
#: fichier corrompu (`image_corrupted`, `pdf_text_unrecoverable`…), un délai dépassé ou une
#: dépendance absente restent des ÉCHECS (FAILED) : on ne masque jamais une vraie erreur.
NO_TEXT_CODES = frozenset({"ocr_no_text", "empty_document"})


def is_no_text_code(code: str, fmt: Optional[str]) -> bool:
    """`ocr_no_text` ne vaut « sans texte » que pour une IMAGE (logo, visuel) : sur un PDF il
    signale un scan illisible, qui reste un ÉCHEC. `empty_document` : fichier lu et vide."""
    if code == "ocr_no_text":
        return fmt == "image"
    return code in NO_TEXT_CODES
#: Statuts qui signalent une PERTE de contenu exploitable : le courriel est alors PARTIEL.
#: Ni un format volontairement non pris en charge (UNSUPPORTED, signalé à part), ni une pièce
#: sans texte (NO_EXTRACTABLE_TEXT) ne rendent un courriel partiel.
PARTIAL_STATUSES = frozenset({ATT_REJECTED, ATT_FAILED, ATT_SKIPPED_LIMIT, ATT_PARTIALLY_INDEXED})
SEARCHABLE_STATUSES = frozenset({ATT_INDEXED, ATT_PARTIALLY_INDEXED})

# Formats dont l'extraction exige un worker DÉDIÉ (dépendance absente du worker général).
_DEDICATED_WORKER_FORMATS = frozenset({"ifc", "dwg"})
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
# Part du budget d'extraction au-delà de laquelle aucune nouvelle pièce n'est ouverte :
# le corps et les pièces déjà traitées ne doivent pas être perdus dans un dépassement global.
_TIME_BUDGET_SHARE = 0.6

ROLE_HEADER = "header"
ROLE_ATTACHMENTS = "attachments"


def safe_attachment_name(name: str, *, index: int, extension: str = "") -> str:
    """Nom AFFICHABLE et sûr : basename déjà normalisé, contrôles retirés, longueur bornée.
    Jamais utilisé comme chemin (aucune pièce n'est écrite sur disque)."""
    clean = _CTRL.sub("", name or "").strip().strip(".")
    clean = clean[:200]
    if not clean:
        clean = f"piece-jointe-{index}" + (f".{extension}" if extension else "")
    return clean


class _Budget:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.start = clock()
        self.deadline = self.start + config.extraction_timeout_s() * _TIME_BUDGET_SHARE
        self.max_count = config.email_max_attachments()
        self.max_bytes = config.email_max_attachment_bytes()
        self.max_total = config.email_max_total_decoded_bytes()
        self.max_depth = config.email_max_nesting_depth()
        self.count = 0
        self.total = 0


def _status_entry(payload: AttachmentPayload, *, index: int, path: str, depth: int, parent_path: str) -> dict:
    att = payload.attachment
    return {
        "index": index,
        "path": path,
        "depth": depth,
        "parent_path": parent_path or None,
        "filename": safe_attachment_name(att.filename, index=index, extension=att.extension),
        "extension": att.extension,
        "mimetype": att.mimetype,
        "size_bytes": att.size_bytes or (len(payload.data) if payload.data else 0),
        "content_id": att.content_id,
        "is_inline": att.is_inline,
        "sha256": None,
        "detected_format": None,
        "status": ATT_PENDING,
        "reason": None,
        "fragment_count": 0,
    }


def _extract_one(data: bytes, entry: dict, ctx: ExtractionContext) -> Tuple[Optional[str], Optional[ExtractionResult]]:
    """Classe et extrait UNE pièce par les primitives d'un dépôt. Renvoie (format, résultat)
    ou pose un statut terminal dans ``entry`` et renvoie (format|None, None)."""
    from . import _REGISTRY
    from .validation import validate_and_classify

    sub_ctx = ExtractionContext(
        filename=entry["filename"],
        declared_content_type=entry["mimetype"] or "",
        ocr_enabled=ctx.ocr_enabled,
        ocr_page_timeout_s=ctx.ocr_page_timeout_s,
        # Aucun visuel découvert pour une pièce jointe : la description Vision des pièces
        # n'est PAS dans ce lot (documenté). Comportement historique de chaque extracteur.
        visual_sources=frozenset(),
        rag_space_id=ctx.rag_space_id,
    )
    try:
        _, fmt = validate_and_classify(data, sub_ctx)
    except UnsupportedFormat as e:
        entry.update(status=ATT_UNSUPPORTED, reason=e.code or "unsupported_file_type")
        return None, None
    except RejectedFile as e:
        entry.update(status=ATT_REJECTED, reason=e.code or "rejected_file")
        return None, None
    entry["detected_format"] = fmt
    if fmt in _DEDICATED_WORKER_FORMATS:
        entry.update(status=ATT_UNSUPPORTED, reason="email_attachment_requires_dedicated_worker")
        return fmt, None
    if fmt in ("msg", "eml"):
        return fmt, None  # courriel imbriqué : traité par l'appelant (profondeur, cycles)
    extractor = _REGISTRY.get(fmt)
    if extractor is None:  # pragma: no cover - garde-fou
        entry.update(status=ATT_UNSUPPORTED, reason="unsupported_file_type")
        return fmt, None
    try:
        result = extractor.extract(data, sub_ctx)
    except ExtractionError as e:
        code = e.code or "extraction_error"
        entry.update(status=ATT_NO_TEXT if is_no_text_code(code, fmt) else ATT_FAILED, reason=code)
        return fmt, None
    except Exception:
        # Aucune exception d'une pièce ne remonte : le courriel et ses autres pièces
        # restent exploitables. Le détail n'est jamais exposé (contenu non fiable).
        entry.update(status=ATT_FAILED, reason="email_attachment_extraction_error")
        return fmt, None
    entry["extraction_method"] = result.method or None
    if result.partial:
        entry["partial"] = True
    return fmt, result


def _parse_nested_bytes(fmt: str, data: bytes, depth_left: int) -> ParsedEmail:
    if fmt == "msg":
        # RAGatm Open : pas de parseur Outlook (dépendance GPL retirée). Une pièce .msg
        # imbriquée est consignée comme non lisible, jamais ouverte.
        raise MsgParseError(MSG_UNSUPPORTED_CODE)
    from .eml_email import parse_eml_bytes

    return parse_eml_bytes(data, max_recipients=config.msg_max_recipients(),
                           max_attachments=config.msg_max_attachments_inventoried(), max_depth=depth_left)


def _email_fingerprint(email: ExtractedEmail) -> str:
    """Empreinte d'un message incorporé (sans octets bruts) : détection des cycles."""
    raw = "\x1e".join(str(x or "") for x in (email.internet_message_id, email.subject, email.sent_at,
                                               email.body_plain, email.body_html))
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def process_attachments(parsed: ParsedEmail, ctx: ExtractionContext, *, clock=time.monotonic):
    """Traite les pièces (et les courriels imbriqués) d'un message. Renvoie
    ``(entrées, résultats)`` : ``entrées`` = statut de CHAQUE pièce (liste plate, ordre du
    message, chemin hiérarchique ``"2.1"``) ; ``résultats`` = ``[(index, ExtractionResult)]``
    des pièces extraites, à indexer."""
    budget = _Budget(clock=clock)
    entries: List[dict] = []
    results: List[tuple] = []
    seen_sha: dict = {}

    def walk(p: ParsedEmail, *, depth: int, parent_path: str, ancestors: frozenset) -> None:
        for payload in p.payloads:
            index = len(entries) + 1
            path = f"{parent_path}.{payload.position + 1}" if parent_path else str(payload.position + 1)
            entry = _status_entry(payload, index=index, path=path, depth=depth, parent_path=parent_path)
            entries.append(entry)

            if payload.error_code:
                status = ATT_SKIPPED_LIMIT if payload.error_code == "email_nesting_limit" else (
                    ATT_UNSUPPORTED if payload.error_code == "email_attachment_unsupported_kind" else ATT_FAILED)
                entry.update(status=status, reason=payload.error_code)
                continue
            if budget.count >= budget.max_count:
                entry.update(status=ATT_SKIPPED_LIMIT, reason="email_attachment_count_limit")
                continue
            if budget.clock() > budget.deadline:
                entry.update(status=ATT_SKIPPED_LIMIT, reason="email_time_budget_exhausted")
                continue

            nested: Optional[ParsedEmail] = None
            if payload.embedded is not None:
                nested = payload.embedded
                entry["detected_format"] = nested.email.source_format
            else:
                data = payload.data or b""
                if not data:
                    entry.update(status=ATT_FAILED, reason="email_attachment_empty")
                    continue
                if len(data) > budget.max_bytes:
                    entry.update(status=ATT_SKIPPED_LIMIT, reason="email_attachment_too_large")
                    continue
                if budget.total + len(data) > budget.max_total:
                    entry.update(status=ATT_SKIPPED_LIMIT, reason="email_total_size_limit")
                    continue
                sha = hashlib.sha256(data).hexdigest()
                entry["sha256"] = sha
                entry["size_bytes"] = len(data)
                if sha in seen_sha:
                    # Mêmes octets deux fois DANS ce courriel : indexés une seule fois, la
                    # seconde occurrence pointe vers la première (provenance conservée).
                    entry.update(status=ATT_DUPLICATE, reason="email_attachment_same_content",
                                 same_as=seen_sha[sha])
                    continue
                budget.count += 1
                budget.total += len(data)
                seen_sha[sha] = path
                fmt, result = _extract_one(data, entry, ctx)
                if fmt in ("msg", "eml"):
                    # Un courriel joint À la profondeur `depth` est un courriel imbriqué de
                    # niveau `depth` : ouvert seulement si `depth <= max`. Même règle que les
                    # messages Outlook incorporés (cf. `msg_email._payloads`).
                    if depth > budget.max_depth:
                        entry.update(status=ATT_SKIPPED_LIMIT, reason="email_nesting_limit")
                        continue
                    try:
                        nested = _parse_nested_bytes(fmt, data, budget.max_depth - depth)
                    except Exception:
                        entry.update(status=ATT_FAILED, reason="email_parse_failed")
                        continue
                elif result is None:
                    continue  # statut terminal déjà posé
                else:
                    if not _has_content(result):
                        # « Lu, rien à indexer » seulement si le fichier a VRAIMENT été lu (au
                        # moins une page). Un PDF tronqué rend un résultat vide SANS page : c'est
                        # un échec, jamais une pièce « sans texte ».
                        if int((result.metadata or {}).get("pages") or 0) > 0:
                            entry.update(status=ATT_NO_TEXT, reason="email_attachment_no_content")
                        else:
                            entry.update(status=ATT_FAILED, reason="email_attachment_unreadable")
                        continue
                    results.append((index, result))
                    continue

            # --- courriel imbriqué ---------------------------------------------
            if payload.embedded is not None:
                budget.count += 1
            fp = _email_fingerprint(nested.email)
            if fp in ancestors:
                entry.update(status=ATT_SKIPPED_LIMIT, reason="email_cycle_detected")
                continue
            entry["detected_format"] = nested.email.source_format
            nested_result = build_email_result(nested, ctx, with_attachments=False)
            entry["nested_subject_present"] = bool(nested.email.subject)
            results.append((index, nested_result))
            walk(nested, depth=depth + 1, parent_path=path, ancestors=ancestors | {fp})

    walk(parsed, depth=1, parent_path="", ancestors=frozenset({_email_fingerprint(parsed.email)}))
    return entries, results


def _has_content(result: ExtractionResult) -> bool:
    if (result.text or "").strip():
        return True
    return bool(result.excel_chunks or result.slides or result.units or result.email_sections
                or result.tables)


def _attachments_section(entries: List[dict], subject: Optional[str]) -> Optional[dict]:
    """Unité « pièces jointes » : rend la RELATION message ↔ pièces interrogeable
    (« quels documents accompagnaient… ») sans dupliquer leur contenu."""
    top = [e for e in entries if e.get("depth") == 1]
    if not top:
        return None
    labels = {ATT_PENDING: "contenu indexé", ATT_INDEXED: "contenu indexé",
              ATT_PARTIALLY_INDEXED: "contenu partiellement indexé", ATT_UNSUPPORTED: "format non pris en charge",
              ATT_REJECTED: "refusée", ATT_FAILED: "extraction en échec", ATT_SKIPPED_LIMIT: "non traitée (limite)",
              ATT_DUPLICATE: "doublon d'une autre pièce", ATT_NOT_INGESTED: "contenu non ingéré",
              ATT_NO_TEXT: "aucun texte exploitable"}
    lines = [f"Pièces jointes du message « {subject} » :" if subject else "Pièces jointes du message :"]
    for e in top:
        lines.append(f"- {e['filename']} ({labels.get(e['status'], e['status'])})")
    return {"role": ROLE_ATTACHMENTS, "index": 0, "title": "Pièces jointes", "text": "\n".join(lines)}


def _format_header(email: ExtractedEmail) -> str:
    """Bloc d'en-tête recherchable : objet, expéditeur, date, type — jamais les destinataires."""
    lines = ["Type : email Outlook" if email.source_format == "msg" else "Type : email"]
    if email.subject:
        lines.append(f"Objet : {email.subject}")
    sender = " ".join(p for p in (email.sender_name, email.sender_address) if p)
    if sender:
        lines.append(f"Expéditeur : {sender}")
    if email.sent_at:
        lines.append(f"Date : {email.sent_at.strftime('%Y-%m-%d %H:%M %Z').strip()}")
    return "\n".join(lines)


def build_email_result(parsed: ParsedEmail, ctx: ExtractionContext, *, with_attachments: bool,
                       clock=time.monotonic) -> ExtractionResult:
    """`ExtractedEmail` (+ pièces) -> `ExtractionResult` du rail email. Commun MSG / EML."""
    from .email_sections import ROLE_BODY, group_sections, select_body, split_boilerplate

    email = parsed.email
    fmt = email.source_format
    res = ExtractionResult()
    for warning in email.warnings:
        res.add_warning(warning)

    body_blocks, body_kind, body_warnings, body_info = select_body(email)
    for warning in body_warnings:
        res.add_warning(warning)
    body_blocks, boilerplate_blocks = split_boilerplate(body_blocks)
    removed_boilerplate_chars = sum(len(b.text or "") for b in boilerplate_blocks)

    header_text = _format_header(email)
    blocks: List[Block] = [Block(type=BlockType.SECTION, text=header_text, level=1)]
    blocks.extend(body_blocks)
    sections: List[dict] = [{"role": ROLE_HEADER, "index": 0, "title": "En-tête", "text": header_text}]
    sections.extend(group_sections(body_blocks))

    body_text = "\n\n".join(b.text for b in body_blocks)
    limit = config.msg_max_body_chars()
    if limit and len(body_text) > limit:
        body_text = body_text[:limit]
        res.partial = True
        res.add_warning("truncated_max_chars")
        res.add_warning("msg_body_truncated")

    md = res.metadata
    md["document_type"] = "email"
    md["source_format"] = fmt
    md["subject"] = email.subject
    md["sender_name"] = email.sender_name
    md["sender_address"] = email.sender_address
    md["to"] = list(email.to)
    md["cc"] = list(email.cc)
    md["recipient_count"] = len(email.to) + len(email.cc)
    md["sent_at"] = email.sent_at.isoformat() if email.sent_at else None
    md["internet_message_id"] = email.internet_message_id
    md["in_reply_to"] = email.in_reply_to
    md["references_count"] = len(email.references)
    md["conversation_id"] = email.conversation_id
    md["conversation_topic"] = email.conversation_topic
    md["body_kind"] = body_kind
    md["removed_boilerplate_chars"] = removed_boilerplate_chars
    md["attachments_detected"] = email.attachment_count
    if body_info.get("links") is not None:
        md["links"] = body_info["links"]

    if with_attachments:
        enabled = config.email_attachments_enabled_for(ctx.rag_space_id)
        if enabled:
            entries, results = process_attachments(parsed, ctx, clock=clock)
        else:
            entries = []
            for pos, att in enumerate(email.attachments):
                entries.append({**{k: v for k, v in att.as_dict().items()},
                                "filename": safe_attachment_name(att.filename, index=pos + 1,
                                                                 extension=att.extension),
                                "index": pos + 1, "path": str(pos + 1), "depth": 1, "parent_path": None,
                                "status": ATT_NOT_INGESTED, "reason": "email_attachment_ingestion_disabled",
                                "fragment_count": 0})
            results = []
        res.email_attachment_results = results
        md["attachments"] = entries
        md["attachments_supported_in_this_release"] = bool(enabled)
        md["attachment_ingestion"] = "enabled" if enabled else "disabled"
        md["attachments_ingested"] = sum(1 for e in entries if e["status"] in (ATT_PENDING, ATT_INDEXED,
                                                                                ATT_PARTIALLY_INDEXED))
        # Section « pièces jointes » seulement quand leur ingestion est OUVERTE : fermée, le
        # rail email produit exactement les fragments d'avant ce lot (historique inchangé).
        section = _attachments_section(entries, email.subject) if enabled else None
        if section is not None:
            sections.insert(1, section)
        if enabled and any(e["status"] in PARTIAL_STATUSES for e in entries):
            md["email_status"] = "partial"
        else:
            md["email_status"] = "complete"
    md["sections"] = len(sections)

    res.blocks = blocks
    res.email_sections = sections
    res.text = "\n\n".join(t for t in (header_text, body_text) if t)

    if body_kind == "none":
        # Parse réussi, mais AUCUN corps exploitable : l'en-tête reste utile, statut PARTIEL.
        res.partial = True
        res.add_warning("msg_body_missing" if fmt == "msg" else "email_body_empty")
    mime = M.OUTLOOK_MSG if fmt == "msg" else M.EML
    return res.finalize(fmt=fmt, mime=mime, method=fmt)


class EmlExtractor:
    """Courriel MIME `.eml`."""

    name = "eml"

    def can_handle(self, *, detected_mime: str, ctx: ExtractionContext) -> bool:
        return detected_mime == M.EML

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        from .base import CorruptedFile
        from .eml_email import EmlParseError, parse_eml_bytes

        depth = config.email_max_nesting_depth() if config.email_attachments_enabled_for(ctx.rag_space_id) else 0
        try:
            parsed = parse_eml_bytes(data or b"", max_recipients=config.msg_max_recipients(),
                                     max_attachments=config.msg_max_attachments_inventoried(), max_depth=depth)
        except EmlParseError as exc:
            raise CorruptedFile(str(exc), code="email_parse_failed") from exc
        return build_email_result(parsed, ctx, with_attachments=True)
