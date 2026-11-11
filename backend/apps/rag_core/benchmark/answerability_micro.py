"""Micro-benchmark contrôlé de PRÉSENCE DE RÉPONSE — P2.4 (§9).

Distinct des 159 cas : chaque paire isole **une** capacité, pour savoir *pourquoi* un
mécanisme échoue, pas seulement *qu'il* échoue.

Ce que quatre lots ont établi : `bge-reranker-base` répond à « ce passage traite-t-il du
sujet », pas à « affirme-t-il la réponse ». Un titre suffisait à faire passer un cas
no-answer de 0.18 à 0.95. Ce micro-benchmark met les trois familles face aux mêmes pièges.

⚠️ 100 % SYNTHÉTIQUE : aucune donnée du corpus client.
"""
from __future__ import annotations

from dataclasses import dataclass

# Capacités testées — un cas qui échoue désigne une capacité manquante, pas un score.
EXPLICIT = "explicit_answer"          # la réponse est écrite noir sur blanc
TOPIC_ONLY = "topic_only"             # le sujet est là, la propriété demandée non
NEIGHBOR_VALUE = "neighbor_value"     # une valeur voisine appartient à un AUTRE objet
INVERSE_RELATION = "inverse_relation"  # la relation est inversée
NEGATION = "negation"                 # le passage nie
MULTI_SENTENCE = "multi_sentence"     # la réponse s'étend sur plusieurs phrases
TABLE = "table_answer"                # la réponse est dans un tableau
IMPLICIT = "implicit_entailment"      # déductible sans être écrite
PARTIAL = "partial_answer"            # une partie seulement de la demande
TWO_CHUNKS = "two_chunks_needed"      # deux passages nécessaires
TITLE_TRAP = "title_trap"             # le titre annonce le sujet, le corps ne répond pas


@dataclass(frozen=True)
class MicroCase:
    id: str
    capability: str
    question: str
    chunks: tuple[str, ...]
    answerable: bool
    note: str


CASES: tuple[MicroCase, ...] = (
    # ---- Présence explicite : le minimum vital ----
    MicroCase("m01", EXPLICIT, "Quel est le port local d'Ollama ?",
              ("Le service Ollama écoute localement sur le port 11434.",),
              True, "réponse littérale"),
    MicroCase("m02", EXPLICIT, "Où se configure la double authentification ?",
              ("La double authentification se configure dans les paramètres de sécurité.",),
              True, "réponse littérale"),

    # ---- Sujet présent, propriété absente : LE piège du cross-encoder ----
    MicroCase("m03", TOPIC_ONLY, "Quel est le port de staging d'Ollama ?",
              ("Ollama est utilisé localement sur le port 11434.",),
              False, "même entité, qualificatif absent"),
    MicroCase("m04", TOPIC_ONLY, "Quelle est la consommation électrique du serveur ?",
              ("Le serveur héberge les services d'intelligence artificielle et exécute "
               "les modèles de génération et d'embeddings.",),
              False, "entité présente, propriété jamais évoquée"),

    # ---- Valeur voisine appartenant à un autre objet ----
    MicroCase("m05", NEIGHBOR_VALUE, "Quel est le port de staging d'Ollama ?",
              ("AI Gateway écoute en staging sur le port 8200.",),
              False, "le qualificatif matche, l'entité non"),
    MicroCase("m06", NEIGHBOR_VALUE, "Quel est le port de production du frontend ?",
              ("Le frontend est servi en développement sur le port 6300. "
               "AI Gateway écoute en production sur le port 8100.",),
              False, "les deux moitiés existent, jamais leur conjonction"),

    # ---- Relation inversée ----
    MicroCase("m07", INVERSE_RELATION, "Quel service utilise le port 8200 ?",
              ("Le port 8100 est utilisé par Rag Core.",),
              False, "même forme, valeur différente"),

    # ---- Négation : le passage NIE la proposition ----
    MicroCase("m08", NEGATION, "Ollama utilise-t-il le port 11434 ?",
              ("Ollama n'utilise pas le port 11434.",),
              # Le passage TRANCHE la question : elle est répondable (par la négative).
              True, "contradiction explicite — répondable, pas absente"),
    MicroCase("m09", NEGATION, "Le service propose-t-il un mode hors-ligne ?",
              ("Le service requiert une connexion permanente et ne fonctionne pas sans réseau.",),
              True, "réponse négative explicite"),

    # ---- Réponse multi-phrase ----
    MicroCase("m10", MULTI_SENTENCE, "Comment réinitialiser son mot de passe ?",
              ("Un lien est envoyé par courriel. Il ouvre un formulaire de saisie d'un "
               "nouveau secret. Le lien expire après un usage unique.",),
              True, "réponse répartie sur trois phrases"),

    # ---- Réponse dans un tableau ----
    MicroCase("m11", TABLE, "Quel port utilise AI Gateway en staging ?",
              ("| Service | Environnement | Port |\n| --- | --- | --- |\n"
               "| AI Gateway | production | 8100 |\n| AI Gateway | staging | 8200 |",),
              True, "réponse en cellule de tableau"),
    MicroCase("m12", TABLE, "Quel port utilise Ollama en staging ?",
              ("| Service | Environnement | Port |\n| --- | --- | --- |\n"
               "| Ollama | local | 11434 |\n| AI Gateway | staging | 8200 |",),
              False, "juxtaposition : Ollama et staging présents, jamais ensemble"),

    # ---- Entailment implicite ----
    MicroCase("m13", IMPLICIT, "Les modèles tournent-ils sur la machine applicative ?",
              ("Toute charge d'inférence est déportée vers la machine dédiée à "
               "l'intelligence artificielle ; la machine applicative n'exécute aucun modèle.",),
              True, "déductible, formulée autrement"),

    # ---- Réponse partielle ----
    MicroCase("m14", PARTIAL, "Quels sont le port et l'adresse IP d'AI Gateway ?",
              ("AI Gateway écoute sur le port 8100.",),
              False, "une moitié seulement de la demande"),

    # ---- Deux chunks nécessaires ----
    MicroCase("m15", TWO_CHUNKS, "Quels sont le port et l'adresse d'AI Gateway ?",
              ("AI Gateway écoute sur le port 8100.",
               "AI Gateway est joignable à l'adresse <hôte-gateway>."),
              True, "réponse complète seulement en combinant les deux"),

    # ---- Le piège du titre : ce qui a fait échouer P2.1 → P2.3 ----
    MicroCase("m16", TITLE_TRAP, "Quel est le port de staging d'Ollama ?",
              ("# Fiche de configuration — service Ollama\n\n## Paramètre\n"
               "Le service Ollama écoute localement sur le port 11434.",),
              False, "titre thématique + réponse absente : bge y répondait 0.95"),
    MicroCase("m17", TITLE_TRAP, "Quel est le port local d'Ollama ?",
              ("# Fiche de configuration — service Ollama\n\n## Paramètre\n"
               "Le service Ollama écoute localement sur le port 11434.",),
              True, "même document, réponse présente : témoin du précédent"),
)


def by_capability() -> dict[str, list[MicroCase]]:
    out: dict[str, list[MicroCase]] = {}
    for c in CASES:
        out.setdefault(c.capability, []).append(c)
    return out


def title_pairs() -> list[tuple[MicroCase, MicroCase]]:
    """Paires (sans titre, avec titre) à contenu de réponse identique (§8).

    Un mécanisme robuste doit rendre le MÊME verdict : le titre n'ajoute aucune réponse.
    """
    idx = {c.id: c for c in CASES}
    return [(idx["m03"], idx["m16"]), (idx["m01"], idx["m17"])]
