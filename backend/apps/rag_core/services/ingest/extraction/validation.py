# backend/apps/rag_core/services/ingest/extraction/validation.py
from __future__ import annotations

"""
Validation pré-extraction : taille, type réel (allowlist), refus explicites.

Renvoie un couple (mime_canonique, format_key) si le fichier est accepté.
Lève une erreur typée sinon (RejectedFile / UnsupportedFormat) — toujours avec
un message utilisateur clair et SANS contenu sensible.
"""

import re
from typing import Tuple

from . import config
from . import mime as M
from .base import ExtractionContext, RejectedFile, UnsupportedFormat
from .dwg_signature import dwg_version
from .dwg_signature import is_supported_version as is_supported_dwg_version
from .dxf_extractor import parser_available as dxf_parser_available
from .email_model import MSG_UNSUPPORTED_CODE, parser_available as msg_parser_available
from .xml_extractor import parser_available as xml_parser_available

# format_key utilisés par le registre d'extracteurs
FMT_TXT = "txt"
FMT_PDF = "pdf"
FMT_MARKDOWN = "markdown"
FMT_DOCX = "docx"
FMT_CSV = "csv"
FMT_HTML = "html"
FMT_IMAGE = "image"
FMT_EXCEL = "excel"
FMT_PPTX = "pptx"
FMT_MSG = "msg"
FMT_XML = "xml"
FMT_DXF = "dxf"
FMT_IFC = "ifc"
FMT_DWG = "dwg"
FMT_EML = "eml"
#: Audio : la FAMILLE annoncée au dépôt ; l'extraction ne lit que la
#: transcription canonique persistée, jamais les octets sonores.
FMT_AUDIO = "audio"
#: Formats COURRIEL : admis au dépôt seulement dans un espace de DOCUMENT_EMAIL_RAGSPACE_IDS.
EMAIL_FORMATS = frozenset({FMT_MSG, FMT_EML})

# Allowlist : MIME réel -> format_key
_ALLOW = {
    M.PLAIN: FMT_TXT,
    M.PDF: FMT_PDF,
    M.MARKDOWN: FMT_MARKDOWN,
    M.DOCX: FMT_DOCX,
    M.CSV: FMT_CSV,
    M.HTML: FMT_HTML,
}

# Refus explicites : MIME réel -> (code, message utilisateur)
#
# NOTE (lot E0) : ces refus lèvent tous `RejectedFile` -> HTTP 400 (cf.
# api/v1/documents/views_write.py). Seul `UnsupportedFormat` produit un 415. Les
# nouveaux refus Office (XLSM/ODS/héritage) suivent donc la convention DÉJÀ en place
# pour XLSX/PPTX/DOCM plutôt que d'introduire un statut divergent.
_REJECT = {
    M.DOCM: (
        "macro_enabled_document_refused",
        "Document Word macro-enabled (.docm) refusé : les fichiers à macros ne sont pas autorisés.",
    ),
    M.XLSM: (
        "macro_enabled_workbook_refused",
        "Classeur Excel macro-enabled (.xlsm) refusé : les fichiers à macros ne sont pas autorisés. "
        "Enregistrez-le au format .xlsx sans macros.",
    ),
    M.XLSX: (
        "unsupported_office_format",
        "Classeur Excel (.xlsx) non supporté. Exportez en CSV.",
    ),
    M.PPTM: (
        "macro_enabled_presentation_refused",
        "Présentation PowerPoint macro-enabled (.pptm) refusée : les fichiers à macros "
        "ne sont pas autorisés. Enregistrez-la au format .pptx sans macros.",
    ),
    M.PPTX: (
        "unsupported_office_format",
        "Présentation PowerPoint (.pptx) non supportée.",
    ),
    M.ODS: (
        "unsupported_spreadsheet_format",
        "Classeur OpenDocument (.ods) non supporté. Exportez-le au format CSV.",
    ),
    M.ODP: (
        "unsupported_presentation_format",
        "Présentation OpenDocument (.odp) non supportée. Enregistrez-la au format .pptx.",
    ),
    # `.odt` était détecté comme ZIP générique : l'utilisateur lisait « Archive (.zip)
    # non supportée : envoyez les fichiers individuellement », consigne absurde pour un
    # traitement de texte. Le format reste NON SUPPORTÉ — seul son NOM est corrigé.
    M.ODT: (
        "unsupported_text_document_format",
        "Document OpenDocument (.odt) non supporté. Enregistrez-le au format .docx.",
    ),
    M.LEGACY_OFFICE: (
        "legacy_office_format",
        "Format Office binaire ancien (.xls / .doc / .ppt) non supporté. "
        "Enregistrez le fichier dans un format moderne (.docx, ou .csv pour un tableur).",
    ),
    # Retombée quand DOCUMENT_MSG_ENABLED=0. Le message nomme Outlook et propose des
    # sorties réalistes pour un e-mail : surtout PAS « enregistrez en .docx ou .csv ».
    M.OUTLOOK_MSG: (
        "msg_ingestion_disabled",
        "Les fichiers Outlook (.msg) ne sont pas activés sur cet environnement. "
        "Exportez le message en .html ou .txt, ou contactez un administrateur.",
    ),
    # Retombée quand DOCUMENT_EML_ENABLED=0. Un `.eml` reconnu n'est
    # PLUS indexé comme texte brut (en-têtes de transport et base64 compris) : refus nommé.
    M.EML: (
        "eml_ingestion_disabled",
        "Les courriels (.eml) ne sont pas activés sur cet environnement. "
        "Contactez un administrateur.",
    ),
    # Retombée quand DOCUMENT_XML_ENABLED=0. Un `.xml` reconnu
    # n'est PLUS indexé comme texte brut : refus explicite, nommé.
    M.XML: (
        "xml_ingestion_disabled",
        "Les fichiers XML (.xml) ne sont pas activés sur cet environnement. "
        "Contactez un administrateur.",
    ),
    # Retombée quand DOCUMENT_DXF_ENABLED=0 : refus explicite, nommé.
    M.DXF: (
        "dxf_ingestion_disabled",
        "Les dessins DXF (.dxf) ne sont pas activés sur cet environnement. "
        "Contactez un administrateur.",
    ),
    # Retombée quand DOCUMENT_DWG_ENABLED=0 : refus explicite, nommé.
    M.DWG: (
        "dwg_ingestion_disabled",
        "Les dessins DWG (.dwg) ne sont pas activés sur cet environnement. "
        "Contactez un administrateur.",
    ),
    # Retombée quand DOCUMENT_IFC_ENABLED=0 : refus explicite, nommé.
    M.IFC: (
        "ifc_ingestion_disabled",
        "Les maquettes IFC (.ifc) ne sont pas activées sur cet environnement. "
        "Contactez un administrateur.",
    ),
    M.ZIP: (
        "archive_not_supported",
        "Archive (.zip) non supportée : envoyez les fichiers individuellement.",
    ),
    M.EXECUTABLE: (
        "executable_refused",
        "Fichier exécutable refusé pour raisons de sécurité.",
    ),
    M.OCTET: (
        "unsupported_file_type",
        "Type de fichier non reconnu ou non supporté.",
    ),
}


def _is_office_temporary_name(lower_name: str) -> bool:
    """Nom de verrou temporaire Office : basename préfixé par `~$`.

    Miroir backend de `frontend/src/app/documents/_lib/folderImport.ts::
    isOfficeTemporaryFilename`. Le frontend filtre par ergonomie (éviter un aller-retour
    réseau inutile) ; ici c'est la seule barrière qui compte, le frontend n'en étant pas
    une.

    Le basename, jamais le chemin : `dossier/~$a.docx` est un verrou, `rapport~$.docx`
    n'en est pas un — le marqueur est un PRÉFIXE. Les deux séparateurs sont gérés :
    `relative_path` arrive normalisé en `/`, mais `filename` vient du client.
    """
    if not lower_name:
        return False
    basename = re.split(r"[\\/]", lower_name)[-1]
    return basename.startswith("~$")


def _reject_message(detected: str, code: str, msg: str, ctx: ExtractionContext) -> str:
    """Affine le libellé d'un refus quand l'extension le permet.

    L'extension ne sert QU'À la formulation du message : elle n'autorise jamais rien
    et ne change jamais le code d'erreur. La signature OLE2 est commune à .xls/.doc/
    .ppt/.msg — on ne peut pas dire lequel c'est ; si l'utilisateur a nommé son
    fichier .xls, autant lui donner la consigne de conversion qui le concerne.
    """
    if detected == M.LEGACY_OFFICE and ctx.lower_name.endswith(".xls"):
        return (
            "Classeur Excel 97-2003 (.xls) non supporté. "
            "Enregistrez-le au format .xlsx ou exportez-le en CSV."
        )
    if detected == M.LEGACY_OFFICE and ctx.lower_name.endswith(".ppt"):
        return (
            "Présentation PowerPoint 97-2003 (.ppt) non supportée. "
            "Enregistrez-la au format .pptx."
        )
    return msg


def _effective_ocr_enabled(ctx: ExtractionContext) -> bool:
    if ctx.ocr_enabled is not None:
        return bool(ctx.ocr_enabled)
    return config.ocr_enabled()


# Mapping nom de format (DOCUMENT_IMAGE_FORMATS) -> MIME réel détecté.
_IMAGE_FORMAT_TO_MIME = {
    "jpeg": M.JPEG, "jpg": M.JPEG,
    "png": M.PNG,
    "tiff": M.TIFF, "tif": M.TIFF,
    "webp": M.WEBP,
    "gif": M.GIF,
    "bmp": M.BMP,
}


def _image_allowed_mimes() -> set:
    """MIME image autorisés, dérivés de l'allowlist DOCUMENT_IMAGE_FORMATS."""
    return {_IMAGE_FORMAT_TO_MIME[f] for f in config.image_formats() if f in _IMAGE_FORMAT_TO_MIME}


def check_size(size_bytes: int) -> None:
    limit = config.max_upload_size_bytes()
    if limit and size_bytes and int(size_bytes) > limit:
        raise RejectedFile(
            f"Fichier trop volumineux : {int(size_bytes)} octets (limite : {limit}). "
            "Ajustez DOCUMENT_MAX_UPLOAD_SIZE_MB si nécessaire.",
            code="file_too_large",
        )


def validate_and_classify(
    data: bytes, ctx: ExtractionContext, *, allow_excel: bool = True, allow_pptx: bool = True
) -> Tuple[str, str]:
    """Valide et classe un fichier. Renvoie (mime_réel, format_key) ou lève.

    ``allow_excel`` : porte SÉPARÉE de ``DOCUMENT_EXCEL_ENABLED``. Elle existe pour un
    appelant dont le chemin ne peut PAS encaisser un classeur — aujourd'hui le
    **corpus métier**, dont l'ingestion est 100 % synchrone (aucune tâche Celery) :
    un gros `.xlsx` y bloquerait une requête HTTP pendant plusieurs secondes. Le
    corpus passe donc ``allow_excel=False`` tant qu'il n'est pas asynchrone.

    ``allow_pptx`` : même porte, même motif, pour les présentations. Une
    présentation est elle aussi une archive compressée au nombre de formes imprévisible,
    et l'extraction est forcée en asynchrone hors corpus (cf. `async_policy`). Les deux
    portes restent SÉPARÉES : ce sont deux formats distincts, avec deux codes et deux
    consignes de conversion différentes.
    """
    data = data or b""

    # 0) Verrou temporaire Office (`~$rapport.docx`).
    #
    # Ces fichiers sont déjà refusés par le contenu (ni OOXML, ni OLE2 -> OCTET ->
    # `unsupported_file_type`). Le refus était donc correct, mais son MESSAGE était
    # trompeur : « Type de fichier non reconnu ou non supporté » laisse croire à un
    # format légitime que la plateforme ne gère pas, alors qu'il s'agit d'un résidu de
    # Word/Excel qui n'a jamais contenu de document. On le nomme.
    #
    # Décider un REFUS sur le nom ne contredit pas la doctrine « ne jamais faire confiance
    # à l'extension » : celle-ci interdit d'ACCEPTER sur le nom. Refuser ne peut que
    # rejeter davantage, jamais laisser passer. Contrepartie assumée : un vrai document
    # renommé `~$…` serait refusé — le message dit exactement quoi faire.
    if _is_office_temporary_name(ctx.lower_name):
        raise RejectedFile(
            "Fichier temporaire Microsoft Office (préfixe « ~$ ») : c'est un fichier de "
            "verrouillage, pas un document. Il ne contient aucun contenu à indexer.",
            code="office_temporary_file",
        )

    # 1) Taille (défense en profondeur : aussi appliquée hors couche HTTP).
    check_size(len(data))

    if not data:
        # cohérent avec le reste : un fichier vide n'est pas indexable
        raise RejectedFile("Fichier vide.", code="empty_file")

    # 2) Type réel
    detected = M.detect_mime(data, ctx)

    # 2 bis) Un détecteur structuré a échoué : refus FERMÉ. Laisser
    #    ce fichier retomber en `text/plain` indexerait à l'aveugle un contenu dont on
    #    ne sait rien. Le message ne cite jamais l'exception du détecteur.
    if detected == M.STRUCTURED_DETECTION_FAILED:
        raise RejectedFile(
            "La reconnaissance du format structuré a échoué : le fichier n'a pas été "
            "classé et n'est pas indexé.",
            code="structured_detection_failed",
        )

    # 3) Images (Lot 2) : porte DÉDIÉE, indépendante de l'OCR PDF global.
    #    Une image acceptée est OCRisée automatiquement (auto-OCR image).
    if detected in M.IMAGE_MIMES:
        if not config.image_ingest_enabled():
            raise RejectedFile(
                "L'ingestion d'images est désactivée sur cet environnement "
                "(DOCUMENT_IMAGE_INGEST_ENABLED=0).",
                code="image_ingest_disabled",
            )
        if detected not in _image_allowed_mimes():
            raise UnsupportedFormat(
                f"Format image non supporté ({detected}). Formats acceptés : "
                f"{', '.join(sorted(config.image_formats()))}.",
                code="image_unsupported_format",
            )
        return detected, FMT_IMAGE

    # 4) Excel (.xlsx) — porte DÉDIÉE, AVANT les refus explicites (XLSX y figure
    #    encore pour les cas où l'extraction est fermée). `.xlsm` n'arrive jamais ici :
    #    il est détecté par CONTENU comme M.XLSM et refusé plus bas (lot E0).
    if detected == M.XLSX:
        if not allow_excel:
            raise RejectedFile(
                "Les classeurs Excel ne sont pas acceptés dans le corpus métier. "
                "Exportez la feuille utile en CSV.",
                code="excel_not_supported_here",
            )
        if config.excel_enabled():
            return detected, FMT_EXCEL
        # DOCUMENT_EXCEL_ENABLED=0 -> on retombe sur le refus historique ci-dessous.

    # 4bis) PowerPoint (.pptx) — porte DÉDIÉE, strictement calquée sur celle d'Excel :
    #    placée AVANT les refus, sortie anticipée si ouverte, sinon retombée silencieuse
    #    sur le refus historique (M.PPTX est CONSERVÉ dans `_REJECT`) — ce qui permet un
    #    rollback par le seul flag, sans toucher au code.
    #    `.pptm` n'arrive jamais ici : il est détecté par CONTENU comme M.PPTM (marqueur
    #    macro) et refusé plus bas, exactement comme `.docm`/`.xlsm`.
    if detected == M.PPTX:
        if not allow_pptx:
            raise RejectedFile(
                "Les présentations PowerPoint ne sont pas acceptées dans le corpus métier. "
                "Exportez la présentation en PDF.",
                code="pptx_not_supported_here",
            )
        if config.pptx_enabled():
            return detected, FMT_PPTX
        # DOCUMENT_PPTX_ENABLED=0 -> on retombe sur le refus historique ci-dessous.

    # 4ter) Outlook (.msg) — porte DÉDIÉE, sur le patron d'Excel/PPTX : placée AVANT les
    #    refus, sortie anticipée si ouverte, sinon retombée sur `msg_ingestion_disabled`
    #    (M.OUTLOOK_MSG est présent dans `_REJECT`) — rollback par le seul flag.
    #
    #    Différence avec Excel/PPTX : la porte est fermée PAR DÉFAUT, et une seconde
    #    condition s'y ajoute — la présence effective du parseur. Annoncer le format
    #    puis échouer à l'extraction serait un faux succès ; on refuse en amont, avec un
    #    code distinct pour que l'exploitant distingue « désactivé » de « non déployé ».
    if detected == M.OUTLOOK_MSG:
        if not config.msg_enabled():
            code, msg = _REJECT[M.OUTLOOK_MSG]
            raise RejectedFile(msg, code=code)
        if not msg_parser_available():
            raise RejectedFile(
                "Les fichiers Outlook (.msg) ne sont pas pris en charge par RAGatm Open : "
                "exportez le message au format .eml.",
                code=MSG_UNSUPPORTED_CODE,
            )
        limit = config.msg_max_file_bytes()
        if limit and len(data) > limit:
            raise RejectedFile(
                f"Message Outlook trop volumineux : {len(data)} octets (limite : {limit}). "
                "Ajustez DOCUMENT_MSG_MAX_FILE_SIZE_MB si nécessaire.",
                code="msg_file_too_large",
            )
        return detected, FMT_MSG

    # 4ter-bis) Courriel MIME (.eml) —, patron d'Outlook : fermé par
    #    défaut, plafond propre. Le parseur est la bibliothèque standard (`email`) : aucune
    #    dépendance à vérifier. L'admission PAR ESPACE (`check_email_admission`) est faite
    #    par les chemins de dépôt, qui connaissent le RagSpace.
    if detected == M.EML:
        if not config.eml_enabled():
            code, msg = _REJECT[M.EML]
            raise RejectedFile(msg, code=code)
        limit = config.eml_max_file_bytes()
        if limit and len(data) > limit:
            raise RejectedFile(
                f"Courriel trop volumineux : {len(data)} octets (limite : {limit}). "
                "Ajustez DOCUMENT_EML_MAX_FILE_SIZE_MB si nécessaire.",
                code="eml_file_too_large",
            )
        return detected, FMT_EML

    # 4quater) XML générique (.xml) — porte DÉDIÉE sur le patron d'Outlook : fermée par
    #    défaut, seconde condition = parseur sécurisé déployé (refus distinct, nommé).
    #    La validation sécurisée (DTD/entités/limites) est faite par l'extracteur.
    if detected == M.XML:
        if not config.xml_enabled():
            code, msg = _REJECT[M.XML]
            raise RejectedFile(msg, code=code)
        if not xml_parser_available():
            raise RejectedFile(
                "L'ingestion XML est activée mais indisponible sur cet environnement "
                "(defusedxml absent). Contactez un administrateur.",
                code="xml_dependency_unavailable",
            )
        return detected, FMT_XML

    # 4quinquies) DXF (.dxf) — même patron : porte fermée par défaut, parseur déployé.
    #    La validation par le parseur (structure, version, limite d'entités) est faite
    #    par l'extracteur. Le Content-Type client (`image/vnd.dxf`…) n'a joué aucun rôle.
    if detected == M.DXF:
        if not config.dxf_enabled():
            code, msg = _REJECT[M.DXF]
            raise RejectedFile(msg, code=code)
        if not dxf_parser_available():
            raise RejectedFile(
                "L'ingestion DXF est activée mais indisponible sur cet environnement "
                "(ezdxf absent). Contactez un administrateur.",
                code="dxf_dependency_unavailable",
            )
        return detected, FMT_DXF

    # 4sexies) IFC (.ifc) — porte fermée par défaut. Contrairement à Outlook/XML/DXF, la
    # présence LOCALE du parseur n'est PAS exigée ici : l'admission
    #    se fait dans le processus web, alors que seul le worker dédié `ingest_ifc` porte
    #    l'extra `[ifc]` (image générale légère). Le flag est l'invariant de déploiement
    #    (« activer seulement si worker_ifc tourne ») ; l'extracteur, côté worker,
    #    refuse `ifc_dependency_unavailable` si la dépendance manque. La validation par
    #    le parseur (en-tête, schéma, limite d'entités) est faite par l'extracteur.
    if detected == M.IFC:
        if not config.ifc_enabled():
            code, msg = _REJECT[M.IFC]
            raise RejectedFile(msg, code=code)
        return detected, FMT_IFC

    # 4septies) DWG (.dwg) — porte fermée par défaut. Comme IFC : l'admission web ne
    #    dépend PAS de la présence locale du convertisseur (seule l'image `worker_dwg`
    #    porte LibreDWG) ; l'invariant de déploiement est le flag. La VERSION du dessin
    #    est en revanche décidée ici, avant toute création de Document : une famille hors
    #    périmètre est refusée nommément plutôt que convertie au hasard. La conversion
    #    elle-même (sous-processus, DXF temporaire) a lieu dans l'extracteur, côté worker.
    if detected == M.DWG:
        if not config.dwg_enabled():
            code, msg = _REJECT[M.DWG]
            raise RejectedFile(msg, code=code)
        if not is_supported_dwg_version(dwg_version(data)):
            raise RejectedFile(
                "Cette version de DWG n'est pas prise en charge. Enregistrez le dessin "
                "dans une version récente (AutoCAD 2000 à 2018) ou fournissez un DXF.",
                code="dwg_unsupported_version",
            )
        return detected, FMT_DWG

    # 4octies) Transcription canonique d'un document AUDIO. Aucune
    #    porte de drapeau ici : la porte DÉPÔT (`check_audio_admission`) a déjà été
    #    franchie, et un audio déjà transcrit doit rester réindexable drapeau fermé.
    from .audio_transcript_signature import AUDIO_TRANSCRIPT_MIME

    if detected == AUDIO_TRANSCRIPT_MIME:
        return detected, FMT_AUDIO

    # 5) Refus explicites (macro-enabled, Office non supporté, archive, exécutable…)
    if detected in _REJECT:
        code, msg = _REJECT[detected]
        raise RejectedFile(_reject_message(detected, code, msg, ctx), code=code)

    # 6) Allowlist
    fmt = _ALLOW.get(detected)
    if fmt is None:
        raise UnsupportedFormat(
            f"Type de fichier non supporté ({detected}).",
            code="unsupported_file_type",
        )
    return detected, fmt


def check_email_admission(format_key: str, rag_space_id) -> None:
    """Porte d'ESPACE des courriels, appelée par les chemins de DÉPÔT.

    Les portes de format (DOCUMENT_MSG_ENABLED, DOCUMENT_EML_ENABLED) sont globales à
    l'environnement : seules, elles ouvriraient les courriels à TOUS les RagSpace. Un
    courriel n'est donc admis que dans un espace listé par DOCUMENT_EMAIL_RAGSPACE_IDS
    (liste vide ou invalide = aucun). Les autres formats ne sont jamais concernés.

    Appliquée au DÉPÔT seulement : la réindexation d'un courriel déjà admis n'est pas
    refusée (son corps reste extrait ; ses pièces jointes ne le sont que si l'espace
    est admis — cf. `config.email_attachments_enabled_for`).
    """
    if format_key not in EMAIL_FORMATS:
        return
    if not config.email_admitted_for(rag_space_id):
        raise RejectedFile(
            "Les courriels (.msg, .eml) ne sont pas activés pour cet espace. "
            "Contactez un administrateur.",
            code="email_ingestion_not_enabled_for_space",
        )


def check_audio_admission(rag_space_id) -> None:
    """Porte de DÉPÔT de l'audio : drapeau ET espace listé.

    Distincte de la validation de format : un audio reconnu dans un espace non admis
    est refusé NOMMÉMENT (`audio_ingestion_disabled`), jamais présenté comme un format
    inconnu. Les droits d'écriture et les quotas restent exigés par l'appelant.
    """
    if not config.audio_admitted_for(rag_space_id):
        raise RejectedFile(
            "L'import audio n'est pas activé pour cet espace. Contactez un administrateur.",
            code="audio_ingestion_disabled",
        )
