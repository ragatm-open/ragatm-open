# backend/apps/rag_core/services/query/source_meta.py
from __future__ import annotations

"""
Métadonnées de source exposées au client et à l'audit — **source unique de vérité**.

`Chunk.meta` est un JSONField libre : il porte tout ce que le pipeline y a mis, y
compris des choses qui n'ont **rien à faire** dans une réponse d'API — les entités
métier extraites par regex (un dict de listes, sur *chaque* chunk PDF), le JSON complet
des lignes d'un tableau P3, les offsets de caractères…

Jusqu'ici, `normalize_source_for_api` renvoyait `Chunk.meta` **en entier**. Ça gonflait
la charge utile de chaque source et exposait des détails d'implémentation que personne
ne lit. On projette donc sur une **allowlist** : ce qui est utile pour citer, et rien
d'autre.

L'audit (`RagQueryLog`) réutilise EXACTEMENT la même projection : API et audit ne
doivent pas diverger — sinon on auditerait autre chose que ce que l'utilisateur a vu.
"""

from typing import Any

# Clés conservées, par famille. Tout le reste de `Chunk.meta` est écarté.
_KEEP = (
    # -- transverse ---------------------------------------------------
    "kind",                  # excel_table | excel_narrative | pptx_slide | pptx_notes | table (P3) | absent
    # -- Excel (lot E2) : ce qui permet de CITER une feuille et une plage
    "sheet_name",
    "sheet_index",
    # Une feuille MASQUÉE est indexée (elle fait partie du fichier), mais l'utilisateur
    # doit le savoir : elle n'apparaît pas dans les onglets de son classeur. Taire son
    # état serait exposer sans le dire.
    "sheet_state",           # visible | hidden | veryHidden
    "sheet_hidden",
    "cell_range",            # = data_range : les cellules de DONNÉES de ce chunk
    "header_range",          # les cellules d'EN-TÊTE, citées séparément (P1.2)
    # Un chunk découpé par COLONNES (ligne trop large) couvre une plage NON CONTIGUË :
    # la colonne d'identification reportée + une fenêtre de colonnes. `cell_range` ne
    # porte alors que la fenêtre — il OMET l'identifiant. Sans `data_ranges`, le
    # visualiseur surlignerait une zone fausse (trop étroite) en la croyant complète.
    "data_ranges",           # liste ORDONNÉE des plages réellement couvertes
    "header_ranges",         # même non-contiguïté, côté en-têtes
    "identity_columns",      # colonne(s) d'identification reportée(s)
    "identity_range",
    # "columns" (ligne large fractionnée) ou "rows_and_columns" (tableau large
    # découpé sur les deux axes) — deux chemins distincts de `excel_render`.
    "chunk_axis",
    "column_part_index",
    "column_part_count",
    "column_window_index",
    "row_start",
    "row_end",
    "column_start",
    "column_end",
    "header_row",
    "headers",
    "headers_inherited",     # un bloc TOTAL emprunte les intitulés du tableau au-dessus
    "title",
    "chunk_of",
    "chunk_total",
    "has_formulas",
    # -- PowerPoint : ce qui permet de CITER une diapositive
    # `page_number` est None pour une présentation (une diapositive n'est pas une page,
    # cf. indexing._build_slide_specs) : sans ces clés, une citation PPTX n'aurait AUCUN
    # repère de provenance et se réduirait au nom du fichier.
    "slide_number",
    "slide_title",
    "has_images",
    "has_text",
    # -- Formats structurés : repère de provenance GÉNÉRIQUE, sans
    # page. `{type, path, identifier}`, chacune optionnelle ; seules ces trois clés
    # sont projetées (cf. `_clean_locator`). Aucun format n'y est nommé.
    "locator",
    # -- Courriels : citation HIÉRARCHIQUE courriel → pièce → repère.
    # Le repère interne (page PDF, diapositive, feuille/plage) reste porté par les clés
    # de son format. JAMAIS d'adresse ni de destinataire : objet et noms de pièces seuls.
    "email_part",                # body | attachment
    "email_subject",             # objet du courriel PARENT
    "email_nested_subject",      # objet d'un courriel imbriqué (transféré)
    "email_attachment_index",
    "email_attachment_path",     # « 2 », « 2.1 » : position dans l'arbre des pièces
    "email_attachment_filename",
    "email_attachment_format",
    "email_nested_depth",
    "email_section",
    # Date d'envoi (ISO) et NOM de l'expéditeur : une citation
    # « Email du 12/05/2026 — De : Fournisseur ». Une ADRESSE n'est jamais projetée
    # (cf. `_email_sender_name`) : la règle de 27A tient toujours.
    "email_sent_at",
    "email_sender",
    # -- Audio : citation TEMPORELLE d'une transcription ASR. La plage
    # affichée vient de `locator` (`time_range`) ; les bornes en ms permettent un futur
    # lien vers l'instant. Jamais le texte complet ni les libellés internes de job.
    "source_type",               # AUDIO_ASR_TRANSCRIPT
    "source_is_asr",
    "start_ms",
    "end_ms",
    "timestamp_precision",       # segment | part
    "speaker_ids",
    # -- PDF / plans (P2-P3) : signaux de confiance déjà exploités ailleurs
    "page_type",
    "table_type",
    "confidence",
    "requires_visual_check",
    "source_is_ocr",
    # -- Fiabilité des preuves numériques (OCR-NUMERIC-CAVEAT-1), calculée à la lecture
    "numeric_evidence",
    "numeric_evidence_reason",
    "native_table_unverified",
)

# Bornes anti-charge-utile : une source est une CITATION, pas un dépôt de données.
_MAX_HEADERS = 32
_MAX_HEADER_CHARS = 120
#: Un chunk couvre au plus quelques plages (identifiant + fenêtre). La borne est un
#: garde-fou contre une méta malformée, pas une troncature attendue.
_MAX_RANGES = 8
_MAX_TITLE_CHARS = 200
_MAX_STR_CHARS = 200
#: Un chemin de repère (XPath simplifié, chemin d'entité…) peut être long ; borné
#: néanmoins — une source est une citation, pas un dépôt de structure.
_MAX_LOCATOR_PATH_CHARS = 512
_LOCATOR_KEYS = ("type", "path", "identifier")


def _clean_str(v: Any, *, max_chars: int = _MAX_STR_CHARS) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    return s[:max_chars]


def _clean_int(v: Any) -> int | None:
    if isinstance(v, bool):          # en Python, bool est un int : on ne veut pas ça ici
        return None
    if isinstance(v, int):
        return v
    return None


def _clean_locator(v: Any) -> dict | None:
    """``{type, path, identifier}`` épuré : chaînes non vides seulement, clés inconnues
    écartées ; ``None`` si rien d'exploitable ou si la valeur n'est pas un dict."""
    if not isinstance(v, dict):
        return None
    out: dict[str, str] = {}
    for key in _LOCATOR_KEYS:
        s = _clean_str(v.get(key), max_chars=_MAX_LOCATOR_PATH_CHARS if key == "path" else _MAX_STR_CHARS)
        if s:
            out[key] = s
    return out or None


def _email_sender_name(v: Any) -> str | None:
    """Nom AFFICHÉ de l'expéditeur, ou rien. L'extracteur range dans `email_sender` le nom
    s'il existe, sinon l'adresse : une valeur qui contient « @ » est une adresse et n'est
    jamais citée (une citation n'expose aucune adresse — qualification interne)."""
    s = _clean_str(v, max_chars=_MAX_TITLE_CHARS)
    if not s or "@" in s:
        return None
    return s


def project_source_meta(meta: Any) -> dict:
    """Projette `Chunk.meta` sur les seules clés utiles à la citation.

    Ne lève jamais : une méta absente, mal typée ou exotique renvoie ``{}``. C'est un
    chemin d'affichage — il ne doit jamais faire échouer une réponse.
    """
    if not isinstance(meta, dict):
        return {}

    out: dict[str, Any] = {}
    for key in _KEEP:
        if key not in meta:
            continue
        v = meta[key]

        if key == "headers":
            if isinstance(v, list):
                heads = [
                    _clean_str(h, max_chars=_MAX_HEADER_CHARS) or ""
                    for h in v[:_MAX_HEADERS]
                ]
                if any(heads):
                    out[key] = heads
            continue

        if key == "email_sender":
            name = _email_sender_name(v)
            if name:
                out[key] = name
            continue

        if key == "locator":
            loc = _clean_locator(v)
            if loc:
                out[key] = loc
            continue

        if key in ("title", "slide_title"):
            s = _clean_str(v, max_chars=_MAX_TITLE_CHARS)
            if s:
                out[key] = s
            continue

        if key == "speaker_ids":
            # Libellés TECHNIQUES du moteur (`speaker_00`…), jamais un nom de personne.
            if isinstance(v, list):
                ids = [x for x in v[:4] if isinstance(x, str) and len(x) <= 16]
                if ids:
                    out[key] = ids
            continue

        if key in ("data_ranges", "header_ranges", "identity_columns"):
            # Listes de RÉFÉRENCES (plages / lettres de colonnes), jamais de valeurs.
            # Bornées comme les en-têtes : une source est une citation, pas un dépôt.
            if isinstance(v, list):
                refs = [r for r in (_clean_str(x, max_chars=64) for x in v[:_MAX_RANGES]) if r]
                if refs:
                    out[key] = refs
            continue

        if key in ("row_start", "row_end", "sheet_index", "chunk_of", "chunk_total",
                   "header_row", "slide_number", "column_part_index", "column_part_count",
                   "column_window_index", "email_attachment_index", "email_nested_depth",
                   "start_ms", "end_ms"):
            n = _clean_int(v)
            if n is not None:
                out[key] = n
            continue

        if key in ("has_formulas", "requires_visual_check", "source_is_ocr",
                   "headers_inherited", "sheet_hidden", "has_images", "has_text", "native_table_unverified",
                   "source_is_asr"):
            if isinstance(v, bool):
                out[key] = v
            continue

        s = _clean_str(v)
        if s:
            out[key] = s

    return out


def is_excel(meta: Any) -> bool:
    """La source vient-elle d'un classeur ? (`kind` ∈ {excel_table, excel_narrative})"""
    if not isinstance(meta, dict):
        return False
    kind = meta.get("kind")
    return isinstance(kind, str) and kind.startswith("excel_")


def is_pptx(meta: Any) -> bool:
    """La source vient-elle d'une présentation ? (`kind` ∈ {pptx_slide, pptx_notes})"""
    if not isinstance(meta, dict):
        return False
    kind = meta.get("kind")
    return isinstance(kind, str) and kind.startswith("pptx_")
