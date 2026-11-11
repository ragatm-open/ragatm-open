"""Niveaux d'export et règles d'autorisation.

Le niveau n'est pas une préférence de confort : il décrit **ce qui sort de
production**. C'est pourquoi chaque montée de niveau ajoute une exigence
(motif, rôle, chiffrement) plutôt qu'une simple option.
"""
from __future__ import annotations

from dataclasses import dataclass

LEVEL_METADATA = 1
LEVEL_FRAGMENTS = 2
LEVEL_SOURCE_FILE = 3


@dataclass(frozen=True)
class ExportLevel:
    value: int
    label: str
    includes_text: bool
    includes_source_file: bool
    requires_justification: bool
    requires_superuser: bool
    description: str


LEVELS: dict[int, ExportLevel] = {
    LEVEL_METADATA: ExportLevel(
        value=LEVEL_METADATA,
        label="métadonnées",
        includes_text=False,
        includes_source_file=False,
        requires_justification=False,
        requires_superuser=False,
        description=(
            "Identifiants, rangs, scores, seuils, configuration effective. Aucun "
            "texte documentaire. Suffit à répondre à « le bon chunk était-il "
            "candidat, et pourquoi a-t-il été écarté ? »."
        ),
    ),
    LEVEL_FRAGMENTS: ExportLevel(
        value=LEVEL_FRAGMENTS,
        label="fragments ciblés",
        includes_text=True,
        includes_source_file=False,
        requires_justification=True,
        requires_superuser=False,
        description=(
            "Niveau 1 + extraits réellement injectés dans le prompt. Contenu "
            "métier : motif obligatoire et chiffrement obligatoire."
        ),
    ),
    LEVEL_SOURCE_FILE: ExportLevel(
        value=LEVEL_SOURCE_FILE,
        label="fichier source",
        includes_text=True,
        includes_source_file=True,
        requires_justification=True,
        requires_superuser=True,
        description=(
            "Niveau 2 + fichier d'origine. Réservé aux défauts d'EXTRACTION ou de "
            "CHUNKING, que par définition aucun extrait déjà découpé ne reproduit."
        ),
    ),
}


class ExportRefused(Exception):
    """Export refusé — motif manquant, rôle insuffisant, niveau inconnu."""


def get_level(value) -> ExportLevel:
    try:
        level = LEVELS[int(value)]
    except (KeyError, TypeError, ValueError):
        raise ExportRefused(f"Niveau d'export inconnu : {value!r}") from None
    return level


def check_authorisation(*, user, level_value: int, justification: str) -> ExportLevel:
    """Valide rôle et motif. Lève :class:`ExportRefused` sinon.

    La justification est exigée AVANT la génération, pas après : un paquet créé
    puis justifié laisserait une fenêtre où le contenu existe sans que personne
    n'ait eu à s'expliquer.
    """
    from apps.feedback.permissions import can_approve_export

    level = get_level(level_value)

    if not can_approve_export(user, level=level.value):
        raise ExportRefused(
            f"Niveau {level.value} ({level.label}) : autorisation insuffisante. "
            + (
                "Le fichier source complet est réservé au superuser."
                if level.requires_superuser
                else "Réservé à l'administration plateforme."
            )
        )

    if level.requires_justification and not (justification or "").strip():
        raise ExportRefused(
            f"Niveau {level.value} ({level.label}) : une justification écrite est obligatoire."
        )

    return level
